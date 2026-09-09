# -*- coding: utf-8 -*-
"""
点状回测引擎（Python 移植版，与 plan 中 backtest 设计对齐）

在最小周期（默认 3 分钟）上逐根 K 线推进，每根收盘后以「截至该时刻的切片」
重放整条链路：画笔(buildBi) → 买卖点(compute_all_marks) → 支阻位(compute_srflip)
→ 交易计划(compute_plan) → 进出场(compute_entries)，避免未来函数。

成交规则（fill_mode，BacktestEngine.__init__）：
  - "anchor"（默认）：信号在「背驰锚点时间之后的第 1 根 fine 周期K线」开盘成交——
    锚点当拍不可知（需后续反向笔确认），含未来函数、回测偏理想化，实盘不可复现；
  - "confirm"：信号在「收集拍的下一根K线」开盘成交（无偷价、无未来函数）；
  - 旧锚点保护：anchor 下锚点距收集时刻 > 检测周期 1 根 bar → 回落 confirm（fillMode=confirm-stale-anchor）。

出场规则（三模式统一口径，模块级 advance_exit_decision / execute_pending_exit
为唯一实现源；mark_entry.stop_ref_of/find_bi_event/forming_seg_ready/trend_following_of）：
  - 触发判定在「已收盘 bar」进行（bar 完整 high/low 判止损穿越；三档止盈用
    endTime ≤ 收盘时刻的已确认笔 / 检测周期形成段合并K线计数）；
  - 成交统一「下一根K线开盘」：止损/保本止损/平一半/全平的事件时间 = 触发 bar 的
    下一根 bar 时间、价格 = 其开盘价（跳空自然体现）；触发 bar 无下一根 → 未成交
    （持仓保持 open，mark-to-market 收尾）；
  - 止损位：正确侧最近支阻位 ± slip_stop（short 上方+ / long 下方−）；无正确侧位兜底
    进场价 ± slip_fallback（止损位永不为 None，不再有「不设止损」仓位）；
  - 保本止损位 beStop：进场成交K线极值 ± slip_be（short: high+ / long: low−；
    run() 批量路径成交 bar 当拍未收盘，存在 ≤1 根 fine bar 的微前视，step_to 实时
    路径无前视——研究口径可接受）；
  - 止盈1 保本：背驰周期（markRes）够笔（进场后首笔有利方向笔完成）→ 止损位上移至
    beStop（状态迁移，当拍生效、事件仅落盘）；
  - 止盈2 平一半（仅顺势：计划 direction ∈ {多头多, 空头空}）：检测周期首个有利方向、
    合并后 ≥5 根K且有成笔预期的形成段 → 下一开盘平一半，剩余半仓止损移至 beStop
    （不要求保本先触发）；
  - 止盈3 全平：顺势 = 检测周期有利方向笔破前高/前低（breakPrev）；逆势（多头空/空头多）
    = 检测周期首个有利方向形成段（合并后 ≥5 根K成笔预期）→ 下一开盘全平；
  - stopSr/stopBe：盘中破坏止损位 / 保本位 beStop；
  - 同向持仓互斥：同方向持仓未终局时新信号不成交（on_suppressed 回调）；多空互不影响；
  - 已平仓盈亏 =（TP2 半仓价 + 终局价各 0.5，未到 TP2 全量终局价，减进场价）× 方向 × lots
    （手数默认 4，参数化），未平仓仍按最新收盘价 mark-to-market × lots。
  - run() 与 step_to(execute=True) 同一套逐根逻辑（实时监控/回放从此也有成交与出场；
    step_to(execute=False) 仅预热推进）。

为避免逐根重复计算，链路只在某周期新增K线（bis 变化）时重算，
MACD / ATR 在各周期切片变化时缓存。注意：本引擎的判定结果是「研究用近似」，
与 JS 端在实时画图上的输出在边界处可能略有差异，但满足点状重放语义。
"""

import bisect
import time

from .chan_core import (
    buildBi, fixBiExtremes, calcATR, calcMACD, intervalSecOf, fmtT,
    CHAN_CFG,
    MacdAccumulator, AtrAccumulator, extendLastBi, extendLastBiFrom,
)
from .mark_buy_sell import compute_all_marks
from .sr_flip import compute_srflip, prepare_bar_arrays
from .trading_plan import compute_plan
from .mark_entry import (
    compute_entries, stop_ref_of, find_bi_event, filterDetectPeriods,
    trend_following_of, forming_seg_ready,
    DEFAULT_LOTS, DEFAULT_SLIP_STOP, DEFAULT_SLIP_FALLBACK, DEFAULT_SLIP_BE,
)

DEFAULT_PERIODS = ["D", "240", "60", "15", "3"]
DEFAULT_WARMUP_BARS = 60
# 批量重同步间隔（fine bar 数）：增量 wick 压平用「已收K线运行 TR 均值」，与全量口径的
# 稳定均值在窗口早期的边界 bar 上可能翻转判定并经包含关系级联放大。每 N 根 fine bar
# 用当前前缀做一次 markWickBars 全量重建（_resync_bis），重同步点上引擎状态严格等于
# batch(前缀)；间隔内的漂移窗口 ≤ N 根，且无未来函数（只用已收盘数据）。
RESYNC_EVERY = 1000

# ============================================================
# 出场状态机（三模式统一口径：收盘判定 → 下一根开盘成交）
# ============================================================


def advance_exit_decision(pos, t, bar, mark_bis, px_bis, px_merged_times=None):
    """出场判定（纯函数，三模式共用）——在「已收盘 bar」上判定一次。

    统一语义（出场阶梯重构 2026-09-09：止损±滑点+兜底 / beStop / 顺势逆势分支）：
      - 用 bar 完整 high/low 判止损/保本止损穿越（保本位 = beStop，非进场价）；
      - TP1 用 markRes 已确认笔（endTime <= t）；TP3a 用 periodX 已确认笔（有利方向
        breakPrev）；TP2/逆势TP3b 用检测周期形成段「合并后≥5根K成笔预期」
        （forming_seg_ready，px_merged_times 缺省 None 时跳过形成段判定）；
      - breakeven：仅状态迁移（止损位 → beStop），当拍生效、事件仅落盘；
      - half/close/stopSr/stopBe：成交型事件——只把 pos['pendingExit'] 挂起，
        由 execute_pending_exit 在「下一根K线开盘」执行成交（同一拍只挂一个，
        逐拍执行后继续判定）。
    @param t      决策时刻 = bar 收盘时刻（下一根开盘时刻）
    @param bar    该根已收盘 K线（{time,open,high,low,close}）
    @param mark_bis / px_bis  背驰级别 / 检测周期笔快照（endTime ≤ t 已含）
    @param px_merged_times    检测周期合并K线块截止时间数组（升序；None 跳过形成段判定）
    @returns 挂起类型（"half"/"close"/"stopSr"/"stopBe"）或 None（含仅 breakeven）
    """
    if pos.get("pendingExit"):
        return None  # 已挂起等下一开盘，不再重复判定
    is_short = pos["direction"] == "short"
    fav = "down" if is_short else "up"    # 有利方向笔（short 盼下跌 / long 盼上涨）
    trend = trend_following_of(pos.get("planDirection"), pos.get("strategyKey"))
    tp1 = find_bi_event(mark_bis, pos["signalTime"], fav)
    tp3a = find_bi_event(px_bis, pos["signalTime"], fav, break_prev=True) if trend else None
    seg5 = forming_seg_ready(px_bis, px_merged_times, is_short) if px_merged_times else False
    # 同一拍顺序：保本 → 半平 → 全平 → 止损（逐拍各挂一个）
    if tp1 and tp1["time"] <= t and not pos.get("beDone"):
        pos["beDone"] = True
        pos["exits"].append({"type": "breakeven", "time": tp1["time"], "price": tp1["price"]})
    if trend and seg5 and not pos.get("halfDone"):
        # TP2 平一半（仅顺势）：形成段成笔预期即触发，不要求保本先触发
        pos["halfDone"] = True
        pos["pendingExit"] = "half"
        return "half"
    if (tp3a and tp3a["time"] <= t) or ((not trend) and seg5):
        # TP3 全平：顺势=有利方向笔破前高/前低；逆势=形成段成笔预期快速离场
        pos["pendingExit"] = "close"
        return "close"
    stop = pos.get("beStop") if pos.get("beDone") else pos.get("stopRef")
    if stop is not None:  # 止损位永不为 None（兜底进场价±slip_fallback），此保护仅防御旧持仓数据
        hit = bar["high"] > stop if is_short else bar["low"] < stop
        if hit:
            typ = "stopBe" if pos.get("beDone") else "stopSr"
            pos["pendingExit"] = typ
            return typ
    return None


