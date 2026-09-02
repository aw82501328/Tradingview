# -*- coding: utf-8 -*-
"""
盘面监控：实时监控 与 K线回放回测 两种模式，进场点提醒 + 盘面标记

实时监控（默认 --mode live）：
  1. 初始化：load_bars 全量加载多周期历史 → BacktestEngine 增量引擎，step_to 预热到最新
     （初始已存在的进场信号忽略，不提醒不画）；
  2. 每轮 tick：读取 3m 尾部K线（监控期间图表固定在 3m，每轮零切换零等待），
     增量并入 3m；仅当检测到「新的已收盘 3m bar」（约每 3 分钟一次）时才做一次
     全周期（D/240/60/15/3）切换刷新（避免每轮都切 5 个周期导致图表频繁跳动），
     刷新后回到 3m；Ctrl+C 退出时恢复监控前的图表周期；
  3. 推进链路（step_to）收集新进场信号（按 (periodX,time,direction,strategyKey) 去重）；
  4. 新信号处理：控制台醒目提醒 + winsound 系统提示音，并在 TradingView 盘面追加
     RT· 箭头标记（多=红上箭头、空=绿下箭头，前缀 RT· 与回测 BT· 隔离），
     按信号时间去重（同一信号不重复画、不覆盖历史标记）。

K线回放回测（--mode replay）：
  1. 复用 load_bars 全量加载历史（不预热），驱动 TradingView Bar Replay 自动播放；
  2. 每轮读 currentDate() 回放位置，按 3m 粒度逐根追赶补齐 step_to，不漏中间 bar 信号；
  3. 信号触发时暂停回放 → 切到信号周期画箭头 → 停留 --hold 秒 → 切回 3m 恢复播放；
  4. 回放结束或 Ctrl+C 时 stopReplay 并恢复用户原周期。

用法：
    python -m py_chain.monitor --symbol OANDA:XAUUSD --interval 15
    python -m py_chain.monitor --mode replay --start 2026-08-01 --speed 1000
    python -m py_chain.monitor --clear                 # 清除本 session 画的 RT· 标记后退出

依赖：websocket-client（data_loader 已有）；winsound（Windows 标准库，仅提醒音用）。
"""

import argparse
import json
import sys
import time

from .data_loader import (
    CDPClient, CDPConfig, DEFAULT_PERIODS, DEFAULT_CDP_PORT, load_bars,
)
from .backtest import BacktestEngine
from .chan_core import fmtT
from .main import parse_from

# 箭头颜色与文本前缀：与回测回画(tv_draw)约定一致
BUY_COLOR = "#F23645"      # 红（做多）
SELL_COLOR = "#089981"     # 绿（做空）
RT_PREFIX = "RT·"
CHUNK = 50
# localStorage 键：记录实时监控画的 shape id（与回测的 bt_arrow_ids 隔离，互不影响）
RT_IDS_KEY = "rt_arrow_ids"
# 每周期切换后等待图表刷新数据的时间（实时只读尾部K线，无需像全量取数那样 scroll 到第一根）
RES_WAIT = 2.0
# 3m 快速检查的切换等待（只读单周期尾部，等待更短以减轻图表跳动）
FAST_WAIT = 0.8


def read_tail(c, n):
    """读取当前图表周期末尾 n 根K线（含未收盘实时bar）。

    @param c  CDPClient
    @param n  读取末尾K线数
    @returns { bars: [{time, open, high, low, close}] } 或 { error }
    """
    expr = (
        "(function() { const c = TradingViewApi.activeChart(); "
        "if (!c) return { error: 'no_chart' }; "
        "const items = c.chartModel().mainSeries().data().m_bars._items; "
        "const out = []; "
        "const start = Math.max(0, items.length - %d); "
        "for (let i = start; i < items.length; i++) { "
        "  const v = items[i].value; "
        "  out.push({ time: v[0], open: v[1], high: v[2], low: v[3], close: v[4] }); } "
        "return { bars: out, total: items.length }; })()"
    ) % int(n)
    return c.evaluate(expr)


def _switch_res(c, res):
    """切换图表到指定周期（等图表刷新该周期数据由调用方 sleep 控制）。"""
    c.evaluate(f"TradingViewApi.activeChart().setResolution({json.dumps(res)});")


def _ensure_res(c, res):
    """确保图表处于指定周期：已处于该周期则跳过 setResolution（避免反复切换刷新）。

    返回是否发生了实际切换（True 时调用方需等待图表刷新）。
    """
    cur = c.evaluate("String(TradingViewApi.activeChart().resolution());")
    cur = str(cur)
    if cur == res:
        return False
    _switch_res(c, res)
    return True


