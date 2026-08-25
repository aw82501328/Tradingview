# -*- coding: utf-8 -*-
"""
缠论算法核心（Python 移植版，与 .cursor/skills/chan-core/scripts/chan_core.js 逐函数对齐）

纯函数模块，不依赖任何行情/交易框架。供 VN.PY 策略与对齐测试复用。

与 JS 版约定一致：
  - 笔对象字段：type(up/down)、startIdx/endIdx(合并K线索引)、startTime/endTime(校准后端点时间)、
    startPrice/endPrice、rawCount(覆盖原始K线数)、span(幅度)、gapLocked(跳空成笔)、macdCross(MACD变色成笔)
  - 配置：CHAN_CFG["gapFilter"]（跳空独立成笔阈值，默认 1.0）、CHAN_CFG["debug"]（调试打印）
  - 所有时间均为 Unix 秒（UTC），与 TradingView K线时间一致

用法：
    import chan_core
    chan_core.CHAN_CFG["debug"] = False
    chan_core.CHAN_CFG["gapFilter"] = 1.0
    merged = chan_core.mergeBars(rawBars)
    ...
"""

from datetime import datetime

# ============================================================
# 配置
# ============================================================

CHAN_CFG = {
    "gapFilter": 1.0,  # 跳空独立成笔阈值：相邻K线缺口 >= gapFilter*ATR 时强制独立成笔
    "debug": False,    # 调试打印（buildBi / 买卖点识别过程）
}

# ============================================================
# 1. 包含关系处理（合并K线）
# ============================================================


def mergeBars(rawBars):
    """包含关系处理（合并K线）。与 JS 版 mergeBars 对齐。"""
    merged = []
    direction = 0
    for bar in rawBars:
        if len(merged) == 0:
            m = dict(bar)
            m["_rawCount"] = 1
            m["highTime"] = bar["time"]
            m["lowTime"] = bar["time"]
            merged.append(m)
            continue
        last = merged[-1]
        containUp = bar["high"] >= last["high"] and bar["low"] <= last["low"]
        containDown = bar["high"] <= last["high"] and bar["low"] >= last["low"]
        hasContain = containUp or containDown
        if hasContain:
            d = direction
            if d == 0 and len(merged) >= 2:
                d = 1 if last["high"] >= merged[-2]["high"] else -1
            if d == 0:
                d = 1
            if d == 1:
                if bar["high"] > last["high"]:
                    last["high"] = bar["high"]
                    last["highTime"] = bar["time"]
                if bar["low"] > last["low"]:
                    last["low"] = bar["low"]
                    last["lowTime"] = bar["time"]
            else:
                if bar["high"] < last["high"]:
                    last["high"] = bar["high"]
                    last["highTime"] = bar["time"]
                if bar["low"] < last["low"]:
                    last["low"] = bar["low"]
                    last["lowTime"] = bar["time"]
            last["_rawCount"] += 1
            last["time"] = bar["time"]
            direction = d
        else:
            direction = 1 if bar["high"] > last["high"] else -1
            m = dict(bar)
            m["_rawCount"] = 1
            m["highTime"] = bar["time"]
            m["lowTime"] = bar["time"]
            merged.append(m)
    return merged


# ============================================================
# 2. 分型识别
# ============================================================


def findFractals(merged):
    """顶/底分型识别。与 JS 版 findFractals 对齐。"""
    fractals = []
    for i in range(1, len(merged) - 1):
        prev = merged[i - 1]
        cur = merged[i]
        nxt = merged[i + 1]
        if cur["high"] > prev["high"] and cur["high"] > nxt["high"] and cur["low"] > prev["low"] and cur["low"] > nxt["low"]:
            fractals.append({"mergedIdx": i, "type": "top", "high": cur["high"], "low": cur["low"], "time": cur["highTime"]})
        if cur["low"] < prev["low"] and cur["low"] < nxt["low"] and cur["high"] < prev["high"] and cur["high"] < nxt["high"]:
            fractals.append({"mergedIdx": i, "type": "bottom", "high": cur["high"], "low": cur["low"], "time": cur["lowTime"]})
    return fractals


# ============================================================
# 3. 笔构建辅助函数
# ============================================================


def countRaw(merged, startIdx, endIdx):
    """统计 (startIdx, endIdx] 覆盖的原始K线数。"""
    t = 0
    for k in range(startIdx + 1, endIdx + 1):
        t += merged[k]["_rawCount"]
    return t


def hasGapBetween(merged, aIdx, bIdx, atr, gapFilter):
    """检测两个分型（合并K线索引区间）之间是否存在跳空缺口。"""
    th = atr * gapFilter
    for i in range(aIdx, bIdx):
        cur = merged[i]
        nxt = merged[i + 1]
        gapUp = nxt["low"] - cur["high"]
        gapDown = cur["low"] - nxt["high"]
        if gapUp >= th or gapDown >= th:
            return True
    return False


