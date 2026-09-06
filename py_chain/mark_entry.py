# -*- coding: utf-8 -*-
"""
进出场逻辑（Python 移植版，与 .cursor/skills/mark-entry/scripts/mark_entry.js 对齐）

纯函数模块：依赖「交易计划」（trading_plan）结果判定各周期当前进场状态，
映射到 6 种进场策略，校验该策略的进场条件后生成进场信号：
  - 买点（多头）= 红色向上箭头（arrow_up）
  - 卖点（空头）= 向下绿色箭头（arrow_down）

信号画在「背驰级别」（更低周期）。出场规则（止损 + 三档止盈）的状态机由
backtest.BacktestEngine 增量推进；本模块提供方向感知止损参考位（stop_ref_of）
与笔事件查找（find_bi_event），与 mark_entry.js 的 stopRefOf/findBiEvent 对齐。

不连接 CDP、不绘图；回测链路通过 compute_entries 直接调用。
"""

import bisect

from .chan_core import (
    calcATR, calcMACD, isBiDiverge, lowerResOf, buildZSByUpper, intervalSecOf,
)

# 箭头颜色：买点（多头）红色、卖点（空头）绿色
BUY_COLOR = "#F23645"
SELL_COLOR = "#089981"

# 靠近支阻位阈值（×当前周期ATR）
NEAR_ATR = 1.0


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
            points.append({"time": cur["endTime"], "price": cur["endPrice"], "direction": "long"})

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
            points.append({"time": cur["endTime"], "price": cur["endPrice"], "direction": "short"})

    return points


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
    """MACD 当前在 0 轴之下（dif < 0）：下0轴后反弹不过0轴。"""
    if not macdArr or len(macdArr) == 0:
        return False
    return macdArr[-1]["dif"] < 0


def macdAboveZero(macdArr):
    """MACD 当前在 0 轴之上（dif > 0）：上0轴后回调不破0轴。"""
    if not macdArr or len(macdArr) == 0:
        return False
    return macdArr[-1]["dif"] > 0


def zsExitWeak(bis, upperBis, macdArr, barSec, ratio=1.0, wantDir="short"):
    """出中枢的力度变弱（buildZSByUpper 取最后一个中枢）：
    离开中枢的笔相对进入中枢的笔 isBiDiverge 为 true，或离开笔 span < 进入笔 span × ratio。"""
    zss = []
    try:
        zss = buildZSByUpper(bis, upperBis or [], barSec)
    except Exception:
        return False
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


def lowerDiverge(periodData, X, wantDir):
    """以下级别出现背驰：收集所有更低周期（intervalSecOf 更小）中方向匹配的背驰点，
    按时间**降序**返回候选列表（最新的在前）。
    调用方（evaluateEntry）依次尝试候选并做支阻位校验，失败回退次新——
    避免「--with-30s 后高频 30S 背驰点抢占原级别点、而 30S 微观点又远离支阻位
    导致信号彻底消失」的问题（如 2026-09-04 13:39 的 3分钟背驰级别信号）。
    @returns [ { res, point:{time,price,direction} }, ... ] 按 point.time 降序
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
    cands.sort(key=lambda c: c["point"]["time"], reverse=True)  # 最新在前
    return cands


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


def strategyExtraOk(key, bis, upperBis, macdArr, barSec):
    """各策略专属条件（原 evaluateEntry 第 2 步抽取为独立函数，确认制/当下制共用）。
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
        if not zsExitWeak(bis, upperBis, macdArr, barSec, 1.0, "short"):
            return "出中枢力度未变弱"
    elif key == "wait1Buy":
        if not brokePrevLow(bis):
            return "未够笔且过低点"
        if not zsExitWeak(bis, upperBis, macdArr, barSec, 1.0, "long"):
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
    nearAtr = ctx.get("nearAtr", NEAR_ATR)
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
    extraReason = strategyExtraOk(key, bis, upperBis, macdArr, barSec)
    if extraReason is not None:
        return {"ok": False, "reason": extraReason}

    # 3+4. 以下级别背驰候选（按时间降序）依次做支阻位校验，失败回退次新点：
    #      策略专属条件不依赖背驰点（第2步已过），只需重试「支阻位附近」。
    #      （--with-30s 后 30S 高频背驰点常抢占原级别点，且其微观极值价常远离支阻位——
    #       无回退时信号彻底消失：旧点被抢占丢弃、新点校验被拒，两边都不出箭头。）
    cands = lowerDiverge(periodData, res, divergeDir)
    if not cands:
        return {"ok": False, "reason": "以下级别无匹配方向背驰"}

    nearTol = nearAtr * atr  # 用背驰点价 vs 检测周期 ATR
    for c in cands:
        near = nearSr(c["point"]["price"], srLevels, nearTol)
        if near is not None:
            return {"ok": True, "markRes": c["res"], "point": c["point"], "nearSr": near["sr"]["price"]}
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


