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
    calcATR, calcMACD, intervalSecOf, fmtT, CHAN_CFG,
    findBuyPoints, findSellPoints, buildZS, buildZSByUpper, isBiDiverge,
    markWickBars, mergeBars, findFractals,
)

from .chan_core import structurePeriods, buildStructureContext, mergedSegmentCount

# ============================================================
# 纯函数：震荡判定（复制自 chan-status SKILL，保持原逻辑不变）
# ============================================================

# 震荡判定参数默认值（参数中心 param_center 的默认值单一来源；cfg 逐键覆盖）
RANGE_DEFAULTS = {
    "rangeBoundOn": True,  # ①横盘判定开关（关闭后跳过 isRangeBound）
    "rangeBarN": 40,       # 震荡判定窗口K线数
    "rangeBiN": 4,         # 震荡判定最近笔数
    "rangeKMult": 5.0,     # K线区间阈值（≤5×ATR 判震荡）
    "rangeBiMult": 7.0,    # 笔端点极差阈值（≤7×ATR）
    "rangeBreakMult": 1.0, # 突破跳过：末笔端点越过窗口另一端 >1×ATR 视为突破
    "rangeZsOn": True,     # ②中枢内判定开关（关闭后跳过未离开中枢+现价在箱内）
}

# 2买/2卖 中间档「附近」容差默认值（绝对点数；参数中心 plan 模块同名透传，cfg 逐键覆盖）
PREV_HIGH_NEAR_PTS = 5.0  # 前高/前低附近：2买（2卖）后首段上涨（下跌）终点距前高（前低）≤ 该值 → 等回调后的类2买点（类2卖点）
SECOND_NEAR_PTS = 5.0     # 回到2买/2卖点：未过前高（前低）时最近一笔回调（反弹）终点距点价 ≤ 该值 → 等回调后的类2买点（类2卖点）
# ③3类点强档开关：开=3买/类3买（3卖/类3卖）过前高不背驰时顺势「等待回调后的新买点/新卖点」（现状）；关=3类点一律弱档
THIRD_STRONG_TREND = True


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


def strategyOf(res, type_, reason, label, cls, cfg=None):
    """依据买卖点类型生成交易策略（用户规则，2026-09-24 三档映射）。与 JS 版 strategyOf 对齐。
    方向命名「X头Y」：X = 结构方向，Y = 操作方向。
    三档（按序判定）：强档（过左高/左低不背驰）→ 中间档（仅 2买/2卖：前高/前低附近、
    或未过前高/前低且回调回到点附近）→ 弱档（多头空/空头多）。
    3类点强档受 thirdStrongTrend 开关控制（默认开=保持现状「等待回调后的新买点/新卖点」）。"""
    cfg = cfg or {}
    third_strong = cfg.get("thirdStrongTrend", THIRD_STRONG_TREND)
    base = {"res": res, "reason": reason, "label": label}
    if type_ == "1卖":
        return dict(base, direction="空头空", strategy="等待反弹后做2卖")
    if type_ == "1买":
        return dict(base, direction="多头多", strategy="等待回调后做2买")
    if type_ in ("2买", "类2买"):
        if cls == "过左高不背驰":
            return dict(base, direction="多头多", strategy="等待回调后的3买点")
        if type_ == "2买" and cls in ("前高附近", "回到2买点"):
            return dict(base, direction="多头多", strategy="等待回调后的类2买点")
        return dict(base, direction="多头空", strategy="等待高点附近的一卖")
    if type_ in ("2卖", "类2卖"):
        if cls == "过左低不背驰":
            return dict(base, direction="空头空", strategy="等待反弹后的3卖点")
        if type_ == "2卖" and cls in ("前低附近", "回到2卖点"):
            return dict(base, direction="空头空", strategy="等待反弹后的类2卖点")
        return dict(base, direction="空头多", strategy="等待低点附近的一买")
    if type_ in ("3买", "类3买"):
        if third_strong and cls == "过左高不背驰":
            return dict(base, direction="多头多", strategy="等待回调后的新买点")
        return dict(base, direction="多头空", strategy="等待高点附近的一卖")
    if type_ in ("3卖", "类3卖"):
        if third_strong and cls == "过左低不背驰":
            return dict(base, direction="空头空", strategy="等待反弹后的新卖点")
        return dict(base, direction="空头多", strategy="等待低点附近的一买")
    return dict(base, direction="观望", strategy="趋势中")


