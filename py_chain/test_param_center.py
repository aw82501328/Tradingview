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
        # 支阻方案文件同样隔离（set_sr_preset 读全局方案列表）
        self._orig_presets = param_center.SR_PRESETS_FILE
        param_center.SR_PRESETS_FILE = str(Path(self.tmp.name) / "sr_presets.json")
        Path(param_center.SR_PRESETS_FILE).write_text(json.dumps([
            {"name": "方案A", "cfg": {"periods": ["D", "60"], "clusterAtr": "0.5",
                                      "fibLevels": "0.382,0.5", "symbol": "X", "from": "2026-01-01"}},
            {"name": "方案B", "cfg": {"periods": ["D", "15"], "clusterAtr": "0.6"}},
        ], ensure_ascii=False), encoding="utf-8")
        # 每个用例从干净默认态开始（CHAN_CFG 恢复默认、清掉上一个用例落盘的 overrides）
        chan_core.reset_cfg()

    def tearDown(self):
        chan_core.reset_cfg()
        param_center.PARAMS_FILE = self._orig_file
        param_center.SR_PRESETS_FILE = self._orig_presets
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
        # 近等双顶新键：周期开关 bool 严格、固定容差无上限
        with self.assertRaises(ValueError):
            param_center.normalize("chan", {"nearDouble60": "yes"})  # 布尔传字符串
        self.assertEqual(param_center.normalize("chan", {"nearDoubleFixed": 99999}),
                         {"nearDoubleFixed": 99999.0})
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
                self.assertIn(spec["type"], ("bool", "int", "float", "str", "multi"))
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

    def test_module_categories_align_registry(self):
        """模块分层归属（2026-09-29 二轮）：chan/zs/points=基础组件，
        sr/plan/entry=策略（缠论V1——支阻位归策略组，参数按方案单选生效）。"""
        for name, spec in param_center.PARAM_MODULES.items():
            self.assertIn(spec["category"], ("base", "strategy"), name)
        self.assertEqual(set(param_center.PARAM_MODULES[n]["category"]
                             for n in ("chan", "zs", "points")), {"base"})
        self.assertEqual(set(param_center.PARAM_MODULES[n]["category"]
                             for n in ("plan", "entry")), {"strategy"})
        snap = param_center.snapshot()
        self.assertEqual(snap["sr"]["category"], "strategy")
        self.assertEqual(snap["plan"]["category"], "strategy")

    def test_sr_preset_apply_switch_unset(self):
        """生效方案单选（复制语义）：生效=方案参数复制入桶并标记；切换=覆盖；取消=保留参数。"""
        eff = param_center.set_sr_preset("XAUUSD", "方案A")
        self.assertEqual(eff["periods"], ["D", "60"])          # 方案参数已入桶
        self.assertNotIn("symbol", eff)                         # 运行时键剥离
        self.assertNotIn("preset", eff)                         # 元数据键不进 effective
        snap = param_center.snapshot()
        self.assertEqual(snap["sr"]["bySymbol"]["XAUUSD"]["preset"], "方案A")
        self.assertIsNone(snap["sr"]["bySymbol"]["XAGUSD"]["preset"])  # 按品种独立
        # 切换（单选覆盖）
        param_center.set_sr_preset("XAUUSD", "方案B")
        eff2 = param_center.effective_sr("XAUUSD")
        self.assertEqual(eff2.get("clusterAtr"), "0.6")
        self.assertEqual(param_center.snapshot()["sr"]["bySymbol"]["XAUUSD"]["preset"], "方案B")
        # 取消：标记清除、桶参数保留
        param_center.set_sr_preset("XAUUSD", None)
        snap3 = param_center.snapshot()
        self.assertIsNone(snap3["sr"]["bySymbol"]["XAUUSD"]["preset"])
        self.assertEqual(param_center.effective_sr("XAUUSD").get("clusterAtr"), "0.6")
        # 未知方案拒绝
        with self.assertRaises(ValueError):
            param_center.set_sr_preset("XAUUSD", "不存在")

    def test_update_sr_keeps_preset_marker(self):
        """编辑器保存（update_sr）不携带 preset 键——落盘保留品种已选生效标记。"""
        param_center.set_sr_preset("XAUUSD", "方案A")
        param_center.update_sr("XAUUSD", {"periods": ["D"], "clusterAtr": "0.7"})
        snap = param_center.snapshot()
        self.assertEqual(snap["sr"]["bySymbol"]["XAUUSD"]["preset"], "方案A")
        self.assertEqual(param_center.effective_sr("XAUUSD").get("clusterAtr"), "0.7")

    def test_reset_sr_clears_preset_marker(self):
        """恢复默认=清空品种桶（含生效标记）。"""
        param_center.set_sr_preset("XAUUSD", "方案A")
        param_center.reset_sr("XAUUSD")
        snap = param_center.snapshot()
        self.assertIsNone(snap["sr"]["bySymbol"]["XAUUSD"]["preset"])

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

    def test_range_res_enum_with_off(self):
        # plan.rangeRes 字符串枚举（2026-10-08 起含关闭项）：240/D 开启闸门、"" 关闭
        # （每周期自判震荡）；其他非法值 raise；默认 = RANGE_RES（240，闸门开启）
        self.assertEqual(param_center.defaults_of("plan")["rangeRes"],
                         trading_plan.RANGE_RES)
        self.assertEqual(param_center.normalize("plan", {"rangeRes": "D"}), {"rangeRes": "D"})
        self.assertEqual(param_center.normalize("plan", {"rangeRes": ""}), {"rangeRes": ""})
        for bad in ("XX", 240, None):
            with self.assertRaises(ValueError):
                param_center.normalize("plan", {"rangeRes": bad})
        schema = param_center.schema_of("plan")["rangeRes"]
        self.assertEqual(schema["type"], "str")
        self.assertEqual(schema["choices"], ["", "240", "D"])
        # 保存-生效往返（"" = 显式关闭，不被默认值覆盖）
        param_center.update("plan", {"rangeRes": ""})
        self.assertEqual(param_center.effective("plan")["rangeRes"], "")
        param_center.reset("plan")
        self.assertEqual(param_center.effective("plan")["rangeRes"], "240")

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

    def test_chan_window_days_keys(self):
        # 小周期绘制窗口（2026-10-02 参数化）：默认 15/30/3、范围 [1,365]、进 chan_cfg_effective
        defaults = param_center.defaults_of("chan")
        self.assertEqual((defaults["windowDays3"], defaults["windowDays15"],
                          defaults["windowDays30S"]), (15, 30, 3))
        self.assertEqual(param_center.normalize("chan", {"windowDays3": 45}),
                         {"windowDays3": 45})
        self.assertEqual(param_center.normalize("chan", {"windowDays15": "90"}),
                         {"windowDays15": 90})
        for bad in (0, 366, "abc"):
            with self.assertRaises(ValueError):
                param_center.normalize("chan", {"windowDays3": bad})
        cfg = param_center.chan_cfg_effective("XAUUSD")
        self.assertEqual((cfg["windowDays3"], cfg["windowDays15"],
                          cfg["windowDays30S"]), (15, 30, 3))
        param_center.update("chan", {"windowDays3": 45, "windowDays15": 90}, symbol="XAUUSD")
        cfg = param_center.chan_cfg_effective("XAUUSD")
        self.assertEqual((cfg["windowDays3"], cfg["windowDays15"]), (45, 90))
        self.assertEqual(param_center.chan_cfg_effective("XAGUSD")["windowDays3"], 15)

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

    def test_update_with_redirected_store_skips_real_export(self):
        # 导出隔离守卫：PARAMS_FILE 被重定向（本 setUp 即是）时 update 不得写/删
        # 真实导出目录 .cursor/cache/chan_cfg_*.json（画笔读取的生产文件）
        real_dir = Path(param_center._CHAN_CFG_EXPORT_DIR)
        before = {p.name: (p.stat().st_mtime_ns, p.read_bytes())
                  for p in real_dir.glob("chan_cfg_*.json")} if real_dir.exists() else {}
        param_center.update("chan", {"fractalSideRealWick": True}, "OANDA:XAUUSD")
        after = {p.name: (p.stat().st_mtime_ns, p.read_bytes())
                 for p in real_dir.glob("chan_cfg_*.json")} if real_dir.exists() else {}
        self.assertEqual(after, before)

    def test_export_chan_cfg_files_roundtrip(self):
        # 导出本体：有桶品种写 chan_cfg_<sid>.json（含参数中心覆盖值），桶清空删文件
        export_dir = Path(self.tmp.name) / "cache"
        orig_dir = param_center._CHAN_CFG_EXPORT_DIR
        orig_default = param_center._DEFAULT_PARAMS_FILE
        param_center._CHAN_CFG_EXPORT_DIR = str(export_dir)
        param_center._DEFAULT_PARAMS_FILE = param_center.PARAMS_FILE  # 守卫对临时存储放行
        try:
            param_center.update("chan", {"fractalSideRealWick": True}, "OANDA:XAUUSD")
            fp = export_dir / "chan_cfg_XAUUSD.json"
            self.assertTrue(fp.exists())
            payload = json.loads(fp.read_text(encoding="utf-8"))
            self.assertEqual(payload["symbol"], "XAUUSD")
            self.assertIn("generatedAt", payload)
            self.assertTrue(payload["cfg"]["fractalSideRealWick"])
            self.assertEqual(payload["cfg"]["nearDoubleFixed"],
                             chan_core.CHAN_CFG_DEFAULTS["nearDoubleFixed"])
            param_center.reset("chan", "OANDA:XAUUSD")
            self.assertFalse(fp.exists())  # 桶清空 → 导出文件同步删除
        finally:
            param_center._CHAN_CFG_EXPORT_DIR = orig_dir
            param_center._DEFAULT_PARAMS_FILE = orig_default
