# -*- coding: utf-8 -*-
"""
支阻互换位逻辑（Python 移植版，与 .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js 对齐）

纯函数模块：基于各周期笔与K线识别「支阻位」，各周期独立成线、不合并。支阻位来源按周期
二选一：系统计算（密集区 cluster）或人工输入（manualLevels）；黄金分割与 BOLL 为独立
「叠加层」（srTypes 开关），独立于支阻位来源、照常叠加、仍进候选池：
  - 密集区（cluster，默认开）：
    - 强支阻互换位：价位被反复测试（触及次数 >= minTouch），之后价格突破该价位，角色互换
      （R2S 阻力转支撑 / S2R 支撑转阻力）
    - 近期极值位：最近若干根笔的 swing 端点（当前最直接的支撑/阻力参考，不要求触及次数，
      标记为 RES 阻力 / SUP 支撑）
    - 每周期候选上限截断（capPerPeriod）
  - 黄金分割（fib，默认关）：对每方向「最新的非一类买卖点」（现算 chan_core findBuyPoints/
    findSellPoints），取其回调笔紧邻前方的顺势笔为参照笔，画经典回撤分割位；该方向
    无已形成点时走「预期回退」（pending）：形成中回调笔 + 其前方顺势笔生成预期位
    （等待2卖/2买 的预期形成区，次高点结构破坏自动失效）
  - BOLL 布林带（boll，默认开）：每周期最后一根已收盘K线的布林带上/中/下轨
    （26 周期 SMA ± 2σ，总体标准差 ÷N），上轨=阻力 RES、下轨=支撑 SUP、中轨按现价侧

不合并（2026-09-12 起取消跨周期合并）：候选逐条展平为全量候选池（merged），
价格=原始识别价（不做任何加权平均），每项附 level（自身周期）与 srcType
（cluster/fib/boll/manual）；显示模型为「各周期独立」选取（pickNearestForDisplay：
每周期图就近上下各 sideCount，仅本周期候选，不继承其它周期线）；人工输入周期
（manualLevels 键存在）替换该周期密集区、全部画出（不受 sideCount/距离上限，就近选取池
不含人工候选），叠加层照常就近叠加（2026-09-13 新增：fib/BOLL 拆为独立叠加层）。

不连接 CDP、不绘图；回测链路通过 compute_srflip 直接调用。
"""

from bisect import bisect_right

from .chan_core import calcATR, calcMACD, intervalSecOf, findBuyPoints, findSellPoints

# numpy 可选加速（countBarsPassing 向量化；不可用时回退纯循环，结果一致）
try:
    import numpy as _np
except Exception:  # pragma: no cover - 环境无 numpy
    _np = None
# 按 bars 列表对象缓存的 lows/highs 数组：链路重算会对多个周期交替调用本函数，
# 必须按对象各自缓存（单槽缓存在多周期交替下每次都重建，形同虚设）。
# 持强引用防 id 复用；条目上限防长回测会话累积。
_barsArraysCache = {}
# countBarsPassing 前缀累加缓存：(id(bars), price, tol) → (bars对象, len, count)
# cut 只增时 = 旧计数 + 新增K线贡献；价位/容差 miss 时全量一次。
_barsPassingCache = {}
_BARS_PASSING_CACHE_MAX = 4096


def prepare_bar_arrays(bars):
    """构建本次回测独享的价格数组，调用方必须裁剪至可见前缀。

    无 NumPy 时返回 None，保留原逐根循环路径。
    """
    if _np is None:
        return None
    return (
        _np.fromiter((b["low"] for b in bars), dtype=_np.float64, count=len(bars)),
        _np.fromiter((b["high"] for b in bars), dtype=_np.float64, count=len(bars)),
    )


def _barsArrays(bars):
    ent = _barsArraysCache.get(id(bars))
    if ent is not None and ent[0] is bars and ent[1] == len(bars):
        return ent[2], ent[3]
    lows, highs = prepare_bar_arrays(bars)
    if len(_barsArraysCache) > 16:
        _barsArraysCache.clear()
    _barsArraysCache[id(bars)] = (bars, len(bars), lows, highs)
    return lows, highs


def _count_passing_range(bars, loP, hiP, start, end, barArrays=None):
    """统计 bars[start:end] 中与价位带重叠的根数（语义与逐根循环一致）。"""
    if start >= end:
        return 0
    n_total = len(bars)
    if _np is not None and n_total >= 512 and end - start >= 64:
        lows, highs = barArrays if barArrays is not None else _barsArrays(bars)
        # barArrays 可能是全量运行数组的 [:cut] 视图，长度须与 bars 对齐
        if lows is not None and len(lows) >= end:
            return int(_np.count_nonzero((lows[start:end] <= hiP) & (highs[start:end] >= loP)))
    n = 0
    for i in range(start, end):
        b = bars[i]
        if b["low"] <= hiP and b["high"] >= loP:
            n += 1
    return n

