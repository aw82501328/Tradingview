# -*- coding: utf-8 -*-
"""分段并行（2026-10-28·独立段口径快跑，用户拍板）单测：四块——

1. 纯函数：normalize_cfg 的 seg_parallel 布尔化（缺省不注入=旧客户端忠实）；
   _split_segments 的 split_seg_days 段长覆盖 + seg_bounds 落 cfg；
2. bt_journal.register 补注册（分段并行防 latest.json/清理多进程竞态的收敛点）；
3. 独立段口径一致性（真实 bars.db 有 OANDA:XAUUSD 才跑）：run_symbol_parallel
   （真实 spawn 孙进程并行）的 done stats/open == 同段界顺序独立段跑（run_chain
   chained=False 固定任务序合并）——独立段结果与完成顺序无关=可复现；
4. run_symbol_parallel 段数<2 回落 run_symbol（旧口径不变）+ webapp 端到端
   （BacktestWorker._run_single_subproc seg_parallel=True → spawn 聚合入口 → done）。

口径说明（与链式顺序非逐位一致，快跑用；定稿请用链式口径复跑）见 bt_batch
模块 docstring 与 webapp._split_segments。引擎级 carry 语义（链式）由
test_auto_split.CarryContinuityTests 锁定，此处不重复。
"""
import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志（journal 例内单独开启）

import datetime
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from py_chain import bt_batch, bt_journal
from py_chain.backtest import merge_run_stats
from py_chain.webapp import BacktestWorker

SYM = "OANDA:XAUUSD"
TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def _have_data():
    from py_chain import data_store
    try:
        return SYM in {s.get("symbol") for s in data_store.list_stores()}
    except Exception:
        return False


def _bt_cfg(from_ts, to_ts, seg_days=4, min_days=2, lead_days=2):
    """小窗口快测 cfg：段长 4 天/阈值 2 天保证拆出多段，预热 2 天压耗时。"""
    return {"symbol": SYM, "strategy": "chan_v1",
            "from_ts": from_ts, "to_ts": to_ts, "lead_days": lead_days,
            "warmup": 60, "auto_split": True,
            "split_seg_days": seg_days, "split_min_days": min_days}


class _FakeQ:
    def __init__(self):
        self.msgs = []

    def put(self, m):
        self.msgs.append(m)


class NormalizeTests(unittest.TestCase):
    def test_seg_parallel_bool(self):
        from py_chain import webapp
        out = webapp.ControlApp.normalize_cfg({"seg_parallel": "true"}, "backtest")
        self.assertIs(out["seg_parallel"], True)
        out = webapp.ControlApp.normalize_cfg({"seg_parallel": "false"}, "backtest")
        self.assertIs(out["seg_parallel"], False)
        # 缺省不注入（旧客户端/旧方案重放忠实：cfg.get → None → 不并行）
        out = webapp.ControlApp.normalize_cfg({}, "backtest")
        self.assertNotIn("seg_parallel", out)

    def test_split_seg_days_passthrough(self):
        from py_chain import webapp
        out = webapp.ControlApp.normalize_cfg({"split_seg_days": "30"}, "backtest")
        self.assertEqual(out["split_seg_days"], 30)


class SplitSegDaysTests(unittest.TestCase):
    FROM = int(datetime.datetime(2026, 6, 1, tzinfo=TZ8).timestamp())

    def segs(self, cfg):
        with mock.patch("py_chain.data_store.list_stores", return_value=[
                {"symbol": SYM, "periods": {
                    r: {"first": self.FROM, "last": self.FROM + 300 * 86400}
                    for r in ("D", "240", "60", "15", "3")}}]):
            return BacktestWorker._split_segments(
                cfg, SYM, cfg.get("from_ts", self.FROM),
                cfg.get("to_ts"), cfg.get("lead_days", 60))

    def test_seg_days_override_controls_bounds(self):
        # 120 天窗：默认 60 → 2 段；段长 30 → 4 段；seg_bounds 落 cfg 供重放
        cfg = {"auto_split": True, "from_ts": self.FROM,
               "to_ts": self.FROM + 120 * 86400, "lead_days": 60}
        segs = self.segs(cfg)
        self.assertEqual(len(segs), 2)
        self.assertEqual(len(cfg["seg_bounds"]), 2)
        cfg2 = {k: v for k, v in cfg.items() if k != "seg_bounds"}
        cfg2["split_seg_days"] = 30
        segs2 = self.segs(cfg2)
        self.assertEqual(len(segs2), 4)
        # 段长覆盖改变段界 → 结果与默认段长跑不可比（口径注记）
        self.assertNotEqual([s["start_ts"] for s in segs],
                            [s["start_ts"] for s in segs2])


