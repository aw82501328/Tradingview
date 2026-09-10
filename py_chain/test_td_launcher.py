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

if __name__ == "__main__": unittest.main()
