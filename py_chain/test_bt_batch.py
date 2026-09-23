# -*- coding: utf-8 -*-
"""多品种批量并行回测（bt_batch / webapp._run_batch 相关）纯逻辑单测。

不连 TradingView、不跑引擎、不依赖大数据——只测：
1. SignalLog 匹配键含 symbol：跨品种同时刻同策略行互不串写（批量并行正确性前提）；
2. ControlApp.normalize_cfg 的 symbols 解析：单选折叠 / 多选去重保序 / 源限制 / 空；
3. bt_runs._save_current 的按品种过滤与 cfg 注入（mock app/worker，不落库）。

端到端（并行结果 = 串行结果、SSE bt_symbol、子进程崩溃兜底）由 webapp 实跑验证。
"""
import types
import unittest

from py_chain.webapp import ControlApp, SignalLog
from py_chain import bt_runs


class SignalLogSymbolKeyTest(unittest.TestCase):
    def test_key_isolated_across_symbols(self):
        """不同品种同时刻同方向同策略：成交/出场只回填本品种行。"""
        log = SignalLog()
        for sym in ("OANDA:XAUUSD", "FX:NAS100"):
            log.append_signal("backtest",
                              {"time": 1000, "periodX": "3", "direction": "long",
                               "strategyKey": "s1"}, symbol=sym)
        log.fill_trade("backtest",
                       {"signalTime": 1000, "periodX": "3", "direction": "long",
                        "strategyKey": "s1", "entryTime": 1200, "entryPrice": 1.0},
                       symbol="FX:NAS100")
        log.fill_exit("backtest",
                      {"signalTime": 1000, "periodX": "3", "direction": "long",
                       "strategyKey": "s1", "exitTime": 1300, "exitPrice": 2.0,
                       "exitType": "close", "pnl": 5.0},
                      symbol="FX:NAS100")
        rows = {r["symbol"]: r for r in log.list(mode="backtest")}
        self.assertEqual(set(rows), {"OANDA:XAUUSD", "FX:NAS100"})
        self.assertEqual(rows["OANDA:XAUUSD"]["status"], "信号")   # 未被 NAS100 串写
        self.assertEqual(rows["FX:NAS100"]["status"], "已平仓")
        self.assertEqual(rows["FX:NAS100"]["pnl"], 5.0)

    def test_same_symbol_still_matches(self):
        """单品种（行内 symbol / 透传 symbol）回填匹配不受键扩展影响。"""
        log = SignalLog()
        log.append_signal("backtest",
                          {"symbol": "OANDA:XAUUSD", "time": 1000, "periodX": "3",
                           "direction": "long", "strategyKey": "s1"})
        row = log.fill_trade("backtest",
                             {"symbol": "OANDA:XAUUSD", "signalTime": 1000,
                              "periodX": "3", "direction": "long",
                              "strategyKey": "s1", "entryTime": 1200})
        self.assertEqual(row["status"], "持仓中")          # 回填到已有行，不是新行
        self.assertEqual(len(log.list(mode="backtest")), 1)


class NormalizeCfgSymbolsTest(unittest.TestCase):
    def test_single_folds_to_symbol(self):
        out = ControlApp.normalize_cfg(
            {"symbols": "OANDA:XAUUSD", "data_source": "store"}, "backtest")
        self.assertEqual(out["symbol"], "OANDA:XAUUSD")
        self.assertNotIn("symbols", out)

    def test_multi_dedup_keeps_order(self):
        out = ControlApp.normalize_cfg(
            {"symbols": "FX:NAS100,OANDA:XAUUSD,FX:NAS100",
             "data_source": "store"}, "backtest")
        self.assertEqual(out["symbols"], ["FX:NAS100", "OANDA:XAUUSD"])

    def test_multi_requires_store(self):
        for src in ("live", "cache"):
            with self.assertRaises(ValueError):
                ControlApp.normalize_cfg(
                    {"symbols": "A,B", "data_source": src}, "backtest")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            ControlApp.normalize_cfg({"symbols": ",", "data_source": "store"}, "backtest")

    def test_without_symbols_untouched(self):
        out = ControlApp.normalize_cfg({"symbol": "OANDA:XAUUSD"}, "replay")
        self.assertNotIn("symbols", out)
        self.assertEqual(out["symbol"], "OANDA:XAUUSD")


class _FakeWorker:
    """_save_current / _save_all 依赖的最小 worker 形状。"""

    def __init__(self, cfg, batch=None):
        self.cfg = cfg
        self.state = "done"
        self._row_base = 0
        self.duration_sec = 123.4
        self.batch = batch


def _fake_app(worker, snapshot_rows):
    signals = types.SimpleNamespace(
        snapshot=lambda mode, min_id=0: [dict(r) for r in snapshot_rows])
    return types.SimpleNamespace(
        workers={"backtest": worker}, signals=signals,
        broadcaster=types.SimpleNamespace(emit=lambda *a, **k: None),
        bt_runs=types.SimpleNamespace(save=lambda *a, **k: {"id": "x"}))


