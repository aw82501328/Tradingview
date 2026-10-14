# -*- coding: utf-8 -*-
"""
买卖点标记逻辑（Python 移植版，与 .cursor/skills/mark-buy-sell/scripts/mark_buy_sell.js 对齐）

纯函数模块：基于各周期笔（chan-bi 画笔产出）、上级笔（区间套锚定）与 MACD，
计算每周期 1/2/3 类买卖点标记，含：
  - 一类点落点（findBuyPoints/findSellPoints 内 project_first_points：上级笔
    内部的小级别起点/终点顶点 + snapToOwnBar 吸附到本周期 bar 边界，2026-10-13
    起不再二次锚定回上级端点）
  - 邻近合并（mergeNearFirstSecond：1买与2买价格很近 → 合并为「真1买」，1卖/2卖对称）
  - 保留截断（每周期不分类，买+卖合并在区间套合并后的完整列表上只保留时间最近 keep 个；
    2026-10-14 规则v2 起脏区间 t0 之前沿用旧标记、之后取新算）
  - 跨周期共振（本级别 1买/1卖 与紧邻上级 1/2/3 类买卖点同点位 → 标绿）

不连接 CDP、不绘图；回测链路只调用纯函数，绘图由 tv_draw 统一完成。
"""

import re

from .chan_core import (
    calcATR, calcMACD, intervalSecOf, fmtT,
    findBuyPoints, findSellPoints,
    snapToOwnBar,
)

# 买卖点标记颜色（与 JS 一致）
RED = "#F23645"
GREEN = "#089981"

# 邻近合并阈值（×ATR）：1买/1卖 与 2买/2卖 的「K线最低/最高价差」不超过该值视为「很近」
NEAR_ATR_RATIO = 0.3
# 每周期保留的标记总数（不分类：买+卖合并后只保留时间上最近 keep 个，减少图上标记数量）
KEEP = 10
# 类2破中枢 / 3类进中枢绝对点位容差（参数中心 points 模块默认）
CLASS2_ZS_TOL = 0.0
THIRD_ZS_TOL = 0.0

# 跨周期共振匹配的正则：上级 1/2/3 类与类2/类3 买卖点
_CLASS_RE = re.compile(r"^([1234]买|[1234]卖|类[234]买|类[234]卖)$")


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


def _family_label(p):
    """二买族强弱标签（2026-10-11 统一二买定义）：点 type 仍为 2买/类2买/2卖/类2卖
    （下游类型表/过滤免改），图表标签显示 强二买/弱二买/强类2买…；strong=None
    （首笔上涨未走完）按弱先行显示。无 strong 字段的点（旧数据/1、3类）原样返回。"""
    if "strong" not in p:
        return p["type"]
    tag = "强" if p.get("strong") else "弱"
    return tag + {"2买": "二买", "类2买": "类2买", "2卖": "二卖", "类2卖": "类2卖"}.get(p["type"], p["type"])


