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
import re

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
    "debug": False,    # 调试打印（buildBi / 买卖点识别过程）
}

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
    """
    n = len(merged)
    if n < 3:
        return []
    # 去掉尾部可能变化的分型（mergedIdx >= n-2）
    kept = [f for f in fractals if f["mergedIdx"] < n - 2]
    f = fractalAt(merged, n - 2)
    if f is not None:
        kept.append(f)
    return kept


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


def buildBi(fractals, merged, atr, macdArr, lockedPivots=None, nearDouble=False):
    """笔构建。与 JS 版 buildBi 对齐。lockedPivots 为上级笔端点（区间套强制对齐，优先级最高）；
    nearDouble=True 时启用「近等双顶/双底平台取后顶/后底」（≥60m 周期由调用方开启）。"""
    gapThreshold = atr * CHAN_CFG["gapFilter"] if atr else 0

    # 阶段一：严格交替分型序列
    seq = []
    for f in fractals:
        if len(seq) == 0:
            seq.append(f)
            continue
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

    # 区间套强制对齐（优先级最高）：上级笔端点（lockedPivots）必须在下级笔中被保留为端点，
    # 不能被阶段二的任何「移除中间分型」逻辑吞掉。在阶段一序列上标记与上级端点方向/价格一致的分型。
    if lockedPivots:
        for f in seq:
            p = f["high"] if f["type"] == "top" else f["low"]
            for lp in lockedPivots:
                if lp["dir"] == f["type"] and abs(lp["price"] - p) <= 0.001:
                    f["locked"] = True
                    break

    def isValid(a, b):
        # 有效笔判断：合并后K线从起点分型到终点分型（含两端分型）至少 5 根即可成笔。
        # gap = b["mergedIdx"] - a["mergedIdx"]，等价于合并K线数 gap+1 >= 5。
        gap = b["mergedIdx"] - a["mergedIdx"]
        return gap >= 4

    def noMoreExtremeInside(a, b):
        for i in range(a["mergedIdx"] + 1, b["mergedIdx"]):
            if b["type"] == "bottom" and merged[i]["low"] < b["low"]:
                return False
            if b["type"] == "top" and merged[i]["high"] > b["high"]:
                return False
        return True

    def fractalRangeClear(a, b):
        # 分型范围脱离检查（双向，与 JS chan-core 对齐）：一笔的两端分型不能互相"包含"。
        # 起点侧：与段同侧的两根（下跌笔顶起点取 [中心, 右] 的最低——不用左 bar，否则
        #   主升前夜/起涨点的旧低点会错误抬高"必须跌破"的阈值，误杀后续健康反弹；
        #   上涨笔底起点对称取 [左, 中心] 的最高）。
        # 终点侧：分型自身三根范围（防反向吞没）：下跌笔的底分型三根K线最高价不得涨回
        #   起点顶价之上（顶后崩盘 bar 跌回起点之下 = 中继弱反弹，不成笔；中心 bar 的
        #   崩盘低点可能被包含合并抬高，须依赖三根中的右 bar 提供证据）；上涨笔对称。
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

    if CHAN_CFG["debug"]:
        def ft(s):
            v = s["high"] if s["type"] == "top" else s["low"]
            return f"{'顶' if s['type']=='top' else '底'}@{s['mergedIdx']}({v})"
        print("[阶段一] 交替分型序列:", " → ".join(ft(s) for s in seq))

    # 阶段二：移除间隔不足的中间分型（回溯替换）
    result = []
    for k in seq:
        if len(result) == 0:
            result.append(k)
            continue
        last = result[-1]
        if k["type"] == last["type"]:
            if last.get("locked", False):
                # locked 端点（上级笔端点，区间套强制对齐）不可被同类型分型替换
                continue
            if not last.get("gapLocked", False):
                if k["type"] == "top":
                    if k["high"] >= last["high"]:
                        result[-1] = k
                else:
                    if k["low"] <= last["low"]:
                        result[-1] = k
            else:
                # 跳空锁定的端点：仅当后续同类型分型「突破」锁定价格时才解锁替换
                if k["type"] == "top":
                    if k["high"] > last["high"]:
                        result[-1] = k
                else:
                    if k["low"] < last["low"]:
                        result[-1] = k
            # 近等双顶/双底平台取后顶/后底（走势终完美；≥60m 周期由调用方开启 nearDouble）：
            #   后顶/后底 k 与前顶/前底 last 近同价（k 略不极端，差 ≤ max(nearDoubleAtrK×ATR,
            #   nearDoublePct×价)），且 last→k 间所有相邻分型间隔 <4（拆不出笔的平台/直拉，
            #   段内无可确认回调结构，走势未完美）；中间确有一次 ≥thr 真实回调。单跳封顶：
            #   被替换端点打 nearDouble 标记，不二次替换（防平台内累积漂移超阈值）。
            if (nearDouble and not last.get("gapLocked", False) and not k.get("locked", False)
                    and not last.get("nearDouble", False)):
                # locked/gapLocked 不参与；macdCross 不豁免（该端点本就是间隔不足靠 MACD 变色
                # 凑出的脆弱顶/底，如 1h 8-31 顶 4464.23，与近等平台取后顶语义一致）
                ref_price = last["high"] if k["type"] == "top" else last["low"]
                thr = max(atr * CHAN_CFG["nearDoubleAtrK"], ref_price * CHAN_CFG["nearDoublePct"])
                diff = (last["high"] - k["high"]) if k["type"] == "top" else (k["low"] - last["low"])
                if 0 <= diff <= thr:
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
                                  f"（差 {diff:.2f} ≤ {thr:.2f}，平台内无成笔结构）")
                        k["nearDouble"] = True  # 单跳封顶
                        result[-1] = k
            continue
        # 异类型
        # MACD 变色成笔端点让位
        if len(result) >= 2:
            prev2 = result[-2]
            topOne = result[-1]
            if prev2.get("macdCross", False) is True and prev2["type"] == k["type"] and \
               not topOne.get("locked", False) and \
               ((k["type"] == "top" and k["high"] > prev2["high"]) or
                (k["type"] == "bottom" and k["low"] < prev2["low"])):
                if CHAN_CFG["debug"]:
                    print(f"[阶段二] MACD端点让位: {prev2['mergedIdx']} -> {k['mergedIdx']}")
                k["macdCross"] = True
                result[-2] = k
                result.pop()
                continue
        # 跳空优先
        hasGap = gapThreshold > 0 and hasGapBetween(merged, last["mergedIdx"], k["mergedIdx"], atr, CHAN_CFG["gapFilter"])
        if hasGap:
            if CHAN_CFG["debug"]:
                print(f"[阶段二] 跳空成笔: {last['mergedIdx']} -> {k['mergedIdx']}")
            k["gapLocked"] = True
            result.append(k)
            continue
        # 前顶/前底作废
        if len(result) >= 3:
            prev3 = result[-3]
            prev2 = result[-2]
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
               not isValid(prev2, last) and \
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
                result[-2] = k
                result.pop()
                continue
        if isValid(last, k) and (noMoreExtremeInside(last, k) or last.get("gapLocked", False)) and \
           (fractalRangeClear(last, k) or last.get("gapLocked", False)):
            result.append(k)
        elif isValid(last, k):
            if CHAN_CFG["debug"]:
                print(f"[阶段二] 忽略 k: {k['mergedIdx']}")
        else:
            # 间隔不足：先检查 last→k 是否满足「合并后只有4根K + 方向性 MACD 变色」成笔。
            # 方向性变色：底到顶(上涨) 柱状体由绿变红；顶到底(下跌) 柱状体由红变绿。
            gap = k["mergedIdx"] - last["mergedIdx"]
            direction = "up" if last["type"] == "bottom" else "down"
            macdCross = bool(macdArr) and hasMacdCrossBetween(macdArr, merged, last["mergedIdx"], k["mergedIdx"], last["time"], k["time"], direction)
            macdRawCount = countRaw(merged, last["mergedIdx"], k["mergedIdx"])
            if gap == 3 and macdCross and noMoreExtremeInside(last, k):
                if CHAN_CFG["debug"]:
                    print(f"[阶段二] MACD变色成笔: {last['mergedIdx']} -> {k['mergedIdx']} (合并4根K, {'绿变红' if direction == 'up' else '红变绿'})")
                k["macdCross"] = True
                k["macdRaw"] = macdRawCount
                result.append(k)
            else:
                if len(result) >= 2 and result[-2]["type"] == k["type"]:
                    prev = result[-2]
                    moreExtreme = k["high"] >= prev["high"] if k["type"] == "top" else k["low"] <= prev["low"]
                    gapPrevLast = last["mergedIdx"] - prev["mergedIdx"]
                    if CHAN_CFG["debug"]:
                        print(f"[阶段二] 间隔不足: {k['mergedIdx']} 与 {last['mergedIdx']}, moreExtreme={moreExtreme}, gapPrevLast={gapPrevLast}")
                    # 前顶/前底作废原则（缠论，与 JS chan-core 一致）：顶被更高顶突破时，
                    # 作废前顶的条件是「前顶右侧是否已有足够K线构成笔」：
                    #   prev→last 构成有效笔（间隔>=4 且 笔内无更极值 且 分型范围脱离）
                    #   → 前顶有效，保留，不能被更高顶作废（如已走出有效下跌笔后，
                    #     更高顶无法与右侧成笔，应作废的是新顶而非前顶）；
                    # 仅当 prev→last 不构成有效笔时，更极端的 k 才能顶替 prev。
                    prev_last_valid_bi = gapPrevLast >= 4 and \
                        noMoreExtremeInside(prev, last) and fractalRangeClear(prev, last)
                    # 最小间隔脆弱笔例外：prev→last 虽构成有效笔，但间隔恰为最小值（4，
                    # 即刚够 5 根合并K线）且回调/反弹浅（< 前段涨跌幅的 50%）时，该笔
                    # 尚未被确认——随后 k 即创更高顶/更低底说明整段仍是同一笔的延伸
                    # （缠论：顶被更高顶突破即作废，上涨笔延伸到新极值），prev 应被 k 顶替。
                    fragile_minimal = False
                    if prev_last_valid_bi and gapPrevLast == 4 and len(result) >= 3:
                        p3 = result[-3]
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
                        if len(result) >= 3:
                            prev3 = result[-3]
                            last_is_deeper = (
                                (k["type"] == "top" and last["low"] < prev3["low"])
                                or (k["type"] == "bottom" and last["high"] > prev3["high"])
                            )
                            if last_is_deeper and not prev.get("locked", False) and not prev3.get("locked", False):
                                if CHAN_CFG["debug"]:
                                    print(f"[阶段二] 回溯替换保护: {'顶' if last['type']=='top' else '底'}@{last['mergedIdx']} 比 {'顶' if prev3['type']=='top' else '底'}@{prev3['mergedIdx']} 更极端，保留 last 为端点，作废 prev，暂不接入 k")
                                result[-3] = last
                                result.pop()
                                result.pop()
                                continue
                        if not last.get("locked", False) and not prev.get("locked", False):
                            result[-2] = k
                            result.pop()

    if CHAN_CFG["debug"]:
        def ft2(s):
            v = s["high"] if s["type"] == "top" else s["low"]
            return f"{'顶' if s['type']=='top' else '底'}@{s['mergedIdx']}({v})"
        print("[阶段二] 结果序列:", " → ".join(ft2(s) for s in result))

    # 阶段三：两两连笔
    bis = []
    for i in range(0, len(result) - 1):
        a = result[i]
        b = result[i + 1]
        startPrice = a["high"] if a["type"] == "top" else a["low"]
        endPrice = b["high"] if b["type"] == "top" else b["low"]
        isUp = b["type"] == "top"
        bis.append({
            "type": "up" if isUp else "down",
            "startIdx": a["mergedIdx"],
            "endIdx": b["mergedIdx"],
            "startTime": a["time"],
            "endTime": b["time"],
            "startPrice": startPrice,
            "endPrice": endPrice,
            "rawCount": countRaw(merged, a["mergedIdx"], b["mergedIdx"]),
            "span": abs(endPrice - startPrice),
            "gapLocked": b.get("gapLocked", False) is True,
            "macdCross": b.get("macdCross", False) is True,
        })
    return bis


# ============================================================
# 4.1 端点极值修正
# ============================================================


def fixBiExtremes(bis, merged):
    """端点极值修正：包含关系合并时（如向上合并取「高高」会把更低的插针低点抬高，
    向下合并取「低低」会把更高的插针高点压低），笔的端点分型可能不是该区域内的真实极值。
    对每笔检查「终点分型之后、下一笔终点分型之前」的合并K线，若存在「被包含合并掩盖」
    （rawLow<low / rawHigh>high）且比当前端点更极端的真实极值，把本笔终点与下一笔起点
    同步平移到该极值所在K线（保持首尾连续）。只处理被掩盖的极值。
    跳空独立成笔（gapLocked）端点固定在缺口处，不参与修正。原地修改并返回 bis。"""
    if not bis or len(bis) == 0 or not merged or len(merged) == 0:
        return bis
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
        b["rawCount"] = countRaw(merged, b["startIdx"], b["endIdx"])
        # 下一笔起点联动（保持两笔端点连续）
        next_["startPrice"] = extreme["price"]
        next_["startTime"] = extreme["time"]
        next_["startIdx"] = extreme["idx"]
        next_["span"] = next_["endPrice"] - next_["startPrice"] if next_["type"] == "up" else next_["startPrice"] - next_["endPrice"]
        next_["rawCount"] = countRaw(merged, next_["startIdx"], next_["endIdx"])
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
    至少 5 笔才画中枢（3~4 笔的中枢强度不足，不输出，但保留扫描逻辑）。
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
            # 至少 5 笔才画中枢（3~4 笔的基础重叠结构强度不足，不输出；保留扫描逻辑）
            if biCount < 5:
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
    last_u = upperBis[len(upperBis) - 1]
    segments = []
    cur = None  # { upper, bis }
    for b in lowerBis:
        ub = None
        for u in upperBis:
            end_ok = b["endTime"] <= u["endTime"] + tol or (open_last and u is last_u)
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
# （百万级调用下 key 回调本身即成热点）。缓存持强引用并以长度校验失效（追加会使长度变化）。
_macdTimesCache = (None, 0, None)


def _macdTimesOf(macdArr):
    global _macdTimesCache
    ref, n, times = _macdTimesCache
    if ref is macdArr and n == len(macdArr):
        return times
    times = [m["time"] for m in macdArr]
    _macdTimesCache = (macdArr, len(macdArr), times)
    return times


def biMacdMetrics(bi, macdArr):
    """计算一笔区间内的 MACD 动能指标
    { redArea, greenArea, difHigh, difLow, redMax, greenMax }。
    与 JS 版一致：redMax=单根红柱最大高度、greenMax=单根绿柱最大绝对值。
    性能：macdArr 按时间升序，用 bisect 定位 [t0,t1] 窗口（闭区间）后再累加，
    替代从头线性扫描——回测链路每次重算会调用本函数上万次，长窗口下线性扫是主要热点。"""
    metrics = {"redArea": 0.0, "greenArea": 0.0, "difHigh": float("-inf"),
               "difLow": float("inf"), "redMax": 0.0, "greenMax": 0.0}
    if not macdArr or len(macdArr) == 0:
        return None
    t0 = bi["startTime"]
    t1 = bi["endTime"]
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
    """判断本周期某笔是否与上一级别某笔完全重合（时间容差 = 本周期 1 个 bar）。"""
    if not upperBis or len(upperBis) == 0:
        return False
    tEps = barSec if barSec else 900
    pEps = 0.01
    for ub in upperBis:
        if ub["type"] != bi["type"]:
            continue
        if abs(ub["startTime"] - bi["startTime"]) <= tEps and \
           abs(ub["endTime"] - bi["endTime"]) <= tEps and \
           abs(ub["startPrice"] - bi["startPrice"]) <= pEps and \
           abs(ub["endPrice"] - bi["endPrice"]) <= pEps:
            return True
    return False


def anchorFirstBuy(cand, upperBis):
    """一买锚定：取候选一买之前最近的上级底部端点。"""
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


def anchorFirstSell(cand, upperBis):
    """一卖锚定：候选在上级上涨笔内则上移到其结束点，否则取最近上级顶部端点。"""
    if not upperBis or len(upperBis) == 0:
        return None
    for b in upperBis:
        if b["type"] != "up":
            continue
        if b["startTime"] <= cand["time"] and b["endTime"] >= cand["time"]:
            return {"time": b["endTime"], "price": b["endPrice"]}
    best = None
    for b in upperBis:
        t = b["endTime"] if b["type"] == "up" else b["startTime"]
        p = b["endPrice"] if b["type"] == "up" else b["startPrice"]
        if t > cand["time"]:
            continue
        if best is None or cand["time"] - t < cand["time"] - best["time"]:
            best = {"time": t, "price": p}
    return best


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


def findBuyPoints(bis, upperBis, macdArr, barSec):
    """买点识别（含区间套与 MACD 背驰）。"""
    if len(bis) < 3:
        return []
    downIdx = [i for i, b in enumerate(bis) if b["type"] == "down"]
    # 性能预计算（回测链路每次重算都会调用本函数，逐根全量扫描是长窗口热点）：
    #   downLows/downTimes：down 笔端点按时间升序（bis 有序），2买 区间套按上级笔时间段
    #     bisect 取窗，替代对全部笔的逐根扫描（选出的集合与顺序和原逐根过滤完全一致）；
    #   idxByEndTime：endTime → 首次出现下标（与 _findIndex 等值查找的首个匹配语义一致）；
    #   upperByType：上级笔按类型分组（isSameAsUpperBi 内部本来就跳过异类型笔）。
    downLows = [(i, bis[i]["endTime"], bis[i]["endPrice"]) for i in downIdx]
    downTimes = [t for _, t, _ in downLows]
    idxByEndTime = {}
    for i, b in enumerate(bis):
        if b["endTime"] not in idxByEndTime:
            idxByEndTime[b["endTime"]] = i
    upperByType = None
    if upperBis:
        upperByType = {"up": [u for u in upperBis if u["type"] == "up"],
                       "down": [u for u in upperBis if u["type"] == "down"]}

    # 候选一买：创新低 + MACD 背驰
    firstBuys = []
    for k in range(1, len(downIdx)):
        cur = bis[downIdx[k]]
        if upperByType is not None and isSameAsUpperBi(cur, upperByType.get(cur["type"]) or [], barSec):
            if CHAN_CFG["debug"]:
                print(f"[一买跳过-与上级笔重合] {fmtT(cur['endTime'])}({cur['endPrice']}) 整笔与上一级别完全重合，本周期不标记")
            continue
        refer = None
        for j in range(k - 1, -1, -1):
            cand = bis[downIdx[j]]
            if cand["span"] < cur["span"] * 0.5:
                continue
            refer = cand
            break
        if refer is not None and cur["endPrice"] < refer["endPrice"]:
            diverge = isBiDiverge(cur, refer, macdArr)
            if CHAN_CFG["debug"]:
                cm = biMacdMetrics(cur, macdArr)
                rm = biMacdMetrics(refer, macdArr)
                print(
                    f"[一买候选] {fmtT(cur['endTime'])}({cur['endPrice']}) vs 参照 {fmtT(refer['endTime'])}({refer['endPrice']}) "
                    f"| 创新低={cur['endPrice'] < refer['endPrice']} "
                    f"| 绿柱面积 {cm['greenArea']:.2f} vs {rm['greenArea']:.2f} "
                    f"| DIF低点 {cm['difLow']:.3f} vs {rm['difLow']:.3f} | 背驰={diverge}"
                )
            if diverge:
                firstBuys.append({"biIdx": downIdx[k], "time": cur["endTime"], "price": cur["endPrice"]})
    firstBuy = firstBuys[-1] if len(firstBuys) > 0 else None

    points = []

    # 2买 / 类2买（区间套）
    if upperBis is not None and len(upperBis) > 0:
        for up in upperBis:
            if up["type"] != "up":
                continue
            lo = bisect.bisect_left(downTimes, up["startTime"])
            hi = bisect.bisect_right(downTimes, up["endTime"] + 1)
            if lo >= hi:
                continue
            lows = [{"biIdx": i, "time": t, "price": p} for i, t, p in downLows[lo:hi]]
            lows.sort(key=lambda x: x["time"])  # 已升序，保留与原实现一致的显式排序
            firstLow = next((l for l in lows if l["price"] > up["startPrice"]), None)
            if firstLow is not None:
                points.append({"type": "2买", "time": firstLow["time"], "price": firstLow["price"]})
                laterHigh = next((l for l in lows if l["time"] > firstLow["time"] and l["price"] > firstLow["price"]), None)
                if laterHigh is not None:
                    points.append({"type": "类2买", "time": laterHigh["time"], "price": laterHigh["price"]})
    else:
        # 结构底
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
                classSecond = None
                for i in range(secondBuy["biIdx"] + 1, len(bis)):
                    if bis[i]["type"] != "down":
                        continue
                    if bis[i]["endPrice"] > secondBuy["price"]:
                        classSecond = {"time": bis[i]["endTime"], "price": bis[i]["endPrice"]}
                        break
                if classSecond is not None:
                    points.append({"type": "类2买", "time": classSecond["time"], "price": classSecond["price"]})

    # 1买：所有 MACD 背驰底
    for fb in firstBuys:
        points.append({"type": "1买", "time": fb["time"], "price": fb["price"]})

    # 3买
    twoBuys = sorted([p for p in points if p["type"] == "2买"], key=lambda x: x["time"])
    thirdBuys = []
    for k in range(len(twoBuys)):
        tb = twoBuys[k]
        twoIdx = idxByEndTime.get(tb["time"], -1)
        if twoIdx < 0:
            continue
        if k + 1 < len(twoBuys):
            endScan = idxByEndTime.get(twoBuys[k + 1]["time"], -1)
        else:
            endScan = len(bis)
        prevTop = None
        for j in range(twoIdx - 1, -1, -1):
            if bis[j]["type"] == "up":
                prevTop = bis[j]["endPrice"]
                break
        if prevTop is None:
            continue
        lastValid = None
        for i in range(twoIdx + 1, endScan):
            if bis[i]["type"] != "up":
                continue
            if bis[i]["endPrice"] <= prevTop:
                continue
            for mm in range(i + 1, endScan):
                if bis[mm]["type"] != "down":
                    continue
                bt = bis[mm]["endTime"]
                bp = bis[mm]["endPrice"]
                if bp > prevTop:
                    inUp = True
                    if upperBis is not None and len(upperBis) > 0:
                        inUp = False
                        for up in upperBis:
                            if up["type"] == "up" and bt >= up["startTime"] and bt <= up["endTime"] and bp > up["startPrice"]:
                                inUp = True
                                break
                    if inUp:
                        lastValid = {"time": bt, "price": bp}
                break
        if lastValid is not None:
            thirdBuys.append(lastValid)
    # 按时间去重后加入
    for t in thirdBuys:
        if any(p["type"] == "3买" and p["time"] == t["time"] for p in points):
            continue
        dup = _findIndex(points, lambda p: p["type"] == "类2买" and p["time"] == t["time"])
        if dup >= 0:
            del points[dup]
        points.append({"type": "3买", "time": t["time"], "price": t["price"]})
    return points


def findSellPoints(bis, upperBis, macdArr, barSec):
    """卖点识别（含区间套与 MACD 背驰，与买点对称）。"""
    if len(bis) < 3:
        return []
    upIdx = [i for i, b in enumerate(bis) if b["type"] == "up"]
    # 性能预计算（与 findBuyPoints 对称）：up 笔端点按时间升序供 2卖 区间套 bisect 取窗、
    # endTime 首次出现下标字典、上级笔按类型分组。
    upHighs = [(i, bis[i]["endTime"], bis[i]["endPrice"]) for i in upIdx]
    upTimes = [t for _, t, _ in upHighs]
    idxByEndTime = {}
    for i, b in enumerate(bis):
        if b["endTime"] not in idxByEndTime:
            idxByEndTime[b["endTime"]] = i
    upperByType = None
    if upperBis:
        upperByType = {"up": [u for u in upperBis if u["type"] == "up"],
                       "down": [u for u in upperBis if u["type"] == "down"]}

    # 候选一卖：创新高 + MACD 背驰
    firstSells = []
    for k in range(1, len(upIdx)):
        cur = bis[upIdx[k]]
        if upperByType is not None and isSameAsUpperBi(cur, upperByType.get(cur["type"]) or [], barSec):
            if CHAN_CFG["debug"]:
                print(f"[一卖跳过-与上级笔重合] {fmtT(cur['endTime'])}({cur['endPrice']}) 整笔与上一级别完全重合，本周期不标记")
            continue
        refer = None
        for j in range(k - 1, -1, -1):
            cand = bis[upIdx[j]]
            if cand["span"] < cur["span"] * 0.5:
                continue
            refer = cand
            break
        if refer is not None and cur["endPrice"] > refer["endPrice"]:
            diverge = isBiDiverge(cur, refer, macdArr)
            if CHAN_CFG["debug"]:
                cm = biMacdMetrics(cur, macdArr)
                rm = biMacdMetrics(refer, macdArr)
                print(
                    f"[一卖候选] {fmtT(cur['endTime'])}({cur['endPrice']}) vs 参照 {fmtT(refer['endTime'])}({refer['endPrice']}) "
                    f"| 创新高={cur['endPrice'] > refer['endPrice']} "
                    f"| 红柱面积 {cm['redArea']:.2f} vs {rm['redArea']:.2f} "
                    f"| DIF高点 {cm['difHigh']:.3f} vs {rm['difHigh']:.3f} | 背驰={diverge}"
                )
            if diverge:
                firstSells.append({"biIdx": upIdx[k], "time": cur["endTime"], "price": cur["endPrice"]})
    firstSell = firstSells[-1] if len(firstSells) > 0 else None

    # 1卖 锚定：对每一个候选一卖都做锚定，全部保留；去重
    anchoredSells = []
    seenSellPos = set()
    for fs in firstSells:
        anchored = fs
        if upperBis is not None and len(upperBis) > 0:
            a = anchorFirstSell(fs, upperBis)
            if a is not None:
                bestBi = None
                bestDist = float("inf")
                for i, b in enumerate(bis):
                    if b["type"] != "up":
                        continue
                    d = abs(b["endTime"] - a["time"])
                    if d < bestDist:
                        bestDist = d
                        bestBi = i
                anchored = {
                    "biIdx": bestBi if bestBi is not None else fs["biIdx"],
                    "time": a["time"],
                    "price": a["price"],
                }
        if anchored["time"] in seenSellPos:
            continue
        seenSellPos.add(anchored["time"])
        anchoredSells.append(anchored)

    points = []

    # 2卖 / 类2卖（区间套）
    if upperBis is not None and len(upperBis) > 0:
        for dn in upperBis:
            if dn["type"] != "down":
                continue
            lo = bisect.bisect_left(upTimes, dn["startTime"])
            hi = bisect.bisect_right(upTimes, dn["endTime"] + 1)
            if lo >= hi:
                continue
            highs = [{"biIdx": i, "time": t, "price": p} for i, t, p in upHighs[lo:hi]]
            highs.sort(key=lambda x: x["time"])  # 已升序，保留与原实现一致的显式排序
            firstHigh = next((h for h in highs if h["price"] < dn["startPrice"]), None)
            if firstHigh is not None:
                points.append({"type": "2卖", "time": firstHigh["time"], "price": firstHigh["price"]})
                laterLow = next((h for h in highs if h["time"] > firstHigh["time"] and h["price"] < firstHigh["price"]), None)
                if laterLow is not None:
                    points.append({"type": "类2卖", "time": laterLow["time"], "price": laterLow["price"]})
    else:
        # 结构顶
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
                classSecond = None
                for i in range(secondSell["biIdx"] + 1, len(bis)):
                    if bis[i]["type"] != "up":
                        continue
                    if bis[i]["endPrice"] < secondSell["price"]:
                        classSecond = {"time": bis[i]["endTime"], "price": bis[i]["endPrice"]}
                        break
                if classSecond is not None:
                    points.append({"type": "类2卖", "time": classSecond["time"], "price": classSecond["price"]})

    # 1卖：所有 MACD 背驰顶（锚定到上级上涨笔结束点）
    for as_ in anchoredSells:
        points.append({"type": "1卖", "time": as_["time"], "price": as_["price"]})

    # 3卖
    twoSells = sorted([p for p in points if p["type"] == "2卖"], key=lambda x: x["time"])
    thirdSells = []
    for k in range(len(twoSells)):
        ts = twoSells[k]
        twoIdx = idxByEndTime.get(ts["time"], -1)
        if twoIdx < 0:
            continue
        if k + 1 < len(twoSells):
            endScan = idxByEndTime.get(twoSells[k + 1]["time"], -1)
        else:
            endScan = len(bis)
        prevLow = None
        for j in range(twoIdx - 1, -1, -1):
            if bis[j]["type"] == "down":
                prevLow = bis[j]["endPrice"]
                break
        if prevLow is None:
            continue
        lastValid = None
        for i in range(twoIdx + 1, endScan):
            if bis[i]["type"] != "down":
                continue
            if bis[i]["endPrice"] >= prevLow:
                continue
            for mm in range(i + 1, endScan):
                if bis[mm]["type"] != "up":
                    continue
                st = bis[mm]["endTime"]
                sp = bis[mm]["endPrice"]
                if sp < prevLow:
                    inDown = True
                    if upperBis is not None and len(upperBis) > 0:
                        inDown = False
                        for dn in upperBis:
                            if dn["type"] == "down" and st >= dn["startTime"] and st <= dn["endTime"] and sp < dn["startPrice"]:
                                inDown = True
                                break
                    if inDown:
                        lastValid = {"time": st, "price": sp}
                break
        if lastValid is not None:
            thirdSells.append(lastValid)
    # 按时间去重后加入
    for t in thirdSells:
        if any(p["type"] == "3卖" and p["time"] == t["time"] for p in points):
            continue
        dup = _findIndex(points, lambda p: p["type"] == "类2卖" and p["time"] == t["time"])
        if dup >= 0:
            del points[dup]
        points.append({"type": "3卖", "time": t["time"], "price": t["price"]})
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