def _draw_signals(c, sigs):
    """画一批 RT· 箭头到当前图表，并把新 shape id 累积记录到 localStorage。

    多=红色向上箭头（arrow_up）、空=绿色向下箭头（arrow_down），文本 RT·BUY/SELL + 价。
    返回新画的 shape id 列表（与 tv_draw 的清除/回画机制同源）。
    """
    calls = []
    for s in sigs:
        shape = "arrow_up" if s["direction"] == "long" else "arrow_down"
        color = BUY_COLOR if s["direction"] == "long" else SELL_COLOR
        label = "BUY" if s["direction"] == "long" else "SELL"
        text = f"{RT_PREFIX}{label} {s['price']:.2f}"
        calls.append(
            "await chart.createShape("
            f"{{ time: {s['time']}, price: {s['price']} }}, "
            f"{{ shape: '{shape}', text: '{text}', lock: true, "
            f"color: '{color}', textColor: '{color}' }})"
        )
    expr = (
        "(async () => { const chart = TradingViewApi.activeChart(); "
        "if (!chart) return { error: 'no_chart' }; const ids = []; "
        + "; ".join(f"ids.push(await ({c}))" for c in calls) +
        "; "
        "try { const old = JSON.parse(localStorage.getItem('" + RT_IDS_KEY + "') || '[]'); "
        "localStorage.setItem('" + RT_IDS_KEY + "', JSON.stringify(old.concat(ids))); } catch (e) {} "
        "; return ids; })()"
    )
    return c.evaluate(expr)


def clear_rt_markers(cfg=None, log=None):
    """清除本 session 画的 RT· 标记（只删自己创建的，不影响用户图形与回测 BT· 标记）。

    @returns 删除数量
    """
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: None)
    with CDPClient(cfg, log=log) as c:
        expr = (
            "(async () => { "
            "const chart = TradingViewApi.activeChart(); "
            "if (!chart) return -1; "
            "const cm = chart.chartModel(); "
            "let ids = []; "
            "try { ids = JSON.parse(localStorage.getItem('" + RT_IDS_KEY + "') || '[]'); } catch (e) {} "
            "let removed = 0; "
            "for (const id of ids) { "
            "  try { "
            "    const ds = cm.dataSourceForId(id); "
            "    if (ds) { cm.removeSource(ds); removed++; } "
            "  } catch (err) {} "
            "} "
            "try { localStorage.setItem('" + RT_IDS_KEY + "', '[]'); } catch (e) {} "
            "return removed; })()"
        )
        removed = int(c.evaluate(expr) or 0)
    log(f"已清除 RT· 实时标记 {removed} 个")
    return removed


# ============================================================
# Bar Replay API 封装（--mode replay 使用）
# 探测结论（2026-09-01 桌面端联调）：
#   - window.TradingViewApi._replayApi 存在，为普通对象；
#   - 方法返回 observable 对象，需 .value() 解包取真实值（isReplayAvailable/isReplayStarted/currentDate）；
#   - currentDate() 解包后为【秒】时间戳（与 BacktestEngine 秒级一致，无需换算）；
#   - selectDate(e) 源码 `1e3*Math.floor(e/1e3)` 接收【毫秒】。
# ============================================================
REPLAY_API = "window.TradingViewApi._replayApi"

# _replayApi 本身或其方法返回值可能是 observable（带 .value() 方法）：
#   兼容开源实现中 _replayApi 为可调用对象的情况，也兼容当前版本返回值需 .value() 解包
#   （若返回值是普通值则原样返回）
REPLAY_UNWRAP = (
    "(function(){ const v = arguments[0]; "
    "if (v !== null && typeof v === 'object' && typeof v.value === 'function') return v.value(); "
    "if (typeof v === 'function') return v(); return v; })"
)


def replay_api_expr(call):
    """构造对 _replayApi 的调用表达式：解包 _replayApi 本体 + 执行 call 并解包返回值。"""
    return (
        "(function() { "
        "  const ra = " + REPLAY_API + "; "
        "  if (!ra) return { error: 'no_replay_api' }; "
        "  const t = (ra && typeof ra === 'object' && typeof ra.value === 'function') ? ra.value() : ra; "
        "  if (!t || typeof t." + call + " !== 'function') "
        "    return { error: 'no_method_" + call + "' }; "
        "  try { "
        "    const v = t." + call + "(); "
        "    const u = (" + REPLAY_UNWRAP + ")(v); "
        "    return { value: u }; "
        "  } catch (e) { return { error: String(e) }; } "
        "})()"
    )


