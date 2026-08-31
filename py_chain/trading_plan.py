# -*- coding: utf-8 -*-
"""
交易计划逻辑（Python 移植版，与 .cursor/skills/trading-plan/scripts/trading_plan.js 对齐）

纯函数模块：区分各周期当前是「震荡」还是「趋势」，
趋势时依据「最近笔端点的买卖点类型」生成对应交易策略。

说明：震荡判定 isRangeBound 复制自 chan-status SKILL（.cursor/skills/chan-status/scripts/chan_status.js），
与 JS 版一致保持原逻辑不变。

不连接 CDP、不绘图；回测链路通过 compute_plan 直接调用。
"""

from .chan_core import (
    calcATR, calcMACD, intervalSecOf, fmtT,
    findBuyPoints, findSellPoints, buildZS, buildZSByUpper, isBiDiverge,
)

# ============================================================
# 纯函数：震荡判定（复制自 chan-status SKILL，保持原逻辑不变）
# ============================================================


def isRangeBound(bis, bars, atr, cfg=None):
    """震荡（横盘整理）判定：K线重叠度高、价格变化不大、无明确方向。
    三条件同时满足才判定为震荡：
      1. K线区间小：最近 rangeBarN 根K线的 maxHigh - minLow <= rangeKMult × ATR
      2. 笔端点区间小：最近 rangeBiN 笔的端点极差（max-min）<= rangeBiMult × ATR
      3. 方向性弱：最近 rangeBiN 笔中涨跌交替（同时存在 up 与 down，且无明显单边）
    突破跳过：最后一笔终点相对窗口区间的另一端明显偏移（> rangeBreakMult × ATR）
    视为突破盘整，跳过震荡判定（返回 range:false, breakOut:true）。
    @returns 判定结果 dict 或 None（bars 缺失时返回 None，跳过）
    """
    if not bars or len(bars) == 0 or not bis or len(bis) < 3 or not atr or atr <= 0:
        return None
    rangeBarN = (cfg or {}).get("rangeBarN", 40)
    rangeBiN = (cfg or {}).get("rangeBiN", 4)
    rangeKMult = (cfg or {}).get("rangeKMult", 5.0)
    rangeBiMult = (cfg or {}).get("rangeBiMult", 7.0)
    rangeBreakMult = (cfg or {}).get("rangeBreakMult", 1.0)

    # 条件1：最近 rangeBarN 根K线区间
    win = bars[-rangeBarN:]
    maxH = max(b["high"] for b in win)
    minL = min(b["low"] for b in win)
    kSpan = maxH - minL
    kAtr = kSpan / atr

    # 新增：最后一笔明显突破窗口区间 → 跳过震荡判定（视为趋势）
    lastBi = bis[-1]
    if lastBi:
        if lastBi["type"] == "up":
            broke = lastBi["endPrice"] > minL + rangeBreakMult * atr
        elif lastBi["type"] == "down":
            broke = lastBi["endPrice"] < maxH - rangeBreakMult * atr
        else:
            broke = False
        if broke:
            return {"range": False, "kSpan": kSpan, "biSpan": 0, "kAtr": kAtr, "biAtr": 0,
                    "alt": True, "breakOut": True, "breakMult": rangeBreakMult,
                    "rangeBarN": rangeBarN, "rangeBiN": rangeBiN, "winBiCount": 0}

    # 条件2/条件3 的前提：笔须落在「最近 rangeBarN 根K线」的时间范围内
    winStart = win[0]["time"]
    winEnd = win[-1]["time"]
    inWin = [b for b in bis
             if (b["startTime"] >= winStart and b["startTime"] <= winEnd) or
                (b["endTime"] >= winStart and b["endTime"] <= winEnd)]
    biAtr = 0
    biSpan = 0
    hasBoth = True
    alt = True
    if len(inWin) > 0:
        recentBis = inWin[-rangeBiN:]
        endpoints = []
        for b in recentBis:
            endpoints.append(b["startPrice"])
            endpoints.append(b["endPrice"])
        biSpan = max(endpoints) - min(endpoints)
        biAtr = biSpan / atr
        types = [b["type"] for b in recentBis]
        hasBoth = "up" in types and "down" in types
        alt = True
        for i in range(2, len(types)):
            if types[i] == types[i - 1] and types[i] == types[i - 2]:
                alt = False
                break
    biOk = len(inWin) == 0 or (biAtr <= rangeBiMult and hasBoth and alt)
    is_range = kAtr <= rangeKMult and biOk
    return {"range": is_range, "kSpan": kSpan, "biSpan": biSpan, "kAtr": kAtr,
            "biAtr": biAtr, "alt": alt, "rangeBarN": rangeBarN, "rangeBiN": rangeBiN,
            "winBiCount": len(inWin)}