# 参数（与 JS 默认值一致）
CLUSTER_ATR = 0.5        # 价位聚类阈值（×ATR）
RECENT_CLUSTER_ATR = 1.0  # 近期极值位聚类容差（×ATR）
TOUCH_WEIGHT = 0.6       # 强度评分：触及次数权重
BARS_WEIGHT = 0.4        # 强度评分：经过K线数量权重
MAX_DIST_ATR = 3.0       # 选取时距离上限（×本级别ATR）
MAX_PER_PERIOD = 50      # 每周期候选数量上限（仅密集区，fib/boll 豁免）
SIDE_COUNT = 2           # 每周期图每侧条数（2 → 每图最多 4 条）
RECENT_BI_COUNT = 20     # 近期极值位取最近 N 根笔

# 支阻位类型开关（与 JS --sr-types 默认一致）
DEFAULT_SR_TYPES = ("cluster", "boll")

# 黄金分割：比率与非一类买卖点白名单（真1买/真1卖是 mark-buy-sell 层后处理合并标注，
# findBuyPoints/findSellPoints 原始输出不存在，白名单过滤即天然排除一类）
FIB_LEVELS = [0.382, 0.5, 0.618]
# BOLL 布林带参数（已收盘K线口径：剔除末根形成中K线后取末 BOLL_LENGTH 根收盘价）
BOLL_LENGTH = 26
BOLL_MULT = 2.0
FIB_BUY_TYPES = ["2买", "类2买", "3买", "类3买"]
FIB_SELL_TYPES = ["2卖", "类2卖", "3卖", "类3卖"]
# 上级周期映射（现算买卖点的区间套用；D 及未收录周期无上级，走结构底分支）
UPPER_OF = {"240": "D", "60": "240", "15": "60", "3": "15"}


def _bis_fingerprint(bis):
    """笔列表指纹：长度 + 末笔端点（延伸/新分型任一变化即 miss）。"""
    if not bis:
        return (0, None, None, None, None)
    last = bis[-1]
    return (len(bis), last.get("startTime"), last.get("endTime"),
            last.get("endPrice"), last.get("type"))

# 级别大小顺序（从大到小），用于候选池排序与可见范围判断
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


def detectFlip(cluster, bars, tol, barTimes=None):
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
    # 时间索引可能包含未来K线，二分上界必须限制在当前可见前缀内。
    start = bisect_right(barTimes, lastTouch, hi=len(bars)) if barTimes is not None else 0
    for i in range(start, len(bars)):
        bar = bars[i]
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


# ============================================================
# 黄金分割支阻位（fib，与 JS 逐行对齐）
# 对每方向「最新的非一类买卖点」，取其回调笔紧邻前方的顺势笔为参照笔，
# 画经典回撤分割位。fib 豁免 capPerPeriod 截断与评分（由结构点派生，评分语义不适用）；
# 直接进全量候选池（不合并，保留自身标记与原始价位）。
# ============================================================


def pickLatestFibPoint(points, types):
    """从买卖点列表中取「最新的非一类点」。
    @param points findBuyPoints/findSellPoints 原始输出 [{type,time,price}]
    @param types  白名单（FIB_BUY_TYPES / FIB_SELL_TYPES）
    @returns 白名单内 time 最大的点（同 time 取数组靠后者）；无 → None
    """
    best = None
    for p in points:
        if p["type"] not in types:
            continue
        if best is None or p["time"] >= best["time"]:
            best = p
    return best


def referBiOfPoint(bis, pointTime, pullbackType):
    """定位买卖点的回调笔与参照笔。
    回调笔 = 终点即该点的笔（买点是 down 笔、卖点是 up 笔）；
    参照笔 = 紧邻回调笔前方的反向顺势笔（买点取前方上涨笔、卖点取前方下跌笔）。
    @param bis          本周期笔列表（应传未过滤的全量，最新点的回调笔可能横跨窗口边界）
    @param pointTime    买卖点时间（= 笔 endTime）
    @param pullbackType 回调笔方向：买点 "down"、卖点 "up"
    @returns { pullback, refer }；匹配不到 / 首笔无前方笔 /
             前一笔同向（笔应交替，同向为脏数据）→ None
    """
    idx = -1
    for i, b in enumerate(bis):
        if b["endTime"] == pointTime and b["type"] == pullbackType:
            idx = i
            break
    if idx < 0 or idx == 0:
        return None
    refer = bis[idx - 1]
    if refer["type"] == pullbackType:
        return None
    return {"pullback": bis[idx], "refer": refer}


