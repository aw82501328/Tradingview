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
            param_center.normalize("chan", {"sinkFallback": "yes"})  # 布尔传字符串
        with self.assertRaises(ValueError):
            param_center.normalize("plan", {"rangeBarN": 5.5})       # 整数传小数
        with self.assertRaises(ValueError):
            param_center.normalize("nope", {})                       # 未知模块
        # 合法值类型规范化
        self.assertEqual(param_center.normalize("chan", {"gapFilter": "0.8"}),
                         {"gapFilter": 0.8})

    def test_update_stores_only_non_defaults(self):
        param_center.update("chan", {"wickRatio": 0.9, "gapFilter": 1.0})  # gapFilter=默认
        with open(param_center.PARAMS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["modules"]["chan"], {"wickRatio": 0.9})  # 默认值键被剔除
        self.assertEqual(param_center.effective("chan")["gapFilter"], 1.0)
        self.assertEqual(chan_core.active_overrides(), {"wickRatio": 0.9})

    def test_chan_apply_and_reset_roundtrip(self):
        param_center.update("chan", {"wickRatio": 0.95, "expectBiEnough": False})
        self.assertEqual(chan_core.CHAN_CFG["wickRatio"], 0.95)       # 全局立即生效
        self.assertFalse(chan_core.CHAN_CFG["expectBiEnough"])
        self.assertEqual(chan_core.CHAN_CFG["gapFilter"],
                         chan_core.CHAN_CFG_DEFAULTS["gapFilter"])    # 未改键不受影响
        param_center.reset("chan")
        self.assertEqual(chan_core.CHAN_CFG, chan_core.CHAN_CFG_DEFAULTS)
        self.assertEqual(param_center.effective("chan"),
                         dict(chan_core.CHAN_CFG_DEFAULTS))
        with open(param_center.PARAMS_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["modules"]["chan"], {})

    def test_effective_merge_priority(self):
        param_center.update("entry", {"near": 2})
        eff = param_center.effective("entry")
        self.assertEqual(eff["near"], 2)                    # override 优先
        self.assertEqual(eff["lots"], 4)                    # 其余保持默认
        self.assertIn("plan", param_center.effective_all()) # 全模块快照可用

    def test_corrupt_file_degrades_to_defaults(self):
        Path(param_center.PARAMS_FILE).write_text("{ not json", encoding="utf-8")
        self.assertEqual(param_center.effective("plan"),
                         {**trading_plan.RANGE_DEFAULTS, "trendRes": trading_plan.TREND_RES,
                          "rangeRes": trading_plan.RANGE_RES})

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


if __name__ == "__main__":
    unittest.main()