def execute_pending_exit(pos, exec_bar):
    """执行挂起的出场：在下一根K线开盘成交。

    @param exec_bar  下一根K线（{time, open, ...}）
    @returns 终局的 trade（close/stopSr/stopBe）或 None（half 平一半后续继续）
    """
    et = pos.get("pendingExit")
    if et is None:
        return None
    pos["pendingExit"] = None
    pos["exits"].append({"type": et, "time": exec_bar["time"], "price": exec_bar["open"]})
    if et == "half":
        # 剩余半仓止损移至保本位 beStop（后续打止损记 stopBe；若 TP1 尚未发生过，
        # breakeven 事件不补记——half 本身已是状态迁移）
        pos["beDone"] = True
        return None
    return close_trade(pos, et, exec_bar["time"], exec_bar["open"])


def close_trade(pos, exit_type, exit_time, exit_price):
    """标记持仓终局并结算盈亏（半仓按 half 事件价加权，整体 × lots 手数）。"""
    pos["state"] = "closed"
    pos["exitType"] = exit_type
    pos["exitTime"] = exit_time
    pos["exitPrice"] = exit_price
    d = 1 if pos["direction"] == "long" else -1
    entry = pos["entryPrice"]
    lots = pos.get("lots", 1)
    half_ev = next((e for e in pos.get("exits", []) if e["type"] == "half"), None)
    if half_ev:
        pos["pnl"] = (0.5 * (half_ev["price"] - entry) + 0.5 * (exit_price - entry)) * d * lots
    else:
        pos["pnl"] = (exit_price - entry) * d * lots
    return pos