class TestExitModesParams(unittest.TestCase):
    """出场方式参数（2026-10-08）：schema/默认/至少启用一种约束/engine 透传"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_file = param_center.PARAMS_FILE
        param_center.PARAMS_FILE = str(Path(self.tmp.name) / "module_params.json")
        chan_core.reset_cfg()

    def tearDown(self):
        chan_core.reset_cfg()
        param_center.PARAMS_FILE = self._orig_file
        self.tmp.cleanup()

    def test_exit_keys_in_schema_and_defaults(self):
        from py_chain.mark_entry import EXIT_MODE_DEFAULTS
        defaults = param_center.defaults_of("entry")
        for k, v in EXIT_MODE_DEFAULTS.items():
            self.assertIn(k, defaults)
            self.assertEqual(defaults[k], v)
            self.assertIn(k, param_center.PARAM_MODULES["entry"]["params"])

    def test_update_rejects_all_off(self):
        with self.assertRaises(ValueError):
            param_center.update("entry", {
                "exitStopSrOn": False, "exitStopBeOn": False, "exitHalfOn": False,
                "exitCloseOn": False, "exitTrailOn": False})
        # 与存量覆盖合并后全关同样拒绝：先显式关三种只留 stopSr + trail
        param_center.update("entry", {"exitStopBeOn": False, "exitHalfOn": False,
                                      "exitCloseOn": False, "exitTrailOn": True})
        # exitTrailOn=False 等于默认被剔除 → stopSrOn 回落默认开，合并后仍非全关，放行
        param_center.update("entry", {"exitTrailOn": False})
        with self.assertRaises(ValueError):
            param_center.update("entry", {"exitStopSrOn": False, "exitStopBeOn": False,
                                          "exitHalfOn": False, "exitCloseOn": False,
                                          "exitTrailOn": False})  # 合并后全关

    def test_update_accepts_partial_off(self):
        eff = param_center.update("entry", {"exitHalfOn": False, "exitTrailOn": True,
                                            "exitHalfPct": 30})
        self.assertFalse(eff["exitHalfOn"])
        self.assertTrue(eff["exitTrailOn"])
        self.assertEqual(eff["exitHalfPct"], 30)

    def test_engine_module_params_passthrough(self):
        param_center.update("entry", {"exitTrailOn": True, "exitClosePct": 80})
        pm = param_center.effective_all()
        mp = param_center.engine_module_params(pm)
        self.assertTrue(mp["exitTrailOn"])
        self.assertEqual(mp["exitClosePct"], 80)
        self.assertTrue(mp["exitStopSrOn"])          # 默认键也透传
        self.assertEqual(mp["exitHalfPct"], 50)


if __name__ == "__main__":
    unittest.main()
