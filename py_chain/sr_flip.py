# -*- coding: utf-8 -*-
"""
支阻互换位逻辑（Python 移植版，与 .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js 对齐）

纯函数模块：基于各周期笔与K线识别「支阻互换位」并跨周期合并：
  - 强支阻互换位：价位被反复测试（触及次数 >= minTouch），之后价格突破该价位，角色互换
    （R2S 阻力转支撑 / S2R 支撑转阻力）
  - 近期极值位：最近若干根笔的 swing 端点（当前最直接的支撑/阻力参考，不要求触及次数，
    标记为 RES 阻力 / SUP 支撑）
  - 跨周期合并（mergeFlipsAcrossPeriods）、每周期候选上限截断（capPerPeriod）、
    按级别上下各取一个（pickByLevel）

不连接 CDP、不绘图；回测链路通过 compute_srflip 直接调用。
"""

from .chan_core import calcATR

# 参数（与 JS 默认值一致）
CLUSTER_ATR = 0.5        # 价位聚类阈值（×ATR）
MERGE_ATR = 0.5          # 跨周期合并阈值（×最小周期ATR）
RECENT_CLUSTER_ATR = 1.0  # 近期极值位聚类容差（×ATR）
TOUCH_WEIGHT = 0.6       # 强度评分：触及次数权重
BARS_WEIGHT = 0.4        # 强度评分：经过K线数量权重
MAX_DIST_ATR = 3.0       # 选取时距离上限（×本级别ATR）
MAX_PER_PERIOD = 50      # 每周期候选数量上限
SIDE_COUNT = 1           # 每级别当前价上/下各保留 1 个
RECENT_BI_COUNT = 20     # 近期极值位取最近 N 根笔

# 级别大小顺序（从大到小），用于取「最大级别」与可见范围判断
LEVEL_ORDER = ["1W", "W", "1D", "D", "240", "4H", "60", "1H", "15", "3"]

# 最少触及次数（按级别）：--min-touch 显式指定时全局覆盖
_MIN_TOUCH_DEFAULT = {"D": 4, "240": 4, "60": 4, "15": 3, "3": 8}


def minTouchFor(res, override=None):
    """最少触及次数（按级别）。"""
    if override:
        return override
    r = str(res).upper()
    return _MIN_TOUCH_DEFAULT.get(r, 4)


# ============================================================
# 支阻互换位识别算法（纯函数）
# ============================================================


def extractSwingPoints(bis):
    """从笔列表中提取 swing 高低转折点（去重）。
    笔首尾相连，每笔的终点就是一次转折；额外补第一笔的起点。
    @returns [{ price, time, kind }]，kind = 'high'（阻力高点）| 'low'（支撑低点）
    """
    points = []
    for i, bi in enumerate(bis):
        if i == 0:
            points.append({"price": bi["startPrice"], "time": bi["startTime"],
                           "kind": "low" if bi["type"] == "up" else "high"})
        points.append({"price": bi["endPrice"], "time": bi["endTime"],
                       "kind": "high" if bi["type"] == "up" else "low"})
    return points


def clusterPoints(points, tol):
    """价位聚类（单遍扫描）：按价格排序后，相邻价差 <= tol 的点并入同一「价位簇」。
    每个簇记录代表价（触及价均值）与全部触及点。
    @returns [{ price, touches:[{price,time,kind}] }]
    """
    sorted_ = sorted(points, key=lambda p: p["price"])
    clusters = []
    for p in sorted_:
        last = clusters[-1] if clusters else None
        if last is not None and p["price"] - last["price"] <= tol:
            last["touches"].append(p)
            last["price"] = sum(x["price"] for x in last["touches"]) / len(last["touches"])
        else:
            clusters.append({"price": p["price"], "touches": [p]})
    return clusters