# ============================================================
# 纯函数：交易计划生成
# ============================================================


def strategyOf(res, type_, reason, label, cls):
    """依据买卖点类型生成交易策略（用户规则）。与 JS 版 strategyOf 对齐。"""
    base = {"res": res, "reason": reason, "label": label}
    if type_ == "1卖":
        return dict(base, direction="空头", strategy="等待反弹后做2卖")
    if type_ == "1买":
        return dict(base, direction="多头", strategy="等待回调后做2买")
    if type_ in ("2买", "类2买", "3买"):
        if cls == "过左高不背驰":
            return dict(base, direction="多头", strategy="等待回调后的新买点")
        return dict(base, direction="多头（逆势）", strategy="等待高点附近的一卖")
    if type_ in ("2卖", "类2卖", "3卖"):
        if cls == "过左低不背驰":
            return dict(base, direction="空头", strategy="等待反弹后的新卖点")
        return dict(base, direction="空头（逆势）", strategy="等待低点附近的一买")
    return dict(base, direction="观望", strategy="趋势中")


def classifySecond(bis, macdArr, p):
    """2/3 类买卖点的后续分类判定（用户规则）：
      买点（2买/类2买/3买）：买点后第一笔上涨是否「过左高」且「不背驰」；
      卖点（2卖/类2卖/3卖）：对称判定。
    @returns "过左高不背驰" | "过左低不背驰" | "其他"
    """
    wantUp = p["type"].endswith("买")
    # 买卖点之前最近顶/底端点
    extreme = {"time": -1, "price": float("-inf") if wantUp else float("inf")}
    for b in bis:
        cands = []
        if wantUp:
            if b["type"] == "up":
                cands.append({"time": b["endTime"], "price": b["endPrice"]})       # 顶：上涨笔终点
            if b["type"] == "down":
                cands.append({"time": b["startTime"], "price": b["startPrice"]})   # 顶：下跌笔起点
        else:
            if b["type"] == "down":
                cands.append({"time": b["endTime"], "price": b["endPrice"]})       # 底：下跌笔终点
            if b["type"] == "up":
                cands.append({"time": b["startTime"], "price": b["startPrice"]})   # 底：上涨笔起点
        for c in cands:
            if c["time"] >= p["time"]:
                continue
            if (c["price"] > extreme["price"]) if wantUp else (c["price"] < extreme["price"]):
                extreme["time"] = c["time"]
                extreme["price"] = c["price"]
    if extreme["time"] == -1:
        return "其他"
    # 买卖点后第一笔同向笔（买点后上涨 / 卖点后下跌），起点在买卖点之后
    after = next((b for b in bis if b["startTime"] >= p["time"] and b["type"] == ("up" if wantUp else "down")), None)
    if after is None:
        return "其他"
    # 过左高 / 过左低
    passed = after["endPrice"] > extreme["price"] if wantUp else after["endPrice"] < extreme["price"]
    if not passed:
        return "其他"
    # 不背驰：after 相对前一同向参照笔（span >= after.span*0.5）isBiDiverge=false
    refer = None
    for i in range(bis.index(after) - 1, -1, -1):
        if bis[i]["type"] != after["type"]:
            continue
        if bis[i]["span"] < after["span"] * 0.5:
            continue
        refer = bis[i]
        break
    diverge = isBiDiverge(after, refer, macdArr) if refer is not None else False
    if diverge:
        return "其他"
    return "过左高不背驰" if wantUp else "过左低不背驰"


