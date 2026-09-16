# -*- coding: utf-8 -*-
"""
进出场逻辑（Python 移植版，与 .cursor/skills/mark-entry/scripts/mark_entry.js 对齐）

纯函数模块：依赖「交易计划」（trading_plan）结果判定各周期当前进场状态，
映射到 6 种进场策略，校验该策略的进场条件后生成进场信号：
  - 买点（多头）= 红色向上箭头（arrow_up）
  - 卖点（空头）= 向下绿色箭头（arrow_down）

信号画在「背驰级别」（更低周期）。出场规则（止损 + 滑点/兜底 + 三档止盈）的状态机由
backtest.BacktestEngine 增量推进；本模块提供出场构件（与 mark_entry.js 对齐）：
  - stop_ref_of        方向感知止损参考位（支阻位 ± 滑点，无正确侧位兜底 进场价 ± 滑点，永不为 None）
  - trend_following_of 顺势/逆势判定（计划 direction ∈ {多头多,空头空} 为顺势）
  - forming_seg_ready  检测周期形成段「合并后≥5根K成笔预期」判定（TP2 / 逆势 TP3b 事件源）
  - find_bi_event      笔事件查找（TP1/TP3a 事件源，返回含 startTime）

不连接 CDP、不绘图；回测链路通过 compute_entries 直接调用。
"""

import bisect

from .chan_core import (
    calcATR, calcMACD, isBiDiverge, lowerResOf, buildZSByUpper, intervalSecOf,
    CHAN_CFG, biMacdMetrics, _areaDurComparable,
)
from .trading_plan import TREND_RES, trend_state_of

# 箭头颜色：买点（多头）红色、卖点（空头）绿色
BUY_COLOR = "#F23645"
SELL_COLOR = "#089981"

# 靠近支阻位阈值（绝对价差，不乘 ATR；2026-09-13 起 ×ATR 语义改为直接输入，
# 与三个滑点同单位——工作台/回测界面/JS CLI 同参同名同默认）
NEAR = 10.0

# ---- 出场参数（2026-09-09 出场阶梯重构；与 mark_entry.js / CLI / Web 回测界面同名） ----
# 进场手数（盈亏 = 价格差 × 方向 × lots）
DEFAULT_LOTS = 4
# 止损位滑点（绝对价格）：正确侧支阻位外侧偏移（short 上方+ / long 下方−）
DEFAULT_SLIP_STOP = 3.0
# 兜底止损滑点：无正确侧支阻位时 止损 = 进场价 ± slip_fallback（止损位永不为 None）
DEFAULT_SLIP_FALLBACK = 10.0
# 保本滑点：保本止损位 beStop = 进场成交K线极值 ± slip_be（short: high+ / long: low−）
DEFAULT_SLIP_BE = 3.0
# 形成段「成笔预期」门槛：合并后 ≥5 根K（chan_core.isValid gap>=4 同口径）
EXIT_MIN_MERGED = 5
# 出中枢力度衰减比例：离开笔 span < 进入笔 span × ratio 即力度变弱（参数中心可调）
ZS_EXIT_WEAK_RATIO = 1.0
# 顺势（计划方向=多头多/空头空）判定集合；plan_direction 缺失时按 strategyKey 兜底
TREND_PLAN_DIRS = {"多头多", "空头空"}
TREND_STRATEGY_KEYS = {"wait2Buy", "waitBuy", "wait2Sell", "waitSell"}


# ============================================================
# 背驰识别算法
# ============================================================


def findDivergePoints(bis, macdArr):
    """识别某周期的背驰点（做多=底背驰，做空=顶背驰）。
    参考 chan_core.findBuyPoints/findSellPoints 的候选逻辑，但不做区间套/锚定：
      - 底背驰：下跌笔创新低 + MACD 背驰（绿柱面积变小 或 DIF低点抬高）
      - 顶背驰：上涨笔创新高 + MACD 背驰（红柱面积变小 或 DIF高点变低）
    参照笔 = 向前最近同向笔（跳过幅度 < 当前 50% 的次级别回调）。
    @returns [{ time, price, direction }] direction='long'（做多）|'short'（做空）
    """
    if not bis or len(bis) < 3:
        return []
    points = []

    # 做多（底背驰）：下跌笔创新低 + 背驰
    downIdx = [i for i, b in enumerate(bis) if b["type"] == "down"]
    for k in range(1, len(downIdx)):
        cur = bis[downIdx[k]]
        refer = None
        for j in range(k - 1, -1, -1):
            cand = bis[downIdx[j]]
            if cand["span"] < cur["span"] * 0.5:
                continue  # 跳过幅度不足的次级别回调
            refer = cand
            break
        if refer is not None and cur["endPrice"] < refer["endPrice"] and isBiDiverge(cur, refer, macdArr):
            points.append({"time": cur["endTime"], "price": cur["endPrice"],
                           "direction": "long", "referStart": refer["startTime"]})

    # 做空（顶背驰）：上涨笔创新高 + 背驰
    upIdx = [i for i, b in enumerate(bis) if b["type"] == "up"]
    for k in range(1, len(upIdx)):
        cur = bis[upIdx[k]]
        refer = None
        for j in range(k - 1, -1, -1):
            cand = bis[upIdx[j]]
            if cand["span"] < cur["span"] * 0.5:
                continue
            refer = cand
            break
        if refer is not None and cur["endPrice"] > refer["endPrice"] and isBiDiverge(cur, refer, macdArr):
            points.append({"time": cur["endTime"], "price": cur["endPrice"],
                           "direction": "short", "referStart": refer["startTime"]})

    return points


# ============================================================
# 区间套下沉判定（SPEC_divergence_chanset.md 规则 1/2/3）
# ============================================================
#
# 规则 1（展开下沉）：候选顶/底 P，从检测周期 X 的「以 P 为终点的笔」开始——该级笔内部，
#   若次一级别存在 ≥3 笔结构（段间不跨该级上级笔边界，含形成中延伸段计 1 笔）且末段
#   终点即 P → 下沉到次级别，重复；次级别展开不足 3 笔 → 停止下沉，在本级判定。
# 规则 2（跨级禁止）：候选背驰段的参照笔必须与候选段同处于其所属上级笔内部；
#   参照属更早的上级笔（跨级比较）→ 候选无效。
# 规则 3（归属）：markRes = 下沉停止的那一级。下沉上限 = 检测周期 X（不越过 X 向上归属）。


def _upperResOf(periodData, res):
    """periodData 中 res 的最近上级（sec 更大的最小者，须有笔数据）；无则 None。"""
    sec = intervalSecOf(res) or 0
    up = None
    for r, pd in periodData.items():
        s = intervalSecOf(r) or 0
        if s > sec and (up is None or s < intervalSecOf(up)) and pd and pd.get("bis"):
            up = r
    return up


def levelsBelow(periodData, X):
    """检测周期 X 之下的**连续**低级别链（逐级映射 lowerResOf：60→15→3；3 之下挂 30S）。
    某级无笔数据（<3 笔）则链在该级截断——区间套逐级下沉、不可跳级
    （60 与 3 之间缺 15 时链止于 60，不得由 60 直接下到 3）。"""
    chain = []
    cur = X
    while True:
        nxt = lowerResOf(cur)
        if nxt is None and str(cur) == "3":
            nxt = "30S"  # 3 分钟之下挂 30 秒（--with-30s 时加载）
        if nxt is None:
            break
        pd = periodData.get(nxt)
        if not pd or not pd.get("bis") or len(pd["bis"]) < 3:
            break
        chain.append(nxt)
        cur = nxt
    return chain