# ============================================================
# 4. 笔构建（交替分型序列 + 回溯替换）
# ============================================================


def buildBi(fractals, merged, atr, macdArr):
    """笔构建。与 JS 版 buildBi 对齐。"""
    gapThreshold = atr * CHAN_CFG["gapFilter"] if atr else 0

    # 阶段一：严格交替分型序列
    seq = []
    for f in fractals:
        if len(seq) == 0:
            seq.append(f)
            continue
        last = seq[-1]
        if f["type"] == last["type"]:
            if f["type"] == "top":
                if f["high"] >= last["high"]:
                    seq[-1] = f
            else:
                if f["low"] <= last["low"]:
                    seq[-1] = f
        else:
            seq.append(f)

    def isValid(a, b):
        gap = b["mergedIdx"] - a["mergedIdx"]
        if gap < 4:
            return False
        return countRaw(merged, a["mergedIdx"], b["mergedIdx"]) >= 5

    def noMoreExtremeInside(a, b):
        for i in range(a["mergedIdx"] + 1, b["mergedIdx"]):
            if b["type"] == "bottom" and merged[i]["low"] < b["low"]:
                return False
            if b["type"] == "top" and merged[i]["high"] > b["high"]:
                return False
        return True

    def fractalRangeClear(a, b):
        i = a["mergedIdx"]
        rangeLow = min(merged[i - 1]["low"], merged[i]["low"], merged[i + 1]["low"])
        rangeHigh = max(merged[i - 1]["high"], merged[i]["high"], merged[i + 1]["high"])
        if a["type"] == "top" and b["type"] == "bottom":
            return b["low"] < rangeLow
        if a["type"] == "bottom" and b["type"] == "top":
            return b["high"] > rangeHigh
        return True

    if CHAN_CFG["debug"]:
        def ft(s):
            v = s["high"] if s["type"] == "top" else s["low"]
            return f"{'顶' if s['type']=='top' else '底'}@{s['mergedIdx']}({v})"
        print("[阶段一] 交替分型序列:", " → ".join(ft(s) for s in seq))

    # 阶段二：移除间隔不足的中间分型（回溯替换）
    result = []
    for k in seq:
        if len(result) == 0:
            result.append(k)
            continue
        last = result[-1]
        if k["type"] == last["type"]:
            if not last.get("gapLocked", False):
                if k["type"] == "top":
                    if k["high"] >= last["high"]:
                        result[-1] = k
                else:
                    if k["low"] <= last["low"]:
                        result[-1] = k
            else:
                # 跳空锁定的端点：仅当后续同类型分型「突破」锁定价格时才解锁替换
                if k["type"] == "top":
                    if k["high"] > last["high"]:
                        result[-1] = k
                else:
                    if k["low"] < last["low"]:
                        result[-1] = k
            continue
        # 异类型
        # MACD 变色成笔端点让位
        if len(result) >= 2:
            prev2 = result[-2]
            if prev2.get("macdCross", False) is True and prev2["type"] == k["type"] and \
               ((k["type"] == "top" and k["high"] > prev2["high"]) or
                (k["type"] == "bottom" and k["low"] < prev2["low"])):
                if CHAN_CFG["debug"]:
                    print(f"[阶段二] MACD端点让位: {prev2['mergedIdx']} -> {k['mergedIdx']}")
                k["macdCross"] = True
                result[-2] = k
                result.pop()
                continue
        # 跳空优先
        hasGap = gapThreshold > 0 and hasGapBetween(merged, last["mergedIdx"], k["mergedIdx"], atr, CHAN_CFG["gapFilter"])
        if hasGap:
            if CHAN_CFG["debug"]:
                print(f"[阶段二] 跳空成笔: {last['mergedIdx']} -> {k['mergedIdx']}")
            k["gapLocked"] = True
            result.append(k)
            continue
        # 前顶/前底作废
        if len(result) >= 3:
            prev3 = result[-3]
            prev2 = result[-2]
            lastMoreExtremeThanPrev3 = \
                (prev3["type"] == "top" and last["high"] > prev3["high"]) or \
                (prev3["type"] == "bottom" and last["low"] < prev3["low"])
            shallow = True
            if prev2["type"] == "top":
                rise = prev2["high"] - prev3["low"]
                pull = prev2["high"] - last["low"]
                shallow = pull < rise * 0.5
            else:
                drop = prev3["high"] - prev2["low"]
                bounce = last["high"] - prev2["low"]
                shallow = bounce < drop * 0.5
            if prev2["type"] == k["type"] and \
               not isValid(prev2, last) and \
               not lastMoreExtremeThanPrev3 and \
               shallow and \
               last.get("macdCross", False) is True and last.get("macdRaw", 0) < 5 and \
               ((k["type"] == "top" and k["high"] > prev2["high"]) or
                (k["type"] == "bottom" and k["low"] < prev2["low"])):
                if CHAN_CFG["debug"]:
                    print(f"[阶段二] 前顶/前底作废: {prev2['mergedIdx']} 被 {k['mergedIdx']} 突破")
                if prev2.get("macdCross", False) is True:
                    k["macdCross"] = True
                result[-2] = k
                result.pop()
                continue
        if isValid(last, k) and (noMoreExtremeInside(last, k) or last.get("gapLocked", False)) and \
           (fractalRangeClear(last, k) or last.get("gapLocked", False)):
            result.append(k)
        elif isValid(last, k):
            if CHAN_CFG["debug"]:
                print(f"[阶段二] 忽略 k: {k['mergedIdx']}")
        else:
            macdCross = bool(macdArr) and hasMacdCrossBetween(macdArr, merged, last["mergedIdx"], k["mergedIdx"], last["time"], k["time"])
            macdRawCount = countRaw(merged, last["mergedIdx"], k["mergedIdx"])
            if macdCross and macdRawCount >= 4 and noMoreExtremeInside(last, k):
                if CHAN_CFG["debug"]:
                    print(f"[阶段二] MACD变色成笔: {last['mergedIdx']} -> {k['mergedIdx']} (原始K线{macdRawCount}>=4)")
                k["macdCross"] = True
                k["macdRaw"] = macdRawCount
                result.append(k)
            else:
                if len(result) >= 2 and result[-2]["type"] == k["type"]:
                    prev = result[-2]
                    moreExtreme = k["high"] >= prev["high"] if k["type"] == "top" else k["low"] <= prev["low"]
                    gapPrevLast = last["mergedIdx"] - prev["mergedIdx"]
                    gapPrevK = k["mergedIdx"] - prev["mergedIdx"]
                    if CHAN_CFG["debug"]:
                        print(f"[阶段二] 间隔不足: {k['mergedIdx']} 与 {last['mergedIdx']}, moreExtreme={moreExtreme}, gapPrevLast={gapPrevLast}")
                    if moreExtreme and (gapPrevLast <= 12 or gapPrevK >= 4):
                        result[-2] = k
                        result.pop()

    if CHAN_CFG["debug"]:
        def ft2(s):
            v = s["high"] if s["type"] == "top" else s["low"]
            return f"{'顶' if s['type']=='top' else '底'}@{s['mergedIdx']}({v})"
        print("[阶段二] 结果序列:", " → ".join(ft2(s) for s in result))

    # 阶段三：两两连笔
    bis = []
    for i in range(0, len(result) - 1):
        a = result[i]
        b = result[i + 1]
        startPrice = a["high"] if a["type"] == "top" else a["low"]
        endPrice = b["high"] if b["type"] == "top" else b["low"]
        isUp = b["type"] == "top"
        bis.append({
            "type": "up" if isUp else "down",
            "startIdx": a["mergedIdx"],
            "endIdx": b["mergedIdx"],
            "startTime": a["time"],
            "endTime": b["time"],
            "startPrice": startPrice,
            "endPrice": endPrice,
            "rawCount": countRaw(merged, a["mergedIdx"], b["mergedIdx"]),
            "span": abs(endPrice - startPrice),
            "gapLocked": b.get("gapLocked", False) is True,
            "macdCross": b.get("macdCross", False) is True,
        })
    return bis


