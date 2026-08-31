# -*- coding: utf-8 -*-
"""
买卖点标记逻辑（Python 移植版，与 .cursor/skills/mark-buy-sell/scripts/mark_buy_sell.js 对齐）

纯函数模块：基于各周期笔（chan-bi 画笔产出）、上级笔（区间套锚定）与 MACD，
计算每周期 1/2/3 类买卖点标记，含：
  - 一买锚定（anchorFirstBuy + snapToOwnBar 映射到本周期 bar 边界）
  - 邻近合并（mergeNearFirstSecond：1买与2买价格很近 → 合并为「真1买」，1卖/2卖对称）
  - 每类保留最近 keep 个（keepRecentEach）
  - 跨周期共振（本级别 1买/1卖 与紧邻上级 1/2/3 类买卖点同点位 → 标绿）

不连接 CDP、不绘图；回测链路只调用纯函数，绘图由 tv_draw 统一完成。
"""

import re

from .chan_core import (
    calcATR, calcMACD, intervalSecOf, fmtT,
    findBuyPoints, findSellPoints, anchorFirstBuy, anchorFirstSell,
    snapToOwnBar, keepRecentEach,
)

# 买卖点标记颜色（与 JS 一致）
RED = "#F23645"
GREEN = "#089981"

# 邻近合并阈值（×ATR）：1买/1卖 与 2买/2卖 的「K线最低/最高价差」不超过该值视为「很近」
NEAR_ATR_RATIO = 0.3
# 每类买卖点保留的个数（每类只保留时间上最近 keep 个，减少图上标记数量）
KEEP = 1

# 跨周期共振匹配的正则：上级 1/2/3 类与类2 买卖点
_CLASS_RE = re.compile(r"^([123]买|[123]卖|类2买|类2卖)$")


def mergeNearFirstSecond(points, firstType, secondType, mergedType, nearPrice):
    """1类 与 2类 买卖点价格很接近时，直接把 1类 标记合并到最近的 2类 标记上，
    标注为「真1买/真1卖」。与 JS 版 mergeNearFirstSecond 对齐。"""
    firsts = [p for p in points if p["type"] == firstType]
    if not firsts:
        return points
    seconds = [p for p in points if p["type"] == secondType]
    if not seconds:
        return points
    usedSecond = set()
    out = []
    for p in points:
        if p["type"] != firstType:
            out.append(p)
            continue
        # 找时间上在 1类 之后（确认点）、价格最接近、且尚未承接过的 2类 标记
        best = None
        bestDiff = float("inf")
        for s in seconds:
            if s["time"] < p["time"]:
                continue
            if s["time"] in usedSecond:
                continue
            d = abs(s["price"] - p["price"])
            if d < bestDiff:
                bestDiff = d
                best = s
        if best is not None and bestDiff <= nearPrice:
            usedSecond.add(best["time"])
            out.append({"type": mergedType, "time": best["time"], "price": best["price"]})
        else:
            out.append(p)  # 附近无价格接近的 2类 标记，保留原 1类 标记
    return out