def pendingReferOf(bis, side):
    """预期回退（pending）：某方向无已形成非一类点时，用「形成中的回调笔 + 其前方顺势笔」
    生成预期黄金分割位（等待2卖/2买 状态下的预期形成区——实盘即「预期位置 + 够笔/背驰确认」）。
    - 卖向：末笔为形成中上涨笔（bis 落盘口径：末笔延伸至最新极值）且其现高点 < 前方
      下跌笔起点（次高点结构未破坏）→ 参照笔 = 该下跌笔；
    - 买向对称：末笔为形成中下跌笔且其现低点 > 前方上涨笔起点；
    - 形成笔突破前方笔起点（结构被否定）→ 条件失效，该方向无预期位（下次重算自动消失）。
    @returns { forming, refer }；末笔是首笔/方向不符/前方笔同向（脏数据）/
             次高点结构破坏 → None
    """
    if len(bis) < 2:
        return None
    forming = bis[-1]
    pullbackType = "down" if side == "buy" else "up"  # 形成中的回调笔方向
    if forming["type"] != pullbackType:
        return None
    refer = bis[-2]
    if refer["type"] == pullbackType:
        return None
    # 次高点/次低点结构未破坏：形成笔现极值未超越前方顺势笔起点
    if side == "sell":
        if not forming["endPrice"] < refer["startPrice"]:
            return None
    else:
        if not forming["endPrice"] > refer["startPrice"]:
            return None
    return {"forming": forming, "refer": refer}


def fibLevelsOf(refer, side, fibLevels):
    """参照笔的黄金分割回撤位。
    buy（参照笔为上涨笔 L→H）：分割位 = H - r×(H-L)，即上涨走势的回撤支撑；
    sell（参照笔为下跌笔 H→L）：分割位 = L + r×(H-L)，即下跌走势的反弹阻力。
    @returns [{ ratio, price }]；span=0（退化笔）→ []
    """
    H = refer["endPrice"] if side == "buy" else refer["startPrice"]
    L = refer["startPrice"] if side == "buy" else refer["endPrice"]
    if not H > L:
        return []
    return [{"ratio": r,
             "price": (H - r * (H - L)) if side == "buy" else (L + r * (H - L))}
            for r in fibLevels]


def buildFibCandidates(fullBis, buyPts, sellPts, fibLevels, bars, tol, barArrays=None):
    """组装黄金分割支阻位候选：每方向优先取「最新非一类点」× 全部比率（不回退更早点）；
    该方向无已形成点时走**预期回退**（pendingReferOf），生成 pending 预期位补位。
    @param fullBis 本周期全量笔（供参照笔定位，见 referBiOfPoint）
    @returns fib 候选列表（买点组在前；预期位带 pending: True）
    """
    out = []
    sides = [
        {"side": "buy", "points": buyPts, "types": FIB_BUY_TYPES, "pullbackType": "down", "type": "SUP"},
        {"side": "sell", "points": sellPts, "types": FIB_SELL_TYPES, "pullbackType": "up", "type": "RES"},
    ]
    for cfg in sides:
        pt = pickLatestFibPoint(cfg["points"], cfg["types"])
        found = None
        srcPoint = None   # 派生源：已形成点 or 预期（形成笔极值）
        pending = False
        if pt is not None:
            # 有已形成点：只用它（预期位只补位，不抢占已形成点语义）；定位不到参照笔则跳过
            f = referBiOfPoint(fullBis, pt["time"], cfg["pullbackType"])
            if f is not None:
                found = f
                srcPoint = {"type": pt["type"], "time": pt["time"], "price": pt["price"]}
        else:
            # 无已形成点：预期回退（等待2卖/2买 的预期形成区）
            p = pendingReferOf(fullBis, cfg["side"])
            if p is not None:
                found = p
                srcPoint = {"type": "预期2买" if cfg["side"] == "buy" else "预期2卖",
                            "time": p["forming"]["endTime"], "price": p["forming"]["endPrice"]}
                pending = True
        if found is None:
            continue
        for lv in fibLevelsOf(found["refer"], cfg["side"], fibLevels):
            cand = {
                "price": lv["price"],
                "type": cfg["type"],
                "fib": True,
                "ratio": lv["ratio"],
                "fromPoint": srcPoint,
                "referBi": {
                    "startTime": found["refer"]["startTime"], "endTime": found["refer"]["endTime"],
                    "startPrice": found["refer"]["startPrice"], "endPrice": found["refer"]["endPrice"],
                },
                "touchCount": 1,
                "firstTouch": found["refer"]["startTime"],
                "lastTouch": srcPoint["time"],
                "breakTime": srcPoint["time"],  # 信号点/形成笔极值时间 = fib 位生效/绘线锚点时间
                "barsPassed": countBarsPassing(lv["price"], bars, tol, barArrays),
            }
            if pending:
                cand["pending"] = True
            out.append(cand)
    return out