class BacktestEngine:
    """点状回测引擎：逐最小周期K线重放整条链路（增量实现）。

    每根K线推进时只做增量计算：
      - 各周期 merged（包含合并）用 _mergeStep 逐根追加（O(1)）；
      - 分型用 updateFractalsTail 只重算尾部（O(1)），仅当分型变化时才从零重建笔；
      - MACD / ATR 用增量累加器（O(1)），并把预计算结果传给链路函数，避免每步重算；
      - 最后一笔用 extendLastBi 延伸到最新极端价（与 chan-bi JS 落盘数据一致）。
    链路（支阻位 → 交易计划 → 进出场）每根K线都按「截至该时刻的切片」重算，
    保证点状重放语义（无未来函数）。买卖点（marks）阶段由计划/进出场内部的
    findBuyPoints/findSellPoints 语义重放，默认不再单独计算（--with-marks 可开启）。
    """

    def __init__(self, bars_by_period, periods=None, warmup_bars=DEFAULT_WARMUP_BARS,
                 with_marks=False, cfg=None, fill_mode="anchor", signal_mode="realtime",
                 sr_types=None, fib_levels=None, boll_length=None, boll_mult=None,
                 lots=DEFAULT_LOTS, slip_stop=DEFAULT_SLIP_STOP,
                 slip_fallback=DEFAULT_SLIP_FALLBACK, slip_be=DEFAULT_SLIP_BE):
        self.periods = list(periods or DEFAULT_PERIODS)
        # 各周期按时间升序整理 + 缓存时间数组
        self.bars = {}
        self._times = {}
        for res in self.periods:
            bl = sorted(bars_by_period.get(res, []) or [], key=lambda x: x["time"])
            self.bars[res] = {"_list": bl, "_times": [b["time"] for b in bl]}
            self._times[res] = self.bars[res]["_times"]
        # 最小周期（细分周期）逐根推进：覆盖感知——按周期间隔从细到粗取第一个
        # 「数据跨度 ≥ 全部周期最大跨度 × 0.9」的周期。30S 等历史深度有限的周期
        # （实测仅约 6 小时/最近3天）不承担回测时间轴，只作低级别背驰候选；
        # 若回测区间就在其覆盖内（如只测最近几小时），它仍会成为时间轴保持细粒度。
        spans = {}
        for res in self.periods:
            bl = self.bars[res]["_list"]
            if len(bl) >= 2:
                spans[res] = bl[-1]["time"] - bl[0]["time"]
        max_span = max(spans.values()) if spans else 0
        fine_res = None
        for res in sorted(self.periods, key=lambda r: intervalSecOf(r) or 0):
            if spans.get(res, 0) >= max_span * 0.9:
                fine_res = res
                break
        if fine_res is None:
            fine_res = min(self.periods, key=lambda r: intervalSecOf(r) or 0)
        self.fine_res = fine_res
        if not self.bars[self.fine_res]["_list"]:
            raise ValueError(f"最小周期 {self.fine_res} 无K线数据")
        self.warmup_bars = warmup_bars
        self.with_marks = with_marks
        # 支阻位类型开关（None → compute_srflip 默认 cluster+boll）；
        # --sr-types=cluster 可复现黄金分割加入前的旧回测结果
        self.sr_types = tuple(sr_types) if sr_types else None
        self.fib_levels = fib_levels
        self.boll_length = boll_length
        self.boll_mult = boll_mult
        self.cfg = cfg or {}
        # 信号模式：
        #   "confirm"（确认制）：只在笔结构变化时收集信号（等检测周期笔端点确认后
        #     回头找已完成低级别背驰笔，实测延迟可达 1-2 小时）；
        #   "realtime"（当下背驰，默认）：每根 fine 收盘评估——低级别形成中段创新低/新高
        #     + 当拍 MACD 对比弱于参照笔即出信号（背驰判断不等反向笔确认，交易基于当下）。
        #     ①够笔③支阻位所需的交易计划/支阻位仍在笔结构变化时重算并缓存（控性能）。
        if signal_mode not in ("confirm", "realtime"):
            raise ValueError(f"未知 signal_mode: {signal_mode}")
        self.signal_mode = signal_mode
        # 成交口径：
        #   "anchor"（默认）：信号在「背驰锚点时间之后的第 1 根 fine 周期K线」开盘成交——
        #     锚点（背驰笔终点 bar）的下一根同周期K线即成交，消除确认延迟。
        #     注意：锚点在其当拍不可知（需后续反向笔确认），该口径含未来函数、
        #     回测结果偏理想化，实盘/实时监控不可复现该成交价（step_to 维持确认成交）。
        #   "confirm"：信号在「引擎收集到信号那一拍的下一根 fine K线」开盘成交（原行为，
        #     无未来函数）。
        #   旧锚点保护：anchor 模式下锚点距收集时刻超过其检测周期 1 根 bar 长度
        #     （intervalSecOf(periodX)，锚点明显过时——如「校验失败回退次新」选中的旧点）
        #     时，该笔回落 confirm 口径，fillMode 记 "confirm-stale-anchor"。
        self.fill_mode = fill_mode
        # 出场参数（2026-09-09 出场阶梯重构）：
        #   lots 手数（盈亏 × lots）；slip_stop 止损位滑点（支阻位外侧）；
        #   slip_fallback 兜底止损滑点（无正确侧支阻位 → 进场价±该值）；
        #   slip_be 保本滑点（beStop = 进场成交K线极值 ± 该值）
        self.lots = lots
        self.slip_stop = slip_stop
        self.slip_fallback = slip_fallback
        self.slip_be = slip_be

        # 增量状态
        self._cut = {res: 0 for res in self.periods}
        self._merged = {res: [] for res in self.periods}
        # 合并块截止时间数组（与 _merged 平行：块 .time = 覆盖的最后一根原始K线时间，
        # 严格递增）——出场 TP2/逆势TP3b 的形成段「合并后≥5根K」计数锚定用
        # （forming_seg_ready 按末笔 endTime 二分定位，不能用 endIdx——延伸不更新它）
        self._merged_times = {res: [] for res in self.periods}
        self._merge_dir = {res: 0 for res in self.periods}
        self._fractals = {res: [] for res in self.periods}
        self._bis = {res: [] for res in self.periods}
        self._macd = {res: MacdAccumulator() for res in self.periods}
        self._macd_times = {res: [] for res in self.periods}  # 与 macd.entries 一一对应（切片二分用）
        self._atr = {res: AtrAccumulator(14) for res in self.periods}
        # 长影预处理增量状态（markWickBars 的逐根版，见 _wick_process）：
        # prev/prev2=最近两根原始bar（延迟判 _topCand 的左右邻）、trSum/trCnt/prevClose=
        # 运行TR均值、pending=上一根压平后的bar
        self._wick = {res: {"prev": None, "prev2": None, "trSum": 0.0, "trCnt": 0,
                            "prevClose": None, "pending": None} for res in self.periods}
        self._trimmed = {res: [] for res in self.periods}  # 压平后K线（merge/延伸用，与原始bar一一对应）
        self._marks = {}
        self._sr = None
        self._plan = {}
        self._entries = {}
        # 当下背驰去重：(periodX, strategyKey, markRes, 形成段起点时间)，每个形成段只发一次
        self._rt_fired = set()
        # 批量重同步水位（fine 周期 cut 达到 last+RESYNC_EVERY 时全周期重同步）
        self._last_resync = 0

        # 实时监控状态（step_to 使用）：跨轮询保持信号去重与统计
        self._live_st = None          # step_to(execute=True) 持久状态（见 _step_execute）
        self._replay_needed = set()   # 需要整周期重放修正增量状态的周期（实时bar被覆盖）
        self._live_allSignals = {}
        self._live_seen = set()
        self._live_stats = {"steps": 0, "signals": 0, "executed": 0,
                            "long": 0, "short": 0, "markRes": {}, "strategyKeys": {}}

    # ---------------- 增量计算 ----------------

    def _wick_process(self, res, bar):
        """长影预处理（markWickBars 的逐根增量版，与图表批量版逐条对齐）：

        - 压平判定自足（影线占比 ≥ wickRatio 且 ≥ wickAtrK×运行TR均值），新 bar 到达即判、
          立即生效；长上影 high 压平至实体顶，长下影 low 压平至实体底并记 _origLow。
        - _topCand 需右邻原始低点，延后一根判（图表批量版同样无法给最后一根判 _topCand——
          无 next）：下一根到达时若「low 不低于左右相邻原始K线低点」，把 _topCand=原 high
          回标到 merged 尾巴（带 > 传播守卫，与 _mergeStep 传播规则一致）。
        - 运行 TR 均值 = 已收K线的全量均值（含当前根 TR），与批量版「全窗口含末根」同构，
          只随窗口增长渐稳、不随局部行情抖动。

        返回压平后的 bar（喂 _mergeStep 与延伸）；原始 bar 由调用方喂 MACD/ATR 累加器。"""
        w = self._wick[res]
        merged = self._merged[res]
        # 1) 运行 TR 均值（先含当前根 TR，再判当前根——与批量版口径一致）
        if w["prevClose"] is not None:
            tr = max(bar["high"] - bar["low"],
                     abs(bar["high"] - w["prevClose"]),
                     abs(bar["low"] - w["prevClose"]))
            w["trSum"] += tr
            w["trCnt"] += 1
        w["prevClose"] = bar["close"]
        min_wick = (w["trSum"] / w["trCnt"]) * CHAN_CFG["wickAtrK"] if w["trCnt"] else 0.0
        # 2) 上一根的延迟 _topCand 判定：左右邻原始低点现已齐全（pend.low 未被上影压平改动）
        pend = w["pending"]
        if pend is not None and pend.get("_wantTopCand"):
            prev2 = w["prev2"]  # pend 的左邻原始 bar
            if prev2 is not None and merged and \
                    pend["low"] >= prev2["low"] and pend["low"] >= bar["low"]:
                top_cand = pend["_origHigh"]
                last = merged[-1]
                if top_cand > last.get("_topCand", 0):
                    last["_topCand"] = top_cand
                    last["_topCandTime"] = pend["time"]
        # 3) 当前 bar 压平判定（自足）
        b = dict(bar)
        amp = b["high"] - b["low"]
        if amp > 0:
            body_top = max(b["open"], b["close"])
            body_bottom = min(b["open"], b["close"])
            upper = b["high"] - body_top
            lower = body_bottom - b["low"]
            if upper >= CHAN_CFG["wickRatio"] * amp and upper >= min_wick:
                # 长上影：先压平 + 记候选（右邻未知），右邻到达时满足 low 条件再回标 _topCand
                b["_origHigh"] = b["high"]
                b["_wantTopCand"] = True
                b["high"] = body_top
            elif lower >= CHAN_CFG["wickRatio"] * amp and lower >= min_wick:
                b["_origLow"] = b["low"]
                b["_origLowTime"] = b["time"]
                b["low"] = body_bottom
        w["prev2"] = w["prev"]
        w["prev"] = bar
        w["pending"] = b
        return b

    def _append_bars(self, res, new_bars):
        """把 res 周期新增的K线逐根并入增量状态；返回该周期笔结构是否变化（新分型或延伸推进）。

        长影预处理先行（_wick_process）：压平后的 bar 才进包含合并与笔延伸；
        MACD/ATR 累加器始终用原始 bar（与 chan-bi：ATR/MACD 基于未剔除的原始K线一致）。"""
        from .chan_core import _mergeStep, updateFractalsTail, extendLastBiFrom
        merged = self._merged[res]
        direction = self._merge_dir[res]
        macd = self._macd[res]
        atr = self._atr[res]
        for bar in new_bars:
            p = self._wick_process(res, bar)
            n0 = len(merged)
            merged, direction = _mergeStep(merged, direction, p)
            # _merged_times 平行维护：新块诞生 append、包含并入更新末元素
            # （_mergeStep 内部对 last["time"] 的赋值与本数组保持同一语义）
            mt = self._merged_times[res]
            if len(merged) > n0:
                mt.append(bar["time"])
            elif mt:
                mt[-1] = bar["time"]
            self._trimmed[res].append(p)
            macd.append(bar)
            self._macd_times[res].append(bar["time"])
            atr.append(bar)
        self._merge_dir[res] = direction
        old_f = self._fractals[res]
        new_f = updateFractalsTail(old_f, merged)
        self._fractals[res] = new_f
        bis_changed = False
        if len(new_f) != len(old_f) or (new_f and old_f and new_f[-1] != old_f[-1]):
            bis_changed = True
        if bis_changed:
            self._bis[res] = self._build_bis(res, merged, new_f, macd.to_list(), atr.value)
        if self._extend_last(res):
            bis_changed = True
        return bis_changed

    def _extend_last(self, res):
        """最后一笔延伸到最新极端价（与 chan-bi 落盘数据一致；用压平后K线，延伸不指向
        已压平的插针价）。返回延伸是否实际推进了端点（供链路短路判断）。"""
        if not self._bis[res]:
            return False
        # 用二分定位最后笔起点在时间轴上的索引（O(log n)），只从该位置起增量扫描，
        # 避免每根K线从 bars 头部全量扫描与整段切片复制导致的 O(n²)
        last_start = self._bis[res][-1].get("startTime")
        start_idx = bisect.bisect_left(self._times[res], last_start) if last_start is not None else 0
        prev_end = (self._bis[res][-1].get("endTime"), self._bis[res][-1].get("endPrice"))
        self._bis[res] = extendLastBiFrom(self._bis[res], self._trimmed[res],
                                          start_idx, endIdx=self._cut[res])
        cur_end = (self._bis[res][-1].get("endTime"), self._bis[res][-1].get("endPrice"))
        return cur_end != prev_end

    def _resync_bis(self, res):
        """批量重同步该周期增量状态（wick/merge/分型/笔全量口径重建，无未来函数）。

        用当前前缀 bars[:cut] 走 markWickBars → mergeBars（_mergeStep 回放）→
        findFractals → buildBi → fixBiExtremes → 延伸，替换全部增量状态——重同步点上
        引擎状态严格等于 batch(前缀)，消除增量 wick 运行均值在窗口早期的阈值漂移
        （见 RESYNC_EVERY 注释）。wick 运行状态（TR 均值/邻居/pending _topCand）同步重建，
        重同步后增量从该前缀无缝继续。"""
        from .chan_core import _mergeStep, markWickBars, findFractals
        cut = self._cut[res]
        raw = self.bars[res]["_list"][:cut]
        trimmed = markWickBars(raw)
        merged = []
        direction = 0
        for p in trimmed:
            merged, direction = _mergeStep(merged, direction, p)
        self._merged[res] = merged
        self._merged_times[res] = [m["time"] for m in merged]
        self._merge_dir[res] = direction
        self._trimmed[res] = list(trimmed)
        self._fractals[res] = findFractals(merged)
        self._bis[res] = self._build_bis(res, merged, self._fractals[res],
                                         self._macd[res].to_list(), self._atr[res].value)
        self._extend_last(res)
        # wick 运行状态重建（与已收前缀一致）
        w = self._wick[res]
        w["trSum"] = 0.0
        w["trCnt"] = 0
        prev_close = None
        for b in raw:
            if prev_close is not None:
                tr = max(b["high"] - b["low"], abs(b["high"] - prev_close), abs(b["low"] - prev_close))
                w["trSum"] += tr
                w["trCnt"] += 1
            prev_close = b["close"]
        w["prevClose"] = prev_close
        w["prev"] = raw[-1] if len(raw) >= 1 else None
        w["prev2"] = raw[-2] if len(raw) >= 2 else None
        # pending 恢复：末根若被上影压平（批量口径），保留 _wantTopCand 语义，
        # 下一根到达时仍走延迟回标通道（批量版末根无 next 同样不判 _topCand，口径一致）
        w["pending"] = None
        if trimmed:
            last_raw = raw[-1]
            last_p = trimmed[-1]
            if last_p["high"] < last_raw["high"]:
                pend = dict(last_p)
                pend["_origHigh"] = last_raw["high"]
                pend["_wantTopCand"] = True
                w["pending"] = pend
            else:
                w["pending"] = dict(last_p)

    def resync_all(self):
        """全部周期批量重同步（run() 收尾前调用，使最终状态严格等于 batch(全前缀)）。"""
        for res in self.periods:
            self._resync_bis(res)
        self._last_resync = self._cut.get(self.fine_res, 0)

    def _build_bis(self, res, merged, fractals, macd, atr):
        """从分型重建笔并做端点极值修正；返回按时间升序的笔列表。
        近等双顶/双底平台取后顶/后底与 chan-bi/build_bis 一致：仅 ≥60m（60/240/D）开启。"""
        from .chan_core import fixBiExtremes
        if len(fractals) < 2:
            return []
        bis = buildBi(fractals, merged, atr, macd, None, intervalSecOf(res) >= 3600)
        bis = fixBiExtremes(bis, merged) or bis
        return bis

    # ---------------- 实时监控 ----------------

    def _rewind_res(self, res):
        """把 res 周期增量状态重置并从 0 重放到当前已加载K线（批量重建，wick 全量口径）。

        实时bar（未收盘）的 OHLC 每轮更新时，合并/分型/MACD/ATR/笔等增量状态
        需随新数据修正，故整周期重放（保证与全量计算一致）。返回 True 触发链路重算。
        """
        bl = self.bars[res]["_list"]
        macd = MacdAccumulator()
        macd_t = []
        atr = AtrAccumulator(14)
        for bar in bl:
            macd.append(bar)
            macd_t.append(bar["time"])
            atr.append(bar)
        self._macd[res] = macd
        self._macd_times[res] = macd_t
        self._atr[res] = atr
        self._cut[res] = len(bl)
        self._resync_bis(res)
        return True

    def append_bars(self, res, new_bars):
        """实时并入轮询到的K线：返回 'append' | 'override' | None。

        - 时间戳晚于已加载最后一根 → 追加为新bar（'append'）；
        - 时间戳等于已加载最后一根（未收盘实时bar在更新） → 覆盖该bar OHLC
          （'override'），并登记到 _replay_needed，由 step_to 整周期重放修正增量状态；
        - 时间戳更早 → 历史已加载，跳过。
        """
        bl = self.bars[res]["_list"]
        if not bl:
            bl.extend(dict(b) for b in new_bars)
            self._times[res] = [b["time"] for b in new_bars]
            return "append"
        by_time = {}
        for b in new_bars:
            by_time[b["time"]] = b
        new_bars = sorted(by_time.values(), key=lambda x: x["time"])
        last_t = self._times[res][-1]
        appended = False
        overridden = False
        for b in new_bars:
            t = b["time"]
            if t > last_t:
                bl.append(dict(b))
                self._times[res].append(t)
                last_t = t
                appended = True
            elif t == last_t:
                bl[-1] = dict(b)
                overridden = True
            # t < last_t：已加载过，跳过
        if overridden:
            self._replay_needed.add(res)
        return "append" if appended else ("override" if overridden else None)

    def step_to(self, t, execute=False):
        """实时推进到时刻 t。

        先处理被覆盖的实时bar（整周期重放修正增量状态），再增量推进各周期切片。
        execute=False（默认，历史预热/只发信号）：仅推进状态并（笔结构变化时）收集
        新信号返回列表——与原「只收集不成交」语义一致，供预热忽略初始历史信号。
        execute=True（回放/实时监控逐根成交）：与 run() 同一套逐根逻辑（收盘判定 →
        下一根开盘成交的进场/出场），返回 dict：
            { signals: 本轮新信号, fills: 本轮新成交, exits: 本轮终局出场 }
        与 run() 共用 advance_exit_decision/execute_pending_exit/_fill_pending，
        保证三模式出场口径一致。
        """
        if not execute:
            changed = False
            if self._replay_needed:
                for res in self._replay_needed:
                    if self._rewind_res(res):
                        changed = True
                self._replay_needed.clear()
            if self._advance_cut(t):
                changed = True
            if not changed:
                return []
            self._rebuild_chain()
            return self._collect_signals(self._live_allSignals, self._live_seen,
                                         self._live_stats)
        return self._step_execute(t)

    def _step_execute(self, t):
        """execute=True 的逐根推进（与 run 循环同序：推进→出场判定→收集→成交槽）。

        持久状态（self._live_st）跨调用保存：处理位置 i、持仓、待成交信号、成交/统计。
        首轮 execute 前引擎通常已 execute=False 预热推进到历史末端（cut 已含全部已收盘
        bar），故从当前 cut 位置起只处理新收盘的 bar（实时/回放推进的增量），与 run()
        每根逻辑逐 bar 对齐。
        """
        fine = self.bars[self.fine_res]["_list"]
        times = self._times[self.fine_res]
        sec = intervalSecOf(self.fine_res) or 180
        st = self._live_st
        if st is None:
            st = self._live_st = {
                "i": self._cut[self.fine_res],          # 已推进（含）bar 数
                "open_pos": {"long": None, "short": None},
                "pending": [],                           # 待下一根开盘成交的信号
                "trades": [],                            # 全部成交（含已平仓）
                "allSignals": {},
                "seen": set(),
                "stats": {"steps": 0, "signals": 0, "executed": 0, "suppressed": 0,
                          "closed": 0, "long": 0, "short": 0,
                          "markRes": {}, "strategyKeys": {}},
            }
        end_cut = min(len(fine), bisect.bisect_right(times, t - sec) if sec > 0
                      else bisect.bisect_right(times, t))
        i = st["i"]
        # 回退保护：增量状态被整体倒放（如回放跳到更早位置触发 _rewind_res）时，
        # 推进位置回退到当前 cut，并清空持仓/待成交，避免跨区间错配（正常监控不回退）。
        if i > end_cut or i > self._cut[self.fine_res]:
            st["i"] = i = self._cut[self.fine_res]
            st["open_pos"] = {"long": None, "short": None}
            st["pending"] = []
        sup = []   # 同向持仓互斥被过滤的信号（供上层标记行状态）
        out = {"signals": [], "fills": [], "exits": [], "suppressed": []}
        while i < end_cut:
            t_dec = fine[i + 1]["time"] if i + 1 < end_cut else fine[i]["time"] + sec
            changed = self._advance_cut(t_dec)
            # ① 出场判定（已收盘 bar_i）：三档止盈/止损 → 挂起等下一开盘
            for d in ("long", "short"):
                pos = st["open_pos"][d]
                if pos is not None:
                    advance_exit_decision(pos, t_dec, fine[i],
                                          self._bis.get(pos.get("markRes")) or [],
                                          self._bis.get(pos.get("periodX")) or [],
                                          self._merged_times.get(pos.get("periodX")) or [])
            # ② 收集进场信号（与 run 同序同口径）
            if self.signal_mode == "realtime":
                if changed:
                    self._rebuild_chain()
                pend = self._collect_realtime(st["allSignals"], st["stats"], t_dec)
            else:
                if changed:
                    self._rebuild_chain()
                    pend = self._collect_signals(st["allSignals"], st["seen"], st["stats"])
                else:
                    pend = []
            st["pending"] += pend
            out["signals"] += list(pend)
            # ③ 成交槽（有下一根才成交）：出场先执行（解锁同向互斥）→ 进场再成交
            if i + 1 < end_cut:
                for d in ("long", "short"):
                    pos = st["open_pos"][d]
                    if pos is None:
                        continue
                    closed_trade = execute_pending_exit(pos, fine[i + 1])
                    if closed_trade is not None:
                        st["open_pos"][d] = None
                        st["stats"]["closed"] += 1
                        if str(closed_trade.get("exitType", "")).startswith("stop"):
                            st["stats"]["stopped"] = st["stats"].get("stopped", 0) + 1
                        out["exits"].append(closed_trade)
                nb = len(st["trades"])
                self._fill_pending(st["trades"], st["pending"],
                                   fine[i + 1]["open"], fine[i + 1]["time"],
                                   st["stats"], collectT=t_dec, open_pos=st["open_pos"],
                                   on_suppressed=lambda _s: sup.append(_s))
                out["fills"] += st["trades"][nb:]
                out["suppressed"] += list(sup)
                del sup[:]
                st["pending"] = []
            st["stats"]["steps"] += 1
            i += 1
        st["i"] = i
        return out

    # ---------------- 主循环 ----------------

    def run(self, to_ts=None, log=None, log_every=2000,
            on_progress=None, on_signal=None, on_trade=None, on_exit=None, on_suppressed=None,
            paused=None, stopped=None):
        """逐根K线重放。

        @param to_ts      结束时间戳（None 表示回测到最后一根）
        @param log        日志函数（None 不输出）
        @param log_every  每 N 根输出一次进度
        @param on_progress 可选：每根推进后调用 on_progress(i, end_i)（供进度条/后台线程）
        @param on_signal  可选：收集到新信号后逐个调用 on_signal(s)
        @param on_trade   可选：新成交产生后逐个调用 on_trade(t)
        @param on_exit    可选：持仓终局（止损/保本止损/全平）后逐个调用 on_exit(t)
        @param on_suppressed 可选：同向持仓互斥过滤的信号逐个调用 on_suppressed(s)
        @param paused     可选：threading.Event，置位时回测挂起等待（clear 后继续）
        @param stopped    可选：threading.Event，置位时提前停止并返回当前部分结果
        @returns dict：{ signals, trades, stats, ... }，见 _finish
        """
        log = log or (lambda *a, **k: None)
        fine = self.bars[self.fine_res]["_list"]
        n = len(fine)
        start_i = min(n, self.warmup_bars)
        end_i = n
        if to_ts is not None:
            end_i = min(end_i, bisect.bisect_right(self.bars[self.fine_res]["_times"], to_ts))

        allSignals = {}          # markRes -> [signals]
        seen = set()             # 去重 (periodX, time, direction, strategyKey)
        trades = []              # 已成交（下一根开盘价）
        pending = []             # 本步新收集、待下一根开盘价成交的信号
        # 同向持仓互斥：各方向最多一个未终局持仓（多空互不影响）
        open_pos = {"long": None, "short": None}
        stats = {"steps": 0, "signals": 0, "executed": 0, "suppressed": 0, "closed": 0,
                 "long": 0, "short": 0, "markRes": {}, "strategyKeys": {}}

        def _wait_if_paused():
            """paused 置位时挂起等待（同时响应 stopped 中断），供外部暂停/继续。"""
            while paused is not None and paused.is_set():
                if stopped is not None and stopped.is_set():
                    return False
                time.sleep(0.2)
            return True

        def _emit_signal(s):
            if on_signal:
                try:
                    on_signal(s)
                except Exception:
                    pass

        def _emit_exit(tr):
            if on_exit:
                try:
                    on_exit(tr)
                except Exception:
                    pass

        # 决策时刻 = 本根（i）收盘瞬间 = 下一根开盘时刻（fine_sec 后）：
        # 评估用截至本根收盘的数据（_advance_cut 只含已收盘 bar），成交用下一根开盘价
        # （同时刻），数据与决策完全同步、无窥视
        fine_sec = intervalSecOf(self.fine_res) or 180

        # 全量 run() 期间历史输入不变，数组仅在本次运行内复用。
        # 实时追加、覆盖和回退仍走原路径，不共享这些数组。
        price_arrays = {res: prepare_bar_arrays(self.bars[res]["_list"])
                        for res in self.periods}

        def rebuild_chain():
            self._rebuild_chain(include_entries=self.signal_mode != "realtime",
                                price_arrays=price_arrays, bar_times=self._times)

        # 预热阶段（warmup 之前），先把切片推进到位（仅计算，不判定进场）
        for i in range(start_i):
            if stopped is not None and stopped.is_set():
                break
            if not _wait_if_paused():
                break
            self._advance_cut(fine[i]["time"] + fine_sec)
        if stopped is not None and stopped.is_set():
            return self._finish(allSignals, trades, stats)
        log(f"预热完成：最小周期 {self.fine_res} 已到第 {start_i} 根（{fmtT(fine[start_i-1]['time'])}）")

        # 预热后先做一次全量链路重算（含支阻位/计划/进出场），之后只在笔结构变化时重算，
        # 避免每根K线全量重算链路（O(n) 扫描）导致回测 O(n²) 卡死
        rebuild_chain()
        if self.signal_mode == "realtime":
            # 当下背驰：预热完成时刻先评一次（链路状态已就绪，t=最后一根预热bar收盘）
            pending = self._collect_realtime(allSignals, stats, fine[start_i - 1]["time"] + fine_sec)
        else:
            pending = self._collect_signals(allSignals, seen, stats)
        for s in pending:
            _emit_signal(s)

        for i in range(start_i, end_i):
            if stopped is not None and stopped.is_set():
                break
            if not _wait_if_paused():
                break
            t = fine[i + 1]["time"] if i + 1 < end_i else fine[i]["time"] + fine_sec
            changed = self._advance_cut(t)
            # 出场判定（统一口径：已收盘 bar 判定，成交挂起到下一根开盘执行）：
            # 用刚收盘的 fine bar（第 i 根）完整 high/low 查止损 + 当前笔快照查三档止盈
            for d in ("long", "short"):
                pos = open_pos[d]
                if pos is None:
                    continue
                advance_exit_decision(pos, t, fine[i],
                                      self._bis.get(pos.get("markRes")) or [],
                                      self._bis.get(pos.get("periodX")) or [],
                                      self._merged_times.get(pos.get("periodX")) or [])
            if self.signal_mode == "realtime":
                # 当下背驰：笔结构变化时重算链路（刷新①③所需的计划/支阻位缓存），
                # 之后每根 fine 收盘都用当前增量状态（bis 已延伸到当下极值、MACD 增量）评估②
                if changed:
                    rebuild_chain()
                pending = self._collect_realtime(allSignals, stats, t)
                for s in pending:
                    _emit_signal(s)
            else:
                # 确认制：笔结构无变化时仅推进K线，不重算链路、不产新信号
                # （信号锚定在笔端点确认时出现）
                if changed:
                    rebuild_chain()
                    pending = self._collect_signals(allSignals, seen, stats)
                    for s in pending:
                        _emit_signal(s)
            # 成交槽（本根已收盘，使用下一根 fine K线开盘成交；无下一根则不成交）：
            # ① 先执行上一根收盘判定挂起的出场（终局出场先解锁同向互斥，本拍开盘才可再进）；
            # ② 再成交上一根收集到的进场信号（confirm=下一根开盘；anchor 见 _fill_pending）。
            if i + 1 < end_i:
                for d in ("long", "short"):
                    pos = open_pos[d]
                    if pos is None:
                        continue
                    closed_trade = execute_pending_exit(pos, fine[i + 1])
                    if closed_trade is not None:
                        open_pos[d] = None
                        stats["closed"] += 1
                        if str(closed_trade.get("exitType", "")).startswith("stop"):
                            stats["stopped"] = stats.get("stopped", 0) + 1
                        _emit_exit(closed_trade)
                n_trades_before = len(trades)
                self._fill_pending(trades, pending, fine[i + 1]["open"], fine[i + 1]["time"], stats,
                                   collectT=t, open_pos=open_pos, on_suppressed=on_suppressed)
                for tr in trades[n_trades_before:]:
                    if on_trade:
                        try:
                            on_trade(tr)
                        except Exception:
                            pass
                pending = []
            stats["steps"] += 1
            if on_progress:
                try:
                    on_progress(i + 1, end_i)
                except Exception:
                    pass
            if log and (i + 1) % log_every == 0:
                log(f"回测进度：第 {i + 1}/{end_i} 根，累计信号 {stats['signals']}，成交 {stats['executed']}")

        if log:
            log(f"回测完成：共 {stats['steps']} 步，信号 {stats['signals']}，成交 {stats['executed']}")
        # 收尾批量重同步：最终笔状态严格等于 batch(全前缀)（增量 wick 漂移归零）
        self.resync_all()
        return self._finish(allSignals, trades, stats)

    def _advance_cut(self, t):
        """把各周期切片推进到「已收盘」bar（收盘时刻 ≤ 决策时刻 t），逐根并入增量状态。

        只含已收盘 bar：bar.time + 周期间隔 ≤ t 才进入切片。旧规则 time<=t 让
        15m/60m/240/D 的 bar 在其开盘时刻即以**完整 OHLC** 进入状态，对决策时刻构成
        未来窥视（60m 最多提前 57 分钟——曾使 8-21 16:00 的中枢因 16:00-17:00 bar
        的未来高点 4585.13 提前成立、信号延后触发）。fine 周期 bar i 的收盘时刻
        恰为决策时刻（下一根开盘），仍及时包含。
        """
        changed = False
        for res in self.periods:
            times = self._times[res]
            sec = intervalSecOf(res) or 0
            k = bisect.bisect_right(times, t - sec) if sec > 0 else bisect.bisect_right(times, t)
            old = self._cut[res]
            if k != old:
                self._cut[res] = k
                new_bars = self.bars[res]["_list"][old:k]
                if self._append_bars(res, new_bars):
                    changed = True
        # 周期性批量重同步：消除增量 wick 阈值漂移（见 RESYNC_EVERY），重同步点上
        # 引擎状态严格等于 batch(前缀)。链路重算由调用方按 changed=True 触发。
        fc = self._cut.get(self.fine_res, 0)
        if fc - self._last_resync >= RESYNC_EVERY:
            self.resync_all()
            changed = True
        return changed

    def _rebuild_chain(self, *, include_entries=True, price_arrays=None, bar_times=None):
        """链路重算：买卖点 → 支阻位 → 交易计划 → 进出场（使用增量缓存指标）。

        30S（periods 含时）只进 bis 与进出场，不进买卖点/支阻位/交易计划——
        与 JS 端语义一致（mark-buy-sell/mark-sr-flip/trading-plan 周期不含 30S，
        sr_flip 的 LEVEL_ORDER 未收录 30S）。
        """
        periodBis = self._bis
        core = [p for p in self.periods if str(p).upper() != "30S"]
        barsByPeriod = {}
        periodMacd = {res: self._macd[res].to_list() for res in self.periods}
        periodAtr = {res: self._atr[res].value for res in self.periods}
        for res in self.periods:
            barsByPeriod[res] = self.bars[res]["_list"][: self._cut[res]]
        # 1. 买卖点（全链路完整性；默认关闭以提速，可由 --with-marks 开启）
        if self.with_marks:
            try:
                self._marks = compute_all_marks(periodBis, barsByPeriod, core,
                                                fromTs=None, periodMacd=periodMacd,
                                                periodAtr=periodAtr)
            except Exception:
                self._marks = {}
        # 2. 支阻位（密集区 + 黄金分割；传 periodMacdIn 复用增量 MACD 缓存）
        srKw = {}
        if self.sr_types is not None:
            srKw["srTypes"] = self.sr_types
        if self.fib_levels is not None:
            srKw["fibLevels"] = self.fib_levels
        if self.boll_length is not None:
            srKw["bollLength"] = self.boll_length
        if self.boll_mult is not None:
            srKw["bollMult"] = self.boll_mult
        if bar_times is not None:
            srKw["periodBarTimesIn"] = bar_times
        if price_arrays is not None:
            srKw["periodBarArraysIn"] = {
                res: (arrays[0][:self._cut[res]], arrays[1][:self._cut[res]])
                for res, arrays in price_arrays.items() if arrays is not None
            }
        try:
            self._sr = compute_srflip(periodBis, barsByPeriod, core,
                                      periodAtrsIn=periodAtr, periodMacdIn=periodMacd, **srKw)
        except Exception:
            self._sr = None
        # 3. 交易计划
        try:
            self._plan = compute_plan(periodBis, barsByPeriod, core,
                                      periodMacd=periodMacd, periodAtr=periodAtr)
        except Exception:
            self._plan = {}
        # 4. 进出场（检测周期与 JS 一致：不含日线、不含 30S——30S 仅作背驰级别；
        #    且须有已加载的更低级别可供区间套下沉，30S 未加载时 3 不作检测周期）
        # realtime 全量回测通过 _collect_realtime 收集信号，不消费 _entries。
        # 默认仍计算确认式结果，保持 step_to 等现有调用方的行为。
        if not include_entries:
            self._entries = {}
            return
        srLevels = (self._sr or {}).get("merged") or []
        detectPeriods = filterDetectPeriods(self.periods)
        try:
            self._entries = compute_entries(periodBis, barsByPeriod, self._plan, srLevels,
                                            detectPeriods=detectPeriods,
                                            periodMacd=periodMacd, periodAtr=periodAtr,
                                            with_30s=any(str(p).upper() == "30S" for p in self.periods))
        except Exception:
            self._entries = {}

    def _collect_signals(self, allSignals, seen, stats):
        """收集当前链路的进场信号（去重），返回本步新收集的信号列表（待成交）。"""
        newSigs = []
        for markRes, sigs in self._entries.items():
            for s in sigs:
                key = (s["periodX"], s["time"], s["direction"], s["strategyKey"])
                if key in seen:
                    continue
                seen.add(key)
                stats["signals"] += 1
                stats["long"] += int(s["direction"] == "long")
                stats["short"] += int(s["direction"] == "short")
                stats["markRes"][markRes] = stats["markRes"].get(markRes, 0) + 1
                stats["strategyKeys"][s["strategyKey"]] = stats["strategyKeys"].get(s["strategyKey"], 0) + 1
                allSignals.setdefault(markRes, []).append(dict(s, tradeNo=0))
                newSigs.append(s)
        return newSigs

    def _collect_realtime(self, allSignals, stats, t):
        """当下背驰模式：用当前增量状态（bis 延伸到当下极值 + MACD 增量 + 缓存的计划/支阻位）
        评估进场信号（每根 fine 收盘调用）。去重按 (periodX, strategyKey, markRes, 段起点)，
        每个形成段只发一次；信号 time=形成中段当前极值时间（当下）。"""
        from .mark_entry import evaluateRealtimeEntries
        srLevels = (self._sr or {}).get("merged") or []
        detectPeriods = filterDetectPeriods(self.periods)  # 与确认制一致：须有已加载更低级别
        sigs = evaluateRealtimeEntries(
            self._bis,
            {res: self._macd[res].entries for res in self.periods},
            {res: self._atr[res].value for res in self.periods},
            self._plan, srLevels, detectPeriods,
            tCut=t, fired=self._rt_fired,
            periodTimes=self._times, periodMacdTimes=self._macd_times,
        )
        newSigs = []
        for s in sigs:
            stats["signals"] += 1
            stats["long"] += int(s["direction"] == "long")
            stats["short"] += int(s["direction"] == "short")
            markRes = s["markRes"]
            stats["markRes"][markRes] = stats["markRes"].get(markRes, 0) + 1
            stats["strategyKeys"][s["strategyKey"]] = stats["strategyKeys"].get(s["strategyKey"], 0) + 1
            allSignals.setdefault(markRes, []).append(dict(s, tradeNo=0))
            newSigs.append(s)
        return newSigs

    def _fill_pending(self, trades, pending, nextOpen, nextTime, stats, collectT=None,
                      open_pos=None, on_suppressed=None):
        """把上一根收集到的信号成交（同向持仓互斥 + 方向感知止损参考位）。

        同向互斥：open_pos[dir] 有未终局持仓的同方向信号不成交（stats["suppressed"] 计数，
        on_suppressed 回调）；多空互不影响。pending 先按检测周期从大到小排序——
        同时刻同向共振信号大周期优先成交，其余被互斥过滤。
        成交口径（self.fill_mode）：
          - "anchor"：在「背驰锚点时间之后的第 1 根 fine 周期K线」开盘成交（消除确认延迟；
            锚点当拍不可知，含未来函数，仅回测理想化口径）；
          - "confirm"：在收集拍的下一根 fine K线开盘成交（原行为，无未来函数）；
          - 旧锚点保护：anchor 模式下锚点距收集时刻 > 检测周期 1 根 bar 长度（回退选中的
            过时旧点）→ 该笔回落 confirm 口径，fillMode = "confirm-stale-anchor"。
        简化的成交模型：单笔 lots 手（默认 4，平一半后 0.5 + 0.5 加权），
        盈亏 = 价格差 × 方向 × lots。
        """
        fine = self.bars[self.fine_res]["_list"]
        fineTimes = self._times[self.fine_res]
        srLevels = (self._sr or {}).get("merged") or []
        for s in sorted(pending, key=lambda x: -(intervalSecOf(x.get("periodX")) or 0)):
            d = s["direction"]
            if open_pos is not None and open_pos.get(d) is not None:
                stats["suppressed"] += 1
                if on_suppressed:
                    try:
                        on_suppressed(s)
                    except Exception:
                        pass
                continue
            entryTime, entryPrice, fillMode = nextTime, nextOpen, "confirm"
            # 当下背驰信号：收集拍即信号拍（形成中段极值在当拍已知），一律 confirm 口径
            # 成交（下一根开盘）——无未来函数，anchor 对其无意义
            if s.get("realtime"):
                fillMode = "confirm"
            elif self.fill_mode == "anchor" and s.get("time") is not None:
                stale = (collectT is not None
                         and (collectT - s["time"]) > (intervalSecOf(s.get("periodX")) or 0))
                if stale:
                    fillMode = "confirm-stale-anchor"
                else:
                    idx = bisect.bisect_right(fineTimes, s["time"])
                    if idx < len(fine):
                        entryTime = fine[idx]["time"]
                        entryPrice = fine[idx]["open"]
                        fillMode = "anchor"
                    # idx 越界（锚点之后已无 fine bar）→ 维持 confirm 口径
            stopRef = stop_ref_of(d, entryPrice, s.get("nearSr"), srLevels,
                                  slip_stop=self.slip_stop, slip_fallback=self.slip_fallback)
            # 保本止损位 beStop = 进场成交K线极值 ± slip_be（short: high+ / long: low−）；
            # 成交 bar 按 entryTime 定位于 fine 时间轴，取不到时兜底 进场价 ± slip_be。
            # 注意：run() 批量路径成交 bar 当拍未收盘（微前视 ≤1 根 fine bar），
            # step_to 实时路径成交 bar 已收盘、无前视（研究口径可接受，见模块 docstring）。
            beStop = entryPrice + (self.slip_be if d == "short" else -self.slip_be)
            bi = bisect.bisect_left(fineTimes, entryTime)
            if bi < len(fine) and fine[bi]["time"] == entryTime:
                beStop = (fine[bi]["high"] + self.slip_be) if d == "short" \
                    else (fine[bi]["low"] - self.slip_be)
            trades.append({
                "tradeNo": len(trades) + 1,
                "periodX": s["periodX"],
                "markRes": s["markRes"],
                "direction": s["direction"],
                "strategyKey": s["strategyKey"],
                "signalTime": s["time"],
                "signalPrice": s["price"],
                "entryTime": entryTime,
                "entryPrice": entryPrice,
                "fillMode": fillMode,
                "nearSr": s.get("nearSr"),
                "planDirection": s.get("planDirection"),
                "lots": self.lots,
                # 出场状态机字段（advance_exit_decision/execute_pending_exit 增量维护）
                "stopRef": stopRef,
                "beStop": beStop,
                "state": "open",
                "beDone": False,
                "halfDone": False,
                "exits": [],
            })
            stats["executed"] += 1
            if open_pos is not None:
                open_pos[d] = trades[-1]

    def _finish(self, allSignals, trades, stats):
        """整理回测结果：信号列表、成交明细、统计、时间轴、盈亏。

        已平仓（state=closed）的盈亏在 close_trade 终局时已结算（on_exit 回调携带），
        此处保留；未平仓按最新收盘价 mark-to-market（已平一半的按 0.5 half 价
        + 0.5 最新收盘加权），整体 × lots 手数。
        """
        lastPrice = None
        lastTime = None
        if self.bars[self.fine_res]["_list"]:
            lastBar = self.bars[self.fine_res]["_list"][-1]
            lastPrice = lastBar["close"]
            lastTime = lastBar["time"]
        for tr in trades:
            if tr.get("state") == "closed":
                continue  # pnl 已在 close_trade 结算
            if lastPrice is None:
                tr["pnl"] = 0.0
                continue
            d = 1 if tr["direction"] == "long" else -1
            lots = tr.get("lots", 1)
            half_ev = next((e for e in tr.get("exits", []) if e["type"] == "half"), None)
            if half_ev:
                # 已平一半：半仓按 half 价已实现 + 半仓按最新收盘 mark-to-market
                tr["pnl"] = (0.5 * (half_ev["price"] - tr["entryPrice"])
                             + 0.5 * (lastPrice - tr["entryPrice"])) * d * lots
            else:
                tr["pnl"] = (lastPrice - tr["entryPrice"]) * d * lots
        return {
            "signals": allSignals,        # { markRes: [signals] }
            "trades": trades,             # [ { tradeNo, periodX, direction, ... } ]
            "stats": stats,
            "lastPrice": lastPrice,
            "lastTime": lastTime,
            "periods": self.periods,
            "fine_res": self.fine_res,
        }


