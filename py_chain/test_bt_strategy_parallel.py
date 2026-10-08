# -*- coding: utf-8 -*-
"""多策略并行回测（2026-10-02）单元测试。

覆盖三块：
1. SignalLog 策略命名空间——同品种同时刻信号在两个策略下各自成行、成交/出场
   回填不串写、list/snapshot/clear 按策略隔离；
2. 回测=纯后台任务（2026-10-07 数据源固定本地存储）——不注册模式锁不占图表，
   回测运行中 ensure_idle/定位/其他模式启动全部放行，参数保存仍被回测阻塞；
3. /api/backtest/* 按策略路由——start/pause/stop 路由到对应策略 Worker，
   未知策略 400；status() 的 backtests 逐策略形状。
"""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import webapp
from . import module_registry


def _reset_mode_locks():
    webapp._active_mode = None
    webapp._active_owner = None


class BtStrategySignalLogTests(unittest.TestCase):
    def setUp(self):
        _reset_mode_locks()
        self.log = webapp.SignalLog()
        self.base = {"symbol": "OANDA:XAUUSD", "time": 100, "periodX": "3",
                     "direction": "long", "strategyKey": "waitBuy"}

    def test_two_strategies_two_rows_and_no_cross_fill(self):
        a = self.log.append_signal("backtest", dict(self.base), strategy="chan_v1")
        b = self.log.append_signal("backtest", dict(self.base), strategy="fxma_v1")
        self.assertIsNot(a, None)
        self.assertIsNot(b, None)
        self.assertEqual(a["strategy"], "chan_v1")
        self.assertEqual(b["strategy"], "fxma_v1")
        # 缠论V1 的成交只回填缠论行；fxma 行保持「信号」状态
        # （tr 与引擎成交字典同形：带 signalTime/periodX/direction/strategyKey）
        tr = {"symbol": "OANDA:XAUUSD", "signalTime": 100, "periodX": "3",
              "direction": "long", "strategyKey": "waitBuy",
              "entryPrice": 2001, "entryTime": 110, "lots": 0.1, "pnl": 5}
        row = self.log.fill_trade("backtest", dict(tr), symbol="OANDA:XAUUSD",
                                  strategy="chan_v1")
        self.assertEqual(row["id"], a["id"])
        self.assertEqual(row["status"], "持仓中")
        fxma_row = self.log.list(mode="backtest", strategy="fxma_v1")[0]
        self.assertEqual(fxma_row["status"], "信号")
        # 出场同样按策略回填
        closed = self.log.fill_exit("backtest", dict(tr, exitTime=200, exitPrice=2010,
                                                     exitType="stopSr", pnl=9),
                                    symbol="OANDA:XAUUSD", strategy="chan_v1")
        self.assertEqual(closed["status"], "已平仓")
        self.assertEqual(self.log.list(mode="backtest", strategy="fxma_v1")[0]["status"], "信号")

    def test_strategy_scoped_list_snapshot_clear(self):
        self.log.append_signal("backtest", dict(self.base), strategy="chan_v1")
        self.log.append_signal("backtest", dict(self.base), strategy="fxma_v1")
        self.log.append_signal("replay", dict(self.base), strategy="chan_v1")
        self.assertEqual(len(self.log.list(mode="backtest")), 2)
        self.assertEqual(len(self.log.list(mode="backtest", strategy="chan_v1")), 1)
        self.assertEqual(len(self.log.list(mode="backtest", strategy="fxma_v1")), 1)
        # snapshot（保存方案）：只含该策略的行
        self.assertEqual([r["strategy"] for r in
                          self.log.snapshot("backtest", strategy="fxma_v1")], ["fxma_v1"])
        # 清 chan_v1：fxma 行与 replay 行保留
        removed = self.log.clear("backtest", strategy="chan_v1")
        self.assertEqual(removed, 1)
        self.assertEqual([r["strategy"] for r in self.log.list()], ["fxma_v1", "chan_v1"])
        self.assertEqual([r["mode"] for r in self.log.list()], ["backtest", "replay"])

    def test_suppressed_key_is_strategy_scoped(self):
        # fxma 的同向过滤不该拦截缠论V1 的同键信号（两策略独立判定）
        self.log.fill_suppressed("backtest", dict(self.base), symbol="OANDA:XAUUSD",
                                 strategy="fxma_v1")
        row = self.log.append_signal("backtest", dict(self.base), strategy="chan_v1")
        self.assertIsNot(row, None)
        again = self.log.append_signal("backtest", dict(self.base), strategy="fxma_v1")
        self.assertIsNone(again)   # fxma 键已被过滤：拒收


