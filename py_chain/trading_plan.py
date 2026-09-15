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
    markWickBars, mergeBars, findFractals,
)

# ============================================================
# 纯函数：震荡判定（复制自 chan-status SKILL，保持原逻辑不变）
# ============================================================

# 震荡判定参数默认值（参数中心 param_center 的默认值单一来源；cfg 逐键覆盖）
RANGE_DEFAULTS = {
    "rangeBarN": 40,       # 震荡判定窗口K线数
    "rangeBiN": 4,         # 震荡判定最近笔数
    "rangeKMult": 5.0,     # K线区间阈值（≤5×ATR 判震荡）
    "rangeBiMult": 7.0,    # 笔端点极差阈值（≤7×ATR）
    "rangeBreakMult": 1.0, # 突破跳过：末笔端点越过窗口另一端 >1×ATR 视为突破
}


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
    cfg = cfg or RANGE_DEFAULTS
    rangeBarN = cfg.get("rangeBarN", RANGE_DEFAULTS["rangeBarN"])
    rangeBiN = cfg.get("rangeBiN", RANGE_DEFAULTS["rangeBiN"])
    rangeKMult = cfg.get("rangeKMult", RANGE_DEFAULTS["rangeKMult"])
    rangeBiMult = cfg.get("rangeBiMult", RANGE_DEFAULTS["rangeBiMult"])
    rangeBreakMult = cfg.get("rangeBreakMult", RANGE_DEFAULTS["rangeBreakMult"])

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
    """依据买卖点类型生成交易策略（用户规则）。与 JS 版 strategyOf 对齐。
    方向命名「X头Y」：X = 结构方向，Y = 操作方向。"""
    base = {"res": res, "reason": reason, "label": label}
    if type_ == "1卖":
        return dict(base, direction="空头空", strategy="等待反弹后做2卖")
    if type_ == "1买":
        return dict(base, direction="多头多", strategy="等待回调后做2买")
    if type_ in ("2买", "类2买", "3买"):
        if cls == "过左高不背驰":
            return dict(base, direction="多头多", strategy="等待回调后的新买点")
        return dict(base, direction="多头空", strategy="等待高点附近的一卖")
    if type_ in ("2卖", "类2卖", "3卖"):
        if cls == "过左低不背驰":
            return dict(base, direction="空头空", strategy="等待反弹后的新卖点")
        return dict(base, direction="空头多", strategy="等待低点附近的一买")
    return dict(base, direction="观望", strategy="趋势中")


def classifySecond(bis, macdArr, p):
    """2/3 类买卖点的后续分类判定（用户规则，与 JS 版对齐）：
      买点（2买/类2买/3买）：买点后第一笔上涨是否「过左高」且「不背驰」。
        左高 = 买点之前时间最近的前顶（同一时刻多端点取最高）；
        过左高 = after（买点后第一笔上涨）终点价 > 左高价；
        不背驰 = after 相对紧邻的前一同向上涨参照笔 isBiDiverge=false。
      卖点（2卖/类2卖/3卖）：对称判定（左低取时间最近前底、参照取紧邻前一同向笔）。
      注：左高/左低取「时间最近」而非全史价格极值；参照笔不按幅度过滤。
    @returns "过左高不背驰" | "过左低不背驰" | "其他"
    """
    wantUp = p["type"].endswith("买")
    # 买卖点之前时间最近的顶/底端点（同一时刻多端点取价格更极端者）
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
            if c["time"] > extreme["time"]:
                extreme["time"] = c["time"]
                extreme["price"] = c["price"]
            elif c["time"] == extreme["time"]:
                if wantUp:
                    extreme["price"] = max(extreme["price"], c["price"])
                else:
                    extreme["price"] = min(extreme["price"], c["price"])
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
    # 不背驰：after 相对紧邻的前一同向参照笔 isBiDiverge=false
    refer = None
    for i in range(bis.index(after) - 1, -1, -1):
        if bis[i]["type"] != after["type"]:
            continue
        refer = bis[i]  # 紧邻前一同向笔（中间隔一次级反向运动，即同级别对照段）
        break
    diverge = isBiDiverge(after, refer, macdArr) if refer is not None else False
    if diverge:
        return "其他"
    return "过左高不背驰" if wantUp else "过左低不背驰"


def predictPlan(res, bis, upperBis, macdArr, lastPrice, bars, atr=0, barSec=None,
                range_cfg=None):
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

    # 1. 震荡优先（A：isRangeBound 横盘判定，range_cfg 来自参数中心逐键覆盖）
    rb = isRangeBound(bis, bars, atr, range_cfg)
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


def compute_plan(periodBis, barsByPeriod, periods, periodMacd=None, periodAtr=None, cfg=None):
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
                        barSec=intervalSecOf(res), range_cfg=cfg)
        planRows[res] = {
            "direction": p["direction"],
            "strategy": p["strategy"],
            "reason": p.get("reason", ""),
            "pointDesc": p.get("pointDesc", ""),
        }
        upperBis = curBis
    return planRows


# ============================================================
# 纯函数：参考周期方向判定（顺势过滤，2026-09-15 口径与用户逐条确认）
# ============================================================

# 顺势参考周期默认值："" = 关闭；"240" = 4小时；"D" = 日线
# （参数中心 plan 模块 trendRes，Web 交易计划页签可配；mark_entry 进场方向过滤消费）
TREND_RES = "240"

# 周期 → 中文名（方向成因展示用，如「4小时2买」「日线1卖」）
RES_NAME_CN = {"D": "日线", "240": "4小时", "60": "1小时", "30": "30分钟",
               "15": "15分钟", "3": "3分钟", "30S": "30秒"}


