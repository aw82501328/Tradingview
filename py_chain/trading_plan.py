# -*- coding: utf-8 -*-
"""
交易计划逻辑（Python 移植版，与 .cursor/skills/trading-plan/scripts/trading_plan.js 对齐）

纯函数模块。2026-10-16 起只保留两项判定（开单链路改为 mark_entry 直接消费
chan_core 买卖点，本模块不再产出方向/策略行——原 strategyOf 三档路由、
classifySecond 四态、双侧锚 predictPlan/compute_plan 已删除）：
  1. 震荡判定：isRangeBound/_rangeVerdict → 参考周期 regime（compute_regime），
     供 mark_entry 震荡闸（默认关闭 range_res=""）；
  2. 顺势方向判定：trend_direction/trend_state_of（mark_entry 顺势过滤消费，
     默认关闭 trend_res=""）。

说明：震荡判定 isRangeBound 复制自 chan-status SKILL（.cursor/skills/chan-status/scripts/chan_status.js），
与 JS 版一致保持原逻辑不变。

不连接 CDP、不绘图；回测链路通过 compute_regime/trend_state_of 直接调用。
"""

from .chan_core import (
    calcATR, calcMACD, intervalSecOf, CHAN_CFG,
    findBuyPoints, findSellPoints, buildZS, buildZSByUpper,
    markWickBars, mergeBars, findFractals,
)

from .chan_core import structurePeriods, buildStructureContext, mergedSegmentCount, lowerStrokePack


def _stroke_chain(res_chain):
    """收笔链 loaded 集（2026-10-23 双周期链）：链周期去掉 30S——秒级不参与收笔
    背驰，与 chan_core.structurePeriods 的收笔链口径一致（旧口径 3 的收笔下级恒空）。"""
    if res_chain is None:
        return None
    return [p for p in res_chain if str(p).upper() != "30S"]

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


def _rangeVerdict(bis, bars, atr, upperBis, lastPrice, barSec, range_cfg):
    """A/B 两支震荡判定（本周期自身判定与参考周期 regime 复用）。
    @returns 震荡判定 dict（{"reason": …}）或 None（非震荡）。"""
    cfg = range_cfg or RANGE_DEFAULTS
    # A 支：isRangeBound 横盘判定（range_cfg 来自参数中心逐键覆盖；rangeBoundOn 关闭则跳过）
    if cfg.get("rangeBoundOn", RANGE_DEFAULTS["rangeBoundOn"]):
        rb = isRangeBound(bis, bars, atr, cfg)
        if rb and rb["range"]:
            reason = (f"最近 {rb['rangeBarN']} 根K线区间 {rb['kSpan']:.2f}（{rb['kAtr']:.1f}×ATR）"
                      + (f"，笔端点区间 {rb['biSpan']:.2f}（{rb['biAtr']:.1f}×ATR），涨跌交替无明确方向"
                         if rb["winBiCount"] > 0 else "，窗口内无笔")
                      + "，判定为震荡整理")
            return {"reason": reason}

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
        return {"reason": reason}
    return None


def _range_gate_row(res, bis, bars, atr, upperBis, lastPrice, barSec, range_cfg, lower_stroke=None):
    """参考周期震荡 regime（原 _plan_gate_row；供更低周期震荡闸）。
    @returns {"range": True, "resName": "4小时", "reason": "4小时：…"} /
             {"range": False, ...} /
             {"range": True, "insufficient": True, ...}——笔数 <2：更低周期直接观望（数据不足）。"""
    name = trend_res_name(res)
    if bis and not bis[-1].get("_contextReady"):
        ctx = buildStructureContext(bis, bars, intervalSecOf(res), None, lowerStroke=lower_stroke)
        bis = ctx["bis"]
    if not bis or len(bis) < 2:
        return {"range": True, "insufficient": True, "resName": name,
                "reason": f"{name}笔数据不足（少于2笔），无法判定震荡/趋势，观望"}
    v = _rangeVerdict(bis, bars, atr, upperBis, lastPrice, barSec, range_cfg)
    if v is None:
        return {"range": False, "resName": name, "reason": ""}
    return {"range": True, "resName": name, "reason": f"{name}：{v['reason']}"}


# ============================================================
# 汇总计算（供回测链路调用）
# ============================================================


