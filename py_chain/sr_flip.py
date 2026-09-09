# -*- coding: utf-8 -*-
"""
支阻互换位逻辑（Python 移植版，与 .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js 对齐）

纯函数模块：基于各周期笔与K线识别「支阻位」并跨周期合并，三类来源（srTypes 可分别开关）：
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

三类同池合并（mergeFlipsAcrossPeriods）：三类候选全部进同一池、同一规则；多来源混合线
删除 fib/pending/boll 标记，纯单来源独立线保留标记；显示模型改为「按周期」选取
（pickNearestForDisplay：每周期图就近上下各 sideCount，高级别线继承到低周期图）。

不连接 CDP、不绘图；回测链路通过 compute_srflip 直接调用。
"""

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


def _barsArrays(bars):
    ent = _barsArraysCache.get(id(bars))
    if ent is not None and ent[0] is bars and ent[1] == len(bars):
        return ent[2], ent[3]
    lows = _np.fromiter((b["low"] for b in bars), dtype=_np.float64, count=len(bars))
    highs = _np.fromiter((b["high"] for b in bars), dtype=_np.float64, count=len(bars))
    if len(_barsArraysCache) > 16:
        _barsArraysCache.clear()
    _barsArraysCache[id(bars)] = (bars, len(bars), lows, highs)
    return lows, highs

# 参数（与 JS 默认值一致）
CLUSTER_ATR = 0.5        # 价位聚类阈值（×ATR）
MERGE_ATR = 0.5          # 跨周期合并阈值（×最小周期ATR）
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
FIB_BUY_TYPES = ["2买", "类2买", "3买"]
FIB_SELL_TYPES = ["2卖", "类2卖", "3卖"]
# 上级周期映射（现算买卖点的区间套用；D 及未收录周期无上级，走结构底分支）
UPPER_OF = {"240": "D", "60": "240", "15": "60", "3": "15"}

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


# ============================================================
# 黄金分割支阻位（fib，与 JS 逐行对齐）
# 对每方向「最新的非一类买卖点」，取其回调笔紧邻前方的顺势笔为参照笔，
# 画经典回撤分割位。fib 走「并行双轨」：不进 capPerPeriod / mergeFlipsAcrossPeriods /
# pickByLevel（三条比率位是成组结构，并簇均价会破坏比例位几何语义）。
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


def buildFibCandidates(fullBis, buyPts, sellPts, fibLevels, bars, tol):
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
                "barsPassed": countBarsPassing(lv["price"], bars, tol),
            }
            if pending:
                cand["pending"] = True
            out.append(cand)
    return out


# ============================================================
# BOLL 布林带支阻位（boll，与 JS 逐行对齐）
# 每周期取「最后一根已收盘K线」的布林带上/中/下轨（BOLL_LENGTH 周期 SMA ± BOLL_MULT×σ，
# 总体标准差 ÷N，与 TradingView 同口径），上轨=阻力 RES、下轨=支撑 SUP、中轨按现价侧。
# boll 同 fib 一样豁免截断与评分（评分语义不适用），但**参与**跨周期合并（三类同池）。
# ============================================================


