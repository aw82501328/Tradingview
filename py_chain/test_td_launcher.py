import threading
import unittest
from unittest.mock import patch
from .td_launcher import TDLauncher
from . import webapp

class LaunchTests(unittest.TestCase):
    def test_shared_task_and_exclusion(self):
        gate = threading.Event()
        calls = []
        def run(allowed):
            calls.append(allowed)
            gate.wait(3)
            return {"state": "ready"}
        m = TDLauncher(webapp.acquire_active, webapp.release_active, run)
        try:
            first = m.start()
            self.assertEqual(first["id"], m.start(True)["id"])
            self.assertEqual(webapp.acquire_active("analysis"), "td-launch")
            acquired = []
            probe = threading.Thread(target=lambda: acquired.append(webapp._marks_lock.acquire()))
            probe.start(); probe.join(3)
            self.assertEqual(acquired, [False])
        finally:
            gate.set()
            m.thread.join(3)
        self.assertEqual(calls, [False])
        self.assertEqual(m.snapshot()["state"], "ready")
        self.assertIsNone(webapp.active_mode())

    def test_busy(self):
        m = TDLauncher(lambda _: "replay", lambda _: None, lambda _: self.fail())
        self.assertFalse(m.start()["ok"])

    def test_confirmation_and_retry(self):
        calls = []
        def run(allowed):
            calls.append(allowed)
            return {"state": "ready" if allowed else "needs_confirmation"}
        m = TDLauncher(lambda _: True, lambda _: None, run)
        m.start(); m.thread.join(3)
        self.assertEqual(m.snapshot()["state"], "needs_confirmation")
        self.assertEqual(calls, [False])  # cancellation sends no additional request
        m.start(True); m.thread.join(3)
        self.assertEqual(calls, [False, True])
        self.assertEqual(m.snapshot()["state"], "ready")

    def test_failure_releases_lock(self):
        released=[]
        def fail(_): raise RuntimeError("启动失败")
        m=TDLauncher(lambda _:True, released.append, fail)
        m.start(); m.thread.join(3)
        self.assertEqual(m.snapshot()["state"], "error")
        self.assertEqual(released,["td-launch"])
        with self.assertRaises(ValueError):m.start("true")

    def test_shared_with_local_backtest(self):
        """本地数据回测占用互斥时启动TD放行：共享模式不占全局锁，结束时也无释放。"""
        calls, released = [], []
        def run(allowed):
            calls.append(allowed)
            return {"state": "ready"}
        m = TDLauncher(lambda mode: "backtest", lambda mode: released.append(mode), run,
                       compat=lambda owner: True)
        self.assertTrue(m.start()["ok"])
        m.thread.join(3)
        self.assertEqual(calls, [False])
        self.assertEqual(m.snapshot()["state"], "ready")
        self.assertEqual(released, [])

    def test_compat_reject_still_blocks(self):
        m = TDLauncher(lambda mode: "backtest", lambda mode: None,
                       lambda _: self.fail(), compat=lambda owner: False)
        self.assertFalse(m.start()["ok"])

    def test_td_launch_compat_by_source(self):
        """ControlApp 兼容判断：store/cache（含批量）放行，live 缺省与非回测互斥。"""
        import tempfile
        from pathlib import Path
        from . import sr_tune
        with tempfile.TemporaryDirectory() as tmp:
            app = webapp.ControlApp(tune_store=sr_tune.Store(Path(tmp) / 'tune'))
        w = app.workers["backtest"]
        gate = threading.Event()
        w.thread = threading.Thread(target=gate.wait, daemon=True)
        w.thread.start()
        try:
            webapp._active_mode = "backtest"
            for src, expect in (("store", True), ("cache", True), ("live", False)):
                w.cfg = {"data_source": src}
                self.assertIs(app._td_launch_compat("backtest"), expect)
            w.cfg = {"data_source": "store", "symbols": ["A", "B"]}   # 多品种批量=store
            self.assertTrue(app._td_launch_compat("backtest"))
            w.cfg = {"use_cache": True}     # 旧键兜底 → cache
            self.assertTrue(app._td_launch_compat("backtest"))
            w.cfg = {}                      # 缺省 → live
            self.assertFalse(app._td_launch_compat("backtest"))
            webapp._active_mode = "replay"  # 非回测占用者一律互斥
            self.assertFalse(app._td_launch_compat("replay"))
        finally:
            webapp._active_mode = None
            gate.set()
            w.thread.join(3)

if __name__ == "__main__": unittest.main()