def trend_res_name(res):
    return RES_NAME_CN.get(str(res).upper(), str(res))


def strong_fractal_after(merged, fractals, t, kind):
    """t（含）之后是否出现强分型（kind="bottom"|"top"）。

    强分型定义（与用户确认，2026-09-15）：底分型右肩（第 3 根合并K）收盘价 >
    左肩（第 1 根合并K）最高价；顶分型镜像（右肩收盘 < 左肩最低）。合并K/分型
    用 chan_core 现有链（markWickBars → mergeBars → findFractals，与 buildBi 同源）。
    """
    for f in fractals:
        if f["type"] != kind or f["time"] < t:
            continue
        i = f["mergedIdx"]
        if i - 1 < 0 or i + 1 >= len(merged):
            continue  # 左/右肩不完整（尾部形成中）
        left, right = merged[i - 1], merged[i + 1]
        if kind == "bottom" and right["close"] > left["high"]:
            return True
        if kind == "top" and right["close"] < left["low"]:
            return True
    return False


def trend_direction(res, bis, bars, upperBis, macdArr, tCut=None):
    """参考周期方向判定（顺势过滤的唯一口径；mark_entry 进场方向过滤消费）。

    规则（2026-09-15 与用户逐条确认）：
      1. 取参考周期最近一个买卖点（findBuyPoints/findSellPoints，最近 60 笔窗口
         与 compute_plan 同口径）；
      2. 1买/1卖：端点后出现强分型（strong_fractal_after）才确立方向，
         强分型出现前回退末笔方向；
      3. 非1类点（2买/类2买/3买、2卖/类2卖/3卖）：出现即确立方向（点锚定的笔
         本身已够笔成笔）；
      4. 确立后破坏：参考周期任一收盘价 跌破买点端点价 / 涨破卖点端点价 →
         反向（下跌/上涨延续），闩锁到下一个买卖点事件（实现口径：从点时间起
         扫描全部收盘，命中即反向——价格收回也不翻回，直到更新的买卖点出现）；
      5. 无买卖点 / 1类点强分型未出现 → 回退最近一笔方向（末笔 up→多、down→空）；
      6. 笔数据不足（<2 笔）→ (None, "")，消费方不过滤。

    @returns (dir, reason)：dir ∈ "long"/"short"/None；reason 展示用，
              如 "4小时2买"（点确立）、"4小时下跌延续"（破坏闩锁）、
              "4小时末笔向上"（回退）；dir=None 时 reason=""。
    """
    name = trend_res_name(res)
    if not bis or len(bis) < 2:
        return None, ""
    win = bis[-60:]  # 与 compute_plan 同窗口：计划只看最新结构
    barSec = intervalSecOf(res)
    try:
        pts = findBuyPoints(win, upperBis, macdArr, barSec) \
            + findSellPoints(win, upperBis, macdArr, barSec)
    except Exception:
        pts = []
    if tCut is not None:
        pts = [p for p in pts if p["time"] <= tCut]
    pts.sort(key=lambda p: p["time"])
    if bis[-1]["type"] == "up":
        fallback = ("long", f"{name}末笔向上")
    else:
        fallback = ("short", f"{name}末笔向下")
    if not pts:
        return fallback
    p = pts[-1]
    t_ = p["type"]
    is_buy = t_ in ("1买", "2买", "类2买", "3买")
    if t_ in ("1买", "1卖"):
        # 1类点须先出现强分型（合并K链与 buildBi 同源；懒计算——仅 1类点需要）
        merged = mergeBars(markWickBars(bars or []))
        if not strong_fractal_after(merged, findFractals(merged), p["time"],
                                    "bottom" if t_ == "1买" else "top"):
            return fallback
    # 破坏闩锁：点确立后任一参考周期收盘破端点价 → 反向延续（价格收回不翻回）
    for b in bars or []:
        if b["time"] < p["time"]:
            continue
        if is_buy:
            if b["close"] < p["price"]:
                return ("short", f"{name}下跌延续")
        elif b["close"] > p["price"]:
            return ("long", f"{name}上涨延续")
    return ("long" if is_buy else "short"), f"{name}{t_}"


def trend_state_of(periodBis, barsByPeriod, trend_res, periodMacd=None):
    """按配置计算参考周期方向状态（mark_entry 顺势过滤统一入口）。

    上级周期取法与 mark_entry.upperResOf 同口径：periodBis 中比参考周期大一级的
    最小周期（240→D），供 findBuyPoints 区间套使用。

    @param trend_res 参考周期（"240"/"D"；""/None = 关闭 → 返回 None）
    @returns {"dir": "long"/"short"/None, "reason": str, "res": 参考周期}；
             参考周期无笔数据时 dir=None（不过滤，规则 6）。检测周期的结构性剔除
             由 mark_entry 按 trend_res 字符串独立执行，与本状态无关。
    """
    if not trend_res:
        return None
    tr = str(trend_res).upper()
    bis = (periodBis or {}).get(tr) or []
    bars = (barsByPeriod or {}).get(tr) or []
    if not bis:
        return {"dir": None, "reason": "", "res": tr}
    sec = intervalSecOf(tr) or 0
    upper = None
    best = None
    for r in periodBis:
        s = intervalSecOf(r) or 0
        if s > sec and (best is None or s < (intervalSecOf(best) or 0)):
            best = r
    if best is not None:
        upper = periodBis.get(best)
    macdArr = (periodMacd or {}).get(tr)
    if macdArr is None:
        macdArr = calcMACD(bars)
    d, reason = trend_direction(tr, bis, bars, upper, macdArr)
    return {"dir": d, "reason": reason, "res": tr}
