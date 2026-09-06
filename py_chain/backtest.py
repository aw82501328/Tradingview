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
为唯一实现源；mark_entry.stop_ref_of/find_bi_event）：
  - 触发判定在「已收盘 bar」进行（bar 完整 high/low 判止损穿越；三档止盈用
    endTime ≤ 收盘时刻的已确认笔）；
  - 成交统一「下一根K线开盘」：止损/保本止损/平一半/全平的事件时间 = 触发 bar 的
    下一根 bar 时间、价格 = 其开盘价（跳空自然体现）；触发 bar 无下一根 → 未成交
    （持仓保持 open，mark-to-market 收尾）；
  - 止盈1 保本：背驰周期（markRes）够笔（进场后首笔有利方向笔完成）→ 止损位上移至
    进场价（状态迁移，当拍生效、事件仅落盘）；
  - 止盈2 平一半：检测周期（periodX）够笔 → 下一开盘平一半（需保本已触发）；
  - 止盈3 全平：检测周期破前底/过前高（进场后开始的不利方向笔端点破前一同向笔端点）
    → 下一开盘全平；
  - 同向持仓互斥：同方向持仓未终局时新信号不成交（on_suppressed 回调）；多空互不影响；
  - 已平仓盈亏按实际出场加权（TP2 半仓价 + 终局价各 0.5；未到 TP2 全量终局价），
    未平仓仍按最新收盘价 mark-to-market。
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
    MacdAccumulator, AtrAccumulator, extendLastBi, extendLastBiFrom,
)
from .mark_buy_sell import compute_all_marks
from .sr_flip import compute_srflip
from .trading_plan import compute_plan
from .mark_entry import compute_entries, stop_ref_of, find_bi_event

DEFAULT_PERIODS = ["D", "240", "60", "15", "3"]
DEFAULT_WARMUP_BARS = 60

# ============================================================
# 出场状态机（三模式统一口径：收盘判定 → 下一根开盘成交）
# ============================================================


def advance_exit_decision(pos, t, bar, mark_bis, px_bis):
    """出场判定（纯函数，三模式共用）——在「已收盘 bar」上判定一次。

    统一语义（用户规则 2026-09-06）：
      - 用 bar 完整 high/low 判止损/保本止损穿越；
      - 三档止盈用 endTime <= t 的已确认笔（markRes 保本/半平、periodX 全平破前低/高）；
      - breakeven：仅状态迁移（止损位 → 进场价），当拍生效、事件仅落盘；
      - half/close/stopSr/stopBe：成交型事件——只把 pos['pendingExit'] 挂起，
        由 execute_pending_exit 在「下一根K线开盘」执行成交（同一拍只挂一个，
        逐拍执行后继续判定）。
    @param t      决策时刻 = bar 收盘时刻（下一根开盘时刻）
    @param bar    该根已收盘 K线（{time,open,high,low,close}）
    @param mark_bis / px_bis  背驰级别 / 检测周期笔快照（endTime ≤ t 已含）
    @returns 挂起类型（"half"/"close"/"stopSr"/"stopBe"）或 None（含仅 breakeven）
    """
    if pos.get("pendingExit"):
        return None  # 已挂起等下一开盘，不再重复判定
    is_short = pos["direction"] == "short"
    fav = "down" if is_short else "up"    # 有利方向笔（short 盼下跌 / long 盼上涨）
    adv = "up" if is_short else "down"    # 不利方向笔（TP3 破高低用）
    tp1 = find_bi_event(mark_bis, pos["signalTime"], fav)
    tp2 = find_bi_event(px_bis, pos["signalTime"], fav)
    tp3 = find_bi_event(px_bis, pos["signalTime"], adv, require_post_start=True, break_prev=True)
    # 同一拍顺序：保本 → 半平 → 全平 → 止损（逐拍各挂一个）
    if tp1 and tp1["time"] <= t and not pos.get("beDone"):
        pos["beDone"] = True
        pos["exits"].append({"type": "breakeven", "time": tp1["time"], "price": tp1["price"]})
    if tp2 and tp2["time"] <= t and pos.get("beDone") and not pos.get("halfDone"):
        pos["halfDone"] = True
        pos["pendingExit"] = "half"
        return "half"
    if tp3 and tp3["time"] <= t:
        pos["pendingExit"] = "close"
        return "close"
    stop = pos["entryPrice"] if pos.get("beDone") else pos.get("stopRef")
    if stop is not None:
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
        return None
    return close_trade(pos, et, exec_bar["time"], exec_bar["open"])


