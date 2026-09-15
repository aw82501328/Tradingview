# -*- coding: utf-8 -*-
"""
三模式 Web 控制台：全量回测（纯后台）/ K线回放 / 实时监控

纯标准库实现（http.server + SSE），统一管理三种模式的启动/暂停/继续/停止，
并把三种模式过程中产生的进场信号实时记录到前端表格。

用法：
    python -m py_chain.webapp --port 8000

架构：
    Web 页面（index.html）→ HTTP 控制接口 + SSE 事件流
      → ModeWorker 后台线程（Backtest/Replay/Live）
      → SignalLog（线程安全信号表）→ SSE 广播到前端表格

并发规则：三种模式同一时间最多运行一种（全局互斥），启动冲突返回 409。
"""

import argparse
import collections
import copy
import math
import json
import os
import queue
import sys
import threading
import tempfile
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, quote

from .data_loader import CDPConfig, DEFAULT_PERIODS, DEFAULT_CDP_PORT, load_bars
from .backtest import BacktestEngine
from .service_restart import RestartManager
from .signal_locator import LocateManager
from .main import parse_from
from .chan_core import fmtT
from .monitor import LiveMonitor, ReplayMonitor, clear_rt_markers
from .marks import draw_signal_marks, draw_sr_marks, clear_signal_marks, clear_all_marks
from . import chan_core
from . import data_store, td_launcher, sr_service, sr_draw, sr_tune, sr_tune_api, sr_preset_excel, analysis_service, analysis_api, bt_runs, param_center, params_api

# ============================================================
# 全局互斥：三种模式同一时间最多运行一种
# ============================================================
_active_lock = threading.Lock()
_active_mode = None          # 当前运行中的模式名（backtest/replay/live）或 None
_active_owner = None
_service_restarting = False


class ChartLock:
    """Atomically coordinate standalone chart operations with mode startup."""
    def __init__(self):
        self._lock = threading.Lock()

    def acquire(self, blocking=False):
        # All current consumers use non-blocking acquisition.
        with _active_lock:
            if _service_restarting:
                return False
            if _active_mode is not None and _active_owner != threading.get_ident():
                return False
            return self._lock.acquire(blocking=False)

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

# 标记操作（marks/draw、marks/sr_draw、marks/clear、sr/*）互斥：
# 都驱动同一张 TradingView 图表，独立锁会让两路 CDP 任务并行切周期
_marks_lock = ChartLock()

# 支阻位调试模块状态：busy 当前操作名（compute/refresh/draw/clear）或 None
_sr_busy = None
_sr_busy_lock = threading.Lock()
# 最近一次计算结果槽（只存结果不存 bars；读多写少，浅拷贝保护）
_sr_result_lock = threading.Lock()

# 参数预设存储（web/sr_presets.json，UTF-8）
SR_PRESETS_FILE = os.path.join(os.path.dirname(__file__), "web", "sr_presets.json")
_presets_lock = threading.Lock()

# 基础数据模块状态：busy 为 None 或 "fetch"；日志环形缓冲 + 进度文案供页面刷新后回放
_data_busy = None
_data_busy_lock = threading.Lock()
_data_logs = collections.deque(maxlen=200)
_data_progress = ""
_data_stop_evt = threading.Event()


def _make_data_callbacks(app):
    """构造基础数据模块的 log/progress 回调（闭包持有 ControlApp）。

    log：环形缓冲 + SSE log 事件（mode='data'，三模式页的监听会过滤掉）；
    progress：进度文案缓存 + SSE progress 事件（label 文案，pct 可选）。
    """
    def log(msg):
        with _data_busy_lock:
            _data_logs.append(f"{time.strftime('%H:%M:%S')} {msg}")
        app.broadcaster.emit("log", {"mode": "data", "msg": msg})

    def progress(label, pct=None):
        global _data_progress
        with _data_busy_lock:
            _data_progress = label
        payload = {"mode": "data", "label": label}
        if pct is not None:
            payload["pct"] = pct
        app.broadcaster.emit("progress", payload)

    return log, progress


def acquire_active(mode):
    """尝试占用模式互斥；成功返回 True，失败返回当前占用者。"""
    global _active_mode, _active_owner
    with _active_lock:
        if _service_restarting:
            return '服务重启中'
        if _active_mode is not None:
            return _active_mode
        if _marks_lock.locked():
            return "图表操作"
        _active_mode = mode
        _active_owner = threading.get_ident()
        return True


def release_active(mode):
    """释放模式互斥（仅当占用者是自己时）。"""
    global _active_mode, _active_owner
    with _active_lock:
        if _active_mode == mode:
            _active_mode = None
            _active_owner = None


def active_mode():
    with _active_lock:
        return _active_mode


def _sr_busy_snapshot():
    with _sr_busy_lock:
        return _sr_busy


def _set_sr_busy(v):
    global _sr_busy
    _sr_busy = v


def ensure_idle():
    """检查三种模式是否均未运行，供标记操作使用。

    @returns (True, None) 全部空闲可操作；或 (False, 提示信息)
    """
    if _service_restarting:
        return False, '服务重启中'
    mode = active_mode()
    if mode is not None:
        return False, f"有 {mode} 模式运行中，请先停止再标记"
    return True, None


# ============================================================
# SignalLog：线程安全信号表
# ============================================================
class SignalLog:
    """记录三种模式产生的进场信号（线程安全），供前端表格查询/实时追加。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []
        self._id = 0
        # 成交回填匹配键：(mode, signalTime, periodX, direction, strategyKey)
        self._key_to_idx = {}

    def _row_key(self, mode, s):
        return (mode, s.get("time") or s.get("signalTime"), s.get("periodX"),
                s.get("direction"), s.get("strategyKey"))

    def append_signal(self, mode, s, symbol=None):
        """记录一条新进场信号，返回该行。"""
        with self.lock:
            self._id += 1
            row = {
                "id": self._id,
                "mode": mode,
                "symbol": symbol or s.get("symbol"),
                "time": s.get("time") or s.get("signalTime"),
                "direction": s.get("direction"),
                "periodX": s.get("periodX"),
                "strategyKey": s.get("strategyKey"),
                "markRes": s.get("markRes"),
                "price": s.get("price"),
                "nearSr": s.get("nearSr"),
                # M1/M2/M4 候选标记（SPEC_divergence_fallback）：fallback=下沉链回退候选、
                # nearEqual=创新低近等容差候选、expectBi=检测周期预期够笔口径；
                # 前端据此在策略列加角标
                "fallback": bool(s.get("fallback", False)),
                "nearEqual": bool(s.get("nearEqual", False)),
                "expectBi": bool(s.get("expectBi", False)),
                "status": "信号",
                "entryTime": None,
                "entryPrice": None,
                "lots": None,
                # 出场相关（成交/出场时回填）
                "stopRef": None,
                "state": None,
                "exitTime": None,
                "exitPrice": None,
                "exitType": None,
                "exits": [],
                "pnl": None,
            }
            self.rows.append(row)
            self._key_to_idx[self._row_key(mode, s)] = len(self.rows) - 1
            return row

    def fill_trade(self, mode, tr, symbol=None):
        """回测成交时回填对应信号行的成交状态（持仓中）；找不到则追加一行记录。"""
        key = (mode, tr.get("signalTime"), tr.get("periodX"),
               tr.get("direction"), tr.get("strategyKey"))
        with self.lock:
            idx = self._key_to_idx.get(key)
            if idx is None:
                self._id += 1
                row = {
                    "id": self._id,
                    "mode": mode,
                    "symbol": symbol or tr.get("symbol"),
                    "time": tr.get("signalTime"),
                    "direction": tr.get("direction"),
                    "periodX": tr.get("periodX"),
                    "strategyKey": tr.get("strategyKey"),
                    "markRes": tr.get("markRes"),
                    "price": tr.get("signalPrice"),
                    "nearSr": tr.get("nearSr"),
                    "fallback": bool(tr.get("fallback", False)),
                    "nearEqual": bool(tr.get("nearEqual", False)),
                    "expectBi": bool(tr.get("expectBi", False)),
                    "status": "持仓中",
                    "entryTime": tr.get("entryTime"),
                    "entryPrice": tr.get("entryPrice"),
                    "fillMode": tr.get("fillMode"),
                    "lots": tr.get("lots"),
                    "stopRef": tr.get("stopRef"),
                    "state": tr.get("state", "open"),
                    "exitTime": None,
                    "exitPrice": None,
                    "exitType": None,
                    "exits": list(tr.get("exits") or []),
                    "pnl": tr.get("pnl"),
                }
                self.rows.append(row)
                return row
            row = self.rows[idx]
            row["status"] = "持仓中"
            row["entryTime"] = tr.get("entryTime")
            row["entryPrice"] = tr.get("entryPrice")
            row["fillMode"] = tr.get("fillMode")
            row["lots"] = tr.get("lots")
            row["stopRef"] = tr.get("stopRef")
            row["state"] = tr.get("state", "open")
            row["exits"] = list(tr.get("exits") or [])
            row["pnl"] = tr.get("pnl")
            return row

    def fill_exit(self, mode, tr):
        """持仓终局（止损/保本止损/全平）时回填出场信息（状态→已平仓）。"""
        key = (mode, tr.get("signalTime"), tr.get("periodX"),
               tr.get("direction"), tr.get("strategyKey"))
        with self.lock:
            idx = self._key_to_idx.get(key)
            if idx is None:
                return None
            row = self.rows[idx]
            row["status"] = "已平仓"
            row["state"] = "closed"
            row["exitTime"] = tr.get("exitTime")
            row["exitPrice"] = tr.get("exitPrice")
            row["exitType"] = tr.get("exitType")
            row["exits"] = list(tr.get("exits") or [])
            row["pnl"] = tr.get("pnl")
            return row

    def fill_suppressed(self, mode, s):
        """同向持仓互斥过滤的信号：状态→同向过滤（保留行，不画箭头）。"""
        key = self._row_key(mode, s)
        with self.lock:
            idx = self._key_to_idx.get(key)
            if idx is None:
                return None
            row = self.rows[idx]
            row["status"] = "同向过滤"
            return row

    def list(self, limit=None, mode=None):
        with self.lock:
            rows = [r for r in self.rows if mode is None or r.get("mode") == mode]
        if limit:
            rows = rows[-int(limit):]
        return rows

    def max_id(self):
        """当前最大行 id（_id 单调递增、clear 不复用，可作"本轮新增行"的基线）。"""
        with self.lock:
            return self._id

    def snapshot(self, mode, min_id=0):
        """深拷贝指定模式 id>min_id 的行（回测方案保存用，避免持引用序列化）。"""
        with self.lock:
            return copy.deepcopy([r for r in self.rows
                                  if r.get("mode") == mode and r.get("id", 0) > min_id])

    def get(self, row_id, mode):
        with self.lock:
            return next((dict(r) for r in self.rows if r['id'] == row_id and r['mode'] == mode), None)

    def clear(self, mode=None):
        with self.lock:
            n = len(self.rows)
            self.rows = [r for r in self.rows if mode is not None and r.get("mode") != mode]
            self._key_to_idx = {self._row_key(r["mode"], r): i for i, r in enumerate(self.rows)}
            # Keep IDs unique for the service lifetime: an in-flight locate must
            # never match a new record created after clearing the table.
            return n - len(self.rows)


# ============================================================
# SSE 广播器
# ============================================================
class Broadcaster:
    """SSE 事件广播：每个订阅一个 queue，emit 时投递到全部订阅。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.subs = set()

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def emit(self, event, data):
        payload = json.dumps(data, ensure_ascii=False, default=str)
        msg = f"event: {event}\ndata: {payload}\n\n"
        dead = []
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.subs.discard(q)


