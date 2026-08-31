# -*- coding: utf-8 -*-
"""
进出场逻辑（Python 移植版，与 .cursor/skills/mark-entry/scripts/mark_entry.js 对齐）

纯函数模块：依赖「交易计划」（trading_plan）结果判定各周期当前进场状态，
映射到 6 种进场策略，校验该策略的进场条件后生成进场信号：
  - 买点（多头）= 红色向上箭头（arrow_up）
  - 卖点（空头）= 向下绿色箭头（arrow_down）

信号画在「背驰级别」（更低周期）。暂未实现出场策略，故无出场信号。

不连接 CDP、不绘图；回测链路通过 compute_entries 直接调用。
"""

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
    """以下级别出现背驰：在所有更低周期（intervalSecOf 更小）中找方向匹配的最新背驰点。
    @returns None 或 { res, point:{time,price,direction} }
    """
    xSec = intervalSecOf(X) or float("inf")
    best = None
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
            if best is None or p["time"] > best["point"]["time"]:
                best = {"res": res, "point": p}
    return best


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

    # 2. 各策略专属条件
    if key == "wait2Sell":
        if not brokePrevLow(bis):
            return {"ok": False, "reason": "下跌段未破前底"}
        if not macdBelowZero(macdArr):
            return {"ok": False, "reason": "MACD 未下0轴或反弹过0轴"}
    elif key == "wait2Buy":
        if not brokePrevHigh(bis):
            return {"ok": False, "reason": "上涨段未过前高"}
        if not macdAboveZero(macdArr):
            return {"ok": False, "reason": "MACD 未上0轴或回调破0轴"}
    elif key == "wait1Sell":
        if not brokePrevHigh(bis):
            return {"ok": False, "reason": "未够笔且过高点"}
        if not zsExitWeak(bis, upperBis, macdArr, barSec, 1.0, "short"):
            return {"ok": False, "reason": "出中枢力度未变弱"}
    elif key == "wait1Buy":
        if not brokePrevLow(bis):
            return {"ok": False, "reason": "未够笔且过低点"}
        if not zsExitWeak(bis, upperBis, macdArr, barSec, 1.0, "long"):
            return {"ok": False, "reason": "出中枢力度未变弱"}
    # waitBuy / waitSell：仅需够笔 + 以下级别背驰 + 支阻位附近

    # 3. 以下级别出现背驰（定位背驰级别与背驰点，箭头画在此级别）
    ld = lowerDiverge(periodData, res, divergeDir)
    if ld is None:
        return {"ok": False, "reason": "以下级别无匹配方向背驰"}

    # 4. 在支阻位附近（用背驰点价 vs 检测周期 ATR）
    nearTol = nearAtr * atr
    near = nearSr(ld["point"]["price"], srLevels, nearTol)
    if near is None:
        return {"ok": False, "reason": "背驰点远离支阻位"}

    return {"ok": True, "markRes": ld["res"], "point": ld["point"], "nearSr": near["sr"]["price"]}


# ============================================================
# 汇总计算（供回测链路调用）
# ============================================================

# 全部参与判定的周期（与 JS 一致）：检测周期 + 日线（仅作为 240 的上一级别笔）
ALL_RES = ["D", "240", "60", "15", "3"]


def compute_entries(periodBis, barsByPeriod, planPeriods, srLevels, detectPeriods,
                    nearAtr=NEAR_ATR, periodMacd=None, periodAtr=None):
    """逐周期判定进场状态（依赖交易计划 plan 结果）→ 生成进场信号。

    @param periodBis     各周期笔 { 周期: [bis] }
    @param barsByPeriod  各周期原始K线 { 周期: [bars] }
    @param planPeriods   交易计划结果 { 周期: {direction, strategy, ...} }
    @param srLevels      支阻位列表（srflip.merged，每项含 price）
    @param detectPeriods 检测周期列表（从大到小，默认 240,60,15,3）
    @param nearAtr       靠近支阻位阈值（×检测周期ATR）
    @param periodMacd    可选：各周期预计算 MACD { 周期: [macdArr] }
    @param periodAtr     可选：各周期预计算 ATR { 周期: atr }
    @returns { 标记级别: [信号...] }，信号含 { periodX, time, price, direction, strategyKey,
             nearSr, color, markRes }
    """
    periodMacd = periodMacd or {}
    periodAtr = periodAtr or {}
    periodData = {}
    for res in ALL_RES:
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