def calcBOLL(bars, length, mult):
    """计算布林带（已收盘口径）：剔除末根形成中K线，取末 length 根收盘价的 SMA 与总体标准差。
    @returns {upper, mid, lower} 或 None（已收盘不足 length 根）
    """
    if not bars:
        return None
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
    closed = bars[:-1]
    anchorTime = closed[-1]["time"]
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
    混合合并线（srcType=mixed）→ 位置线；boll → BOLL上轨/中轨/下轨；
    fib → 预期<N>（pending）/ 黄金分割<ratio>（已形成）；cluster → 密集区。
    """
    if f.get("srcType") == "mixed":
        return "位置线"
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


def countBarsPassing(price, bars, tol):
    """统计某价位带（price ± tol）被多少根 K 线覆盖/穿越（含影线）。

    性能：回测链路每次重算会对几十个支阻位各调一次本函数，逐根循环是长窗口下的
    主要热点之一。numpy 可用时用向量化比较（比较语义与逐根循环完全一致），
    并按 bars 列表对象缓存 lows/highs 数组——同一链路重算内 bars 不变，只建一次。
    无 numpy 时回退逐根循环（结果一致）。"""
    hiP, loP = price + tol, price - tol
    if _np is not None and len(bars) >= 512:
        lows, highs = _barsArrays(bars)
        return int(_np.count_nonzero((lows <= hiP) & (highs >= loP)))
    n = 0
    for b in bars:
        if b["low"] <= hiP and b["high"] >= loP:
            n += 1
    return n


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
    """候选来源类型：boll/fib/cluster（由标记反推，与 JS 一致）。"""
    if f.get("boll"):
        return "boll"
    if f.get("fib"):
        return "fib"
    return "cluster"


def _memberSnapshot(item):
    """合并成员快照（mergeDetail 用）：记录并入前的原始价/来源/类型等字段。
    price 必须在加权平均【之前】取值——成员原价与合并价的差是判断合并是否合理的关键依据。"""
    m = {"source": item["source"], "kind": item["_kinds"][0], "type": item["type"],
         "price": item["price"], "touchCount": item["touchCount"],
         "barsPassed": item.get("barsPassed", 0), "breakTime": item["breakTime"],
         "recent": bool(item.get("recent"))}
    if item.get("fib"):
        m["ratio"] = item.get("ratio")
    if item.get("boll"):
        m["boll"] = item.get("boll")
    if item.get("pending"):
        m["pending"] = True
    return m


def mergeFlipsAcrossPeriods(allFlips, tol, detail=False):
    """跨周期合并：三类候选（密集区/fib/boll）同一池、同一规则。
    合并后确定「主要来源级别」= 来源中最大的级别；多来源混合线删除 fib/pending/boll
    标记（统一按「位置线」口径），纯单来源独立线保留标记。
    @param detail  True 时每条合并项附 members 快照（成员原始价/来源/类型，供调试页展示合并前状态）
    @returns [{ price, type, touchCount, firstTouch, breakTime, sources:[...], level, srcType[, members] }]
    """
    all_ = []
    for res, flips in allFlips.items():
        for f in flips:
            item = dict(f)
            item["source"] = res
            item["_kinds"] = [_kindOf(f)]
            all_.append(item)
    all_.sort(key=lambda f: f["price"])
    merged = []
    for f in all_:
        last = merged[-1] if merged else None
        if last is not None and f["price"] - last["price"] <= tol:
            prevTouch = last["touchCount"]
            if detail:
                last["members"].append(_memberSnapshot(f))
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
            if f["_kinds"][0] not in last["_kinds"]:
                last["_kinds"].append(f["_kinds"][0])
        else:
            item = dict(f, sources=[f["source"]])
            if detail:
                item["members"] = [_memberSnapshot(item)]
            merged.append(item)
    # 确定每个合并项的主要来源级别 = 来源中最大的级别（大级别优先）
    for m in merged:
        m["level"] = dominantLevel(m["sources"])
        m.pop("source", None)
        kinds = m.pop("_kinds", [])
        # 多来源混合线：删除 fib/pending/boll 等具体来源标记，统一按「位置线」口径
        if len(kinds) > 1:
            m["srcType"] = "mixed"
            for k in ("fib", "pending", "ratio", "fromPoint", "referBi", "boll"):
                m.pop(k, None)
        elif len(kinds) == 1:
            m["srcType"] = kinds[0]
    return merged


def dominantLevel(sources):
    """从来源周期列表确定主要来源级别：取最大的级别（LEVEL_ORDER 中更靠前）。"""
    best = None
    for res in sources:
        if best is None or LEVEL_ORDER.index(res) < LEVEL_ORDER.index(best):
            best = res
    return best


def pickByLevel(merged, currentPrice, sideCount, maxDistAtr, periodAtrs,
                touchWeight=TOUCH_WEIGHT, barsWeight=BARS_WEIGHT):
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
            f["score"] = flipScore(f, group, touchWeight, barsWeight)
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


def pickNearestForDisplay(merged, displayPeriods, currentPrice, sideCount, maxDistAtr, periodAtrs):
    """按显示周期选取（新显示模型，与 JS 逐行对齐）：每个显示周期图最多 2×sideCount 条线。
    候选池 = 该级别及以上级别的合并线（高级别线继承到低周期图，如 3m 图候选池含 3/15/60/240/D
    全部位置线），取「距现价最近的上方 sideCount 条 + 下方 sideCount 条」，每条仍受
    ≤ maxDistAtr×线自身级别ATR 距离上限（periodAtrs[line.level] 缺失时 Infinity），允许上下不对称。
    @returns { 周期: [line,...] }
    """
    out = {}
    for L in displayPeriods:
        li = LEVEL_ORDER.index(L)
        pool = [f for f in merged if LEVEL_ORDER.index(f["level"]) <= li]

        def maxDistOf(f):
            levelAtr = periodAtrs.get(f["level"])
            return maxDistAtr * levelAtr if levelAtr else float("inf")

        above = sorted(
            [f for f in pool if f["price"] >= currentPrice and f["price"] - currentPrice <= maxDistOf(f)],
            key=lambda f: f["price"])[:sideCount]
        below = sorted(
            [f for f in pool if f["price"] < currentPrice and currentPrice - f["price"] <= maxDistOf(f)],
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
                       clusterParts=("flip", "recent"), with_strength=True):
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
                flip = detectFlip(c, bars, tol)
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
            item["barsPassed"] = countBarsPassing(item["price"], bars, tol)
    return out


def compute_srflip(periodBis, barsByPeriod, periods,
                   clusterAtr=CLUSTER_ATR, mergeAtr=MERGE_ATR,
                   recentClusterAtr=RECENT_CLUSTER_ATR,
                   maxDistAtr=MAX_DIST_ATR, maxPerPeriod=MAX_PER_PERIOD,
                   minTouchOverride=None, periodAtrsIn=None,
                   srTypes=DEFAULT_SR_TYPES, fibLevels=FIB_LEVELS, periodMacdIn=None,
                   bollLength=BOLL_LENGTH, bollMult=BOLL_MULT,
                   clusterParts=("flip", "recent"), minTouchsIn=None,
                   recentBiCount=RECENT_BI_COUNT,
                   touchWeight=TOUCH_WEIGHT, barsWeight=BARS_WEIGHT,
                   sideCount=SIDE_COUNT, mergeDetail=False,
                   clusterParamsByPeriod=None):
    """逐周期识别支阻位（密集区 + 黄金分割 + BOLL）并跨周期合并、按周期选取。

    @param periodBis    各周期笔 { 周期: [bis] }
    @param barsByPeriod 各周期原始K线 { 周期: [bars] }
    @param periods      周期列表（从大到小）
    @param periodAtrsIn 可选：各周期预计算 ATR { 周期: atr }（增量回测用，避免重复计算）
    @param srTypes      支阻位类型开关（"cluster" 密集区 / "fib" 黄金分割 / "boll" 布林带）
    @param fibLevels    黄金分割比率列表
    @param periodMacdIn 可选：各周期预计算 MACD { 周期: macdArr }（增量回测用）
    @param bollLength   BOLL SMA 周期（已收盘K线口径）
    @param bollMult     BOLL 标准差倍数
    @param clusterParts cluster 子开关（"flip" 强互换 / "recent" 近期极值，任意组合；全空则该周期无 cluster 候选）
    @param minTouchsIn  按级别最少触及次数 { 周期: int }，命中键优先于 minTouchOverride/默认
    @param recentBiCount 近期极值位取最近 N 根笔
    @param touchWeight/barsWeight 强度评分权重（仅 capPerPeriod 截断与 score 字段，显示选取纯按价就近）
    @param sideCount    每周期图每侧条数（总 ≤ 2×sideCount）
    @param mergeDetail  True 时 merged 各项附 members 成员快照（默认 False，输出与旧版逐键一致）
    @returns { periods: 各周期候选(密集区截断后+fib+boll), merged: 三类统一合并结果,
               drawnByPeriod: 各显示周期选中的 ≤2×sideCount 条(含来源标注),
               currentPrice: 当前价, periodAtrs: 各周期ATR }
    """
    periodAtrsIn = periodAtrsIn or {}
    periodMacdIn = periodMacdIn or {}
    allFlips = {}
    allFibs = {}
    allBolls = {}
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
        pcfg = (clusterParamsByPeriod or {}).get(str(res).upper(), {})
        localCluster = pcfg.get("clusterAtr", clusterAtr)
        tol = localCluster * atr
        minTouch = (minTouchsIn or {}).get(str(res).upper()) or minTouchFor(res, minTouchOverride)
        allFlips[res] = cluster_candidates(
            bis, bars, atr, clusterAtr=localCluster,
            recentClusterAtr=pcfg.get("recentClusterAtr", recentClusterAtr),
            recentBiCount=pcfg.get("recentBiCount", recentBiCount),
            minTouch=minTouch, clusterParts=clusterParts) if "cluster" in srTypes else []

        # 黄金分割支阻位：现算非一类买卖点（本函数无 fromTs 概念，窗口由调用方决定），
        # 每方向只取最新点，参照笔=回调前顺势笔（在全量笔上定位）
        if "fib" in srTypes:
            macd = periodMacdIn.get(res) or calcMACD(bars)
            upperRes = UPPER_OF.get(str(res).upper())
            upperBis = periodBis.get(upperRes) if upperRes else None
            buyPts = findBuyPoints(bis, upperBis, macd, intervalSecOf(res))
            sellPts = findSellPoints(bis, upperBis, macd, intervalSecOf(res))
            allFibs[res] = buildFibCandidates(bis, buyPts, sellPts, fibLevels, bars, clusterAtr * atr)

    # 每周期候选数量上限（数据层截断，仅密集区；fib/boll 评分语义不适用，豁免）
    allFlipsCapped = capPerPeriod(allFlips, maxPerPeriod, touchWeight, barsWeight)

    # 当前价格：用最小有数据周期的最后一根K线收盘价（各周期收盘价接近，取最小周期最精确）
    currentPrice = None
    for k in ("3", "15", "60", "240", "D"):
        if k in lastCloseByRes:
            currentPrice = lastCloseByRes[k]
            break

    # BOLL 布林带候选：中轨按现价侧，需 currentPrice 已知后生成（逐周期独立，三轨一组）
    if "boll" in srTypes:
        for res in periods:
            if res not in periodAtrs:
                continue
            bars = barsByPeriod.get(res, []) or []
            if bars:
                allBolls[res] = buildBollCandidates(bars, bollLength, bollMult, currentPrice)

    # 统一合并池：三类候选（密集区截断后 + fib + boll）全部进 mergeFlipsAcrossPeriods（同一池、同一规则）。
    # 合并容差按「最小有数据的周期 ATR」缩放：小级别价位密集，按最小周期ATR只合并真正的「同一价位」。
    combined = {}
    for res in periods:
        arr = (allFlipsCapped.get(res, []) or []) + (allFibs.get(res, []) or []) + (allBolls.get(res, []) or [])
        if arr:
            combined[res] = arr
    atrValues = [periodAtrs[r] for r in periods
                 if r in combined and combined[r] and r in periodAtrs]
    minAtr = min(atrValues) if atrValues else 0
    mergeTol = mergeAtr * minAtr
    mergedOut = mergeFlipsAcrossPeriods(combined, mergeTol, detail=mergeDetail)

    # 按显示周期选取：每周期图 ≤ 2×sideCount 条（就近上下各 N，高级别线继承到低周期图）。
    # 显示周期 = 成功处理（有 ATR/K线）的周期，顺序沿 periods（从大到小）。
    displayPeriods = [r for r in periods if r in periodAtrs]
    drawnByPeriod = pickNearestForDisplay(mergedOut, displayPeriods, currentPrice,
                                          sideCount, maxDistAtr, periodAtrs) \
        if currentPrice is not None else {}
    for L, lines in drawnByPeriod.items():
        for f in lines:
            f["label"] = labelOf(f)

    periodsOut = {}
    for res, group in allFlipsCapped.items():
        periodsOut[res] = group + (allFibs.get(res, []) or []) + (allBolls.get(res, []) or [])
    # 纯 boll/fib 周期可能未进 allFlipsCapped（cluster 关闭时），补齐
    for res in periods:
        if res not in periodsOut:
            arr = (allFibs.get(res, []) or []) + (allBolls.get(res, []) or [])
            if arr:
                periodsOut[res] = arr

    return {
        "periods": periodsOut,
        "merged": mergedOut,
        "drawnByPeriod": drawnByPeriod,
        "currentPrice": currentPrice,
        "periodAtrs": periodAtrs,
    }
