# -*- coding: utf-8 -*-
"""参数中心单测：校验/持久化/CHAN_CFG override 往返/生效链路参数传递。"""
import json
from pathlib import Path
import tempfile
import unittest

from . import chan_core, param_center, trading_plan


class ParamCenterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_file = param_center.PARAMS_FILE
        param_center.PARAMS_FILE = str(Path(self.tmp.name) / "module_params.json")
        # 每个用例从干净默认态开始（CHAN_CFG 恢复默认、清掉上一个用例落盘的 overrides）
        chan_core.reset_cfg()

    def tearDown(self):
        chan_core.reset_cfg()
        param_center.PARAMS_FILE = self._orig_file
        self.tmp.cleanup()

    def test_normalize_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            param_center.normalize("chan", {"wickRatio": 1.5})       # 超范围 (>1)
        with self.assertRaises(ValueError):
            param_center.normalize("chan", {"gapFilter": "abc"})     # 非数值
        with self.assertRaises(ValueError):
            param_center.normalize("chan", {"unknownKey": 1})        # 未知键
        with self.assertRaises(ValueError):
            param_center.normalize("chan", {"sinkFallback": True})   # 已迁出到 entry
        with self.assertRaises(ValueError):
            param_center.normalize("entry", {"sinkFallback": "yes"})  # 布尔传字符串
        with self.assertRaises(ValueError):
            param_center.normalize("plan", {"rangeBarN": 5.5})       # 整数传小数
        with self.assertRaises(ValueError):
            param_center.normalize("nope", {})                       # 未知模块
        # 合法值类型规范化
        self.assertEqual(param_center.normalize("chan", {"gapFilter": "0.8"}),
                         {"gapFilter": 0.8})

    def test_update_stores_only_non_defaults(self):
        param_center.update("chan", {"wickRatio": 0.9, "gapFilter": 1.0})  # gapFilter=默认；空 symbol→黄金
        with open(param_center.PARAMS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["modules"]["chan"], {"XAUUSD": {"wickRatio": 0.9}})
        self.assertEqual(param_center.effective("chan")["gapFilter"], 1.0)
        self.assertEqual(chan_core.active_overrides(), {"wickRatio": 0.9})

    def test_chan_apply_and_reset_roundtrip(self):
        # 画笔与进出场分别写 CHAN_CFG；reset 画笔不清进出场覆盖
        param_center.update("chan", {"wickRatio": 0.95})
        param_center.update("entry", {"expectBiEnough": False})
        self.assertEqual(chan_core.CHAN_CFG["wickRatio"], 0.95)       # 全局立即生效
        self.assertFalse(chan_core.CHAN_CFG["expectBiEnough"])
        self.assertEqual(chan_core.CHAN_CFG["gapFilter"],
                         chan_core.CHAN_CFG_DEFAULTS["gapFilter"])    # 未改键不受影响
        param_center.reset("chan")
        self.assertEqual(chan_core.CHAN_CFG["wickRatio"],
                         chan_core.CHAN_CFG_DEFAULTS["wickRatio"])
        self.assertFalse(chan_core.CHAN_CFG["expectBiEnough"])        # entry 覆盖仍在
        self.assertEqual(param_center.effective("chan"),
                         param_center.defaults_of("chan"))
        self.assertNotEqual(param_center.effective("chan"),
                            dict(chan_core.CHAN_CFG_DEFAULTS))        # 画笔 ≠ 整表 CHAN_CFG
        with open(param_center.PARAMS_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["modules"]["chan"], {})
        param_center.reset("entry")
        self.assertEqual(chan_core.CHAN_CFG["expectBiEnough"],
                         chan_core.CHAN_CFG_DEFAULTS["expectBiEnough"])

    def test_legacy_chan_keys_migrate_on_load(self):
        # 旧文件把 divergeDurRatio / expectBiEnough 写在 modules.chan 下 → 读入归到 points/entry
        Path(param_center.PARAMS_FILE).write_text(json.dumps({
            "version": 1,
            "modules": {
                "chan": {"wickRatio": 0.9, "divergeDurRatio": 5, "expectBiEnough": False},
                "points": {},
                "zs": {},
                "entry": {},
                "plan": {},
            },
            "savedAt": {},
        }), encoding="utf-8")
        self.assertEqual(param_center.effective("chan")["wickRatio"], 0.9)
        self.assertNotIn("divergeDurRatio", param_center.effective("chan"))
        self.assertEqual(param_center.effective("points")["divergeDurRatio"], 5)
        self.assertFalse(param_center.effective("entry")["expectBiEnough"])
        cfg = param_center.chan_cfg_effective()
        self.assertEqual(cfg["wickRatio"], 0.9)
        self.assertEqual(cfg["divergeDurRatio"], 5)
        self.assertFalse(cfg["expectBiEnough"])
        # 目标模块已有同键时不覆盖
        Path(param_center.PARAMS_FILE).write_text(json.dumps({
            "version": 1,
            "modules": {
                "chan": {"expectBiEnough": False},
                "entry": {"expectBiEnough": True},
            },
            "savedAt": {},
        }), encoding="utf-8")
        self.assertTrue(param_center.effective("entry")["expectBiEnough"])

    def test_chan_cfg_effective_merges_modules(self):
        param_center.update("chan", {"gapFilter": 0.5})
        param_center.update("points", {"divergeDurRatio": 7})
        param_center.update("entry", {"macdZeroTol": 8.0})
        cfg = param_center.chan_cfg_effective()
        self.assertEqual(cfg["gapFilter"], 0.5)
        self.assertEqual(cfg["divergeDurRatio"], 7)
        self.assertEqual(cfg["macdZeroTol"], 8.0)
        self.assertEqual(cfg["wickRatio"], chan_core.CHAN_CFG_DEFAULTS["wickRatio"])
        self.assertEqual(set(cfg), set(chan_core.CHAN_CFG_DEFAULTS))

    def test_effective_merge_priority(self):
        param_center.update("entry", {"near": 2})
        eff = param_center.effective("entry")
        self.assertEqual(eff["near"], 2)                    # override 优先
        self.assertEqual(eff["lots"], 4)                    # 其余保持默认
        self.assertIn("plan", param_center.effective_all()) # 全模块快照可用

    def test_per_symbol_lots_keys_and_lots_of(self):
        # 进出场按品种分桶（2026-09-24）：手数在各品种桶的 lots；未列品种走代码默认
        gold = param_center.effective("entry", "OANDA:XAUUSD")
        self.assertEqual(gold["lots"], param_center.mark_entry.DEFAULT_LOTS)
        self.assertNotIn("lots_xauusd", gold)
        self.assertEqual(param_center.lots_of(gold, "OANDA:XAUUSD"), gold["lots"])
        other = param_center.effective("entry", "FOO:BAR")
        self.assertEqual(param_center.lots_of(other, "FOO:BAR"),
                         param_center.mark_entry.DEFAULT_LOTS)
        param_center.update("entry", {"lots": 2}, symbol="OANDA:XAGUSD")
        self.assertEqual(param_center.lots_of(
            param_center.effective("entry", "OANDA:XAGUSD"), "OANDA:XAGUSD"), 2)
        self.assertEqual(param_center.effective("entry", "OANDA:XAUUSD")["lots"],
                         param_center.mark_entry.DEFAULT_LOTS)

    def test_entry_per_symbol_isolation_and_legacy(self):
        # 改黄金不影响白银；CHAN_CFG 按品种拼合
        param_center.update("entry", {"near": 12, "macdZeroTol": 8.0}, symbol="XAUUSD")
        param_center.update("entry", {"near": 0.2}, symbol="XAGUSD")
        self.assertEqual(param_center.effective("entry", "XAUUSD")["near"], 12)
        self.assertEqual(param_center.effective("entry", "XAGUSD")["near"], 0.2)
        self.assertEqual(param_center.chan_cfg_effective("XAUUSD")["macdZeroTol"], 8.0)
        self.assertEqual(param_center.chan_cfg_effective("XAGUSD")["macdZeroTol"],
                         chan_core.CHAN_CFG_DEFAULTS["macdZeroTol"])
        # 旧扁平 lots_* + 全局 near → 分到各品种桶
        Path(param_center.PARAMS_FILE).write_text(json.dumps({
            "version": 1,
            "modules": {"entry": {"near": 15.0, "lots_xagusd": 6, "lots": 4}},
            "savedAt": {},
        }), encoding="utf-8")
        self.assertEqual(param_center.effective("entry", "XAGUSD")["lots"], 6)
        self.assertEqual(param_center.effective("entry", "XAGUSD")["near"], 15)
        self.assertEqual(param_center.effective("entry", "XAUUSD")["near"], 15)
        self.assertEqual(param_center.effective("entry", "XAUUSD")["lots"],
                         param_center.mark_entry.DEFAULT_LOTS)
        snap = param_center.snapshot()["entry"]
        self.assertIn("bySymbol", snap)
        self.assertEqual([s["id"] for s in snap["symbols"]],
                         list(param_center.ENTRY_SYMBOLS))

    def test_corrupt_file_degrades_to_defaults(self):
        Path(param_center.PARAMS_FILE).write_text("{ not json", encoding="utf-8")
        self.assertEqual(param_center.effective("plan"),
                         {**trading_plan.RANGE_DEFAULTS, "trendRes": trading_plan.TREND_RES,
                          "rangeRes": trading_plan.RANGE_RES,
                          "trendRebound": trading_plan.TREND_REBOUND,
                          "reboundNearPts": trading_plan.REBOUND_NEAR_PTS,
                          "reboundAngleRef": trading_plan.REBOUND_ANGLE_REF,
                          "prevHighNearPts": trading_plan.PREV_HIGH_NEAR_PTS,
                          "secondNearPts": trading_plan.SECOND_NEAR_PTS,
                          "thirdStrongTrend": trading_plan.THIRD_STRONG_TREND})

    def test_plan_cfg_changes_range_verdict(self):
        # 震荡阈值收得很紧（kMult=0.5 必不满足）→ 原本判震荡的窗口变趋势；
        # 反之 kMult 极大必判震荡——证明 cfg 真正进入 isRangeBound
        bars = [{"time": i * 900, "high": 100 + (i % 7), "low": 99 + (i % 5),
                 "open": 99.5, "close": 100} for i in range(60)]
        atr = 1.5
        bis = []
        for i in range(6):
            t0 = i * 10 * 900
            bis.append({"type": "up" if i % 2 == 0 else "down", "startTime": t0,
                        "endTime": t0 + 9 * 900, "startPrice": 100 + i,
                        "endPrice": 100 + i + 2})
        tight = trading_plan.isRangeBound(bis, bars, atr, {"rangeKMult": 0.001})
        loose = trading_plan.isRangeBound(bis, bars, atr, {"rangeKMult": 50.0})
        self.assertFalse(tight["range"])
        self.assertTrue(loose["range"])
        # 默认 cfg=None 走 RANGE_DEFAULTS（与旧硬编码行为一致）
        self.assertIsNotNone(trading_plan.isRangeBound(bis, bars, atr))

    def test_schema_matches_defaults_keys(self):
        for name in param_center.PARAM_MODULES:
            schema = param_center.schema_of(name)
            defaults = param_center.defaults_of(name)
            self.assertEqual(set(schema), set(defaults))
            for key, spec in schema.items():
                self.assertEqual(spec["default"], defaults[key])
                self.assertIn(spec["type"], ("bool", "int", "float", "str"))
                self.assertTrue(spec["label"])
        # 归属：成笔键在画笔；背驰时长在买卖点；进场扩展在进出场
        self.assertIn("gapFilter", param_center.defaults_of("chan"))
        self.assertIn("divergeDurRatio", param_center.defaults_of("points"))
        self.assertIn("expectBiEnough", param_center.defaults_of("entry"))
        self.assertNotIn("expectBiEnough", param_center.defaults_of("chan"))

    def test_module_titles_align_workbench(self):
        titles = {k: v["title"] for k, v in param_center.PARAM_MODULES.items()}
        self.assertEqual(titles["chan"], "画笔")
        self.assertEqual(titles["zs"], "画中枢")
        self.assertEqual(titles["points"], "标记买卖点")
        self.assertEqual(titles["entry"], "标记进出场")
        self.assertEqual(titles["plan"], "交易计划")

    def test_trend_res_enum(self):
        # plan.trendRes 字符串枚举：合法值通过、非法值/非字符串 raise；默认 = TREND_RES
        self.assertEqual(param_center.defaults_of("plan")["trendRes"],
                         trading_plan.TREND_RES)
        self.assertEqual(param_center.normalize("plan", {"trendRes": "D"}), {"trendRes": "D"})
        self.assertEqual(param_center.normalize("plan", {"trendRes": ""}), {"trendRes": ""})
        for bad in ("XX", 240, None):
            with self.assertRaises(ValueError):
                param_center.normalize("plan", {"trendRes": bad})
        schema = param_center.schema_of("plan")["trendRes"]
        self.assertEqual(schema["type"], "str")
        self.assertEqual(schema["choices"], ["", "240", "D"])
        # 保存-生效往返（"" = 显式关闭，不被默认值覆盖）
        param_center.update("plan", {"trendRes": ""})
        self.assertEqual(param_center.effective("plan")["trendRes"], "")
        param_center.reset("plan")
        self.assertEqual(param_center.effective("plan")["trendRes"], "240")

    def test_range_res_enum_required(self):
        # plan.rangeRes 字符串枚举（必填，2026-09-16 起无关闭项）：仅 240/D；
        # ""（未配置）与其他非法值一样 raise；默认 = RANGE_RES
        self.assertEqual(param_center.defaults_of("plan")["rangeRes"],
                         trading_plan.RANGE_RES)
        self.assertEqual(param_center.normalize("plan", {"rangeRes": "D"}), {"rangeRes": "D"})
        for bad in ("", "XX", 240, None):
            with self.assertRaises(ValueError):
                param_center.normalize("plan", {"rangeRes": bad})
        schema = param_center.schema_of("plan")["rangeRes"]
        self.assertEqual(schema["type"], "str")
        self.assertEqual(schema["choices"], ["240", "D"])

    def test_slip_atr_k_keys(self):
        # 滑点 ATR 系数（2026-09-19）：默认 0（关闭）、允许 0、范围 [0, 10]、override 往返
        defaults = param_center.defaults_of("entry")
        for k in ("slip_stop_atr_k", "slip_fallback_atr_k", "slip_be_atr_k"):
            self.assertEqual(defaults[k], 0.0)
        self.assertEqual(param_center.normalize("entry", {"slip_stop_atr_k": 0}),
                         {"slip_stop_atr_k": 0.0})
        self.assertEqual(param_center.normalize("entry", {"slip_fallback_atr_k": "0.5"}),
                         {"slip_fallback_atr_k": 0.5})
        for bad in (-0.1, 10.1, "abc"):
            with self.assertRaises(ValueError):
                param_center.normalize("entry", {"slip_be_atr_k": bad})
        param_center.update("entry", {"slip_be_atr_k": 0.3})
        self.assertEqual(param_center.effective("entry")["slip_be_atr_k"], 0.3)
        param_center.reset("entry")
        self.assertEqual(param_center.effective("entry")["slip_be_atr_k"], 0.0)

    def test_chan_per_symbol_isolation_and_legacy_flat(self):
        # 改 chan 黄金不影响白银；旧扁平 chan 迁到五品种
        param_center.update("chan", {"wickRatio": 0.88}, symbol="XAUUSD")
        param_center.update("chan", {"wickRatio": 0.55}, symbol="XAGUSD")
        self.assertEqual(param_center.effective("chan", "XAUUSD")["wickRatio"], 0.88)
        self.assertEqual(param_center.effective("chan", "XAGUSD")["wickRatio"], 0.55)
        self.assertEqual(param_center.effective("chan", "USOIL")["wickRatio"],
                         chan_core.CHAN_CFG_DEFAULTS["wickRatio"])
        Path(param_center.PARAMS_FILE).write_text(json.dumps({
            "version": 1,
            "modules": {"chan": {"wickRatio": 0.77, "gapFilter": 0.4}},
            "savedAt": {},
        }), encoding="utf-8")
        for sid in param_center.SYMBOLS:
            self.assertEqual(param_center.effective("chan", sid)["wickRatio"], 0.77)
            self.assertEqual(param_center.effective("chan", sid)["gapFilter"], 0.4)

    def test_chan_cfg_effective_per_symbol_merges(self):
        # chan/points/entry 均按品种桶拼合
        param_center.update("chan", {"gapFilter": 0.3}, symbol="XAUUSD")
        param_center.update("points", {"divergeDurRatio": 9}, symbol="XAUUSD")
        param_center.update("entry", {"macdZeroTol": 6.0}, symbol="XAUUSD")
        param_center.update("chan", {"gapFilter": 0.8}, symbol="XAGUSD")
        gold = param_center.chan_cfg_effective("XAUUSD")
        silver = param_center.chan_cfg_effective("XAGUSD")
        self.assertEqual(gold["gapFilter"], 0.3)
        self.assertEqual(gold["divergeDurRatio"], 9)
        self.assertEqual(gold["macdZeroTol"], 6.0)
        self.assertEqual(silver["gapFilter"], 0.8)
        self.assertEqual(silver["divergeDurRatio"],
                         chan_core.CHAN_CFG_DEFAULTS["divergeDurRatio"])
        self.assertEqual(silver["macdZeroTol"],
                         chan_core.CHAN_CFG_DEFAULTS["macdZeroTol"])

    def test_sr_per_symbol_and_snapshot(self):
        # sr 按品种隔离；snapshot 含 sr.bySymbol
        param_center.update_sr("XAUUSD", {
            "srTypes": ["cluster", "boll"], "periods": ["D", "60"],
            "symbol": "OANDA:XAUUSD", "from_ts": 1,  # 运行时键应剔除
        })
        param_center.update_sr("XAGUSD", {
            "srTypes": ["boll"], "periods": ["15"],
        })
        gold = param_center.effective_sr("XAUUSD")
        self.assertEqual(gold["srTypes"], ["cluster", "boll"])
        self.assertEqual(gold["periods"], ["D", "60"])
        self.assertNotIn("symbol", gold)
        self.assertNotIn("from_ts", gold)
        self.assertEqual(param_center.effective_sr("XAGUSD")["srTypes"], ["boll"])
        self.assertEqual(param_center.effective_sr("USOIL"),
                         param_center.SR_DEFAULTS)
        snap = param_center.snapshot()
        self.assertIn("sr", snap)
        self.assertIsNone(snap["sr"]["schema"])
        self.assertIn("bySymbol", snap["sr"])
        self.assertEqual(snap["sr"]["bySymbol"]["XAUUSD"]["effective"]["periods"],
                         ["D", "60"])
        self.assertEqual([s["id"] for s in snap["sr"]["symbols"]],
                         list(param_center.SYMBOLS))
        # 各 PARAM_MODULES 也带 bySymbol
        for name in param_center.PARAM_MODULES:
            self.assertIn("bySymbol", snap[name])
        param_center.reset_sr("XAUUSD")
        self.assertEqual(param_center.effective_sr("XAUUSD"),
                         param_center.SR_DEFAULTS)
        self.assertEqual(param_center.effective_sr("XAGUSD")["srTypes"], ["boll"])


if __name__ == "__main__":
    unittest.main()