def filterDetectPeriods(periods):
    """检测周期 = 自身之下存在**已加载**更低级别的周期（不含 D/30S）。
    下一级映射与 levelsBelow 一致：lowerResOf（240→60→15→3），3 之下挂 30S。
    30S 未加载时 3 之下无级别 → 3 不作为检测周期（最小检测 15m，背驰最深 3m），
    避免 realtime 模式下沉链为空、背驰落在检测周期自身（det=3/div=3）。"""
    loaded = {str(p).upper() for p in periods}
    out = []
    for p in periods:
        pu = str(p).upper()
        if pu in ("D", "30S"):
            continue
        nxt = lowerResOf(pu)
        if nxt is None and pu == "3":
            nxt = "30S"
        if nxt is None or nxt not in loaded:
            continue
        out.append(p)
    return out


def biEndingAt(bis, pTime, tol, wantType):
    """找「以 pTime 为终点」的笔：从尾部向前找第一根 endTime 与 pTime 相差 ≤ tol
    且方向匹配的笔（不同级别端点时间有不超过 1 根本级 bar 的偏移，区间套同一结构点）。"""
    for i in range(len(bis) - 1, -1, -1):
        b = bis[i]
        if wantType is not None and b["type"] != wantType:
            continue
        if abs(b["endTime"] - pTime) <= tol:
            return b
    return None


def _upperContainingBi(periodData, res, bi):
    """找 res 最近上级中包含笔 bi 的上级笔（bi 区间落在上级笔区间内，容差 1 根上级 bar；
    末笔视为开放段 +∞——形成中的上级笔，与 buildZSByUpper open_last 口径一致）。"""
    up = _upperResOf(periodData, res)
    if up is None:
        return None
    tol = intervalSecOf(up) or 0
    ub = periodData[up]["bis"]
    for i, u in enumerate(ub):
        last_open = (i == len(ub) - 1)
        if u["startTime"] - tol <= bi["startTime"] and (u["endTime"] + tol >= bi["endTime"] or last_open):
            return u
    return None


def _bisect_ge_key(bis, key, t):
    """二分：笔列表按 key 字段（startTime/endTime）升序，返回首个 bis[key] >= t 的索引。
    30S 级笔列表可达数万根，从索引 0 线性扫描会随窗口平方级放大。"""
    lo, hi = 0, len(bis)
    while lo < hi:
        mid = (lo + hi) // 2
        if bis[mid][key] < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_gt_key(bis, key, t):
    """二分：返回首个 bis[key] > t 的索引（key 升序）。"""
    lo, hi = 0, len(bis)
    while lo < hi:
        mid = (lo + hi) // 2
        if bis[mid][key] <= t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _expansionCount(bisL, parentBi, endT, tolStart, tolEnd):
    """数 lower 级笔在上级笔 parentBi 内部的展开笔数：startTime ≥ parentBi.startTime - tolStart
    且 endTime ≤ endT + tolEnd（段间不跨上级笔边界；含形成中延伸段计 1 笔）。
    startTime 升序 → 二分定位下界，只扫窗口内元素。"""
    lo_t = parentBi["startTime"] - tolStart
    i = _bisect_ge_key(bisL, "startTime", lo_t)
    hi_t = endT + tolEnd
    n = len(bisL)
    cnt = 0
    while i < n and bisL[i]["startTime"] <= hi_t:
        cnt += 1
        i += 1
    return cnt


def _virtualBi(afterBi, pTime):
    """虚拟形成笔：X 末段端点已过（如 60m 平台顶 4464.23 后的近等后顶 4461.7，
    图表最终结构经「近等双顶取后顶」并入同一笔；当下时刻该替换尚未确认）——
    以「末段端点 → P」的开放段作为容器参与展开计数（SPEC v1 口径：上级笔未闭合，
    用延伸中的上级笔 + 已闭合段计数）。"""
    return {"startTime": afterBi["endTime"], "endTime": pTime,
            "type": "down" if afterBi["type"] == "up" else "up",
            "startPrice": afterBi["endPrice"], "endPrice": None}


def counterMoveQualifies(times, lastBi, tCut, enabled=None):
    """检测周期「预期够笔」（回测页面可选，默认开，SPEC_divergence_fallback M4）：
    末笔方向与策略方向相反（回调/反弹的反向段尚未确认为笔——笔端点分型需右邻K线收盘，
    固有 1 根本级K线滞后）时，末笔端点后已走出 ≥ expectBiMinBars 根本级K线即视为
    「回调中/反弹中」。安全性：末笔未被延伸（无同向新极值）说明反向段未被否定；
    反向段的成笔质量由下沉链展开 ≥3 与背驰/支阻闸门保证。
    @param enabled 显式开关（页面参数）；None → 读 CHAN_CFG.expectBiEnough（默认开）。
           False = 恢复旧口径：末笔必须已是确认的反向笔（分型右邻收盘）。
    @returns True/False（开关关闭或 K线数不足 → False）"""
    if enabled is None:
        enabled = CHAN_CFG.get("expectBiEnough")
    if not enabled:
        return False
    minBars = int(CHAN_CFG.get("expectBiMinBars", 5) or 5)
    # 端点后的K线数（严格不含端点所在根；含当根未收盘K线，与闸① _barsSince 口径一致）
    i0 = bisect.bisect_right(times, lastBi["endTime"])
    i1 = bisect.bisect_right(times, tCut)
    return (i1 - i0) >= minBars


def _sinkChainRealtimeNodes(periodData, X, wantDir, tCut=None, periodTimes=None,
                            expectBiEnabled=None):
    """当下制下沉链（逐级节点版，供 realtimeLowerDiverge 回退用；判定与原实现逐行等价）：
    返回节点列表 [ {res, F, parentBi} ... ]——
      - 节点 0 = X 自身（F = X 末段，parentBi = X 的上级包含笔，仅停止级=X 时用作 containment）；
      - 其后每级 = 成功下沉一级（段内展开 ≥3），parentBi = 该级段所在容器（上级笔/虚拟形成笔）。
    链不通（X 末段方向不符且预期够笔不成立）返回 []。
    容器判定：① 两级共享当下极值 → 上级末段；② 下级末段越过上级端点 → 虚拟开放段；
    预期够笔模式（末笔反向、tCut/periodTimes 给定且 counterMoveQualifies 通过）：以
    「末笔端点 → 下级末段当下极值」的虚拟形成笔为承载笔下沉（仅首个下沉层；下级末段
    未越过末笔端点 → 断链），后续层级恢复正常容器判定。"""
    wantType = "down" if wantDir == "long" else "up"
    bisX = (periodData.get(X) or {}).get("bis") or []
    if not bisX:
        return []
    expectMode = bisX[-1]["type"] != wantType
    if expectMode:
        timesX = (periodTimes or {}).get(X)
        if (tCut is None or not timesX
                or not counterMoveQualifies(timesX, bisX[-1], tCut, enabled=expectBiEnabled)):
            return []  # 末笔反向且反向段未预期成笔 → 无链（保持旧口径的短路行为）
    nodes = [{"res": X, "F": bisX[-1], "parentBi": None}]
    C, B_C = X, bisX[-1]
    for L in levelsBelow(periodData, X):
        secC = intervalSecOf(C) or 0
        secL = intervalSecOf(L) or 0
        bisL = periodData[L]["bis"]
        F_L = bisL[-1]  # L 的形成中段（已延伸到当下极值）
        if F_L["type"] != wantType or F_L["endTime"] <= B_C["startTime"]:
            break  # L 末段非候选段（方向不符/早于容器起点）
        if expectMode:
            if F_L["endTime"] <= B_C["endTime"]:
                break  # 下级末段未越过末笔端点（反向段尚未走出本级端点）→ 断链
            container = _virtualBi(B_C, F_L["endTime"])  # 预期段承载笔：末笔端点 → 当下极值
        elif abs(F_L["endTime"] - B_C["endTime"]) <= secC:
            container = B_C                       # ① 两级共享当下极值
        elif F_L["endTime"] > B_C["endTime"] + secC:
            container = _virtualBi(B_C, F_L["endTime"])  # ② X 端点已过 → 虚拟开放段
        else:
            break
        cnt = _expansionCount(bisL, container, F_L["endTime"], secC, secL)
        if cnt >= 3:
            nodes.append({"res": L, "F": F_L, "parentBi": container})
            C, B_C = L, F_L
            expectMode = False  # 预期承载笔只用于首个下沉层，其后恢复正常容器判定
        else:
            break
    # 从未下沉（停止级 = X）：参照 containment 用 X 的上级包含笔（开放末笔）
    nodes[0]["parentBi"] = _upperContainingBi(periodData, X, bisX[-1])
    return nodes