def predictPlan(res, bis, upperBis, macdArr, lastPrice, bars, atr=0, barSec=None):
    """核心：对单个周期生成「方向 + 策略」。

    判定顺序：
      1. 震荡优先：isRangeBound（A 震荡）或 buildZS 最后一个中枢未离开且当前价在中枢内（B 震荡）
         → 方向「观望」，策略「震荡整理，观望等待方向选择」；
      2. 趋势：获取本周期买卖点（findBuyPoints/findSellPoints）：
         - 先匹配最后一笔终点上的买卖点 → 按类型映射策略；
         - 若最后一笔终点无买卖点 → 再向前获取一笔（逐笔向前扫描最近笔端点）；
         - 仍无 → 「趋势中无匹配买卖点」。
    @returns { res, direction, strategy, reason, label, pointDesc }
    """
    if barSec is None:
        barSec = intervalSecOf(res) or 60
    empty = {"res": res, "direction": "观望", "strategy": "数据不足",
             "reason": "笔数量不足，无法判断", "label": "数据不足"}
    if not bis or len(bis) < 2:
        return empty

    # 1. 震荡优先（A：isRangeBound 横盘判定）
    rb = isRangeBound(bis, bars, atr)
    if rb and rb["range"]:
        reason = (f"最近 {rb['rangeBarN']} 根K线区间 {rb['kSpan']:.2f}（{rb['kAtr']:.1f}×ATR）"
                  + (f"，笔端点区间 {rb['biSpan']:.2f}（{rb['biAtr']:.1f}×ATR），涨跌交替无明确方向"
                     if rb["winBiCount"] > 0 else "，窗口内无笔")
                  + "，判定为震荡整理")
        return {"res": res, "direction": "观望", "strategy": "震荡整理，观望等待方向选择",
                "reason": reason, "label": "震荡观望"}

    # 1b. 震荡判定（B：存在未离开的中枢且当前价在中枢区间内）
    zss = []
    try:
        if upperBis and len(upperBis) > 0:
            zss = buildZSByUpper(bis, upperBis, barSec)
        else:
            zss = buildZS(bis, barSec)
    except Exception:
        pass
    upperLast = upperBis[-1] if (upperBis and len(upperBis) > 0) else None
    zsList = [z for z in zss if upperLast and z.get("upperStart") is not None
              and z["upperStart"] >= upperLast["startTime"] - barSec] if upperLast else zss
    lastZS = zsList[-1] if zsList else None
    if lastZS and lastZS.get("exitTime") is None and lastPrice is not None \
       and lastZS["zd"] <= lastPrice <= lastZS["zg"]:
        reason = (f"存在未离开中枢 [{lastZS['zd']:.2f}, {lastZS['zg']:.2f}]（归属上一级别同一笔内），"
                  f"当前价 {lastPrice:.2f} 位于中枢内，判定为震荡整理")
        return {"res": res, "direction": "观望", "strategy": "震荡整理（中枢内），观望等待方向选择",
                "reason": reason, "label": "震荡观望"}

    # 2. 趋势 → 获取本周期买卖点
    buyPts = []
    sellPts = []
    try:
        if len(bis) >= 3:
            buyPts = findBuyPoints(bis, upperBis or [], macdArr or [], barSec)
            sellPts = findSellPoints(bis, upperBis or [], macdArr or [], barSec)
    except Exception:
        pass

    # 匹配某笔终点上的买卖点（时间容差 = 本周期 1 个 bar，价格容差 = max(ATR×0.2, 0.05)）
    tolSec = barSec
    tolPrice = max(atr * 0.2, 0.05)

    def matchAt(biIdx):
        bi = bis[biIdx]
        if bi is None:
            return None
        all_ = buyPts + sellPts
        for p in all_:
            if abs(p["time"] - bi["endTime"]) <= tolSec and abs(p["price"] - bi["endPrice"]) <= tolPrice:
                return {"point": p, "bi": bi}
        return None

    # 3. 先取最后一笔终点的买卖点
    lastMatch = matchAt(len(bis) - 1)
    if lastMatch:
        p = lastMatch["point"]
        reason = f"找到最近买卖点 {p['type']} @ {fmtT(p['time'])} {p['price']:.2f}（最后一笔端点）"
        cls = classifySecond(bis, macdArr, p) if p["type"] in ("2买", "类2买", "3买", "2卖", "类2卖", "3卖") else "其他"
        out = strategyOf(res, p["type"], reason, f"趋势|{p['type']}", cls)
        out["strategyLabel"] = out["strategy"]
        out["pointDesc"] = f"{p['type']}@{fmtT(p['time'])}({p['price']:.2f})"
        return out

    # 4. 最后一笔终点无买卖点 → 逐笔向前扫描最近笔端点
    prevMatch = None
    for j in range(len(bis) - 2, -1, -1):
        prevMatch = matchAt(j)
        if prevMatch:
            break
    if prevMatch:
        p = prevMatch["point"]
        reason = f"找到最近买卖点 {p['type']} @ {fmtT(p['time'])} {p['price']:.2f}（向前扫描最近笔端点）"
        cls = classifySecond(bis, macdArr, p) if p["type"] in ("2买", "类2买", "3买", "2卖", "类2卖", "3卖") else "其他"
        out = strategyOf(res, p["type"], reason, f"趋势|{p['type']}", cls)
        out["strategyLabel"] = out["strategy"]
        out["pointDesc"] = f"{p['type']}@{fmtT(p['time'])}({p['price']:.2f})"
        return out

    # 5. 趋势但未匹配到买卖点
    reason = "趋势（非震荡），但最近笔端点均无已确认买卖点"
    return {"res": res, "direction": "观望", "strategy": "趋势中无匹配买卖点",
            "reason": reason, "label": "观察"}