def close_trade(pos, exit_type, exit_time, exit_price):
    """标记持仓终局并结算盈亏（半仓按 half 事件价加权）。"""
    pos["state"] = "closed"
    pos["exitType"] = exit_type
    pos["exitTime"] = exit_time
    pos["exitPrice"] = exit_price
    d = 1 if pos["direction"] == "long" else -1
    entry = pos["entryPrice"]
    half_ev = next((e for e in pos.get("exits", []) if e["type"] == "half"), None)
    if half_ev:
        pos["pnl"] = 0.5 * (half_ev["price"] - entry) * d + 0.5 * (exit_price - entry) * d
    else:
        pos["pnl"] = (exit_price - entry) * d
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
                 with_marks=False, cfg=None, fill_mode="anchor", signal_mode="realtime"):
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

        # 增量状态
        self._cut = {res: 0 for res in self.periods}
        self._merged = {res: [] for res in self.periods}
        self._merge_dir = {res: 0 for res in self.periods}
        self._fractals = {res: [] for res in self.periods}
        self._bis = {res: [] for res in self.periods}
        self._macd = {res: MacdAccumulator() for res in self.periods}
        self._macd_times = {res: [] for res in self.periods}  # 与 macd.entries 一一对应（切片二分用）
        self._atr = {res: AtrAccumulator(14) for res in self.periods}
        self._marks = {}
        self._sr = None
        self._plan = {}
        self._entries = {}
        # 当下背驰去重：(periodX, strategyKey, markRes, 形成段起点时间)，每个形成段只发一次
        self._rt_fired = set()

        # 实时监控状态（step_to 使用）：跨轮询保持信号去重与统计
        self._live_st = None          # step_to(execute=True) 持久状态（见 _step_execute）
        self._replay_needed = set()   # 需要整周期重放修正增量状态的周期（实时bar被覆盖）
        self._live_allSignals = {}
        self._live_seen = set()
        self._live_stats = {"steps": 0, "signals": 0, "executed": 0,
                            "long": 0, "short": 0, "markRes": {}, "strategyKeys": {}}

    # ---------------- 增量计算 ----------------

    def _append_bars(self, res, new_bars):
        """把 res 周期新增的K线逐根并入增量状态；返回该周期笔结构是否变化（新分型或延伸推进）。"""
        from .chan_core import _mergeStep, updateFractalsTail, extendLastBiFrom
        merged = self._merged[res]
        direction = self._merge_dir[res]
        macd = self._macd[res]
        atr = self._atr[res]
        for bar in new_bars:
            merged, direction = _mergeStep(merged, direction, bar)
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
            self._bis[res] = self._build_bis(merged, new_f, macd.to_list(), atr.value)
        # 最后一笔始终延伸到最新极端价（与 chan-bi 落盘数据一致）
        if self._bis[res]:
            # 用二分定位最后笔起点在时间轴上的索引（O(log n)），只从该位置起增量扫描，
            # 避免每根K线从 bars 头部全量扫描与整段切片复制导致的 O(n²)
            last_start = self._bis[res][-1].get("startTime")
            start_idx = bisect.bisect_left(self._times[res], last_start) if last_start is not None else 0
            prev_end = (self._bis[res][-1].get("endTime"), self._bis[res][-1].get("endPrice"))
            self._bis[res] = extendLastBiFrom(self._bis[res], self.bars[res]["_list"],
                                              start_idx, endIdx=self._cut[res])
            # 延伸实际推进了最后笔端点也算笔结构变化（供链路短路判断）
            cur_end = (self._bis[res][-1].get("endTime"), self._bis[res][-1].get("endPrice"))
            if cur_end != prev_end:
                bis_changed = True
        return bis_changed

    def _build_bis(self, merged, fractals, macd, atr):
        """从分型重建笔并做端点极值修正；返回按时间升序的笔列表。"""
        from .chan_core import fixBiExtremes
        if len(fractals) < 2:
            return []
        bis = buildBi(fractals, merged, atr, macd)
        bis = fixBiExtremes(bis, merged) or bis
        return bis

    # ---------------- 实时监控 ----------------

    def _rewind_res(self, res):
        """把 res 周期增量状态重置并从 0 重放到当前已加载K线。

        实时bar（未收盘）的 OHLC 每轮更新时，合并/分型/MACD/ATR/笔等增量状态
        需随新数据修正，故整周期重放（保证与全量计算一致）。返回 True 触发链路重算。
        """
        self._merged[res] = []
        self._merge_dir[res] = 0
        self._fractals[res] = []
        self._bis[res] = []
        self._macd[res] = MacdAccumulator()
        self._macd_times[res] = []
        self._atr[res] = AtrAccumulator(14)
        self._cut[res] = len(self.bars[res]["_list"])
        self._append_bars(res, self.bars[res]["_list"])
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
                                          self._bis.get(pos.get("periodX")) or [])
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
        self._rebuild_chain()
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
                                      self._bis.get(pos.get("periodX")) or [])
            if self.signal_mode == "realtime":
                # 当下背驰：笔结构变化时重算链路（刷新①③所需的计划/支阻位缓存），
                # 之后每根 fine 收盘都用当前增量状态（bis 已延伸到当下极值、MACD 增量）评估②
                if changed:
                    self._rebuild_chain()
                pending = self._collect_realtime(allSignals, stats, t)
                for s in pending:
                    _emit_signal(s)
            else:
                # 确认制：笔结构无变化时仅推进K线，不重算链路、不产新信号
                # （信号锚定在笔端点确认时出现）
                if changed:
                    self._rebuild_chain()
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
        return changed

    def _rebuild_chain(self):
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
        # 2. 支阻位
        try:
            self._sr = compute_srflip(periodBis, barsByPeriod, core,
                                      periodAtrsIn=periodAtr)
        except Exception:
            self._sr = None
        # 3. 交易计划
        try:
            self._plan = compute_plan(periodBis, barsByPeriod, core,
                                      periodMacd=periodMacd, periodAtr=periodAtr)
        except Exception:
            self._plan = {}
        # 4. 进出场（检测周期与 JS 一致：不含日线、不含 30S——30S 仅作背驰级别）
        srLevels = (self._sr or {}).get("merged") or []
        detectPeriods = [p for p in core if str(p).upper() != "D"]
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
        detectPeriods = [p for p in self.periods
                         if str(p).upper() not in ("D", "30S")]  # 与确认制一致：不含日线、30S 仅作背驰级别
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
        简化的成交模型：单笔等权 1 手（平一半后 0.5 + 0.5），用于盈亏统计。
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
            stopRef = stop_ref_of(d, entryPrice, s.get("nearSr"), srLevels)
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
                # 出场状态机字段（advance_exit_decision/execute_pending_exit 增量维护）
                "stopRef": stopRef,
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

        已平仓（state=closed）的盈亏在 _close_pos 终局时已结算（on_exit 回调携带），
        此处保留；未平仓按最新收盘价 mark-to-market。
        """
        lastPrice = None
        lastTime = None
        if self.bars[self.fine_res]["_list"]:
            lastBar = self.bars[self.fine_res]["_list"][-1]
            lastPrice = lastBar["close"]
            lastTime = lastBar["time"]
        for tr in trades:
            if tr.get("state") == "closed":
                continue  # pnl 已在 _close_pos 结算
            d = 1 if tr["direction"] == "long" else -1
            tr["pnl"] = (lastPrice - tr["entryPrice"]) * d if lastPrice is not None else 0.0
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
                 signal_mode="realtime"):
    """便捷入口：构建引擎并运行。fill_mode 见 BacktestEngine（anchor=锚点当拍成交，confirm=确认成交）；
    signal_mode：realtime=当下背驰（每拍评估形成中段，默认），confirm=确认制（结构变化时收集）。"""
    engine = BacktestEngine(bars_by_period, periods=periods, warmup_bars=warmup_bars,
                            with_marks=with_marks, fill_mode=fill_mode,
                            signal_mode=signal_mode)
    return engine.run(to_ts=to_ts, log=log)


def build_bis(bars_by_period, periods=None):
    """对整段数据各周期一次性重建笔（全链路/实时态使用），返回 { 周期: [bis] }。
    最后一笔会延伸到最新极端价（与 chan-bi JS 落盘数据一致）。"""
    periods = list(periods or DEFAULT_PERIODS)
    out = {}
    for res in periods:
        bl = sorted(bars_by_period.get(res, []) or [], key=lambda x: x["time"])
        if len(bl) < 6:
            continue
        from .chan_core import mergeBars, findFractals
        merged = mergeBars(bl)
        fractals = findFractals(merged)
        atr = calcATR(bl, 14)
        macd = calcMACD(bl)
        # 近等双顶/双底平台取后顶/后底：与 chan-bi 一致仅 ≥60m（60/240/D）开启
        bis = buildBi(fractals, merged, atr, macd, None, intervalSecOf(res) >= 3600)
        bis = fixBiExtremes(bis, merged) or bis
        bis = extendLastBi(bis, bl)
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
        "已平仓数": len(closedT),
        "仍持仓数": len(openT),
        "出场类型": exitTypes,
        "平一半次数": halfCnt,
        "已平仓盈亏": round(realized, 2),
        "未平仓浮盈": round(floating, 2),
        "最新收盘价": result["lastPrice"],
        "最新K线时间": fmtT(result["lastTime"]) if result["lastTime"] else None,
    }
