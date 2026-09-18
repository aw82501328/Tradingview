# -*- coding: utf-8 -*-
"""
缠论算法核心（Python 移植版，与 .cursor/skills/chan-core/scripts/chan_core.js 逐函数对齐）

纯函数模块，不依赖任何行情/交易框架。供回测链路（画笔 → 标记买卖点 → 支阻位 → 交易计划 → 进出场）复用。

与 JS 版约定一致：
  - 笔对象字段：type(up/down)、startIdx/endIdx(合并K线索引)、startTime/endTime(校准后端点时间)、
    startPrice/endPrice、rawCount(覆盖原始K线数)、span(幅度)、gapLocked(跳空成笔)、macdCross(MACD变色成笔)
  - 配置：CHAN_CFG["gapFilter"]（跳空独立成笔阈值，默认 1.0）、CHAN_CFG["debug"]（调试打印）
  - 所有时间均为 Unix 秒（UTC），与 TradingView K线时间一致

本文件以 vnpy/chan_core.py 为基线（保留原有全部逻辑），补齐相对 chan_core.js 缺失的：
  - markWickBars（长影压平 + _topCand/_origLow 旁路，与 JS 对齐）
  - mergeBars 增加 rawHighTime/rawLowTime 字段（端点极值修正 fixBiExtremes 用），
    并传播 _topCand/_origLow 旁路字段
  - fixBiExtremes（端点极值修正；底端点含分型中心、_origLow 恢复通道）
  - buildZS / buildZSByUpper（中枢构建，交易计划与进出场判定用）
  - keepRecentEach 增加 keep 参数（与 JS 一致）

用法：
    import chan_core
    chan_core.CHAN_CFG["debug"] = False
    chan_core.CHAN_CFG["gapFilter"] = 1.0
    merged = chan_core.mergeBars(rawBars)
    ...
"""

import bisect
import math
import re
import threading

from collections import OrderedDict

from datetime import datetime

# ============================================================
# 配置
# ============================================================

CHAN_CFG = {
    "gapFilter": 1.0,  # 跳空独立成笔阈值：相邻K线缺口 >= gapFilter*ATR 时强制独立成笔
    "wickRatio": 0.70,  # 长影剔除：影线占整根K线振幅的比例阈值（>= 时视为冲高/探底插针）
    "wickAtrK": 0.5,    # 长影剔除：影线绝对长度下限 = wickAtrK * ATR（窄幅小K线免疫）
    "divergeDurRatio": 3,  # 背驰面积判据的时长可比上限：面积Σ=柱高×K线根数、与区间时长线性相关，
                           # 两段时长比 > 该值时不具可比性，面积项不计入背驰（只用 DIF/柱高判据）
    "nearDoubleAtrK": 0.3,  # 近等双顶/双底平台取后顶/后底：价差与回调深度的 ATR 系数
    "nearDoublePct": 0.001,  # 近等双顶/双底平台取后顶/后底：价差下限（价格比例，与 ATR 项取 max）
    "nearDoubleLowerRelax": 1.5,  # 仅60m：15m双动能确认的容差倍数
    "nearDoubleLowerRatio": 0.5,  # 柱峰值和DIF幅度均不超过前段50%
    # ---- 进场背驰「近等双底二底」扩展（SPEC_divergence_fallback，2026-09-11）----
    # 全量回测对比（XAUUSD 7-20~9-11）：baseline PF 1.78/+483 → M1+rearm PF 2.19/+863，
    # 8-7 08:45 型二底由回退候选捕获（+280.5）；代价为再进场磨损簇（8-14 四连小止损 -68）。
    "sinkFallback": True,        # M1 下沉链回退：停止级无候选时沿链向上一级（仍 < 检测周期）重评
    "sinkFallbackRearm": True,   # M1 配套：同向持仓终局后重置该 (periodX,strategyKey,markRes) 段去重
    "nearEqualAtrK": 0.0,        # M2 创新低/新高近等容差 ATR 系数（<=0 且比例项 <=0 时禁用；
                                  # 实测仅 1 笔且为负贡献，默认关）
    "nearEqualPct": 0.0,         # M2 近等容差价格比例（与 ATR 项取 max；近等带内要求三判据 AND）
    "expectBiEnough": True,      # M4 检测周期预期够笔（固定口径）：末笔方向相反且端点后
                                  # ≥expectBiMinBars 根本级K线即视为回调/反弹中（不等反向分型
                                  # 确认——分型需右邻收盘，固有 1 根本级K线滞后，如 8-6 10:00 顶
                                  # 的下跌笔到 21:00 才可见、23:00 才成笔）
    "expectBiMinBars": 5,        # 预期够笔的K线数门槛（本级原始K线数）
    "divergeConfirm": False,     # M4 背驰进场时机（回测页面可选，默认当下）：True=极值K线
                                  # 右邻K收盘（分型可见最早时刻的代理）后的下一根 fine 开盘成交
    "macdZeroTol": 5.0,          # 2买/2卖 MACD 0 轴容差（2026-09-16）：2买 dif > -tol、
                                  # 2卖 dif < +tol 视为「上/下过 0 轴后回调/反弹未破 0 轴太多」，
                                  # 动能还在（原严格口径 dif>0 / dif<0，tol=0 即回退）
    "debug": False,    # 调试打印（buildBi / 买卖点识别过程）
}

# 默认值快照（参数中心 param_center 的默认值单一来源；CHAN_CFG 运行期可被 apply_cfg 覆盖）
CHAN_CFG_DEFAULTS = dict(CHAN_CFG)

_cfg_lock = threading.Lock()


def apply_cfg(overrides):
    """应用参数中心覆盖（进程内全局生效）：只接受 CHAN_CFG_DEFAULTS 已有的键，
    值按默认值类型校验（bool 严格、int/float 数值化），未知键忽略。
    @returns 实际应用的 {key: value}（过滤+校验后）
    """
    applied = {}
    with _cfg_lock:
        for k, v in (overrides or {}).items():
            if k not in CHAN_CFG_DEFAULTS:
                continue
            dv = CHAN_CFG_DEFAULTS[k]
            if isinstance(dv, bool):
                if not isinstance(v, bool):
                    continue
            elif isinstance(dv, int) and not isinstance(dv, bool):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            elif isinstance(dv, float):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
            CHAN_CFG[k] = v
            applied[k] = v
    return applied


def reset_cfg():
    """恢复全部默认值（参数中心「恢复默认」用）。"""
    with _cfg_lock:
        CHAN_CFG.clear()
        CHAN_CFG.update(CHAN_CFG_DEFAULTS)


def active_overrides():
    """当前与默认不同的键（用于落盘 overrides-only 存储）。"""
    with _cfg_lock:
        return {k: v for k, v in CHAN_CFG.items()
                if k in CHAN_CFG_DEFAULTS and CHAN_CFG_DEFAULTS[k] != v}

# ============================================================
# 0. 长影线标记（冲高/探底插针：影线可成端点、不参与区间竞争）
# ============================================================


def markWickBars(rawBars):
    """长影线处理（冲高插针，压平 + 端点候选价），与 JS markWickBars 逐行为对齐：

    影线占比 >= wickRatio 且 >= wickAtrK*稳定ATR 的长上影K线一律压平 high 至实体顶
    （保持历史验收的合并/笔结构——避免影线价参与合并改变结构或污染笔区间），但：
    若该 bar 的 low 不低于左右相邻原始K线低点（压平会消灭一个本可成立的顶分型中心），
    记 `_topCand = 原 high`——findFractals 在该 bar（或其合并 bar）成为顶分型中心时
    用影线价作端点价，结构本身保持压平版。
    反之（low 条件不满足）→ 纯压平：插针本就不成顶分型，影线价不出现。
    长下影（探底插针）：low 压平至实体底（结构/区间竞争保持压平语义），压平前把原低
    记入 `_origLow`/`_origLowTime`，经 mergeBars 传播，由 fixBiExtremes 恢复为更低的
    真实笔底端点（只进端点恢复通道，不进 rawLow/rawHigh——跳空检测保持压平语义）。
    ATR 用全窗口 TR 均值（而非 calcATR 的尾部 14 根——局部行情急涨会使 ATR 数倍放大，
    长影下限随之漂移，剔除结果随行情抖动）。
    不原地修改，返回处理后的新数组。
    """
    ratio = CHAN_CFG["wickRatio"]
    n = len(rawBars)
    avg_atr = 0.0
    if n > 1:
        s = 0.0
        for i in range(1, n):
            h = rawBars[i]["high"]
            l = rawBars[i]["low"]
            pc = rawBars[i - 1]["close"]
            s += max(h - l, abs(h - pc), abs(l - pc))
        avg_atr = s / (n - 1)
    min_wick = avg_atr * CHAN_CFG["wickAtrK"]
    out = []
    for idx in range(n):
        bar = rawBars[idx]
        b = dict(bar)
        amp = b["high"] - b["low"]
        if amp > 0:
            body_top = max(b["open"], b["close"])
            body_bottom = min(b["open"], b["close"])
            upper = b["high"] - body_top
            lower = body_bottom - b["low"]
            if upper >= ratio * amp and upper >= min_wick:
                # 长上影（冲高插针）：high 压平至实体顶；low 不低于左右相邻原始K线低点时
                # 记 _topCand = 原 high（影线可成端点，仅在该 bar 成为顶分型中心时生效）
                prev_ = rawBars[idx - 1] if idx - 1 >= 0 else None
                next_ = rawBars[idx + 1] if idx + 1 < n else None
                if prev_ is not None and next_ is not None and \
                        b["low"] >= prev_["low"] and b["low"] >= next_["low"]:
                    b["_topCand"] = b["high"]
                b["high"] = body_top
            elif lower >= ratio * amp and lower >= min_wick:
                # 长下影（探底插针）：low 压平至实体底；原低记入 _origLow（端点恢复通道）
                b["_origLow"] = b["low"]
                b["_origLowTime"] = b["time"]
                b["low"] = body_bottom
        out.append(b)
    return out


# ============================================================
# 1. 包含关系处理（合并K线）
# ============================================================


def _mergeStep(merged, direction, bar):
    """处理单根K线的包含合并（mergeBars 的单步逻辑，供增量回测复用）。

    与 mergeBars 逐根处理完全一致：把 bar 并入 merged 尾部，返回新的 direction。
    仅当 merged 为空时返回 (merged, direction)，调用方需自行判断。
    """
    if len(merged) == 0:
        m = dict(bar)
        m["_rawCount"] = 1
        m["_firstTime"] = bar["time"]
        m["highTime"] = bar["time"]
        m["lowTime"] = bar["time"]
        m["rawHigh"] = bar["high"]
        m["rawLow"] = bar["low"]
        m["rawHighTime"] = bar["time"]
        m["rawLowTime"] = bar["time"]
        merged.append(m)
        return merged, direction
    last = merged[-1]
    containUp = bar["high"] >= last["high"] and bar["low"] <= last["low"]
    containDown = bar["high"] <= last["high"] and bar["low"] >= last["low"]
    hasContain = containUp or containDown
    if hasContain:
        d = direction
        if d == 0 and len(merged) >= 2:
            d = 1 if last["high"] >= merged[-2]["high"] else -1
        if d == 0:
            d = 1
        if d == 1:
            if bar["high"] > last["high"]:
                last["high"] = bar["high"]
                last["highTime"] = bar["time"]
            if bar["low"] > last["low"]:
                last["low"] = bar["low"]
                last["lowTime"] = bar["time"]
        else:
            if bar["high"] < last["high"]:
                last["high"] = bar["high"]
                last["highTime"] = bar["time"]
            if bar["low"] < last["low"]:
                last["low"] = bar["low"]
                last["lowTime"] = bar["time"]
        # 记录覆盖原始K线的真实极值范围（跳空检测用，不受合并方向高低取舍影响），
        # 同时记录极值出现的原始K线时间（端点极值修正用，见 fixBiExtremes）
        if bar["high"] > last["rawHigh"]:
            last["rawHigh"] = bar["high"]
            last["rawHighTime"] = bar["time"]
        if bar["low"] < last["rawLow"]:
            last["rawLow"] = bar["low"]
            last["rawLowTime"] = bar["time"]
        # 端点候选价（_topCand）随覆盖范围传播：覆盖范围内「可成顶分型中心」的长影 bar
        # （markWickBars 记 _topCand）的影线价，作为合并 bar 成为顶分型中心时的端点价，
        # 同时记录影线价所在原始K线时间（端点时间用——合并 bar 的 highTime 可能被
        # 抬高的普通 bar 占据，需用 _topCandTime 定位真实冲高 bar）
        tc = bar.get("_topCand")
        if tc is not None and tc > last.get("_topCand", 0):
            last["_topCand"] = tc
            last["_topCandTime"] = bar["time"]
        # 探底插针真低（_origLow）随覆盖范围传播：markWickBars 压平长下影时保留的原低
        # （及所在原始K线时间），供 fixBiExtremes 在笔终点后恢复为更低的真实端点。
        # 只进端点恢复通道，不写入 rawLow/rawHigh——跳空检测与分型结构保持压平语义
        ol = bar.get("_origLow")
        if ol is not None and (last.get("_origLow") is None or ol < last["_origLow"]):
            last["_origLow"] = ol
            last["_origLowTime"] = bar.get("_origLowTime", bar["time"])
        last["_rawCount"] += 1
        last["time"] = bar["time"]
        direction = d
    else:
        direction = 1 if bar["high"] > last["high"] else -1
        m = dict(bar)
        m["_rawCount"] = 1
        m["_firstTime"] = bar["time"]
        m["highTime"] = bar["time"]
        m["lowTime"] = bar["time"]
        m["rawHigh"] = bar["high"]
        m["rawLow"] = bar["low"]
        m["rawHighTime"] = bar["time"]
        m["rawLowTime"] = bar["time"]
        merged.append(m)
    return merged, direction


def mergeBars(rawBars):
    """包含关系处理（合并K线）。与 JS 版 mergeBars 对齐。"""
    merged = []
    direction = 0
    for bar in rawBars:
        merged, direction = _mergeStep(merged, direction, bar)
    return merged


# ============================================================
# 2. 分型识别
# ============================================================