def detectFlip(cluster, bars, tol):
    """判断某个价位簇是否构成「支阻互换位」。
    规则：
      1. 若首尾触及角色相反（先高后低 → R2S，先低后高 → S2R），说明价位已被双向测试、角色已反转；
      2. 若角色未反转（全部高点或全部低点），用突破判定：
         - 主导为阻力（高点多）：之后收盘价向上突破价位 → R2S
         - 主导为支撑（低点多）：之后收盘价向下跌破价位 → S2R
    @returns 互换位 { price, type, breakTime, touchCount, firstTouch, lastTouch } 或 None
    """
    touches = sorted(cluster["touches"], key=lambda t: t["time"])
    first = touches[0]
    last = touches[-1]
    price = cluster["price"]
    base = {"price": price, "touchCount": len(touches),
            "firstTouch": first["time"], "lastTouch": last["time"]}

    # 情况1：首尾角色相反（价位已被双向测试，角色已反转）
    if first["kind"] == "high" and last["kind"] == "low":
        return dict(base, type="R2S", breakTime=last["time"])
    if first["kind"] == "low" and last["kind"] == "high":
        return dict(base, type="S2R", breakTime=last["time"])

    # 情况2：角色未反转，用突破判定（收盘价有效穿越价位）
    highCount = sum(1 for t in touches if t["kind"] == "high")
    lowCount = sum(1 for t in touches if t["kind"] == "low")
    dominant = "resistance" if highCount >= lowCount else "support"
    lastTouch = last["time"]
    for bar in bars:
        if bar["time"] <= lastTouch:
            continue
        if dominant == "resistance" and bar["close"] > price + tol:
            return dict(base, type="R2S", breakTime=bar["time"], dominant=dominant)
        if dominant == "support" and bar["close"] < price - tol:
            return dict(base, type="S2R", breakTime=bar["time"], dominant=dominant)
    return None


def extractRecentExtremes(bis, count):
    """提取最近若干根笔的 swing 端点（近期极值位候选）。
    @returns [{ price, time, kind }]，kind = 'high' | 'low'
    """
    points = []
    start = max(0, len(bis) - count)
    for i in range(start, len(bis)):
        bi = bis[i]
        if i == 0:
            points.append({"price": bi["startPrice"], "time": bi["startTime"],
                           "kind": "low" if bi["type"] == "up" else "high"})
        points.append({"price": bi["endPrice"], "time": bi["endTime"],
                       "kind": "high" if bi["type"] == "up" else "low"})
    return points


def detectRecentFlip(cluster):
    """近期极值位的轻量判定：不要求触及次数（minTouch）。
    - 簇内同时存在高、低点 → 价位被双向测试，按首尾角色判定互换类型；
    - 否则按主导角色记为纯阻力 RES / 纯支撑 SUP（当前最直接的阻挡/承接位）。
    @returns { price, type, breakTime, touchCount, firstTouch, lastTouch, recent: true }
    """
    touches = sorted(cluster["touches"], key=lambda t: t["time"])
    first = touches[0]
    last = touches[-1]
    price = cluster["price"]
    base = {"price": price, "touchCount": len(touches),
            "firstTouch": first["time"], "lastTouch": last["time"], "recent": True}
    highCount = sum(1 for t in touches if t["kind"] == "high")
    lowCount = sum(1 for t in touches if t["kind"] == "low")
    if highCount > 0 and lowCount > 0:
        if first["kind"] == "high" and last["kind"] == "low":
            return dict(base, type="R2S", breakTime=last["time"])
        if first["kind"] == "low" and last["kind"] == "high":
            return dict(base, type="S2R", breakTime=last["time"])
    return dict(base, type="RES" if highCount >= lowCount else "SUP", breakTime=last["time"])


def countBarsPassing(price, bars, tol):
    """统计某价位带（price ± tol）被多少根 K 线覆盖/穿越（含影线）。"""
    n = 0
    for b in bars:
        if b["low"] <= price + tol and b["high"] >= price - tol:
            n += 1
    return n


def flipScore(f, group):
    """支阻位强度评分：score = 0.6 × norm(触及次数) + 0.4 × norm(经过K线数量)。
    同一级别候选集内 min-max 归一化。"""
    ts = [g["touchCount"] for g in group]
    bs = [g["barsPassed"] for g in group]
    tMin, tMax = min(ts), max(ts)
    bMin, bMax = min(bs), max(bs)
    normTouch = (f["touchCount"] - tMin) / (tMax - tMin) if tMax > tMin else 1
    normBars = (f["barsPassed"] - bMin) / (bMax - bMin) if bMax > bMin else 1
    return TOUCH_WEIGHT * normTouch + BARS_WEIGHT * normBars