# ============================================================
# BOLL 布林带支阻位（boll，与 JS 逐行对齐）
# 每周期取「最后一根已收盘K线」的布林带上/中/下轨（BOLL_LENGTH 周期 SMA ± BOLL_MULT×σ，
# 总体标准差 ÷N，与 TradingView 同口径），上轨=阻力 RES、下轨=支撑 SUP、中轨按现价侧。
# boll 同 fib 一样豁免截断与评分（评分语义不适用）；直接进全量候选池（不合并）。
# ============================================================


def calcBOLL(bars, length, mult):
    """计算布林带（已收盘口径）：剔除末根形成中K线，取末 length 根收盘价的 SMA 与总体标准差。
    @returns {upper, mid, lower} 或 None（已收盘不足 length 根）
    """
    if not bars:
        return None
    if length >= 1:
        # 尾切片恰取末 length 根已收盘K线（等价 closed=bars[:-1] 后 closed[-length:]，
        # 免 O(n) 整表拷贝）；同元素同序求和，浮点结果逐位不变
        tail = bars[-(length + 1):-1]
        if len(tail) < length:
            return None
        closes = [b["close"] for b in tail]
    else:
        closed = bars[:-1]
        if len(closed) < length:
            return None
        closes = [b["close"] for b in closed[-length:]]
    n = len(closes)
    mid = sum(closes) / n
    variance = sum((c - mid) ** 2 for c in closes) / n  # 总体方差（÷N）
    sigma = variance ** 0.5
    return {"upper": mid + mult * sigma, "mid": mid, "lower": mid - mult * sigma}


def buildBollCandidates(bars, length, mult, currentPrice):
    """组装 BOLL 候选：三轨各一条。上轨 RES、下轨 SUP、中轨按现价侧（现价 >= 中轨 → 支撑）。
    @returns 3 个候选（无布林位时为空列表）
    """
    band = calcBOLL(bars, length, mult)
    if band is None:
        return []
    # 锚点 = 末根已收盘K线时间（= bars[:-1] 的末元素，免 O(n) 整表拷贝）
    anchorTime = bars[-2]["time"] if len(bars) >= 2 else bars[-1]["time"]
    midType = "SUP" if (currentPrice is not None and currentPrice >= band["mid"]) else "RES"

    def make(price, type_, tag):
        return {
            "price": price, "type": type_, "boll": tag,
            "touchCount": 1, "barsPassed": 0,
            "firstTouch": anchorTime, "lastTouch": anchorTime, "breakTime": anchorTime,
        }
    return [
        make(band["upper"], "RES", "upper"),
        make(band["mid"], midType, "mid"),
        make(band["lower"], "SUP", "lower"),
    ]


# ============================================================
# 人工支阻位（manual，与 JS buildManualCandidates 逐行对齐）
# 配置键 manualLevels = { 周期: [价位,...] }：键存在 = 该周期支阻位来源=人工，
# 替换该周期密集区计算（叠加层 fib/BOLL 独立照常）；type 按现价侧推导；
# 全部画出（不受 sideCount/距离上限）。
# ============================================================


def buildManualCandidates(prices, bars, currentPrice):
    """人工价位候选：替换该周期的密集区支阻位（叠加层独立）。type 按现价侧推导（现价 >= 价位 → SUP，
    否则 → RES，仿 buildBollCandidates 中轨口径；currentPrice 未知时统一 RES）；
    锚点 = 该周期末根已收盘K线时间（单根K线回退末根，避免 breakTime=0）。
    全部候选豁免 capPerPeriod、pickNearestForDisplay 的 sideCount 与距离上限
    （由 compute_srflip 覆写 drawnByPeriod 实现「输入几条画几条」）。
    @param prices 人工价位列表（已由校验层去重升序）
    @returns [{ price, type, manual: True, touchCount: 1, barsPassed: 0,
               firstTouch/lastTouch/breakTime: 末根已收盘K线 time }]
    """
    anchorTime = bars[-2]["time"] if len(bars) >= 2 else bars[-1]["time"]
    out = []
    for p in prices:
        typ = "SUP" if (currentPrice is not None and currentPrice >= p) else "RES"
        out.append({"price": float(p), "type": typ, "manual": True,
                    "touchCount": 1, "barsPassed": 0,
                    "firstTouch": anchorTime, "lastTouch": anchorTime, "breakTime": anchorTime})
    return out