def realtimeLowerDiverge(periodData, X, wantDir, tCut,
                          periodTimes=None, minBars=REALTIME_MIN_BARS):
    """当下背驰：低级别「形成中段」实时对比参照笔（每根 fine 收盘调用）。

    形成中段 = 低级别笔列表最后一笔——回测引擎的增量状态已用 extendLastBiFrom
    把它延伸到最新极值（endTime/endPrice = 当下极值），天然就是"正在走的这段"。

    条件（与确认制 findDivergePoints 同一套背驰标准，只是对象换成形成中段）：
      - 段方向匹配（做多→形成中下跌段 / 做空→形成中上涨段）；
      - 段长 ≥ minBars（够笔门槛，见 REALTIME_MIN_BARS）；
      - 创新低/新高：段当前极值 < refer.endPrice（多）/ > refer.endPrice（空）；
      - isBiDiverge（当下对比）：绿柱面积变小 或 DIF低点抬高 或 绿柱最大高度变小（OR）。
    参照笔 = 向前最近同向**已完成**笔（跳过幅度 < 当前段 50% 的次级别回调，同确认制）。

    @param periodTimes   各周期K线时间数组（升序，二分用）；缺省时从 periodData[res].bars 现建
    @returns 候选列表 [ { res, point:{time,price,direction}, segStart } ]，
             级别从大到小排序（次级别优先于次次级别），每级别最多 1 个（形成中段）
    """
    xSec = intervalSecOf(X) or float("inf")
    wantType = "down" if wantDir == "long" else "up"
    cands = []
    lowers = []
    for res, pd in periodData.items():
        sec = intervalSecOf(res) or 0
        if sec < xSec and pd and pd.get("bis") and len(pd["bis"]) >= 3:
            lowers.append((sec, res, pd))
    lowers.sort(key=lambda x: -x[0])  # 次级别（更大的低级别）在前
    for _sec, res, pd in lowers:
        bis = pd["bis"]
        F = bis[-1]  # 形成中段（引擎增量状态已延伸到当前极值）
        if F["type"] != wantType:
            continue
        times = (periodTimes or {}).get(res)
        if not times:
            times = [b["time"] for b in (pd.get("bars") or [])]
        if _barsSince(times, F["startTime"], tCut) < minBars:
            continue  # 段太短（微回调/微反弹），不算够笔
        # 参照笔：向前最近同向已完成笔（不含形成中段），跳过幅度不足的次级别回调
        refer = None
        for j in range(len(bis) - 2, -1, -1):
            cand = bis[j]
            if cand["type"] != F["type"]:
                continue
            if cand["span"] < F["span"] * 0.5:
                continue
            refer = cand
            break
        if refer is None:
            continue
        madeNew = F["endPrice"] < refer["endPrice"] if wantDir == "long" \
            else F["endPrice"] > refer["endPrice"]
        if not madeNew:
            continue
        # 当下对比 MACD：窗口取 [refer.startTime, F.endTime] 的切片（避免全量数组线性扫）
        macdArr = pd.get("macdArr") or []
        macdT = pd.get("macdTimes") or [m["time"] for m in macdArr]
        if not macdT:
            continue
        lo = bisect.bisect_left(macdT, refer["startTime"])
        hi = bisect.bisect_right(macdT, F["endTime"])
        if not isBiDiverge(F, refer, macdArr[lo:hi]):
            continue
        cands.append({"res": res,
                      "point": {"time": F["endTime"], "price": F["endPrice"],
                                "direction": wantDir},
                      "segStart": F["startTime"]})
    return cands