# ============================================================
# ModeWorker：三种模式的统一后台线程
# ============================================================
class ModeWorker:
    """后台任务基类：状态机 idle/running/paused/stopped/done/error，pause/resume/stop 控制。"""

    MODE = "base"

    def __init__(self, signals, broadcaster, log=None):
        self.signals = signals
        self.broadcaster = broadcaster
        self.log_fn = log or (lambda *a, **k: None)
        self.thread = None
        self.cfg = {}
        self.state = "idle"
        # 本次运行开始前的信号表最大行 id：start 不清信号表（连跑多次行会混叠），
        # 保存回测方案时按 id>_row_base 过滤出"本次运行新增行"，保证 cfg 与行配对
        self._row_base = 0
        self.error = None
        self.progress = {"current": 0, "total": 0, "pct": 0}
        self._pause_evt = threading.Event()
        self._stop_evt = threading.Event()
        self.monitor = None

    # ---- 日志 ----
    def log(self, msg):
        self.log_fn(f"[{self.MODE}] {msg}")
        self.broadcaster.emit("log", {"mode": self.MODE, "msg": str(msg)})

    def set_state(self, s):
        self.state = s
        self.broadcaster.emit("status", {"mode": self.MODE, "state": s})

    def set_progress(self, current, total=None):
        if total:
            self.progress = {"current": int(current), "total": int(total),
                             "pct": round(100.0 * current / total, 1)}
        else:
            self.progress = {"current": int(current), "total": None, "pct": None}
        self.broadcaster.emit("progress", {"mode": self.MODE, **self.progress})

    # ---- 信号记录 ----
    def _on_signal(self, s):
        row = self.signals.append_signal(self.MODE, s, symbol=self.cfg.get('symbol'))
        self.broadcaster.emit("signal", {"mode": self.MODE, "row": row})
        d = "做多" if s.get("direction") == "long" else "做空"
        self.log(f"新进场信号：{d} 策略 {s.get('strategyKey')} "
                 f"周期 {s.get('periodX')} @ {fmtT(s.get('time'))} {s.get('price')}")

    def _on_trade(self, tr):
        row = self.signals.fill_trade(self.MODE, tr, symbol=self.cfg.get('symbol'))
        self.broadcaster.emit("signal", {"mode": self.MODE, "row": row})

    def _on_exit(self, tr):
        """持仓终局（止损/保本止损/全平）：行状态→已平仓并推送。"""
        row = self.signals.fill_exit(self.MODE, tr)
        if row is not None:
            self.broadcaster.emit("signal", {"mode": self.MODE, "row": row})
            name = {"stopSr": "支阻位止损", "stopBe": "保本止损", "close": "全平"}.get(
                tr.get("exitType"), tr.get("exitType"))
            self.log(f"出场：{name} {fmtT(tr.get('exitTime'))} "
                     f"@ {tr.get('exitPrice')}（盈亏 {tr.get('pnl', 0):.2f}）")

    def _on_suppressed(self, s):
        """同向持仓互斥过滤的信号：行状态→同向过滤。"""
        row = self.signals.fill_suppressed(self.MODE, s)
        if row is not None:
            self.broadcaster.emit("signal", {"mode": self.MODE, "row": row})

    # ---- 控制 ----
    def start(self, cfg):
        if self.thread and self.thread.is_alive():
            return {"ok": False, "error": f"{self.MODE} 已在运行"}
        holder = acquire_active(self.MODE)
        if holder is not True:
            return {"ok": False, "error": f"当前有 {holder} 模式运行中，请先停止"}
        self.cfg = dict(cfg)
        self._row_base = self.signals.max_id()
        self.error = None
        self._pause_evt = threading.Event()
        self._stop_evt = threading.Event()
        self.progress = {"current": 0, "total": 0, "pct": 0}
        self.thread = threading.Thread(target=self._run_wrapper, daemon=True,
                                       name=f"worker-{self.MODE}")
        self.thread.start()
        return {"ok": True}

    def pause(self):
        self._pause_evt.set()
        self.set_state("paused")
        return {"ok": True}

    def resume(self):
        self._pause_evt.clear()
        self.set_state("running")
        return {"ok": True}

    def stop(self):
        self._stop_evt.set()
        return {"ok": True}

    def _run_wrapper(self):
        try:
            self.set_state("running")
            self._run()
        except Exception as e:
            self.error = str(e)
            self.log(f"运行异常：{e}")
            self.set_state("error")
        finally:
            release_active(self.MODE)
            if self.monitor is not None:
                try:
                    self.monitor.restore_chart()
                except Exception:
                    pass
            self.log(f"已结束（state={self.state}）")

    def _run(self):
        raise NotImplementedError

    def status(self):
        return {"mode": self.MODE, "state": self.state,
                "error": self.error, "progress": self.progress}