# ============================================================
# 来源标注（与 JS sourceLabelOf/labelOf/periodNameOf 逐行对齐）
# ============================================================


def periodNameOf(res):
    """周期中文名（标注用）：240→4小时、60→1小时、15→15分钟、3→3分钟、D→日线。"""
    r = str(res).upper()
    return {
        "3": "3分钟", "15": "15分钟",
        "60": "1小时", "1H": "1小时",
        "240": "4小时", "4H": "4小时",
        "1D": "日线", "D": "日线",
        "1W": "周线", "W": "周线",
        "30S": "30秒",
    }.get(r, str(res))


def sourceLabelOf(f):
    """支阻位来源类型的中文标注（落盘 drawnByPeriod 的 label 用）。
    manual → 手动位；boll → BOLL上轨/中轨/下轨；fib → 预期<N>（pending）/
    黄金分割<ratio>（已形成）；cluster → 密集区。
    """
    if f.get("manual"):
        return "手动位"
    if f.get("boll"):
        b = f["boll"]
        return "BOLL上轨" if b == "upper" else "BOLL中轨" if b == "mid" else "BOLL下轨"
    if f.get("fib"):
        if f.get("pending"):
            fp = f.get("fromPoint")
            return fp["type"] if fp else "预期"
        return "黄金分割%s" % f.get("ratio")
    return "密集区"


def labelOf(f):
    """完整标注：`<来源类型>+<周期中文名>`（落盘 drawnByPeriod 的 label 字段）。"""
    return "%s+%s" % (sourceLabelOf(f), periodNameOf(f.get("level")))


def countBarsPassing(price, bars, tol, barArrays=None):
    """统计某价位带（price ± tol）被多少根 K 线覆盖/穿越（含影线）。

    性能：回测链路每次重算会对几十个支阻位各调一次本函数，逐根循环是长窗口下的
    主要热点之一。numpy 可用时用向量化比较（比较语义与逐根循环完全一致），
    并按 bars 列表对象缓存 lows/highs 数组——同一链路重算内 bars 不变，只建一次。
    barArrays 可传本次运行预建的 (lows, highs)，必须已裁剪至 bars 的可见前缀。
    前缀累加：同一 (bars对象, price, tol) 在 cut 只增时 = 旧计数 + 新增段，
    与全量重算 int 级一致。无 numpy 时回退逐根循环（结果一致）。"""
    hiP, loP = price + tol, price - tol
    n = len(bars)
    key = (id(bars), price, tol)
    ent = _barsPassingCache.get(key)
    if ent is not None and ent[0] is bars:
        prev_n, prev_cnt = ent[1], ent[2]
        if prev_n == n:
            return prev_cnt
        if prev_n < n:
            cnt = prev_cnt + _count_passing_range(bars, loP, hiP, prev_n, n, barArrays)
            _barsPassingCache[key] = (bars, n, cnt)
            return cnt
    # miss 或 cut 回退：全量一次
    if _np is not None and n >= 512:
        lows, highs = barArrays if barArrays is not None else _barsArrays(bars)
        if lows is not None and len(lows) >= n:
            cnt = int(_np.count_nonzero((lows[:n] <= hiP) & (highs[:n] >= loP)))
        else:
            cnt = _count_passing_range(bars, loP, hiP, 0, n, barArrays)
    else:
        cnt = _count_passing_range(bars, loP, hiP, 0, n, barArrays)
    if len(_barsPassingCache) >= _BARS_PASSING_CACHE_MAX:
        _barsPassingCache.clear()
    _barsPassingCache[key] = (bars, n, cnt)
    return cnt


def flipScore(f, group, touchWeight=TOUCH_WEIGHT, barsWeight=BARS_WEIGHT):
    """支阻位强度评分：score = 触及权重 × norm(触及次数) + 经过K线权重 × norm(经过K线数量)。
    同一级别候选集内 min-max 归一化。权重默认与模块常量一致（0.6/0.4）。"""
    ts = [g["touchCount"] for g in group]
    bs = [g["barsPassed"] for g in group]
    tMin, tMax = min(ts), max(ts)
    bMin, bMax = min(bs), max(bs)
    normTouch = (f["touchCount"] - tMin) / (tMax - tMin) if tMax > tMin else 1
    normBars = (f["barsPassed"] - bMin) / (bMax - bMin) if bMax > bMin else 1
    return touchWeight * normTouch + barsWeight * normBars