# ============================================================
# 5. ATR / MACD
# ============================================================


def calcATR(rawBars, period=14):
    """计算 ATR（最近 period 根 TR 的简单平均）。"""
    trs = []
    for i in range(1, len(rawBars)):
        h = rawBars[i]["high"]
        l = rawBars[i]["low"]
        pc = rawBars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    start = max(0, len(trs) - period)
    sl = trs[start:]
    if len(sl) == 0:
        return 0
    s = 0.0
    for x in sl:
        s += x
    return s / len(sl)


def calcMACD(rawBars):
    """计算 MACD（EMA12/EMA26/DIF/DEA）。macd>0 红柱，macd<0 绿柱。"""
    if not rawBars or len(rawBars) < 2:
        return []
    closes = [b["close"] for b in rawBars]

    def ema(period):
        k = 2 / (period + 1)
        out = []
        prev = closes[0]
        out.append(prev)
        for i in range(1, len(closes)):
            prev = closes[i] * k + prev * (1 - k)
            out.append(prev)
        return out

    ema12 = ema(12)
    ema26 = ema(26)
    dif = [ema12[i] - ema26[i] for i in range(len(closes))]
    dea = []
    prevDea = dif[0]
    dea.append(prevDea)
    for i in range(1, len(dif)):
        prevDea = dif[i] * (2 / (9 + 1)) + prevDea * (1 - (2 / (9 + 1)))
        dea.append(prevDea)
    return [
        {"time": b["time"], "macd": (dif[i] - dea[i]) * 2, "dif": dif[i], "dea": dea[i]}
        for i, b in enumerate(rawBars)
    ]


