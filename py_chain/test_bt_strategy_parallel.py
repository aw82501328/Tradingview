# -*- coding: utf-8 -*-
"""多策略并行回测（2026-10-02）单元测试。

覆盖三块：
1. SignalLog 策略命名空间——同品种同时刻信号在两个策略下各自成行、成交/出场
   回填不串写、list/snapshot/clear 按策略隔离；
2. acquire_bt/release_bt 多持有者锁——不同策略可并行、与 replay/live 互斥、
   spares_chart 取全体交集、最后一个退出才释放模式锁；
3. /api/backtest/* 按策略路由——start/pause/stop 路由到对应策略 Worker，
   未知策略 400；status() 的 backtests 逐策略形状。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import webapp
from . import module_registry


def _reset_mode_locks():
    webapp._active_mode = None
    webapp._active_owner = None
    webapp._active_spares_chart = False
    webapp._bt_holders.clear()


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


class BtParallelLockTests(unittest.TestCase):
    def setUp(self):
        _reset_mode_locks()

    def tearDown(self):
        _reset_mode_locks()

    def test_parallel_strategies_share_backtest_mode(self):
        self.assertIs(webapp.acquire_bt("chan_v1", spares_chart=True), True)
        self.assertIs(webapp.acquire_bt("fxma_v1", spares_chart=True), True)   # 并行
        self.assertEqual(webapp._active_mode, "backtest")
        self.assertTrue(webapp._active_spares_chart)
        # 其他模式仍与回测互斥
        self.assertEqual(webapp.acquire_active("replay"), "backtest")
        self.assertEqual(webapp.acquire_active("live"), "backtest")
        # 最后一个策略退出才释放
        webapp.release_bt("chan_v1")
        self.assertEqual(webapp._active_mode, "backtest")
        self.assertTrue(webapp._active_spares_chart)
        webapp.release_bt("fxma_v1")
        self.assertIsNone(webapp._active_mode)
        self.assertFalse(webapp._active_spares_chart)
        # 释放后 replay 可占
        self.assertIs(webapp.acquire_active("replay"), True)
        # 回测此时被 replay 挡住
        self.assertEqual(webapp.acquire_bt("chan_v1"), "replay")
        webapp.release_active("replay")

    def test_spares_chart_is_intersection(self):
        self.assertIs(webapp.acquire_bt("chan_v1", spares_chart=True), True)
        self.assertIs(webapp.acquire_bt("fxma_v1", spares_chart=False), True)  # CDP取数
        self.assertFalse(webapp._active_spares_chart)   # 任一策略占图表 → 整体视为占用
        webapp.release_bt("fxma_v1")
        self.assertTrue(webapp._active_spares_chart)    # 占用者退出后恢复
        webapp.release_bt("chan_v1")


class _FakeApp:
    """ControlApp 的可测替身：真实 bt_workers 工厂 + Mock start/pause/stop。"""

    def __init__(self):
        self.signals = webapp.SignalLog()
        self.broadcaster = SimpleNamespace(emit=Mock())
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