def compute_period_marks(res, bis, upperBis, rawBars, atr, macdArr, pi, periodMarks, periods,
                         nearAtrRatio=NEAR_ATR_RATIO, keep=KEEP):
    """计算单个周期的买卖点标记列表。

    @param res          周期名（如 "60"）
    @param bis          本周期笔列表（已过滤/校准，与图上一致）
    @param upperBis     上一级别笔（一买锚定 / 区间套用）
    @param rawBars      本周期原始K线（ATR / 吸附用）
    @param atr          本周期 ATR
    @param macdArr      本周期 MACD 数组
    @param pi           本周期在 periods 列表中的下标（0 为最大周期，一买锚定跳过）
    @param periodMarks  已计算出的上级周期标记 { 周期: [marks] }（跨周期共振用）
    @param periods      周期列表（从大到小）
    @returns marks 列表，每项 { label, time, price, rawTime, rawPrice, color }
    """
    barSec = intervalSecOf(res)

    buyPts = findBuyPoints(bis, upperBis, macdArr, barSec)
    sellPts = findSellPoints(bis, upperBis, macdArr, barSec)

    # 一买锚定：除最大周期外，每个一买都锚定到上一级某笔的底部端点
    anchoredBuyPts = buyPts
    firstBuyPts = [p for p in buyPts if p["type"] == "1买"]
    if firstBuyPts and pi > 0 and upperBis:
        anchoredMarks = []
        seenMarkPos = set()
        for cand in firstBuyPts:
            anchored = anchorFirstBuy(cand, upperBis)
            if anchored is None:
                continue
            snapTime = snapToOwnBar(anchored["price"], anchored["time"], rawBars)
            # 多个候选锚定到同一上级底（同一位置）时只保留一个
            if snapTime in seenMarkPos:
                continue
            seenMarkPos.add(snapTime)
            anchoredMarks.append({"type": "1买", "time": snapTime, "price": anchored["price"]})
        anchoredBuyPts = [p for p in buyPts if p["type"] != "1买"] + anchoredMarks

    # 1类 与 2类 价格很接近 → 合并标注「真1买/真1卖」
    nearPrice = max(atr * nearAtrRatio, 0.05)
    anchoredBuyPts = mergeNearFirstSecond(anchoredBuyPts, "1买", "2买", "真1买", nearPrice)
    sellPts = mergeNearFirstSecond(sellPts, "1卖", "2卖", "真1卖", nearPrice)

    # 每类只保留时间上最近 keep 个
    anchoredBuyPts = keepRecentEach(anchoredBuyPts, keep)
    sellPts = keepRecentEach(sellPts, keep)

    # 汇总标记：时间吸附到本周期 bar 边界；rawTime/rawPrice 保存原始点位（共振判定用）
    marks = []
    offset = max(atr * 0.5, 0.05)
    for p in anchoredBuyPts:
        t = snapToOwnBar(p["price"], p["time"], rawBars)
        # 真1买 与 2买 同点位，文字偏移双倍（更靠下）避免重叠
        mul = 2 if p["type"] == "真1买" else 1
        marks.append({"label": p["type"], "time": t, "price": p["price"] - offset * mul,
                      "rawTime": p["time"], "rawPrice": p["price"]})
    for p in sellPts:
        t = snapToOwnBar(p["price"], p["time"], rawBars)
        # 真1卖 与 2卖 同点位，文字偏移双倍（更靠上）避免重叠
        mul = 2 if p["type"] == "真1卖" else 1
        marks.append({"label": p["type"], "time": t, "price": p["price"] + offset * mul,
                      "rawTime": p["time"], "rawPrice": p["price"]})

    # 跨周期共振：本级别 1买/1卖 与紧邻上级 1/2/3 类买卖点落在同一点位 → 标记绿色
    upperRes = periods[pi - 1] if pi > 0 else None
    upperMarks = periodMarks.get(upperRes) if upperRes else None
    for mk in marks:
        mk["color"] = RED
        if (mk["label"] in ("1买", "1卖")) and upperMarks:
            for um in upperMarks:
                if not _CLASS_RE.match(um["label"]):
                    continue
                if abs(mk["rawTime"] - um["rawTime"]) <= 60 and \
                   abs(mk["rawPrice"] - um["rawPrice"]) <= max(atr * 0.2, 0.05):
                    mk["color"] = GREEN
                    break
    return marks


def compute_all_marks(bisByPeriod, barsByPeriod, periods, fromTs=None,
                      nearAtrRatio=NEAR_ATR_RATIO, keep=KEEP,
                      periodMacd=None, periodAtr=None):
    """按周期从大到小计算全部买卖点标记。

    @param bisByPeriod   各周期笔 { 周期: [bis] }
    @param barsByPeriod  各周期原始K线 { 周期: [bars] }
    @param periods       周期列表（从大到小）
    @param fromTs        起始日期时间戳（过滤该时间之后的笔；None 不过滤）
    @param periodMacd    可选：各周期预计算 MACD { 周期: [macdArr] }
    @param periodAtr     可选：各周期预计算 ATR { 周期: atr }
    @returns { 周期: [marks] }，marks 含 { label, time, price, rawTime, rawPrice, color }
    """
    periodMacd = periodMacd or {}
    periodAtr = periodAtr or {}
    periodMarks = {}
    for pi, res in enumerate(periods):
        curBis = bisByPeriod.get(res, []) or []
        if not curBis:
            continue
        # 过滤到起始日期之后的笔（与画笔/买卖点标记一致）
        if fromTs is not None:
            curBis = [b for b in curBis if b["endTime"] >= fromTs]
        rawBars = barsByPeriod.get(res, []) or []
        if not rawBars:
            continue
        upperBis = bisByPeriod.get(periods[pi - 1], []) if pi > 0 else None
        atr = periodAtr.get(res)
        if atr is None:
            atr = calcATR(rawBars, 14)
        macdArr = periodMacd.get(res)
        if macdArr is None:
            macdArr = calcMACD(rawBars)
        marks = compute_period_marks(res, curBis, upperBis, rawBars, atr, macdArr,
                                     pi, periodMarks, periods, nearAtrRatio, keep)
        if marks:
            periodMarks[res] = marks
    return periodMarks


# ============================================================
# 工具：时间格式化（复用 chan_core.fmtT，供调用方打印）
# ============================================================


def fmt(t):
    """Unix 秒 → "M-d HH:MM"（本地时区），与 JS 的 toT 一致。"""
    return fmtT(t)