# ============================================================
# 6. MACD 背驰判定
# ============================================================


def fmtT(ts):
    """时间格式化（本地时区，仅供调试打印）。"""
    dt = datetime.fromtimestamp(ts)
    return f"{dt.month}-{dt.day} {dt.hour:02d}:{dt.minute:02d}"


def biMacdMetrics(bi, macdArr):
    """计算一笔区间内的 MACD 动能指标 { redArea, greenArea, difHigh, difLow }。"""
    metrics = {"redArea": 0.0, "greenArea": 0.0, "difHigh": float("-inf"), "difLow": float("inf")}
    if not macdArr or len(macdArr) == 0:
        return None
    t0 = bi["startTime"]
    t1 = bi["endTime"]
    found = False
    for m in macdArr:
        if m["time"] < t0:
            continue
        if m["time"] > t1:
            break
        found = True
        if m["macd"] > 0:
            metrics["redArea"] += m["macd"]
        else:
            metrics["greenArea"] += -m["macd"]
        if m["dif"] > metrics["difHigh"]:
            metrics["difHigh"] = m["dif"]
        if m["dif"] < metrics["difLow"]:
            metrics["difLow"] = m["dif"]
    if not found:
        return None
    return metrics


def isBiDiverge(bi, refer, macdArr):
    """MACD 背驰判定（OR 关系，满足其一即算背驰）。"""
    cur = biMacdMetrics(bi, macdArr)
    ref = biMacdMetrics(refer, macdArr)
    if cur is None or ref is None:
        return False
    if bi["type"] == "down":
        return cur["greenArea"] < ref["greenArea"] or cur["difLow"] > ref["difLow"]
    return cur["redArea"] < ref["redArea"] or cur["difHigh"] < ref["difHigh"]


# ============================================================
# 7. MACD 红绿转换检测
# ============================================================


def hasMacdCrossBetween(macdArr, merged, aIdx, bIdx, aTime, bTime):
    """检测两个分型之间是否发生 MACD 红绿转换（用分型极值时间作边界）。"""
    if not macdArr or len(macdArr) == 0:
        return False
    t0 = aTime if aTime is not None else merged[aIdx]["time"]
    t1 = bTime if bTime is not None else merged[bIdx]["time"]
    prev = None
    for mm in macdArr:
        if mm["time"] < t0:
            continue
        if mm["time"] > t1:
            break
        if prev is not None:
            crossed = (prev["macd"] >= 0 and mm["macd"] < 0) or (prev["macd"] <= 0 and mm["macd"] > 0)
            if crossed:
                return True
        prev = mm
    return False


# ============================================================
# 8. 未完成笔延伸 / 周期映射 / 端点校准
# ============================================================


def extendLastBi(bisArr, bars):
    """未完成笔延伸：最后一笔推进到最新极端价。"""
    if not bisArr or len(bisArr) == 0:
        return bisArr
    last = bisArr[-1]
    if last.get("gapLocked", False):
        return bisArr
    startIdx = -1
    for i, k in enumerate(bars):
        if k["time"] >= last["startTime"]:
            startIdx = i
            break
    if startIdx == -1:
        return bisArr
    tail = bars[startIdx:]
    if len(tail) < 2:
        return bisArr

    if last["type"] == "up":
        maxBar = tail[0]
        for k in tail:
            if k["high"] > maxBar["high"]:
                maxBar = k
        if maxBar["time"] > last["endTime"] and maxBar["high"] > last["endPrice"]:
            last["endTime"] = maxBar["time"]
            last["endPrice"] = maxBar["high"]
            last["span"] = maxBar["high"] - last["startPrice"]
    else:
        minBar = tail[0]
        for k in tail:
            if k["low"] < minBar["low"]:
                minBar = k
        if minBar["time"] > last["endTime"] and minBar["low"] < last["endPrice"]:
            last["endTime"] = minBar["time"]
            last["endPrice"] = minBar["low"]
            last["span"] = last["startPrice"] - minBar["low"]
    return bisArr


def lowerResOf(res):
    """逐级校准映射：15分钟←3分钟，1小时←15分钟，4小时←1小时，日线←4小时。"""
    s = str(res).upper()
    if s == "D" or s == "1D":
        return "240"
    if s == "240" or s == "4H":
        return "60"
    if s == "60" or s == "1H":
        return "15"
    if s == "15":
        return "3"
    return None