def _kindOf(f):
    """候选来源类型：manual/boll/fib/cluster（由标记反推，与 JS 一致）。"""
    if f.get("manual"):
        return "manual"
    if f.get("boll"):
        return "boll"
    if f.get("fib"):
        return "fib"
    return "cluster"


def flatten_candidates(combined):
    """展平各周期候选为全量候选池（不合并，与 JS flattenCandidates 逐行对齐）：
    每条候选独立成线，价格=原始识别价（不做任何加权平均），同价位不同周期/不同类型
    的候选也各自保留；附 level（自身周期）与 srcType（cluster/fib/boll，由自身标记反推）。
    排序：先按 LEVEL_ORDER 级别序（大→小，未知键落尾），再按 price 升序（确定性输出）。
    @param combined { 周期: [候选,...] }（密集区截断后 + fib + boll）
    @returns [{ ...原候选字段, level, srcType }]
    """
    def levelIdx(res):
        r = str(res).upper()
        return LEVEL_ORDER.index(r) if r in LEVEL_ORDER else len(LEVEL_ORDER)

    out = []
    for res, flips in combined.items():
        for f in flips:
            out.append(dict(f, level=res, srcType=_kindOf(f)))
    out.sort(key=lambda f: (levelIdx(f["level"]), f["price"]))
    return out


def pickNearestForDisplay(merged, displayPeriods, currentPrice, sideCount, maxDistAtr, periodAtrs):
    """按显示周期选取（各周期独立，与 JS 逐行对齐）：每个显示周期图最多 2×sideCount 条线。
    候选池 = 仅该周期自身的候选（merged 中 level == L，不继承其它周期线），
    取「距现价最近的上方 sideCount 条 + 下方 sideCount 条」，每条受
    ≤ maxDistAtr×本周期ATR 距离上限（periodAtrs[L] 缺失时 Infinity），允许上下不对称。
    @returns { 周期: [line,...] }
    """
    out = {}
    for L in displayPeriods:
        atrL = periodAtrs.get(L)
        maxDist = maxDistAtr * atrL if atrL else float("inf")
        pool = [f for f in merged if f["level"] == L]
        above = sorted(
            [f for f in pool if f["price"] >= currentPrice and f["price"] - currentPrice <= maxDist],
            key=lambda f: f["price"])[:sideCount]
        below = sorted(
            [f for f in pool if f["price"] < currentPrice and currentPrice - f["price"] <= maxDist],
            key=lambda f: f["price"], reverse=True)[:sideCount]
        out[L] = above + below
    return out


def capPerPeriod(allFlips, maxPerPeriod, touchWeight=TOUCH_WEIGHT, barsWeight=BARS_WEIGHT):
    """每周期候选数量上限截断：每周期最多保留 maxPerPeriod 个候选。
    超出时按「强度评分降序」保留 Top N（评分权重可配，默认与模块常量一致）。"""
    if not allFlips:
        return allFlips
    if not maxPerPeriod or maxPerPeriod <= 0:
        return allFlips
    out = {}
    for res, group in allFlips.items():
        if not group or len(group) <= maxPerPeriod:
            out[res] = group
            continue
        scored = [dict(f, score=flipScore(f, group, touchWeight, barsWeight)) for f in group]
        scored.sort(key=lambda f: f["score"], reverse=True)
        out[res] = scored[:maxPerPeriod]
    return out


# ============================================================
# 汇总计算（供回测链路调用）
# ============================================================


def cluster_candidates(bis, bars, atr, *, clusterAtr=CLUSTER_ATR,
                       recentClusterAtr=RECENT_CLUSTER_ATR, minTouch=4,
                       recentBiCount=RECENT_BI_COUNT,
                       clusterParts=("flip", "recent"), with_strength=True,
                       barTimes=None, barArrays=None):
    """Single source for uncapped cluster generation (regular engine and tuner).

    with_strength=False omits only barsPassed, which is irrelevant before capping.
    Neither input nor cached candidates are mutated.
    """
    if len(bis) < 3 or not bars:
        return []
    tol = clusterAtr * atr
    out = []
    if "flip" in clusterParts:
        for c in clusterPoints(extractSwingPoints(bis), tol):
            if len(c["touches"]) >= minTouch:
                flip = detectFlip(c, bars, tol, barTimes)
                if flip:
                    out.append(flip)
    if "recent" in clusterParts:
        for c in clusterPoints(extractRecentExtremes(bis, recentBiCount),
                               recentClusterAtr * atr):
            recent = detectRecentFlip(c)
            if recent:
                out.append(recent)
    if with_strength:
        for item in out:
            item["barsPassed"] = countBarsPassing(item["price"], bars, tol, barArrays)
    return out