def compute_regime(periodBis, barsByPeriod, periods, periodMacd=None, periodAtr=None, cfg=None,
                   work_cache=None, range_res=None, res_chain=None):
    """震荡 regime 判定（交易计划保留的「震荡」职能；原 compute_plan 的闸门部分抽出）。

    @param range_res  震荡判定参考周期（None → RANGE_RES；"" = 关闭 → 返回 None；
                      "240"/"D" = 开启）。
                      开启时：严格更低的检测周期 regime 震荡/笔不足 → mark_entry 观望
                      （range_watch 闸）；参考周期自身与更高周期由 mark_entry 按
                      range_res 结构性剔除（res_ge_range），与原计划行为一致。
    @returns None（关闭）或参考周期 regime 行：
             {"range": bool, "insufficient": bool, "resName": str, "reason": str}
    """
    from .sr_flip import _bis_fingerprint
    if range_res is None:
        range_res = RANGE_RES
    if not range_res:
        return None
    periodBis = structurePeriods(periodBis or {}, barsByPeriod or {}, work_cache=work_cache,
                                 res_chain=res_chain)
    ref = next((r for r in periods if str(r).upper() == str(range_res).upper()), None)
    curBis = (periodBis.get(ref) or []) if ref is not None else []
    name = trend_res_name(range_res)
    if not curBis:
        # 参考周期整行无数据——预置不足闸（原 compute_plan 预置口径）
        return {"range": True, "insufficient": True, "resName": name,
                "reason": f"{name}笔数据不足（少于2笔），无法判定震荡/趋势，观望"}
    # 上级周期取法与 trend_state_of 同口径：比参考周期大一级的最小周期
    sec = intervalSecOf(ref) or 0
    upper = None
    best = None
    for r in periodBis:
        s = intervalSecOf(r) or 0
        if s > sec and (best is None or s < (intervalSecOf(best) or 0)):
            best = r
    if best is not None:
        upper = periodBis.get(best)
    rawBars = barsByPeriod.get(ref) or []
    atr = (periodAtr or {}).get(ref)
    if atr is None:
        atr = calcATR(rawBars, 14)
    lastBar = rawBars[-1] if rawBars else None
    lastPrice = lastBar["close"] if lastBar else None
    # 震荡判定只看最近 rangeBarN 根；截尾与全量在该窗口内逐位一致
    range_n = (cfg or RANGE_DEFAULTS).get("rangeBarN", RANGE_DEFAULTS["rangeBarN"])
    range_bars = rawBars[-range_n:] if rawBars and len(rawBars) > range_n else rawBars
    cfg_fp = tuple(sorted((cfg or {}).items())) if cfg else ()
    gate_key = ("rangeGate", ref, _bis_fingerprint(curBis), len(rawBars),
                lastPrice, atr, _bis_fingerprint(upper), cfg_fp)
    if work_cache is not None:
        gate_ent = work_cache.get(("rangeGate", ref))
        if gate_ent is not None and gate_ent[0] == gate_key:
            return gate_ent[1]
    gate = _range_gate_row(ref, curBis, range_bars, atr, upper, lastPrice,
                           intervalSecOf(ref), cfg,
                           lower_stroke=lowerStrokePack(ref, periodBis, barsByPeriod,
                                                        loaded=_stroke_chain(res_chain)))
    if work_cache is not None:
        work_cache[("rangeGate", ref)] = (gate_key, gate)
    return gate


# ============================================================
# 纯函数：参考周期方向判定（顺势过滤，2026-09-15 口径与用户逐条确认）
# ============================================================

# 顺势参考周期默认值："" = 关闭（2026-10-10 黄金参数页生效值收成全局默认）；
# "240" = 4小时；"D" = 日线
# （参数中心 plan 模块 trendRes，Web 交易计划页签可配；mark_entry 进场方向过滤消费）
TREND_RES = ""

# 震荡判定参考周期（2026-09-16 闸门口径；2026-10-08 增加「关闭」项，参数中心 plan 模块
# rangeRes 可配）："" = 关闭（默认，2026-10-10）→ 无参考周期闸门。
# 2026-10-12：关闭后任何周期（含 4 小时、日线）的计划行都不自判震荡，直接走买卖点
# （与图表 JS trading_plan.js 同口径）；A/B 仅在参考周期开启时用作 regime 门。
# 2026-10-16：交易计划策略行取消，本开关只控制 mark_entry 的震荡观望闸（compute_regime）。
# "240" = 4小时；"D" = 日线。
RANGE_RES = ""

# 方向相位判定（2026-09-17 与用户确认，图片决策树；参数中心 plan 模块可配）：
# 锚点确立后、破坏闩锁未触发期间，按「形成段方向 → 够笔 → 位置 → 角度强弱」分相位，
# 可返回观望（dir=None 带 reason，消费方双向放行）。总表 ①~⑤ 与闩锁优先级不变。
TREND_REBOUND = True    # 相位判定开关（关闭 → 退回旧行为：确立方向锁到闩锁/新点）
REBOUND_NEAR_PTS = 5.0  # 「附近」容差（绝对点数）：形成段极值距中枢上/下沿或前低/前高
REBOUND_ANGLE_REF = 5.0 # 角度 45° 基准（点/根）：当前笔平均每根幅度 > 该值 = 角度>45°（强）
                        # （XAUUSD 4h 量级默认；跨品种需调参）