def calibrateBiTimes(bis, bigBars, refBars, bigIntervalSec):
    """跨周期端点时间校准：用低一级周期K线校准端点时间。"""
    if not bis or len(bis) == 0 or not refBars or len(refBars) == 0:
        return bis
    eps = 0.001

    def calibrateTime(t, price):
        big = None
        for k in bigBars:
            if k["time"] <= t and t < k["time"] + bigIntervalSec:
                big = k
                break
        if big is None:
            return t
        rangeEnd = big["time"] + bigIntervalSec
        best = None
        for rb in refBars:
            if rb["time"] < big["time"] or rb["time"] >= rangeEnd:
                continue
            if abs(rb["high"] - price) < eps or abs(rb["low"] - price) < eps:
                best = rb
        return best["time"] if best is not None else t

    for b in bis:
        b["startTime"] = calibrateTime(b["startTime"], b["startPrice"])
        b["endTime"] = calibrateTime(b["endTime"], b["endPrice"])
    return bis


def intervalSecOf(res):
    """周期 → 单根K线时长（秒）。"""
    r = str(res).upper()
    if r == "3":
        return 180
    if r == "5":
        return 300
    if r == "15":
        return 900
    if r == "30":
        return 1800
    if r == "60" or r == "1H":
        return 3600
    if r == "240" or r == "4H":
        return 14400
    if r == "D" or r == "1D":
        return 86400
    if r == "W" or r == "1W":
        return 604800
    return 0


# ============================================================
# 9. 买卖点识别
# ============================================================


def _findIndex(arr, pred):
    for i, x in enumerate(arr):
        if pred(x):
            return i
    return -1


def isSameAsUpperBi(bi, upperBis, barSec):
    """判断本周期某笔是否与上一级别某笔完全重合（时间容差 = 本周期 1 个 bar）。"""
    if not upperBis or len(upperBis) == 0:
        return False
    tEps = barSec if barSec else 900
    pEps = 0.01
    for ub in upperBis:
        if ub["type"] != bi["type"]:
            continue
        if abs(ub["startTime"] - bi["startTime"]) <= tEps and \
           abs(ub["endTime"] - bi["endTime"]) <= tEps and \
           abs(ub["startPrice"] - bi["startPrice"]) <= pEps and \
           abs(ub["endPrice"] - bi["endPrice"]) <= pEps:
            return True
    return False


def anchorFirstBuy(cand, upperBis):
    """一买锚定：取候选一买之前最近的上级底部端点。"""
    if not upperBis or len(upperBis) == 0:
        return None
    best = None
    for b in upperBis:
        t = b["startTime"] if b["type"] == "up" else b["endTime"]
        p = b["startPrice"] if b["type"] == "up" else b["endPrice"]
        if t > cand["time"]:
            continue
        if best is None or cand["time"] - t < cand["time"] - best["time"]:
            best = {"time": t, "price": p}
    return best


def anchorFirstSell(cand, upperBis):
    """一卖锚定：候选在上级上涨笔内则上移到其结束点，否则取最近上级顶部端点。"""
    if not upperBis or len(upperBis) == 0:
        return None
    for b in upperBis:
        if b["type"] != "up":
            continue
        if b["startTime"] <= cand["time"] and b["endTime"] >= cand["time"]:
            return {"time": b["endTime"], "price": b["endPrice"]}
    best = None
    for b in upperBis:
        t = b["endTime"] if b["type"] == "up" else b["startTime"]
        p = b["endPrice"] if b["type"] == "up" else b["startPrice"]
        if t > cand["time"]:
            continue
        if best is None or cand["time"] - t < cand["time"] - best["time"]:
            best = {"time": t, "price": p}
    return best


def snapToOwnBar(price, refTime, bars):
    """把极值价格/时间映射到本周期K线的 bar 边界。"""
    eps = 0.001
    best = None
    bestDist = float("inf")
    for k in bars:
        if abs(k["high"] - price) < eps or abs(k["low"] - price) < eps:
            d = abs(k["time"] - refTime)
            if d < bestDist:
                bestDist = d
                best = k["time"]
    if best is not None:
        return best
    nearest = bars[0]["time"] if len(bars) > 0 else refTime
    nd = float("inf")
    for k in bars:
        d = abs(k["time"] - refTime)
        if d < nd:
            nd = d
            nearest = k["time"]
    return nearest