def sinkChainRealtime(periodData, X, wantDir):
    """当下制下沉链：P = 候选顶/底（随下沉逐级发现的各级末段当下极值）。
    X 级承载笔 B_X 两种形态：
      ① 末段终点 ≈ P（通常：X 末段与下级末段共享当下极值）→ B_X = X 末段；
      ② 下级末段越过 X 末段端点（X 端点已过、后续反向结构未确认为笔，案例 A）→
         B_X = 虚拟形成笔（X 末段端点 → P 开放段），段内展开 ≥3 同样下沉。
    @returns (stopRes, parentBi)：stopRes=下沉停止级（可为 X 自身）；parentBi=停止级段
    的所属上级笔（规则2 参照 containment 窗口）；链不通返回 (None, None)。"""
    nodes = _sinkChainRealtimeNodes(periodData, X, wantDir)
    if not nodes:
        return None, None
    if len(nodes) == 1:
        return nodes[0]["res"], nodes[0]["parentBi"]
    return nodes[-1]["res"], nodes[-1]["parentBi"]


def sinkChainConfirm(periodData, X, pTime, pDir):
    """确认制下沉链：P = 已完成背驰笔端点。X 级承载笔 B_X：
      ① X 级存在「以 P 为终点」的笔（任意位置，含末段延伸笔）→ 该笔；
      ② P 晚于 X 末段端点（X 端点已过、后续反向结构未确认，案例 A 同型）→ 虚拟形成笔；
      其余（P 早于末段端点且无精确匹配）→ 无链（候选无效）。
    @returns (stopRes, parentBi)，语义同 sinkChainRealtime；stopRes=None 表示无链。"""
    wantType = "up" if pDir == "short" else "down"
    bisX = (periodData.get(X) or {}).get("bis") or []
    if not bisX:
        return None, None
    secX = intervalSecOf(X) or 0
    B_C = biEndingAt(bisX, pTime, secX, wantType)
    if B_C is None:
        if pTime > bisX[-1]["endTime"] + secX:
            B_C = _virtualBi(bisX[-1], pTime)  # ② 虚拟形成笔（开放段）
        else:
            return None, None
    C = X
    parentBi = None
    for L in levelsBelow(periodData, X):
        secC = intervalSecOf(C) or 0
        secL = intervalSecOf(L) or 0
        bisL = periodData[L]["bis"]
        F_L = biEndingAt(bisL, pTime, secL, wantType)
        if F_L is None:
            break
        cnt = _expansionCount(bisL, B_C, F_L["endTime"], secC, secL)
        if cnt >= 3:
            parentBi, C, B_C = B_C, L, F_L
        else:
            break
    if parentBi is None and C == X:
        parentBi = _upperContainingBi(periodData, X, B_C)
    return C, parentBi


# ============================================================
# 进场状态 → 策略映射（依赖交易计划 plan 结果）
# ============================================================


def entryStrategyOf(planStrategy):
    """交易计划策略 → 进场策略映射（用户规则）。
    震荡/数据不足/趋势中无匹配（方向=观望）等不产生进场策略，返回 None。
    @returns None 或 { key, direction, label }
    """
    mapping = {
        "等待反弹后做2卖": {"key": "wait2Sell", "direction": "short", "label": "等待反弹后做2卖"},
        "等待回调后做2买": {"key": "wait2Buy", "direction": "long", "label": "等待回调后做2买"},
        "等待高点附近的一卖": {"key": "wait1Sell", "direction": "short", "label": "等待一卖"},
        "等待低点附近的一买": {"key": "wait1Buy", "direction": "long", "label": "等待一买"},
        "等待回调后的新买点": {"key": "waitBuy", "direction": "long", "label": "等待回调后买点"},
        "等待反弹后的新卖点": {"key": "waitSell", "direction": "short", "label": "等待反弹后卖点"},
    }
    return mapping.get(planStrategy)


# ============================================================
# 6 种进场策略的条件判定（纯函数）
# ============================================================


def lastBiOk(bis, wantType):
    """够笔：最后一笔是否为预期方向（空头→up 反弹、多头→down 回调）。"""
    if not bis or len(bis) == 0:
        return False
    return bis[-1]["type"] == wantType


def brokePrevLow(bis):
    """下跌段破前底：最近完成的一笔 down 笔终点，跌破更早最近同向 down 笔终点（创新低）。"""
    if not bis or len(bis) < 2:
        return False
    lastDownIdx = -1
    for i in range(len(bis) - 1, -1, -1):
        if bis[i]["type"] == "down":
            lastDownIdx = i
            break
    if lastDownIdx <= 0:
        return False
    prevLow = float("inf")
    for i in range(lastDownIdx - 1, -1, -1):
        if bis[i]["type"] != "down":
            continue
        prevLow = bis[i]["endPrice"]
        break
    if prevLow == float("inf"):
        return False
    return bis[lastDownIdx]["endPrice"] < prevLow


def brokePrevHigh(bis):
    """上涨段过前高：最近完成的一笔 up 笔终点，突破更早最近同向 up 笔终点（创新高）。"""
    if not bis or len(bis) < 2:
        return False
    lastUpIdx = -1
    for i in range(len(bis) - 1, -1, -1):
        if bis[i]["type"] == "up":
            lastUpIdx = i
            break
    if lastUpIdx <= 0:
        return False
    prevHigh = float("-inf")
    for i in range(lastUpIdx - 1, -1, -1):
        if bis[i]["type"] != "up":
            continue
        prevHigh = bis[i]["endPrice"]
        break
    if prevHigh == float("-inf"):
        return False
    return bis[lastUpIdx]["endPrice"] > prevHigh


def macdBelowZero(macdArr):
    """MACD 当前未破 0 轴太多（dif < +macdZeroTol，默认容差 5）：下0轴后反弹不过0轴太多，
    空头动能还在（原严格口径 dif < 0，tol=0 回退；2026-09-16）。"""
    if not macdArr or len(macdArr) == 0:
        return False
    tol = float(CHAN_CFG.get("macdZeroTol", 5.0) or 0.0)
    return macdArr[-1]["dif"] < tol


def macdAboveZero(macdArr):
    """MACD 当前未破 0 轴太多（dif > -macdZeroTol，默认容差 5）：上0轴后回调不破0轴太多，
    多头动能还在（原严格口径 dif > 0，tol=0 回退；2026-09-16）。"""
    if not macdArr or len(macdArr) == 0:
        return False
    tol = float(CHAN_CFG.get("macdZeroTol", 5.0) or 0.0)
    return macdArr[-1]["dif"] > -tol