def classifySecond(bis, macdArr, p, cfg=None):
    """2/3 类买卖点的后续分类判定（用户规则，2026-09-24 三档，与 JS 版对齐）：
      强档——买点（2买/类2买/3买）：买点后第一笔上涨「过左高」且「不背驰」。
        左高 = 买点之前时间最近的前顶（同一时刻多端点取最高）；
        过左高 = after（买点后第一笔上涨）终点价 > 左高价；
        不背驰 = after 相对紧邻的前一同向上涨参照笔 isBiDiverge=false。
      中间档（仅 2买/2卖 在 strategyOf 消费，这里一并返回）：
        前高附近 = after 终点距左高 ≤ prevHighNearPts（绝对点数，含刚越过但背驰的情形）；
        回到2买点 = 未过左高，且 after 之后最近一笔反向笔（回调/反弹，含形成中）
                    终点价距点价 ≤ secondNearPts。
      卖点（2卖/类2卖/3卖）：对称判定（左低取时间最近前底、参照取紧邻前一同向笔）。
      注：左高/左低取「时间最近」而非全史价格极值；参照笔不按幅度过滤。
    @returns "过左高不背驰" | "过左低不背驰" | "前高附近" | "前低附近" | "回到2买点" | "回到2卖点" | "其他"
    """
    cfg = cfg or {}
    prev_high_near = cfg.get("prevHighNearPts", PREV_HIGH_NEAR_PTS)
    second_near = cfg.get("secondNearPts", SECOND_NEAR_PTS)
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
    diverge = False
    if passed:
        # 不背驰：after 相对紧邻的前一同向参照笔 isBiDiverge=false
        refer = None
        for i in range(bis.index(after) - 1, -1, -1):
            if bis[i]["type"] != after["type"]:
                continue
            refer = bis[i]  # 紧邻前一同向笔（中间隔一次级反向运动，即同级别对照段）
            break
        diverge = isBiDiverge(after, refer, macdArr) if refer is not None else False
    if passed and not diverge:
        return "过左高不背驰" if wantUp else "过左低不背驰"
    # 中间档A：前高/前低附近（after 终点距左高/左低 ≤ prevHighNearPts，含刚越过但背驰的情形）
    if abs(after["endPrice"] - extreme["price"]) <= prev_high_near:
        return "前高附近" if wantUp else "前低附近"
    # 中间档B：未过左高/左低，且之后最近一笔反向笔（回调/反弹，含形成中）终点回到点价附近
    if not passed:
        pullback = None
        for b in bis[bis.index(after) + 1:]:
            if b["type"] == ("down" if wantUp else "up"):
                pullback = b  # 取最近一笔回调/反弹
        if pullback is not None and abs(pullback["endPrice"] - p["price"]) <= second_near:
            return "回到2买点" if wantUp else "回到2卖点"
    return "其他"