def compute_period_marks(res, bis, upperBis, rawBars, atr, macdArr, pi, periodMarks, periods,
                         nearAtrRatio=NEAR_ATR_RATIO, keep=KEEP,
                         class2ZsTol=CLASS2_ZS_TOL, thirdZsTol=THIRD_ZS_TOL, upperMacdArr=None):
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
    @param upperMacdArr 可选：上一级别 MACD（弱2类买卖点大周期背驰闸；缺省区间套兜底）
    @returns marks 列表，每项 { label, time, price, rawTime, rawPrice, color }
    """
    barSec = intervalSecOf(res)

    buyPts = findBuyPoints(bis, upperBis, macdArr, barSec, class2ZsTol, thirdZsTol,
                           upperMacd=upperMacdArr)
    sellPts = findSellPoints(bis, upperBis, macdArr, barSec, class2ZsTol, thirdZsTol,
                             upperMacd=upperMacdArr)

    # 2026-10-13：一类点已在 findBuyPoints 内落到「上级笔内部的小级别起点/终点」
    # 顶点（project_first_points），不再做 anchorFirstBuy 二次锚定——那会把点拉回
    # 上级端点/上级K开盘时刻。展示时间统一由下方 snapToOwnBar 吸附到本周期 bar。
    anchoredBuyPts = buyPts

    # 1类 与 2类 价格很接近 → 合并标注「真1买/真1卖」
    nearPrice = max(atr * nearAtrRatio, 0.05)
    anchoredBuyPts = mergeNearFirstSecond(anchoredBuyPts, "1买", "2买", "真1买", nearPrice)
    sellPts = mergeNearFirstSecond(sellPts, "1卖", "2卖", "真1卖", nearPrice)

    # （2026-10-14 规则v2）保留截断（keep）移至 compute_all_marks：区间套笔区间粒度合并
    # （脏区间 t0 之前沿用旧标记）之后再截，避免跨边界截断丢点。本函数输出全部新鲜标记。

    # 汇总标记：时间吸附到本周期 bar 边界；rawTime/rawPrice 保存原始点位（共振判定用）
    marks = []
    offset = max(atr * 0.5, 0.05)
    for p in anchoredBuyPts:
        t = snapToOwnBar(p["price"], p["time"], rawBars)
        # 真1买 与 2买 同点位，文字偏移双倍（更靠下）避免重叠
        mul = 2 if p["type"] == "真1买" else 1
        marks.append({"label": _family_label(p), "time": t, "price": p["price"] - offset * mul,
                      "rawTime": p["time"], "rawPrice": p["price"]})
    for p in sellPts:
        t = snapToOwnBar(p["price"], p["time"], rawBars)
        # 真1卖 与 2卖 同点位，文字偏移双倍（更靠上）避免重叠
        mul = 2 if p["type"] == "真1卖" else 1
        marks.append({"label": _family_label(p), "time": t, "price": p["price"] + offset * mul,
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
                      periodMacd=None, periodAtr=None,
                      class2ZsTol=CLASS2_ZS_TOL, thirdZsTol=THIRD_ZS_TOL,
                      changed_spans=None, prev_marks=None):
    """按周期从大到小计算全部买卖点标记。

    @param bisByPeriod   各周期笔 { 周期: [bis] }
    @param barsByPeriod  各周期原始K线 { 周期: [bars] }
    @param periods       周期列表（从大到小）
    @param fromTs        起始日期时间戳（过滤该时间之后的笔；None 不过滤）
    @param periodMacd    可选：各周期预计算 MACD { 周期: [macdArr] }
    @param periodAtr     可选：各周期预计算 ATR { 周期: atr }
    @param changed_spans 可选：各周期笔变化时间段。None=全量重算；有值时从第一个变化
                         周期起，该周期及更小周期重算——脏区间 t0 之前沿用 prev_marks、
                         之后取新算结果（2026-10-14 规则v2 笔区间粒度），更大周期整体沿用
    @param prev_marks    上次各周期标记，供未变化的大级别/脏区间前沿沿用
    @returns { 周期: [marks] }，marks 含 { label, time, price, rawTime, rawPrice, color }
    """
    from .chan_core import structurePeriods, nested_dirty_periods
    bisByPeriod = structurePeriods(bisByPeriod, barsByPeriod)
    periodMacd = periodMacd or {}
    periodAtr = periodAtr or {}
    prev_marks = prev_marks or {}
    dirty = nested_dirty_periods(periods, changed_spans)
    periodMarks = {}
    for pi, res in enumerate(periods):
        # 更大级别笔没变：沿用上次标记，供下级共振上色；本级不重算
        if changed_spans is not None and res not in dirty:
            kept = prev_marks.get(res)
            if kept:
                periodMarks[res] = kept
            continue
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
        upperBars = barsByPeriod.get(periods[pi - 1], []) if pi > 0 else []
        upperMacdArr = (periodMacd.get(periods[pi - 1]) if pi > 0 else None)
        if upperMacdArr is None and pi > 0 and upperBars:
            upperMacdArr = calcMACD(upperBars)
        atr = periodAtr.get(res)
        if atr is None:
            atr = calcATR(rawBars, 14)
        macdArr = periodMacd.get(res)
        if macdArr is None:
            macdArr = calcMACD(rawBars)
        marks = compute_period_marks(res, curBis, upperBis, rawBars, atr, macdArr,
                                     pi, periodMarks, periods, nearAtrRatio, keep,
                                     class2ZsTol, thirdZsTol, upperMacdArr=upperMacdArr)
        # 区间套动态更新（2026-10-14 规则v2·笔区间粒度）：大级别笔变化经 nested_dirty_periods
        # 级联到全部更小周期（span=并集）——脏区间 t0 之前的旧标记沿用上次结果、之后取本次
        # 新算；保留截断（keep）在合并后的完整列表上做。
        span = dirty.get(res)
        prev_list = prev_marks.get(res) or []
        if span is not None and span["t0"] != float("-inf") and prev_list:
            keep_before = [m for m in prev_list if m.get("rawTime", m["time"]) < span["t0"]]
            fresh = [m for m in marks if m.get("rawTime", m["time"]) >= span["t0"]]
            merged = keep_before + fresh
        else:
            merged = marks
        merged.sort(key=lambda m: m["time"], reverse=True)
        merged = merged[:keep]
        merged.sort(key=lambda m: m["time"])
        if merged:
            periodMarks[res] = merged
    return periodMarks


# ============================================================
# 工具：时间格式化（复用 chan_core.fmtT，供调用方打印）
# ============================================================


def fmt(t):
    """Unix 秒 → "M-d HH:MM"（本地时区），与 JS 的 toT 一致。"""
    return fmtT(t)