def fractalAt(merged, i):
    """判定第 i 根合并K线是否为分型（顶/底分型）。与 findFractals 单点判定一致。

    顶分型端点价：覆盖范围内若含「可成顶分型的长影 bar」（markWickBars _topCand），
    顶分型价用其影线价——结构保持压平版，影线价只在该 bar 成为分型中心端点时生效；
    端点时间用影线价所在原始K线时间（_topCandTime，缺省回落 highTime）。"""
    if i < 1 or i >= len(merged) - 1:
        return None
    prev = merged[i - 1]
    cur = merged[i]
    nxt = merged[i + 1]
    if cur["high"] > prev["high"] and cur["high"] > nxt["high"] and cur["low"] > prev["low"] and cur["low"] > nxt["low"]:
        use_cand = cur.get("_topCand") is not None and cur["_topCand"] > cur["high"]
        return {
            "mergedIdx": i, "type": "top",
            "high": cur["_topCand"] if use_cand else cur["high"],
            "low": cur["low"],
            "time": cur["_topCandTime"] if (use_cand and cur.get("_topCandTime") is not None) else cur["highTime"],
        }
    if cur["low"] < prev["low"] and cur["low"] < nxt["low"] and cur["high"] < prev["high"] and cur["high"] < nxt["high"]:
        return {"mergedIdx": i, "type": "bottom", "high": cur["high"], "low": cur["low"], "time": cur["lowTime"]}
    return None


def mergedSegmentCount(merged, startTime, barSec=0):
    """Count merged blocks from the block containing a calibrated endpoint (inclusive).

    Input must be the closed prefix at the decision time, never a full-history merge.
    A block covers [_firstTime, time + barSec); the containing block counts once.
    """
    if not merged:
        return 0
    lo, hi = 0, len(merged)
    while lo < hi:
        mid = (lo + hi) // 2
        before = merged[mid]["time"] + barSec <= startTime if barSec else merged[mid]["time"] < startTime
        if before:
            lo = mid + 1
        else:
            hi = mid
    if lo == len(merged) or startTime < merged[lo].get("_firstTime", merged[lo]["time"]):
        return 0
    return len(merged) - lo


def confirmedStructureBis(bis):
    return [b for b in (bis or []) if not b.get("_forming")]


def pointEligibleBis(bis):
    # A prospective segment can contain child points immediately, but cannot itself
    # create a second/third point until its own merged length is sufficient.
    return [b for b in (bis or []) if not b.get("_forming") or b.get("enough")]


def buildStructureContext(bis, bars, barSec, tCut=None, merged=None, fractals=None, lowerContext=None):
    """Pure closed-prefix structure: confirmed strokes + at most one prospective leg.

    Passing tCut also accepts full raw history: rebuild strokes if future bars were
    removed. Callers with authoritative prefix strokes can pass their merged/fractal
    state to avoid rebuilding it. The prospective endpoint is never a confirmed pivot.
    """
    raw = bars or []
    known = confirmedStructureBis(bis)
    if tCut is not None and raw and raw[-1]["time"] + barSec > tCut:
        raw = [b for b in raw if b["time"] + barSec <= tCut]
        merged = mergeBars(markWickBars(raw))
        fractals = findFractals(merged)
        known = buildBi(fractals, merged, calcATR(raw), calcMACD(raw), None, barSec >= 3600, lowerContext)
        known = fixBiExtremes(known, merged) or known
        known = extendLastBi(known, markWickBars(raw))
    if merged is None:
        merged = mergeBars(markWickBars(raw))
    cutoff = tCut if tCut is not None else (raw[-1]["time"] + barSec if raw else 0)
    result = {"confirmedBis": known, "bis": list(known), "current": None,
              "merged": merged, "cutoff": cutoff}
    if not known or not raw or not merged:
        return result
    last = known[-1]
    current = dict(last, phase="confirmed", _contextReady=True, coverageEnd=cutoff,
                   mergedCount=mergedSegmentCount(merged, last["startTime"], barSec), enough=True)
    # Match the actual endpoint's merged block, not any earlier bottom/top.  Time
    # containment handles lower-period endpoint calibration and recovered wick lows.
    count = mergedSegmentCount(merged, last["endTime"], barSec)
    idx = len(merged) - count if count else -1
    fs = fractals if fractals is not None else findFractals(merged)
    kind = "bottom" if last["type"] == "down" else "top"
    endpoint = next((f for f in fs if f["mergedIdx"] == idx and f["type"] == kind), None)
    after = [b for b in raw if b["time"] + barSec > last["endTime"]]
    if endpoint is not None and after:
        broken = any(b["low"] < last["endPrice"] - 1e-8 for b in after) if kind == "bottom" \
            else any(b["high"] > last["endPrice"] + 1e-8 for b in after)
        future = [b for b in raw if b["time"] > merged[idx]["time"]]
        if not broken and future:
            field = "high" if kind == "bottom" else "low"
            extreme = (max if kind == "bottom" else min)(future, key=lambda b: b[field])
            price = extreme[field]
            if (price > last["endPrice"] if kind == "bottom" else price < last["endPrice"]):
                current = {"type": "up" if kind == "bottom" else "down",
                           "startTime": last["endTime"], "startPrice": last["endPrice"],
                           "endTime": extreme["time"], "endPrice": price,
                           "span": abs(price - last["endPrice"]), "_forming": True,
                           "_contextReady": True, "mergedCount": count,
                           "enough": count >= 5, "phase": "running" if count >= 5 else "expected",
                           "coverageEnd": cutoff}
                result["bis"].append(current)
    if not current.get("_forming"):
        result["bis"][-1] = current
    result["current"] = current
    return result


def structurePeriods(periodBis, barsByPeriod, tCut=None, mergedByPeriod=None,
                     fractalsByPeriod=None, work_cache=None):
    """Prepare per-period structural views; cache only on closed-input changes.

    Coverage advances every decision even when the upper period has not closed.
    The caller invalidates work_cache on historical corrections/resynchronization.
    """
    if tCut is None:
        tCut = max((v[-1]["time"] + intervalSecOf(r) for r, v in barsByPeriod.items() if v), default=0)
    out = {}
    for res, bis in periodBis.items():
        if bis and bis[-1].get("_contextReady"):
            out[res] = bis
            continue
        raw = barsByPeriod.get(res) or []
        tail = raw[-1] if raw else {}
        key = (len(raw), tuple(tail.get(k) for k in ("time", "open", "high", "low", "close")),
               tuple((b["type"], b["startTime"], b["endTime"], b["startPrice"], b["endPrice"]) for b in bis),
               min(tCut, tail.get("time", 0) + intervalSecOf(res)))
        slot = ("structure", res)
        ent = work_cache.get(slot) if work_cache is not None else None
        if ent is not None and ent[0] == key:
            ctx = ent[1]
        else:
            ctx = buildStructureContext(bis, raw, intervalSecOf(res), tCut,
                                        (mergedByPeriod or {}).get(res), (fractalsByPeriod or {}).get(res),
                                        makeBiLowerContext(res, barsByPeriod.get("15") or [], tCut) if str(res) == "60" else None)
            if work_cache is not None:
                work_cache[slot] = (key, ctx)
        view = list(ctx["bis"])
        if view and ctx["current"] is not None:
            view[-1] = dict(view[-1], coverageEnd=tCut)
        out[res] = view
    return out


def findFractals(merged):
    """顶/底分型识别。与 JS 版 findFractals 对齐。"""
    fractals = []
    for i in range(1, len(merged) - 1):
        f = fractalAt(merged, i)
        if f is not None:
            fractals.append(f)
    return fractals


def updateFractalsTail(fractals, merged):
    """增量分型更新：仅在 merged 尾部新增/修改一根合并K线后调用。
    只有倒数第二个索引（n-2）的分型可能变化（其右邻 n-1 可能刚更新），
    之前的索引都已冻结。与 findFractals 在最终 merged 上的结果完全一致。
    **原地**修改：分型按 mergedIdx 升序，仅从尾部弹出 mergedIdx >= n-2 的
    分型（至多几根）再补算 n-2，O(尾部长度) 而非 O(F)——30S 级 F 可达数万，
    每根K线全量过滤会平方级放大。返回入参列表本身。
    """
    n = len(merged)
    if n < 3:
        del fractals[:]
        return fractals
    # 去掉尾部可能变化的分型（mergedIdx >= n-2；升序 → 只从末尾弹出）
    while fractals and fractals[-1]["mergedIdx"] >= n - 2:
        fractals.pop()
    f = fractalAt(merged, n - 2)
    if f is not None:
        fractals.append(f)
    return fractals


# ============================================================
# 3. 笔构建辅助函数
# ============================================================


def countRaw(merged, startIdx, endIdx):
    """统计 (startIdx, endIdx] 覆盖的原始K线数。"""
    t = 0
    for k in range(startIdx + 1, endIdx + 1):
        t += merged[k]["_rawCount"]
    return t


def hasGapBetween(merged, aIdx, bIdx, atr, gapFilter):
    """检测两个分型（合并K线索引区间）之间是否存在跳空缺口。"""
    th = atr * gapFilter
    for i in range(aIdx, bIdx):
        cur = merged[i]
        nxt = merged[i + 1]
        # 用覆盖原始K线的真实极值范围判断跳空，避免合并K线（向下合并压低高点/向上合并抬高低点）
        # 造成「假缺口」：真实原始K线之间若无价格跳空，不应被判为跳空。
        curHigh = cur.get("rawHigh", cur["high"])
        curLow = cur.get("rawLow", cur["low"])
        nextHigh = nxt.get("rawHigh", nxt["high"])
        nextLow = nxt.get("rawLow", nxt["low"])
        gapUp = nextLow - curHigh
        gapDown = curLow - nextHigh
        if gapUp >= th or gapDown >= th:
            return True
    return False


# ============================================================
# 4. 笔构建（交替分型序列 + 回溯替换）
# ============================================================


def makeBiLowerContext(res, bars, cutoff=float('inf'), macd=None):
    if str(res) != '60' or not bars:
        return None
    values = calcMACD(bars) if macd is None else macd
    if len(values) != len(bars) or any(not isinstance(m, dict) or m.get('time') != b['time']
                                      or not isinstance(m.get('macd'), (int, float))
                                      or not isinstance(m.get('dif'), (int, float))
                                      or not math.isfinite(m['macd']) or not math.isfinite(m['dif'])
                                      for m, b in zip(values, bars)):
        return None
    return dict(res='60', bars=bars, times=[b['time'] for b in bars], macd=values, cutoff=cutoff)


def lowerEndpointWeaker(old, end, fractals, context):
    """近等端点补充确认，不修改买卖点的创新极值背驰定义。"""
    if not context or context['res'] != '60' or not context['bars']:
        return False
    bars, times, macd, cutoff = (context[k] for k in ('bars', 'times', 'macd', 'cutoff'))

    def before(f):
        return next((x for x in reversed(fractals)
                     if x['mergedIdx'] < f['mergedIdx'] and x['type'] != f['type']), None)

    def exact(f):
        if not f or not times or times[0] > f['time']:
            return None
        field = 'high' if f['type'] == 'top' else 'low'
        i = bisect.bisect_left(times, f['time'])
        while i < len(times) and times[i] < f['time'] + 3600:
            if times[i] + 900 > cutoff:
                break
            if abs(bars[i][field] - f[field]) <= 0.001:
                return times[i]
            i += 1
        return None

    t = [exact(before(old)), exact(old), exact(before(end)), exact(end)]
    if any(x is None for x in t) or t[0] >= t[1] or t[2] >= t[3]:
        return False
    a = biMacdMetrics(dict(startTime=t[0], endTime=t[1]), macd)
    b = biMacdMetrics(dict(startTime=t[2], endTime=t[3]), macd)
    top = end['type'] == 'top'
    peak, dif, sign = ('redMax', 'difHigh', 1) if top else ('greenMax', 'difLow', -1)
    ratio = CHAN_CFG['nearDoubleLowerRatio']
    return bool(a and b and a[peak] > 0 and b[peak] > 0 and a[dif] * sign > 0 and b[dif] * sign > 0
                and b[peak] <= a[peak] * ratio and abs(b[dif]) <= abs(a[dif]) * ratio)


def _stkCons(k, prev):
    """阶段二结果的不可变栈节点 (elem, prev, depth)；空栈为 None。
    旧节点永不改动 → 任一位置的栈头即该位置快照（增量续算的基础）。"""
    return (k, prev, (prev[2] + 1) if prev is not None else 1)


def _stkLen(head):
    return head[2] if head is not None else 0


def biListFromHead(head):
    """栈头 → 元素列表（左侧为栈底）。"""
    out = []
    while head is not None:
        out.append(head[0])
        head = head[1]
    out.reverse()
    return out


def biSeqStep(seq, f):
    """阶段一同型合并单步：f 与 seq 末元素同型时保留更极端者，否则追加。
    （buildBi 批量与 bi_inc 增量构建共用，保证单一算法源。）"""
    if len(seq) == 0:
        seq.append(f)
        return
    last = seq[-1]
    if f["type"] == last["type"]:
        if f["type"] == "top":
            if f["high"] >= last["high"]:
                seq[-1] = f
        else:
            if f["low"] <= last["low"]:
                seq[-1] = f
    else:
        seq.append(f)


