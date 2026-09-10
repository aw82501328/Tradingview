"""Task orchestration tests: no real chart mutation or shared cache publication."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from .analysis_service import AnalysisManager, dependency_order, ORDER
from . import webapp, sr_service


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.events = []
        self.holder = None
        self.chart = threading.Lock()
        self.release_count = 0
        def acquire(name):
            if self.holder:
                return self.holder
            self.holder = name
            return True
        def release(name):
            self.holder = None
            self.release_count += 1
        self.m = AnalysisManager(lambda *x: self.events.append(x), acquire, release, self.chart,
                                 webapp.ControlApp.normalize_sr_cfg, lambda *x: None,
                                 storage=self.tmp.name, stage_runner=self.stage,
                                 target_reader=lambda cfg, restore=None: "15")
        self.m.configure({"targetId": "test", "symbol": "OANDA:XAUUSD", "from": "2026-06-30",
                          "sr": {"srTypes": ["cluster", "boll"]}})
        self.m._promote = lambda *args: None

    def stage(self, key, cfg, folder, log):
        self.calls.append((key, copy.deepcopy(cfg)))
        return {"count": 0}

    def finish(self):
        self.m.thread.join(3)
        self.assertFalse(self.m.thread.is_alive())

    def tearDown(self):
        self.m.close()
        if self.m.thread:
            self.m.thread.join(3)
        self.tmp.cleanup()

    def test_full_order_and_release(self):
        self.assertTrue(self.m.start()["ok"])
        self.finish()
        self.assertEqual([x[0] for x in self.calls], ORDER)
        self.assertEqual(self.m.job["state"], "success")
        self.assertIsNotNone(self.m.last_success)
        self.assertEqual(self.release_count, 1)
        self.assertFalse(self.chart.locked())

    def test_entry_dependencies_not_visual_order(self):
        self.assertEqual(dependency_order("entry"), ["bi", "sr", "plan", "entry"])
        self.m.start("entry")
        self.finish()
        self.assertEqual(self.m.job["stages"]["points"]["state"], "stale")
        self.assertIsNone(self.m.last_success)

    def test_failure_blocks_downstream_and_disables_auto(self):
        def fail(key, *args):
            self.calls.append(key)
            if key == "points":
                raise RuntimeError("drawing failed")
            return {}
        self.m.stage_runner = fail
        self.m.auto = True
        self.m.start()
        self.finish()
        self.assertEqual(self.calls, ["bi", "points"])
        self.assertEqual(self.m.job["state"], "error")
        self.assertFalse(self.m.auto)
        self.assertFalse(self.chart.locked())

    def test_stop_and_configuration_snapshot(self):
        begun, unblock = threading.Event(), threading.Event()
        def stage(key, cfg, *args):
            self.calls.append((key, cfg))
            begun.set()
            unblock.wait(2)
            return {}
        self.m.stage_runner = stage
        self.m.start()
        self.assertTrue(begun.wait(1))
        self.m.configure({"near": 2})
        self.assertFalse(self.m.start()["ok"])
        self.m.stop()
        unblock.set()
        self.finish()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["near"], 1)
        self.assertEqual(self.m.cfg["near"], 2)
        self.assertEqual(self.m.job["state"], "stopped")

    def test_auto_waits_for_other_tasks(self):
        self.holder = "live"
        self.m.auto = True
        self.m.next_at = time.time() - 100
        self.m.poll()
        self.assertIn("live", self.m.waiting)
        self.assertEqual(self.calls, [])
        self.assertGreater(self.m.next_at, time.time())

    def test_restart_never_resumes_auto(self):
        self.m.auto = True
        self.m._save()
        other = AnalysisManager(lambda *x: None, lambda _: True, lambda _: None, threading.Lock(),
                                webapp.ControlApp.normalize_sr_cfg, lambda *x: None, storage=self.tmp.name)
        self.assertFalse(other.auto)
        self.assertEqual(other.cfg["symbol"], "OANDA:XAUUSD")
        self.assertIsNone(other.scheduler)
        other.close()

    def test_target_change_before_start_no_mutation(self):
        self.m.target_reader = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ANALYSIS_TARGET_CHANGED"))
        self.m.start()
        self.finish()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.m.job["state"], "error")

    def test_invalid_config(self):
        for patch_cfg in ({"intervalMinutes": 0}, {"intervalMinutes": float("nan")},
                          {"from": "nonsense"}, {"keep": 0}, {"near": -1},
                          {"sr": {"periods": ["W"], "srTypes": ["boll"]}}):
            with self.assertRaises(ValueError):
                self.m.configure(patch_cfg)

    def test_publication_failure_restores_old_files(self):
        root = Path(self.tmp.name) / "repo"
        cache = root / ".cursor" / "cache"
        cache.mkdir(parents=True)
        old = cache / "bis_OANDA_XAUUSD.json"
        old.write_text('{"old":true}', encoding="utf-8")
        folder = Path(self.tmp.name) / "run"
        folder.mkdir()
        for prefix in ("bis", "plan"):
            (folder / f"{prefix}_OANDA_XAUUSD.json").write_text(
                '{"symbol":"OANDA:XAUUSD","periods":{}}', encoding="utf-8")
        from . import analysis_service
        write = analysis_service.atomic_json
        def fail_second(path, data):
            if Path(path).name.startswith("plan_"):
                raise OSError("disk full")
            write(path, data)
        with patch.object(analysis_service, "ROOT", root), patch.object(analysis_service, "atomic_json", side_effect=fail_second):
            with self.assertRaises(OSError):
                AnalysisManager._promote(self.m, folder, {"id": "test", "cfg": self.m.cfg}, ["bi", "plan"])
        self.assertEqual(json.loads(old.read_text()), {"old": True})
        self.assertFalse((cache / "plan_OANDA_XAUUSD.json").exists())

    def test_replay_is_rejected_before_any_mutation(self):
        from . import analysis_service
        with patch.object(analysis_service, "CDPClient") as client, \
             patch.object(analysis_service, "replay_started", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "历史回放"):
                self.m._target(self.m.cfg)
            client.return_value.__enter__.return_value.evaluate.assert_not_called()

    def test_supplied_bis_not_rebuilt(self):
        bars = {"15": [{"time": i * 900, "open": 100, "close": 101, "high": 102, "low": 99} for i in range(40)]}
        result = {"currentPrice": 101, "periodAtrs": {"15": 3}, "merged": [], "drawnByPeriod": {"15": []}, "periods": {"15": []}}
        cfg = self.m.normalize_sr({"periods": ["15"], "srTypes": ["boll"]})
        bis = {"15": []}
        with patch.object(sr_service, "build_bis", side_effect=AssertionError("must not rebuild")), \
             patch.object(sr_service, "compute_srflip", return_value=result) as compute:
            sr_service.build_chain_result(bars, cfg, bis_by_period=bis)
            self.assertIs(compute.call_args.args[0], bis)


class GateTests(unittest.TestCase):
    def tearDown(self):
        if webapp._marks_lock.locked():
            webapp._marks_lock.release()
        if webapp.active_mode():
            webapp.release_active(webapp.active_mode())

    def test_chart_and_mode_are_atomic(self):
        self.assertTrue(webapp._marks_lock.acquire(False))
        self.assertEqual(webapp.acquire_active("analysis"), "图表操作")
        webapp._marks_lock.release()
        self.assertIs(webapp.acquire_active("analysis"), True)
        # The request owner may take the second gate; other request threads cannot.
        result = []
        t = threading.Thread(target=lambda: result.append(webapp._marks_lock.acquire(False)))
        t.start(); t.join()
        self.assertEqual(result, [False])
        self.assertTrue(webapp._marks_lock.acquire(False))


if __name__ == "__main__":
    unittest.main()
