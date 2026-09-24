# -*- coding: utf-8 -*-
"""支阻位参数页多品种（webapp symbols 口径）纯逻辑单测。

不连 TradingView、不取数、不计算引擎——只测：
1. ControlApp.normalize_sr_cfg 的 symbols 解析：去重保序 / 空 / 超上限 /
   symbol=首个 / 无 symbols 键时旧路径不动 / symbols 优先于残留 symbol；
2. run_sr_compute 逐品种循环：单品种失败隔离继续、顶层=第一个成功品种、
   全部失败不动旧结果槽、sr_counts 的 by_symbol 形状（mock ensure_data /
   build_chain_result，fake app 不落库不起线程）。

端到端（多品种取数/SSE 进度/画图切品种）由 webapp 实跑验证。
"""
import types
import unittest
from unittest.mock import patch

from py_chain.webapp import ControlApp, run_sr_compute


def _sr_cfg(**kw):
    """normalize_sr_cfg 可接受的最小合法 cfg（支阻位页 collectCfg 同形）。"""
    base = {"from": "2026-07-01", "periods": ["D", "240", "60", "15", "3"],
            "srTypes": ["cluster", "boll"], "clusterParts": ["flip", "recent"]}
    base.update(kw)
    return base


class NormalizeSrSymbolsTest(unittest.TestCase):
    def test_single_keeps_list_and_symbol(self):
        # 与回测 normalize_cfg「单选折叠删除 symbols」不同：支阻位页需要列表回填
        # 多选面板（勾 1 个=单品种行为），故单元素也保留 symbols，symbol=首个
        out = ControlApp.normalize_sr_cfg(_sr_cfg(symbols="OANDA:XAUUSD"))
        self.assertEqual(out["symbols"], ["OANDA:XAUUSD"])
        self.assertEqual(out["symbol"], "OANDA:XAUUSD")

    def test_multi_dedup_keeps_order(self):
        out = ControlApp.normalize_sr_cfg(
            _sr_cfg(symbols="FX:NAS100,OANDA:XAUUSD,FX:NAS100"))
        self.assertEqual(out["symbols"], ["FX:NAS100", "OANDA:XAUUSD"])
        self.assertEqual(out["symbol"], "FX:NAS100")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            ControlApp.normalize_sr_cfg(_sr_cfg(symbols=","))

    def test_over_ten_rejected(self):
        with self.assertRaises(ValueError):
            ControlApp.normalize_sr_cfg(_sr_cfg(symbols=",".join(f"S{i}" for i in range(11))))

    def test_without_symbols_untouched(self):
        out = ControlApp.normalize_sr_cfg(_sr_cfg(symbol="OANDA:XAGUSD"))
        self.assertNotIn("symbols", out)
        self.assertEqual(out["symbol"], "OANDA:XAGUSD")

    def test_symbols_wins_over_stale_symbol(self):
        # 预设移植：symbols 列表优先，残留的旧 symbol 单值被首个选中品种覆盖
        out = ControlApp.normalize_sr_cfg(
            _sr_cfg(symbol="OANDA:XAUUSD", symbols="TVC:USOIL,BITSTAMP:BTCUSD"))
        self.assertEqual(out["symbol"], "TVC:USOIL")
        self.assertEqual(out["symbols"], ["TVC:USOIL", "BITSTAMP:BTCUSD"])


def _fake_app():
    """run_sr_compute / sr_counts 依赖的最小 app 形状（不动磁盘/网络）。"""
    events = []

    class _Bc:
        @staticmethod
        def emit(kind, payload):
            events.append((kind, payload))

    app = types.SimpleNamespace(
        sr={"cfg": "OLD_CFG", "computed_at": 111, "result": "OLD", "meta": "OLD_META"},
        broadcaster=_Bc, _events=events)
    return app


def _engine_result(current=100.0, merged=(1.0, 2.0), drawn=("D", "60")):
    return {"currentPrice": current,
            "merged": [{"price": p} for p in merged],
            "drawnByPeriod": {L: [{"price": 1.0}] for L in drawn}}