class BiBuildCtx:
    """笔构建阶段二的规则上下文（buildBi 批量与 bi_inc 增量共用同一规则源）。

    merged/atr/macdArr/lockedPivots/nearDouble/lowerContext 与 buildBi 形参同义。
    gapDiffs：可选的逐对跳空差值表（下标 p → (nextLow-curHigh, curLow-nextHigh)，
    raw 口径），提供时 gapBetween 只扫描 [a,b) 区间而不建全量计数前缀——增量路径
    专用，判定谓词与批量口径逐对一致（块一旦不是末块即不可变，差值与全量构建
    时计算的相同）。rawCounter：可选的 (a,b)→原始K线数回调（前缀和 O(1) 查询）。"""

    def __init__(self, merged, atr, macdArr, lockedPivots=None, nearDouble=False,
                 lowerContext=None, fractals=None, gapDiffs=None, rawCounter=None):
        self.merged = merged
        self.atr = atr
        self.macdArr = macdArr
        self.lockedPivots = lockedPivots
        self.nearDouble = nearDouble
        self.lowerContext = lowerContext
        self.fractals = fractals
        self.gapDiffs = gapDiffs
        self.rawCounter = rawCounter
        self.gapThreshold = atr * CHAN_CFG["gapFilter"] if atr else 0
        self._gapCounts = None

    def gapBetween(self, a, b):
        if a >= b:
            return False
        if self.gapDiffs is not None:
            th = self.gapThreshold
            diffs = self.gapDiffs
            for i in range(a, b):
                up, dn = diffs[i]
                if up >= th or dn >= th:
                    return True
            return False
        # ATR 在一次构建内固定，跳空判定只取决于相邻合并K线的原始极值。
        # 首次需要时建立计数前缀，后续任意 [a,b) 区间直接查询。
        # 不跨 buildBi 调用缓存，避免 ATR 改变或包含合并回写导致过期。
        if self._gapCounts is None:
            gc = [0]
            remaining = iter(self.merged)
            previous = next(remaining)
            prevHigh = previous.get("rawHigh", previous["high"])
            prevLow = previous.get("rawLow", previous["low"])
            count = 0
            for current in remaining:
                curHigh = current.get("rawHigh", current["high"])
                curLow = current.get("rawLow", current["low"])
                if curLow - prevHigh >= self.gapThreshold or prevLow - curHigh >= self.gapThreshold:
                    count += 1
                gc.append(count)
                prevHigh, prevLow = curHigh, curLow
            self._gapCounts = gc
        return self._gapCounts[b] != self._gapCounts[a]

    def replacementExtremesClear(self, origin, old, middle, end):
        ceiling = origin["high"] if origin["type"] == "top" else end["high"]
        floor = end["low"] if end["type"] == "bottom" else origin["low"]
        for x in (old, middle):
            if x["high"] > ceiling or x["low"] < floor:
                return False
        for i in range(origin["mergedIdx"] + 1, end["mergedIdx"]):
            if self.merged[i]["high"] > ceiling or self.merged[i]["low"] < floor:
                return False
        return True

    def isValid(self, a, b):
        # 有效笔判断：合并后K线从起点分型到终点分型（含两端分型）至少 5 根即可成笔。
        # gap = b["mergedIdx"] - a["mergedIdx"]，等价于合并K线数 gap+1 >= 5。
        return b["mergedIdx"] - a["mergedIdx"] >= 4

    def noMoreExtremeInside(self, a, b):
        for i in range(a["mergedIdx"] + 1, b["mergedIdx"]):
            if b["type"] == "bottom" and self.merged[i]["low"] < b["low"]:
                return False
            if b["type"] == "top" and self.merged[i]["high"] > b["high"]:
                return False
        return True

    def fractalRangeClear(self, a, b):
        # 分型范围脱离检查（双向，与 JS chan-core 对齐）：一笔的两端分型不能互相"包含"。
        # 起点侧：与段同侧的两根（下跌笔顶起点取 [中心, 右] 的最低——不用左 bar，否则
        #   主升前夜/起涨点的旧低点会错误抬高"必须跌破"的阈值，误杀后续健康反弹；
        #   上涨笔底起点对称取 [左, 中心] 的最高）。
        # 终点侧：分型自身三根范围（防反向吞没）：下跌笔的底分型三根K线最高价不得涨回
        #   起点顶价之上（顶后崩盘 bar 跌回起点之下 = 中继弱反弹，不成笔；中心 bar 的
        #   崩盘低点可能被包含合并抬高，须依赖三根中的右 bar 提供证据）；上涨笔对称。
        merged = self.merged
        i = a["mergedIdx"]
        j = b["mergedIdx"]
        if a["type"] == "top":
            range_low = min(merged[i]["low"], merged[i + 1]["low"])
            range_high = max(merged[i]["high"], merged[i + 1]["high"])
        else:
            range_low = min(merged[i - 1]["low"], merged[i]["low"])
            range_high = max(merged[i - 1]["high"], merged[i]["high"])
        end_low = min(merged[j - 1]["low"], merged[j]["low"], merged[j + 1]["low"])
        end_high = max(merged[j - 1]["high"], merged[j]["high"], merged[j + 1]["high"])
        if a["type"] == "top" and b["type"] == "bottom":
            return b["low"] < range_low and end_high < a["high"]
        if a["type"] == "bottom" and b["type"] == "top":
            return b["high"] > range_high and end_low > a["low"]
        return True

    def countRawBetween(self, a, b):
        if self.rawCounter is not None:
            return self.rawCounter(a, b)
        return countRaw(self.merged, a, b)


def biStep(ctx, head, k):
    """阶段二单步（回溯替换）：把分型 k 并入不可变结果栈 head，返回新 head。
    规则体与原 buildBi 阶段二逐行一致；result[-n] 栈操作映射见 _stkCons 注释。
    近等双顶块只在 ctx.nearDouble 时激活（增量路径用于 30S，恒 False）。"""
    if head is None:
        return _stkCons(k, None)
    last = head[0]
    if k["type"] == last["type"]:
        if last.get("locked", False):
            # locked 端点（上级笔端点，区间套强制对齐）不可被同类型分型替换
            return head
        if not last.get("gapLocked", False):
            if k["type"] == "top":
                if k["high"] >= last["high"]:
                    head = _stkCons(k, head[1])       # result[-1] = k
            else:
                if k["low"] <= last["low"]:
                    head = _stkCons(k, head[1])       # result[-1] = k
        else:
            # 跳空锁定的端点：仅当后续同类型分型「突破」锁定价格时才解锁替换
            if k["type"] == "top":
                if k["high"] > last["high"]:
                    head = _stkCons(k, head[1])       # result[-1] = k
            else:
                if k["low"] < last["low"]:
                    head = _stkCons(k, head[1])       # result[-1] = k
        # 近等双顶/双底平台取后顶/后底（走势终完美；≥60m 周期由调用方开启 nearDouble）：
        #   后顶/后底 k 与前顶/前底 last 近同价（k 略不极端，差 ≤ max(nearDoubleAtrK×ATR,
        #   nearDoublePct×价)），且 last→k 间所有相邻分型间隔 <4（拆不出笔的平台/直拉，
        #   段内无可确认回调结构，走势未完美）；中间确有一次 ≥thr 真实回调。单跳封顶：
        #   被替换端点打 nearDouble 标记，不二次替换（防平台内累积漂移超阈值）。
        if (ctx.nearDouble and not last.get("gapLocked", False) and not k.get("locked", False)
                and not last.get("nearDouble", False)):
            # locked/gapLocked 不参与；macdCross 不豁免（该端点本就是间隔不足靠 MACD 变色
            # 凑出的脆弱顶/底，如 1h 8-31 顶 4464.23，与近等平台取后顶语义一致）
            atr = ctx.atr
            fractals = ctx.fractals
            lowerContext = ctx.lowerContext
            ref_price = last["high"] if k["type"] == "top" else last["low"]
            thr = max(atr * CHAN_CFG["nearDoubleAtrK"], ref_price * CHAN_CFG["nearDoublePct"])
            diff = (last["high"] - k["high"]) if k["type"] == "top" else (k["low"] - last["low"])
            lower_confirmed = (diff > thr and diff <= thr * CHAN_CFG['nearDoubleLowerRelax']
                               and lowerEndpointWeaker(last, k, fractals, lowerContext))
            if diff >= 0 and (diff <= thr or lower_confirmed):
                plateau, pull, prev_f, cnt = True, False, last, 0
                for f in fractals:
                    if f["mergedIdx"] <= last["mergedIdx"] or f["mergedIdx"] >= k["mergedIdx"]:
                        continue
                    cnt += 1
                    if f["mergedIdx"] - prev_f["mergedIdx"] >= 4:
                        plateau = False
                    if k["type"] == "top" and f["type"] == "bottom" and last["high"] - f["low"] >= thr:
                        pull = True
                    if k["type"] == "bottom" and f["type"] == "top" and f["high"] - last["low"] >= thr:
                        pull = True
                    prev_f = f
                if k["mergedIdx"] - prev_f["mergedIdx"] >= 4:
                    plateau = False
                if cnt > 0 and plateau and pull:
                    if CHAN_CFG["debug"]:
                        print(f"[阶段二] 近等双顶/双底平台取后: {k['type']}@{last['mergedIdx']}({ref_price}) -> "
                              f"{k['type']}@{k['mergedIdx']}({k['high'] if k['type']=='top' else k['low']}) "
                              f"（差 {diff:.2f} ≤ {thr * (CHAN_CFG['nearDoubleLowerRelax'] if lower_confirmed else 1):.2f}"
                              f"{'，15m双动能确认' if lower_confirmed else ''}，平台内无成笔结构）")
                    k["nearDouble"] = True  # 单跳封顶
                    head = _stkCons(k, head[1])       # result[-1] = k
        return head
    # 异类型
    # MACD 端点让位必须保住整根候选笔的双向极值（等价允许）。
    # 中间分型可能携带影线端点价，不能只检查合并K线。
    if _stkLen(head) >= 3:
        origin = head[1][1][0]
        prev2 = head[1][0]
        topOne = last
        if prev2.get("macdCross", False) is True and prev2["type"] == k["type"] and \
           not topOne.get("locked", False) and not prev2.get("locked", False) and \
           ((k["type"] == "top" and k["high"] > prev2["high"]) or
            (k["type"] == "bottom" and k["low"] < prev2["low"])) and \
           ctx.replacementExtremesClear(origin, prev2, topOne, k):
            if CHAN_CFG["debug"]:
                print(f"[阶段二] MACD端点让位: {prev2['mergedIdx']} -> {k['mergedIdx']}")
            k["macdCross"] = True
            return _stkCons(k, head[1][1])            # result[-2] = k; pop()
    # 跳空优先
    hasGap = ctx.gapThreshold > 0 and ctx.gapBetween(last["mergedIdx"], k["mergedIdx"])
    if hasGap:
        if CHAN_CFG["debug"]:
            print(f"[阶段二] 跳空成笔: {last['mergedIdx']} -> {k['mergedIdx']}")
        k["gapLocked"] = True
        return _stkCons(k, head)                      # result.append(k)
    # 前顶/前底作废
    if _stkLen(head) >= 3:
        prev3 = head[1][1][0]
        prev2 = head[1][0]
        lastMoreExtremeThanPrev3 = \
            (prev3["type"] == "top" and last["high"] > prev3["high"]) or \
            (prev3["type"] == "bottom" and last["low"] < prev3["low"])
        shallow = True
        if prev2["type"] == "top":
            rise = prev2["high"] - prev3["low"]
            pull = prev2["high"] - last["low"]
            shallow = pull < rise * 0.5
        else:
            drop = prev3["high"] - prev2["low"]
            bounce = last["high"] - prev2["low"]
            shallow = bounce < drop * 0.5
        if prev2["type"] == k["type"] and \
           not ctx.isValid(prev2, last) and \
           not lastMoreExtremeThanPrev3 and \
           shallow and \
           last.get("macdCross", False) is True and last.get("macdRaw", 0) < 5 and \
           not last.get("locked", False) and not prev2.get("locked", False) and \
           ((k["type"] == "top" and k["high"] > prev2["high"]) or
            (k["type"] == "bottom" and k["low"] < prev2["low"])):
            if CHAN_CFG["debug"]:
                print(f"[阶段二] 前顶/前底作废: {prev2['mergedIdx']} 被 {k['mergedIdx']} 突破")
            if prev2.get("macdCross", False) is True:
                k["macdCross"] = True
            return _stkCons(k, head[1][1])            # result[-2] = k; pop()
    if ctx.isValid(last, k) and (ctx.noMoreExtremeInside(last, k) or last.get("gapLocked", False)) and \
       (ctx.fractalRangeClear(last, k) or last.get("gapLocked", False)):
        return _stkCons(k, head)                      # result.append(k)
    elif ctx.isValid(last, k):
        if CHAN_CFG["debug"]:
            print(f"[阶段二] 忽略 k: {k['mergedIdx']}")
        return head
    else:
        # 间隔不足：先检查 last→k 是否满足「合并后只有4根K + 方向性 MACD 变色」成笔。
        # 方向性变色：底到顶(上涨) 柱状体由绿变红；顶到底(下跌) 柱状体由红变绿。
        gap = k["mergedIdx"] - last["mergedIdx"]
        direction = "up" if last["type"] == "bottom" else "down"
        # 只有间隔恰为3才可能走 MACD 成笔；其余间隔无需计算变色。
        macdCross = gap == 3 and bool(ctx.macdArr) and hasMacdCrossBetween(
            ctx.macdArr, ctx.merged, last["mergedIdx"], k["mergedIdx"],
            last["time"], k["time"], direction)
        if gap == 3 and macdCross and ctx.noMoreExtremeInside(last, k):
            if CHAN_CFG["debug"]:
                print(f"[阶段二] MACD变色成笔: {last['mergedIdx']} -> {k['mergedIdx']} (合并4根K, {'绿变红' if direction == 'up' else '红变绿'})")
            k["macdCross"] = True
            k["macdRaw"] = ctx.countRawBetween(last["mergedIdx"], k["mergedIdx"])
            return _stkCons(k, head)                  # result.append(k)
        else:
            if _stkLen(head) >= 2 and head[1][0]["type"] == k["type"]:
                prev = head[1][0]
                moreExtreme = k["high"] >= prev["high"] if k["type"] == "top" else k["low"] <= prev["low"]
                gapPrevLast = last["mergedIdx"] - prev["mergedIdx"]
                if CHAN_CFG["debug"]:
                    print(f"[阶段二] 间隔不足: {k['mergedIdx']} 与 {last['mergedIdx']}, moreExtreme={moreExtreme}, gapPrevLast={gapPrevLast}")
                # 前顶/前底作废原则（缠论，与 JS chan-core 一致）：顶被更高顶突破时，
                # 作废前顶的条件是「前顶右侧是否已有足够K线构成笔」：
                #   prev→last 构成有效笔（间隔>=4 且 笔内无更极值 且 分型范围脱离）
                #   → 前顶有效，保留，不能被更高顶作废（如已走出有效下跌笔后，
                #   更高顶无法与右侧成笔，应作废的是新顶而非前顶）；
                #   仅当 prev→last 不构成有效笔时，更极端的 k 才能顶替 prev。
                prev_last_valid_bi = gapPrevLast >= 4 and \
                    ctx.noMoreExtremeInside(prev, last) and ctx.fractalRangeClear(prev, last)
                # 最小间隔脆弱笔例外：prev→last 虽构成有效笔，但间隔恰为最小值（4，
                # 即刚够 5 根合并K线）且回调/反弹浅（< 前段涨跌幅的 50%）时，该笔
                # 尚未被确认——随后 k 即创更高顶/更低底说明整段仍是同一笔的延伸
                # （缠论：顶被更高顶突破即作废，上涨笔延伸到新极值），prev 应被 k 顶替。
                fragile_minimal = False
                if prev_last_valid_bi and gapPrevLast == 4 and _stkLen(head) >= 3:
                    p3 = head[1][1][0]
                    if prev["type"] == "top":
                        rise = prev["high"] - p3["low"]
                        fragile_minimal = rise > 0 and (prev["high"] - last["low"]) < rise * 0.5
                    else:
                        drop = p3["high"] - prev["low"]
                        fragile_minimal = drop > 0 and (last["high"] - prev["low"]) < drop * 0.5
                    if CHAN_CFG["debug"] and fragile_minimal:
                        print(f"[阶段二] 最小间隔脆弱笔: {'顶' if prev['type']=='top' else '底'}@{prev['mergedIdx']}→"
                              f"{'顶' if last['type']=='top' else '底'}@{last['mergedIdx']} 间隔恰4且回调浅，"
                              f"允许被 {'顶' if k['type']=='top' else '底'}@{k['mergedIdx']} 顶替")
                if moreExtreme and (not prev_last_valid_bi or fragile_minimal):
                    # 回溯替换保护（区间套一致性）：当 last 比更早的同类型分型 result[-3] 更极端时，
                    # last 是笔内真实转折点（如插针低点/插针高点），不能无条件 pop 掉——吞掉会导致
                    # 该笔内部藏着更极值（违反笔内极值原则），且本级别笔端点与上级周期（区间套）不重合。
                    # 此时保留 last 取代 result[-3]，prev 被更高顶/更低底突破而作废移除，
                    # k 与 last 间隔不足、暂不接入，等待后续满足最小间隔的分型成笔。
                    if _stkLen(head) >= 3:
                        prev3 = head[1][1][0]
                        last_is_deeper = (
                            (k["type"] == "top" and last["low"] < prev3["low"])
                            or (k["type"] == "bottom" and last["high"] > prev3["high"])
                        )
                        if last_is_deeper and not prev.get("locked", False) and not prev3.get("locked", False):
                            if CHAN_CFG["debug"]:
                                print(f"[阶段二] 回溯替换保护: {'顶' if last['type']=='top' else '底'}@{last['mergedIdx']} 比 "
                                      f"{'顶' if prev3['type']=='top' else '底'}@{prev3['mergedIdx']} 更极端，保留 last 为端点，作废 prev，暂不接入 k")
                            return _stkCons(last, head[1][1][1])   # result[-3]=last; pop(); pop()
                    if not last.get("locked", False) and not prev.get("locked", False):
                        return _stkCons(k, head[1][1])            # result[-2] = k; pop()
        return head