def zsExitWeak(bis, upperBis, macdArr, barSec, ratio=1.0, wantDir="short"):
    """出中枢的力度变弱（buildZSByUpper 取最后一个中枢）：
    离开中枢的笔相对进入中枢的笔 isBiDiverge 为 true，或离开笔 span < 进入笔 span × ratio。

    性能：realtime 每根 fine 收盘可能反复进入 wait1Buy/wait1Sell；按笔快照缓存
    buildZSByUpper 结果（末笔延伸即失效），输出与未缓存路径逐位一致。"""
    zss = _zs_by_upper_cached(bis, upperBis, barSec)
    if not zss:
        return False
    last = zss[-1]
    if last is None:
        return False
    enter = next((b for b in bis if b["endTime"] == last["enterEndTime"]), None)
    if enter is None:
        return False
    # 离开笔 = exitTime 对应的笔（exitStartTime 为离开笔起点时间，原笔对象时间戳精确匹配）
    exitBi = next((b for b in bis if b["startTime"] == last["exitStartTime"]), None) \
        if last.get("exitTime") is not None else None
    if exitBi is None:
        return False  # 中枢未离开（仍在延伸），无「出中枢」力度可言
    # 期望的离开方向过滤：一卖应向上离开中枢、一买应向下离开中枢
    if wantDir == "short" and exitBi["type"] != "up":
        return False
    if wantDir == "long" and exitBi["type"] != "down":
        return False
    # 力度变弱：离开笔相对进入笔 MACD 背驰 或 离开笔幅度小于进入笔幅度 × ratio
    if isBiDiverge(exitBi, enter, macdArr):
        return True
    if exitBi["span"] < enter["span"] * (ratio or 1.0):
        return True
    return False


# buildZSByUpper 快照缓存：键含下级/上级笔数量与末笔端点；末笔延伸即 miss。
_zsByUpperCache = {}
_ZS_CACHE_MAX = 256


def _zs_by_upper_cached(bis, upperBis, barSec):
    """按笔快照缓存 buildZSByUpper；列表内容未变时复用同一结果。"""
    upperBis = upperBis or []
    lb = bis[-1] if bis else None
    ub = upperBis[-1] if upperBis else None
    key = (
        id(bis), len(bis) if bis else 0,
        lb.get("startTime") if lb else None,
        lb.get("endTime") if lb else None,
        lb.get("endPrice") if lb else None,
        id(upperBis), len(upperBis),
        ub.get("startTime") if ub else None,
        ub.get("endTime") if ub else None,
        barSec or 0,
    )
    ent = _zsByUpperCache.get(key)
    if ent is not None and ent[0] is bis and ent[1] is upperBis:
        return ent[2]
    try:
        zss = buildZSByUpper(bis, upperBis, barSec)
    except Exception:
        zss = []
    if len(_zsByUpperCache) >= _ZS_CACHE_MAX:
        _zsByUpperCache.clear()
    _zsByUpperCache[key] = (bis, upperBis, zss)
    return zss


def lowerDiverge(periodData, X, wantDir):
    """以下级别出现背驰（区间套下沉版，SPEC_divergence_chanset 规则 1/2/3）：
    收集所有更低周期中方向匹配的背驰点后逐个做下沉链校验，仅保留
    「候选级别 == 该点下沉停止级」且「参照笔与候选段同处其所属上级笔内部」的候选；
    按时间**降序**返回（最新的在前）。
    调用方（evaluateEntry）依次尝试候选并做支阻位校验，失败回退次新——过滤后所见
    候选已全部下沉合法，天然不会把高级别候选替换成跨级 3m/30S 微点。
    @returns [ { res, point:{time,price,direction,referStart} }, ... ] 按 point.time 降序
    """
    xSec = intervalSecOf(X) or float("inf")
    cands = []
    for res, pd in periodData.items():
        sec = intervalSecOf(res) or 0
        if sec >= xSec:
            continue  # 只取更低级别
        if not pd or not pd.get("bis") or len(pd["bis"]) < 3:
            continue
        try:
            pts = findDivergePoints(pd["bis"], pd.get("macdArr"))
        except Exception:
            continue
        for p in pts:
            if p["direction"] != wantDir:
                continue
            cands.append({"res": res, "point": p})
    filtered = []
    for c in cands:
        stopRes, parentBi = sinkChainConfirm(periodData, X, c["point"]["time"], c["point"]["direction"])
        if stopRes != c["res"]:
            continue  # 规则 1/3：该点的下沉停止级不是候选级别（其结构已在停止级或更高级判定）
        if parentBi is not None:
            tol = intervalSecOf(c["res"]) or 0
            if (c["point"].get("referStart") or 0) < parentBi["startTime"] - tol:
                continue  # 规则 2：参照跨出候选段所属上级笔 → 候选无效
        filtered.append(c)
    filtered.sort(key=lambda c: c["point"]["time"], reverse=True)  # 最新在前
    return filtered


def nearSr(price, srLevels, nearTol):
    """在支阻位附近：背驰点价与任一 srLevels 支阻位价差 ≤ nearTol。
    @returns None 或 { sr, dist } 最近命中的支阻位
    """
    if not srLevels or len(srLevels) == 0:
        return None
    best = None
    for sr in srLevels:
        d = abs(sr["price"] - price)
        if d <= nearTol and (best is None or d < best["dist"]):
            best = {"sr": sr, "dist": d}
    return best


def strategyExtraOk(key, bis, upperBis, macdArr, barSec, zs_exit_weak_ratio=ZS_EXIT_WEAK_RATIO):
    """各策略专属条件（原 evaluateEntry 第 2 步抽取为独立函数，确认制/当下制共用）。
    zs_exit_weak_ratio：出中枢力度衰减比例（参数中心可调，默认 ZS_EXIT_WEAK_RATIO）。
    @returns None（全部通过）或 失败原因字符串"""
    if key == "wait2Sell":
        if not brokePrevLow(bis):
            return "下跌段未破前底"
        if not macdBelowZero(macdArr):
            return "MACD 未下0轴或反弹过0轴"
    elif key == "wait2Buy":
        if not brokePrevHigh(bis):
            return "上涨段未过前高"
        if not macdAboveZero(macdArr):
            return "MACD 未上0轴或回调破0轴"
    elif key == "wait1Sell":
        if not brokePrevHigh(bis):
            return "未够笔且过高点"
        if not zsExitWeak(bis, upperBis, macdArr, barSec, zs_exit_weak_ratio, "short"):
            return "出中枢力度未变弱"
    elif key == "wait1Buy":
        if not brokePrevLow(bis):
            return "未够笔且过低点"
        if not zsExitWeak(bis, upperBis, macdArr, barSec, zs_exit_weak_ratio, "long"):
            return "出中枢力度未变弱"
    # waitBuy / waitSell：仅需够笔 + 以下级别背驰 + 支阻位附近
    return None