# ============================================================
# 汇总计算（供回测链路调用）
# ============================================================


def compute_plan(periodBis, barsByPeriod, periods, periodMacd=None, periodAtr=None):
    """逐周期（从大到小）计算交易计划。

    @param periodBis    各周期笔 { 周期: [bis] }
    @param barsByPeriod 各周期原始K线 { 周期: [bars] }
    @param periods      周期列表（从大到小）
    @param periodMacd   可选：各周期预计算 MACD { 周期: [macdArr] }（增量回测用，避免重复计算）
    @param periodAtr    可选：各周期预计算 ATR { 周期: atr }
    @returns { 周期: { direction, strategy, reason, pointDesc } }
    """
    periodMacd = periodMacd or {}
    periodAtr = periodAtr or {}
    planRows = {}
    upperBis = None
    for res in periods:
        curBis = periodBis.get(res, []) or []
        if not curBis:
            continue
        rawBars = barsByPeriod.get(res, []) or []
        atr = periodAtr.get(res)
        if atr is None:
            atr = calcATR(rawBars, 14)
        macdArr = periodMacd.get(res)
        if macdArr is None:
            macdArr = calcMACD(rawBars)
        lastBar = rawBars[-1] if rawBars else None
        lastPrice = lastBar["close"] if lastBar else None
        # 取最近 60 笔即可（计划只看最新结构）
        if len(curBis) > 60:
            curBis = curBis[-60:]
        p = predictPlan(res=res, bis=curBis, upperBis=upperBis, macdArr=macdArr,
                        lastPrice=lastPrice, bars=rawBars, atr=atr,
                        barSec=intervalSecOf(res))
        planRows[res] = {
            "direction": p["direction"],
            "strategy": p["strategy"],
            "reason": p.get("reason", ""),
            "pointDesc": p.get("pointDesc", ""),
        }
        upperBis = curBis
    return planRows
