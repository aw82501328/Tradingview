# -*- coding: utf-8 -*-
"""参数漂移守卫测试（2026-10-20 语义指纹改造）。

覆盖三层：
1. config_fingerprint 语义口径：同有效参数（键序重排/深拷贝/无关桶）→ 同指纹；
   有效值/cfg/引擎版本变 → 不同指纹；策略隔离（fxma 桶不影响 chan_v1、sr 不影响
   fxma_v1）；指纹模块集与策略实际消费一致。
2. drift_diff 人读差异：值变化/缺键/引擎版本/截断 12 条/旧存档无指纹 → 口径变更。
3. _drift_guard 分支：无 session/哈希一致恢复；漂移+无持仓自动放行；漂移+有持仓
   拒启（信息含差异与票号与处置路径）；--accept-param-drift 与 param_drift≠refuse
   放行路径（有持仓时带孤儿仓警告）。

运行：py -3.12 -m unittest py_chain.test_config_drift
"""

import copy
import os
import shutil
import tempfile
import unittest
from unittest import mock

from . import live_store, param_center
from .live_trader import (LiveTrader, config_fingerprint, drift_diff,
                          load_config)
from .mt5_broker import MockBroker

SR_STUB = {"atrK": 2.0, "minSpace": 5.0}


def _fp(pm, cfg):
    """固定 sr 读数，隔离真实 module_params.json 的 sr 桶。"""
    with mock.patch.object(param_center, "effective_sr", return_value=dict(SR_STUB)):
        return config_fingerprint(pm, cfg)


class TestFingerprint(unittest.TestCase):

    def setUp(self):
        self.cfg = load_config(None)
        self.pm = param_center.effective_all(self.cfg.get("symbol"))

    def test_same_params_same_hash(self):
        """同有效参数：深拷贝/键序重排/savedAt 类外层噪音 → 指纹不变。"""
        fp1 = _fp(self.pm, self.cfg)
        pm2 = copy.deepcopy(self.pm)
        for mod in pm2.values():          # 内层键序打乱
            items = list(mod.items())
            items.reverse()
            mod.clear()
            mod.update(dict(items))
        outer = list(pm2.items())
        outer.reverse()                   # 外层模块键序打乱（fxma 等无关桶顺序）
        pm2 = dict(outer)
        self.assertEqual(_fp(pm2, self.cfg)["hash"], fp1["hash"])

    def test_effective_value_change(self):
        pm2 = copy.deepcopy(self.pm)
        k = next(iter(pm2["chan"]))
        pm2["chan"][k] = "^^changed^^"
        self.assertNotEqual(_fp(pm2, self.cfg)["hash"],
                            _fp(self.pm, self.cfg)["hash"])

    def test_cfg_change(self):
        cfg2 = copy.deepcopy(self.cfg)
        cfg2["lots"] = int(cfg2["lots"]) + 1
        self.assertNotEqual(_fp(self.pm, cfg2)["hash"],
                            _fp(self.pm, self.cfg)["hash"])

    def test_engine_version_in_fingerprint(self):
        fp1 = _fp(self.pm, self.cfg)
        with mock.patch("py_chain.live_trader.ENGINE_LOGIC_VERSION", "x-test"):
            fp2 = _fp(self.pm, self.cfg)
        self.assertNotEqual(fp1["hash"], fp2["hash"])

    def test_strategy_isolation_fxma_bucket(self):
        """fxma 桶变化：chan_v1 指纹不变（不消费），fxma_v1 指纹变。"""
        pm2 = copy.deepcopy(self.pm)
        k = next(iter(pm2["fxma"]))
        pm2["fxma"][k] = "^^changed^^"
        cfg_fx = load_config(None, {"strategy": "fxma_v1"})
        self.assertEqual(_fp(pm2, self.cfg)["hash"],
                         _fp(self.pm, self.cfg)["hash"])
        self.assertNotEqual(_fp(pm2, cfg_fx)["hash"],
                            _fp(self.pm, cfg_fx)["hash"])

    def test_sr_only_affects_chan_v1(self):
        with mock.patch.object(param_center, "effective_sr",
                               return_value={"atrK": 9.9}):
            fp_sr_changed = config_fingerprint(self.pm, self.cfg)
        self.assertNotEqual(fp_sr_changed["hash"], _fp(self.pm, self.cfg)["hash"])
        cfg_fx = load_config(None, {"strategy": "fxma_v1"})
        with mock.patch.object(param_center, "effective_sr",
                               return_value={"atrK": 9.9}):
            fp_fx = config_fingerprint(self.pm, cfg_fx)
        self.assertEqual(fp_fx["hash"], _fp(self.pm, cfg_fx)["hash"])

    def test_modules_scope(self):
        # chain（周期链）2026-10-23 入 chan_v1 param_modules → 进指纹（改链触发漂移守卫）
        self.assertEqual(set(_fp(self.pm, self.cfg)["modules"]),
                         {"chan", "zs", "points", "sr", "plan", "entry", "chain"})
        cfg_fx = load_config(None, {"strategy": "fxma_v1"})
        self.assertEqual(set(_fp(self.pm, cfg_fx)["modules"]),
                         {"chan", "zs", "points", "fxma"})