def evaluateEntry(ctx, strategy):
    """校验某个进场策略的全部条件（在检测周期 X 上）。
    公共条件：够笔 + 以下级别背驰 + 在支阻位附近；按策略附加专属条件。
    @returns { ok, reason?, markRes?, point?, nearSr? }，ok=True 时 markRes=背驰所在更低周期、
             point=背驰点、nearSr=命中支阻位价格
    """
    res = ctx["res"]
    bis = ctx["bis"]
    upperBis = ctx.get("upperBis")
    macdArr = ctx.get("macdArr")
    atr = ctx.get("atr", 0)
    barSec = ctx.get("barSec", 0)
    near = ctx.get("near", NEAR)
    srLevels = ctx.get("srLevels")
    periodData = ctx.get("periodData")
    key = strategy["key"]
    direction = strategy["direction"]
    wantType = "up" if direction == "short" else "down"  # 空头等反弹(up)，多头等回调(down)
    divergeDir = direction  # 空头→顶背驰(short)，多头→底背驰(long)

    # 1. 够笔
    if not lastBiOk(bis, wantType):
        return {"ok": False,
                "reason": f"最后一笔为 {bis[-1]['type'] if bis else '?'}，需 {wantType}（反弹/回调不够笔）"}

    # 2. 各策略专属条件（与当下制共用 strategyExtraOk）
    extraReason = strategyExtraOk(key, bis, upperBis, macdArr, barSec,
                                  ctx.get("zs_exit_weak_ratio", ZS_EXIT_WEAK_RATIO))
    if extraReason is not None:
        return {"ok": False, "reason": extraReason}

    # 3+4. 以下级别背驰候选（按时间降序）依次做支阻位校验，失败回退次新点：
    #      策略专属条件不依赖背驰点（第2步已过），只需重试「支阻位附近」。
    #      （--with-30s 后 30S 高频背驰点常抢占原级别点，且其微观极值价常远离支阻位——
    #       无回退时信号彻底消失：旧点被抢占丢弃、新点校验被拒，两边都不出箭头。）
    cands = lowerDiverge(periodData, res, divergeDir)
    if not cands:
        return {"ok": False, "reason": "以下级别无匹配方向背驰"}

    nearTol = near  # 绝对价差（2026-09-13 起不再乘 ATR）
    for c in cands:
        hit = nearSr(c["point"]["price"], srLevels, nearTol)
        if hit is not None:
            return {"ok": True, "markRes": c["res"], "point": c["point"], "nearSr": hit["sr"]["price"]}
    return {"ok": False, "reason": "以下级别背驰点均远离支阻位"}


# ============================================================
# 当下背驰（实时判断）：形成中段创新低/新高 + MACD 当拍对比，无需反向笔确认
# ============================================================

# 形成中段最小K线数（够笔门槛）：isValid 要求合并K线 ≥5 根，这里用原始K线数 ≥5 作
# 宽松代理——避免 1-2 根K线的微回调/微反弹触发，同时不引入合并结构重算
REALTIME_MIN_BARS = 5


def _barsSince(times, t0, tCut):
    """times（升序）中 (t0, tCut] 覆盖的K线数，供形成中段长度门槛。"""
    if not times:
        return 0
    i0 = bisect.bisect_left(times, t0)
    i1 = bisect.bisect_right(times, tCut)
    return i1 - i0


def _nearEqualTol(periodData, res, referPrice):
    """M2（默认关，nearEqualAtrK/nearEqualPct <= 0）：创新低/新高的近等容差
    = max(nearEqualAtrK × 该级 ATR, nearEqualPct × |参照价|)；与 60m 近等双底同口径。"""
    kA = float(CHAN_CFG.get("nearEqualAtrK", 0.0) or 0.0)
    kP = float(CHAN_CFG.get("nearEqualPct", 0.0) or 0.0)
    if kA <= 0 and kP <= 0:
        return 0.0
    atr = (periodData.get(res) or {}).get("atr") or 0.0
    return max(kA * atr, kP * abs(referPrice))


def _biDivergeStrictAll(bi, refer, macdArr):
    """M2 近等带内护栏：背驰三判据 AND（isBiDiverge 为 OR）——近等（未严格创新极值）
    的候选要求动能全面衰竭，防普通双底误判为背驰。"""
    cur = biMacdMetrics(bi, macdArr)
    ref = biMacdMetrics(refer, macdArr)
    if cur is None or ref is None:
        return False
    if bi["type"] == "down":
        return ((not _areaDurComparable(bi, refer) or cur["greenArea"] < ref["greenArea"])
                and cur["difLow"] > ref["difLow"]
                and cur["greenMax"] < ref["greenMax"])
    return ((not _areaDurComparable(bi, refer) or cur["redArea"] < ref["redArea"])
            and cur["difHigh"] < ref["difHigh"]
            and cur["redMax"] < ref["redMax"])


def _evalRealtimeNode(periodData, node, wantDir, tCut, periodTimes, minBars):
    """对下沉链单个节点（某级形成中段）评当下背驰候选（原停止级逻辑的逐级抽出版）。
    @returns None 或 { res, point:{time,price,direction}, segStart, nearEqual? }"""
    res, F, parentBi = node["res"], node["F"], node["parentBi"]
    pd = periodData.get(res) or {}
    bis = pd.get("bis") or []
    if len(bis) < 2:
        return None  # 需有参照笔
    wantType = "down" if wantDir == "long" else "up"
    if F["type"] != wantType:
        return None
    times = (periodTimes or {}).get(res)
    if not times:
        times = [b["time"] for b in (pd.get("bars") or [])]
    if _barsSince(times, F["startTime"], tCut) < minBars:
        return None  # 段太短（微回调/微反弹），不算够笔
    # 参照笔：向前最近同向已完成笔（不含形成中段），跳过幅度不足的次级别回调；
    # 规则 2：参照须与 F 同处上级笔内部（更早的参照只会更靠外，直接无效）
    refer = None
    secS = intervalSecOf(res) or 0
    for j in range(len(bis) - 2, -1, -1):
        cand = bis[j]
        if cand["type"] != F["type"]:
            continue
        if cand["span"] < F["span"] * 0.5:
            continue
        if parentBi is not None and cand["startTime"] < parentBi["startTime"] - secS:
            break  # 参照跨出所属上级笔 → 候选无效（不回退更早）
        refer = cand
        break
    if refer is None:
        return None
    strictNew = F["endPrice"] < refer["endPrice"] if wantDir == "long" \
        else F["endPrice"] > refer["endPrice"]
    madeNew = strictNew
    if not madeNew:
        # M2 近等容差（默认关）：做多 F 略高于参照低点（F ≥ 参照，gap = F − 参照 ∈ [0, tol]）；
        # 做空对称（F 略低于参照高点）。近等二底/二顶不创新极值但动能衰竭的形态。
        tol = _nearEqualTol(periodData, res, refer["endPrice"])
        if tol > 0:
            gap = (F["endPrice"] - refer["endPrice"]) if wantDir == "long" \
                else (refer["endPrice"] - F["endPrice"])
            madeNew = 0 <= gap <= tol
    if not madeNew:
        return None
    # 当下对比 MACD：窗口取 [refer.startTime, F.endTime] 的切片（避免全量数组线性扫）
    macdArr = pd.get("macdArr") or []
    macdT = pd.get("macdTimes") or [m["time"] for m in macdArr]
    if not macdT:
        return None
    lo = bisect.bisect_left(macdT, refer["startTime"])
    hi = bisect.bisect_right(macdT, F["endTime"])
    if not isBiDiverge(F, refer, macdArr[lo:hi]):
        return None
    if not strictNew and not _biDivergeStrictAll(F, refer, macdArr[lo:hi]):
        return None  # 近等带内护栏：三判据 AND
    out = {"res": res,
           "point": {"time": F["endTime"], "price": F["endPrice"],
                     "direction": wantDir},
           "segStart": F["startTime"]}
    if not strictNew:
        out["nearEqual"] = True
    return out