class JournalRegisterTests(unittest.TestCase):
    def test_register_updates_latest_and_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for i in range(3):
                p = os.path.join(td, f"bt_chanA_s{i + 1}of3_{SYM.replace(':', '-')}"
                                     f"_20261028_00000{i}00.ndjson")
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write('{"ev": "header"}\n')
                paths.append(p)
            with mock.patch.object(bt_journal, "JOURNAL_DIR", td), \
                    mock.patch.object(bt_journal, "LATEST_JSON",
                                      os.path.join(td, "latest.json")):
                for i, p in enumerate(paths):
                    bt_journal.register(p, f"chanA_s{i + 1}of3", SYM)
                state = json.load(open(os.path.join(td, "latest.json"),
                                       encoding="utf-8"))
            self.assertEqual(len(state), 3)
            for i in range(3):
                hit = state[f"chanA_s{i + 1}of3|{bt_journal.sanitize_symbol(SYM)}"]
                self.assertEqual(hit["path"], paths[i])
                self.assertTrue(os.path.exists(hit["path"]))


class ParallelEquivalenceTests(unittest.TestCase):
    """独立段口径一致性：并行（真实 spawn 孙进程）== 顺序独立段（固定任务序合并）。
    journal 关闭跑（引擎测试口径）；真实 bars.db 有 XAUUSD 才跑。"""

    FROM = int(datetime.datetime(2026, 9, 1, tzinfo=TZ8).timestamp())

    def _build(self):
        cfg = _bt_cfg(self.FROM, self.FROM + 10 * 86400)
        tasks = BacktestWorker._chain_tasks(BacktestWorker, cfg, SYM)
        segs = BacktestWorker._split_segments(
            cfg, SYM, cfg["from_ts"], cfg["to_ts"], cfg["lead_days"],
            periods_needed=sorted({p for t in tasks for p in t["periods"]}))
        self.assertIsNotNone(segs)
        self.assertGreaterEqual(len(segs), 2)
        from py_chain import param_center
        child_cfg = {"strategy": "chan_v1",
                     "chan_cfg": param_center.chan_cfg_effective(SYM),
                     "chains": [{"id": t["id"], "periods": t["periods"],
                                 "engine_kwargs": t["engine_kwargs"]}
                                for t in tasks],
                     "data_from_ts": max(0, cfg["from_ts"] - cfg["lead_days"] * 86400),
                     "to_ts": cfg["to_ts"],
                     "start_ts": cfg["from_ts"],
                     "segments": segs}
        return child_cfg, tasks, segs

    def test_parallel_equals_sequential_independent(self):
        if not _have_data():
            self.skipTest("本地无 OANDA:XAUUSD 数据")
        child_cfg, tasks, segs = self._build()
        # 期望值：顺序独立段（run_chain chained=False，固定任务序合并）
        merged = None
        opens = []
        logs = []

        def _log(m):
            logs.append(str(m))

        for task in tasks:
            r = bt_batch.run_chain(child_cfg, SYM, task, segs,
                                   lambda **m: None, _log,
                                   threading.Event(), threading.Event(),
                                   chained=False, journal_register=False)
            merged = merge_run_stats(merged, r["stats"])
            opens.extend(r["open"])
        # 实际：并行入口（spawn 孙进程；BT_SEG_PARALLEL=2 保证并发）
        q = _FakeQ()
        with mock.patch.dict(os.environ, {"BT_SEG_PARALLEL": "2"}):
            bt_batch.run_symbol_parallel(child_cfg, SYM, q,
                                         threading.Event(), threading.Event())
        done = [m for m in q.msgs if m.get("kind") == "done"]
        errs = [m for m in q.msgs if m.get("kind") == "error"]
        self.assertFalse(errs, errs)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["stats"], merged)
        self.assertEqual(sorted(map(json.dumps, done[0]["open"])),
                         sorted(map(json.dumps, opens)))
        self.assertIsNone(done[0]["journal"])   # journal 关闭口径：无日志路径