class BacktestWorker(ModeWorker):
    """全量回测（纯后台）：BacktestEngine.run() 逐根推进，进度/信号/成交实时推送。"""

    MODE = "backtest"

    @staticmethod
    def _sr_preset_kwargs(cfg, periods):
        """回测「支阻预设」下拉：载入与 /sr、工作台共享的预设（识别参数+叠加开关+人工位），
        以回测周期 ∩ 支阻级别过滤后映射为 compute_srflip kwargs（engine_kwargs_of）；
        30S 是回测时间轴/背驰次级别，不属于支阻级别（白名单刻意拒收），不透传。
        未选预设（空）→ None（走引擎默认：密集区+BOLL；叠加开关只存在于预设的 srTypes）。
        兼容：旧 cfg 里的 overlay_fib/overlay_boll 键不再读取（2026-09-13 移除回测页叠加开关）。"""
        name = str(cfg.get("sr_preset") or "").strip()
        if not name:
            return None
        from .sr_service import engine_kwargs_of
        preset = next((p for p in _presets_load()
                       if isinstance(p, dict) and p.get("name") == name), None)
        if preset is None or not isinstance(preset.get("cfg"), dict):
            raise ValueError(f"未找到支阻预设「{name}」，请刷新预设列表后重选")
        sr_periods = ([p for p in periods
                       if str(p).strip().upper() in sr_service.CANONICAL_LEVELS]
                      or list(sr_service.DEFAULT_LEVELS))
        pcfg = dict(preset["cfg"])
        pcfg.update(periods=sr_periods, symbol=cfg.get("symbol") or pcfg.get("symbol"),
                    **{"from": cfg.get("from") or pcfg.get("from") or "2026-06-30"})
        pcfg = ControlApp.normalize_sr_cfg(pcfg)   # 校验 + manualLevels 按支阻周期过滤
        return engine_kwargs_of(pcfg)

    def _run(self):
        cfg = self.cfg
        periods = cfg.get("periods") or DEFAULT_PERIODS
        # 预热提前（lead_days > 0）：「起始日期=交易开始日」口径——取数自动前移 lead 天
        # 建状态（笔/支阻位/MACD 就绪），引擎 start_ts 前只推进状态不交易、从空仓起步；
        # 0 = 旧口径（从 from 起交易，warmup_bars 根预热）。旧方案库 cfg 无此键 → 0 → 行为不变。
        lead_days = int(cfg.get("lead_days") or 0)
        from_ts = int(cfg.get("from_ts", 0))
        data_from_ts = max(0, from_ts - lead_days * 86400)
        start_ts = from_ts if lead_days > 0 else None
        # 数据源：store=本地SQLite存储（不连CDP，TV关闭可跑）；cache=bars_all_tf.json；live=CDP实时
        src = cfg.get("data_source") or ("cache" if cfg.get("use_cache") else "live")
        if lead_days > 0:
            self.log(f"预热提前 {lead_days} 天：数据起点 {fmtT(data_from_ts)}，交易起点 {fmtT(from_ts)}")
        if src == "store":
            self.log(f"取数：数据源=本地存储 symbol={cfg.get('symbol')} "
                     f"periods={periods} from_ts={data_from_ts}")
            bars = data_store.load_store(cfg.get("symbol"), periods=periods,
                                         from_ts=data_from_ts)
        else:
            self.log(f"取数：数据源={'本地缓存' if src == 'cache' else 'CDP实时'} "
                     f"symbol={cfg.get('symbol')} periods={periods} "
                     f"use_cache={cfg.get('use_cache')}")
            bars = load_bars(periods=periods, from_ts=data_from_ts,
                             use_cache=cfg.get("use_cache", False),
                             symbol=cfg.get("symbol"), log=self.log)
        for res in periods:
            n = len(bars.get(res, []) or [])
            if n:
                self.log(f"  {res:>4}: {n} 根（{fmtT(bars[res][-1]['time'])} 止）")
        # 参数中心（参数配置页统一管理）：API 显式值优先（历史方案复现/后端覆盖能力），
        # 缺省用参数中心当前值；回写 cfg 保证 bt_runs 历史方案快照/对比表显示实际生效参数。
        # diverge_confirm/expect_bi 页面不再传入（None → 引擎读 CHAN_CFG，由缠论核心模块控制）。
        pm = param_center.effective_all()
        chan_core.apply_cfg(pm["chan"])   # 幂等重放（启动已应用；防参数文件被手改）
        ep = pm["entry"]
        for k in ("lots", "slip_stop", "slip_fallback", "slip_be", "near"):
            if cfg.get(k) is None:
                cfg[k] = ep[k]
        engine = BacktestEngine(bars, periods=periods,
                                warmup_bars=cfg.get("warmup", 60),
                                with_marks=cfg.get("with_marks", False),
                                fill_mode=cfg.get("fill_mode", "anchor"),
                                signal_mode=cfg.get("signal_mode", "realtime"),
                                lots=cfg["lots"],
                                slip_stop=cfg["slip_stop"],
                                slip_fallback=cfg["slip_fallback"],
                                slip_be=cfg["slip_be"],
                                near=cfg["near"],
                                sr_kwargs=self._sr_preset_kwargs(cfg, periods),
                                diverge_confirm=cfg.get("diverge_confirm"),
                                expect_bi=cfg.get("expect_bi"),
                                module_params=_engine_module_params(pm))
        self.log(f"回测开始（最小周期 {engine.fine_res}，成交口径 {engine.fill_mode}，"
                 f"信号模式 {'当下背驰' if engine.signal_mode == 'realtime' else '确认制'}，"
                 f"背驰进场 {'分型确认后下一根开盘' if engine.diverge_confirm else '当下'}，"
                 f"检测周期够笔 {'预期' if engine.expect_bi else '分型确认'}）...")
        result = engine.run(
            start_ts=start_ts,
            log=self.log,
            on_progress=self._on_progress,
            on_signal=self._on_signal,
            on_trade=self._on_trade,
            on_exit=self._on_exit,
            on_suppressed=self._on_suppressed,
            paused=self._pause_evt,
            stopped=self._stop_evt,
        )
        if self._stop_evt.is_set():
            self.set_state("stopped")
        else:
            self.set_state("done")
        # 回测结束后回推「持仓中」行：浮动盈亏只在 _finish（循环后）算出，
        # 逐单 fill_trade + 广播把 pnl 推到表格（已平仓行终局时已带 pnl，不动）
        for tr in result["trades"]:
            if tr.get("state") == "closed":
                continue
            row = self.signals.fill_trade(self.MODE, tr, symbol=self.cfg.get('symbol'))
            self.broadcaster.emit("signal", {"mode": self.MODE, "row": row})
        st = result["stats"]
        self.log(f"回测完成：{st['steps']} 步，信号 {st['signals']}，成交 {st['executed']}，"
                 f"同向过滤 {st.get('suppressed', 0)}，已平仓 {st.get('closed', 0)}")

    def _on_progress(self, i, total):
        if i % max(1, total // 100) == 0 or i == total:
            self.set_progress(i, total)


class LiveWorker(ModeWorker):
    """实时监控：后台线程轮询 LiveMonitor.tick()，暂停跳过轮询，停止恢复原周期。"""

    MODE = "live"

    def _run(self):
        cfg = self.cfg
        periods = cfg.get("periods") or DEFAULT_PERIODS
        pm = param_center.effective_all()
        chan_core.apply_cfg(pm["chan"])
        m = LiveMonitor(symbol=cfg.get("symbol"), periods=periods,
                        from_ts=cfg.get("from_ts", 0), port=cfg.get("port", DEFAULT_CDP_PORT),
                        interval=cfg.get("interval", 15.0), tail=cfg.get("tail", 100),
                        use_cache=cfg.get("use_cache", False), log=self.log,
                        module_params=_engine_module_params(pm))
        self.monitor = m
        self.log("实时监控就绪（Ctrl+C 无效，用停止按钮）")
        while not self._stop_evt.is_set():
            if self._pause_evt.is_set():
                time.sleep(0.5)
                continue
            t0 = time.time()
            try:
                sigs = m.tick()
            except Exception as e:
                self.log(f"本轮异常（忽略）：{e}")
                sigs = []
            step = getattr(m, "last_step", None) or {}
            for tr in step.get("fills") or []:
                self._on_trade(tr)
            for tr in step.get("exits") or []:
                self._on_exit(tr)
            for s in step.get("suppressed") or []:
                self._on_suppressed(s)
            for s in sigs:
                self._on_signal(s)
                m._announce(s)
            if sigs:
                m._mark_signals(sigs)
            self.set_progress(0, None)
            elapsed = time.time() - t0
            time.sleep(max(0.5, m.interval - elapsed))
        self.set_state("stopped")


class ReplayWorker(ModeWorker):
    """K线回放：进入回放后轮询 ReplayMonitor.tick()，暂停/继续/停止映射到 autoplay 与 stopReplay。"""

    MODE = "replay"

    def _run(self):
        cfg = self.cfg
        periods = cfg.get("periods") or DEFAULT_PERIODS
        pm = param_center.effective_all()
        chan_core.apply_cfg(pm["chan"])
        m = ReplayMonitor(symbol=cfg.get("symbol"), periods=periods,
                          from_ts=cfg.get("from_ts", 0), port=cfg.get("port", DEFAULT_CDP_PORT),
                          start_ts=cfg.get("start_ts"),
                          speed_ms=cfg.get("speed", 1000), hold_sec=cfg.get("hold", 2.0),
                          interval=cfg.get("interval", 0.5), tail=cfg.get("tail", 100),
                          use_cache=cfg.get("use_cache", False), log=self.log,
                          module_params=_engine_module_params(pm))
        self.monitor = m
        m.enter_replay()
        self.log(f"回放自动播放已启动：速度 {m.speed_ms}ms/根，默认驻留 3m")
        while not self._stop_evt.is_set():
            if self._pause_evt.is_set():
                if not getattr(self, "_paused_done", False):
                    m.pause()
                    self._paused_done = True
                time.sleep(0.5)
                continue
            if getattr(self, "_paused_done", False):
                m.resume()
                self._paused_done = False
            t0 = time.time()
            try:
                sigs = m.tick()
            except Exception as e:
                self.log(f"本轮异常（忽略）：{e}")
                sigs = []
            step = getattr(m, "last_step", None) or {}
            for tr in step.get("fills") or []:
                self._on_trade(tr)
            for tr in step.get("exits") or []:
                self._on_exit(tr)
            for s in step.get("suppressed") or []:
                self._on_suppressed(s)
            for s in sigs:
                self._on_signal(s)
                m._announce(s)
            if m._finished:
                self.set_state("done")
                break
            elapsed = time.time() - t0
            time.sleep(max(0.2, m.interval - elapsed))
        if self._stop_evt.is_set():
            self.set_state("stopped")
        # 停止/结束时退出回放并恢复周期（含 stopReplay + 恢复原周期）
        try:
            m.stop()
        except Exception as e:
            self.log(f"退出回放失败（忽略）：{e}")

    def pause(self):
        if not getattr(self, "_paused_done", False):
            self._pause_evt.set()
        self.set_state("paused")
        return {"ok": True}

    def resume(self):
        self._pause_evt.clear()
        self._paused_done = False
        self.set_state("running")
        return {"ok": True}


# ============================================================
# HTTP 服务
# ============================================================
class ControlApp:
    def __init__(self, tune_store=None):
        self.service = None
        self.signals = SignalLog()
        self.broadcaster = Broadcaster()
        self.locator = LocateManager(self.signals, _marks_lock, self.broadcaster.emit)
        self.workers = {
            "backtest": BacktestWorker(self.signals, self.broadcaster),
            "replay": ReplayWorker(self.signals, self.broadcaster),
            "live": LiveWorker(self.signals, self.broadcaster),
        }
        # 支阻位调试模块：最近一次计算结果槽（cfg 快照 / computed_at / result / meta）
        self.sr = {"cfg": None, "computed_at": None, "result": None, "meta": None}
        # 全量回测历史方案存储（bt_runs/bt_signals 表，建在基础数据库 bars.db 里）
        self.bt_runs = bt_runs.BtRunStore()
        self.sr_tune = sr_tune.TuneManager(store=tune_store, emit=self.broadcaster.emit)
        self.td_launcher = td_launcher.TDLauncher(acquire_active, release_active)
        self.analysis = analysis_service.AnalysisManager(
            self.broadcaster.emit, acquire_active, release_active, _marks_lock,
            self.normalize_sr_cfg, self.publish_analysis_sr)
        # 参数中心：启动时恢复持久化的缠论核心参数（进程内全局生效；
        # points/entry/plan 在每次任务启动时读取，无需预热应用）
        chan_core.apply_cfg(param_center.effective("chan"))

    def publish_analysis_sr(self, cfg, result, meta):
        with _sr_result_lock:
            self.sr = {"cfg": cfg, "computed_at": time.time(), "result": result, "meta": meta}

    def sr_counts(self):
        """支阻位结果概要（小载荷，供 /api/sr/state 与 status() 使用）。"""
        with _sr_result_lock:
            sr = self.sr or {}
            result = sr.get("result") or {}
            computed_at = sr.get("computed_at")
            periods = (sr.get("cfg") or {}).get("periods") or []
        drawn = result.get("drawnByPeriod") or {}
        return {
            "has_result": bool(result),
            "computed_at": computed_at,
            "periods": [str(p) for p in periods],
            "drawn_total": sum(len(v) for v in drawn.values()),
            "merged": len(result.get("merged") or []),
            "by_period": {str(k): len(v) for k, v in drawn.items()},
        }

    @staticmethod
    def normalize_sr_cfg(cfg):
        """把页面字符串配置规范化/校验为引擎类型（数字/周期/类型开关/比率）。
        @raises ValueError 非法值（前端 400）
        @returns 规范化后的 cfg（periods/from_ts 已就绪；draw 相关字段原样透传）
        """
        cfg = dict(cfg or {})
        try:
            periods = sr_service.normalize_periods(
                cfg.get("periods") or sr_service.DEFAULT_LEVELS)
        except ValueError as e:
            raise ValueError(str(e))
        if not periods:
            raise ValueError("至少勾选一个级别（W/D/240/60/15/3）")
        cfg["periods"] = periods
        symbol = str(cfg.get("symbol") or "OANDA:XAUUSD").strip()
        cfg["symbol"] = symbol or "OANDA:XAUUSD"
        from_s = str(cfg.get("from") or sr_service.DEFAULT_FROM)
        try:
            cfg["from_ts"] = parse_from(from_s)
        except Exception:
            raise ValueError(f"起始日期无法解析：{from_s}")
        # 类型开关
        sr_types = [s for s in (cfg.get("srTypes") or []) if s in ("cluster", "fib", "boll")]
        if not sr_types:
            raise ValueError("至少开启一种支阻类型（密集区/黄金分割/BOLL）")
        cfg["srTypes"] = sr_types
        parts = [s for s in cfg.get("clusterParts", ["flip", "recent"])
                 if s in ("flip", "recent")]
        cfg["clusterParts"] = parts
        # 数字字段（非法直接 400）
        floats = {k: float(cfg[k]) for k in
                  ("clusterAtr", "recentClusterAtr", "maxDistAtr",
                   "touchWeight", "barsWeight", "bollMult")
                  if k in cfg and cfg[k] not in (None, "")}
        for k in ("clusterAtr", "recentClusterAtr", "maxDistAtr"):
            if floats.get(k) is not None and floats.get(k) <= 0:
                raise ValueError(f"{k} 须 > 0")
        ints = {k: int(cfg[k]) for k in
                ("maxPerPeriod", "recentBiCount", "sideCount", "bollLength")
                if k in cfg and cfg[k] not in (None, "")}
        for k, lo in (("maxPerPeriod", 1), ("recentBiCount", 1),
                      ("sideCount", 1), ("bollLength", 2)):
            if ints.get(k) is not None and ints[k] < lo:
                raise ValueError(f"{k} 须 >= {lo}")
        cfg.update(floats)
        cfg.update(ints)
        # minTouch 矩阵（级别键 → int >= 1）
        mt = {}
        for res, v in (cfg.get("minTouchs") or {}).items():
            try:
                iv = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{res} 的 minTouch 非法：{v}")
            if iv < 1:
                raise ValueError(f"{res} 的 minTouch 须 >= 1")
            mt[str(res).upper()] = iv
        cfg["minTouchs"] = mt
        cfg["clusterParamsByPeriod"] = sr_tune.normalize_overrides(cfg.get("clusterParamsByPeriod", {}))
        # 黄金分割比率：逗号分隔文本或列表，0 < r < 1 且 <= 6 项
        raw_fib = cfg.get("fibLevels", "0.382,0.5,0.618")
        if isinstance(raw_fib, str):
            raw_fib = [x for x in raw_fib.replace("，", ",").split(",") if x.strip()]
        fibs = []
        for x in raw_fib:
            try:
                v = float(x)
            except (TypeError, ValueError):
                raise ValueError(f"黄金分割比率非法：{x}")
            if not (0 < v < 1):
                raise ValueError(f"黄金分割比率须在 (0,1)：{x}")
            fibs.append(round(v, 4))
        if not fibs or len(fibs) > 6:
            raise ValueError("黄金分割比率须为 1~6 项")
        cfg["fibLevels"] = fibs
        # 人工支阻位：{ 周期: 价位文本/列表 } → { 周期: [float,...] }
        # 键存在 = 该周期支阻位来源=人工（替换密集区；空列表 = 该周期无支阻位）；
        # 未勾选周期的键丢弃不报错（预设移植：载入后临时取消勾选不应报错）
        raw_ml = cfg.get("manualLevels") or {}
        if not isinstance(raw_ml, dict):
            raise ValueError("manualLevels 须为 周期→价位 对象")
        alias = {"1W": "W", "1D": "D", "4H": "240", "1H": "60"}
        ml = {}
        for res, v in raw_ml.items():
            r = alias.get(str(res).strip().upper(), str(res).strip().upper())
            if r not in periods:
                continue
            if isinstance(v, str) and not v.strip():
                ml[r] = []            # 空 = 该周期无支阻位（不回退系统计算）
                continue
            if isinstance(v, (list, tuple)) and not v:
                ml[r] = []
                continue
            try:
                prices = sr_tune.price_list(v)
            except ValueError as e:
                raise ValueError(f"{r} 的人工价位非法：{e}")
            if len(prices) > 50:
                raise ValueError(f"{r} 的人工价位最多 50 条")
            ml[r] = prices
        cfg["manualLevels"] = ml
        # 透传默认值（draw 相关：color/draw_text/draw_raw/clear_first）
        cfg.setdefault("color", "#787B86")
        cfg.setdefault("draw_text", True)
        cfg.setdefault("draw_raw", False)
        cfg.setdefault("clear_first", True)
        return cfg

    @staticmethod
    def normalize_cfg(cfg, mode):
        """把前端字符串配置规范化为引擎所需类型（时间戳/数字/周期列表）。"""
        out = dict(cfg or {})
        for k in ("warmup", "speed", "tail", "port", "lots", "lead_days"):
            if k in out and out[k] not in (None, ""):
                try:
                    out[k] = int(out[k])
                except (TypeError, ValueError):
                    pass
        for k in ("interval", "hold", "slip_stop", "slip_fallback", "slip_be", "near"):
            if k in out and out[k] not in (None, ""):
                try:
                    out[k] = float(out[k])
                except (TypeError, ValueError):
                    pass
        # 支阻预设名（回测「支阻预设」下拉；空 = 不用预设）——仅字符串清洗
        if "sr_preset" in out:
            out["sr_preset"] = str(out["sr_preset"] or "").strip()
        if "use_cache" in out:
            v = out["use_cache"]
            out["use_cache"] = v in (True, "true", "True", "1", 1)
        if out.get("data_source") not in ("live", "cache", "store"):
            out["data_source"] = "live"
        if "periods" in out and isinstance(out["periods"], str):
            out["periods"] = [p.strip() for p in out["periods"].split(",") if p.strip()]
        if "from" in out and out.get("from"):
            try:
                out["from_ts"] = parse_from(str(out["from"]))
            except Exception:
                out["from_ts"] = 0
        if mode == "replay":
            if out.get("start"):
                try:
                    out["start_ts"] = parse_from(str(out["start"]))
                except Exception:
                    out["start_ts"] = None
            else:
                out["start_ts"] = out.get("from_ts", 0)
        return out

    def status(self):
        with _sr_busy_lock:
            busy = _sr_busy
        base = {
            "service": self.service.status() if self.service else None,
            "active": active_mode(),
            "modes": {name: w.status() for name, w in self.workers.items()},
            "analysis": self.analysis.snapshot(),
        }
        try:
            base["sr"] = {"busy": busy, **self.sr_counts()}
        except Exception:
            base["sr"] = {"busy": busy, "has_result": False}
        return base


def _presets_load():
    """读参数预设列表 [{name, saved_at, cfg}]；文件缺失/损坏返回 []。"""
    try:
        with open(SR_PRESETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return list(data) if isinstance(data, list) else []
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def _presets_save(presets):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(SR_PRESETS_FILE), suffix=".tmp", delete=False) as f:
            temp_path = f.name
            json.dump(presets, f, ensure_ascii=False, indent=1, allow_nan=False)
        os.replace(temp_path, SR_PRESETS_FILE)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _params_busy(app):
    """参数中心 chan 模块保存前的忙碌检查：三模式/支阻计算/分析轮次任一运行中即拒绝
    （CHAN_CFG 全局变更会让运行中的单次计算混用两套参数）。"""
    if active_mode() is not None:
        return True
    if _sr_busy_snapshot():
        return True
    job = (app.analysis.snapshot().get("job") or {})
    return job.get("state") in ("pending", "running", "stopping")


def _engine_module_params(pm):
    """参数中心 effective_all → BacktestEngine.module_params 映射（三模式 Worker 共用）。"""
    ep = pm["entry"]
    return {"plan": pm["plan"], "marks": pm["points"],
            "exit_min_merged": ep["exit_min_merged"],
            "realtime_min_bars": ep["realtime_min_bars"],
            "zs_exit_weak_ratio": ep["zs_exit_weak_ratio"]}


def make_handler(app):
    """构造 HTTP 请求处理器（闭包携带 ControlApp）。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return {}

        def log_message(self, fmt, *args):
            pass

        def handle_one_request(self):
            """覆盖基类：读取请求头阶段客户端中断连接（浏览器刷新/关闭）时，
            Windows 抛 ConnectionAbortedError/ConnectionResetError，基类不捕获会刷屏日志。
            这里静默忽略这类连接异常，其余走原逻辑。
            """
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == '/api/signals/locate':
                self._send_json({'ok': True, 'job': app.locator.snapshot()})
                return
            if analysis_api.handle(self, app, "GET"):
                return
            if params_api.handle(self, app, "GET"):
                return
            if bt_runs.handle(self, app, "GET"):
                return
            if self._tune("GET"):
                return
            if path in ("/sr-tune.js", "/sr-tune.css"):
                self._serve_file(path[1:], "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
                return
            if path == "/" or path == "/index.html":
                self._serve_file("workbench.html", "text/html; charset=utf-8")
                return
            if path == "/modes.html":
                self._serve_file("index.html")
                return
            if path in ("/analysis.js", "/service-restart.js", "/analysis-catalog.json", "/shell.css", "/theme.css", "/legacy-theme.css"):
                content_type = "text/javascript" if path.endswith(".js") else "application/json" if path.endswith(".json") else "text/css"
                self._serve_file(path[1:], content_type + "; charset=utf-8")
                return
            if path == "/sr" or path == "/sr.html":
                self._serve_file("sr.html", "text/html; charset=utf-8")
                return
            if path == "/params" or path == "/params.html":
                self._serve_file("params.html", "text/html; charset=utf-8")
                return
            if path == "/api/status":
                self._send_json(app.status())
                return
            if path == "/api/signals":
                qs = parse_qs(parsed.query)
                limit = int(qs.get("limit", ["0"])[0]) or None
                self._send_json({"rows": app.signals.list(limit)})
                return
            if path == "/api/stream":
                self._serve_sse()
                return
            if path == "/api/sr/state":
                self._send_json({"ok": True, "active": active_mode(),
                                 "busy": _sr_busy_snapshot(), **app.sr_counts()})
                return
            if path == "/api/sr/result":
                with _sr_result_lock:
                    sr = app.sr or {}
                    has = sr.get("result") is not None
                    if not has:
                        self._send_json({"ok": False, "error": "尚无计算结果，请先计算"})
                        return
                    payload = {
                        "ok": True, "has_result": True,
                        "computed_at": sr.get("computed_at"),
                        "cfg": sr.get("cfg"),
                        "meta": sr.get("meta"),
                        "periods": sr["result"].get("periods") or {},
                        "merged": sr["result"].get("merged") or [],
                        "drawn_by_period": sr["result"].get("drawnByPeriod") or {},
                        "current_price": sr["result"].get("currentPrice"),
                        "period_atrs": sr["result"].get("periodAtrs") or {},
                    }
                self._send_json(payload)
                return
            if path == "/api/sr/presets":
                with _presets_lock:
                    self._send_json({"ok": True, "presets": _presets_load()})
                return
            if path == "/data" or path == "/data.html":
                self._serve_file("data.html", "text/html; charset=utf-8")
                return
            if path == "/api/data/list":
                self._send_json({"ok": True, "stores": data_store.list_stores()})
                return
            if path == "/api/data/status":
                with _data_busy_lock:
                    self._send_json({"ok": True, "busy": _data_busy,
                                     "progress": _data_progress,
                                     "logs": list(_data_logs)})
                return
            if path == "/api/data/stats":
                self._send_json({"ok": True, "stats": data_store.stats()})
                return
            if path == "/api/data/bars":
                self._serve_data_bars(parsed)
                return
            self._send_json({"ok": False, "error": f"未知路径 {path}"}, 404)

        def do_POST(self):
            global _service_restarting, _data_busy, _data_progress
            path = urlparse(self.path).path
            if path == '/api/service/restart':
                self._read_body()
                origin = self.headers.get('Origin')
                if origin and urlparse(origin).netloc != self.headers.get('Host'):
                    self._send_json({'ok': False, 'error': '不允许跨站重启请求'}, 403)
                    return
                if app.service is None:
                    self._send_json({'ok': False, 'error': '服务重启尚未初始化'}, 503)
                    return
                with _active_lock:
                    _service_restarting = True
                try:
                    service = app.service.prepare()
                except Exception as exc:
                    with _active_lock:
                        _service_restarting = False
                    self._send_json({'ok': False, 'error': str(exc)}, 503)
                    return
                try:
                    self._send_json({'ok': True, 'service': service}, 202)
                    self.wfile.flush()
                finally:
                    app.service.commit()
                return
            if _service_restarting:
                self.close_connection = True
                self._send_json({'ok': False, 'error': '服务重启中，请稍候'}, 503)
                return
            if analysis_api.handle(self, app, "POST"):
                return
            if params_api.handle(self, app, "POST", lambda: _params_busy(app)):
                return
            if bt_runs.handle(self, app, "POST"):
                return
            if self._tune("POST"):
                return
            if path == '/api/signals/locate':
                body = self._read_body()
                if (not isinstance(body, dict) or body.get('mode') not in ('backtest', 'replay', 'live')
                        or type(body.get('id')) is not int or body['id'] <= 0):
                    self._send_json({'ok': False, 'error': '无效的模式或记录ID'}, 400)
                    return
                # 单行标记颜色（可选）：非 dict 视为缺省，由后端默认色兜底
                colors = body.get('colors')
                if not isinstance(colors, dict):
                    colors = None
                try:
                    job = app.locator.start(body['mode'], body['id'], colors=colors)
                except ValueError as exc:
                    self._send_json({'ok': False, 'error': str(exc)}, 400)
                except LookupError as exc:
                    self._send_json({'ok': False, 'error': str(exc)}, 404)
                except RuntimeError as exc:
                    self._send_json({'ok': False, 'error': str(exc)}, 409)
                except Exception as exc:
                    self._send_json({'ok': False, 'error': str(exc)}, 500)
                else:
                    self._send_json({'ok': True, 'job': job}, 202)
                return
            if path in ("/api/signals/clear", "/api/marks/draw", "/api/marks/sr_draw"):
                body = self._read_body()
                if not isinstance(body, dict) or ("mode" in body and body["mode"] not in ("backtest", "replay", "live")):
                    self._send_json({"ok": False, "error": "无效的mode"}, 400)
                    return
                selected_mode = body.get("mode")
            if path == "/api/signals/clear":
                n = app.signals.clear(selected_mode)
                app.broadcaster.emit("signals_cleared", {"n": n, "mode": selected_mode})
                self._send_json({"ok": True, "cleared": n})
                return
            if path == "/api/marks/draw":
                ok, err = ensure_idle()
                if not ok:
                    self._send_json({"ok": False, "error": err}, 409)
                    return
                colors = body.get("colors") or {}
                rows = app.signals.list(None, mode=selected_mode)
                if not rows:
                    self._send_json({"ok": False, "error": "信号列表为空，无可标记的进场点"})
                    return
                if not _marks_lock.acquire(blocking=False):
                    self._send_json({"ok": False, "error": "已有标记操作进行中，请稍候"}, 409)
                    return

                def _job():
                    def log(msg):
                        app.broadcaster.emit("log", {"mode": "mark", "msg": str(msg)})
                    err = None
                    try:
                        draw_signal_marks(rows, cfg=CDPConfig(),
                                          clear_first=True, colors=colors, log=log)
                    except Exception as e:  # 含 CDPError（读超时/页面无响应）——错误透出给前端
                        err = str(e)
                        try:
                            log(f"标记失败：{e}")
                        except Exception:
                            pass
                    finally:
                        _marks_lock.release()
                        app.broadcaster.emit("mark_done", {"op": "draw", "error": err})

                threading.Thread(target=_job, daemon=True, name="marks-draw").start()
                self._send_json({"ok": True, "started": True})
                return
            if path == "/api/marks/sr_draw":
                # 「标记支阻位」：近支阻价位画 11 根K线宽横线（中心=进场点K线，只在背驰周期显示）
                ok, err = ensure_idle()
                if not ok:
                    self._send_json({"ok": False, "error": err}, 409)
                    return
                colors = body.get("colors") or {}
                rows = app.signals.list(None, mode=selected_mode)
                if not rows:
                    self._send_json({"ok": False, "error": "信号列表为空，无可标记的支阻位"}, 409)
                    return
                if not any(r.get("nearSr") is not None for r in rows):
                    self._send_json({"ok": False, "error": "信号列表没有「近支阻」非空的行"}, 409)
                    return
                if not _marks_lock.acquire(blocking=False):
                    self._send_json({"ok": False, "error": "已有标记操作进行中，请稍候"}, 409)
                    return

                def _job():
                    def log(msg):
                        app.broadcaster.emit("log", {"mode": "mark", "msg": str(msg)})
                    err = None
                    try:
                        draw_sr_marks(rows, cfg=CDPConfig(),
                                      clear_first=True, colors=colors, log=log)
                    except Exception as e:  # 含 CDPError（读超时/页面无响应）——错误透出给前端
                        err = str(e)
                        try:
                            log(f"支阻位标记失败：{e}")
                        except Exception:
                            pass
                    finally:
                        _marks_lock.release()
                        app.broadcaster.emit("mark_done", {"op": "sr_draw", "error": err})

                threading.Thread(target=_job, daemon=True, name="marks-sr-draw").start()
                self._send_json({"ok": True, "started": True})
                return
            if path == "/api/marks/clear":
                ok, err = ensure_idle()
                if not ok:
                    self._send_json({"ok": False, "error": err}, 409)
                    return
                if not _marks_lock.acquire(blocking=False):
                    self._send_json({"ok": False, "error": "已有标记操作进行中，请稍候"}, 409)
                    return

                def _job():
                    def log(msg):
                        app.broadcaster.emit("log", {"mode": "mark", "msg": str(msg)})
                    err = None
                    removed = None
                    try:
                        # 清全部系统标记：ML·（箭头+支阻横线）/ BT·（回测）/ RT·（实时）
                        removed = clear_all_marks(cfg=CDPConfig(), log=log)
                    except Exception as e:  # 含 CDPError（读超时/页面无响应）——错误透出给前端
                        err = str(e)
                        try:
                            log(f"清除失败：{e}")
                        except Exception:
                            pass
                    finally:
                        _marks_lock.release()
                        app.broadcaster.emit("mark_done",
                                             {"op": "clear", "error": err, "removed": removed})

                threading.Thread(target=_job, daemon=True, name="marks-clear").start()
                self._send_json({"ok": True, "started": True})
                return
            if path == "/api/sr/presets/excel/export":
                name = str(self._read_body().get("name") or "").strip()
                with _presets_lock:
                    preset = next((p for p in _presets_load() if p.get("name") == name), None)
                if preset is None:
                    self._send_json({"ok": False, "error": "预设不存在，请先保存"}, 404)
                    return
                try:
                    payload = sr_preset_excel.export_preset(name, preset["cfg"])
                except (ValueError, TypeError) as e:
                    self._send_json({"ok": False, "error": str(e)}, 400)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name + ".xlsx", safe=""))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == "/api/sr/presets/excel/import":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if not 0 < length <= sr_preset_excel.LIMIT:
                        self.close_connection = True
                        raise ValueError("请选择不超过 2 MB 的 .xlsx 文件")
                    source_name, cfg = sr_preset_excel.import_preset(self.rfile.read(length))
                    name = parse_qs(urlparse(self.path).query).get("name", [source_name])[0].strip()
                    if not name or len(name) > 40:
                        raise ValueError("预设名须为 1~40 字符")
                    # Validate without replacing original types or dropping additional settings.
                    def finite(value):
                        if isinstance(value, float) and not math.isfinite(value):
                            raise ValueError("参数须为有限数字")
                        if isinstance(value, dict):
                            for v in value.values(): finite(v)
                        if isinstance(value, list):
                            for v in value: finite(v)
                    finite(cfg)
                    normalized = app.normalize_sr_cfg(cfg)
                    finite(normalized)
                    for k in ("recentBiCount", "maxPerPeriod", "sideCount", "bollLength"):
                        if k in cfg and float(cfg[k]) != normalized[k]:
                            raise ValueError(k + " 须为整数")
                    for p, v in cfg.get("minTouchs", {}).items():
                        if float(v) != int(v):
                            raise ValueError(p + " 的 minTouch 须为整数")
                    for k in ("draw_text", "draw_raw"):
                        if k in cfg and not isinstance(cfg[k], bool):
                            raise ValueError(k + " 须为 boolean 类型")
                except (ValueError, TypeError, AttributeError, OverflowError, RecursionError) as e:
                    self._send_json({"ok": False, "error": "导入失败：" + str(e)}, 400)
                    return
                with _presets_lock:
                    presets = _presets_load()
                    overwritten = any(p.get("name") == name for p in presets)
                    out = [p for p in presets if p.get("name") != name]
                    out.append({"name": name, "saved_at": int(time.time()), "cfg": cfg})
                    try:
                        _presets_save(out)
                    except OSError as e:
                        self._send_json({"ok": False, "error": "导入保存失败：" + str(e)}, 500)
                        return
                self._send_json({"ok": True, "name": name, "cfg": cfg, "overwritten": overwritten})
                return
            if path == "/api/sr/presets":
                # POST 保存/覆盖预设（同名覆盖）
                body = self._read_body()
                name = str(body.get("name") or "").strip()
                if not name or len(name) > 40:
                    self._send_json({"ok": False, "error": "预设名须为 1~40 字符"}, 400)
                    return
                pcfg = body.get("cfg")
                if not isinstance(pcfg, dict):
                    self._send_json({"ok": False, "error": "缺少预设配置 cfg"}, 400)
                    return
                overwritten = False
                with _presets_lock:
                    presets = _presets_load()
                    out = []
                    for p in presets:
                        if str(p.get("name") or "") == name:
                            overwritten = True
                            continue
                        out.append(p)
                    out.append({"name": name, "saved_at": int(time.time()), "cfg": pcfg})
                    try:
                        _presets_save(out)
                    except OSError as e:
                        self._send_json({"ok": False, "error": f"预设保存失败：{e}"}, 500)
                        return
                self._send_json({"ok": True, "overwritten": overwritten})
                return
            # ---- 支阻位调试模块（/api/sr/*，复用 _marks_lock 保证与 marks 操作互斥）----
            if path == "/api/sr/compute":
                ok, err = ensure_idle()
                if not ok:
                    self._send_json({"ok": False, "error": err}, 409)
                    return
                if not _marks_lock.acquire(blocking=False):
                    self._send_json({"ok": False, "error": "已有标记/支阻操作进行中，请稍候"}, 409)
                    return
                body = self._read_body()
                mode = "refresh" if str(body.get("mode")) == "refresh" else "auto"
                try:
                    cfg = ControlApp.normalize_sr_cfg(body.get("cfg") or {})
                except ValueError as e:
                    _marks_lock.release()
                    self._send_json({"ok": False, "error": str(e)}, 400)
                    return
                with _sr_busy_lock:
                    _set_sr_busy("refresh" if mode == "refresh" else "compute")

                def _job():
                    def log(msg):
                        app.broadcaster.emit("log", {"mode": "sr", "msg": str(msg)})

                    def prog(phase, cur=0, total=0):
                        # 取数 0–70、重建笔 80、计算 95、全部完成后 100（到头）
                        if phase == "fetch":
                            pct = int(cur / total * 70) if total else 0
                        else:
                            pct = {"bis": 80, "compute": 95, "done": 100}.get(phase, 0)
                        app.broadcaster.emit("progress", {"mode": "sr", "phase": phase,
                                                          "current": cur, "total": total,
                                                          "pct": pct})
                    err = None
                    counts = None
                    try:
                        prog("fetch", 0, 1)
                        bars = sr_service.ensure_data(cfg["periods"], cfg["from_ts"],
                                                      log=log, refresh=(mode == "refresh"),
                                                      symbol=cfg["symbol"])
                        prog("bis")
                        result, meta = sr_service.build_chain_result(bars, cfg, log=log)
                        with _sr_result_lock:
                            app.sr["cfg"] = cfg
                            app.sr["computed_at"] = int(time.time())
                            app.sr["result"] = result
                            app.sr["meta"] = meta
                        counts = app.sr_counts()
                        prog("compute", 1, 1)
                        prog("done", 1, 1)
                        log(f"计算完成：当前价 {result.get('currentPrice')}，"
                            f"候选池 {len(result.get('merged') or [])} 条，"
                            f"图上 {sum(len(v) for v in (result.get('drawnByPeriod') or {}).values())} 条")
                    except Exception as e:  # 含 CDPError——原样透出给前端
                        err = str(e)
                        try:
                            log(f"支阻位计算失败：{e}")
                        except Exception:
                            pass
                    finally:
                        with _sr_busy_lock:
                            _set_sr_busy(None)
                        _marks_lock.release()
                        app.broadcaster.emit("sr_done", {"op": "compute",
                                                         "error": err, "counts": counts})
                threading.Thread(target=_job, daemon=True, name="sr-compute").start()
                self._send_json({"ok": True, "started": True})
                return
            if path == "/api/sr/draw":
                ok, err = ensure_idle()
                if not ok:
                    self._send_json({"ok": False, "error": err}, 409)
                    return
                if not _marks_lock.acquire(blocking=False):
                    self._send_json({"ok": False, "error": "已有标记/支阻操作进行中，请稍候"}, 409)
                    return
                body = self._read_body()
                with _sr_result_lock:
                    has = (app.sr or {}).get("result") is not None
                    result = (app.sr or {}).get("result")
                    cfg = (app.sr or {}).get("cfg") or {}
                if not has:
                    _marks_lock.release()
                    self._send_json({"ok": False, "error": "尚无计算结果，请先计算"}, 409)
                    return
                color = str(body.get("color") or cfg.get("color") or "#787B86")
                clear_first = body.get("clear_first", cfg.get("clear_first", True))
                draw_text = bool(body.get("draw_text", cfg.get("draw_text", True)))
                draw_raw = bool(body.get("draw_raw", cfg.get("draw_raw", False)))
                with _sr_busy_lock:
                    _set_sr_busy("draw")

                def _job():
                    def log(msg):
                        app.broadcaster.emit("log", {"mode": "sr", "msg": str(msg)})
                    err = None
                    res = None
                    try:
                        main_by_period = sr_service.main_lines(result)
                        raw_by_period = sr_service.raw_pool_lines(
                            result, maxDistAtr=float(cfg.get("maxDistAtr") or 3.0)) \
                            if draw_raw else None
                        res = sr_draw.draw_sr_lines(
                            main_by_period=main_by_period, raw_by_period=raw_by_period,
                            cfg=CDPConfig(), clear_first=bool(clear_first),
                            color=color, draw_text=draw_text, log=log)
                    except Exception as e:
                        err = str(e)
                        try:
                            log(f"支阻位画图失败：{e}")
                        except Exception:
                            pass
                    finally:
                        with _sr_busy_lock:
                            _set_sr_busy(None)
                        _marks_lock.release()
                        app.broadcaster.emit("sr_done", {"op": "draw", "error": err, "counts": res})
                threading.Thread(target=_job, daemon=True, name="sr-draw").start()
                self._send_json({"ok": True, "started": True})
                return
            if path == "/api/sr/clear":
                ok, err = ensure_idle()
                if not ok:
                    self._send_json({"ok": False, "error": err}, 409)
                    return
                if not _marks_lock.acquire(blocking=False):
                    self._send_json({"ok": False, "error": "已有标记/支阻操作进行中，请稍候"}, 409)
                    return
                with _sr_busy_lock:
                    _set_sr_busy("clear")

                def _job():
                    def log(msg):
                        app.broadcaster.emit("log", {"mode": "sr", "msg": str(msg)})
                    err = None
                    removed = None
                    try:
                        removed = sr_draw.clear_sr_test_marks(cfg=CDPConfig(), log=log)
                    except Exception as e:
                        err = str(e)
                        try:
                            log(f"支阻线清除失败：{e}")
                        except Exception:
                            pass
                    finally:
                        with _sr_busy_lock:
                            _set_sr_busy(None)
                        _marks_lock.release()
                        app.broadcaster.emit("sr_done", {"op": "clear", "error": err,
                                                         "removed": removed})
                threading.Thread(target=_job, daemon=True, name="sr-clear").start()
                self._send_json({"ok": True, "started": True})
                return
            if path == "/api/data/fetch":
                body = self._read_body()
                symbol = str(body.get("symbol") or "").strip()
                periods = body.get("periods") or list(DEFAULT_PERIODS)
                if isinstance(periods, str):
                    periods = [p.strip() for p in periods.split(",") if p.strip()]
                mode = body.get("mode") or "auto"
                if not symbol:
                    self._send_json({"ok": False, "error": "缺少品种 symbol"}, 400)
                    return
                if mode not in ("auto", "live"):
                    self._send_json({"ok": False, "error": f"非法拉取方式 {mode}"}, 400)
                    return
                if not periods:
                    self._send_json({"ok": False, "error": "缺少周期 periods"}, 400)
                    return
                try:
                    from_ts = parse_from(str(body.get("from") or "")) if body.get("from") else 0
                except Exception:
                    self._send_json({"ok": False, "error": "起始日期非法（YYYY-MM-DD）"}, 400)
                    return
                try:
                    to_ts = parse_from(str(body.get("to"))) if body.get("to") else None
                except Exception:
                    self._send_json({"ok": False, "error": "结束日期非法（YYYY-MM-DD）"}, 400)
                    return
                if to_ts and to_ts <= from_ts:
                    self._send_json({"ok": False, "error": "结束日期须晚于起始日期"}, 400)
                    return
                # 互斥：与三模式/分析/标记互斥（sr-tune 双锁先例——active 挡模式启动，
                # marks lock 挡图表操作；data 拉取全程驱动共享图表）
                with _data_busy_lock:
                    if _data_busy:
                        self._send_json(
                            {"ok": False, "error": f"数据拉取进行中（{_data_busy}）"}, 409)
                        return
                holder = acquire_active("data")
                if holder is not True:
                    self._send_json(
                        {"ok": False, "error": f"当前有 {holder} 模式运行中，请先停止"}, 409)
                    return
                if not _marks_lock.acquire(blocking=False):
                    release_active("data")
                    self._send_json(
                        {"ok": False, "error": "已有标记/支阻操作进行中，请稍后再试"}, 409)
                    return
                with _data_busy_lock:
                    _data_busy = "fetch"
                    _data_progress = ""
                _data_stop_evt.clear()
                log, progress = _make_data_callbacks(app)

                def _fetch_job():
                    global _data_busy, _data_progress
                    err = None
                    summary = None
                    try:
                        summary = data_store.fetch_and_store(
                            symbol, periods, from_ts, to_ts=to_ts, mode=mode,
                            log=log, progress=progress, stop_evt=_data_stop_evt)
                    except Exception as e:
                        err = str(e)
                        log(f"拉取失败：{e}")
                    finally:
                        with _data_busy_lock:
                            _data_busy = None
                            _data_progress = ""
                        _marks_lock.release()
                        release_active("data")
                        app.broadcaster.emit("data_fetch_done", {
                            "error": err, "symbol": symbol, "summary": summary})

                threading.Thread(target=_fetch_job, daemon=True,
                                 name="data-fetch").start()
                self._send_json({"ok": True, "started": True})
                return
            if path == "/api/data/stop":
                with _data_busy_lock:
                    busy = _data_busy
                if not busy:
                    self._send_json({"ok": False, "error": "当前没有进行中的拉取"}, 409)
                    return
                _data_stop_evt.set()
                self._send_json({"ok": True})
                return
            # /api/{mode}/start|pause|resume|stop
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "api" and parts[1] in app.workers:
                mode, action = parts[1], parts[2]
                worker = app.workers[mode]
                body = self._read_body()
                if action == "start":
                    cfg = body.get("cfg") or body
                    cfg = ControlApp.normalize_cfg(cfg, mode)
                    r = worker.start(cfg)
                elif action == "pause":
                    r = worker.pause()
                elif action == "resume":
                    r = worker.resume()
                elif action == "stop":
                    r = worker.stop()
                else:
                    r = {"ok": False, "error": f"未知动作 {action}"}
                code = 409 if (not r.get("ok") and "运行" in str(r.get("error", ""))) else 200
                self._send_json(r, code)
                return
            self._send_json({"ok": False, "error": f"未知路径 {path}"}, 404)

        def do_DELETE(self):
            path = urlparse(self.path).path
            if bt_runs.handle(self, app, "DELETE"):
                return
            if self._tune("DELETE"):
                return
            if path == "/api/data":
                qs = parse_qs(urlparse(self.path).query)
                symbol = str(qs.get("symbol", [""])[0]).strip()
                res = str(qs.get("res", [""])[0]).strip()
                if not symbol:
                    self._send_json({"ok": False, "error": "缺少品种 symbol"}, 400)
                    return
                with _data_busy_lock:
                    if _data_busy:
                        self._send_json(
                            {"ok": False, "error": "数据拉取进行中，请先停止"}, 409)
                        return
                if res:  # 行级：只删该品种此周期
                    if not data_store.delete_res(symbol, res):
                        self._send_json(
                            {"ok": False,
                             "error": f"存储中没有 {symbol} 的 {res} 周期"}, 404)
                        return
                elif not data_store.delete_store(symbol):
                    self._send_json({"ok": False, "error": f"存储中没有品种 {symbol}"}, 404)
                    return
                self._send_json({"ok": True})
                return
            if path == "/api/sr/presets":
                qs = parse_qs(urlparse(self.path).query)
                name = str(qs.get("name", [""])[0]).strip()
                if not name:
                    self._send_json({"ok": False, "error": "缺少预设名 name"}, 400)
                    return
                removed = False
                with _presets_lock:
                    presets = _presets_load()
                    out = [p for p in presets
                           if str(p.get("name") or "") != name]
                    if len(out) != len(presets):
                        removed = True
                        try:
                            _presets_save(out)
                        except OSError as e:
                            self._send_json({"ok": False, "error": f"预设删除失败：{e}"}, 500)
                            return
                if not removed:
                    self._send_json({"ok": False, "error": f"预设不存在：{name}"}, 404)
                    return
                self._send_json({"ok": True, "removed": name})
                return
            self._send_json({"ok": False, "error": f"未知路径 {path}"}, 404)

        def _tune(self, method):
            return sr_tune_api.handle(self, app, method, {
                "acquire_active": acquire_active, "release_active": release_active,
                "marks_lock": _marks_lock, "set_busy": _set_sr_busy,
                "normalize_cfg": ControlApp.normalize_sr_cfg})

        def _serve_data_bars(self, parsed):
            """明细查询：JSON 分页（默认）或 CSV 全量导出（format=csv）。"""
            import csv
            import io
            qs = parse_qs(parsed.query)
            symbol = str(qs.get("symbol", [""])[0]).strip()
            res = str(qs.get("res", [""])[0]).strip()
            if not symbol or not res:
                self._send_json({"ok": False, "error": "缺少 symbol/res 参数"}, 400)
                return

            def _ts(key):
                v = str(qs.get(key, [""])[0]).strip()
                if not v:
                    return None
                try:
                    return parse_from(v)
                except Exception:
                    raise ValueError(f"{key} 日期非法（YYYY-MM-DD）")

            try:
                from_ts = _ts("from") or 0
                to_ts = _ts("to")
                offset = max(0, int(qs.get("offset", ["0"])[0]))
                limit = min(1000, max(1, int(qs.get("limit", ["100"])[0])))
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
                return
            order = qs.get("order", ["asc"])[0]
            if order not in ("asc", "desc"):
                order = "asc"
            try:
                if qs.get("format", [""])[0] == "csv":
                    # 全量导出（不分页），UTF-8 BOM 供 Excel 直开
                    q = data_store.query_bars(symbol, res, from_ts, to_ts,
                                              offset=0, limit=10 ** 9, order=order)
                    buf = io.StringIO()
                    buf.write("﻿")
                    w = csv.writer(buf)
                    w.writerow(["time", "datetime", "open", "high", "low", "close"])
                    for r in q["rows"]:
                        dt = datetime.fromtimestamp(r["time"] + 8 * 3600, tz=timezone.utc)
                        w.writerow([r["time"],
                                    dt.strftime("%Y-%m-%d %H:%M:%S"),
                                    r["open"], r["high"], r["low"], r["close"]])
                    body = buf.getvalue().encode("utf-8")
                    safe = "".join(c if c.isalnum() or c in "._-" else "_"
                                   for c in symbol)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="bars_{safe}_{res}.csv"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                q = data_store.query_bars(symbol, res, from_ts, to_ts,
                                          offset=offset, limit=limit, order=order)
                self._send_json({"ok": True, **q})
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, 404)

        def _serve_file(self, name, content_type="text/html; charset=utf-8"):
            """服务 py_chain/web/ 下静态文件（index.html / sr.html）。"""
            path = os.path.join(os.path.dirname(__file__), "web", name)
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self._send_json({"ok": False, "error": f"缺少 web/{name}"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = app.broadcaster.subscribe()
            try:
                # 发送初始状态，便于前端建表
                self.wfile.write(b"event: init\ndata: "
                                 + json.dumps(app.status(), ensure_ascii=False).encode("utf-8")
                                 + b"\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            # Windows 下浏览器关闭/刷新页面会中止连接，抛 ConnectionAbortedError（WinError 10053）
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                app.broadcaster.unsubscribe(q)

    return Handler


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="三模式 Web 控制台：回测/回放/实时监控")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    ap.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    args = ap.parse_args(argv)

    app = ControlApp()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    def shutdown_service():
        app.analysis.close()
        app.sr_tune.stop_event.set()
        for worker in app.workers.values():
            worker._stop_evt.set()
        deadline = time.monotonic() + 9
        threads = [w.thread for w in app.workers.values()] + [app.analysis.thread, app.sr_tune.thread]
        for thread in threads:
            if thread and thread.is_alive():
                thread.join(max(0, deadline - time.monotonic()))
        server.shutdown()
    app.service = RestartManager(args.host, args.port, shutdown_service)
    print(f"三模式 Web 控制台已启动：http://{args.host}:{args.port}")
    print("三种模式同一时间最多运行一种；Ctrl+C 退出。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出 Web 控制台。")
    finally:
        app.analysis.close()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