def findBuyPoints(bis, upperBis, macdArr, barSec):
    """买点识别（含区间套与 MACD 背驰）。"""
    if len(bis) < 3:
        return []
    downIdx = [i for i, b in enumerate(bis) if b["type"] == "down"]

    # 候选一买：创新低 + MACD 背驰
    firstBuys = []
    for k in range(1, len(downIdx)):
        cur = bis[downIdx[k]]
        if isSameAsUpperBi(cur, upperBis, barSec):
            if CHAN_CFG["debug"]:
                print(f"[一买跳过-与上级笔重合] {fmtT(cur['endTime'])}({cur['endPrice']}) 整笔与上一级别完全重合，本周期不标记")
            continue
        refer = None
        for j in range(k - 1, -1, -1):
            cand = bis[downIdx[j]]
            if cand["span"] < cur["span"] * 0.5:
                continue
            refer = cand
            break
        if refer is not None and cur["endPrice"] < refer["endPrice"]:
            diverge = isBiDiverge(cur, refer, macdArr)
            if CHAN_CFG["debug"]:
                cm = biMacdMetrics(cur, macdArr)
                rm = biMacdMetrics(refer, macdArr)
                print(
                    f"[一买候选] {fmtT(cur['endTime'])}({cur['endPrice']}) vs 参照 {fmtT(refer['endTime'])}({refer['endPrice']}) "
                    f"| 创新低={cur['endPrice'] < refer['endPrice']} "
                    f"| 绿柱面积 {cm['greenArea']:.2f} vs {rm['greenArea']:.2f} "
                    f"| DIF低点 {cm['difLow']:.3f} vs {rm['difLow']:.3f} | 背驰={diverge}"
                )
            if diverge:
                firstBuys.append({"biIdx": downIdx[k], "time": cur["endTime"], "price": cur["endPrice"]})
    firstBuy = firstBuys[-1] if len(firstBuys) > 0 else None

    points = []

    # 2买 / 类2买（区间套）
    if upperBis is not None and len(upperBis) > 0:
        for up in upperBis:
            if up["type"] != "up":
                continue
            lows = []
            for i, b in enumerate(bis):
                if b["type"] != "down":
                    continue
                if b["endTime"] >= up["startTime"] and b["endTime"] <= up["endTime"] + 1:
                    lows.append({"biIdx": i, "time": b["endTime"], "price": b["endPrice"]})
            if len(lows) == 0:
                continue
            lows.sort(key=lambda x: x["time"])
            firstLow = next((l for l in lows if l["price"] > up["startPrice"]), None)
            if firstLow is not None:
                points.append({"type": "2买", "time": firstLow["time"], "price": firstLow["price"]})
                laterHigh = next((l for l in lows if l["time"] > firstLow["time"] and l["price"] > firstLow["price"]), None)
                if laterHigh is not None:
                    points.append({"type": "类2买", "time": laterHigh["time"], "price": laterHigh["price"]})
    else:
        # 结构底
        structBottomIdx = None
        if firstBuy is not None:
            minP = float("inf")
            for i in downIdx:
                if i >= firstBuy["biIdx"]:
                    break
                if bis[i]["endPrice"] < minP:
                    minP = bis[i]["endPrice"]
                    structBottomIdx = i
        if structBottomIdx is None:
            minP = float("inf")
            for i in downIdx:
                if bis[i]["endPrice"] < minP:
                    minP = bis[i]["endPrice"]
                    structBottomIdx = i
        if structBottomIdx is not None:
            bottom = bis[structBottomIdx]
            secondBuy = None
            for i in range(structBottomIdx + 1, len(bis)):
                if bis[i]["type"] != "down":
                    continue
                if bis[i]["endPrice"] > bottom["endPrice"]:
                    secondBuy = {"biIdx": i, "time": bis[i]["endTime"], "price": bis[i]["endPrice"]}
                    break
            if secondBuy is not None:
                points.append({"type": "2买", "time": secondBuy["time"], "price": secondBuy["price"]})
                classSecond = None
                for i in range(secondBuy["biIdx"] + 1, len(bis)):
                    if bis[i]["type"] != "down":
                        continue
                    if bis[i]["endPrice"] > secondBuy["price"]:
                        classSecond = {"time": bis[i]["endTime"], "price": bis[i]["endPrice"]}
                        break
                if classSecond is not None:
                    points.append({"type": "类2买", "time": classSecond["time"], "price": classSecond["price"]})

    # 1买：所有 MACD 背驰底
    for fb in firstBuys:
        points.append({"type": "1买", "time": fb["time"], "price": fb["price"]})

    # 3买
    twoBuys = sorted([p for p in points if p["type"] == "2买"], key=lambda x: x["time"])
    thirdBuys = []
    for k in range(len(twoBuys)):
        tb = twoBuys[k]
        twoIdx = _findIndex(bis, lambda b: b["endTime"] == tb["time"])
        if twoIdx < 0:
            continue
        if k + 1 < len(twoBuys):
            endScan = _findIndex(bis, lambda b: b["endTime"] == twoBuys[k + 1]["time"])
        else:
            endScan = len(bis)
        prevTop = None
        for j in range(twoIdx - 1, -1, -1):
            if bis[j]["type"] == "up":
                prevTop = bis[j]["endPrice"]
                break
        if prevTop is None:
            continue
        lastValid = None
        for i in range(twoIdx + 1, endScan):
            if bis[i]["type"] != "up":
                continue
            if bis[i]["endPrice"] <= prevTop:
                continue
            for mm in range(i + 1, endScan):
                if bis[mm]["type"] != "down":
                    continue
                bt = bis[mm]["endTime"]
                bp = bis[mm]["endPrice"]
                if bp > prevTop:
                    inUp = True
                    if upperBis is not None and len(upperBis) > 0:
                        inUp = False
                        for up in upperBis:
                            if up["type"] == "up" and bt >= up["startTime"] and bt <= up["endTime"] and bp > up["startPrice"]:
                                inUp = True
                                break
                    if inUp:
                        lastValid = {"time": bt, "price": bp}
                break
        if lastValid is not None:
            thirdBuys.append(lastValid)
    # 按时间去重后加入
    for t in thirdBuys:
        if any(p["type"] == "3买" and p["time"] == t["time"] for p in points):
            continue
        dup = _findIndex(points, lambda p: p["type"] == "类2买" and p["time"] == t["time"])
        if dup >= 0:
            del points[dup]
        points.append({"type": "3买", "time": t["time"], "price": t["price"]})
    return points


