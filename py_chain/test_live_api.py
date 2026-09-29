# -*- coding: utf-8 -*-
"""live_api 测试：多策略 overview 聚合 + POST /api/live/trading 开关写 kv 与审计。

运行：py -3.12 -m unittest py_chain.test_live_api（无需 bars.db / MT5 终端）。
"""

import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import live_api, live_store, module_registry


class LiveApiBase(unittest.TestCase):
    """每用例独立临时 live 库。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="live_api_test_")
        os.environ["LIVE_DB_PATH"] = os.path.join(self.tmp, "live.db")
        live_store.ensure_tables()

    def tearDown(self):
        os.environ.pop("LIVE_DB_PATH", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _key(base, sid="chan_v1"):
        return live_store.state_key(base, sid)


class TestOverview(LiveApiBase):

    def _snapshot(self):
        d = live_api.overview()
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["strategies"]), len(module_registry.strategy_ids()))
        return next(s for s in d["strategies"] if s["strategy"] == "chan_v1")

    def test_lists_all_strategies_with_session(self):
        live_store.save_state(self._key("session"), {
            "id": "S1", "strategy": "chan_v1", "symbol": "XAUUSD", "lots": 2,
            "shadow": False})
        live_store.save_state(self._key("heartbeat"),
                              {"ts": int(time.time()), "fine_last": 100})
        snap = self._snapshot()
        for k in ("strategy", "strategy_title", "online", "session", "heartbeat",
                  "engine_cursor", "trading_enabled", "open_trades", "closed_trades",
                  "orders", "events", "pnl_summary"):
            self.assertIn(k, snap)
        self.assertTrue(snap["online"])
        self.assertEqual(snap["session"]["id"], "S1")
        self.assertEqual(snap["strategy_title"],
                         module_registry.STRATEGIES["chan_v1"]["title"])
        self.assertTrue(snap["trading_enabled"])

    def test_no_session_strategy_placeholder(self):
        """无会话也占位（offline 空表），保证 tab 列表稳定。"""
        snap = self._snapshot()
        self.assertEqual(snap["session"], {})
        self.assertFalse(snap["online"])
        self.assertEqual(snap["open_trades"] + snap["closed_trades"]
                         + snap["orders"] + snap["events"], [])
        self.assertIsNone(snap["pnl_summary"])
        self.assertTrue(snap["trading_enabled"])

    def test_trading_flag_from_kv(self):
        live_store.save_state(self._key(live_api.TRADING_KEY), {"enabled": False})
        self.assertFalse(self._snapshot()["trading_enabled"])
        live_store.save_state(self._key(live_api.TRADING_KEY), {"enabled": True})
        self.assertTrue(self._snapshot()["trading_enabled"])

    def test_session_data_populated(self):
        live_store.save_state(self._key("session"), {"id": "S1"})
        live_store.log_order("S1", "entry", direction="long", volume=0.02, ok=0,
                             retcode=0, retcomment="gate:test", shadow=1)
        live_store.log_event("S1", "gate_blocked", {"reason": "test"})
        snap = self._snapshot()
        self.assertEqual(len(snap["orders"]), 1)
        self.assertEqual(snap["orders"][0]["retcomment"], "gate:test")
        self.assertEqual(len(snap["events"]), 1)
        self.assertEqual(snap["open_trades"], [])


class TestTradingPost(LiveApiBase):

    def _post(self, body, path="/api/live/trading"):
        reply = []
        h = SimpleNamespace(path=path, _read_body=lambda: body,
                            _send_json=lambda obj, code=200: reply.append((obj, code)))
        return live_api.handle(h, None, "POST"), reply

    def test_disable_writes_kv_and_audits(self):
        live_store.save_state(self._key("session"), {"id": "S1"})
        handled, reply = self._post({"strategy": "chan_v1", "enabled": False})
        self.assertTrue(handled)
        obj, code = reply[0]
        self.assertEqual(code, 200)
        self.assertTrue(obj["ok"])
        self.assertIs(obj["enabled"], False)
        self.assertEqual(live_store.load_state(self._key(live_api.TRADING_KEY)),
                         {"enabled": False})
        self.assertEqual(live_store.recent_events("S1", n=10)[-1]["kind"],
                         "trading_disabled")

    def test_enable_back_without_session_web_fallback(self):
        live_store.save_state(self._key(live_api.TRADING_KEY), {"enabled": False})
        handled, reply = self._post({"enabled": True})   # strategy 缺省 → 默认
        obj, code = reply[0]
        self.assertEqual(code, 200)
        self.assertTrue(obj["enabled"])
        self.assertEqual(live_store.load_state(self._key(live_api.TRADING_KEY)),
                         {"enabled": True})
        self.assertEqual(live_store.recent_events("web:chan_v1", n=10)[-1]["kind"],
                         "trading_enabled")

    def test_unknown_strategy_400(self):
        handled, reply = self._post({"strategy": "ema_v9", "enabled": True})
        self.assertTrue(handled)
        obj, code = reply[0]
        self.assertEqual(code, 400)
        self.assertFalse(obj["ok"])
        self.assertIsNone(live_store.load_state(
            live_store.state_key(live_api.TRADING_KEY, "ema_v9")))

    def test_invalid_body_400(self):
        for body in ({"enabled": "yes"}, {"strategy": "chan_v1"}, "not-a-dict"):
            handled, reply = self._post(body)
            self.assertTrue(handled)
            self.assertEqual(reply[0][1], 400)

    def test_unknown_post_path_not_handled(self):
        handled, reply = self._post({}, path="/api/live/nope")
        self.assertFalse(handled)
        self.assertEqual(reply, [])

    def test_get_routes_unchanged(self):
        reply = []
        h = SimpleNamespace(path="/api/live/overview",
                            _send_json=lambda obj, code=200: reply.append((obj, code)),
                            _serve_file=lambda *a, **k: None)
        self.assertTrue(live_api.handle(h, None, "GET"))
        self.assertIn("strategies", reply[0][0])
        h2 = SimpleNamespace(path="/api/live/nope",
                             _send_json=lambda obj, code=200: None,
                             _serve_file=lambda *a, **k: None)
        self.assertFalse(live_api.handle(h2, None, "GET"))


class TestWebappRouting(LiveApiBase):

    def test_do_post_routes_live_trading(self):
        from . import webapp
        app = SimpleNamespace(signals=webapp.SignalLog(),
                              broadcaster=SimpleNamespace(emit=lambda *a, **k: None))
        reply = []
        handler = SimpleNamespace(
            path="/api/live/trading",
            _read_body=lambda: {"strategy": "chan_v1", "enabled": False},
            _tune=lambda _: False,
            _send_json=lambda obj, code=200: reply.append((obj, code)))
        with patch.object(webapp.analysis_api, "handle", return_value=False):
            webapp.make_handler(app).do_POST(handler)
        obj, code = reply[0]
        self.assertEqual(code, 200)
        self.assertTrue(obj["ok"])
        self.assertEqual(live_store.load_state(self._key(live_api.TRADING_KEY)),
                         {"enabled": False})


if __name__ == "__main__":
    unittest.main()