class BtOpenPushbackTests(unittest.TestCase):
    """回测结束「持仓中」回推必须原位更新，不得追加重复行（2026-10-08 修复）。

    引擎 trade 字典不带 strategy 字段；子进程 done/batch done/线程路径收尾的
    fill_trade 若不传 worker 的 strategy，行键 (mode, strategy, ...) 不匹配 →
    走"找不到则追加"分支：每笔期末未平仓交易在 /api/signals 里出现两行
    （运行中的持仓中旧行 + 一条带完整出场明细的新行）。"""

    SYM = "OANDA:XAUUSD"

    def _worker_with_signal_row(self):
        app = _FakeApp()
        w = app.bt_workers["chan_v1"]
        w._on_signal({"time": 100, "symbol": self.SYM, "periodX": "15",
                      "direction": "short", "strategyKey": "wait3Sell",
                      "price": 4172.8}, symbol=self.SYM)
        return app, w

    OPEN_TR = {"signalTime": 100, "periodX": "15", "direction": "short",
               "strategyKey": "wait3Sell", "entryTime": 110, "entryPrice": 4172.5,
               "lots": 4, "state": "open", "pnl": 91.0,
               "exits": [{"type": "trailStop", "lots": 1.0}]}

    def _assert_single_updated_row(self, app):
        rows = app.signals.list(mode="backtest")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strategy"], "chan_v1")
        self.assertEqual(rows[0]["status"], "持仓中")
        self.assertEqual(len(rows[0]["exits"]), 1)
        self.assertEqual(rows[0]["pnl"], 91.0)

    def test_subproc_done_pushback_updates_in_place(self):
        app, w = self._worker_with_signal_row()
        done = w._dispatch_single_msg({"kind": "done", "symbol": self.SYM,
                                       "stats": {"steps": 1},
                                       "open": [dict(self.OPEN_TR)]})
        self.assertTrue(done)
        self._assert_single_updated_row(app)

    def test_batch_done_pushback_updates_in_place(self):
        app, w = self._worker_with_signal_row()
        w.batch = {self.SYM: {"state": "running"}}
        sym = w._dispatch_batch_msg({"kind": "done", "symbol": self.SYM,
                                     "stats": {"steps": 1},
                                     "open": [dict(self.OPEN_TR)]})
        self.assertEqual(sym, self.SYM)
        self._assert_single_updated_row(app)


class BtBackgroundTaskTests(unittest.TestCase):
    """回测固定本地存储后为纯后台任务：不注册模式锁、不占图表（2026-10-07）。

    口径：Worker.start 已保证同策略串行；_acquire_mode 只拦服务重启期；
    active_mode() 在回测运行期间保持 None，ensure_idle/ChartLock/其他模式
    启动全部放行；_params_busy 仍被运行中的回测阻塞（批量子进程逐品种读参数中心）。
    """

    def setUp(self):
        _reset_mode_locks()
        self.app = _FakeApp()

    def tearDown(self):
        _reset_mode_locks()

    def _mark_running(self, sid):
        """启动该策略 Worker 的占位线程并走真实 _acquire_mode 口径。"""
        w = self.app.bt_workers[sid]
        self.assertIs(w._acquire_mode({"data_source": "store"}), True)
        evt = threading.Event()
        w.thread = threading.Thread(target=evt.wait, daemon=True)
        w.thread.start()
        self.addCleanup(evt.set)
        return w

    def test_backtest_does_not_hold_mode_lock(self):
        self._mark_running("chan_v1")
        self._mark_running("fxma_v1")
        self.assertIsNone(webapp.active_mode())
        # 标记操作/定位的闸门全放行
        self.assertEqual(webapp.ensure_idle(), (True, None))
        self.assertTrue(webapp._marks_lock.acquire(blocking=False))
        webapp._marks_lock.release()
        # 回放/实时可在回测运行中启动（回测纯本地不碰图表）
        self.assertIs(webapp.acquire_active("replay"), True)
        webapp.release_active("replay")

    def test_backtest_can_start_during_other_mode(self):
        self.assertIs(webapp.acquire_active("replay"), True)
        self.addCleanup(webapp.release_active, "replay")
        # 回测不参与模式互斥：replay 运行中仍可启动回测
        w = self.app.bt_workers["chan_v1"]
        self.assertIs(w._acquire_mode({"data_source": "store"}), True)
        w._release_mode()   # 空操作：不清别人的模式锁
        self.assertEqual(webapp.active_mode(), "replay")

    def test_same_strategy_start_rejected_while_running(self):
        # 同策略串行守卫在 Worker.start（须真实实例；_FakeApp 的 start 是 Mock）
        w = webapp.BacktestWorker(self.app.signals, self.app.broadcaster,
                                  strategy="chan_v1")
        self.assertIs(w._acquire_mode({"data_source": "store"}), True)
        evt = threading.Event()
        w.thread = threading.Thread(target=evt.wait, daemon=True)
        w.thread.start()
        self.addCleanup(evt.set)
        reply = w.start({"strategy": "chan_v1"})
        self.assertFalse(reply["ok"])
        self.assertIn("已在运行", reply["error"])

    def test_backtest_rejected_while_service_restarting(self):
        webapp._service_restarting = True
        self.addCleanup(setattr, webapp, "_service_restarting", False)
        w = self.app.bt_workers["chan_v1"]
        self.assertEqual(w._acquire_mode({}), "服务重启中")

    def test_params_busy_while_backtest_running(self):
        self._mark_running("chan_v1")
        self.assertTrue(webapp._params_busy(self.app))