def findSellPoints(bis, upperBis, macdArr, barSec):
    """卖点识别（含区间套与 MACD 背驰，与买点对称）。"""
    if len(bis) < 3:
        return []
    upIdx = [i for i, b in enumerate(bis) if b["type"] == "up"]

    # 候选一卖：创新高 + MACD 背驰
    firstSells = []
    for k in range(1, len(upIdx)):
        cur = bis[upIdx[k]]
        if isSameAsUpperBi(cur, upperBis, barSec):
            if CHAN_CFG["debug"]:
                print(f"[一卖跳过-与上级笔重合] {fmtT(cur['endTime'])}({cur['endPrice']}) 整笔与上一级别完全重合，本周期不标记")
            continue
        refer = None
        for j in range(k - 1, -1, -1):
            cand = bis[upIdx[j]]
            if cand["span"] < cur["span"] * 0.5:
                continue
            refer = cand
            break
        if refer is not None and cur["endPrice"] > refer["endPrice"]:
            diverge = isBiDiverge(cur, refer, macdArr)
            if CHAN_CFG["debug"]:
                cm = biMacdMetrics(cur, macdArr)
                rm = biMacdMetrics(refer, macdArr)
                print(
                    f"[一卖候选] {fmtT(cur['endTime'])}({cur['endPrice']}) vs 参照 {fmtT(refer['endTime'])}({refer['endPrice']}) "
                    f"| 创新高={cur['endPrice'] > refer['endPrice']} "
                    f"| 红柱面积 {cm['redArea']:.2f} vs {rm['redArea']:.2f} "
                    f"| DIF高点 {cm['difHigh']:.3f} vs {rm['difHigh']:.3f} | 背驰={diverge}"
                )
            if diverge:
                firstSells.append({"biIdx": upIdx[k], "time": cur["endTime"], "price": cur["endPrice"]})
    firstSell = firstSells[-1] if len(firstSells) > 0 else None

    # 1卖 锚定：对每一个候选一卖都做锚定，全部保留；去重
    anchoredSells = []
    seenSellPos = set()
    for fs in firstSells:
        anchored = fs
        if upperBis is not None and len(upperBis) > 0:
            a = anchorFirstSell(fs, upperBis)
            if a is not None:
                bestBi = None
                bestDist = float("inf")
                for i, b in enumerate(bis):
                    if b["type"] != "up":
                        continue
                    d = abs(b["endTime"] - a["time"])
                    if d < bestDist:
                        bestDist = d
                        bestBi = i
                anchored = {
                    "biIdx": bestBi if bestBi is not None else fs["biIdx"],
                    "time": a["time"],
                    "price": a["price"],
                }
        if anchored["time"] in seenSellPos:
            continue
        seenSellPos.add(anchored["time"])
        anchoredSells.append(anchored)

    points = []

    # 2卖 / 类2卖（区间套）
    if upperBis is not None and len(upperBis) > 0:
        for dn in upperBis:
            if dn["type"] != "down":
                continue
            highs = []
            for i, b in enumerate(bis):
                if b["type"] != "up":
                    continue
                if b["endTime"] >= dn["startTime"] and b["endTime"] <= dn["endTime"] + 1:
                    highs.append({"biIdx": i, "time": b["endTime"], "price": b["endPrice"]})
            if len(highs) == 0:
                continue
            highs.sort(key=lambda x: x["time"])
            firstHigh = next((h for h in highs if h["price"] < dn["startPrice"]), None)
            if firstHigh is not None:
                points.append({"type": "2卖", "time": firstHigh["time"], "price": firstHigh["price"]})
                laterLow = next((h for h in highs if h["time"] > firstHigh["time"] and h["price"] < firstHigh["price"]), None)
                if laterLow is not None:
                    points.append({"type": "类2卖", "time": laterLow["time"], "price": laterLow["price"]})
    else:
        # 结构顶
        structTopIdx = None
        if firstSell is not None:
            maxP = float("-inf")
            for i in upIdx:
                if i >= firstSell["biIdx"]:
                    break
                if bis[i]["endPrice"] > maxP:
                    maxP = bis[i]["endPrice"]
                    structTopIdx = i
        if structTopIdx is None:
            maxP = float("-inf")
            for i in upIdx:
                if bis[i]["endPrice"] > maxP:
                    maxP = bis[i]["endPrice"]
                    structTopIdx = i
        if structTopIdx is not None:
            top = bis[structTopIdx]
            secondSell = None
            for i in range(structTopIdx + 1, len(bis)):
                if bis[i]["type"] != "up":
                    continue
                if bis[i]["endPrice"] < top["endPrice"]:
                    secondSell = {"biIdx": i, "time": bis[i]["endTime"], "price": bis[i]["endPrice"]}
                    break
            if secondSell is not None:
                points.append({"type": "2卖", "time": secondSell["time"], "price": secondSell["price"]})
                classSecond = None
                for i in range(secondSell["biIdx"] + 1, len(bis)):
                    if bis[i]["type"] != "up":
                        continue
                    if bis[i]["endPrice"] < secondSell["price"]:
                        classSecond = {"time": bis[i]["endTime"], "price": bis[i]["endPrice"]}
                        break
                if classSecond is not None:
                    points.append({"type": "类2卖", "time": classSecond["time"], "price": classSecond["price"]})

    # 1卖：所有 MACD 背驰顶（锚定到上级上涨笔结束点）
    for as_ in anchoredSells:
        points.append({"type": "1卖", "time": as_["time"], "price": as_["price"]})

    # 3卖
    twoSells = sorted([p for p in points if p["type"] == "2卖"], key=lambda x: x["time"])
    thirdSells = []
    for k in range(len(twoSells)):
        ts = twoSells[k]
        twoIdx = _findIndex(bis, lambda b: b["endTime"] == ts["time"])
        if twoIdx < 0:
            continue
        if k + 1 < len(twoSells):
            endScan = _findIndex(bis, lambda b: b["endTime"] == twoSells[k + 1]["time"])
        else:
            endScan = len(bis)
        prevLow = None
        for j in range(twoIdx - 1, -1, -1):
            if bis[j]["type"] == "down":
                prevLow = bis[j]["endPrice"]
                break
        if prevLow is None:
            continue
        lastValid = None
        for i in range(twoIdx + 1, endScan):
            if bis[i]["type"] != "down":
                continue
            if bis[i]["endPrice"] >= prevLow:
                continue
            for mm in range(i + 1, endScan):
                if bis[mm]["type"] != "up":
                    continue
                st = bis[mm]["endTime"]
                sp = bis[mm]["endPrice"]
                if sp < prevLow:
                    inDown = True
                    if upperBis is not None and len(upperBis) > 0:
                        inDown = False
                        for dn in upperBis:
                            if dn["type"] == "down" and st >= dn["startTime"] and st <= dn["endTime"] and sp < dn["startPrice"]:
                                inDown = True
                                break
                    if inDown:
                        lastValid = {"time": st, "price": sp}
                break
        if lastValid is not None:
            thirdSells.append(lastValid)
    # 按时间去重后加入
    for t in thirdSells:
        if any(p["type"] == "3卖" and p["time"] == t["time"] for p in points):
            continue
        dup = _findIndex(points, lambda p: p["type"] == "类2卖" and p["time"] == t["time"])
        if dup >= 0:
            del points[dup]
        points.append({"type": "3卖", "time": t["time"], "price": t["price"]})
    return points


def keepRecentEach(points):
    """低级别每类买卖点只保留时间上最近的一个（历史策略保留）。"""
    byType = {}
    for p in points:
        if p["type"] not in byType or p["time"] > byType[p["type"]]["time"]:
            byType[p["type"]] = p
    return sorted(byType.values(), key=lambda x: x["time"])