def realtimeLowerDiverge(periodData, X, wantDir, tCut,
                          periodTimes=None, minBars=REALTIME_MIN_BARS,
                          divergeConfirm=None, expectBiEnabled=None):
    """当下背驰（区间套下沉版，每根 fine 收盘调用）。

    形成中段 = 笔列表最后一笔——回测引擎的增量状态已用 extendLastBiFrom
    把它延伸到最新极值（endTime/endPrice = 当下极值），天然就是"正在走的这段"。

    下沉判定（SPEC_divergence_chanset 规则 1/2/3）：对 (X, wantDir) 先走下沉链
    （P = X 末段当前极值），只在「下沉停止级 S」产候选——
    S 级展开不足 3 笔的更低级别（如与其上级 15m 笔同笔的 3m 末段）不再产候选；
    S = X（次级别展开不足、下沉链一步未走）→ **不产信号**：背驰必须落在严格
    更低级别（markRes < periodX），在本级自身形成段上判背驰等于无低级别确认。

    M1 下沉链回退（CHAN_CFG.sinkFallback，SPEC_divergence_fallback）：停止级 S 无候选
    （方向/创新低/背驰任一不过）时，沿链向上一级（仍须严格 < X）用该级形成中段 +
    该级参照笔重评，首个命中级产候选并带 fallback=True——最细级别的"动能加速"
    不再一票否决大级别已成立的背驰（如 8-7 08:45：3m 加速但 15m 真背驰）。
    关闭时保持原行为（只在停止级评估）。

    候选条件（与确认制 findDivergePoints 同一套背驰标准，对象换成该级形成中段）：
      - 段方向匹配（做多→形成中下跌段 / 做空→形成中上涨段）；
      - 段长 ≥ minBars（够笔门槛，见 REALTIME_MIN_BARS）；
      - 创新低/新高：段当前极值 < refer.endPrice（多）/ > refer.endPrice（空）；
        M2 近等容差（nearEqualAtrK/nearEqualPct，默认关）放宽为 nearTol 带内，
        且近等带内要求三判据 AND（_biDivergeStrictAll），候选带 nearEqual=True；
      - isBiDiverge（当下对比）：绿柱面积变小 或 DIF低点抬高 或 绿柱最大高度变小（OR）。
    参照笔 = 向前最近同向**已完成**笔（跳过幅度 < 当前段 50% 的次级别回调），
    且必须与候选段同处其所属上级笔内部（规则 2）——参照跨上级笔边界则候选无效。

    @param periodTimes   各周期K线时间数组（升序，二分用）；缺省时从 periodData[res].bars 现建
    @returns 候选列表（≤1 条）[ { res, point:{time,price,direction}, segStart, fallback?, nearEqual? } ]
    """
    nodes = _sinkChainRealtimeNodes(periodData, X, wantDir, tCut=tCut,
                                    periodTimes=periodTimes,
                                    expectBiEnabled=expectBiEnabled)
    if not nodes or len(nodes) < 2:
        return []  # 链不通 / 停止级=检测周期自身 → 无严格更低级别背驰，不产信号
    if divergeConfirm is None:
        divergeConfirm = bool(CHAN_CFG.get("divergeConfirm"))
    last = len(nodes) - 1
    order = range(last, 0, -1) if CHAN_CFG.get("sinkFallback") else [last]
    for depth in order:  # 停止级优先；M1 开启时逐级向粗回退（不含 X 自身）
        cand = _evalRealtimeNode(periodData, nodes[depth], wantDir, tCut,
                                 periodTimes, minBars)
        if cand is not None:
            # M4 分型确认进场（页面可选，默认当下）：极值K线的右邻K收盘（分型可见最早
            # 时刻的代理，未做合并包含严格校验）后本拍才出信号 → 下一根 fine 开盘成交。
            # 段延伸（极值后移）时确认时钟自动重置。
            if divergeConfirm:
                confirmAt = cand["point"]["time"] + 2 * (intervalSecOf(cand["res"]) or 0)
                if tCut < confirmAt:
                    return []
            if depth < last:
                cand["fallback"] = True  # 非停止级命中 = 回退候选
            return [cand]
    return []


def evaluateRealtimeEntries(periodBis, periodMacd, periodAtr, planPeriods, srLevels,
                            detectPeriods, near=NEAR, tCut=None, fired=None,
                            periodTimes=None, periodMacdTimes=None, divergeConfirm=None,
                            expectBiEnabled=None, realtimeMinBars=None,
                            zsExitWeakRatio=None, trend_res=None, trend_state=None):
    """当下模式进场评估（每根 fine 收盘调用，信号无需等反向笔确认）。

    三条件与确认制同构，差异只在"何时评"与"②用什么评"：
      ① 够笔：检测周期最后一笔（引擎中=延伸中的形成段）方向匹配且段长 ≥ REALTIME_MIN_BARS；
         预期够笔（expectBiEnough 固定口径）：末笔方向相反但端点后 ≥ expectBiMinBars 根
         本级K线即视为回调/反弹中（信号带 expectBi=True，不等反向分型右邻收盘）；
      ② 当下背驰：realtimeLowerDiverge（区间套下沉到停止级，仅在该级的形成中段上
         判创新低/新高 + 当拍 MACD 对比，SPEC_divergence_chanset 规则 1/2/3）；
      ③ 支阻位附近：形成中段当前极值价 vs srLevels（价差 ≤ near，绝对价差不乘 ATR）。
    策略专属条件与确认制共用（strategyExtraOk）。

    去重：fired 集合按 (periodX, strategyKey, markRes, 段起点时间)——每个形成段只发一次，
    段延伸（继续创新低）不重发；新段（新起点）重新评估。

    @param tCut              当前时刻（fine 收盘时间）；None 时取各周期数据末尾
    @param fired             去重集合（调用方跨拍持有，原地更新）
    @param periodTimes       各周期K线时间数组 { res: [times] }（二分用，可选）
    @param periodMacdTimes   各周期MACD时间数组 { res: [times] }（切片用，可选；
                             缺省时 realtimeLowerDiverge 内部现建）
    @param divergeConfirm    M4 背驰进场时机（None → 读 CHAN_CFG.divergeConfirm）：
                             True=极值K线右邻K收盘后才出信号（分型确认后下一根开盘进场）
    @param trend_res         顺势参考周期（None→TREND_RES 默认 "240"；""=关闭）。
                             参考周期及以上不作检测周期；方向与信号相反时跳过信号
                             （规则见 trading_plan.trend_direction）。
    @param trend_state       参考周期方向状态（trend_state_of 结果）。本函数无 bars
                             入参、无法自行计算，须由调用方传入（回测引擎在链路重算拍
                             算好复用；None = 不过滤，仅保留检测周期剔除）。
    @returns 新信号列表（flat），每项含 { periodX, markRes, time, price, direction,
             strategyKey, nearSr, planDirection, realtime:True, segStart,
             fallback?, nearEqual?, expectBi, trendDirection?, trendReason? }
    """
    fired = fired if fired is not None else set()
    # 出场参数（参数中心可调；None → 模块常量默认）
    min_bars = realtimeMinBars if realtimeMinBars is not None else REALTIME_MIN_BARS
    zs_ratio = zsExitWeakRatio if zsExitWeakRatio is not None else ZS_EXIT_WEAK_RATIO
    if tCut is None:
        tCut = max((ts[-1] for ts in (periodTimes or {}).values() if ts), default=0)
    # 组装 periodData（bis/macdArr/atr/macdTimes）
    periodData = {}
    for res, bis in (periodBis or {}).items():
        if not bis:
            continue
        periodData[res] = {"bis": bis,
                           "macdArr": (periodMacd or {}).get(res) or [],
                           "macdTimes": (periodMacdTimes or {}).get(res),
                           "atr": (periodAtr or {}).get(res) or 0,
                           "bars": []}
    if not periodData:
        return []

    def upperResOf(res):
        sec = intervalSecOf(res) or 0
        best = None
        for r in periodData:
            s = intervalSecOf(r) or 0
            if s > sec and (best is None or s < intervalSecOf(best)):
                best = r
        return best

    out = []
    # 顺势参考周期（同 compute_entries；trend_state 须由调用方传入，见 docstring）
    trend_res = TREND_RES if trend_res is None else trend_res
    trend_sec = (intervalSecOf(trend_res) or 0) if trend_res else 0
    trend_dir = (trend_state or {}).get("dir") if trend_res else None
    trend_reason = (trend_state or {}).get("reason", "") if trend_res else ""
    for X in (detectPeriods or []):
        pd = periodData.get(X)
        if pd is None:
            continue
        if trend_sec and (intervalSecOf(X) or 0) >= trend_sec:
            continue  # 顺势参考周期及以上不作检测周期（只作方向锚，结构性剔除）
        plan = (planPeriods or {}).get(X)
        planStrategy = plan.get("strategy") if plan else None
        if not planStrategy or plan.get("direction") == "观望":
            continue
        strategy = entryStrategyOf(planStrategy)
        if strategy is None:
            continue
        if trend_dir and trend_dir != strategy["direction"]:
            continue  # 逆参考周期方向的信号跳过（顺势过滤）
        key = strategy["key"]
        direction = strategy["direction"]
        wantType = "up" if direction == "short" else "down"  # 空头等反弹(up)，多头等回调(down)
        bis = pd["bis"]
        if not bis:
            continue
        times = (periodTimes or {}).get(X) or [b["time"] for b in (pd.get("bars") or [])]
        # ① 够笔（当下制）：最后一笔=形成中段，方向匹配 + 段长门槛；
        #    预期够笔（expectBiEnabled，页面可选默认开）：末笔方向不符（回调/反弹的反向段
        #    尚未确认为笔）但末笔端点后已走出 ≥ expectBiMinBars 根本级K线 → 视为回调/反弹中，
        #    段起点改用末笔端点（分型确认固有 1 根K线滞后，不等右邻收盘）；
        #    False = 旧口径：末笔必须已是确认的反向笔。
        expectBi = bis[-1]["type"] != wantType
        if expectBi:
            if not counterMoveQualifies(times, bis[-1], tCut, enabled=expectBiEnabled):
                continue
            segStart = bis[-1]["endTime"]
        else:
            segStart = bis[-1]["startTime"]
        if _barsSince(times, segStart, tCut) < min_bars:
            continue
        # 该 (periodX, strategyKey, segStart) 已在任一 markRes 发过 → 跳过重算背驰链
        # （形成段延伸不重发；多 markRes 同段在既有口径下也只会命中一次有效进场路径）
        if any(f[0] == X and f[1] == key and f[3] == segStart for f in fired):
            continue
        # 策略专属条件（与确认制共用）
        upRes = upperResOf(X)
        upperBis = periodData[upRes]["bis"] if (upRes and upRes in periodData) else None
        extraReason = strategyExtraOk(key, bis, upperBis, pd["macdArr"], intervalSecOf(X),
                                      zs_ratio)
        if extraReason is not None:
            continue
        # ② 当下背驰 + ③ 支阻位附近（候选级别从大到小，命中即出）
        # 注意循环变量用 hit：near 是本函数参数（绝对价差），曾用 near 接收返回 dict
        # 导致后续检测周期 nearTol 变 dict（float<=dict 崩溃，2026-09-13 修复）
        nearTol = near  # 绝对价差（不乘 ATR，恒 > 0）
        for c in realtimeLowerDiverge(periodData, X, direction, tCut,
                                      periodTimes=periodTimes or {},
                                      divergeConfirm=divergeConfirm,
                                      expectBiEnabled=expectBiEnabled):
            fkey = (X, key, c["res"], c["segStart"])
            if fkey in fired:
                continue  # 该形成段已发过，段延伸不重发
            hit = nearSr(c["point"]["price"], srLevels, nearTol)
            if hit is None:
                continue
            fired.add(fkey)
            sig = {
                "periodX": X,
                "markRes": c["res"],
                "time": c["point"]["time"],
                "price": c["point"]["price"],
                "direction": direction,
                "strategyKey": key,
                "nearSr": hit["sr"]["price"],
                "planDirection": plan.get("direction"),
                "realtime": True,
                "segStart": c["segStart"],
                "fallback": bool(c.get("fallback", False)),   # M1 回退候选（非停止级命中）
                "nearEqual": bool(c.get("nearEqual", False)), # M2 近等候选（未严格创新极值）
                "expectBi": expectBi,                         # M4 检测周期走了预期够笔口径
            }
            if trend_dir:
                sig["trendDirection"] = trend_dir
                sig["trendReason"] = trend_reason
            out.append(sig)
    return out