def biPair(a, b, merged, ctx=None):
    """阶段三两两连笔单步（buildBi 批量与 bi_inc 增量共用）。"""
    startPrice = a["high"] if a["type"] == "top" else a["low"]
    endPrice = b["high"] if b["type"] == "top" else b["low"]
    isUp = b["type"] == "top"
    return {
        "type": "up" if isUp else "down",
        "startIdx": a["mergedIdx"],
        "endIdx": b["mergedIdx"],
        "startTime": a["time"],
        "endTime": b["time"],
        "startPrice": startPrice,
        "endPrice": endPrice,
        "rawCount": ctx.countRawBetween(a["mergedIdx"], b["mergedIdx"]) if ctx is not None
                    else countRaw(merged, a["mergedIdx"], b["mergedIdx"]),
        "span": abs(endPrice - startPrice),
        "gapLocked": b.get("gapLocked", False) is True,
        "macdCross": b.get("macdCross", False) is True,
    }


def buildBi(fractals, merged, atr, macdArr, lockedPivots=None, nearDouble=False, lowerContext=None):
    """笔构建。与 JS 版 buildBi 对齐。lockedPivots 为上级笔端点（区间套强制对齐，优先级最高）；
    nearDouble=True 时启用「近等双顶/双底平台取后顶/后底」（≥60m 周期由调用方开启）。
    lowerContext 为可选15分钟上下文，仅60m补充分支使用；缺省时沿用原阈值。
    内部经 biSeqStep/biStep/biPair 单步组合（与 bi_inc 增量构建器共用规则源）。"""
    ctx = BiBuildCtx(merged, atr, macdArr, lockedPivots=lockedPivots,
                     nearDouble=nearDouble, lowerContext=lowerContext, fractals=fractals)

    # 阶段一：严格交替分型序列
    seq = []
    for f in fractals:
        biSeqStep(seq, f)

    # 区间套强制对齐（优先级最高）：上级笔端点（lockedPivots）必须在下级笔中被保留为端点，
    # 不能被阶段二的任何「移除中间分型」逻辑吞掉。在阶段一序列上标记与上级端点方向/价格一致的分型。
    if lockedPivots:
        for f in seq:
            p = f["high"] if f["type"] == "top" else f["low"]
            for lp in lockedPivots:
                if lp["dir"] == f["type"] and abs(lp["price"] - p) <= 0.001:
                    f["locked"] = True
                    break

    if CHAN_CFG["debug"]:
        def ft(s):
            v = s["high"] if s["type"] == "top" else s["low"]
            return f"{'顶' if s['type']=='top' else '底'}@{s['mergedIdx']}({v})"
        print("[阶段一] 交替分型序列:", " → ".join(ft(s) for s in seq))

    # 阶段二：移除间隔不足的中间分型（回溯替换）
    head = None
    for k in seq:
        head = biStep(ctx, head, k)

    result = biListFromHead(head)
    if CHAN_CFG["debug"]:
        def ft2(s):
            v = s["high"] if s["type"] == "top" else s["low"]
            return f"{'顶' if s['type']=='top' else '底'}@{s['mergedIdx']}({v})"
        print("[阶段二] 结果序列:", " → ".join(ft2(s) for s in result))

    # 阶段三：两两连笔
    bis = []
    for i in range(0, len(result) - 1):
        bis.append(biPair(result[i], result[i + 1], merged, ctx))
    return bis


# ============================================================
# 4.1 端点极值修正
# ============================================================


def fixBiExtremes(bis, merged, count_raw=None):
    """端点极值修正：包含关系合并时（如向上合并取「高高」会把更低的插针低点抬高，
    向下合并取「低低」会把更高的插针高点压低），笔的端点分型可能不是该区域内的真实极值。
    对每笔检查「终点分型之后、下一笔终点分型之前」的合并K线，若存在「被包含合并掩盖」
    （rawLow<low / rawHigh>high）且比当前端点更极端的真实极值，把本笔终点与下一笔起点
    同步平移到该极值所在K线（保持首尾连续）。只处理被掩盖的极值。
    跳空独立成笔（gapLocked）端点固定在缺口处，不参与修正。原地修改并返回 bis。
    count_raw：可选 (a,b)→原始K线数 回调（bi_inc 增量路径传前缀和查询）。"""
    if not bis or len(bis) == 0 or not merged or len(merged) == 0:
        return bis
    if count_raw is None:
        count_raw = lambda a, b: countRaw(merged, a, b)
    eps = 1e-9
    for i in range(len(bis)):
        b = bis[i]
        if b.get("gapLocked", False):
            continue  # 跳空成笔端点固定在缺口处
        if i + 1 >= len(bis):
            continue  # 最后一笔由 extendLastBi 负责延伸
        next_ = bis[i + 1]
        toIdx = next_["endIdx"] - 1  # 不含下一笔终点分型，避免笔退化
        extreme = None
        if b["type"] == "down":
            # 终点是底：从 b.endIdx 起扫（含分型中心）——中心合并K线可能因包含合并/长下影
            # 压平把更低的真低藏在自身 low 之下，仅扫 endIdx 之后会漏掉。
            # 若真低恰在分型中心上（k === endIdx），只改价/时间、idx 不动，笔结构无损。
            # 候选真低 = _origLow（markWickBars 压平的长下影原低）或 rawLow 原值；
            # 分型中心 bar 只认 _origLow（中心可能因向上合并把早于本笔结构的老蜡烛吞入链内，
            # 其 rawLow 未必属于笔底区间，恢复 rawLow 会过度下移）；中心之后两者都认。
            k0 = b["endIdx"]
            if k0 > toIdx:
                continue
            for k in range(k0, toIdx + 1):
                mk = merged[k]
                is_center = k == b["endIdx"]
                cand_low = None
                if is_center:
                    cand_low = mk.get("_origLow")
                elif mk.get("_origLow") is not None:
                    cand_low = mk["_origLow"]
                else:
                    cand_low = mk.get("rawLow")
                if cand_low is None or cand_low >= mk["low"]:
                    continue  # 未被合并/压平掩盖
                if cand_low < b["endPrice"] - eps and (extreme is None or cand_low < extreme["price"]):
                    t = mk["_origLowTime"] if mk.get("_origLowTime") is not None else mk.get("rawLowTime")
                    extreme = {"price": cand_low, "time": t, "idx": k}
        else:
            # 终点是顶：保持 endIdx+1 起扫（上影压平不产生 _origHigh，分型中心自身即端点
            # 价；被包含合并掩盖的更高真实高点走 rawHigh 原值，不会把端点平移回已压平的插针价）
            k0 = b["endIdx"] + 1
            if k0 > toIdx:
                continue
            for k in range(k0, toIdx + 1):
                mk = merged[k]
                if mk.get("rawHigh") is None or mk["rawHigh"] <= mk["high"]:
                    continue  # 未被掩盖
                if mk["rawHigh"] > b["endPrice"] + eps and (extreme is None or mk["rawHigh"] > extreme["price"]):
                    extreme = {"price": mk["rawHigh"], "time": mk["rawHighTime"], "idx": k}
        if extreme is None:
            continue
        if CHAN_CFG["debug"]:
            print(f"[端点极值修正] {'上涨' if b['type']=='up' else '下跌'}笔终点 {b['endPrice']} 平移到更极端 {extreme['price']}@idx={extreme['idx']}")
        # 本笔终点更新
        b["endPrice"] = extreme["price"]
        b["endTime"] = extreme["time"]
        b["endIdx"] = extreme["idx"]
        b["span"] = b["endPrice"] - b["startPrice"] if b["type"] == "up" else b["startPrice"] - b["endPrice"]
        b["rawCount"] = count_raw(b["startIdx"], b["endIdx"])
        # 下一笔起点联动（保持两笔端点连续）
        next_["startPrice"] = extreme["price"]
        next_["startTime"] = extreme["time"]
        next_["startIdx"] = extreme["idx"]
        next_["span"] = next_["endPrice"] - next_["startPrice"] if next_["type"] == "up" else next_["startPrice"] - next_["endPrice"]
        next_["rawCount"] = count_raw(next_["startIdx"], next_["endIdx"])
    return bis


# ============================================================
# 4.2 中枢构建
# ============================================================


def buildZS(bis, barSec=0):
    """构建笔中枢（基于笔序列，标准缠论笔中枢）。
    取连续三笔（笔序列天然交替）的重叠区间构成中枢：
      中枢上沿 ZG = min(三笔高点)，中枢下沿 ZD = max(三笔低点)，ZG > ZD 时成立。
    中枢形成后支持延伸：后续笔与 [ZD, ZG] 有重叠则纳入中枢（GG/DD 扩展），
    出现离开中枢的笔时中枢结束（笔与中枢区间完全无重叠 → 离开；笔的起点在中枢
    区间内、终点突破中枢边界 → 也视为离开）。
    至少 3 笔即可输出中枢（三笔重叠即成）。
    中枢区间 [zd, zg] 取「构成中枢的全部笔（含离开笔）的重叠部分」。
    中枢水平边缘：左边缘 = 进入笔终点 - 5×barSec；右边缘 = 离开笔起点 + 5×barSec；
    无离开笔时右边缘 = 构成中枢最后一笔的终点 + 5×barSec。
    @param bis 笔数组（已排序，含 startTime/endTime/startPrice/endPrice）
    @param barSec 本周期单根K线时长（秒），用于左右各外扩 5 根K线；默认 0 表示不外扩
    @returns 中枢列表 [{ startTime, endTime, zd, zg, dd, gg, biCount, extended, exitTime, enterEndTime, exitStartTime }]
    """
    if not bis or len(bis) < 3:
        return []
    n = len(bis)
    pad = (barSec or 0) * 5  # 左右各外扩 5 根K线（本周期时长）
    hi = lambda b: max(b["startPrice"], b["endPrice"])
    lo = lambda b: min(b["startPrice"], b["endPrice"])
    zss = []
    i = 0
    while i + 2 < n:
        b1, b2, b3 = bis[i], bis[i + 1], bis[i + 2]
        H1, L1 = hi(b1), lo(b1)
        H2, L2 = hi(b2), lo(b2)
        H3, L3 = hi(b3), lo(b3)
        zg = min(H1, H2, H3)
        zd = max(L1, L2, L3)
        if zg > zd:
            # 三笔重叠 → 形成中枢，向后延伸扫描
            j = i + 3
            dd = min(L1, L2, L3)
            gg = max(H1, H2, H3)
            exitTime = None  # 离开中枢的笔的起点时间（若有）
            eps = 1e-9
            while j < n:
                bj = bis[j]
                Hj, Lj = hi(bj), lo(bj)
                if Lj <= zg and Hj >= zd:  # 与中枢区间有重叠 → 判断延伸还是离开
                    # 离开判定：笔的起点在中枢区间内、终点突破中枢边界，视为「离开中枢的笔」
                    startIn = bj["startPrice"] >= zd - eps and bj["startPrice"] <= zg + eps
                    endBreak = bj["endPrice"] < zd - eps or bj["endPrice"] > zg + eps
                    if startIn and endBreak:
                        exitTime = bj["startTime"]  # 离开笔起点
                        break
                    dd = min(dd, Lj)
                    gg = max(gg, Hj)
                    j += 1
                else:
                    exitTime = bj["startTime"]  # 与中枢区间完全无重叠 → 离开中枢
                    break
            # 笔数 = 构成中枢的笔（i..j-1）+ 离开笔（若有 1 笔）
            biCount = (1 if exitTime is not None else 0) + (j - i)
            # 至少 3 笔即可输出中枢（三笔重叠即成）
            if biCount < 3:
                i = j
                continue
            # 中枢区间 = 构成中枢的全部笔（i..i+biCount-1，含离开笔）的重叠部分
            zsZd = float("-inf")
            zsZg = float("inf")
            for k in range(i, i + biCount):
                bk = bis[k]
                zsZd = max(zsZd, lo(bk))
                zsZg = min(zsZg, hi(bk))
            # 全部笔重叠后仍可能 zg <= zd（笔数过多、覆盖区间收窄为空），防御性跳过
            if zsZg <= zsZd:
                i = j
                continue
            # 水平边缘：进入笔 = 三笔重叠形成中枢的第一笔 bis[i]，其终点；
            # 离开笔 = exitTime 的笔（bis[j]），其起点；无离开笔时取构成中枢最后一笔的终点
            enterEndTime = b1["endTime"]  # 进入笔终点（外扩前）
            exitStartTime = exitTime if exitTime is not None else bis[j - 1]["endTime"]  # 离开笔起点（外扩前）
            zss.append({
                "startTime": enterEndTime - pad,       # 左边缘 = 进入笔终点 - 5根K
                "endTime": exitStartTime + pad,        # 右边缘 = 离开笔起点 + 5根K
                "zd": zsZd, "zg": zsZg, "dd": dd, "gg": gg,
                "biCount": biCount,
                "extended": biCount > 3,
                "exitTime": exitTime,                  # 离开中枢的笔的起点时间（记录，供排查）
                "enterEndTime": enterEndTime,          # 进入笔终点（外扩前原始时间）
                "exitStartTime": exitStartTime,        # 离开笔起点（外扩前原始时间）
            })
            i = j  # 从离开中枢的笔开始重新扫描
        else:
            i += 1  # 三笔不重叠，滑窗
    return zss