def replay_check(c, log=None):
    """探测 _replayApi 可用性：API 存在 + isReplayAvailable 为 true。

    返回 True 可用；不可用抛 RuntimeError（版本不兼容，不强行适配）。
    """
    log = log or (lambda *a, **k: None)
    try:
        r = c.evaluate(replay_api_expr("isReplayAvailable"))
    except Exception as e:
        raise RuntimeError(f"探测 replay API 失败：{e}")
    if not r or r.get("error") == "no_replay_api":
        raise RuntimeError("当前 TradingView 桌面端没有 _replayApi（版本不兼容），回放模式不可用")
    if r.get("error"):
        raise RuntimeError(f"isReplayAvailable 调用失败：{r['error']}")
    if not r.get("value"):
        raise RuntimeError("当前图表不支持 Bar Replay（isReplayAvailable=false）")
    log("replay API 探测通过：isReplayAvailable=true")
    return True


def replay_show_toolbar(c):
    """打开回放工具栏（返回 Promise，需 await）。"""
    return c.evaluate(replay_api_expr("showReplayToolbar"))


def replay_select_date(c, ts_ms, log=None):
    """selectDate(毫秒) 进入回放；失败 fallback selectFirstAvailableDate()。

    @param ts_ms  回放起点（毫秒时间戳）
    @returns dict：{ value } 或 { error }
    """
    log = log or (lambda *a, **k: None)
    expr = (
        "(function() { "
        "  const ra = " + REPLAY_API + "; "
        "  if (!ra) return { error: 'no_replay_api' }; "
        "  const t = (ra && typeof ra === 'object' && typeof ra.value === 'function') ? ra.value() : ra; "
        "  if (!t || typeof t.selectDate !== 'function') return { error: 'no_method_selectDate' }; "
        "  return t.selectDate(" + str(int(ts_ms)) + ") "
        "    .then(function() { return { value: true }; }) "
        "    .catch(function(err) { "
        "      if (typeof t.selectFirstAvailableDate === 'function') "
        "        return t.selectFirstAvailableDate().then(function() { return { value: 'fallback' }; }); "
        "      return { error: String(err) }; "
        "    }); "
        "})()"
    )
    return c.evaluate(expr)


def replay_current_date(c):
    """currentDate() 返回当前回放位置（秒时间戳）；未启动/不可用时返回 None。"""
    r = c.evaluate(replay_api_expr("currentDate"))
    if not r or "error" in r:
        return None
    v = r.get("value")
    return int(v) if isinstance(v, (int, float)) else None


def replay_started(c):
    """isReplayStarted() 布尔；异常时返回 False。"""
    try:
        r = c.evaluate(replay_api_expr("isReplayStarted"))
        if r and "value" in r:
            return bool(r["value"])
    except Exception:
        pass
    return False


def replay_set_speed(c, ms, log=None):
    """changeAutoplayDelay(ms) 设置自动播放速度；返回 True 成功。"""
    log = log or (lambda *a, **k: None)
    expr = (
        "(function() { "
        "  const ra = " + REPLAY_API + "; "
        "  if (!ra) return { error: 'no_replay_api' }; "
        "  const t = (ra && typeof ra === 'object' && typeof ra.value === 'function') ? ra.value() : ra; "
        "  if (!t || typeof t.changeAutoplayDelay !== 'function') "
        "    return { error: 'no_method_changeAutoplayDelay' }; "
        "  try { t.changeAutoplayDelay(" + str(int(ms)) + "); return { value: true }; } "
        "  catch (e) { return { error: String(e) }; } "
        "})()"
    )
    r = c.evaluate(expr)
    if not r or "error" in r:
        log(f"changeAutoplayDelay 失败：{r}")
        return False
    return True


def replay_toggle_autoplay(c):
    """切换 autoplay 播放/暂停。"""
    return c.evaluate(replay_api_expr("toggleAutoplay"))


def replay_stop(c):
    """stopReplay() 退出回放，返回图表实时态。"""
    return c.evaluate(replay_api_expr("stopReplay"))