def _rangeVerdict(bis, bars, atr, upperBis, lastPrice, barSec, range_cfg):
    """A/B 两支震荡判定（原 predictPlan 1/1b 抽取；本周期自身判定与参考周期 regime 复用）。
    @returns 震荡观望行 dict（direction/strategy/reason/label）或 None（非震荡）。"""
    cfg = range_cfg or RANGE_DEFAULTS
    # A 支：isRangeBound 横盘判定（range_cfg 来自参数中心逐键覆盖；rangeBoundOn 关闭则跳过）
    if cfg.get("rangeBoundOn", RANGE_DEFAULTS["rangeBoundOn"]):
        rb = isRangeBound(bis, bars, atr, cfg)
        if rb and rb["range"]:
            reason = (f"最近 {rb['rangeBarN']} 根K线区间 {rb['kSpan']:.2f}（{rb['kAtr']:.1f}×ATR）"
                      + (f"，笔端点区间 {rb['biSpan']:.2f}（{rb['biAtr']:.1f}×ATR），涨跌交替无明确方向"
                         if rb["winBiCount"] > 0 else "，窗口内无笔")
                      + "，判定为震荡整理")
            return {"direction": "观望", "strategy": "震荡整理，观望等待方向选择",
                    "reason": reason, "label": "震荡观望"}

    # B 支：存在未离开的中枢且当前价在中枢区间内（rangeZsOn 关闭则跳过）
    if not cfg.get("rangeZsOn", RANGE_DEFAULTS["rangeZsOn"]):
        return None
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
        return {"direction": "观望", "strategy": "震荡整理（中枢内），观望等待方向选择",
                "reason": reason, "label": "震荡观望"}
    return None


def _plan_gate_row(res, bis, bars, atr, upperBis, lastPrice, barSec, range_cfg):
    """参考周期震荡 regime（compute_plan 在参考周期行上顺带计算，供更低周期 range_gate；
    2026-09-16 替换语义——更低周期不再看自身 A/B 震荡，参考周期笔不足也观望）。
    @returns {"range": True, "resName": "4小时", "reason": "4小时：…"} /
             {"range": False, ...} /
             {"range": True, "insufficient": True, ...}——笔数 <2：更低周期直接观望（数据不足）。"""
    name = trend_res_name(res)
    if bis and not bis[-1].get("_contextReady"):
        ctx = buildStructureContext(bis, bars, intervalSecOf(res), tCut)
        bis = ctx["bis"]
        if tCut is not None:
            bars = [b for b in (bars or []) if b["time"] + intervalSecOf(res) <= tCut]
    if not bis or len(bis) < 2:
        return {"range": True, "insufficient": True, "resName": name,
                "reason": f"{name}笔数据不足（少于2笔），无法判定震荡/趋势，观望"}
    v = _rangeVerdict(bis, bars, atr, upperBis, lastPrice, barSec, range_cfg)
    if v is None:
        return {"range": False, "resName": name, "reason": ""}
    return {"range": True, "resName": name, "reason": f"{name}：{v['reason']}"}