def run_backtest(bars_by_period, periods=None, warmup_bars=DEFAULT_WARMUP_BARS,
                 with_marks=False, to_ts=None, log=None, fill_mode="anchor",
                 signal_mode="realtime", sr_types=None, fib_levels=None,
                 boll_length=None, boll_mult=None,
                 lots=DEFAULT_LOTS, slip_stop=DEFAULT_SLIP_STOP,
                 slip_fallback=DEFAULT_SLIP_FALLBACK, slip_be=DEFAULT_SLIP_BE):
    """便捷入口：构建引擎并运行。fill_mode 见 BacktestEngine（anchor=锚点当拍成交，confirm=确认成交）；
    signal_mode：realtime=当下背驰（每拍评估形成中段，默认），confirm=确认制（结构变化时收集）；
    sr_types/fib_levels 透传支阻位类型开关与黄金分割比率（None → compute_srflip 默认）；
    boll_length/boll_mult 透传 BOLL 布林带周期与标准差倍数（None → compute_srflip 默认 26/2）；
    lots/slip_stop/slip_fallback/slip_be 透传出场参数（手数/止损滑点/兜底止损滑点/保本滑点，
    默认 4 / 3 / 10 / 3，绝对价格单位）。"""
    engine = BacktestEngine(bars_by_period, periods=periods, warmup_bars=warmup_bars,
                            with_marks=with_marks, fill_mode=fill_mode,
                            signal_mode=signal_mode,
                            sr_types=sr_types, fib_levels=fib_levels,
                            boll_length=boll_length, boll_mult=boll_mult,
                            lots=lots, slip_stop=slip_stop,
                            slip_fallback=slip_fallback, slip_be=slip_be)
    return engine.run(to_ts=to_ts, log=log)