# ============================================================
# 汇总计算（供回测链路调用）
# ============================================================

# 全部参与判定的周期（与 JS 一致）：检测周期 + 日线（仅作为 240 的上一级别笔）。
# with_30s=True 时追加 30S（3 分钟状态可用 30S 背驰产生进场信号，箭头画在 30S 级别）
ALL_RES = ["D", "240", "60", "15", "3"]
ALL_RES_WITH_30S = ["D", "240", "60", "15", "3", "30S"]


def compute_entries(periodBis, barsByPeriod, planPeriods, srLevels, detectPeriods,
                    near=NEAR, periodMacd=None, periodAtr=None, with_30s=False,
                    zs_exit_weak_ratio=ZS_EXIT_WEAK_RATIO,
                    trend_res=None, trend_state=None):
    """逐周期判定进场状态（依赖交易计划 plan 结果）→ 生成进场信号。

    @param periodBis     各周期笔 { 周期: [bis] }
    @param barsByPeriod  各周期原始K线 { 周期: [bars] }
    @param planPeriods   交易计划结果 { 周期: {direction, strategy, ...} }
    @param srLevels      支阻位列表（srflip.merged 全量候选池，每项含 price）
    @param detectPeriods 检测周期列表（从大到小，默认 240,60,15,3）
    @param near          靠近支阻位阈值（绝对价差，不乘 ATR）
    @param periodMacd    可选：各周期预计算 MACD { 周期: [macdArr] }
    @param periodAtr     可选：各周期预计算 ATR { 周期: atr }
    @param with_30s      启用 30 秒级别（ALL_RES 追加 30S，仍按数据存在性过滤）
    @param trend_res     顺势参考周期（None→TREND_RES 默认 "240"；""=关闭）。
                         参考周期及以上不作检测周期（只作方向锚，结构性剔除）；
                         参考周期方向与信号方向相反时跳过（规则见
                         trading_plan.trend_direction）。
    @param trend_state   调用方预计算的参考周期方向状态（trend_state_of 结果），
                         缺省时本函数内部计算（回测引擎在链路重算拍复用传入）。
    @returns { 标记级别: [信号...] }，信号含 { periodX, time, price, direction, strategyKey,
             nearSr, color, markRes, planDirection, trendDirection, trendReason }
    """
    periodMacd = periodMacd or {}
    periodAtr = periodAtr or {}
    all_res = ALL_RES_WITH_30S if with_30s else ALL_RES
    periodData = {}
    for res in all_res:
        bis = periodBis.get(res, []) or []
        if not bis:
            continue
        bars = barsByPeriod.get(res, []) or []
        if not bars:
            continue
        atr = periodAtr.get(res)
        if atr is None:
            atr = calcATR(bars, 14)
        macdArr = periodMacd.get(res)
        if macdArr is None:
            macdArr = calcMACD(bars)
        periodData[res] = {
            "bis": bis,
            "bars": bars,
            "atr": atr,
            "macdArr": macdArr,
        }

    # 上一级别周期映射：240→D、60→240、15→60、3→15（取有数据的最小更大级别）
    def upperResOf(res):
        sec = intervalSecOf(res) or 0
        best = None
        for r in periodData:
            s = intervalSecOf(r) or 0
            if s > sec and (best is None or s < intervalSecOf(best)):
                best = r
        return best

    # 顺势参考周期方向（trading_plan.trend_state_of；trend_state 可由调用方在链路
    # 重算拍预计算传入复用）。trend_res 为空 = 关闭：参考周期照常检测、方向不过滤。
    trend_res = TREND_RES if trend_res is None else trend_res
    trend_sec = (intervalSecOf(trend_res) or 0) if trend_res else 0
    if trend_state is None and trend_res:
        trend_state = trend_state_of(periodBis, barsByPeriod, trend_res,
                                     periodMacd=periodMacd)
    trend_dir = (trend_state or {}).get("dir") if trend_res else None
    trend_reason = (trend_state or {}).get("reason", "") if trend_res else ""

    allEntries = {}
    for res in detectPeriods:
        pd = periodData.get(res)
        if pd is None:
            continue
        if trend_sec and (intervalSecOf(res) or 0) >= trend_sec:
            continue  # 顺势参考周期及以上不作检测周期（只作方向锚，结构性剔除）
        plan = planPeriods.get(res)
        planStrategy = plan.get("strategy") if plan else None
        if not planStrategy or plan.get("direction") == "观望":
            continue
        strategy = entryStrategyOf(planStrategy)
        if strategy is None:
            continue
        if trend_dir and trend_dir != strategy["direction"]:
            continue  # 逆参考周期方向的信号跳过（顺势过滤）
        upRes = upperResOf(res)
        ctx = {
            "res": res,
            "bis": pd["bis"],
            "upperBis": periodData[upRes]["bis"] if (upRes and upRes in periodData) else None,
            "macdArr": pd["macdArr"],
            "atr": pd["atr"],
            "barSec": intervalSecOf(res),
            "near": near,
            "srLevels": srLevels,
            "periodData": periodData,
            "zs_exit_weak_ratio": zs_exit_weak_ratio,
        }
        evalRes = evaluateEntry(ctx, strategy)
        if not evalRes["ok"]:
            continue
        # 命中：在背驰级别标记箭头
        sig = {
            "periodX": res,
            "time": evalRes["point"]["time"],
            "price": evalRes["point"]["price"],
            "direction": strategy["direction"],
            "strategyKey": strategy["key"],
            "nearSr": evalRes["nearSr"],
            "color": BUY_COLOR if strategy["direction"] == "long" else SELL_COLOR,
            "markRes": evalRes["markRes"],
            "planDirection": plan.get("direction"),
        }
        if trend_dir:
            sig["trendDirection"] = trend_dir
            sig["trendReason"] = trend_reason
        allEntries.setdefault(evalRes["markRes"], []).append(sig)
    return allEntries