# 周期 → 中文名（方向成因展示用，如「4小时2买」「日线1卖」）
RES_NAME_CN = {"D": "日线", "240": "4小时", "60": "1小时", "30": "30分钟",
               "15": "15分钟", "5": "5分钟", "3": "3分钟", "1": "1分钟", "30S": "30秒"}


def trend_res_name(res):
    return RES_NAME_CN.get(str(res).upper(), str(res))


def strong_fractal_after(merged, fractals, t, kind):
    """t（含）之后是否出现强分型（kind="bottom"|"top"）。

    强分型只比实体、不含影线：右肩收盘价与左肩开盘价比较。
    底分型：右肩收盘 > 左肩开盘。顶分型：右肩收盘 < 左肩开盘。
    合并K/分型用 chan_core 现有链（markWickBars → mergeBars → findFractals，与 buildBi 同源）。
    """
    for f in fractals:
        if f["type"] != kind or f["time"] < t:
            continue
        i = f["mergedIdx"]
        if i - 1 < 0 or i + 1 >= len(merged):
            continue  # 左/右肩不完整（尾部形成中）
        left, right = merged[i - 1], merged[i + 1]
        if kind == "bottom" and right["close"] > left["open"]:
            return True
        if kind == "top" and right["close"] < left["open"]:
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


def trend_direction(res, bis, bars, upperBis, macdArr, tCut=None, rebound=None, lower_stroke=None,
                    upperMacd=None):
    """参考周期方向判定（顺势过滤的唯一口径；mark_entry 进场方向过滤消费）。

    规则（2026-09-15 与用户逐条确认；7 为 2026-09-17 相位树更新）：
      1. 取参考周期最近一个买卖点（findBuyPoints/findSellPoints，最近 60 笔窗口）；
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
        ctx = buildStructureContext(bis, bars, intervalSecOf(res), tCut, lowerStroke=lower_stroke)
        bis = ctx["bis"]
        if tCut is not None:
            bars = [b for b in (bars or []) if b["time"] + intervalSecOf(res) <= tCut]
    if not bis or len(bis) < 2:
        return None, ""
    win = bis[-60:]  # 只看最新结构
    barSec = intervalSecOf(res)
    try:
        pts = findBuyPoints(win, upperBis, macdArr, barSec, upperMacd=upperMacd) \
            + findSellPoints(win, upperBis, macdArr, barSec, upperMacd=upperMacd)
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
    # 2026-10-10 提前入列（原 anchorUndecidedSkip「未定型回退前锚」淘汰）：
    # 最近点即锚——未定型点也定向（2买→long 等），与计划层弱档先行同口径，
    # 避免顺势过滤回退前锚反向、拦掉刚武装的新方向。
    p = pts[-1]
    t_ = p["type"]
    is_buy = t_ in ("1买", "2买", "类2买", "3买", "类3买", "4买", "类4买")
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
                   cfg=None, res_chain=None):
    """按配置计算参考周期方向状态（mark_entry 顺势过滤统一入口）。

    上级周期取法与 mark_entry.upperResOf 同口径：periodBis 中比参考周期大一级的
    最小周期（240→D），供 findBuyPoints 区间套使用。

    @param trend_res 参考周期（"240"/"D"；""/None = 关闭 → 返回 None）
    @param work_cache 可选：跨次调用复用的 dict（与 compute_regime 同一引擎级缓存；
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
    periodBis = structurePeriods(periodBis or {}, barsByPeriod or {}, work_cache=work_cache,
                                 res_chain=res_chain)
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
    upperMacd = (periodMacd or {}).get(best) if best is not None else None
    if upperMacd is None and best is not None:
        ub = (barsByPeriod or {}).get(best) or []
        if ub:
            upperMacd = calcMACD(ub)
    d, reason = trend_direction(tr, bis, bars, upper, macdArr, rebound=rebound,
                                lower_stroke=lowerStrokePack(tr, periodBis, barsByPeriod,
                                                             loaded=_stroke_chain(res_chain)),
                                upperMacd=upperMacd)
    row = {"dir": d, "reason": reason, "res": tr}
    if work_cache is not None:
        work_cache[("trend", tr)] = (key, row)
    return row