def build_bis(bars_by_period, periods=None):
    """对整段数据各周期一次性重建笔（全链路/实时态使用），返回 { 周期: [bis] }。
    与 chan-bi JS 同口径：长影压平（markWickBars）后才做包含合并；ATR/MACD 用原始K线；
    未完成笔延伸用压平后K线（延伸不指向已压平的插针价）。
    最后一笔会延伸到最新极端价（与 chan-bi JS 落盘数据一致）。"""
    periods = list(periods or DEFAULT_PERIODS)
    out = {}
    for res in periods:
        bl = sorted(bars_by_period.get(res, []) or [], key=lambda x: x["time"])
        if len(bl) < 6:
            continue
        from .chan_core import mergeBars, findFractals, markWickBars
        trimmed = markWickBars(bl)
        merged = mergeBars(trimmed)
        fractals = findFractals(merged)
        atr = calcATR(bl, 14)
        macd = calcMACD(bl)
        # 近等双顶/双底平台取后顶/后底：与 chan-bi 一致仅 ≥60m（60/240/D）开启
        bis = buildBi(fractals, merged, atr, macd, None, intervalSecOf(res) >= 3600)
        bis = fixBiExtremes(bis, merged) or bis
        bis = extendLastBi(bis, trimmed)
        if bis:
            out[res] = bis
    return out