# ============================================================
# 出场规则（与 mark_entry.js 对齐：stopRefOf / findBiEvent / formingSegReady）
# ============================================================


def stop_ref_of(direction, entry_price, near_sr, sr_levels,
                slip_stop=DEFAULT_SLIP_STOP, slip_fallback=DEFAULT_SLIP_FALLBACK):
    """方向感知的止损参考位（含滑点偏移与兜底，返回值永不为 None）：
    short 取进场价上方最近支阻位（阻力）+ slip_stop、long 取下方最近（支撑）− slip_stop。

    near_sr（进场校验按绝对价差最近命中的支阻位价，不分上下方）已在正确侧直接沿用；
    否则从 sr_levels 重选正确侧最近位（进场判定逻辑不变，仅供出场止损参考）。
    无正确侧位 → 兜底止损 = 进场价 ± slip_fallback（不再有「不设止损」情形）。
    @param direction     "long" | "short"
    @param entry_price   进场价
    @param near_sr       信号自带的近支阻位价格（可为 None）
    @param sr_levels     支阻位列表（dict 含 "price"，或直接为价格数值）
    @param slip_stop     支阻位滑点（绝对价格，short + / long −）
    @param slip_fallback 兜底止损滑点（绝对价格，short + / long −）
    """
    is_short = direction == "short"
    slip = slip_stop if is_short else -slip_stop
    if near_sr is not None and (near_sr > entry_price if is_short else near_sr < entry_price):
        return near_sr + slip
    best = None
    for sr in (sr_levels or []):
        p = sr.get("price") if isinstance(sr, dict) else sr
        if p is None:
            continue
        if (p > entry_price) if is_short else (p < entry_price):
            d = abs(p - entry_price)
            if best is None or d < best[1]:
                best = (p, d)
    if best is None:
        return entry_price + (slip_fallback if is_short else -slip_fallback)
    return best[0] + slip


def trend_following_of(plan_direction, strategy_key=None):
    """顺势/逆势判定（TP2 平一半 / TP3 分支门槛）：
    交易计划 direction ∈ {多头多, 空头空} 为顺势（计划结构方向=操作方向）；
    {多头空, 空头多} 为逆势。plan_direction 缺失时按 strategyKey 兜底
    （wait2Buy/waitBuy/wait2Sell/waitSell → 顺势；wait1Buy/wait1Sell → 逆势）。
    """
    if plan_direction:
        return plan_direction in TREND_PLAN_DIRS
    return strategy_key in TREND_STRATEGY_KEYS


def forming_seg_ready(px_bis, px_merged_times, is_short, min_merged=EXIT_MIN_MERGED):
    """检测周期形成段「成笔预期」判定（TP2 / 逆势 TP3b 事件源，当下状态无前视）：
    末笔为不利方向（short→up / long→down）且其后正在走的有利方向形成段，
    自末笔延伸终点所在合并块起，其后合并K线块数 ≥ min_merged−1（与 isValid
    「两分型间隔 gap>=4」同口径，含锚点块共 min_merged 块）。

    锚点按末笔 endTime 在 px_merged_times（合并块截止时间数组，升序）中二分定位——
    不能用 bis[-1]["endIdx"]：extendLastBiFrom 延伸时不更新 endIdx。
    末笔延伸（创新不利极值）时 endTime 推进、锚点右移、计数自动归零。
    @param px_bis          检测周期笔列表（末笔可为延伸中的形成笔）
    @param px_merged_times 检测周期合并K线块截止时间数组（升序，与引擎 _merged_times 同构）
    @param is_short        持仓方向是否空头
    @param min_merged      成笔预期门槛（默认 EXIT_MIN_MERGED=5）
    """
    if not px_bis or not px_merged_times:
        return False
    last = px_bis[-1]
    if last["type"] != ("up" if is_short else "down"):
        return False  # 末笔为有利方向 → 其后形成段为不利方向，不触发
    anchor = bisect.bisect_left(px_merged_times, last["endTime"])
    if anchor >= len(px_merged_times):
        return False
    return (len(px_merged_times) - 1) - anchor >= min_merged - 1


def find_bi_event(bis, from_t, bi_type, require_post_start=False, break_prev=False):
    """找 from_t 之后首个完成的指定 type 笔（够笔/破高低点事件源）。

    @param bis               笔列表（按时间升序）
    @param from_t            起始时间（秒，不含等于）
    @param bi_type           "up" | "down"
    @param require_post_start True 时要求 startTime >= from_t（TP3 用：必须是进场后
                             开始的新反向笔，排除进场前已存在的同向笔——进场背驰点
                             本身常是「创新高/新低」笔）
    @param break_prev        True 时再要求端点破前一同向笔端点（up 过前高 / down 破前底）
    @returns None | {time, price, startTime}（笔完成时间 endTime、端点价、起点时间）
    """
    if not bis:
        return None
    # endTime 升序 → 二分跳过 from_t 之前的部分，只从首个 endTime > from_t 起向后找
    for i in range(_bisect_gt_key(bis, "endTime", from_t), len(bis)):
        b = bis[i]
        if b["type"] != bi_type:
            continue
        if require_post_start and b["startTime"] < from_t:
            continue
        if break_prev:
            j = i - 1
            while j >= 0 and bis[j]["type"] != bi_type:
                j -= 1
            if j < 0:
                continue
            broke = (b["endPrice"] > bis[j]["endPrice"]) if bi_type == "up" \
                else (b["endPrice"] < bis[j]["endPrice"])
            if not broke:
                continue
        return {"time": b["endTime"], "price": b["endPrice"], "startTime": b["startTime"]}
    return None