def evaluateRealtimeEntries(periodBis, periodMacd, periodAtr, planPeriods, srLevels,
                            detectPeriods, nearAtr=NEAR_ATR, tCut=None, fired=None,
                            periodTimes=None, periodMacdTimes=None):
    """当下模式进场评估（每根 fine 收盘调用，信号无需等反向笔确认）。

    三条件与确认制同构，差异只在"何时评"与"②用什么评"：
      ① 够笔：检测周期最后一笔（引擎中=延伸中的形成段）方向匹配且段长 ≥ REALTIME_MIN_BARS；
      ② 当下背驰：realtimeLowerDiverge（形成中段创新低/新高 + 当拍 MACD 对比）；
      ③ 支阻位附近：形成中段当前极值价 vs srLevels（价差 ≤ nearAtr × 检测周期ATR）。
    策略专属条件与确认制共用（strategyExtraOk）。

    去重：fired 集合按 (periodX, strategyKey, markRes, 段起点时间)——每个形成段只发一次，
    段延伸（继续创新低）不重发；新段（新起点）重新评估。

    @param tCut              当前时刻（fine 收盘时间）；None 时取各周期数据末尾
    @param fired             去重集合（调用方跨拍持有，原地更新）
    @param periodTimes       各周期K线时间数组 { res: [times] }（二分用，可选）
    @param periodMacdTimes   各周期MACD时间数组 { res: [times] }（切片用，可选；
                             缺省时 realtimeLowerDiverge 内部现建）
    @returns 新信号列表（flat），每项含 { periodX, markRes, time, price, direction,
             strategyKey, nearSr, realtime:True, segStart }
    """
    fired = fired if fired is not None else set()
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
    for X in (detectPeriods or []):
        pd = periodData.get(X)
        if pd is None:
            continue
        plan = (planPeriods or {}).get(X)
        planStrategy = plan.get("strategy") if plan else None
        if not planStrategy or plan.get("direction") == "观望":
            continue
        strategy = entryStrategyOf(planStrategy)
        if strategy is None:
            continue
        key = strategy["key"]
        direction = strategy["direction"]
        wantType = "up" if direction == "short" else "down"  # 空头等反弹(up)，多头等回调(down)
        bis = pd["bis"]
        # ① 够笔（当下制）：最后一笔=形成中段，方向匹配 + 段长门槛
        if not bis or bis[-1]["type"] != wantType:
            continue
        times = (periodTimes or {}).get(X) or [b["time"] for b in (pd.get("bars") or [])]
        if _barsSince(times, bis[-1]["startTime"], tCut) < REALTIME_MIN_BARS:
            continue
        # 策略专属条件（与确认制共用）
        upRes = upperResOf(X)
        upperBis = periodData[upRes]["bis"] if (upRes and upRes in periodData) else None
        extraReason = strategyExtraOk(key, bis, upperBis, pd["macdArr"], intervalSecOf(X))
        if extraReason is not None:
            continue
        # ② 当下背驰 + ③ 支阻位附近（候选级别从大到小，命中即出）
        nearTol = nearAtr * (pd["atr"] or 0)
        if nearTol <= 0:
            continue
        for c in realtimeLowerDiverge(periodData, X, direction, tCut,
                                      periodTimes=periodTimes or {}):
            fkey = (X, key, c["res"], c["segStart"])
            if fkey in fired:
                continue  # 该形成段已发过，段延伸不重发
            near = nearSr(c["point"]["price"], srLevels, nearTol)
            if near is None:
                continue
            fired.add(fkey)
            out.append({
                "periodX": X,
                "markRes": c["res"],
                "time": c["point"]["time"],
                "price": c["point"]["price"],
                "direction": direction,
                "strategyKey": key,
                "nearSr": near["sr"]["price"],
                "realtime": True,
                "segStart": c["segStart"],
            })
    return out


# ============================================================
# 汇总计算（供回测链路调用）
# ============================================================

# 全部参与判定的周期（与 JS 一致）：检测周期 + 日线（仅作为 240 的上一级别笔）。
# with_30s=True 时追加 30S（3 分钟状态可用 30S 背驰产生进场信号，箭头画在 30S 级别）
ALL_RES = ["D", "240", "60", "15", "3"]
ALL_RES_WITH_30S = ["D", "240", "60", "15", "3", "30S"]