# ============================================================
# 结果统计/导出
# ============================================================


def summarize(result):
    """把回测结果转成可直接打印/保存的统计 dict。"""
    st = result["stats"]
    trades = result["trades"]
    longT = sum(1 for t in trades if t["direction"] == "long")
    shortT = sum(1 for t in trades if t["direction"] == "short")
    closedT = [t for t in trades if t.get("state") == "closed"]
    openT = [t for t in trades if t.get("state") != "closed"]
    exitTypes = {}
    halfCnt = 0
    for t in trades:
        et = t.get("exitType")
        if et:
            name = {"stopSr": "支阻位止损", "stopBe": "保本止损", "close": "全平"}.get(et, et)
            exitTypes[name] = exitTypes.get(name, 0) + 1
        halfCnt += sum(1 for e in t.get("exits", []) if e["type"] == "half")
    realized = sum(t["pnl"] for t in closedT)
    floating = sum(t["pnl"] for t in openT)
    return {
        "周期": result["periods"],
        "最小周期": result["fine_res"],
        "回测步数": st["steps"],
        "信号数": st["signals"],
        "成交数": st["executed"],
        "同向过滤": st.get("suppressed", 0),
        "信号多空": {"多": st["long"], "空": st["short"]},
        "按背驰级别分布": st["markRes"],
        "按策略分布": st["strategyKeys"],
        "成交多空": {"多": longT, "空": shortT},
        "手数合计": sum(t.get("lots", 1) for t in trades),
        "已平仓数": len(closedT),
        "仍持仓数": len(openT),
        "出场类型": exitTypes,
        "平一半次数": halfCnt,
        "已平仓盈亏": round(realized, 2),
        "未平仓浮盈": round(floating, 2),
        "最新收盘价": result["lastPrice"],
        "最新K线时间": fmtT(result["lastTime"]) if result["lastTime"] else None,
    }
