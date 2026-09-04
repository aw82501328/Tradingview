# -*- coding: utf-8 -*-
"""
点状回测引擎（Python 移植版，与 plan 中 backtest 设计对齐）

在最小周期（默认 3 分钟）上逐根 K 线推进，每根收盘后以「截至该时刻的切片」
重放整条链路：画笔(buildBi) → 买卖点(compute_all_marks) → 支阻位(compute_srflip)
→ 交易计划(compute_plan) → 进出场(compute_entries)，避免未来函数。

成交规则：
  - 信号在下一根K线开盘价成交（bar by bar，无偷价）；
  - 暂不实现出场策略，故无出场箭头；持仓仅记录未平仓浮盈（mark-to-market）。

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
from .mark_entry import compute_entries

DEFAULT_PERIODS = ["D", "240", "60", "15", "3"]
DEFAULT_WARMUP_BARS = 60


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
                 with_marks=False, cfg=None):
        self.periods = list(periods or DEFAULT_PERIODS)
        # 各周期按时间升序整理 + 缓存时间数组
        self.bars = {}
        self._times = {}
        for res in self.periods:
            bl = sorted(bars_by_period.get(res, []) or [], key=lambda x: x["time"])
            self.bars[res] = {"_list": bl, "_times": [b["time"] for b in bl]}
            self._times[res] = self.bars[res]["_times"]
        # 最小周期（细分周期）逐根推进：取区间最小且有数据的周期
        self.fine_res = min(self.periods, key=lambda r: intervalSecOf(r) or 0)
        if not self.bars[self.fine_res]["_list"]:
            raise ValueError(f"最小周期 {self.fine_res} 无K线数据")
        self.warmup_bars = warmup_bars
        self.with_marks = with_marks
        self.cfg = cfg or {}

        # 增量状态
        self._cut = {res: 0 for res in self.periods}
        self._merged = {res: [] for res in self.periods}
        self._merge_dir = {res: 0 for res in self.periods}
        self._fractals = {res: [] for res in self.periods}
        self._bis = {res: [] for res in self.periods}
        self._macd = {res: MacdAccumulator() for res in self.periods}
        self._atr = {res: AtrAccumulator(14) for res in self.periods}
        self._marks = {}
        self._sr = None
        self._plan = {}
        self._entries = {}

        # 实时监控状态（step_to 使用）：跨轮询保持信号去重与统计
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

    def step_to(self, t):
        """实时推进到时刻 t，返回本轮新进场信号列表（已去重，不做成交）。

        先处理被覆盖的实时bar（整周期重放修正增量状态），再增量推进各周期切片；
        仅当某周期笔结构变化时重算链路并收集新信号（与回测 run 的短路语义一致）。
        """
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
        return self._collect_signals(self._live_allSignals, self._live_seen, self._live_stats)

    # ---------------- 主循环 ----------------

    def run(self, to_ts=None, log=None, log_every=2000,
            on_progress=None, on_signal=None, on_trade=None,
            paused=None, stopped=None):
        """逐根K线重放。

        @param to_ts      结束时间戳（None 表示回测到最后一根）
        @param log        日志函数（None 不输出）
        @param log_every  每 N 根输出一次进度
        @param on_progress 可选：每根推进后调用 on_progress(i, end_i)（供进度条/后台线程）
        @param on_signal  可选：收集到新信号后逐个调用 on_signal(s)
        @param on_trade   可选：新成交产生后逐个调用 on_trade(t)
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
        stats = {"steps": 0, "signals": 0, "executed": 0,
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

        # 预热阶段（warmup 之前），先把切片推进到位（仅计算，不判定进场）
        for i in range(start_i):
            if stopped is not None and stopped.is_set():
                break
            if not _wait_if_paused():
                break
            t = fine[i]["time"]
            self._advance_cut(t)
        if stopped is not None and stopped.is_set():
            return self._finish(allSignals, trades, stats)
        log(f"预热完成：最小周期 {self.fine_res} 已到第 {start_i} 根（{fmtT(fine[start_i-1]['time'])}）")

        # 预热后先做一次全量链路重算（含支阻位/计划/进出场），之后只在笔结构变化时重算，
        # 避免每根K线全量重算链路（O(n) 扫描）导致回测 O(n²) 卡死
        self._rebuild_chain()
        pending = self._collect_signals(allSignals, seen, stats)
        for s in pending:
            _emit_signal(s)

        for i in range(start_i, end_i):
            if stopped is not None and stopped.is_set():
                break
            if not _wait_if_paused():
                break
            t = fine[i]["time"]
            changed = self._advance_cut(t)
            # 笔结构无变化时仅推进K线，不重算链路、不产新信号（信号锚定在笔端点确认时出现）
            if changed:
                self._rebuild_chain()
                pending = self._collect_signals(allSignals, seen, stats)
                for s in pending:
                    _emit_signal(s)
            # 上一根收集到的信号在下一根开盘价成交（延迟一拍）
            if i + 1 < end_i:
                n_trades_before = len(trades)
                self._fill_pending(trades, pending, fine[i + 1]["open"], fine[i + 1]["time"], stats)
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
        """把各周期切片推进到 time<=t，逐根并入增量状态；返回是否有周期笔结构变化。"""
        changed = False
        for res in self.periods:
            times = self._times[res]
            k = bisect.bisect_right(times, t)
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

    def _fill_pending(self, trades, pending, nextOpen, nextTime, stats):
        """把上一根收集到的信号以 nextOpen 成交（无出场，直接入持仓）。"""
        # 简化的成交模型：信号在下一根开盘价建立持仓（单笔等权 1 手，用于浮盈统计）
        for s in pending:
            trades.append({
                "tradeNo": len(trades) + 1,
                "periodX": s["periodX"],
                "markRes": s["markRes"],
                "direction": s["direction"],
                "strategyKey": s["strategyKey"],
                "signalTime": s["time"],
                "signalPrice": s["price"],
                "entryTime": nextTime,
                "entryPrice": nextOpen,
                "nearSr": s.get("nearSr"),
            })
            stats["executed"] += 1

    def _finish(self, allSignals, trades, stats):
        """整理回测结果：信号列表、成交明细、统计、时间轴、浮盈。"""
        lastPrice = None
        lastTime = None
        if self.bars[self.fine_res]["_list"]:
            lastBar = self.bars[self.fine_res]["_list"][-1]
            lastPrice = lastBar["close"]
            lastTime = lastBar["time"]
        # 浮盈：暂不实现出场，全部按最新收盘价 mark-to-market
        for tr in trades:
            tr["pnl"] = (lastPrice - tr["entryPrice"]) * (1 if tr["direction"] == "long" else -1) \
                if lastPrice is not None else 0.0
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
                 with_marks=False, to_ts=None, log=None):
    """便捷入口：构建引擎并运行。"""
    engine = BacktestEngine(bars_by_period, periods=periods, warmup_bars=warmup_bars,
                            with_marks=with_marks)
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
        bis = buildBi(fractals, merged, atr, macd)
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
    pnl = sum(t["pnl"] for t in trades)
    return {
        "周期": result["periods"],
        "最小周期": result["fine_res"],
        "回测步数": st["steps"],
        "信号数": st["signals"],
        "成交数": st["executed"],
        "信号多空": {"多": st["long"], "空": st["short"]},
        "按背驰级别分布": st["markRes"],
        "按策略分布": st["strategyKeys"],
        "成交多空": {"多": longT, "空": shortT},
        "未平仓浮盈": round(pnl, 2),
        "最新收盘价": result["lastPrice"],
        "最新K线时间": fmtT(result["lastTime"]) if result["lastTime"] else None,
    }