def buildZSByUpper(lowerBis, upperBis, tolSec=0, open_last=True):
    """按上级笔分解构建中枢（分解原则，不跨周期）：
    本级别中枢只能构建在「同一个上级笔」内部。用上级笔时间区间把本级别笔切段，
    每段内独立运行 buildZS，保证中枢不跨上级笔端点。
    @param lowerBis 本级别笔（已校准/对齐的最终绘制笔）
    @param upperBis 上一级别笔（用于分解约束，可为空数组）
    @param tolSec 时间容差（秒）：本级别端点经低一级校准后可能与上级端点有最多一个
                  本级别bar的偏移；同时作为 buildZS 的 barSec——中枢水平边缘左右各外扩 5×tolSec
    @param open_last 最后一段开放段（默认 True）：最后一个上级笔的 endTime 视为 +∞（当下）——
                  形成中的下级笔归属于形成中的上级笔（正在走的行情天然属于正在走的上级笔）。
                  否则上级形成笔的终点只随上级 bar 收盘延伸，下级最新笔会因 endTime 超出
                  上级段被丢弃，导致「出中枢力度对比」（zsExitWeak）在够笔当下无法评估、
                  只能等上级 bar 收盘（曾致 8-21 15:48 信号延后 15 分钟才触发）。
    @returns 中枢列表，每项额外含 upperStart/upperEnd（所属上级笔时间范围）
    """
    lowerBis = pointEligibleBis(lowerBis)
    if not lowerBis or len(lowerBis) < 3:
        return []
    tol = tolSec or 0
    out = []
    if not upperBis or len(upperBis) == 0:
        # 无上级约束（如最外层）：直接用本级别全部笔构建
        for z in buildZS(lowerBis, tol):
            z["upperStart"] = lowerBis[0]["startTime"]
            z["upperEnd"] = lowerBis[len(lowerBis) - 1]["endTime"]
            out.append(z)
        return out
    # 按时间完整归属到上级笔区间：笔必须 startTime 与 endTime 都落在同一上级笔内
    # （含 tol 容差）。不完整落在任何上级笔内的笔不参与中枢。
    # 最后一段开放段：最后一个上级笔的 endTime 边界视为 +∞（见 open_last 说明）。
    # 归属判定二分：满足 u.start - tol ≤ b.start 的 u 中取最后一个（bisect），
    # 再向下多查 3 个前驱（相邻上级笔共享端点 + tol 双向容差下，跨界笔可能命中
    # 更早的 u；按原列表顺序首个命中即归属，与原全量线性扫语义一致）。
    last_u = upperBis[len(upperBis) - 1]
    uStarts = [u["startTime"] for u in upperBis]
    nU = len(upperBis)
    segments = []
    cur = None  # { upper, bis }
    for b in lowerBis:
        j = bisect.bisect_right(uStarts, b["startTime"] + tol) - 1
        ub = None
        for jj in range(max(0, j - 3), j + 1):
            if jj >= nU:
                break
            u = upperBis[jj]
            boundary = u.get("coverageEnd", u["endTime"])
            end_ok = b["endTime"] <= boundary + tol or (open_last and u is last_u and "coverageEnd" not in u)
            if b["startTime"] >= u["startTime"] - tol and end_ok:
                ub = u
                break
        if ub is None:
            continue  # 不完整归属任何上级笔的零散笔不参与中枢
        if cur is None or cur["upper"] != ub:
            if cur is not None and len(cur["bis"]) > 0:
                segments.append(cur)
            cur = {"upper": ub, "bis": []}
        cur["bis"].append(b)
    if cur is not None and len(cur["bis"]) > 0:
        segments.append(cur)
    for seg in segments:
        for z in buildZS(seg["bis"], tol):
            z["upperStart"] = seg["upper"]["startTime"]
            z["upperEnd"] = seg["upper"]["endTime"]
            out.append(z)
    return out


# ============================================================
# 5. ATR / MACD
# ============================================================


def calcATR(rawBars, period=14):
    """计算 ATR（最近 period 根 TR 的简单平均）。"""
    trs = []
    for i in range(1, len(rawBars)):
        h = rawBars[i]["high"]
        l = rawBars[i]["low"]
        pc = rawBars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    start = max(0, len(trs) - period)
    sl = trs[start:]
    if len(sl) == 0:
        return 0
    s = 0.0
    for x in sl:
        s += x
    return s / len(sl)


def calcMACD(rawBars):
    """计算 MACD（EMA12/EMA26/DIF/DEA）。macd>0 红柱，macd<0 绿柱。"""
    if not rawBars or len(rawBars) < 2:
        return []
    closes = [b["close"] for b in rawBars]

    def ema(period):
        k = 2 / (period + 1)
        out = []
        prev = closes[0]
        out.append(prev)
        for i in range(1, len(closes)):
            prev = closes[i] * k + prev * (1 - k)
            out.append(prev)
        return out

    ema12 = ema(12)
    ema26 = ema(26)
    dif = [ema12[i] - ema26[i] for i in range(len(closes))]
    dea = []
    prevDea = dif[0]
    dea.append(prevDea)
    for i in range(1, len(dif)):
        prevDea = dif[i] * (2 / (9 + 1)) + prevDea * (1 - (2 / (9 + 1)))
        dea.append(prevDea)
    return [
        {"time": b["time"], "macd": (dif[i] - dea[i]) * 2, "dif": dif[i], "dea": dea[i]}
        for i, b in enumerate(rawBars)
    ]


class MacdAccumulator:
    """增量 MACD：逐根追加K线，输出与 calcMACD 完全一致的数组（EMA 状态递推）。"""

    def __init__(self):
        self.entries = []
        self._k12 = 2 / (12 + 1)
        self._k26 = 2 / (26 + 1)
        self._k9 = 2 / (9 + 1)
        self._ema12 = None
        self._ema26 = None
        self._dea = None

    def append(self, bar):
        """追加一根K线，返回新增的 MACD 条目；首根返回 dif/dea/macd 均为 0 的条目。"""
        c = bar["close"]
        if self._ema12 is None:
            self._ema12 = c
            self._ema26 = c
            dif = 0.0
            self._dea = 0.0
        else:
            self._ema12 = c * self._k12 + self._ema12 * (1 - self._k12)
            self._ema26 = c * self._k26 + self._ema26 * (1 - self._k26)
            dif = self._ema12 - self._ema26
            self._dea = dif * self._k9 + self._dea * (1 - self._k9)
        entry = {"time": bar["time"], "macd": (dif - self._dea) * 2, "dif": dif, "dea": self._dea}
        self.entries.append(entry)
        return entry

    def to_list(self):
        return self.entries


class AtrAccumulator:
    """增量 ATR：逐根追加K线，输出与 calcATR(period) 一致的值（最近 period 根 TR 的平均）。"""

    def __init__(self, period=14):
        self.period = period
        self._trs = []
        self._prevClose = None

    def append(self, bar):
        """追加一根K线，返回当前 ATR（不足 period 根 TR 时返回已有 TR 的平均，与 calcATR 一致）。"""
        if self._prevClose is not None:
            h = bar["high"]
            l = bar["low"]
            pc = self._prevClose
            tr = max(h - l, abs(h - pc), abs(l - pc))
            self._trs.append(tr)
            if len(self._trs) > self.period:
                self._trs.pop(0)
        self._prevClose = bar["close"]
        return self.value

    @property
    def value(self):
        """当前 ATR 值（float）。"""
        if len(self._trs) == 0:
            return 0.0
        return sum(self._trs) / len(self._trs)


# ============================================================
# 6. MACD 背驰判定
# ============================================================


def fmtT(ts):
    """时间格式化（本地时区，仅供调试打印）。"""
    dt = datetime.fromtimestamp(ts)
    return f"{dt.month}-{dt.day} {dt.hour:02d}:{dt.minute:02d}"


def _macdTime(m):
    """macdArr 条目的时间键（数组按时间升序，供 bisect 二分定位窗口）。"""
    return m["time"]


# macdArr → 平行时间列表缓存：同一 macdArr（回测引擎中为累加器内部列表，append-only）
# 反复进入 biMacdMetrics/hasMacdCrossBetween，缓存其时间列表可避免 bisect 的 key 回调
# （百万级调用下 key 回调本身即成热点）。按对象 LRU 多槽（持强引用防 id 复用）：
# 五周期交替调用下单槽会每拍互踢重建；长度只增时增量 extend（append-only 不变式），
# 实时切片（mark_entry macdArr[lo:hi]）为一次性对象，自然 miss 且被 LRU 淘汰，
# 不会挤掉持久周期槽。
_macdTimesSlots = OrderedDict()


def _macdTimesOf(macdArr):
    n = len(macdArr)
    key = id(macdArr)
    ent = _macdTimesSlots.get(key)
    if ent is not None and ent[0] is macdArr:
        times = ent[1]
        if len(times) == n:
            _macdTimesSlots.move_to_end(key)
            return times
        if len(times) < n:
            # append-only：只补新增段（引擎累加器不回退；回退路径换新对象 → 身份失配）
            times.extend(m["time"] for m in macdArr[len(times):n])
            _macdTimesSlots.move_to_end(key)
            return times
        # 长度回退（同对象截断，理论不可达）：丢弃重建
    times = [m["time"] for m in macdArr]
    _macdTimesSlots[key] = (macdArr, times)
    if len(_macdTimesSlots) > 16:
        _macdTimesSlots.popitem(last=False)
    return times


# biMacdMetrics 窗口结果缓存（按 macdArr 对象 LRU 多槽 + frozenTime 前缀界守卫）：
# 回测链路同一 (macdArr, [t0,t1] 窗口) 跨重算高频复发（find* 候选背驰、实时下沉链、
# 近等端点确认、zsExitWeak）。macdArr append-only 且条目时间严格递增 → 计算时
# t1 <= frozen（末条时间）的窗口此后不会再有条目落入，结果可共享；t1 > frozen 的
# 未定型窗口不入缓存。None 结果同守卫可缓存（窗口真空则永远空）。
# 返回值为缓存共享对象，调用方须只读（2026-09-18 核实全部调用方只读）。
_macdMetricsSlots = OrderedDict()


def biMacdMetrics(bi, macdArr):
    """计算一笔区间内的 MACD 动能指标
    { redArea, greenArea, difHigh, difLow, redMax, greenMax }。
    与 JS 版一致：redMax=单根红柱最大高度、greenMax=单根绿柱最大绝对值。
    性能：macdArr 按时间升序，用 bisect 定位 [t0,t1] 窗口（闭区间）后再累加，
    替代从头线性扫描——回测链路每次重算会调用本函数上万次，长窗口下线性扫是主要热点。
    窗口结果按 (macdArr 对象, (t0,t1)) 记忆化（见 _macdMetricsSlots）；返回值只读。"""
    if not macdArr or len(macdArr) == 0:
        return None
    t0 = bi["startTime"]
    t1 = bi["endTime"]
    key = id(macdArr)
    slot = _macdMetricsSlots.get(key)
    if slot is not None and slot[0] is macdArr:
        ent = slot[1].get((t0, t1))
        if ent is not None and t1 <= ent[1]:
            _macdMetricsSlots.move_to_end(key)
            return ent[0]
    metrics = _biMacdMetricsCompute(bi, macdArr, t0, t1)
    frozen = macdArr[-1]["time"]
    if slot is None or slot[0] is not macdArr:
        slot = [macdArr, {}]
        _macdMetricsSlots[key] = slot
        if len(_macdMetricsSlots) > 64:
            _macdMetricsSlots.popitem(last=False)
    elif len(slot[1]) >= 262144:
        slot[1].clear()
    if t1 <= frozen:
        slot[1][(t0, t1)] = (metrics, frozen)
    return metrics


def _biMacdMetricsCompute(bi, macdArr, t0, t1):
    """biMacdMetrics 的原始计算体（无缓存路径，bisect 窗口 + 累加）。"""
    metrics = {"redArea": 0.0, "greenArea": 0.0, "difHigh": float("-inf"),
               "difLow": float("inf"), "redMax": 0.0, "greenMax": 0.0}
    times = _macdTimesOf(macdArr)
    lo = bisect.bisect_left(times, t0)
    hi = bisect.bisect_right(times, t1)
    if lo >= hi:
        return None
    for m in macdArr[lo:hi]:
        if m["macd"] > 0:
            metrics["redArea"] += m["macd"]
            if m["macd"] > metrics["redMax"]:
                metrics["redMax"] = m["macd"]
        else:
            metrics["greenArea"] += -m["macd"]
            if -m["macd"] > metrics["greenMax"]:
                metrics["greenMax"] = -m["macd"]
        if m["dif"] > metrics["difHigh"]:
            metrics["difHigh"] = m["dif"]
        if m["dif"] < metrics["difLow"]:
            metrics["difLow"] = m["dif"]
    return metrics