class TestDriftDiff(unittest.TestCase):

    @staticmethod
    def _fp_of(modules, version="v1"):
        return {"engine_version": version, "modules": modules}

    def test_value_change(self):
        old = self._fp_of({"chan": {"windowDays15": 15}})
        new = self._fp_of({"chan": {"windowDays15": 50}})
        self.assertEqual(drift_diff(old, new), ["chan.windowDays15: 15→50"])

    def test_missing_key(self):
        old = self._fp_of({"chan": {"a": 1, "b": 2}})
        new = self._fp_of({"chan": {"a": 1}})
        self.assertEqual(drift_diff(old, new), ["chan.b: 2→<缺>"])
        self.assertEqual(drift_diff(new, old), ["chan.b: <缺>→2"])

    def test_engine_version_change(self):
        old = self._fp_of({"chan": {"a": 1}}, version="v1")
        new = self._fp_of({"chan": {"a": 1}}, version="v2")
        self.assertEqual(drift_diff(old, new), ["引擎逻辑版本: v1→v2"])

    def test_truncated_at_12(self):
        old = self._fp_of({f"m{i}": {"k": i} for i in range(15)})
        new = self._fp_of({f"m{i}": {"k": -i} for i in range(15)})
        self.assertEqual(len(drift_diff(old, new)), 12)

    def test_old_archive_without_fp(self):
        new = self._fp_of({"chan": {"a": 1}})
        self.assertEqual(drift_diff(None, new), ["指纹口径变更（旧会话无语义指纹存档）"])


class TestDriftGuard(unittest.TestCase):
    """_drift_guard 分支（MockBroker + 临时 live 库；不构建引擎）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drift_test_")
        os.environ["LIVE_DB_PATH"] = os.path.join(self.tmp, "live.db")
        live_store.ensure_tables()
        self.broker = MockBroker("XAUUSD", magic=20260923)
        self.logs = []

    def tearDown(self):
        os.environ.pop("LIVE_DB_PATH", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _trader(self, cfg=None, accept=False):
        cfg = cfg or load_config(None)
        return LiveTrader(cfg, self.broker, feed=None, log=self.logs.append,
                          accept_param_drift=accept)

    def _fp(self, trader, window_days=50):
        pm = param_center.effective_all(trader.cfg.get("symbol"))
        pm = copy.deepcopy(pm)
        pm["chan"]["windowDays15"] = window_days
        with mock.patch.object(param_center, "effective_sr", return_value=dict(SR_STUB)):
            return config_fingerprint(pm, trader.cfg)

    def _save_session(self, fp, hash_override=None, with_fp=True):
        live_store.save_state("session@chan_v1", {
            "id": "S1", "T_start": 0, "config_hash": hash_override or fp["hash"],
            "symbol": "XAUUSD", "lots": 2, "strategy": "chan_v1",
            **({"config_fp": {"engine_version": fp["engine_version"],
                              "modules": fp["modules"]}} if with_fp else {})})

    def _inject_position(self, ticket=700001):
        self.broker.positions_store[ticket] = {
            "ticket": ticket, "magic": self.broker.magic, "type": 1,
            "volume": 0.02, "sl": 0.0, "price_open": 4000.0, "profit": 0.0}

    def test_no_session(self):
        t = self._trader()
        self.assertIsNone(t._drift_guard(self._fp(t)))
        self.assertFalse(self.logs)

    def test_hash_match_restores(self):
        t = self._trader()
        fp = self._fp(t)
        self._save_session(fp)
        st = t._drift_guard(fp)
        self.assertEqual(st["id"], "S1")
        self.assertFalse(self.logs)

    def test_drift_no_positions_autopass(self):
        t = self._trader()
        self._save_session(self._fp(t, window_days=15))   # 旧指纹 windowDays15=15
        st = t._drift_guard(self._fp(t, window_days=50))  # 当前 50 → 漂移
        self.assertIsNone(st)
        self.assertTrue(any("自动放行重建会话" in x for x in self.logs))
        self.assertTrue(any("chan.windowDays15: 15→50" in x for x in self.logs))

    def test_drift_with_positions_refuses(self):
        t = self._trader()
        self._save_session(self._fp(t, window_days=15))
        self._inject_position(700001)
        with self.assertRaises(RuntimeError) as cm:
            t._drift_guard(self._fp(t, window_days=50))
        msg = str(cm.exception)
        self.assertIn("参数漂移守卫", msg)
        self.assertIn("chan.windowDays15: 15→50", msg)
        self.assertIn("700001", msg)
        self.assertIn("--accept-param-drift", msg)
        self.assertIn("--kill-close", msg)

    def test_drift_accept_flag_orphans_warned(self):
        t = self._trader(accept=True)
        self._save_session(self._fp(t, window_days=15))
        self._inject_position(700001)
        self.assertIsNone(t._drift_guard(self._fp(t, window_days=50)))
        self.assertTrue(any("脱离引擎管理" in x and "700001" in x for x in self.logs))
        self.assertTrue(any("已放行：重置会话" in x for x in self.logs))

    def test_drift_param_drift_not_refuse(self):
        cfg = load_config(None, {"run": {"param_drift": "accept"}})
        t = self._trader(cfg=cfg)
        self._save_session(self._fp(t, window_days=15))
        self._inject_position(700001)
        self.assertIsNone(t._drift_guard(self._fp(t, window_days=50)))
        self.assertTrue(any("已放行：重置会话" in x for x in self.logs))

    def test_old_session_without_fp_notes_migration(self):
        t = self._trader()
        self._save_session(self._fp(t), hash_override="deadbeef00000000",
                           with_fp=False)
        self._inject_position(700001)
        with self.assertRaises(RuntimeError) as cm:
            t._drift_guard(self._fp(t))
        self.assertIn("指纹口径变更（旧会话无语义指纹存档）", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