class ParallelFallbackTests(unittest.TestCase):
    FROM = int(datetime.datetime(2026, 9, 1, tzinfo=TZ8).timestamp())

    def test_single_segment_falls_back_to_run_symbol(self):
        if not _have_data():
            self.skipTest("本地无 OANDA:XAUUSD 数据")
        # 无 segments 键（<2 任务：1 链×1 段）→ 直接 run_symbol 旧口径
        from py_chain import param_center
        cfg = _bt_cfg(self.FROM, self.FROM + 3 * 86400, seg_days=30, min_days=90)
        tasks = BacktestWorker._chain_tasks(BacktestWorker, cfg, SYM)
        child_cfg = {"strategy": "chan_v1",
                     "chan_cfg": param_center.chan_cfg_effective(SYM),
                     "chains": [{"id": t["id"], "periods": t["periods"],
                                 "engine_kwargs": t["engine_kwargs"]}
                                for t in tasks],
                     "data_from_ts": max(0, cfg["from_ts"] - cfg["lead_days"] * 86400),
                     "to_ts": cfg["to_ts"], "start_ts": cfg["from_ts"]}
        q = _FakeQ()
        with mock.patch.object(bt_batch, "run_symbol",
                               side_effect=lambda *a: q.msgs.append("called")) as m:
            bt_batch.run_symbol_parallel(child_cfg, SYM, q,
                                         threading.Event(), threading.Event())
        m.assert_called_once()
        self.assertIn("called", q.msgs)


class WorkerEndToEndTests(unittest.TestCase):
    """webapp 端到端（真实子进程树）：seg_parallel=True → spawn run_symbol_parallel
    → done（中继/聚合/孙进程全链路）。"""

    def test_worker_seg_parallel_run(self):
        if not _have_data():
            self.skipTest("本地无 OANDA:XAUUSD 数据")
        from py_chain import webapp
        from unittest.mock import Mock
        w = webapp.BacktestWorker(Mock(), Mock(), strategy="chan_v1")
        w.cfg = _bt_cfg(int(datetime.datetime(2026, 9, 1, tzinfo=TZ8).timestamp()),
                        int(datetime.datetime(2026, 9, 11, tzinfo=TZ8).timestamp()))
        w.cfg["seg_parallel"] = True
        w._pause_evt = threading.Event()
        w._stop_evt = threading.Event()
        states = []
        w.set_state = lambda s: states.append(s)
        with mock.patch.dict(os.environ, {"BT_SEG_PARALLEL": "3"}):
            w._run_single_subproc()
        self.assertIn("done", states)
        self.assertNotIn("error", states)
        self.assertIsNone(w.error)

    def test_worker_seg_parallel_off_still_runs(self):
        if not _have_data():
            self.skipTest("本地无 OANDA:XAUUSD 数据")
        from py_chain import webapp
        from unittest.mock import Mock
        w = webapp.BacktestWorker(Mock(), Mock(), strategy="chan_v1")
        w.cfg = _bt_cfg(int(datetime.datetime(2026, 9, 1, tzinfo=TZ8).timestamp()),
                        int(datetime.datetime(2026, 9, 11, tzinfo=TZ8).timestamp()))
        w._pause_evt = threading.Event()
        w._stop_evt = threading.Event()
        states = []
        w.set_state = lambda s: states.append(s)
        w._run_single_subproc()
        self.assertIn("done", states)
        self.assertNotIn("error", states)


if __name__ == "__main__":
    unittest.main()