def _areaDurComparable(a, b):
    """面积判据的时长可比门：面积Σ=柱高×K线根数的累加，与区间时长线性相关——
    时长悬殊的两段（如 15.65h 缓跌 vs 4.7h 急跌，Σ=136.2 vs 22.7）面积差主要来自
    时长而非动能，直接比较会把「短时急跌」误判为背驰。两段时长比 > divergeDurRatio
    （或某段时长为 0/负）时返回 False → 面积项不计入背驰，只用 DIF/柱高判据。"""
    da = (a.get("endTime") or 0) - (a.get("startTime") or 0)
    db = (b.get("endTime") or 0) - (b.get("startTime") or 0)
    mx, mn = max(da, db), min(da, db)
    return mn > 0 and mx / mn <= CHAN_CFG["divergeDurRatio"]


def isBiDiverge(bi, refer, macdArr):
    """MACD 背驰判定（OR 关系，满足其一即算背驰，与 JS 一致）：
    底背驰（对应一买，下跌笔）：绿柱面积变小 或 黄白线低点抬高 或 绿柱最大高度变小；
    顶背驰（对应一卖，上涨笔）：红柱面积变小 或 黄白线高点变低 或 红柱最大高度变小。
    面积两项受 _areaDurComparable 时长门约束（两段时长不可比时仅用 DIF/柱高判据）。"""
    cur = biMacdMetrics(bi, macdArr)
    ref = biMacdMetrics(refer, macdArr)
    if cur is None or ref is None:
        return False
    if bi["type"] == "down":
        return ((_areaDurComparable(bi, refer) and cur["greenArea"] < ref["greenArea"])
                or cur["difLow"] > ref["difLow"]
                or cur["greenMax"] < ref["greenMax"])
    return ((_areaDurComparable(bi, refer) and cur["redArea"] < ref["redArea"])
            or cur["difHigh"] < ref["difHigh"]
            or cur["redMax"] < ref["redMax"])


# ============================================================
# 7. MACD 红绿转换检测
# ============================================================


def hasMacdCrossBetween(macdArr, merged, aIdx, bIdx, aTime, bTime, direction=None):
    """检测两个分型之间是否发生方向性 MACD 红绿转换（用分型极值时间作边界）。
    direction="up"   ：底到顶（上涨），柱状体由绿变红（<=0 转 >0）
    direction="down" ：顶到底（下跌），柱状体由红变绿（>0 转 <=0）
    其余（None）：任意红绿转换（历史兼容）。
    性能：同 biMacdMetrics，bisect 定位 [t0,t1] 窗口（闭区间）后仅扫窗口内条目。"""
    if not macdArr or len(macdArr) == 0:
        return False
    t0 = aTime if aTime is not None else merged[aIdx]["time"]
    t1 = bTime if bTime is not None else merged[bIdx]["time"]
    times = _macdTimesOf(macdArr)
    lo = bisect.bisect_left(times, t0)
    hi = bisect.bisect_right(times, t1)
    prev = None
    for mm in macdArr[lo:hi]:
        if prev is not None:
            if direction == "up":
                crossed = prev["macd"] <= 0 and mm["macd"] > 0
            elif direction == "down":
                crossed = prev["macd"] > 0 and mm["macd"] <= 0
            else:
                crossed = (prev["macd"] >= 0 and mm["macd"] < 0) or (prev["macd"] <= 0 and mm["macd"] > 0)
            if crossed:
                return True
        prev = mm
    return False


# ============================================================
# 8. 未完成笔延伸 / 周期映射 / 端点校准
# ============================================================


def extendLastBi(bisArr, bars):
    """未完成笔延伸：最后一笔推进到最新极端价。

    原逻辑：从头线性扫描 bars 找最后笔起点（O(n)），再扫描起点到末尾取极端价。
    现改为：用二分（bars 时间升序）定位起点（O(log n)），再复用 extendLastBiFrom 做增量延伸。
    对外行为与原来完全一致（gapLocked 不延伸、up 找更高高点、down 找更低低点）。
    """
    if not bisArr or len(bisArr) == 0:
        return bisArr
    last = bisArr[-1]
    if last.get("gapLocked", False):
        return bisArr
    startIdx = bisect.bisect_left(bars, last["startTime"], key=lambda k: k["time"])
    return extendLastBiFrom(bisArr, bars, startIdx)


def extendLastBiFrom(bisArr, bars, startIdx, endIdx=None):
    """未完成笔延伸（增量入口）：从 startIdx 起只扫描新到K线，推进最后一笔到最新极端价。

    与原 extendLastBi 主体逻辑完全一致（gapLocked 不延伸、up 找更高高点、down 找更低低点、
    仅当极端价时间晚于当前 endTime 且价格更极端时才推进终点），
    区别只在于扫描窗口为 bars[startIdx:endIdx]（调用方传已记录的最后笔起点索引），
    避免从 bars 头部重复扫描与整段切片复制（O(n²) → O(窗口)）。
    """
    if not bisArr or len(bisArr) == 0:
        return bisArr
    last = bisArr[-1]
    if last.get("gapLocked", False):
        return bisArr
    if endIdx is None:
        endIdx = len(bars)
    if startIdx < 0 or startIdx >= endIdx:
        return bisArr
    tail = bars[startIdx:endIdx]
    if len(tail) < 2:
        return bisArr

    if last["type"] == "up":
        maxBar = tail[0]
        for k in tail:
            if k["high"] > maxBar["high"]:
                maxBar = k
        if maxBar["time"] > last["endTime"] and maxBar["high"] > last["endPrice"]:
            last["endTime"] = maxBar["time"]
            last["endPrice"] = maxBar["high"]
            last["span"] = maxBar["high"] - last["startPrice"]
    else:
        minBar = tail[0]
        for k in tail:
            if k["low"] < minBar["low"]:
                minBar = k
        if minBar["time"] > last["endTime"] and minBar["low"] < last["endPrice"]:
            last["endTime"] = minBar["time"]
            last["endPrice"] = minBar["low"]
            last["span"] = last["startPrice"] - minBar["low"]
    return bisArr


def lowerResOf(res):
    """逐级校准映射：15分钟←3分钟，1小时←15分钟，4小时←1小时，日线←4小时。"""
    s = str(res).upper()
    if s == "D" or s == "1D":
        return "240"
    if s == "240" or s == "4H":
        return "60"
    if s == "60" or s == "1H":
        return "15"
    if s == "15":
        return "3"
    return None


def calibrateBiTimes(bis, bigBars, refBars, bigIntervalSec):
    """跨周期端点时间校准：用低一级周期K线校准端点时间。"""
    if not bis or len(bis) == 0 or not refBars or len(refBars) == 0:
        return bis
    eps = 0.001

    def calibrateTime(t, price):
        big = None
        for k in bigBars:
            if k["time"] <= t and t < k["time"] + bigIntervalSec:
                big = k
                break
        if big is None:
            return t
        rangeEnd = big["time"] + bigIntervalSec
        best = None
        for rb in refBars:
            if rb["time"] < big["time"] or rb["time"] >= rangeEnd:
                continue
            if abs(rb["high"] - price) < eps or abs(rb["low"] - price) < eps:
                best = rb
        return best["time"] if best is not None else t

    for b in bis:
        b["startTime"] = calibrateTime(b["startTime"], b["startPrice"])
        b["endTime"] = calibrateTime(b["endTime"], b["endPrice"])
    return bis


def intervalSecOf(res):
    """周期 → 单根K线时长（秒）。注意 "30" = 30分钟，"30S" = 30秒（TradingView resolution 后缀 S 表秒级）。"""
    r = str(res).upper()
    if re.fullmatch(r"\d+S", r):
        return int(r[:-1])  # "30S" → 30（秒级）
    if r == "3":
        return 180
    if r == "5":
        return 300
    if r == "15":
        return 900
    if r == "30":
        return 1800
    if r == "60" or r == "1H":
        return 3600
    if r == "240" or r == "4H":
        return 14400
    if r == "D" or r == "1D":
        return 86400
    if r == "W" or r == "1W":
        return 604800
    return 0


# ============================================================
# 9. 买卖点识别
# ============================================================


def _findIndex(arr, pred):
    for i, x in enumerate(arr):
        if pred(x):
            return i
    return -1


def isSameAsUpperBi(bi, upperBis, barSec):
    """判断本周期某笔是否与上一级别某笔完全重合（时间容差 = 本周期 1 个 bar）。

    完全重合（同笔）说明本周期该笔内部无更细结构，本级别无从选有效参照（跨上级笔
    边界的比较无意义）→ 上级笔已结束时由本周期做「同笔」纯结构标记（不选参照笔、
    不比创新低/背驰）；上级笔仍为末笔（延伸中、反向笔未确认）时不标记。
    @returns 命中的上级笔对象（与 upperBis 内元素同引用）| None（未命中/空表），
             调用方可按真值使用（旧 bool 契约兼容）。"""
    if not upperBis or len(upperBis) == 0:
        return None
    tEps = barSec if barSec else 900
    pEps = 0.01
    # 时间带二分（回测链路 fib 热点：find* 每次调用逐笔判定，线性全扫是平方级主项）：
    # 命中须 |ΔstartTime| ≤ tEps，而同型上级笔 startTime 严格递增 → 带外必不匹配；
    # 带内按原列表顺序扫描，首个通过全部条件者与原全量扫描完全一致（输出不变）。
    starts = _ubStartTimes(upperBis)
    lo = bisect.bisect_left(starts, bi["startTime"] - tEps)
    hi = bisect.bisect_right(starts, bi["startTime"] + tEps)
    for ub in upperBis[lo:hi]:
        if ub["type"] != bi["type"]:
            continue
        if abs(ub["startTime"] - bi["startTime"]) <= tEps and \
           abs(ub["endTime"] - bi["endTime"]) <= tEps and \
           abs(ub["startPrice"] - bi["startPrice"]) <= pEps and \
           abs(ub["endPrice"] - bi["endPrice"]) <= pEps:
            return ub
    return None


# 上级笔 startTime 平行列表缓存（对象身份持强引用防 id 复用 + 长度/末元素校验失效，
# Fix 3 同款模式）：isSameAsUpperBi 的调用方（findBuy/findSellPoints）每次调用按类型
# 新建过滤子列表——缓存主要在同一次 find* 调用内命中（~B/2 次），跨调用自然失配重建。
_ubStartCache = {}


def _ubStartTimes(upperBis):
    n = len(upperBis)
    ent = _ubStartCache.get(id(upperBis))
    if (ent is not None and ent[0] is upperBis and len(ent[1]) == n
            and (n == 0 or ent[1][n - 1] == upperBis[n - 1]["startTime"])):
        return ent[1]
    starts = [u["startTime"] for u in upperBis]
    if len(_ubStartCache) >= 64:
        _ubStartCache.clear()
    _ubStartCache[id(upperBis)] = (upperBis, starts)
    return starts


def anchorFirstBuy(cand, upperBis):
    """一买锚定：取候选一买之前最近的上级底部端点。"""
    upperBis = confirmedStructureBis(upperBis)
    if not upperBis or len(upperBis) == 0:
        return None
    best = None
    for b in upperBis:
        t = b["startTime"] if b["type"] == "up" else b["endTime"]
        p = b["startPrice"] if b["type"] == "up" else b["endPrice"]
        if t > cand["time"]:
            continue
        if best is None or cand["time"] - t < cand["time"] - best["time"]:
            best = {"time": t, "price": p}
    return best


def _anchorSellPre(upperBis):
    """anchorFirstSell 的每调用预计算（吃**已确认**上级笔列表）。返回
    (upStarts, upEnds, upEndPrices, anchorT, anchorP) 或 None（单调性守卫失败
    → 调用方回退线性原路径）。"""
    upStarts, upEnds, upEndPrices = [], [], []
    anchorT, anchorP = [], []
    for b in upperBis:
        if b["type"] == "up":
            upStarts.append(b["startTime"])
            upEnds.append(b["endTime"])
            upEndPrices.append(b["endPrice"])
            t, p = b["endTime"], b["endPrice"]
        else:
            t, p = b["startTime"], b["startPrice"]
        anchorT.append(t)
        anchorP.append(p)
    for arr in (upStarts, anchorT):
        for i in range(1, len(arr)):
            if arr[i] < arr[i - 1]:
                return None
    return (upStarts, upEnds, upEndPrices, anchorT, anchorP)


def anchorFirstSell(cand, upperBis, _pre=None):
    """一卖锚定：候选在上级上涨笔内则上移到其结束点，否则取最近上级顶部端点。

    _pre 为 _anchorSellPre 对**已确认**上级笔（confirmedStructureBis 结果）的
    预计算（可选）：第一趟包含判定 bisect 后只查前驱与命中两个 up 笔（链序 up 笔
    不重叠，共享端点时先见者=前笔）；第二趟取 ≤ t 的最大右锚点（up→end /
    down→start，沿笔列表单调；相邻笔共享端点的重复时刻值等价）。缺省走线性
    原路径（内部自行 confirmedStructureBis，外部调用方不变）。"""
    if _pre is None:
        upperBis = confirmedStructureBis(upperBis)
    if not upperBis or len(upperBis) == 0:
        return None
    t = cand["time"]
    if _pre is not None:
        upStarts, upEnds, upEndPrices, anchorT, anchorP = _pre
        j = bisect.bisect_right(upStarts, t) - 1
        for jj in (j - 1, j):
            if 0 <= jj < len(upStarts) and upStarts[jj] <= t <= upEnds[jj]:
                return {"time": upEnds[jj], "price": upEndPrices[jj]}
        k = bisect.bisect_right(anchorT, t) - 1
        if k >= 0:
            return {"time": anchorT[k], "price": anchorP[k]}
        return None
    for b in upperBis:
        if b["type"] != "up":
            continue
        if b["startTime"] <= t and b["endTime"] >= t:
            return {"time": b["endTime"], "price": b["endPrice"]}
    best = None
    for b in upperBis:
        bt = b["endTime"] if b["type"] == "up" else b["startTime"]
        p = b["endPrice"] if b["type"] == "up" else b["startPrice"]
        if bt > t:
            continue
        if best is None or t - bt < t - best["time"]:
            best = {"time": bt, "price": p}
    return best


def _nearestUpBiIdx(upIdx, upTimes, t):
    """时间 t 最近的 up 笔在 bis 中的下标（与全量扫描同语义：严格 < 先见者胜，
    等距取先见者）。upTimes 与 upIdx 平行（up 笔 endTime 升序）；无 up 笔返回 None。"""
    if not upTimes:
        return None
    hi = bisect.bisect_left(upTimes, t)
    left = hi - 1
    while left > 0 and upTimes[left - 1] == upTimes[left]:
        left -= 1  # 重复段首下标（先见者）
    if left < 0:
        return upIdx[hi]  # t 早于全部 up 笔 endTime → 首个
    if hi >= len(upTimes):
        return upIdx[left]  # t 晚于全部 → 末个
    return upIdx[left] if (t - upTimes[left]) <= (upTimes[hi] - t) else upIdx[hi]