class RunSrComputeIsolationTest(unittest.TestCase):
    def _cfg(self, symbols):
        return ControlApp.normalize_sr_cfg(_sr_cfg(symbols=symbols))

    def test_failure_isolated_top_level_is_first_success(self):
        app = _fake_app()
        results = {"OANDA:XAUUSD": _engine_result(current=2450.0),
                   "FX:NAS100": _engine_result(current=18000.0)}

        def fake_ensure(periods, from_ts, log=None, refresh=False, symbol=None):
            if symbol == "OANDA:XAUUSD":
                raise RuntimeError("拉取失败")
            return {"D": [{"time": 1}]}

        def fake_build(bars, cfg, log=None):
            return results[cfg["symbol"]], {"per_level_atr": {}}

        with patch("py_chain.sr_service.ensure_data", side_effect=fake_ensure), \
             patch("py_chain.sr_service.build_chain_result", side_effect=fake_build):
            err, per_symbol = run_sr_compute(app, self._cfg("OANDA:XAUUSD,FX:NAS100"), "auto")

        self.assertIsNone(err)                      # 有成功品种 → 整体不算失败
        self.assertEqual(per_symbol["OANDA:XAUUSD"]["ok"], False)
        self.assertIn("拉取失败", per_symbol["OANDA:XAUUSD"]["error"])
        self.assertTrue(per_symbol["FX:NAS100"]["ok"])
        # 顶层（旧单品种消费方）= 第一个成功的品种，不是第一个请求的
        self.assertEqual(app.sr["result"], results["FX:NAS100"])
        self.assertEqual(set(app.sr["results"]), {"OANDA:XAUUSD", "FX:NAS100"})
        self.assertIn("error", app.sr["results"]["OANDA:XAUUSD"])
        self.assertIn("result", app.sr["results"]["FX:NAS100"])

    def test_all_fail_leaves_slot_untouched(self):
        app = _fake_app()
        with patch("py_chain.sr_service.ensure_data",
                   side_effect=RuntimeError("CDP 拒绝")):
            err, per_symbol = run_sr_compute(app, self._cfg("A,B"), "auto")
        self.assertIsNotNone(err)
        self.assertFalse(any(v["ok"] for v in per_symbol.values()))
        self.assertEqual(app.sr, {"cfg": "OLD_CFG", "computed_at": 111,
                                  "result": "OLD", "meta": "OLD_META"})

    def test_single_symbol_success_and_progress_symbol(self):
        app = _fake_app()
        with patch("py_chain.sr_service.ensure_data", return_value={"D": [{"time": 1}]}), \
             patch("py_chain.sr_service.build_chain_result",
                   return_value=(_engine_result(), {"per_level_atr": {}})):
            err, per_symbol = run_sr_compute(app, self._cfg("OANDA:XAUUSD"), "auto")
        self.assertIsNone(err)
        self.assertEqual(set(app.sr["results"]), {"OANDA:XAUUSD"})
        self.assertEqual(app.sr["result"]["currentPrice"], 100.0)
        # SSE progress 载荷带 symbol（前端加 [品种] 前缀），完成事件 pct=100
        progs = [p for kind, p in app._events if p.get("mode") == "sr"]
        self.assertTrue(any(p.get("symbol") == "OANDA:XAUUSD" for p in progs))
        self.assertTrue(any(p.get("phase") == "done" and p.get("pct") == 100 for p in progs))

    def test_sr_counts_by_symbol(self):
        app = _fake_app()
        app.sr = {"cfg": {"periods": ["D"], "symbol": "A"},
                  "computed_at": 1,
                  "result": _engine_result(),
                  "meta": {},
                  "results": {"A": {"result": _engine_result(), "meta": {}},
                              "B": {"error": "x"}}}
        counts = ControlApp.sr_counts(app)
        self.assertTrue(counts["has_result"])
        self.assertEqual(counts["by_symbol"]["A"],
                         {"ok": True, "error": None, "merged": 2, "drawn": 2})
        self.assertEqual(counts["by_symbol"]["B"],
                         {"ok": False, "error": "x", "merged": 0, "drawn": 0})


if __name__ == "__main__":
    unittest.main()