class SaveCurrentSymbolTest(unittest.TestCase):
    ROWS = [{"id": 1, "mode": "backtest", "symbol": "OANDA:XAUUSD"},
            {"id": 2, "mode": "backtest", "symbol": "FX:NAS100"}]

    def _capture_save(self, app):
        saved = {}

        def _save(name, cfg, rows, worker_state=None, duration_sec=None):
            saved.update(name=name, cfg=cfg, rows=rows, duration=duration_sec)
            return {"id": "run1"}

        app.bt_runs.save = _save
        return saved

    def test_batch_filters_rows_and_injects_symbol(self):
        worker = _FakeWorker({"symbols": ["OANDA:XAUUSD", "FX:NAS100"],
                              "from": "2026-07-02"},
                             batch={"FX:NAS100": {"duration": 66.6}})
        app = _fake_app(worker, self.ROWS)
        saved = self._capture_save(app)
        bt_runs._save_current(app, {"name": "n", "symbol": "FX:NAS100"})
        self.assertEqual([r["symbol"] for r in saved["rows"]], ["FX:NAS100"])
        self.assertEqual(saved["cfg"]["symbol"], "FX:NAS100")       # cfg 注入该品种
        self.assertEqual(saved["duration"], 66.6)                   # 用该品种自身耗时

    def test_batch_without_symbol_rejected(self):
        worker = _FakeWorker({"symbols": ["A", "B"]}, batch={"A": {}, "B": {}})
        with self.assertRaises(bt_runs._HttpError):
            bt_runs._save_current(_fake_app(worker, []), {"name": "n"})

    def test_batch_unknown_symbol_rejected(self):
        worker = _FakeWorker({"symbols": ["A", "B"]}, batch={"A": {}, "B": {}})
        with self.assertRaises(bt_runs._HttpError):
            bt_runs._save_current(_fake_app(worker, self.ROWS),
                                  {"name": "n", "symbol": "C"})

    def test_single_symbol_passthrough(self):
        rows = [{"id": 1, "mode": "backtest", "symbol": "OANDA:XAUUSD"}]
        worker = _FakeWorker({"symbol": "OANDA:XAUUSD"}, batch=None)
        app = _fake_app(worker, rows)
        saved = self._capture_save(app)
        bt_runs._save_current(app, {"name": "n"})     # 不传 symbol：旧行为
        self.assertEqual(saved["rows"], rows)
        self.assertEqual(saved["duration"], 123.4)


class SaveAllTest(unittest.TestCase):
    ROWS = [{"id": 1, "mode": "backtest", "symbol": "OANDA:XAUUSD"},
            {"id": 2, "mode": "backtest", "symbol": "FX:NAS100"},
            {"id": 3, "mode": "backtest", "symbol": "TVC:USOIL"}]

    def _batch(self):
        return {"OANDA:XAUUSD": {"state": "done", "duration": 11.0},
                "FX:NAS100": {"state": "done", "duration": 22.0},
                "TVC:USOIL": {"state": "skipped", "error": "无数据"},
                "OANDA:XAGUSD": {"state": "done", "duration": 33.0}}

    def _capture(self, app):
        calls = []

        def _save(name, cfg, rows, worker_state=None, duration_sec=None):
            calls.append({"name": name, "cfg": cfg, "rows": rows,
                          "duration": duration_sec})
            return {"id": name}

        app.bt_runs.save = _save
        return calls

    def test_save_all_per_symbol(self):
        worker = _FakeWorker({"symbols": ["OANDA:XAUUSD", "FX:NAS100",
                                          "TVC:USOIL", "OANDA:XAGUSD"]},
                             batch=self._batch())
        app = _fake_app(worker, self.ROWS)
        calls = self._capture(app)
        out = bt_runs._save_all(app, {})
        # 完成（done）且有行的品种各存一条：XAUUSD、NAS100
        self.assertEqual([c["cfg"]["symbol"] for c in calls],
                         ["OANDA:XAUUSD", "FX:NAS100"])
        self.assertEqual([len(c["rows"]) for c in calls], [1, 1])
        self.assertEqual([c["duration"] for c in calls], [11.0, 22.0])
        self.assertTrue(all(c["name"].startswith(c["cfg"]["symbol"]) for c in calls))
        self.assertEqual([s["symbol"] for s in out["saved"]],
                         ["OANDA:XAUUSD", "FX:NAS100"])
        # skipped 品种：无数据跳过；无行品种：跳过并记原因
        self.assertEqual({s["symbol"] for s in out["skipped"]},
                         {"TVC:USOIL", "OANDA:XAGUSD"})

    def test_save_all_duplicate_name_fails_symbol_not_others(self):
        worker = _FakeWorker({"symbols": ["OANDA:XAUUSD", "FX:NAS100"]},
                             batch={"OANDA:XAUUSD": {"state": "done", "duration": 1.0},
                                    "FX:NAS100": {"state": "done", "duration": 2.0}})
        app = _fake_app(worker, self.ROWS[:2])
        real_save = app.bt_runs.save

        def _save(name, cfg, rows, worker_state=None, duration_sec=None):
            if cfg["symbol"] == "OANDA:XAUUSD":
                raise bt_runs.sqlite3.IntegrityError()
            return real_save(name, cfg, rows, worker_state, duration_sec)

        app.bt_runs.save = _save
        out = bt_runs._save_all(app, {})
        self.assertEqual([f["symbol"] for f in out["failed"]], ["OANDA:XAUUSD"])
        self.assertEqual([s["symbol"] for s in out["saved"]], ["FX:NAS100"])

    def test_save_all_requires_batch(self):
        worker = _FakeWorker({"symbol": "OANDA:XAUUSD"}, batch=None)
        with self.assertRaises(bt_runs._HttpError):
            bt_runs._save_all(_fake_app(worker, self.ROWS), {})

    def test_save_all_rejects_while_running(self):
        worker = _FakeWorker({"symbols": ["A", "B"]}, batch={})
        worker.state = "running"
        with self.assertRaises(bt_runs._HttpError):
            bt_runs._save_all(_fake_app(worker, []), {})


if __name__ == "__main__":
    unittest.main()