def compute_entries(periodBis, barsByPeriod, planPeriods, srLevels, detectPeriods,
                    nearAtr=NEAR_ATR, periodMacd=None, periodAtr=None, with_30s=False):
    """逐周期判定进场状态（依赖交易计划 plan 结果）→ 生成进场信号。

    @param periodBis     各周期笔 { 周期: [bis] }
    @param barsByPeriod  各周期原始K线 { 周期: [bars] }
    @param planPeriods   交易计划结果 { 周期: {direction, strategy, ...} }
    @param srLevels      支阻位列表（srflip.merged，每项含 price）
    @param detectPeriods 检测周期列表（从大到小，默认 240,60,15,3）
    @param nearAtr       靠近支阻位阈值（×检测周期ATR）
    @param periodMacd    可选：各周期预计算 MACD { 周期: [macdArr] }
    @param periodAtr     可选：各周期预计算 ATR { 周期: atr }
    @param with_30s      启用 30 秒级别（ALL_RES 追加 30S，仍按数据存在性过滤）
    @returns { 标记级别: [信号...] }，信号含 { periodX, time, price, direction, strategyKey,
             nearSr, color, markRes }
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

    allEntries = {}
    for res in detectPeriods:
        pd = periodData.get(res)
        if pd is None:
            continue
        plan = planPeriods.get(res)
        planStrategy = plan.get("strategy") if plan else None
        if not planStrategy or plan.get("direction") == "观望":
            continue
        strategy = entryStrategyOf(planStrategy)
        if strategy is None:
            continue
        upRes = upperResOf(res)
        ctx = {
            "res": res,
            "bis": pd["bis"],
            "upperBis": periodData[upRes]["bis"] if (upRes and upRes in periodData) else None,
            "macdArr": pd["macdArr"],
            "atr": pd["atr"],
            "barSec": intervalSecOf(res),
            "nearAtr": nearAtr,
            "srLevels": srLevels,
            "periodData": periodData,
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
        }
        allEntries.setdefault(evalRes["markRes"], []).append(sig)
    return allEntries


# ============================================================
# 出场规则（与 mark_entry.js 对齐：stopRefOf / findBiEvent）
# ============================================================


def stop_ref_of(direction, entry_price, near_sr, sr_levels):
    """方向感知的止损参考位：short 取进场价上方最近支阻位（阻力）、long 取下方最近（支撑）。

    near_sr（进场校验按绝对价差最近命中的支阻位价，不分上下方）已在正确侧直接沿用；
    否则从 sr_levels 重选正确侧最近位（进场判定逻辑不变，仅供出场止损参考）。
    无正确侧位 → None（该仓不设止损，仅三档止盈出场）。
    @param direction   "long" | "short"
    @param entry_price 进场价
    @param near_sr     信号自带的近支阻位价格（可为 None）
    @param sr_levels   支阻位列表（dict 含 "price"，或直接为价格数值）
    """
    is_short = direction == "short"
    if near_sr is not None and (near_sr > entry_price if is_short else near_sr < entry_price):
        return near_sr
    best = None
    for sr in (sr_levels or []):
        p = sr.get("price") if isinstance(sr, dict) else sr
        if p is None:
            continue
        if (p > entry_price) if is_short else (p < entry_price):
            d = abs(p - entry_price)
            if best is None or d < best[1]:
                best = (p, d)
    return best[0] if best else None


def find_bi_event(bis, from_t, bi_type, require_post_start=False, break_prev=False):
    """找 from_t 之后首个完成的指定 type 笔（够笔/破高低点事件源）。

    @param bis               笔列表（按时间升序）
    @param from_t            起始时间（秒，不含等于）
    @param bi_type           "up" | "down"
    @param require_post_start True 时要求 startTime >= from_t（TP3 用：必须是进场后
                             开始的新反向笔，排除进场前已存在的同向笔——进场背驰点
                             本身常是「创新高/新低」笔）
    @param break_prev        True 时再要求端点破前一同向笔端点（up 过前高 / down 破前底）
    @returns None | {time, price}（笔完成时间 endTime 与端点价）
    """
    if not bis:
        return None
    for i, b in enumerate(bis):
        if b["type"] != bi_type:
            continue
        if not (b["endTime"] > from_t):
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
        return {"time": b["endTime"], "price": b["endPrice"]}
    return None