def mergeFlipsAcrossPeriods(allFlips, tol):
    """跨周期合并：把各周期识别出的互换位按价格合并，价格相近的合成一个。
    合并后确定「主要来源级别」= 来源中最大的级别。
    @returns [{ price, type, touchCount, firstTouch, breakTime, sources:[...], level }]
    """
    all_ = []
    for res, flips in allFlips.items():
        for f in flips:
            item = dict(f)
            item["source"] = res
            all_.append(item)
    all_.sort(key=lambda f: f["price"])
    merged = []
    for f in all_:
        last = merged[-1] if merged else None
        if last is not None and f["price"] - last["price"] <= tol:
            prevTouch = last["touchCount"]
            totalTouch = prevTouch + f["touchCount"]
            # 价格按触及次数加权平均
            last["price"] = (last["price"] * prevTouch + f["price"] * f["touchCount"]) / totalTouch
            last["touchCount"] = totalTouch
            # 经过 K 线数量同样累加
            last["barsPassed"] = last.get("barsPassed", 0) + f.get("barsPassed", 0)
            if f["source"] not in last["sources"]:
                last["sources"].append(f["source"])
            last["firstTouch"] = min(last["firstTouch"], f["firstTouch"])
            last["breakTime"] = max(last["breakTime"], f["breakTime"])
            # 类型冲突（罕见）：以触及次数更多者为准
            if f["touchCount"] > prevTouch:
                last["type"] = f["type"]
        else:
            merged.append(dict(f, sources=[f["source"]]))
    # 确定每个合并项的主要来源级别 = 来源中最大的级别（大级别优先）
    for m in merged:
        m["level"] = dominantLevel(m["sources"])
        m.pop("source", None)
    return merged


def dominantLevel(sources):
    """从来源周期列表确定主要来源级别：取最大的级别（LEVEL_ORDER 中更靠前）。"""
    best = None
    for res in sources:
        if best is None or LEVEL_ORDER.index(res) < LEVEL_ORDER.index(best):
            best = res
    return best


def pickByLevel(merged, currentPrice, sideCount, maxDistAtr, periodAtrs):
    """每个级别只保留「当前价格上方最近的 N 个 + 下方最近的 N 个」支阻位。
    先限定距离范围（距当前价 ≤ maxDistAtr×本级别ATR），同一侧仍存在多个候选时，
    选「强度评分最高」的 N 个。"""
    byLevel = {}
    for f in merged:
        byLevel.setdefault(f["level"], []).append(f)
    result = []
    for level, group in byLevel.items():
        # 本级别距离上限 = maxDistAtr × 本级别ATR（无ATR时退回与当前价最近）
        levelAtr = periodAtrs.get(level)
        maxDist = maxDistAtr * levelAtr if levelAtr else float("inf")
        # 距离范围内先给同级别候选集计算强度评分（min-max 归一化）
        for f in group:
            f["score"] = flipScore(f, group)
        # 上方：>= 当前价 且在距离范围内，评分降序取前 sideCount
        above = sorted(
            [f for f in group if f["price"] >= currentPrice and f["price"] - currentPrice <= maxDist],
            key=lambda f: f["score"], reverse=True)[:sideCount]
        # 下方：< 当前价 且在距离范围内，评分降序取前 sideCount
        below = sorted(
            [f for f in group if f["price"] < currentPrice and currentPrice - f["price"] <= maxDist],
            key=lambda f: f["score"], reverse=True)[:sideCount]
        result.extend(above)
        result.extend(below)
    return result


def capPerPeriod(allFlips, maxPerPeriod):
    """每周期候选数量上限截断：每周期最多保留 maxPerPeriod 个候选。
    超出时按「强度评分降序」保留 Top N。"""
    if not allFlips:
        return allFlips
    if not maxPerPeriod or maxPerPeriod <= 0:
        return allFlips
    out = {}
    for res, group in allFlips.items():
        if not group or len(group) <= maxPerPeriod:
            out[res] = group
            continue
        scored = [dict(f, score=flipScore(f, group)) for f in group]
        scored.sort(key=lambda f: f["score"], reverse=True)
        out[res] = scored[:maxPerPeriod]
    return out