def replay_enter(c, ts_sec, log=None):
    """完整进入回放：showReplayToolbar → selectDate(毫秒) → 等 isReplayStarted + currentDate 就绪。

    @param ts_sec  回放起点（秒时间戳）
    @returns 实际生效的回放起点（秒）；失败抛 RuntimeError
    """
    log = log or (lambda *a, **k: None)
    replay_show_toolbar(c)
    r = replay_select_date(c, ts_sec * 1000, log=log)
    if r and "error" in r and r["error"] != "fallback":
        raise RuntimeError(f"进入回放失败：{r['error']}")
    # 轮询等待回放就绪（isReplayStarted + currentDate 返回有效位置）
    deadline = time.time() + 10.0
    pos = None
    while time.time() < deadline:
        if replay_started(c):
            pos = replay_current_date(c)
            if pos:
                log(f"回放已就绪：位置 {fmtT(pos)}")
                return pos
        time.sleep(0.5)
    raise RuntimeError("进入回放超时（isReplayStarted/currentDate 未就绪）")


class LiveMonitor:
    """实时监控：轮询 CDP 最新K线，增量推进链路，检测新进场信号并提醒/画标记。"""

    def __init__(self, symbol=None, periods=None, from_ts=0, port=DEFAULT_CDP_PORT,
                 interval=15.0, tail=100, use_cache=False, log=None):
        self.periods = list(periods or DEFAULT_PERIODS)
        self.cfg = CDPConfig(port=port, periods=self.periods)
        self.interval = float(interval)
        self.tail = int(tail)
        self.log = log or (lambda *a, **k: print(*a))

        # 0. 记录图表初始周期；监控期间图表固定在 3m（主检测周期），退出时恢复原周期
        try:
            with CDPClient(self.cfg, log=self.log) as c:
                r = c.evaluate("String(TradingViewApi.activeChart().resolution());")
                self._display_res = str(r)
                if _ensure_res(c, "3"):
                    time.sleep(FAST_WAIT)
        except Exception:
            self._display_res = self.periods[-1]
        self.log(f"图表初始周期：{self._display_res}（监控期间固定在 3m，退出时恢复）")

        # 1. 全量历史初始化增量引擎
        self.log(f"加载历史K线：symbol={symbol} periods={self.periods} use_cache={use_cache}")
        bars_by_period = load_bars(periods=self.periods, from_ts=from_ts,
                                   use_cache=use_cache, symbol=symbol, log=self.log)
        for res in self.periods:
            n = len(bars_by_period.get(res, []) or [])
            if n:
                last = bars_by_period[res][-1]["time"]
                self.log(f"  {res:>4}: {n} 根（{fmtT(last)} 止）")
        self.engine = BacktestEngine(bars_by_period, periods=self.periods, warmup_bars=0)

        # 2. 预热到最新，初始信号忽略（历史信号不提醒不画）
        last_ts = 0
        for res in self.periods:
            ts = self.engine.bars[res]["_times"]
            if ts and ts[-1] > last_ts:
                last_ts = ts[-1]
        if last_ts:
            init_sigs = self.engine.step_to(last_ts)
            if init_sigs:
                self.log(f"预热完成：忽略初始历史进场信号 {len(init_sigs)} 个")
        self._drawn = set()       # 已画过的 (time, direction)
        # 上次全周期刷新时 3m 最新已收盘 bar 的时间；None 表示首轮强制全周期刷新。
        # 已收盘 bar 判定：3m 尾部倒数第二根（图表最新一根始终是进行中的未收盘 bar）。
        self._last_closed = None
        self.log(f"实时监控就绪：轮询间隔 {self.interval}s，每周期读取末尾 {self.tail} 根")

    def _full_refresh(self, c):
        """全周期切换刷新：逐周期(D/240/60/15/3)读取尾部K线并入增量引擎。

        仅在检测到新的已收盘 3m bar 时调用（约每 3 分钟一次），避免每轮都切 5 个周期。
        返回本轮各周期最新时间戳中的最大值（供 step_to 推进）。
        """
        last_ts = 0
        for res in self.periods:
            try:
                switched = _ensure_res(c, res)
                if switched:
                    time.sleep(RES_WAIT)
                d = read_tail(c, self.tail)
                if not d or not d.get("bars"):
                    self.log(f"  [{res:>4}] 未读到K线")
                    continue
                kind = self.engine.append_bars(res, d["bars"])
                n = len(self.engine.bars[res]["_list"])
                if kind:
                    self.log(f"  [{res:>4}] 并入 {len(d['bars'])} 根（{kind}），累计 {n} 根")
                times = self.engine.bars[res]["_times"]
                if times:
                    last_ts = max(last_ts, times[-1])
            except Exception as e:
                self.log(f"  [{res:>4}] 读取失败（忽略）：{e}")
        return last_ts

    def tick(self):
        """轮询一轮：3m 快速检查，仅在新收盘 3m bar 时全周期刷新 → 推进链路。

        返回本轮新进场信号列表（预热时的历史信号已忽略，这里均为实时新信号）。
        """
        last_ts = 0
        with CDPClient(self.cfg, log=self.log) as c:
            # 1. 3m 快速检查（监控固定周期为 3m，每轮通常零切换零等待）
            try:
                if _ensure_res(c, "3"):
                    time.sleep(FAST_WAIT)
                d = read_tail(c, self.tail)
                if d and d.get("bars"):
                    kind = self.engine.append_bars("3", d["bars"])
                    n = len(self.engine.bars["3"]["_list"])
                    if kind:
                        self.log(f"  [  3] 并入 {len(d['bars'])} 根（{kind}），累计 {n} 根")
                    # 3m 尾部倒数第二根 = 最新已收盘 bar（最新一根始终是进行中 bar）
                    bars3 = self.engine.bars["3"]["_list"]
                    last_closed = bars3[-2]["time"] if len(bars3) >= 2 else None
                    # 首轮（None）或已出现新的收盘 bar → 全周期切换刷新
                    if last_closed is not None and last_closed != self._last_closed:
                        if self._last_closed is not None:
                            self.log(f"  [  3] 检测到新收盘 bar（{fmtT(last_closed)}），全周期刷新")
                        self._last_closed = last_closed
                        last_ts = max(last_ts, self._full_refresh(c))
                        # 全周期刷新后确保回到监控固定周期 3m
                        if _ensure_res(c, "3"):
                            time.sleep(FAST_WAIT)
                    times = self.engine.bars["3"]["_times"]
                    if times:
                        last_ts = max(last_ts, times[-1])
            except Exception as e:
                self.log(f"  [  3] 读取失败（忽略）：{e}")
        if not last_ts:
            return []
        return self.engine.step_to(last_ts)

    def _announce(self, s):
        """控制台醒目提醒 + 系统提示音。"""
        d = "做多" if s["direction"] == "long" else "做空"
        self.log("")
        self.log("=" * 62)
        self.log(f"[实时监控] 新进场信号：{d}（{'BUY' if s['direction'] == 'long' else 'SELL'}）")
        self.log(f"  策略：{s['strategyKey']}  检测周期 {s['periodX']}  背驰级别 {s['markRes']}")
        self.log(f"  信号时间：{fmtT(s['time'])}   价格：{s['price']:.2f}   近支阻：{s['nearSr']:.2f}")
        self.log("=" * 62)
        # Windows 系统提示音（标准库，桌面提醒）
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass

    def _mark_signals(self, sigs):
        """把新信号画成 RT· 箭头（按 (time, direction) 去重，不重复画、不覆盖历史标记）。"""
        todo = [s for s in sigs if (s["time"], s["direction"]) not in self._drawn]
        if not todo:
            return
        try:
            with CDPClient(self.cfg, log=self.log) as c:
                for i in range(0, len(todo), CHUNK):
                    chunk = todo[i:i + CHUNK]
                    r = _draw_signals(c, chunk)
                    n = len(r) if isinstance(r, list) else 0
                    for s in chunk[:n]:
                        self._drawn.add((s["time"], s["direction"]))
                    self.log(f"[实时监控] 已画实时标记（累计 {len(self._drawn)} 个，本次 {n} 个）")
        except Exception as e:
            self.log(f"[实时监控] 画标记失败（忽略）：{e}")

    def restore_chart(self):
        """恢复图表到监控前的用户原周期（退出/停止时调用）。"""
        if not getattr(self, "_display_res", None):
            return
        try:
            with CDPClient(self.cfg, log=self.log) as c:
                _ensure_res(c, self._display_res)
            self.log(f"已恢复图表周期：{self._display_res}")
        except Exception:
            pass

    def run_loop(self):
        """常驻循环：每 interval 秒 tick 一次，检测到新信号则提醒 + 画标记。"""
        self.log("实时监控启动：Ctrl+C 退出（盘面上的 RT· 标记保留）")
        try:
            while True:
                t0 = time.time()
                try:
                    sigs = self.tick()
                except Exception as e:
                    self.log(f"[实时监控] 本轮异常（忽略，下轮继续）：{e}")
                    sigs = []
                for s in sigs:
                    self._announce(s)
                if sigs:
                    self._mark_signals(sigs)
                elapsed = time.time() - t0
                time.sleep(max(1.0, self.interval - elapsed))
        except KeyboardInterrupt:
            self.log("\n已停止实时监控（Ctrl+C）。盘面上的 RT· 标记保留，可用 --clear 清除。")
            # 退出时恢复用户原图表周期
            self.restore_chart()