def compute_srflip(periodBis, barsByPeriod, periods,
                   clusterAtr=CLUSTER_ATR, recentClusterAtr=RECENT_CLUSTER_ATR,
                   maxDistAtr=MAX_DIST_ATR, maxPerPeriod=MAX_PER_PERIOD,
                   minTouchOverride=None, periodAtrsIn=None,
                   srTypes=DEFAULT_SR_TYPES, fibLevels=FIB_LEVELS, periodMacdIn=None,
                   bollLength=BOLL_LENGTH, bollMult=BOLL_MULT,
                   clusterParts=("flip", "recent"), minTouchsIn=None,
                   recentBiCount=RECENT_BI_COUNT,
                   touchWeight=TOUCH_WEIGHT, barsWeight=BARS_WEIGHT,
                   sideCount=SIDE_COUNT,
                   clusterParamsByPeriod=None, manualLevels=None, periodBarTimesIn=None,
                   periodBarArraysIn=None, work_cache=None):
    """逐周期识别支阻位（密集区 + 黄金分割 + BOLL + 人工输入），展平为全量候选池、各周期独立选取。

    @param work_cache 可选：跨次调用复用的 dict。未变周期（cut/ATR/笔指纹相同）直接复用
                      该周期的 cluster+fib 结果；BOLL/展平/选取仍每拍重算（依赖现价）。
                      输出与无缓存路径逐位一致。
    """
    # 可选加速输入：时间索引与 bars 同序；价格数组仅含当前可见前缀。
    periodAtrsIn = periodAtrsIn or {}
    periodMacdIn = periodMacdIn or {}
    periodBarTimesIn = periodBarTimesIn or {}
    periodBarArraysIn = periodBarArraysIn or {}
    # 人工支阻位：周期键大写归一（别名 1H/4H 已在服务层归一，此处兜底）
    manualLevels = {str(k).upper(): list(v) for k, v in (manualLevels or {}).items()}
    allFlips = {}
    allFibs = {}
    allBolls = {}
    allManuals = {}
    periodAtrs = {}
    lastCloseByRes = {}
    for res in periods:
        manual = manualLevels.get(str(res).upper())
        bis = periodBis.get(res, []) or []
        bars = barsByPeriod.get(res, []) or []
        # 人工周期不要求 bis≥3（无笔也能用）；系统周期保留原门槛
        if manual is None and (not bis or len(bis) < 3):
            continue
        if not bars:
            continue
        lastCloseByRes[res] = bars[-1]["close"]
        atr = periodAtrsIn.get(res)
        if atr is None:
            atr = calcATR(bars, 14)
        periodAtrs[res] = atr

        # 未变周期：复用 cluster + fib（BOLL 依赖现价，后面统一重算）
        upperRes = UPPER_OF.get(str(res).upper())
        upperBis = periodBis.get(upperRes) if upperRes else None
        cache_key = (
            "sr_cf", res, len(bars), atr, _bis_fingerprint(bis),
            _bis_fingerprint(upperBis) if ("fib" in srTypes) else None,
            tuple(srTypes) if not isinstance(srTypes, tuple) else srTypes,
            clusterAtr, recentClusterAtr, recentBiCount, maxPerPeriod,
            tuple(clusterParts) if clusterParts is not None else None,
            minTouchOverride, bollLength, bollMult,
            tuple(fibLevels) if fibLevels is not None else None,
        )
        if work_cache is not None:
            ent = work_cache.get(("sr_cf", res))
            if ent is not None and ent[0] == cache_key:
                if ent[1] is not None:
                    allFlips[res] = ent[1]
                if ent[2] is not None:
                    allFibs[res] = ent[2]
                continue

        flips = None
        fibs = None
        if manual is None:
            # 系统计算支阻位 = 密集区（人工周期跳过，支阻位由人工价位提供）
            pcfg = (clusterParamsByPeriod or {}).get(str(res).upper(), {})
            localCluster = pcfg.get("clusterAtr", clusterAtr)
            minTouch = (minTouchsIn or {}).get(str(res).upper()) or minTouchFor(res, minTouchOverride)
            flips = cluster_candidates(
                bis, bars, atr, clusterAtr=localCluster,
                recentClusterAtr=pcfg.get("recentClusterAtr", recentClusterAtr),
                recentBiCount=pcfg.get("recentBiCount", recentBiCount),
                minTouch=minTouch, clusterParts=clusterParts,
                barTimes=periodBarTimesIn.get(res),
                barArrays=periodBarArraysIn.get(res)) if "cluster" in srTypes else []
            allFlips[res] = flips

        # 黄金分割叠加层：独立于支阻位来源，人工周期照常生成（需笔：bis≥3）
        if "fib" in srTypes and bis and len(bis) >= 3:
            macd = periodMacdIn.get(res) or calcMACD(bars)
            buyPts = findBuyPoints(bis, upperBis, macd, intervalSecOf(res))
            sellPts = findSellPoints(bis, upperBis, macd, intervalSecOf(res))
            fibs = buildFibCandidates(bis, buyPts, sellPts, fibLevels, bars, clusterAtr * atr,
                                      periodBarArraysIn.get(res))
            allFibs[res] = fibs

        if work_cache is not None:
            work_cache[("sr_cf", res)] = (cache_key, flips, fibs)

    # 每周期候选数量上限（数据层截断，仅密集区；fib/boll 评分语义不适用，豁免）
    allFlipsCapped = capPerPeriod(allFlips, maxPerPeriod, touchWeight, barsWeight)

    # 当前价格：用最小有数据周期的最后一根K线收盘价（各周期收盘价接近，取最小周期最精确）
    currentPrice = None
    for k in ("3", "15", "60", "240", "D"):
        if k in lastCloseByRes:
            currentPrice = lastCloseByRes[k]
            break

    # BOLL 布林带叠加层：独立于支阻位来源，人工周期照常生成（中轨按现价侧，
    # 需 currentPrice 已知后生成，逐周期独立三轨一组）
    if "boll" in srTypes:
        for res in periods:
            if res not in periodAtrs:
                continue
            bars = barsByPeriod.get(res, []) or []
            if bars:
                allBolls[res] = buildBollCandidates(bars, bollLength, bollMult, currentPrice)

    # 人工支阻位候选：type 按现价侧推导，需 currentPrice 已知后生成（键存在即完全替换该周期系统计算）
    for res in periods:
        manual = manualLevels.get(str(res).upper())
        if manual is None or res not in periodAtrs:
            continue
        bars = barsByPeriod.get(res, []) or []
        if bars:
            allManuals[res] = buildManualCandidates(manual, bars, currentPrice)

    # 全量候选池（不合并）：候选（密集区截断后 + fib + boll + manual）逐条展平，
    # 每条附自身周期 level 与来源 srcType，价格=原始识别价（不做任何加权平均）。
    combined = {}
    for res in periods:
        arr = (allFlipsCapped.get(res, []) or []) + (allFibs.get(res, []) or []) \
            + (allBolls.get(res, []) or []) + (allManuals.get(res, []) or [])
        if arr:
            combined[res] = arr
    mergedOut = flatten_candidates(combined)

    # 按显示周期选取：每周期图 ≤ 2×sideCount 条（就近上下各 N，各周期独立、不继承其它周期线）。
    # 就近选取池 = 非人工候选（密集区+fib+boll）——人工支阻位不走就近，全部画出。
    # 显示周期 = 成功处理（有 ATR/K线）的周期，顺序沿 periods（从大到小）。
    displayPeriods = [r for r in periods if r in periodAtrs]
    overlayPool = [f for f in mergedOut if not f.get("manual")]
    drawnByPeriod = pickNearestForDisplay(overlayPool, displayPeriods, currentPrice,
                                          sideCount, maxDistAtr, periodAtrs) \
        if currentPrice is not None else {}
    # 人工周期：支阻位全部画出（不受 sideCount 与距离上限）+ 叠加层就近结果照常叠加；
    # 取 merged 池中该周期的人工条目（已附 level/srcType，labelOf 需要 level）；
    # currentPrice 未知时维持空（与 boll 中轨降级口径一致）
    if currentPrice is not None:
        for L in displayPeriods:
            if L in allManuals:
                drawnByPeriod[L] = [f for f in mergedOut
                                    if f["level"] == L and f.get("manual")] \
                    + drawnByPeriod.get(L, [])
    for L, lines in drawnByPeriod.items():
        for f in lines:
            f["label"] = labelOf(f)

    periodsOut = {}
    for res, group in allFlipsCapped.items():
        periodsOut[res] = group + (allFibs.get(res, []) or []) + (allBolls.get(res, []) or []) \
            + (allManuals.get(res, []) or [])
    # 纯 boll/fib/manual 周期可能未进 allFlipsCapped（cluster 关闭/人工替换时），补齐
    for res in periods:
        if res not in periodsOut:
            arr = (allFibs.get(res, []) or []) + (allBolls.get(res, []) or []) + (allManuals.get(res, []) or [])
            if arr:
                periodsOut[res] = arr

    return {
        "periods": periodsOut,
        "merged": mergedOut,
        "drawnByPeriod": drawnByPeriod,
        "currentPrice": currentPrice,
        "periodAtrs": periodAtrs,
    }
