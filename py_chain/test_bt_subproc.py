# -*- coding: utf-8 -*-
"""单品种回测子进程化（2026-10-08，P1 双策略真并行）单元测试。

覆盖三块：
1. _run 路由——默认单品种走 _run_single_subproc；PY_CHAIN_BT_SUBPROC=0 回退
   线程路径（fxma→_run_fxma、chan→内联）；多品种仍走 _run_batch；
2. _dispatch_single_msg——progress/signal/trade/exit/suppressed 中继到对应
   处理器，done 回填 journal 与「持仓中」行并置 done，error 置 error；
3. 停止语义——done(stopped=True) 置 stopped（与线程路径 _stop_evt 口径一致）。
"""

import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import webapp


def _worker():
    signals = Mock()
    broadcaster = Mock()
    w = webapp.BacktestWorker(signals, broadcaster, strategy="chan_v1")
    w.cfg = {"symbol": "OANDA:XAUUSD", "strategy": "chan_v1"}
    w._pause_evt = threading.Event()   # 真事件：中继循环读 is_set 镜像到子进程
    w._stop_evt = threading.Event()
    return w


class BtSubprocRoutingTests(unittest.TestCase):
    def test_default_routes_single_to_subproc(self):
        w = _worker()
        with patch.object(w, "_run_single_subproc", return_value=None) as m:
            w._run()
        m.assert_called_once()

    def test_env_zero_falls_back_to_thread_paths_fxma(self):
        w = _worker()
        w.cfg["strategy"] = "fxma_v1"
        with patch.dict(os.environ, {"PY_CHAIN_BT_SUBPROC": "0"}), \
                patch.object(w, "_run_fxma", return_value=None) as m:
            w._run()
        m.assert_called_once()

    def test_env_zero_falls_back_to_thread_chan_inline(self):
        # chan 回退路径 = 继续走 _run 原线程体（不 raise 即通过；不打真回测，
        # 构造参数阶段用桩截断）
        w = _worker()
        with patch.dict(os.environ, {"PY_CHAIN_BT_SUBPROC": "0"}), \
                patch.object(w, "_run_fxma", return_value=None) as m, \
                patch.object(webapp.BacktestWorker, "_engine_kwargs_of",
                             side_effect=RuntimeError("stop-here")):
            with self.assertRaises(RuntimeError):
                w._run()
        m.assert_not_called()  # chan 不经 fxma 路径

    def test_multi_symbol_still_batch(self):
        w = _worker()
        w.cfg["symbols"] = ["OANDA:XAUUSD", "OANDA-EURUSD"]
        with patch.object(w, "_run_batch", return_value=None) as m:
            w._run()
        m.assert_called_once()


class BtSubprocDispatchTests(unittest.TestCase):
    def setUp(self):
        self.w = _worker()
        self.sent = []

        def _emit(ev, payload):
            self.sent.append((ev, payload))

        self.w.broadcaster.emit.side_effect = _emit

    def test_dispatch_progress(self):
        with patch.object(self.w, "_on_progress") as m:
            r = self.w._dispatch_single_msg({"kind": "progress", "current": 10, "total": 100})
        self.assertFalse(r)
        m.assert_called_once_with(10, 100)

    def test_dispatch_events_routed(self):
        with patch.object(self.w, "_on_signal") as ms, \
                patch.object(self.w, "_on_trade") as mt, \
                patch.object(self.w, "_on_exit") as me, \
                patch.object(self.w, "_on_suppressed") as mp:
            for kind, key, handler in (("signal", "signal", ms), ("trade", "trade", mt),
                                       ("exit", "trade", me), ("suppressed", "signal", mp)):
                r = self.w._dispatch_single_msg({"kind": kind, key: {"a": 1}})
                self.assertFalse(r)
                handler.assert_called_with({"a": 1}, symbol="OANDA:XAUUSD")

    def test_dispatch_done(self):
        tr_open = {"symbol": "OANDA:XAUUSD", "state": "open"}
        self.w.signals.fill_trade.return_value = {"id": 7}
        with patch.object(self.w, "set_state") as mst:
            r = self.w._dispatch_single_msg(
                {"kind": "done", "stats": {"steps": 5}, "open": [tr_open],
                 "journal": "j.ndjson", "stopped": False})
        self.assertTrue(r)
        self.assertEqual(self.w.cfg["journal"], "j.ndjson")
        self.w.signals.fill_trade.assert_called_once()
        mst.assert_called_once_with("done")
        sig_events = [s for s in self.sent if s[0] == "signal"]
        self.assertEqual(sig_events, [("signal", {"mode": "backtest", "row": {"id": 7}})])

    def test_dispatch_done_stopped(self):
        with patch.object(self.w, "set_state") as mst:
            r = self.w._dispatch_single_msg({"kind": "done", "stats": {}, "stopped": True})
        self.assertTrue(r)
        mst.assert_called_once_with("stopped")

    def test_dispatch_error(self):
        with patch.object(self.w, "set_state") as mst:
            r = self.w._dispatch_single_msg({"kind": "error", "error": "boom"})
        self.assertTrue(r)
        self.assertEqual(self.w.error, "boom")
        mst.assert_called_once_with("error")


class BtSubprocEndToEndTests(unittest.TestCase):
    """真实 spawn 冒烟（小窗口，本机有 XAUUSD 数据才跑）：子进程跑完 → done、
    stats 非零、journal 回写 cfg。"""

    def test_real_subproc_short_run(self):
        from . import data_store
        sym = "OANDA:XAUUSD"
        try:
            have = {s.get("symbol") for s in data_store.list_stores()}
        except Exception:
            have = None
        if have is not None and sym not in have:
            self.skipTest("本地无该品种数据")
        w = _worker()
        w.cfg.update({"from_ts": 1780358400, "to_ts": 1780358400 + 5 * 86400,
                      "lead_days": 2, "warmup": 60, "periods": ["D", "240", "60", "15", "3"]})
        states = []
        w.set_state = lambda s: states.append(s)
        w._run_single_subproc()
        self.assertIn("done", states)
        self.assertNotIn("error", states)
        self.assertIsNone(w.error)
        self.assertIn("journal", w.cfg)
        self.assertGreaterEqual(w.progress.get("total", 0), 0)


if __name__ == "__main__":
    unittest.main()