class ReplayMonitor(LiveMonitor):
    """K线回放回测模式：驱动 TradingView Bar Replay 自动播放历史K线。

    与实时监控的差异：
      - 不预热到最新（回放从起始点逐步推进，避免初始信号被吞）；
      - 数据全量在内存（load_bars），信号计算走内存切片，图表仅作回放动画 + 标记载体；
      - 每轮读 currentDate()（秒）与上次记录比较，按 3m 粒度逐根追赶补齐 step_to，
        中间每根 bar 的信号不漏（增量计算单根 <1ms）；
      - 图表默认驻留 3m；信号触发时暂停回放 → 切到信号周期画箭头 → 停留 hold_sec 秒
        → 切回 3m → 恢复播放（播放粒度始终跟随 3m，切换间隙暂停不产生错乱）；
      - pause/resume 通过 toggleAutoplay 控制；stop 调用 stopReplay + 恢复原周期。
    """

    def __init__(self, symbol=None, periods=None, from_ts=0, port=DEFAULT_CDP_PORT,
                 start_ts=None, speed_ms=1000, hold_sec=2.0, interval=0.5,
                 tail=100, use_cache=False, log=None):
        self.periods = list(periods or DEFAULT_PERIODS)
        self.cfg = CDPConfig(port=port, periods=self.periods)
        self.interval = float(interval)
        self.tail = int(tail)
        self.log = log or (lambda *a, **k: print(*a))
        self.start_ts = start_ts
        self.speed_ms = speed_ms
        self.hold_sec = hold_sec
        self._last_pos = None     # 上次已处理回放位置（秒）
        self._entered = False     # 是否已进入回放
        self._paused = False      # autoplay 是否暂停
        self._stopped = False
        self._finished = False    # 回放是否已结束（isReplayStarted=false）

        # 0. 记录图表初始周期；回放期间默认驻留 3m（信号触发时临时切换），退出时恢复
        try:
            with CDPClient(self.cfg, log=self.log) as c:
                r = c.evaluate("String(TradingViewApi.activeChart().resolution());")
                self._display_res = str(r)
                if _ensure_res(c, "3"):
                    time.sleep(FAST_WAIT)
        except Exception:
            self._display_res = self.periods[-1]
        self.log(f"图表初始周期：{self._display_res}（回放期间默认驻留 3m，退出时恢复）")

        # 1. 全量历史初始化增量引擎（不预热，回放从起始点逐步推进）
        self.log(f"加载历史K线：symbol={symbol} periods={self.periods} use_cache={use_cache}")
        bars_by_period = load_bars(periods=self.periods, from_ts=from_ts,
                                   use_cache=use_cache, symbol=symbol, log=self.log)
        for res in self.periods:
            n = len(bars_by_period.get(res, []) or [])
            if n:
                last = bars_by_period[res][-1]["time"]
                self.log(f"  {res:>4}: {n} 根（{fmtT(last)} 止）")
        self.engine = BacktestEngine(bars_by_period, periods=self.periods, warmup_bars=0)
        self._drawn = set()       # 已画过的 (time, direction)
        self.log(f"回放回测就绪：起点 {fmtT(start_ts) if start_ts else '（使用回放工具栏当前选择）'} "
                 f"速度 {speed_ms}ms/根，信号停留 {hold_sec}s")

    # ---------------- 回放控制 ----------------

    def enter_replay(self):
        """连接 CDP → 探测 replay API → 打开工具栏 → 选日期进入回放 → 设速度 → 启动播放。"""
        with CDPClient(self.cfg, log=self.log) as c:
            replay_check(c, log=self.log)
            if self.start_ts:
                pos = replay_enter(c, self.start_ts, log=self.log)
            else:
                # 未指定起点：打开工具栏后取当前回放位置（或由工具栏默认）
                replay_show_toolbar(c)
                pos = None
                deadline = time.time() + 10.0
                while time.time() < deadline:
                    if replay_started(c):
                        pos = replay_current_date(c)
                        if pos:
                            break
                    time.sleep(0.5)
                if not pos:
                    raise RuntimeError("进入回放超时（未获取到回放位置）")
            self._last_pos = pos
            replay_set_speed(c, self.speed_ms, log=self.log)
            replay_toggle_autoplay(c)   # 启动自动播放
            self._paused = False
            self._entered = True
            self.log(f"回放自动播放已启动：速度 {self.speed_ms}ms/根")

    def pause(self):
        """暂停自动播放（toggleAutoplay）。"""
        if not self._entered or self._paused:
            return
        try:
            with CDPClient(self.cfg, log=self.log) as c:
                replay_toggle_autoplay(c)
            self._paused = True
            self.log("回放已暂停")
        except Exception as e:
            self.log(f"[回放] 暂停失败（忽略）：{e}")

    def resume(self):
        """恢复自动播放（toggleAutoplay）。"""
        if not self._entered or not self._paused:
            return
        try:
            with CDPClient(self.cfg, log=self.log) as c:
                replay_toggle_autoplay(c)
            self._paused = False
            self.log("回放已继续")
        except Exception as e:
            self.log(f"[回放] 继续失败（忽略）：{e}")

    def stop(self):
        """退出回放 + 恢复用户原周期（重复调用安全）。"""
        if self._stopped:
            return
        self._stopped = True
        try:
            with CDPClient(self.cfg, log=self.log) as c:
                try:
                    replay_stop(c)
                except Exception as e:
                    self.log(f"stopReplay 失败（忽略）：{e}")
                # requestCloseReplay 是异步的，轮询等 isReplayStarted 变 false 确认退出
                deadline = time.time() + 5.0
                while time.time() < deadline and replay_started(c):
                    time.sleep(0.3)
        except Exception:
            pass
        self.restore_chart()
        self.log("回放回测已停止，已退出回放并恢复图表周期")

    # ---------------- 主循环 ----------------

    def tick(self):
        """轮询一轮：读 currentDate() 回放位置，追赶补齐逐根推进，处理信号。

        返回本轮新进场信号列表；回放结束返回 [] 并置 _finished=True。
        """
        with CDPClient(self.cfg, log=self.log) as c:
            if not replay_started(c):
                # 已进入但回放结束（到底/用户跳回实时）
                if self._entered:
                    self._finished = True
                    self.log("回放已结束（isReplayStarted=false）")
                return []
            pos = replay_current_date(c)
            if pos is None:
                return []
            if self._last_pos is not None and pos <= self._last_pos:
                return []     # 未推进到新 bar
            sigs = self._catch_up(pos)
            if sigs:
                # 信号触发：暂停 → 切到信号周期画箭头 → 停留 → 切回 3m → 恢复播放
                self._handle_signal_pause(c, sigs)
            return sigs

    def _catch_up(self, pos):
        """从 _last_pos 到 pos 按 3m 粒度逐根补齐，返回新信号列表。"""
        step = 180    # 3m 粒度（秒）
        max_steps = 500
        sigs = []
        if self._last_pos is None:
            # 首次：只推进到当前回放位置（起始时刻不重复触发历史信号）
            self._last_pos = pos
            self.engine.step_to(pos)
            return []
        gap = pos - self._last_pos
        if gap > max_steps * step:
            # 异常卡顿：先快进到最近窗口再对窗口逐根补齐，避免一次性补几千根
            self.log(f"[回放] 落后 {gap // step} 根超过保护阈值 {max_steps}，先快进再补齐最近窗口")
            self.engine.step_to(pos - max_steps * step)
            t = pos - max_steps * step + step
        else:
            t = self._last_pos + step
        n = 0
        while t < pos and n < max_steps:
            s = self.engine.step_to(t)
            if s:
                sigs.extend(s)
            t += step
            n += 1
        s = self.engine.step_to(pos)
        if s:
            sigs.extend(s)
        self._last_pos = pos
        return sigs

    def _handle_signal_pause(self, c, sigs):
        """信号触发：暂停回放 → 切到信号周期画箭头 → 停留 → 切回 3m → 恢复播放。"""
        try:
            replay_toggle_autoplay(c)    # 暂停自动播放
            time.sleep(0.3)
            # 同一批多个周期信号时切到时间最新的那个信号所属周期
            target = max(sigs, key=lambda s: s["time"])
            res = str(target["periodX"])
            self.log(f"[回放] 信号触发，暂停回放，切到 {res} 查看")
            if _ensure_res(c, res):
                time.sleep(RES_WAIT)
            self._mark_signals(c, sigs)
            time.sleep(self.hold_sec)
            if _ensure_res(c, "3"):
                time.sleep(FAST_WAIT)
            replay_toggle_autoplay(c)    # 恢复自动播放
        except Exception as e:
            self.log(f"[回放] 信号处理（暂停/切周期）失败（忽略）：{e}")

    def _mark_signals(self, c, sigs):
        """在指定 CDP 连接上画 RT· 箭头（按 (time, direction) 去重）。"""
        todo = [s for s in sigs if (s["time"], s["direction"]) not in self._drawn]
        if not todo:
            return
        try:
            for i in range(0, len(todo), CHUNK):
                chunk = todo[i:i + CHUNK]
                r = _draw_signals(c, chunk)
                n = len(r) if isinstance(r, list) else 0
                for s in chunk[:n]:
                    self._drawn.add((s["time"], s["direction"]))
                self.log(f"[回放] 已画实时标记（累计 {len(self._drawn)} 个，本次 {n} 个）")
        except Exception as e:
            self.log(f"[回放] 画标记失败（忽略）：{e}")

    def run_loop(self):
        """进入回放 → 循环轮询推进 → 回放结束或 Ctrl+C 停止并恢复。"""
        self.log("回放回测启动：Ctrl+C 退出（盘面上的 RT· 标记保留）")
        try:
            self.enter_replay()
            while not self._stopped and not self._finished:
                t0 = time.time()
                try:
                    sigs = self.tick()
                except Exception as e:
                    self.log(f"[回放] 本轮异常（忽略，下轮继续）：{e}")
                    sigs = []
                for s in sigs:
                    self._announce(s)
                elapsed = time.time() - t0
                time.sleep(max(0.2, self.interval - elapsed))
        except KeyboardInterrupt:
            self.log("\n已停止回放回测（Ctrl+C）。")
        finally:
            if not self._finished:
                self.stop()