class _FakeApp:
    """ControlApp 的可测替身：真实 bt_workers 工厂 + Mock start/pause/stop。"""

    def __init__(self):
        self.signals = webapp.SignalLog()
        self.broadcaster = SimpleNamespace(emit=Mock())
        self.analysis = SimpleNamespace(snapshot=lambda: {"job": None})
        self.bt_workers = {}
        for sid in module_registry.strategy_ids():
            w = webapp.BacktestWorker(self.signals, self.broadcaster, strategy=sid)
            w.start = Mock(return_value={"ok": True})
            w.pause = Mock(return_value={"ok": True})
            w.stop = Mock(return_value={"ok": True})
            self.bt_workers[sid] = w
        default = module_registry.DEFAULT_STRATEGY
        self.workers = {"backtest": self.bt_workers[default],
                        "replay": Mock(), "live": Mock()}


class BtStrategyRoutingTests(unittest.TestCase):
    def setUp(self):
        _reset_mode_locks()
        self.app = _FakeApp()

    def tearDown(self):
        _reset_mode_locks()

    def request(self, path, body):
        reply = []

        handler = SimpleNamespace(
            path=path, _read_body=lambda: body, _tune=lambda _: False,
            _send_json=lambda obj, code=200: reply.append((obj, code)))
        with patch.object(webapp.analysis_api, "handle", return_value=False):
            webapp.make_handler(self.app).do_POST(handler)
        return reply

    def test_start_routes_by_strategy(self):
        for sid in module_registry.strategy_ids():
            reply = self.request("/api/backtest/start", {"cfg": {"strategy": sid}})
            self.assertEqual(reply[0][1], 200, reply)
            worker = self.app.bt_workers[sid]
            worker.start.assert_called_once()
            self.assertEqual(worker.start.call_args.args[0]["strategy"], sid)
        # 无 strategy 的请求 → 默认策略 Worker（旧调用方兼容）
        for w in self.app.bt_workers.values():
            w.start.reset_mock()
        reply = self.request("/api/backtest/start", {"cfg": {}})
        self.assertEqual(reply[0][1], 200, reply)
        self.app.bt_workers[module_registry.DEFAULT_STRATEGY].start.assert_called_once()

    def test_unknown_strategy_rejected(self):
        reply = self.request("/api/backtest/start", {"cfg": {"strategy": "nope"}})
        self.assertEqual(reply[0][1], 400)

    def test_pause_stop_route_by_strategy(self):
        sid = "fxma_v1" if "fxma_v1" in self.app.bt_workers else module_registry.DEFAULT_STRATEGY
        self.request("/api/backtest/stop", {"strategy": sid})
        self.app.bt_workers[sid].stop.assert_called_once()
        for other, w in self.app.bt_workers.items():
            if other != sid:
                w.stop.assert_not_called()
        # 缺省 → 默认策略
        self.request("/api/backtest/pause", {})
        self.app.bt_workers[module_registry.DEFAULT_STRATEGY].pause.assert_called_once()

    def test_bt_status_aggregate_shape(self):
        # 用轻量桩直接驱动聚合逻辑（不构造完整 ControlApp）
        fake = SimpleNamespace(
            bt_workers={
                "chan_v1": SimpleNamespace(state="running", strategy="chan_v1",
                                           error=None, ended_at=None,
                                           progress={"current": 1, "total": 2, "pct": 50},
                                           batch={"A": {"state": "running"}}),
                "fxma_v1": SimpleNamespace(state="done", strategy="fxma_v1",
                                           error=None, ended_at=100.0,
                                           progress=None, batch=None),
            })
        agg = webapp.ControlApp._bt_status(fake)
        self.assertEqual(agg["state"], "running")           # 任一运行中 → running
        self.assertEqual(agg["batch"], {"A": {"state": "running"}})
        fake.bt_workers["chan_v1"].state = "done"
        fake.bt_workers["chan_v1"].ended_at = 200.0
        agg = webapp.ControlApp._bt_status(fake)
        self.assertEqual(agg["state"], "done")              # 全部结束 → 最近结束状态
        fake.bt_workers["fxma_v1"].state = "paused"
        fake.bt_workers["chan_v1"].state = "paused"
        agg = webapp.ControlApp._bt_status(fake)
        self.assertEqual(agg["state"], "paused")


if __name__ == "__main__":
    unittest.main()