def snapToOwnBar(price, refTime, bars):
    """把极值价格/时间映射到本周期K线的 bar 边界。"""
    eps = 0.001
    best = None
    bestDist = float("inf")
    for k in bars:
        if abs(k["high"] - price) < eps or abs(k["low"] - price) < eps:
            d = abs(k["time"] - refTime)
            if d < bestDist:
                bestDist = d
                best = k["time"]
    if best is not None:
        return best
    nearest = bars[0]["time"] if len(bars) > 0 else refTime
    nd = float("inf")
    for k in bars:
        d = abs(k["time"] - refTime)
        if d < nd:
            nd = d
            nearest = k["time"]
    return nearest


def _pick_zs_for_seg(zss, seg_start, seg_end):
    """取与上级笔段重叠的中枢（优先 upperStart/End 精确匹配，否则时间相交、取最晚形成）。"""
    if not zss:
        return None
    exact = [z for z in zss
             if z.get("upperStart") == seg_start and z.get("upperEnd") == seg_end]
    pool = exact
    if not pool:
        pool = []
        for z in zss:
            us, ue = z.get("upperStart"), z.get("upperEnd")
            if us is not None and ue is not None:
                if us <= seg_end and ue >= seg_start:
                    pool.append(z)
            elif z["startTime"] <= seg_end and z["endTime"] >= seg_start:
                pool.append(z)
    if not pool:
        return None
    return max(pool, key=lambda z: z.get("enterEndTime") or z["startTime"])


def _pick_zs_for_two(zss, seg_start, seg_end, two_time):
    """为 2买/2卖选取关联中枢：优先时间覆盖该点的中枢，否则取其后最早形成的中枢。"""
    if not zss:
        return None
    pool = []
    for z in zss:
        us, ue = z.get("upperStart"), z.get("upperEnd")
        if us is not None and ue is not None:
            if not (us <= seg_end and ue >= seg_start):
                continue
        elif not (z["startTime"] <= seg_end and z["endTime"] >= seg_start):
            continue
        pool.append(z)
    if not pool:
        return None
    t0 = lambda z: z.get("enterEndTime") or z["startTime"]
    t1 = lambda z: z.get("exitTime") or z.get("exitStartTime") or z["endTime"]
    containing = [z for z in pool if t0(z) <= two_time <= t1(z)]
    if containing:
        return min(containing, key=t0)
    after = [z for z in pool if t0(z) >= two_time]
    if after:
        return min(after, key=t0)
    return max(pool, key=t0)


def _append_third_points(points, third_list, main_type, class_type, class2_type):
    """写入 3/类3；与类2同点时删类2保留 3/类3。

    去重/定位用增量索引（3/类3 时间集合 + 类2同刻首现下标表；删除后下标表整体
    重建——删除罕见，摊销 O(1)），与 any/_findIndex 全量扫描语义逐位一致。"""
    mc_types = (main_type, class_type)
    mc_times = {p["time"] for p in points if p["type"] in mc_types}
    c2_first = {}
    for i, p in enumerate(points):
        if p["type"] == class2_type and p["time"] not in c2_first:
            c2_first[p["time"]] = i
    for t in third_list:
        if t["time"] in mc_times:
            continue
        dup = c2_first.get(t["time"], -1)
        if dup >= 0:
            del points[dup]
            c2_first = {}
            for i, p in enumerate(points):
                if p["type"] == class2_type and p["time"] not in c2_first:
                    c2_first[p["time"]] = i
        points.append({"type": t["type"], "time": t["time"], "price": t["price"]})
        if t["type"] in mc_types:
            mc_times.add(t["time"])


# ---- find* 一买/一卖候选循环的冻结前缀记录缓存（第七批 S1）----
# 引擎 bis 只发生「后缀拼接换新 dict（seam ≥ len-24）/ resync 整表换新 / 末笔原地
# 延伸」三类变化，冻结前缀按引用共享且不再修改；structurePeriods 视图每拍新建但
# 元素为共享引用 → 记录序列按元素身份锚定，延伸/新笔只重算受影响尾部。
_FIND1_MARGIN = 26     # 冻结裕量：prospective/延伸中末笔 + BiInc _TAIL=24 splice 区
_FIND1_MIN_BIS = 64    # 过短列表不启用


def _upperSafeTime(knownUpper, upperByType):
    """上级安全界：endTime 早于该值的下级笔，其同笔判定（±1 本级 bar 时间带）不可能
    再被未来确认的上级笔覆盖——none/append 记录可安全复用。取倒数第 _FIND1_MARGIN
    个已确认上级笔的 startTime；无上级（判定恒 None）或上级过短（无裕量）→ ±inf。"""
    if upperByType is None:
        return float("inf")     # 恒无同笔命中，CAND 结果不依赖上级演化
    j = len(knownUpper) - _FIND1_MARGIN
    return knownUpper[j]["startTime"] if j >= 0 else float("-inf")


def _firstPointsLoop(bis, idxArr, upperByType, knownUpper, macdArr, barSec, records, is_sell):
    """一买/一卖候选循环（记录化执行，findBuy/findSellPoints 共用）。

    记录项 (idx_k, cur_ref, sameUpper_ref|None, kind, payload) 与 k=1..len(records)
    一一对应（每 k 恒产记录保证连续性）：
      0=无产出；1=无条件产出（同笔标记/背驰 append，对冻结前缀永久稳定）；
      2=当时命中上级末笔（延伸中）→ 重放时重做 `same_ref is knownUpper[-1]` 身份
        比较：仍是末笔→跳过；已非末笔（上级反向笔确认，单调迁移）→产出。
    复用守卫（pop 不可复用尾，后缀拼接不变量下 O(1) 摊销）：cur.endTime <
    上级安全界；idxArr[k] 对齐且 bis[idx_k] is cur_ref。尾部走原始逻辑并记录到
    冻结边界。debug 打印仅在尾部原始路径产生（重放不重放打印）。"""
    ust = _upperSafeTime(knownUpper, upperByType)
    while records:
        idx_k, cur_ref = records[-1][0], records[-1][1]
        k = len(records)
        if (cur_ref["endTime"] >= ust or k >= len(idxArr)
                or idxArr[k] != idx_k or bis[idx_k] is not cur_ref):
            records.pop()
        else:
            break
    out = []
    for (_idx_k, _cur, same_ref, kind, payload) in records:
        if kind == 1:
            out.append(payload)
        elif kind == 2 and not (knownUpper and same_ref is knownUpper[-1]):
            out.append(payload)
    freezeIdx = len(bis) - 1 - _FIND1_MARGIN
    for k in range(len(records) + 1, len(idxArr)):
        cur = bis[idxArr[k]]
        sameUpper = None
        kind = 0
        payload = None
        if not cur.get("_forming"):
            sameUpper = isSameAsUpperBi(cur, upperByType.get(cur["type"]) or [], barSec) \
                if upperByType is not None else None
            if sameUpper is not None:
                payload = {"biIdx": idxArr[k], "time": cur["endTime"], "price": cur["endPrice"]}
                if sameUpper is knownUpper[-1]:
                    if CHAN_CFG["debug"]:
                        print(f"[一{'卖' if is_sell else '买'}跳过-上级末笔延伸中] "
                              f"{fmtT(cur['endTime'])}({cur['endPrice']}) 与上级末笔重合，"
                              f"上级反向笔未确认")
                    kind = 2
                else:
                    if CHAN_CFG["debug"]:
                        print(f"[一{'卖' if is_sell else '买'}同笔] {fmtT(cur['endTime'])}"
                              f"({cur['endPrice']}) 与上级已结束{'上涨' if is_sell else '下跌'}笔重合，"
                              f"结构同笔标记1{'卖' if is_sell else '买'}")
                    out.append(payload)
                    kind = 1
            else:
                refer = None
                for j in range(k - 1, -1, -1):
                    cand = bis[idxArr[j]]
                    if cand["span"] < cur["span"] * 0.5:
                        continue
                    refer = cand
                    break
                if refer is not None and ((cur["endPrice"] > refer["endPrice"]) if is_sell
                                          else (cur["endPrice"] < refer["endPrice"])):
                    diverge = isBiDiverge(cur, refer, macdArr)
                    if CHAN_CFG["debug"]:
                        cm = biMacdMetrics(cur, macdArr)
                        rm = biMacdMetrics(refer, macdArr)
                        if is_sell:
                            print(f"[一卖候选] {fmtT(cur['endTime'])}({cur['endPrice']}) vs 参照 "
                                  f"{fmtT(refer['endTime'])}({refer['endPrice']}) "
                                  f"| 创新高={cur['endPrice'] > refer['endPrice']} "
                                  f"| 红柱面积 {cm['redArea']:.2f} vs {rm['redArea']:.2f} "
                                  f"| DIF高点 {cm['difHigh']:.3f} vs {rm['difHigh']:.3f} | 背驰={diverge}")
                        else:
                            print(f"[一买候选] {fmtT(cur['endTime'])}({cur['endPrice']}) vs 参照 "
                                  f"{fmtT(refer['endTime'])}({refer['endPrice']}) "
                                  f"| 创新低={cur['endPrice'] < refer['endPrice']} "
                                  f"| 绿柱面积 {cm['greenArea']:.2f} vs {rm['greenArea']:.2f} "
                                  f"| DIF低点 {cm['difLow']:.3f} vs {rm['difLow']:.3f} | 背驰={diverge}")
                    if diverge:
                        payload = {"biIdx": idxArr[k], "time": cur["endTime"],
                                   "price": cur["endPrice"]}
                        out.append(payload)
                        kind = 1
        if idxArr[k] <= freezeIdx:
            records.append((idxArr[k], cur, sameUpper, kind, payload))
    return out


def _find1RecordsSlot(cache, macdArr, kind):
    """候选循环记录槽（按 macdArr 对象身份分槽，持强引用）。"""
    slot = cache.get((kind, id(macdArr)))
    if slot is None or slot[0] is not macdArr:
        slot = [macdArr, []]
        cache[(kind, id(macdArr))] = slot
    return slot[1]


def _find2WinSlot(cache, macdArr, kind):
    """2买/2卖窗口结果槽（按 macdArr 对象身份分槽；值 {id(上级笔): (dn_ref,
    win_points, meta_entry|None)}，持强引用防 id 复用）。"""
    slot = cache.get((kind, id(macdArr)))
    if slot is None or slot[0] is not macdArr:
        slot = [macdArr, {}]
        cache[(kind, id(macdArr))] = slot
    return slot[1]