# ============================================================
# 汇总计算（供回测链路调用）
# ============================================================


def compute_srflip(periodBis, barsByPeriod, periods,
                   clusterAtr=CLUSTER_ATR, mergeAtr=MERGE_ATR,
                   recentClusterAtr=RECENT_CLUSTER_ATR,
                   maxDistAtr=MAX_DIST_ATR, maxPerPeriod=MAX_PER_PERIOD,
                   minTouchOverride=None, periodAtrsIn=None):
    """逐周期识别支阻互换位并跨周期合并、按级别选取。

    @param periodBis    各周期笔 { 周期: [bis] }
    @param barsByPeriod 各周期原始K线 { 周期: [bars] }
    @param periods      周期列表（从大到小）
    @param periodAtrsIn 可选：各周期预计算 ATR { 周期: atr }（增量回测用，避免重复计算）
    @returns { periods: 各周期候选(截断后), merged: 跨周期合并结果, drawn: 按级别选取结果,
               currentPrice: 当前价, periodAtrs: 各周期ATR }
    """
    periodAtrsIn = periodAtrsIn or {}
    allFlips = {}
    periodAtrs = {}
    lastCloseByRes = {}
    for res in periods:
        bis = periodBis.get(res, []) or []
        if not bis or len(bis) < 3:
            continue
        bars = barsByPeriod.get(res, []) or []
        if not bars:
            continue
        lastCloseByRes[res] = bars[-1]["close"]
        atr = periodAtrsIn.get(res)
        if atr is None:
            atr = calcATR(bars, 14)
        periodAtrs[res] = atr
        tol = clusterAtr * atr
        minTouch = minTouchFor(res, minTouchOverride)

        # 强支阻互换位
        swingPoints = extractSwingPoints(bis)
        clusters = clusterPoints(swingPoints, tol)
        flips = []
        for c in clusters:
            if len(c["touches"]) < minTouch:
                continue
            flip = detectFlip(c, bars, tol)
            if flip:
                flip["barsPassed"] = countBarsPassing(flip["price"], bars, tol)
                flips.append(flip)

        # 近期极值位（更宽的聚类容差，不要求触及次数）
        recentPoints = extractRecentExtremes(bis, RECENT_BI_COUNT)
        recentClusters = clusterPoints(recentPoints, recentClusterAtr * atr)
        recentFlips = []
        for c in recentClusters:
            r = detectRecentFlip(c)
            if r:
                r["barsPassed"] = countBarsPassing(r["price"], bars, tol)
                recentFlips.append(r)

        allFlips[res] = flips + recentFlips

    # 每周期候选数量上限（数据层截断）
    allFlipsCapped = capPerPeriod(allFlips, maxPerPeriod)

    # 当前价格：用最小有数据周期的最后一根K线收盘价（各周期收盘价接近，取最小周期最精确）
    currentPrice = None
    for k in ("3", "15", "60", "240", "D"):
        if k in lastCloseByRes:
            currentPrice = lastCloseByRes[k]
            break

    # 跨周期合并：合并容差按「最小有数据的周期 ATR」缩放
    atrValues = [periodAtrs[r] for r in periods
                 if r in allFlipsCapped and allFlipsCapped[r] and r in periodAtrs]
    minAtr = min(atrValues) if atrValues else 0
    mergeTol = mergeAtr * minAtr
    mergedFlips = mergeFlipsAcrossPeriods(allFlipsCapped, mergeTol)

    # 每个级别只保留当前价「上方 1 个 + 下方 1 个」
    drawnFlips = pickByLevel(mergedFlips, currentPrice, SIDE_COUNT, maxDistAtr, periodAtrs) \
        if currentPrice is not None else mergedFlips

    return {
        "periods": allFlipsCapped,
        "merged": mergedFlips,
        "drawn": drawnFlips,
        "currentPrice": currentPrice,
        "periodAtrs": periodAtrs,
    }
