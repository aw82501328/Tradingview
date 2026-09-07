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
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .data_loader import CDPConfig, DEFAULT_PERIODS, DEFAULT_CDP_PORT, load_bars
from .backtest import BacktestEngine
from .main import parse_from
from .chan_core import fmtT
from .monitor import LiveMonitor, ReplayMonitor, clear_rt_markers
from .marks import draw_signal_marks, draw_sr_marks, clear_signal_marks, clear_all_marks

# ============================================================
# 全局互斥：三种模式同一时间最多运行一种
# ============================================================
_active_lock = threading.Lock()
_active_mode = None          # 当前运行中的模式名（backtest/replay/live）或 None

# 标记操作（marks/draw、marks/clear）互斥：后台线程执行期间防止并发
_marks_lock = threading.Lock()


def acquire_active(mode):
    """尝试占用模式互斥；成功返回 True，失败返回当前占用者。"""
    global _active_mode
    with _active_lock:
        if _active_mode is not None:
            return _active_mode
        _active_mode = mode
        return True


def release_active(mode):
    """释放模式互斥（仅当占用者是自己时）。"""
    global _active_mode
    with _active_lock:
        if _active_mode == mode:
            _active_mode = None


def active_mode():
    with _active_lock:
        return _active_mode


def ensure_idle():
    """检查三种模式是否均未运行，供标记操作使用。

    @returns (True, None) 全部空闲可操作；或 (False, 提示信息)
    """
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

    def append_signal(self, mode, s):
        """记录一条新进场信号，返回该行。"""
        with self.lock:
            self._id += 1
            row = {
                "id": self._id,
                "mode": mode,
                "time": s.get("time") or s.get("signalTime"),
                "direction": s.get("direction"),
                "periodX": s.get("periodX"),
                "strategyKey": s.get("strategyKey"),
                "markRes": s.get("markRes"),
                "price": s.get("price"),
                "nearSr": s.get("nearSr"),
                "status": "信号",
                "entryTime": None,
                "entryPrice": None,
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

    def fill_trade(self, mode, tr):
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
                    "time": tr.get("signalTime"),
                    "direction": tr.get("direction"),
                    "periodX": tr.get("periodX"),
                    "strategyKey": tr.get("strategyKey"),
                    "markRes": tr.get("markRes"),
                    "price": tr.get("signalPrice"),
                    "nearSr": tr.get("nearSr"),
                    "status": "持仓中",
                    "entryTime": tr.get("entryTime"),
                    "entryPrice": tr.get("entryPrice"),
                    "fillMode": tr.get("fillMode"),
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

    def list(self, limit=None):
        with self.lock:
            rows = list(self.rows)
        if limit:
            rows = rows[-int(limit):]
        return rows

    def clear(self):
        with self.lock:
            n = len(self.rows)
            self.rows = []
            self._key_to_idx = {}
            self._id = 0
            return n


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
        row = self.signals.append_signal(self.MODE, s)
        self.broadcaster.emit("signal", {"mode": self.MODE, "row": row})
        d = "做多" if s.get("direction") == "long" else "做空"
        self.log(f"新进场信号：{d} 策略 {s.get('strategyKey')} "
                 f"周期 {s.get('periodX')} @ {fmtT(s.get('time'))} {s.get('price')}")

    def _on_trade(self, tr):
        row = self.signals.fill_trade(self.MODE, tr)
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

    def _run(self):
        cfg = self.cfg
        periods = cfg.get("periods") or DEFAULT_PERIODS
        self.log(f"取数：symbol={cfg.get('symbol')} periods={periods} "
                 f"use_cache={cfg.get('use_cache')}")
        bars = load_bars(periods=periods, from_ts=cfg.get("from_ts", 0),
                         use_cache=cfg.get("use_cache", False),
                         symbol=cfg.get("symbol"), log=self.log)
        for res in periods:
            n = len(bars.get(res, []) or [])
            if n:
                self.log(f"  {res:>4}: {n} 根（{fmtT(bars[res][-1]['time'])} 止）")
        engine = BacktestEngine(bars, periods=periods,
                                warmup_bars=cfg.get("warmup", 60),
                                with_marks=cfg.get("with_marks", False),
                                fill_mode=cfg.get("fill_mode", "anchor"),
                                signal_mode=cfg.get("signal_mode", "realtime"))
        self.log(f"回测开始（最小周期 {engine.fine_res}，成交口径 {engine.fill_mode}，"
                 f"信号模式 {'当下背驰' if engine.signal_mode == 'realtime' else '确认制'}）...")
        result = engine.run(
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
            row = self.signals.fill_trade(self.MODE, tr)
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
        m = LiveMonitor(symbol=cfg.get("symbol"), periods=periods,
                        from_ts=cfg.get("from_ts", 0), port=cfg.get("port", DEFAULT_CDP_PORT),
                        interval=cfg.get("interval", 15.0), tail=cfg.get("tail", 100),
                        use_cache=cfg.get("use_cache", False), log=self.log)
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
        m = ReplayMonitor(symbol=cfg.get("symbol"), periods=periods,
                          from_ts=cfg.get("from_ts", 0), port=cfg.get("port", DEFAULT_CDP_PORT),
                          start_ts=cfg.get("start_ts"),
                          speed_ms=cfg.get("speed", 1000), hold_sec=cfg.get("hold", 2.0),
                          interval=cfg.get("interval", 0.5), tail=cfg.get("tail", 100),
                          use_cache=cfg.get("use_cache", False), log=self.log)
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
    def __init__(self):
        self.signals = SignalLog()
        self.broadcaster = Broadcaster()
        self.workers = {
            "backtest": BacktestWorker(self.signals, self.broadcaster),
            "replay": ReplayWorker(self.signals, self.broadcaster),
            "live": LiveWorker(self.signals, self.broadcaster),
        }

    @staticmethod
    def normalize_cfg(cfg, mode):
        """把前端字符串配置规范化为引擎所需类型（时间戳/数字/周期列表）。"""
        out = dict(cfg or {})
        for k in ("warmup", "speed", "tail", "port"):
            if k in out and out[k] not in (None, ""):
                try:
                    out[k] = int(out[k])
                except (TypeError, ValueError):
                    pass
        for k in ("interval", "hold"):
            if k in out and out[k] not in (None, ""):
                try:
                    out[k] = float(out[k])
                except (TypeError, ValueError):
                    pass
        if "use_cache" in out:
            v = out["use_cache"]
            out["use_cache"] = v in (True, "true", "True", "1", 1)
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
        return {
            "active": active_mode(),
            "modes": {name: w.status() for name, w in self.workers.items()},
        }


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
                pass

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/" or path == "/index.html":
                self._serve_index()
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
            self._send_json({"ok": False, "error": f"未知路径 {path}"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/signals/clear":
                n = app.signals.clear()
                app.broadcaster.emit("signals_cleared", {"n": n})
                self._send_json({"ok": True, "cleared": n})
                return
            if path == "/api/marks/draw":
                ok, err = ensure_idle()
                if not ok:
                    self._send_json({"ok": False, "error": err}, 409)
                    return
                body = self._read_body()
                colors = body.get("colors") or {}
                rows = app.signals.list(None)
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
                body = self._read_body()
                colors = body.get("colors") or {}
                rows = app.signals.list(None)
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

        def _serve_index(self):
            import os
            path = os.path.join(os.path.dirname(__file__), "web", "index.html")
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self._send_json({"ok": False, "error": "缺少 web/index.html"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
    print(f"三模式 Web 控制台已启动：http://{args.host}:{args.port}")
    print("三种模式同一时间最多运行一种；Ctrl+C 退出。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出 Web 控制台。")
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