def findBuyPoints(bis, upperBis, macdArr, barSec, class2ZsTol=0.0, thirdZsTol=0.0, cache=None):
    """买点识别（MACD 背驰 + 抬高结构 + 中枢类2/3）。
    2买：上级上涨笔内首个 price > up.startPrice（无上级：结构底抬高），不读 zd/zg。
    类2买：2买后更高抬高且落在中枢 [zd-class2ZsTol, zg]；无中枢不标。
    3买/类3买：离 zg 后回踩 > zg-thirdZsTol；同段第1/第2个；无中枢不标。
    1买逻辑不变。
    """
    bis = pointEligibleBis(bis)
    knownUpper = confirmedStructureBis(upperBis)
    if len(bis) < 3:
        return []
    c2tol = float(class2ZsTol or 0)
    t3tol = float(thirdZsTol or 0)
    downIdx = [i for i, b in enumerate(bis) if b["type"] == "down"]
    downLows = [(i, bis[i]["endTime"], bis[i]["endPrice"]) for i in downIdx]
    downTimes = [t for _, t, _ in downLows]
    idxByEndTime = {}
    for i, b in enumerate(bis):
        if b["endTime"] not in idxByEndTime:
            idxByEndTime[b["endTime"]] = i
    upperByType = None
    if knownUpper:
        upperByType = {"up": [u for u in knownUpper if u["type"] == "up"],
                       "down": [u for u in knownUpper if u["type"] == "down"]}

    # 候选一买：创新低 + MACD 背驰；同笔例外（S1 记录化：冻结前缀重放 + 尾部续算）
    recs = _find1RecordsSlot(cache, macdArr, "find1buy") \
        if (cache is not None and len(bis) >= _FIND1_MIN_BIS) else []
    firstBuys = _firstPointsLoop(bis, downIdx, upperByType, knownUpper, macdArr,
                                 barSec, recs, is_sell=False)
    firstBuy = firstBuys[-1] if firstBuys else None

    points = []
    twoBuyMeta = []  # 有中枢的 2买，供类2/3/类3

    if upperBis is not None and len(upperBis) > 0:
        # S3：冻结上级笔的窗口结果记忆化（窗口内下级笔与 dn 均已冻结 → 结果稳定）
        wincache = _find2WinSlot(cache, macdArr, "find2buy") if cache is not None else None
        lowerSafeEnd = bis[len(bis) - _FIND1_MARGIN]["endTime"] \
            if len(bis) > _FIND1_MARGIN else None
        nUpperStable = len(upperBis) - _FIND1_MARGIN
        zss = None
        for i_up, up in enumerate(upperBis):
            if up["type"] != "up":
                continue
            stable = (wincache is not None and i_up < nUpperStable
                      and lowerSafeEnd is not None
                      and up.get("coverageEnd", up["endTime"]) + 1 < lowerSafeEnd)
            ent = wincache.get(id(up)) if stable else None
            if ent is not None and ent[0] is up:
                if ent[1]:
                    points.extend(ent[1])
                if ent[2] is not None:
                    twoBuyMeta.append(ent[2])
                continue
            if zss is None:
                zss = buildZSByUpper(bis, upperBis, barSec)
            lo = bisect.bisect_left(downTimes, up["startTime"])
            segEnd = up.get("coverageEnd", up["endTime"])
            hi = bisect.bisect_right(downTimes, segEnd + 1)
            if lo >= hi:
                continue
            lows = [{"biIdx": i, "time": t, "price": p} for i, t, p in downLows[lo:hi]]
            lows.sort(key=lambda x: x["time"])
            # 2买：抬高结构，不依赖中枢
            firstLow = next((l for l in lows if l["price"] > up["startPrice"]), None)
            if firstLow is None:
                continue
            win_points = [{"type": "2买", "time": firstLow["time"], "price": firstLow["price"]}]
            zs = _pick_zs_for_two(zss, up["startTime"], up["endTime"], firstLow["time"])
            meta_entry = None
            if zs is not None:
                zd, zg = zs["zd"], zs["zg"]
                meta_entry = {"time": firstLow["time"], "price": firstLow["price"],
                              "zg": zg, "segStart": up["startTime"], "segEnd": segEnd}
                later = next((l for l in lows
                              if l["time"] > firstLow["time"] and l["price"] > firstLow["price"]
                              and (zd - c2tol) <= l["price"] <= zg), None)
                if later is not None:
                    win_points.append({"type": "类2买", "time": later["time"], "price": later["price"]})
            if stable:
                wincache[id(up)] = (up, win_points, meta_entry)
            points.extend(win_points)
            if meta_entry is not None:
                twoBuyMeta.append(meta_entry)
    else:
        # 结构底 → 2买（不依赖中枢）
        structBottomIdx = None
        if firstBuy is not None:
            minP = float("inf")
            for i in downIdx:
                if i >= firstBuy["biIdx"]:
                    break
                if bis[i]["endPrice"] < minP:
                    minP = bis[i]["endPrice"]
                    structBottomIdx = i
        if structBottomIdx is None:
            minP = float("inf")
            for i in downIdx:
                if bis[i]["endPrice"] < minP:
                    minP = bis[i]["endPrice"]
                    structBottomIdx = i
        if structBottomIdx is not None:
            bottom = bis[structBottomIdx]
            secondBuy = None
            for i in range(structBottomIdx + 1, len(bis)):
                if bis[i]["type"] != "down":
                    continue
                if bis[i]["endPrice"] > bottom["endPrice"]:
                    secondBuy = {"biIdx": i, "time": bis[i]["endTime"], "price": bis[i]["endPrice"]}
                    break
            if secondBuy is not None:
                points.append({"type": "2买", "time": secondBuy["time"], "price": secondBuy["price"]})
                zss = buildZS(bis, barSec)
                zs = _pick_zs_for_two(zss, secondBuy["time"], secondBuy["time"], secondBuy["time"])
                if zs is not None:
                    zd, zg = zs["zd"], zs["zg"]
                    t0 = zs.get("enterEndTime") or zs["startTime"]
                    t1 = zs.get("exitTime") or zs["endTime"]
                    twoBuyMeta.append({"time": secondBuy["time"], "price": secondBuy["price"],
                                       "zg": zg, "segStart": t0, "segEnd": t1})
                    classSecond = None
                    for i in range(secondBuy["biIdx"] + 1, len(bis)):
                        if bis[i]["type"] != "down":
                            continue
                        p = bis[i]["endPrice"]
                        if p > secondBuy["price"] and (zd - c2tol) <= p <= zg:
                            classSecond = {"time": bis[i]["endTime"], "price": p}
                            break
                    if classSecond is not None:
                        points.append({"type": "类2买", "time": classSecond["time"],
                                       "price": classSecond["price"]})

    for fb in firstBuys:
        points.append({"type": "1买", "time": fb["time"], "price": fb["price"]})

    # 3买 / 类3买：仅对有中枢的 2买段；扫到「下一个 2买」（含无中枢的 2买）之前
    all_two = sorted([p for p in points if p["type"] == "2买"], key=lambda x: x["time"])
    twoBuyMeta.sort(key=lambda x: x["time"])
    third_out = []
    for tb in twoBuyMeta:
        twoIdx = idxByEndTime.get(tb["time"], -1)
        if twoIdx < 0:
            continue
        endScan = len(bis)
        for nxt in all_two:
            if nxt["time"] > tb["time"]:
                endScan = idxByEndTime.get(nxt["time"], len(bis))
                if endScan < 0:
                    endScan = len(bis)
                break
        zg = tb["zg"]
        valids = []
        for i in range(twoIdx + 1, endScan):
            if bis[i]["type"] != "up":
                continue
            if bis[i]["endPrice"] <= zg:
                continue
            for mm in range(i + 1, endScan):
                if bis[mm]["type"] != "down":
                    continue
                bt = bis[mm]["endTime"]
                bp = bis[mm]["endPrice"]
                if bp > zg - t3tol and bt >= tb["segStart"] and bt <= tb["segEnd"] + 1:
                    valids.append({"time": bt, "price": bp})
                break
        if valids:
            third_out.append({"type": "3买", "time": valids[0]["time"], "price": valids[0]["price"]})
            if len(valids) >= 2:
                third_out.append({"type": "类3买", "time": valids[1]["time"], "price": valids[1]["price"]})
    _append_third_points(points, third_out, "3买", "类3买", "类2买")
    return points


def findSellPoints(bis, upperBis, macdArr, barSec, class2ZsTol=0.0, thirdZsTol=0.0, cache=None):
    """卖点识别（与买点对称）：2卖不依赖中枢；类2/3/类3 依赖中枢。"""
    bis = pointEligibleBis(bis)
    knownUpper = confirmedStructureBis(upperBis)
    if len(bis) < 3:
        return []
    c2tol = float(class2ZsTol or 0)
    t3tol = float(thirdZsTol or 0)
    upIdx = [i for i, b in enumerate(bis) if b["type"] == "up"]
    upHighs = [(i, bis[i]["endTime"], bis[i]["endPrice"]) for i in upIdx]
    upTimes = [t for _, t, _ in upHighs]
    idxByEndTime = {}
    for i, b in enumerate(bis):
        if b["endTime"] not in idxByEndTime:
            idxByEndTime[b["endTime"]] = i
    upperByType = None
    if knownUpper:
        upperByType = {"up": [u for u in knownUpper if u["type"] == "up"],
                       "down": [u for u in knownUpper if u["type"] == "down"]}

    # 候选一卖：创新高 + MACD 背驰；同笔例外（S1 记录化：冻结前缀重放 + 尾部续算）
    recs = _find1RecordsSlot(cache, macdArr, "find1sell") \
        if (cache is not None and len(bis) >= _FIND1_MIN_BIS) else []
    firstSells = _firstPointsLoop(bis, upIdx, upperByType, knownUpper, macdArr,
                                  barSec, recs, is_sell=True)
    firstSell = firstSells[-1] if firstSells else None

    anchoredSells = []
    seenSellPos = set()
    # 锚定链路预计算（每调用一次；O(U)）——单调性守卫失败回退线性原路径
    anchorPre = _anchorSellPre(knownUpper) if knownUpper else None
    for fs in firstSells:
        anchored = fs
        if knownUpper:
            a = anchorFirstSell(fs, knownUpper, _pre=anchorPre)
            if a is not None:
                # 最近 up 笔：upTimes 升序 → bisect（严格 < 先见者胜、等距取先者，
                # 与原全量扫描逐位一致）
                bi_i = _nearestUpBiIdx(upIdx, upTimes, a["time"])
                anchored = {
                    "biIdx": bi_i if bi_i is not None else fs["biIdx"],
                    "time": a["time"],
                    "price": a["price"],
                }
        if anchored["time"] in seenSellPos:
            continue
        seenSellPos.add(anchored["time"])
        anchoredSells.append(anchored)

    points = []
    twoSellMeta = []

    if upperBis is not None and len(upperBis) > 0:
        # S3：冻结上级笔的窗口结果记忆化（窗口内下级笔与 dn 均已冻结 → 结果稳定）
        wincache = _find2WinSlot(cache, macdArr, "find2sell") if cache is not None else None
        lowerSafeEnd = bis[len(bis) - _FIND1_MARGIN]["endTime"] \
            if len(bis) > _FIND1_MARGIN else None
        nUpperStable = len(upperBis) - _FIND1_MARGIN
        zss = None
        for i_dn, dn in enumerate(upperBis):
            if dn["type"] != "down":
                continue
            stable = (wincache is not None and i_dn < nUpperStable
                      and lowerSafeEnd is not None
                      and dn.get("coverageEnd", dn["endTime"]) + 1 < lowerSafeEnd)
            ent = wincache.get(id(dn)) if stable else None
            if ent is not None and ent[0] is dn:
                if ent[1]:
                    points.extend(ent[1])
                if ent[2] is not None:
                    twoSellMeta.append(ent[2])
                continue
            if zss is None:
                zss = buildZSByUpper(bis, upperBis, barSec)
            lo = bisect.bisect_left(upTimes, dn["startTime"])
            segEnd = dn.get("coverageEnd", dn["endTime"])
            hi = bisect.bisect_right(upTimes, segEnd + 1)
            if lo >= hi:
                continue
            highs = [{"biIdx": i, "time": t, "price": p} for i, t, p in upHighs[lo:hi]]
            highs.sort(key=lambda x: x["time"])
            # 2卖：次高结构，不依赖中枢
            firstHigh = next((h for h in highs if h["price"] < dn["startPrice"]), None)
            if firstHigh is None:
                continue
            win_points = [{"type": "2卖", "time": firstHigh["time"], "price": firstHigh["price"]}]
            zs = _pick_zs_for_two(zss, dn["startTime"], dn["endTime"], firstHigh["time"])
            meta_entry = None
            if zs is not None:
                zd, zg = zs["zd"], zs["zg"]
                meta_entry = {"time": firstHigh["time"], "price": firstHigh["price"],
                              "zd": zd, "segStart": dn["startTime"], "segEnd": segEnd}
                later = next((h for h in highs
                              if h["time"] > firstHigh["time"] and h["price"] < firstHigh["price"]
                              and zd <= h["price"] <= (zg + c2tol)), None)
                if later is not None:
                    win_points.append({"type": "类2卖", "time": later["time"], "price": later["price"]})
            if stable:
                wincache[id(dn)] = (dn, win_points, meta_entry)
            points.extend(win_points)
            if meta_entry is not None:
                twoSellMeta.append(meta_entry)
    else:
        # 结构顶 → 2卖
        structTopIdx = None
        if firstSell is not None:
            maxP = float("-inf")
            for i in upIdx:
                if i >= firstSell["biIdx"]:
                    break
                if bis[i]["endPrice"] > maxP:
                    maxP = bis[i]["endPrice"]
                    structTopIdx = i
        if structTopIdx is None:
            maxP = float("-inf")
            for i in upIdx:
                if bis[i]["endPrice"] > maxP:
                    maxP = bis[i]["endPrice"]
                    structTopIdx = i
        if structTopIdx is not None:
            top = bis[structTopIdx]
            secondSell = None
            for i in range(structTopIdx + 1, len(bis)):
                if bis[i]["type"] != "up":
                    continue
                if bis[i]["endPrice"] < top["endPrice"]:
                    secondSell = {"biIdx": i, "time": bis[i]["endTime"], "price": bis[i]["endPrice"]}
                    break
            if secondSell is not None:
                points.append({"type": "2卖", "time": secondSell["time"], "price": secondSell["price"]})
                zss = buildZS(bis, barSec)
                zs = _pick_zs_for_two(zss, secondSell["time"], secondSell["time"], secondSell["time"])
                if zs is not None:
                    zd, zg = zs["zd"], zs["zg"]
                    t0 = zs.get("enterEndTime") or zs["startTime"]
                    t1 = zs.get("exitTime") or zs["endTime"]
                    twoSellMeta.append({"time": secondSell["time"], "price": secondSell["price"],
                                        "zd": zd, "segStart": t0, "segEnd": t1})
                    classSecond = None
                    for i in range(secondSell["biIdx"] + 1, len(bis)):
                        if bis[i]["type"] != "up":
                            continue
                        p = bis[i]["endPrice"]
                        if p < secondSell["price"] and zd <= p <= (zg + c2tol):
                            classSecond = {"time": bis[i]["endTime"], "price": p}
                            break
                    if classSecond is not None:
                        points.append({"type": "类2卖", "time": classSecond["time"],
                                       "price": classSecond["price"]})

    for as_ in anchoredSells:
        points.append({"type": "1卖", "time": as_["time"], "price": as_["price"]})

    twoSellMeta.sort(key=lambda x: x["time"])
    all_two = sorted([p for p in points if p["type"] == "2卖"], key=lambda x: x["time"])
    third_out = []
    for ts in twoSellMeta:
        twoIdx = idxByEndTime.get(ts["time"], -1)
        if twoIdx < 0:
            continue
        endScan = len(bis)
        for nxt in all_two:
            if nxt["time"] > ts["time"]:
                endScan = idxByEndTime.get(nxt["time"], len(bis))
                if endScan < 0:
                    endScan = len(bis)
                break
        zd = ts["zd"]
        valids = []
        for i in range(twoIdx + 1, endScan):
            if bis[i]["type"] != "down":
                continue
            if bis[i]["endPrice"] >= zd:
                continue
            for mm in range(i + 1, endScan):
                if bis[mm]["type"] != "up":
                    continue
                st = bis[mm]["endTime"]
                sp = bis[mm]["endPrice"]
                if sp < zd + t3tol and st >= ts["segStart"] and st <= ts["segEnd"] + 1:
                    valids.append({"time": st, "price": sp})
                break
        if valids:
            third_out.append({"type": "3卖", "time": valids[0]["time"], "price": valids[0]["price"]})
            if len(valids) >= 2:
                third_out.append({"type": "类3卖", "time": valids[1]["time"], "price": valids[1]["price"]})
    _append_third_points(points, third_out, "3卖", "类3卖", "类2卖")
    return points


def keepRecentEach(points, keep=1):
    """低级别每类买卖点只保留时间上最近 keep 个（与 JS 版 keepRecentEach 对齐）。"""
    n = max(1, int(keep) or 1)
    if n == 1:
        byType = {}
        for p in points:
            if p["type"] not in byType or p["time"] > byType[p["type"]]["time"]:
                byType[p["type"]] = p
        return sorted(byType.values(), key=lambda x: x["time"])
    # keep > 1：每类按时间倒序取最近 n 个（保证保留的是时间上最新的一组）
    groups = {}
    for p in points:
        groups.setdefault(p["type"], []).append(p)
    out = []
    for key in groups:
        groups[key].sort(key=lambda x: x["time"], reverse=True)
        out.extend(groups[key][:n])
    return sorted(out, key=lambda x: x["time"])


def keepRecentAll(points, keep=10):
    """每周期买卖点不分类（买+卖合并），只保留时间上最近 keep 个（与 JS 版 keepRecentAll 对齐）。"""
    n = max(1, int(keep) or 1)
    pts = sorted(points, key=lambda p: p["time"], reverse=True)[:n]
    return sorted(pts, key=lambda p: p["time"])