def predictPlan(res, bis, upperBis, macdArr, lastPrice, bars, atr=0, barSec=None,
                range_cfg=None, range_gate=None):
    """核心：对单个周期生成「方向 + 策略」。

    判定顺序：
      1. 震荡优先——判定源二选一（2026-09-16）：range_gate 给定时用参考周期 regime
         （替换语义：regime 震荡 → 观望；regime 非震荡 → 跳过本周期 A/B 支直接走趋势分支；
         regime 带 insufficient 标记（参考周期笔 <2）→ 直接观望「笔数据不足」）；
         None 时按本周期自身判定：isRangeBound（A 震荡）或 buildZS 最后一个中枢未离开且
         当前价在中枢内（B 震荡）→ 方向「观望」，策略「震荡整理，观望等待方向选择」；
         （自身判定分支保留为纯函数原语供直调/JS 对齐——引擎 compute_plan 中仅
          参考周期自身经 _plan_gate_row 走 A/B，其余周期要么听门要么只作锚。）
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

    # 1. 震荡优先（判定源见 docstring；range_gate 由 compute_plan 按周期层级传入）
    if range_gate is not None:
        if range_gate.get("range"):
            if range_gate.get("insufficient"):
                return {"res": res, "direction": "观望",
                        "strategy": (f"{range_gate.get('resName', '参考周期')}"
                                     "笔数据不足，观望"),
                        "reason": range_gate.get("reason", ""), "label": "数据不足"}
            return {"res": res, "direction": "观望",
                    "strategy": (f"震荡整理（{range_gate.get('resName', '参考周期')}），"
                                 "观望等待方向选择"),
                    "reason": range_gate.get("reason", ""), "label": "震荡观望"}
        # regime 非震荡 → 跳过本周期 A/B 支，直接走趋势分支
    else:
        v = _rangeVerdict(bis, bars, atr, upperBis, lastPrice, barSec, range_cfg)
        if v is not None:
            return dict(v, res=res)

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
        cls = classifySecond(bis, macdArr, p, range_cfg) if p["type"] in ("2买", "类2买", "3买", "类3买", "2卖", "类2卖", "3卖", "类3卖") else "其他"
        out = strategyOf(res, p["type"], reason, f"趋势|{p['type']}", cls, range_cfg)
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
        cls = classifySecond(bis, macdArr, p, range_cfg) if p["type"] in ("2买", "类2买", "3买", "类3买", "2卖", "类2卖", "3卖", "类3卖") else "其他"
        out = strategyOf(res, p["type"], reason, f"趋势|{p['type']}", cls, range_cfg)
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


def compute_plan(periodBis, barsByPeriod, periods, periodMacd=None, periodAtr=None, cfg=None,
                 work_cache=None, range_res=None):
    """逐周期（从大到小）计算交易计划。

    @param work_cache 可选：跨次调用复用。本周期笔指纹/cut/ATR/上级笔未变时直接复用该行计划。
    @param range_res  震荡判定参考周期（必填；None → RANGE_RES；"" = 未配置（防御））。
                      2026-09-16 最终口径：
                      - "" → 全部周期固定观望（「参考周期未配置」，参数面已不提供关闭选项）；
                      - 不低于该周期的行（参考周期自身与更高周期）固定观望，只作锚——
                        regime（震荡门）与 trend_direction（顺势过滤）照常在内部计算，
                        但不再做计划行判定（无自身 A/B、无买卖点匹配）；
                      - 严格更低的周期只看该周期 regime（_plan_gate_row）：震荡 → 观望；
                        非震荡 → 直接走趋势分支；笔 <2 → 观望「笔数据不足」
                        （不回退自身判定——引擎内自身 A/B 只剩参考周期给自己判这一处）。
    """
    from .sr_flip import _bis_fingerprint
    if range_res is None:
        range_res = RANGE_RES
    periodBis = structurePeriods(periodBis, barsByPeriod, work_cache=work_cache)
    periodMacd = periodMacd or {}
    periodAtr = periodAtr or {}
    planRows = {}
    cfg_fp = tuple(sorted((cfg or {}).items())) if cfg else ()
    if not range_res:
        # 必填项未配置（防御）——全部周期观望，不做任何判定
        for res in periods:
            if not (periodBis.get(res, []) or []):
                continue
            planRows[res] = {"direction": "观望", "strategy": "参考周期未配置，观望",
                             "reason": "rangeRes 未配置（必填项），全部周期观望",
                             "pointDesc": "", "label": "配置缺失"}
        return planRows
    ref_sec = intervalSecOf(range_res) or 0
    ref_name = trend_res_name(range_res)
    upperBis = None
    range_gate = None  # 参考周期震荡 regime（见 _plan_gate_row；随循环向更低周期传递）
    ref_row = next((r for r in periods if str(r).upper() == str(range_res).upper()), None)
    if ref_row is not None and len(periodBis.get(ref_row) or []) < 2:
        # 参考周期笔不足（含整行无数据被 continue 跳过的情况）——预置不足闸；
        # 行内 ≥2 笔时会被真闸覆盖
        range_gate = _plan_gate_row(ref_row, periodBis.get(ref_row) or [],
                                    None, None, None, None, None, None)
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
        # 震荡判定只看最近 rangeBarN 根；截尾与全量在该窗口内逐位一致
        range_n = (cfg or RANGE_DEFAULTS).get("rangeBarN", RANGE_DEFAULTS["rangeBarN"])
        plan_bars = rawBars[-range_n:] if rawBars and len(rawBars) > range_n else rawBars

        # 震荡判定源 regime：在参考周期自身的行上顺带计算（其 upperBis 此刻 = 更高一级笔），
        # 挂 work_cache（输入指纹未变时复用，与 plan 行同口径；重同步后由调用方清空）
        is_range_src = str(res).upper() == str(range_res).upper()
        if is_range_src:
            gate_key = ("planGate", res, _bis_fingerprint(curBis), len(rawBars),
                        lastPrice, atr, _bis_fingerprint(upperBis), cfg_fp)
            gate_ent = work_cache.get(("planGate", res)) if work_cache is not None else None
            if gate_ent is not None and gate_ent[0] == gate_key:
                range_gate = gate_ent[1]
            else:
                range_gate = _plan_gate_row(res, curBis, plan_bars, atr, upperBis,
                                            lastPrice, intervalSecOf(res), cfg)
                if work_cache is not None:
                    work_cache[("planGate", res)] = (gate_key, range_gate)

        cache_key = (
            "plan", res, _bis_fingerprint(curBis), len(rawBars), lastPrice, atr,
            _bis_fingerprint(upperBis), cfg_fp,
        )
        if work_cache is not None:
            ent = work_cache.get(("plan", res))
            if ent is not None and ent[0] == cache_key:
                planRows[res] = ent[1]
                upperBis = ent[2]
                continue

        if (intervalSecOf(res) or 0) >= ref_sec:
            # 不低于参考周期：只作锚，固定观望（不做自身 A/B 与买卖点判定）
            p = {"res": res, "direction": "观望",
                 "strategy": "参考周期，观望（只作锚不交易）",
                 "reason": f"不低于震荡参考周期{ref_name}，不参与交易计划判定",
                 "label": "参考周期", "pointDesc": ""}
        else:
            p = predictPlan(res=res, bis=curBis, upperBis=upperBis, macdArr=macdArr,
                            lastPrice=lastPrice, bars=plan_bars, atr=atr,
                            barSec=intervalSecOf(res), range_cfg=cfg,
                            range_gate=range_gate)
        row = {
            "direction": p["direction"],
            "strategy": p["strategy"],
            "reason": p.get("reason", ""),
            "pointDesc": p.get("pointDesc", ""),
        }
        planRows[res] = row
        if work_cache is not None:
            work_cache[("plan", res)] = (cache_key, row, curBis)
        upperBis = curBis
    return planRows


# ============================================================
# 纯函数：参考周期方向判定（顺势过滤，2026-09-15 口径与用户逐条确认）
# ============================================================

# 顺势参考周期默认值："" = 关闭；"240" = 4小时；"D" = 日线
# （参数中心 plan 模块 trendRes，Web 交易计划页签可配；mark_entry 进场方向过滤消费）
TREND_RES = "240"

# 震荡判定参考周期（必填，2026-09-16 最终口径）："240" = 4小时（默认）；"D" = 日线
# （参数中心 plan 模块 rangeRes，枚举无关闭项）；"" = 未配置（防御）→ 全部周期观望。
# 更低周期只看该周期 regime（compute_plan → _plan_gate_row → predictPlan(range_gate=…)，
# 参考周期笔不足 → 观望）；参考周期自身与更高周期固定观望只作锚。
RANGE_RES = "240"

# 方向相位判定（2026-09-17 与用户确认，图片决策树；参数中心 plan 模块可配）：
# 锚点确立后、破坏闩锁未触发期间，按「形成段方向 → 够笔 → 位置 → 角度强弱」分相位，
# 可返回观望（dir=None 带 reason，消费方双向放行）。总表 ①~⑤ 与闩锁优先级不变。
TREND_REBOUND = True    # 相位判定开关（关闭 → 退回旧行为：确立方向锁到闩锁/新点）
REBOUND_NEAR_PTS = 5.0  # 「附近」容差（绝对点数）：形成段极值距中枢上/下沿或前低/前高
REBOUND_ANGLE_REF = 5.0 # 角度 45° 基准（点/根）：当前笔平均每根幅度 > 该值 = 角度>45°（强）
                        # （XAUUSD 4h 量级默认；跨品种需调参）

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


def _seg_bars_since(bars, t0, tCut=None):
    """t0（不含）之后、tCut（含）之前的原始K根数（与 mark_entry._barsSince 同口径 (t0, tCut]）。"""
    n = 0
    for b in bars or []:
        if b["time"] <= t0:
            continue
        if tCut is not None and b["time"] > tCut:
            continue
        n += 1
    return n


def _phase_direction(name, p, t_, bis, bars, upperBis, barSec, rb, tCut=None):
    """方向相位树（2026-09-17 与用户确认，图片决策树；卖点侧 + 买点完全镜像）。

    适用窗口：锚点确立后（1类点强分型已过 / 非1类点出现即确立）、破坏闩锁未触发
    （trend_direction 里闩锁循环优先，走到这里即未触发）。

    判据口径（用户确认）：
      - 形成段 = 结构上下文末段，含底/顶分型确认后立即参与的预期段；
      - 够笔 = 形成段起点所在合并块起（含）≥ rb["min_bars"] 块；
      - 角度强/弱（下跌角度与反弹/回调力度同一口径）= 当前笔平均每根点数
        span/根数 > rb["angle_ref"]（45° 基准，点/根）为强，否则弱；
      - 近中枢边界 = 形成段极值（末笔端点价）距「锚点前最近已形成中枢」的 ZG/ZD
        ≤ rb["near_pts"]（上/下沿附近结论相同，实现合并判）；无中枢 → 其他位置；
      - 前低/前高（2/3 类锚点）= 锚点前最近 down/up 笔端点价，极值距其 ≤ near_pts 为附近；
      - 观望 = (None, reason)：消费方双向放行，信号仍附 trendReason 注记。

    @returns (dir, reason)，dir ∈ "long"/"short"/None。
    """
    seg = bis[-1]
    seg_type = seg["type"]
    seg_bars = _seg_bars_since(bars, seg["startTime"], tCut)
    merged = mergeBars(markWickBars(bars or []))
    enough = mergedSegmentCount(merged, seg["startTime"], barSec) >= int(rb.get("min_bars", 5) or 5)
    angle_strong = seg["span"] / max(1, seg_bars) > float(rb.get("angle_ref", 5.0))
    near_pts = float(rb.get("near_pts", 5.0))
    extreme = seg["endPrice"]  # 形成段极值（延伸中末笔的端点价）
    is_buy_anchor = t_.endswith("买")
    is_first = t_ in ("1买", "1卖")

    def _near_zs():
        """形成段极值是否临近锚点前最近中枢的 ZG/ZD 任一边界。"""
        zss = []
        try:
            if upperBis and len(upperBis) > 0:
                zss = buildZSByUpper(bis, upperBis, barSec)
            else:
                zss = buildZS(bis, barSec)
        except Exception:
            return False
        before = [z for z in zss if z.get("startTime") is not None
                  and z["startTime"] <= p["time"]]
        if not before:
            return False
        z = before[-1]
        return min(abs(extreme - z["zg"]), abs(extreme - z["zd"])) <= near_pts

    def _prev_extreme(want_type):
        """锚点前最近一个 want_type 笔的端点价（前低=down 笔端点 / 前高=up 笔端点）。"""
        cands = [b for b in bis[:-1] if b["type"] == want_type
                 and b["endTime"] <= p["time"]]
        return cands[-1]["endPrice"] if cands else None

    if is_first:
        # ===== 1类锚点 =====
        if is_buy_anchor:
            if seg_type == "up":
                if not enough:
                    return "long", f"{name}1买进行中"
                if _near_zs():
                    return (None, f"{name}1买近中枢观望") if angle_strong \
                        else ("short", f"{name}1买转空预期")
                if angle_strong:
                    return "long", f"{name}1买进行中"
                return None, f"{name}1买后方向不明"
            # 形成段向下（回调）
            if seg["startTime"] <= p["time"]:
                return "long", f"{name}1买进行中"  # 上涨未开始（当前下跌笔起点不晚于买点）
            if not enough:
                return "short", f"{name}1买回调"
            return ("short", f"{name}1买强回") if angle_strong \
                else ("long", f"{name}2买预期")
        # 1卖锚点
        if seg_type == "down":
            if not enough:
                return "short", f"{name}1卖进行中"
            if _near_zs():
                return (None, f"{name}1卖近中枢观望") if angle_strong \
                    else ("long", f"{name}1卖转多预期")
            if angle_strong:
                return "short", f"{name}1卖进行中"
            return None, f"{name}1卖后方向不明"
        # 形成段向上（反弹）
        if seg["startTime"] <= p["time"]:
            return "short", f"{name}1卖进行中"  # 下跌未开始（当前上涨笔起点不晚于卖点）
        if not enough:
            return "long", f"{name}1卖反弹"
        return ("long", f"{name}1卖强反") if angle_strong \
            else ("short", f"{name}2卖预期")

    # ===== 2/3 类锚点（2买/类2买/3买/类3买、2卖/类2卖/3卖/类3卖）=====
    if is_buy_anchor:
        if seg_type == "down":
            return None, f"{name}{t_}回调中"  # 图未覆盖，默认观望（2026-09-17 用户确认）
        prev_high = _prev_extreme("up")
        if prev_high is not None and abs(extreme - prev_high) <= near_pts:
            return None, f"{name}{t_}前高附近"
        if not enough:
            return "long", f"{name}{t_}后上涨"
        return ("long", f"{name}{t_}后上涨") if angle_strong \
            else (None, f"{name}{t_}后方向不明")
    # 卖类锚点
    if seg_type == "up":
        return None, f"{name}{t_}反弹中"  # 图未覆盖，默认观望（2026-09-17 用户确认）
    prev_low = _prev_extreme("down")
    if prev_low is not None and abs(extreme - prev_low) <= near_pts:
        return None, f"{name}{t_}前低附近"
    if not enough:
        return "short", f"{name}{t_}后下跌"
    return ("short", f"{name}{t_}后下跌") if angle_strong \
        else (None, f"{name}{t_}后方向不明")


def trend_direction(res, bis, bars, upperBis, macdArr, tCut=None, rebound=None):
    """参考周期方向判定（顺势过滤的唯一口径；mark_entry 进场方向过滤消费）。

    规则（2026-09-15 与用户逐条确认；7 为 2026-09-17 相位树更新）：
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
      6. 笔数据不足（<2 笔）→ (None, "")，消费方不过滤；
      7. 相位树（rebound 提供且 enabled；闩锁 4 优先——未触发才走到这里）：
         锚点确立后按「形成段方向 → 够笔 → 位置（中枢上/下沿、前低/前高）→
         角度强弱」分相位，可返回观望 (None, reason)（消费方双向放行、信号仍附
         注记）；规则全文见 _phase_direction 与 spec/plans/SPEC_trend_rebound_phase.md。
         rebound 未提供 / enabled=False → 退回旧行为（确立方向锁到闩锁/新点）。

    @returns (dir, reason)：dir ∈ "long"/"short"/None；reason 展示用，
              如 "4小时2卖预期"（相位）、"4小时下跌延续"（破坏闩锁）、
              "4小时末笔向上"（回退）；观望态 dir=None 但 reason 非空；
              数据不足时 (None, "")。
    """
    name = trend_res_name(res)
    if bis and not bis[-1].get("_contextReady"):
        ctx = buildStructureContext(bis, bars, intervalSecOf(res), tCut)
        bis = ctx["bis"]
        if tCut is not None:
            bars = [b for b in (bars or []) if b["time"] + intervalSecOf(res) <= tCut]
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
    if bis[-1].get("_forming"):
        fallback = ("long" if bis[-1]["type"] == "up" else "short",
                    f"{name}{'预期' if bis[-1]['phase'] == 'expected' else '够笔'}"
                    f"{'上涨' if bis[-1]['type'] == 'up' else '下跌'}")
    if not pts:
        return fallback
    p = pts[-1]
    t_ = p["type"]
    is_buy = t_ in ("1买", "2买", "类2买", "3买", "类3买")
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
    # 相位树（2026-09-17 图片决策树；闩锁优先——上面循环未返回即未触发）：
    # 1类与非1类锚点都走；关闭（rebound 未提供 / enabled=False）→ 旧行为确立返回
    if rebound and rebound.get("enabled"):
        direction, reason = _phase_direction(name, p, t_, bis, bars, upperBis, barSec, rebound, tCut)
        if bis[-1].get("phase") == "expected":
            reason += "（预期段）"
        return direction, reason
    return ("long" if is_buy else "short"), f"{name}{t_}"


def trend_state_of(periodBis, barsByPeriod, trend_res, periodMacd=None, work_cache=None,
                   cfg=None):
    """按配置计算参考周期方向状态（mark_entry 顺势过滤统一入口）。

    上级周期取法与 mark_entry.upperResOf 同口径：periodBis 中比参考周期大一级的
    最小周期（240→D），供 findBuyPoints 区间套使用。

    @param trend_res 参考周期（"240"/"D"；""/None = 关闭 → 返回 None）
    @param work_cache 可选：跨次调用复用的 dict（与 compute_plan 同一引擎级缓存；
             参考周期/上级笔指纹、bars/MACD 长度、相位参数未变时直接复用——参考周期
             输入只在其收盘或笔结构变化时才变，两次收盘间（约 80 根 fine）全命中）。
             重同步后由调用方清空（中段笔修正可能指纹漏判）。
    @param cfg 参数中心 plan 模块参数（trendRebound/reboundNearPts/reboundAngleRef；
             None → 模块常量默认，相位判定默认开启）。
    @returns {"dir": "long"/"short"/None, "reason": str, "res": 参考周期}；
             参考周期无笔数据时 dir=None（不过滤，规则 6）。检测周期的结构性剔除
             由 mark_entry 按 trend_res 字符串独立执行，与本状态无关。
    """
    if not trend_res:
        return None
    from .sr_flip import _bis_fingerprint
    tr = str(trend_res).upper()
    periodBis = structurePeriods(periodBis or {}, barsByPeriod or {}, work_cache=work_cache)
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
    # 相位判定参数组装（cfg 缺省 → 模块常量默认；trendRebound=False → 关闭退旧行为）
    rebound = None
    if cfg is None or cfg.get("trendRebound", TREND_REBOUND):
        rebound = {
            "enabled": True,
            "near_pts": float(cfg.get("reboundNearPts", REBOUND_NEAR_PTS)) if cfg
                        else REBOUND_NEAR_PTS,
            "angle_ref": float(cfg.get("reboundAngleRef", REBOUND_ANGLE_REF)) if cfg
                         else REBOUND_ANGLE_REF,
            "min_bars": int(CHAN_CFG.get("expectBiMinBars", 5) or 5),
        }
    rb_key = None
    if rebound:
        rb_key = (rebound["near_pts"], rebound["angle_ref"], rebound["min_bars"])
    if work_cache is not None:
        key = ("trend", tr, _bis_fingerprint(bis), _bis_fingerprint(upper),
               len(bars), len(macdArr) if macdArr is not None else len(bars), rb_key,
               bis[-1].get("phase"), bis[-1].get("mergedCount"))
        ent = work_cache.get(("trend", tr))
        if ent is not None and ent[0] == key:
            return ent[1]
    if macdArr is None:
        macdArr = calcMACD(bars)
    d, reason = trend_direction(tr, bis, bars, upper, macdArr, rebound=rebound)
    row = {"dir": d, "reason": reason, "res": tr}
    if work_cache is not None:
        work_cache[("trend", tr)] = (key, row)
    return row