def main(argv=None):
    # Windows 控制台默认 GBK，统一转 UTF-8 输出，避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="盘面监控：实时监控 / 软件K线回放回测，进场点提醒 + 盘面标记")
    ap.add_argument("--mode", choices=["live", "replay"], default="live",
                    help="监控模式：live 实时监控（默认）/ replay 软件K线回放回测")
    ap.add_argument("--symbol", default=None, help="品种，如 OANDA:XAUUSD（先切换图表品种再取数）")
    ap.add_argument("--periods", default=",".join(DEFAULT_PERIODS), help="周期列表，默认 D,240,60,15,3")
    ap.add_argument("--from", dest="from_date", default="2026-07-02", help="起始日期 YYYY-MM-DD（UTC）")
    ap.add_argument("--start", dest="start_date", default=None, help="回放起点 YYYY-MM-DD（默认取 --from）")
    ap.add_argument("--port", type=int, default=DEFAULT_CDP_PORT, help="CDP 调试端口，默认 9222")
    ap.add_argument("--interval", type=float, default=15.0, help="轮询间隔秒，默认 15（replay 默认 0.5）")
    ap.add_argument("--speed", type=int, default=1000, help="回放自动播放速度 ms/bar（100~10000）")
    ap.add_argument("--hold", type=float, default=2.0, help="信号触发切周期后停留秒数（默认 2s）")
    ap.add_argument("--tail", type=int, default=100, help="每周期读取末尾K线数，默认 100")
    ap.add_argument("--use-cache", action="store_true",
                    help="优先读 bars_all_tf.json 缓存初始化（不读实时K线）")
    ap.add_argument("--clear", action="store_true", help="清除本 session 画的 RT· 标记后退出")
    args = ap.parse_args(argv)

    if args.clear:
        clear_rt_markers(CDPConfig(port=args.port), log=lambda *a: print(*a))
        return 0

    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    from_ts = parse_from(args.from_date)
    if args.mode == "replay":
        start_ts = parse_from(args.start_date) if args.start_date else from_ts
        monitor = ReplayMonitor(symbol=args.symbol, periods=periods,
                                from_ts=from_ts, port=args.port,
                                start_ts=start_ts, speed_ms=args.speed,
                                hold_sec=args.hold, interval=args.interval,
                                tail=args.tail, use_cache=args.use_cache)
    else:
        monitor = LiveMonitor(symbol=args.symbol, periods=periods,
                              from_ts=from_ts, port=args.port,
                              interval=args.interval, tail=args.tail,
                              use_cache=args.use_cache)
    monitor.run_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
