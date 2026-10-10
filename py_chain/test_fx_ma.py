import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志

# -*- coding: utf-8 -*-
"""强分型均线V1（fxma_v1）策略引擎单测。

覆盖：
  - 组件：MaAcc（SMA/EMA 增量与就绪）、strong_fx_after（实体口径 + minPts 落差）、
    parse_multi / fx_strategy_key / 参数中心 fxma 模块（multi 校验、默认值、kwargs 映射）
  - 注册表与引擎分发（module_registry fxma_v1 / engine_dispatch / fxma_load_periods）
  - 信号评估（_fx_collect，猴子补丁 _fx_latest_points 注入合成买卖点）：
    点→强分型→均线分离齐备才触发 / 每点一次 / crossMinPts 间距不足不出 /
    pointValidBars 超时作废 / pointValidPts 盘中价距超限等待（不作废，回范围可触发）/
    反向点作废 / 类别过滤 / SMA 与 EMA 两口径
  - 出场状态机：止损/止盈盘中触及（fine 周期粒度、按触发价即时成交）、同根双触
    stop 优先与 tp 口径、进场前根不判出场（_evalCutFine 边界）、P≠fine 周期持仓
    也按 fine 根判
  - 互斥：global 同向一笔 + 同拍共振取大周期；perPeriod 每周期独立
  - run()/step_to() 集成：合成K线 + 注入点 → 信号→成交→出场全链路（含 30S 周期）

运行：python -m unittest py_chain.test_fx_ma -v
"""

import unittest
from contextlib import ExitStack
from unittest.mock import patch

from py_chain import param_center, module_registry
from py_chain.engine_dispatch import engine_class_of, fxma_load_periods
from py_chain.fx_ma import (
    FxMaEngine, MaAcc, parse_multi, parse_fib_levels, fx_strategy_key,
    strong_fx_after, FXMA_DEFAULTS, run_fxma_backtest,
)

SEC3 = 180


def bar(time, open_, high, low, close):
    return {"time": time, "open": open_, "high": high, "low": low, "close": close}


def bars_from_closes(base_time, closes, sec=SEC3):
    """按收盘价序列构造K线（方向感知影线，保证相邻不包含、V 反处能成分型）：
    下跌K：high=开+0.2、low=收−0.4（留下影）；上涨K：high=收+0.2、low=开（无下影）
    —— 反转阳线的低点（=开盘=前收）高于前一根下跌K的低点，底分型可成立。"""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev if i else c - 1
        if c < o:      # 下跌K
            hi, lo = o + 0.2, c - 0.4
        elif c > o:    # 上涨K
            hi, lo = c + 0.2, o
        else:          # 平盘K
            hi, lo = o + 0.2, o - 0.2
        out.append(bar(base_time + i * sec, round(o, 4), round(hi, 4), round(lo, 4), c))
        prev = c
    return out


def mk_engine(bars3, **kw):
    """单周期 '3' 的引擎（其余周期空数据；默认 2/3 类点均线对 5/8、止损10/止盈30）。"""
    kw.setdefault("entry_res", "3")
    kw.setdefault("point_classes", "1,2,2x,3,3x")
    return FxMaEngine({"3": bars3}, **kw)


# ============================================================
# 组件
# ============================================================

class MaAccTests(unittest.TestCase):
    def test_sma_incremental_and_readiness(self):
        m = MaAcc("SMA", 3)
        self.assertFalse(m.ready)
        m.push(10)
        m.push(20)
        self.assertFalse(m.ready)
        m.push(30)
        self.assertTrue(m.ready)
        self.assertEqual(m.value, 20.0)
        m.push(40)  # 窗口滑动：20,30,40 → 30
        self.assertEqual(m.value, 30.0)

    def test_ema_incremental(self):
        m = MaAcc("EMA", 3)
        self.assertFalse(m.ready)
        m.push(10)
        self.assertTrue(m.ready)
        self.assertAlmostEqual(m.value, 10.0)
        m.push(20)
        self.assertAlmostEqual(m.value, 10 + (20 - 10) * 0.5)  # k=2/(3+1)


class StrongFxTests(unittest.TestCase):
    def _merged(self, left_open, right_close):
        return [{"open": left_open, "close": left_open + 1},
                {"open": 50, "close": 50},
                {"open": right_close + 1, "close": right_close}]

    def test_bottom_entity_rule_and_min_pts(self):
        merged = self._merged(100, 101)  # 右肩收盘 101 > 左肩开 100
        f = [{"type": "bottom", "time": 500, "mergedIdx": 1}]
        self.assertEqual(strong_fx_after(merged, f, 400, "bottom"), 500)
        self.assertIsNone(strong_fx_after(merged, f, 400, "top"))
        self.assertIsNone(strong_fx_after(merged, f, 600, "bottom"))  # 点在分型后 → 无效

    def test_min_pts_threshold(self):
        merged = self._merged(100, 103)  # 落差 3
        f = [{"type": "bottom", "time": 500, "mergedIdx": 1}]
        self.assertEqual(strong_fx_after(merged, f, 400, "bottom", min_pts=3.0), 500)
        self.assertIsNone(strong_fx_after(merged, f, 400, "bottom", min_pts=3.5))

    def test_top_mirror(self):
        merged = self._merged(105, 100)  # 左开 105 > 右收 100 → 顶分型落差 5
        f = [{"type": "top", "time": 500, "mergedIdx": 1}]
        self.assertEqual(strong_fx_after(merged, f, 400, "top", min_pts=5.0), 500)
        self.assertIsNone(strong_fx_after(merged, f, 400, "top", min_pts=6.0))


class ParseAndKeysTests(unittest.TestCase):
    def test_parse_multi(self):
        self.assertEqual(parse_multi("15, 3,15", ("3", "15"), "x"), ["15", "3"])
        self.assertEqual(parse_multi(["60", "3"], ("3", "15", "60"), "x"), ["60", "3"])
        with self.assertRaises(ValueError):
            parse_multi("3,30", ("3", "15"), "x")
        with self.assertRaises(ValueError):
            parse_multi(",", ("3", "15"), "x")

    def test_strategy_keys(self):
        self.assertEqual(fx_strategy_key(1, "long"), "fx1Buy")
        self.assertEqual(fx_strategy_key(2, "short"), "fx2Sell")
        self.assertEqual(fx_strategy_key(3, "short"), "fx3Sell")
        self.assertEqual(fx_strategy_key("2x", "long"), "fx2xBuy")
        self.assertEqual(fx_strategy_key("2x", "short"), "fx2xSell")
        self.assertEqual(fx_strategy_key("3x", "long"), "fx3xBuy")


class ParamCenterFxmaTests(unittest.TestCase):
    def test_defaults_and_schema(self):
        d = param_center.defaults_of("fxma")
        self.assertEqual(d, FXMA_DEFAULTS)
        sch = param_center.schema_of("fxma")
        self.assertEqual(sch["entryRes"]["type"], "multi")
        self.assertEqual(sch["entryRes"]["choices"], ["30S", "3", "15", "60"])
        self.assertEqual(sch["pointClasses"]["choices"], ["1", "2", "2x", "3", "3x"])
        self.assertEqual(sch["maType"]["type"], "str")
        self.assertEqual(sch["maType"]["choices"], ["SMA", "EMA"])

    def test_normalize_multi_and_ranges(self):
        self.assertEqual(param_center.normalize("fxma", {"entryRes": "60,3,60"})["entryRes"], "60,3")
        self.assertEqual(param_center.normalize("fxma", {"pointClasses": "1,2x"})["pointClasses"], "1,2x")
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"pointClasses": "1,4"})
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"pointClasses": "4x"})
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"stopPts": 0})
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"maType": "WMA"})

    def test_engine_kwargs_mapping(self):
        pm = {"fxma": dict(FXMA_DEFAULTS, entryRes="15,60", stopPts=8.0, lots=2.0),
              "points": {"class2ZsTol": 0.5, "thirdZsTol": 0.25}}
        kw = param_center.fxma_engine_kwargs(pm, lots_override=6, contract_mult=1.5)
        self.assertEqual(kw["entry_res"], "15,60")
        self.assertEqual(kw["stop_pts"], 8.0)
        self.assertEqual(kw["lots"], 6)          # 显式手数优先
        self.assertEqual(kw["contract_mult"], 1.5)
        self.assertEqual(kw["marks_params"], {"class2ZsTol": 0.5, "thirdZsTol": 0.25})
        kw2 = param_center.fxma_engine_kwargs(pm)  # 无显式手数 → 参数中心 fxma.lots
        self.assertEqual(kw2["lots"], 2.0)

    def test_engine_params_roundtrip(self):
        eng = FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])},
                         **FxMaEngine.kwargs_from_params(
                             dict(FXMA_DEFAULTS, entryRes="3", mutexScope="perPeriod")))
        self.assertEqual(eng.entry_res, ["3"])
        self.assertEqual(eng.mutex_scope, "perPeriod")
        with self.assertRaises(ValueError):
            FxMaEngine({"3": []}, entry_res="5")  # 非法周期
        with self.assertRaises(ValueError):
            FxMaEngine({"3": []}, ma_fast1=20, ma_slow1=8)  # 快线须小于慢线

    def test_condition_toggle_schema_and_kwargs(self):
        # 强分型/均线分离两个条件开关：schema 布尔 + kwargs 映射 + 引擎落属性
        sch = param_center.schema_of("fxma")
        self.assertEqual(sch["maOn"]["type"], "bool")
        self.assertEqual(sch["strongFxOn"]["type"], "bool")
        self.assertIs(sch["maOn"]["default"], False)
        self.assertIs(sch["strongFxOn"]["default"], False)
        self.assertIs(param_center.normalize("fxma", {"maOn": False})["maOn"], False)
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"strongFxOn": "0"})  # 布尔不接受字符串
        kw = FxMaEngine.kwargs_from_params(
            dict(FXMA_DEFAULTS, entryRes="3", maOn=False, strongFxOn=False))
        self.assertIs(kw["ma_on"], False)
        self.assertIs(kw["strong_fx_on"], False)
        eng = FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])}, **kw)
        self.assertIs(eng.ma_on, False)
        self.assertIs(eng.strong_fx_on, False)

    def test_pick_and_req_schema_and_kwargs(self):
        # 条件性质（必选/可选）+ 各类点满足数（三选N）：schema 枚举 + normalize +
        # kwargs 布尔/整数映射 + 引擎落属性；默认=全必选+全N=1；
        # fibReq/divLowerReq 已删（黄金分割=硬门槛、背驰与条件组合互斥，2026-10-09）
        sch = param_center.schema_of("fxma")
        for k in ("maReq", "maStandReq", "strongFxReq"):
            self.assertEqual(sch[k]["type"], "str")
            self.assertEqual(sch[k]["choices"], ["required", "optional"])
            self.assertEqual(sch[k]["default"], "required")
        for gone in ("fibReq", "divLowerReq"):
            self.assertNotIn(gone, sch)   # 已删键：normalize 拒绝未知键
            with self.assertRaises(ValueError):
                param_center.normalize("fxma", {gone: "required"})
        for k in ("entryPick1", "entryPick2", "entryPick2x", "entryPick3", "entryPick3x"):
            self.assertEqual(sch[k]["choices"], ["1", "2", "3"])
            self.assertEqual(sch[k]["default"], "1")
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"maReq": "必选"})   # 非法枚举
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"entryPick2": "4"})  # 三选N 上限 3
        n = param_center.normalize("fxma", {"maReq": "optional", "entryPick3x": "1"})
        self.assertEqual(n["maReq"], "optional")
        kw = FxMaEngine.kwargs_from_params(n)
        self.assertIs(kw["ma_req"], False)          # optional → False
        self.assertIs(kw["strong_fx_req"], True)    # 缺省 required → True
        self.assertEqual(kw["entry_pick"]["3x"], 1)
        self.assertEqual(kw["entry_pick"]["2"], 3)  # 缺省 3
        eng = FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])}, **kw)
        self.assertIs(eng.ma_req, False)
        self.assertEqual(eng.entry_pick["3x"], 1)
        eng2 = FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])},
                          entry_pick={"2": 3})   # 3=三选三合法（上限）
        self.assertEqual(eng2.entry_pick["2"], 3)
        with self.assertRaises(ValueError):
            FxMaEngine({"3": []}, entry_pick={"9": 2})   # 非法选择键
        with self.assertRaises(ValueError):
            FxMaEngine({"3": []}, entry_pick={"2": 0})   # 超出 1..3
        with self.assertRaises(ValueError):
            FxMaEngine({"3": []}, entry_pick={"2": 4})   # 超出 1..3（五选N 已收敛为三选N）

    def test_ma_stand_schema_and_kwargs(self):
        # 收盘站线：开关布尔 + 一类/二三类分开的均线周期（1~500）
        sch = param_center.schema_of("fxma")
        self.assertEqual(sch["maStandOn"]["type"], "bool")
        self.assertIs(sch["maStandOn"]["default"], False)
        self.assertEqual(sch["maStand1"]["type"], "int")
        self.assertEqual((sch["maStand1"]["min"], sch["maStand1"]["max"]), (1, 500))
        self.assertEqual(sch["maStand2"]["default"], 5)
        self.assertEqual(param_center.normalize("fxma", {"maStand2": 30})["maStand2"], 30)
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"maStand1": 0})   # 周期须 ≥1
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"maStandOn": "0"})  # 布尔不接受字符串
        kw = FxMaEngine.kwargs_from_params(
            dict(FXMA_DEFAULTS, entryRes="3", maStandOn=False, maStand1=7, maStand2=13))
        self.assertIs(kw["ma_stand_on"], False)
        self.assertEqual(kw["ma_stand1"], 7)
        self.assertEqual(kw["ma_stand2"], 13)
        with self.assertRaises(ValueError):
            FxMaEngine({"3": []}, entry_res="3", ma_stand1=0)  # 引擎侧同校验

    def test_fib_upper_schema_and_kwargs(self):
        # 黄金分割附近/上级同向：开关布尔 + 档位多选串 + 容差点数（黄金分割默认开，上级同向默认关）
        sch = param_center.schema_of("fxma")
        self.assertEqual(sch["fibNearOn"]["type"], "bool")
        self.assertIs(sch["fibNearOn"]["default"], True)
        self.assertEqual(sch["fibLevels"]["type"], "multi")
        self.assertEqual(sch["fibLevels"]["default"], "0.382,0.5,0.618")
        self.assertEqual(param_center.normalize("fxma", {"fibLevels": "0.5"})["fibLevels"], "0.5")
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"fibLevels": "0.65"})   # 档位不在预设集
        self.assertEqual(sch["fibNearPts"]["type"], "float")
        self.assertEqual((sch["fibNearPts"]["min"], sch["fibNearPts"]["max"]), (0.0, 100.0))
        self.assertEqual(sch["upperDirOn"]["type"], "bool")
        self.assertIs(sch["upperDirOn"]["default"], False)
        self.assertIs(param_center.normalize("fxma", {"fibNearOn": True})["fibNearOn"], True)
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"fibNearOn": "1"})   # 布尔不接受字符串
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"fibNearPts": -1})   # 点数须 ≥0
        kw = FxMaEngine.kwargs_from_params(
            dict(FXMA_DEFAULTS, entryRes="3", fibNearOn=True,
                 fibLevels="0.5", fibNearPts=8.0, upperDirOn=True))
        self.assertIs(kw["fib_near_on"], True)
        self.assertEqual(kw["fib_near_levels"], "0.5")
        self.assertEqual(kw["fib_near_pts"], 8.0)
        self.assertIs(kw["upper_dir_on"], True)
        eng = FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])}, **kw)
        self.assertEqual(eng.fib_near_levels, [0.5])
        with self.assertRaises(ValueError):
            FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])},
                       entry_res="3", fib_near_levels="0,1.5")  # 档位须 0<r<1
        with self.assertRaises(ValueError):
            FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])},
                       entry_res="3", fib_near_pts=-1)           # 容差须 ≥0
        FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])},
                   entry_res="3", fib_near_levels="0.5")         # 合法（对照）

    def test_parse_fib_levels(self):
        self.assertEqual(parse_fib_levels("0.382,0.5,0.618"), [0.382, 0.5, 0.618])
        self.assertEqual(parse_fib_levels("0.5,0.5"), [0.5])          # 去重
        self.assertEqual(parse_fib_levels(["0.382", 0.5]), [0.382, 0.5])  # 序列输入
        for bad in ("", "0", "1", "1.5", "abc"):
            with self.assertRaises(ValueError):
                parse_fib_levels(bad)


class RegistryDispatchTests(unittest.TestCase):
    def test_registry_entry(self):
        spec = module_registry.STRATEGIES["fxma_v1"]
        self.assertEqual(spec["engine"], "fx_ma")
        self.assertEqual(spec["param_modules"], ["fxma"])
        self.assertEqual(module_registry.order_of("fxma_v1"),
                         module_registry.BASE_STAGES + ["fxma_entry"])
        self.assertIn("fxma", {m for mods in
                               (module_registry.STRATEGIES[s]["param_modules"]
                                for s in module_registry.STRATEGIES) for m in mods})

    def test_engine_dispatch(self):
        self.assertIs(engine_class_of("chan_v1").__name__ and engine_class_of("chan_v1"),
                      __import__("py_chain.backtest", fromlist=["BacktestEngine"]).BacktestEngine)
        from py_chain.fx_ma import FxMaEngine as FX
        self.assertIs(engine_class_of("fxma_v1"), FX)
        self.assertEqual(fxma_load_periods("3,15,60"), ["D", "240", "60", "15", "3"])
        self.assertEqual(fxma_load_periods("30S"), ["D", "240", "60", "15", "3", "30S"])

    def test_webapp_normalize_cfg_strategy(self):
        from py_chain.webapp import ControlApp
        out = ControlApp.normalize_cfg({"strategy": "fxma_v1", "data_source": "store"}, "backtest")
        self.assertEqual(out["strategy"], "fxma_v1")
        self.assertEqual(ControlApp.normalize_cfg({}, "backtest")["strategy"], "chan_v1")
        with self.assertRaises(ValueError):
            ControlApp.normalize_cfg({"strategy": "bogus"}, "backtest")


# ============================================================
# 信号评估（注入合成买卖点）
# ============================================================

def signal_bars(n_head=12):
    """头段上涨 → 下跌 → V 反（底分型）→ 持续反弹：尾部 SMA5 > SMA8。"""
    closes = []
    closes += [100 + 2.0 * i for i in range(n_head)]            # 上涨
    closes += [closes[-1] - 2.8 * (i + 1) for i in range(10)]   # 下跌
    closes += [closes[-1] + 9.0]                                 # V 反大阳（强底分型右肩）
    closes += [closes[-1] + 2.0 * (i + 1) for i in range(10)]   # 反弹拉开均线
    return closes


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.closes = signal_bars()
        self.bars = bars_from_closes(0, self.closes)
        self.engine = mk_engine(self.bars)
        # 逐根推进（分型增量更新每次只评估 n-2，批量跳拍会缺分型——引擎按逐拍喂数设计）。
        # 不预 _fx_sync_ma：collect 以「新增已收K线」为评估触发（预同步会吃掉水位）
        for b in self.bars:
            self.engine._advance_cut(b["time"] + SEC3)
        self.engine._fx_align()
        # 底分型中心 ≈ 下跌末根（V 反前一根）
        self.pt_time = self.bars[len(self.closes) - 12]["time"]
        self.pt = {"type": "2买", "time": self.pt_time, "price": self.closes[len(self.closes) - 12]}
        self.t_end = self.bars[-1]["time"] + SEC3

    def _fx_ma_state(self):
        return self.engine._fx_ma["3"]

    def _collect_with(self, point):
        st = self.engine._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points",
                          lambda self, P: (point, None)):
            return self.engine._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                           self.t_end), st

    def test_signal_fires_once_with_all_conditions(self):
        # 底分型在点之后成立 + 尾部 SMA5 高于 SMA8 ≥2 点 → fx2Buy 一次
        sigs, st = self._collect_with(self.pt)
        self.assertEqual(len(sigs), 1)
        fxs = self._fx_ma_state()
        fast, slow = fxs["mas"][2].value, fxs["mas"][3].value
        self.assertGreater(fast - slow, 2.0)  # 前置：行情确实满足分离
        s = sigs[0]
        self.assertEqual(s["strategyKey"], "fx2Buy")
        self.assertEqual(s["direction"], "long")
        self.assertEqual(s["periodX"], "3")
        self.assertEqual(s["price"], self.closes[-1])
        self.assertEqual(s["time"], self.t_end)
        self.assertGreater(s["standGap"], 0)          # 默认开：收盘站上 SMA5
        self.assertIn("站上均线", s["reason"])
        self.assertIn("SMA5", s["signalNote"])
        # 同一点再评：每点只触发一次（fired 已落键）
        self.assertEqual(self.engine._fx_collect(st["allSignals"], st["stats"],
                                                 st["fired"], self.t_end + SEC3), [])

    def test_cross_min_pts_blocks_insufficient_gap(self):
        # 间距阈值拉到不可能的大值 → 不出信号（点未作废，仅条件不满足）
        self.engine.cross_min_pts = 1e6
        sigs, _ = self._collect_with(self.pt)
        self.assertEqual(sigs, [])

    # ---------- 条件计票（必选硬门槛 + 通过票数 ≥ 生效N） ----------

    def test_default_config_equals_legacy_and(self):
        # 前置回归：默认（全启用+全必选+N=3）= 旧 AND 行为——单条件破坏即不触发
        self.engine.cross_min_pts = 1e6
        sigs, st = self._collect_with(self.pt)
        self.assertEqual(sigs, [])
        self.assertFalse(st["fired"])   # 必选未过=点存活等待（与旧 fx_ma_gap_fail 一致）

    def test_optional_condition_vote_two_of_three(self):
        # 强分型必选、均线可选、站线可选、二类点三选二：破坏均线后
        # 强分型+站线 2 票达标仍触发；未通过的均线不进 reason/note
        self.engine.cross_min_pts = 1e6
        self.engine.ma_req = False
        self.engine.entry_pick = {"1": 3, "2": 2, "2x": 3, "3": 3, "3x": 3}
        sigs, _ = self._collect_with(self.pt)
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual(s["pickNeed"], 2)
        self.assertEqual(s["pickGot"], 2)
        self.assertEqual(s["reason"], "2买+强分型+站上均线")   # 未通过的均线不在列
        self.assertIn("条件满足 2/3（需2）", s["signalNote"])

    def test_required_failure_blocks_even_with_low_need(self):
        # 强分型必选未过 → 即使 N=1 且另两票在手也不触发（必选=硬门槛）
        self.engine.strong_fx_min_pts = 1e6
        self.engine.ma_req = False
        self.engine.ma_stand_req = False
        self.engine.entry_pick = {"1": 3, "2": 1, "2x": 3, "3": 3, "3x": 3}
        sigs, st = self._collect_with(self.pt)
        self.assertEqual(sigs, [])
        self.assertFalse(st["fired"])

    def test_pick_one_fires_with_single_active_condition(self):
        # 三选一 + 只留强分型（其余停用）：生效N=min(1,1)=1，单条件过即触发
        self.engine.ma_on = False
        self.engine.ma_stand_on = False
        self.engine.entry_pick = {"1": 3, "2": 1, "2x": 3, "3": 3, "3x": 3}
        sigs, _ = self._collect_with(self.pt)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["pickNeed"], 1)
        self.assertEqual(sigs[0]["pickGot"], 1)
        self.assertEqual(sigs[0]["reason"], "2买+强分型")

    def test_disabled_condition_shrinks_effective_need(self):
        # N=3 但停用均线 → 生效N=min(3,2)=2：强分型+站线过即触发（=旧停用行为）
        self.engine.ma_on = False
        sigs, _ = self._collect_with(self.pt)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["pickNeed"], 2)
        self.assertEqual(sigs[0]["pickGot"], 2)
        self.assertIn("条件满足 2/2（需2）", sigs[0]["signalNote"])

    def test_vote_insufficient_waits_then_fires(self):
        # 票数不足=等待（点不作废）：首拍均线破坏 1/2 差票；恢复阈值推进一根后触发
        closes = self.closes + [self.closes[-1] + 2.0]
        e = mk_engine(bars_from_closes(0, closes), strong_fx_on=False,
                      ma_req=False, ma_stand_req=False,
                      entry_pick={"1": 3, "2": 2, "2x": 3, "3": 3, "3x": 3})
        e.cross_min_pts = 1e6
        for b in e.bars["3"]["_list"][:-1]:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        st = e._fx_init_state()
        last = e.bars["3"]["_list"][-1]
        with patch.object(FxMaEngine, "_fx_latest_points",
                          lambda s, P: (self.pt, None)):
            self.assertEqual(e._fx_collect(st["allSignals"], st["stats"],
                                           st["fired"], last["time"]), [])
            self.assertFalse(st["fired"])   # 差票等待，点存活
            e.cross_min_pts = 2.0
            e._advance_cut(last["time"] + SEC3)
            e._fx_align()
            sigs = e._fx_collect(st["allSignals"], st["stats"],
                                 st["fired"], last["time"] + SEC3)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["pickNeed"], 2)
        self.assertEqual(sigs[0]["pickGot"], 2)

    def test_fib_hard_gate_blocks_and_passes(self):
        # 黄金分割=基本参数·独立硬门槛（2026-10-09 起不入票池，无必选/可选之分）：
        # 容差设 -1 使其恒未过（规避合成行情检出不定点）→ 三条件全过（3/3）也不触发；
        # 关闭黄金分割（对照引擎——同一引擎同拍二次评估会被均线水位跳过）→ 恢复触发
        self.engine.fib_near_on = True
        self.engine.fib_near_pts = -1.0
        sigs, st = self._collect_with(self.pt)
        self.assertEqual(sigs, [])
        self.assertFalse(st["fired"])
        e2 = mk_engine(self.bars)
        for b in self.bars:
            e2._advance_cut(b["time"] + SEC3)
        e2._fx_align()
        st2 = e2._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points",
                          lambda s, P: (self.pt, None)):
            sigs2 = e2._fx_collect(st2["allSignals"], st2["stats"],
                                   st2["fired"], self.t_end)
        self.assertEqual(len(sigs2), 1)
        self.assertEqual(sigs2[0]["pickGot"], 3)
        self.assertIn("条件满足 3/3（需3）", sigs2[0]["signalNote"])

    def test_point_valid_bars_expiry(self):
        # 点有效期 1 根：点后已走多根 → 作废不出信号
        self.engine.point_valid_bars = 1
        sigs, _ = self._collect_with(self.pt)
        self.assertEqual(sigs, [])

    def test_point_valid_pts_blocks_far_bar(self):
        # 点有效期(值)：买=评估根最高价(123.2)−点价(94)=29.2 > 5 → 该拍不评估；
        # 等待语义 → fired 不落键（区别于根数版作废）
        self.engine.point_valid_pts = 5.0
        sigs, st = self._collect_with(self.pt)
        self.assertEqual(sigs, [])
        self.assertFalse(st["fired"])

    def test_point_valid_pts_wait_then_recover(self):
        # 超距只等待不作废：阈值 29 拦下（29.2>29）后放开到 30，推进一根平盘尾K
        # （high=123.2、drift 29.2 ≤ 30）同一点仍触发
        closes = self.closes + [self.closes[-1]]
        e = mk_engine(bars_from_closes(0, closes),
                      strong_fx_on=False, ma_on=False, ma_stand_on=False,
                      point_valid_pts=29.0)
        for b in e.bars["3"]["_list"][:-1]:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        st = e._fx_init_state()
        last = e.bars["3"]["_list"][-1]
        with patch.object(FxMaEngine, "_fx_latest_points",
                          lambda s, P: (self.pt, None)):
            self.assertEqual(e._fx_collect(st["allSignals"], st["stats"],
                                           st["fired"], last["time"]), [])
            self.assertFalse(st["fired"])
            e.point_valid_pts = 30.0
            e._advance_cut(last["time"] + SEC3)
            e._fx_align()
            sigs = e._fx_collect(st["allSignals"], st["stats"],
                                 st["fired"], last["time"] + SEC3)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "fx2Buy")

    def test_point_valid_pts_sell_side_mirror(self):
        # 卖侧镜像：评估根最低价(95.6)距卖点最高价(128)=32.4，阈值 32 拦下；
        # 放开到 33 后推进平盘尾K（low=95.8、drift 32.2 ≤ 33）触发 fx2Sell
        closes = [100 + 2.0 * i for i in range(15)] + [126 - 2.0 * (i + 1) for i in range(15)]
        closes.append(closes[-1])
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, strong_fx_on=False, ma_on=False, ma_stand_on=False,
                      point_valid_pts=32.0)
        for b in bars[:-1]:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        pt = {"type": "2卖", "time": bars[14]["time"], "price": 128.0}
        st = e._fx_init_state()
        last = bars[-1]
        with patch.object(FxMaEngine, "_fx_latest_points", lambda s, P: (None, pt)):
            self.assertEqual(e._fx_collect(st["allSignals"], st["stats"],
                                           st["fired"], last["time"]), [])
            e.point_valid_pts = 33.0
            e._advance_cut(last["time"] + SEC3)
            e._fx_align()
            sigs = e._fx_collect(st["allSignals"], st["stats"],
                                 st["fired"], last["time"] + SEC3)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "fx2Sell")

    def test_opposite_point_invalidates(self):
        # 反向（卖点）出现在点之后 → 点作废
        opp = {"type": "1卖", "time": self.pt_time + 5 * SEC3, "price": 120.0}
        e, pt = self.engine, self.pt
        st = e._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points",
                          lambda self, P: (pt, opp)):
            self.assertEqual(e._fx_collect(st["allSignals"], st["stats"], st["fired"], self.t_end), [])

    def test_class_filter(self):
        self.engine.point_classes = {"3"}  # 只交易3类
        sigs, _ = self._collect_with(self.pt)
        self.assertEqual(sigs, [])

    def test_class_split_2x_3x(self):
        # 类2/类3 与严格 2/3 分开选：选择键 2x/3x，策略键 fx2x*/fx3x*；
        # 均线对/站线仍走二三类（maFast2/maSlow2/maStand2），行情不变。
        # 注：collect 后 _fx_sync_ma 水位已推进、无新K线不再评估 → 每场景独立引擎
        def collect(pt_type, classes):
            e = mk_engine(self.bars, point_classes=classes)
            for b in self.bars:
                e._advance_cut(b["time"] + SEC3)
            e._fx_align()
            pt = dict(self.pt, type=pt_type)
            st = e._fx_init_state()
            with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
                return e._fx_collect(st["allSignals"], st["stats"], st["fired"], self.t_end)

        sigs = collect("类2买", "1,2,2x,3,3x")  # 默认全选 → 类2买触发
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "fx2xBuy")
        self.assertEqual(sigs[0]["pointType"], "类2买")
        self.assertEqual(collect("类2买", "1,2,3"), [])  # 不含 2x → 类2买被拒
        self.assertEqual(collect("2买", "2x"), [])      # 只选 2x → 严格 2买被拒
        sigs = collect("类2买", "2x")
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "fx2xBuy")
        sigs = collect("类3买", "3x")
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "fx3xBuy")

    def test_ma_not_ready_no_signal(self):
        # 均线窗口未满（慢线 8 → 头部 7 根内评估）不出信号
        e = mk_engine(bars_from_closes(0, signal_bars()[:7]), ma_slow2=8)
        for b in e.bars["3"]["_list"]:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        pt = {"type": "2买", "time": 0, "price": 100.0}
        st = e._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
            self.assertEqual(e._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                           e.bars["3"]["_list"][-1]["time"] + SEC3), [])

    def test_ema_type_evaluates(self):
        e, pt = mk_engine(self.bars, ma_type="EMA"), self.pt
        for b in self.bars:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        st = e._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
            sigs = e._fx_collect(st["allSignals"], st["stats"], st["fired"], self.t_end)
        self.assertEqual(len(sigs), 1)  # EMA 口径下同样满足（反弹趋势明确）

    def test_ma_off_skips_gap_condition(self):
        # 均线分离关闭：间距阈值不可能满足（对照 test_cross_min_pts_blocks_insufficient_gap）
        # 仍出信号；crossGap=None
        self.engine.cross_min_pts = 1e6
        self.engine.ma_on = False
        sigs, _ = self._collect_with(self.pt)
        self.assertEqual(len(sigs), 1)
        self.assertIsNone(sigs[0]["crossGap"])
        self.assertNotIn("均线分离", sigs[0]["signalNote"])  # 关闭的条件不进叙事

    def test_strong_fx_off_skips_fx_condition(self):
        # 纯单调上涨：点之后无底分型 → 强分型条件卡住；关闭后（均线满足）即出信号
        closes = [100 + 2.0 * i for i in range(30)]
        bars = bars_from_closes(0, closes)
        pt = {"type": "2买", "time": bars[10]["time"], "price": closes[10]}

        def run(e):
            for b in bars:
                e._advance_cut(b["time"] + SEC3)
            e._fx_align()
            st = e._fx_init_state()
            with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
                return e._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                     bars[-1]["time"] + SEC3)

        self.assertEqual(run(mk_engine(bars)), [])  # 对照：强分型开（默认）→ 无信号
        sigs = run(mk_engine(bars, strong_fx_on=False))
        self.assertEqual(len(sigs), 1)
        self.assertIsNone(sigs[0]["strongFxTime"])

    def test_all_conditions_off_fires_on_point_alone(self):
        # 三条件都关：均线未满周期、无分型也照常触发（点属所选类别且未失效即当拍出信号）
        closes = [100 + 2.0 * i for i in range(6)]  # 慢线8未满
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, strong_fx_on=False, ma_on=False, ma_stand_on=False)
        for b in bars:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        pt = {"type": "2买", "time": bars[2]["time"], "price": closes[2]}
        st = e._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
            sigs = e._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                 bars[-1]["time"] + SEC3)
        self.assertEqual(len(sigs), 1)
        self.assertIsNone(sigs[0]["strongFxTime"])
        self.assertIsNone(sigs[0]["crossGap"])
        self.assertIsNone(sigs[0]["standGap"])
        self.assertEqual(sigs[0]["reason"], "2买")

    def test_ma_stand_fail_waits_then_fires_on_recovery(self):
        # 反弹拉开分离后急跌一根：均线分离仍成立、收盘却跌回 SMA5 下方 → 站线不满足
        # 不出信号（点未作废）；再拉开站上后的首个收盘拍触发
        closes = signal_bars()
        closes += [closes[-1] - 5.0]                          # 急跌一根：收盘回到 SMA5 下方
        closes += [closes[-1] + 2.0 * (i + 1) for i in range(8)]  # 再拉开：站上且分离重建
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars)
        dip_end = len(signal_bars())                          # 急跌那根下标（33）
        pt = {"type": "2买", "time": bars[21]["time"], "price": closes[21]}
        st = e._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
            for b in bars[:dip_end + 1]:
                e._advance_cut(b["time"] + SEC3)
            e._fx_align()
            self.assertEqual(e._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                           bars[dip_end]["time"] + SEC3), [])
            for b in bars[dip_end + 1:]:
                e._advance_cut(b["time"] + SEC3)
            e._fx_align()
            sigs = e._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                 bars[-1]["time"] + SEC3)
        self.assertEqual(len(sigs), 1)
        self.assertGreater(sigs[0]["standGap"], 0)
        self.assertIn("站上均线", sigs[0]["reason"])
        self.assertIn("SMA5", sigs[0]["signalNote"])

    def test_ma_stand_not_ready_blocks(self):
        # 站线均线周期拉到不可能的大值 → 未满周期不出信号；一类用 maStand1、二三类用 maStand2
        for pt_type, kw in (("2买", {"ma_stand2": 500}), ("1买", {"ma_stand1": 500})):
            e = mk_engine(self.bars, **kw)
            for b in self.bars:
                e._advance_cut(b["time"] + SEC3)
            e._fx_align()
            pt = {"type": pt_type, "time": self.pt_time, "price": 100.0}
            st = e._fx_init_state()
            with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
                self.assertEqual(e._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                               self.t_end), [])

    def test_ma_stand_off_skips_stand_condition(self):
        # 对照 test_ma_stand_fail_waits...：同行情在急跌拍，关闭站线条件即出信号
        closes = signal_bars() + [signal_bars()[-1] - 5.0]
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, ma_stand_on=False)
        for b in bars:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        pt = {"type": "2买", "time": bars[21]["time"], "price": closes[21]}
        st = e._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
            sigs = e._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                 bars[-1]["time"] + SEC3)
        self.assertEqual(len(sigs), 1)
        self.assertIsNone(sigs[0]["standGap"])
        self.assertNotIn("站上均线", sigs[0]["reason"])  # 关闭的条件不进叙事

    def test_ma_stand_sell_side_strict_below(self):
        # 卖侧对称（只验站线，关强分型/分离）：收盘高于 SMA5 时不出信号（点未作废），
        # 跌回下方后触发 fx2Sell、standGap<0
        closes = [100 + 2.0 * i for i in range(15)] + [126 - 2.0 * (i + 1) for i in range(15)]
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, strong_fx_on=False, ma_on=False)
        pt = {"type": "2卖", "time": bars[14]["time"], "price": closes[14]}
        st = e._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (None, pt)):
            for b in bars[:11]:
                e._advance_cut(b["time"] + SEC3)
            e._fx_align()
            self.assertEqual(e._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                           bars[10]["time"] + SEC3), [])
            for b in bars[11:]:
                e._advance_cut(b["time"] + SEC3)
            e._fx_align()
            sigs = e._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                 bars[-1]["time"] + SEC3)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "fx2Sell")
        self.assertLess(sigs[0]["standGap"], 0)
        self.assertIn("站下均线", sigs[0]["reason"])


class FibUpperGateTests(unittest.TestCase):
    """④黄金分割附近 / ⑤上级周期同向 两闸门（2026-10-03 增，默认关）。

    猴子补丁注入最新点（_fx_latest_points）与前一同侧点（_fx_prev_point）、
    上级末笔方向（_fx_upper_dir）；摆动段极值取自真实K线（与引擎口径一致）。
    """

    def setUp(self):
        self.closes = signal_bars()
        self.bars = bars_from_closes(0, self.closes)
        self.engine = self._engine()
        self.pt_time = self.bars[len(self.closes) - 12]["time"]
        self.pt = {"type": "2买", "time": self.pt_time,
                   "price": self.closes[len(self.closes) - 12]}
        self.t_end = self.bars[-1]["time"] + SEC3
        # 买侧摆动段素材：前点放 bars[0]，窗 = (bars[0].time, pt_time] = bars[1..21]
        self.buy_ext = max(b["high"] for b in self.bars[1:len(self.closes) - 11])

    def _engine(self, **kw):
        e = mk_engine(self.bars, **kw)
        for b in self.bars:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        return e

    def _collect(self, engine, point, prev="unset", upper_dir="unset"):
        st = engine._fx_init_state()
        with ExitStack() as es:
            es.enter_context(patch.object(FxMaEngine, "_fx_latest_points",
                                          lambda self, P: (point, None)))
            if prev != "unset":
                es.enter_context(patch.object(FxMaEngine, "_fx_prev_point",
                                              lambda self, P, pt, side: prev))
            if upper_dir != "unset":
                es.enter_context(patch.object(FxMaEngine, "_fx_upper_dir",
                                              lambda self, P: upper_dir))
            sigs = engine._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                      self.t_end)
        return sigs, st

    # ---------------- ④ 黄金分割附近 ----------------

    def test_fib_near_in_band_fires(self):
        # 前点价定在 0.618 回撤位正中（gap=0）→ 过闸；最近档位=0.618
        prev = {"type": "1买", "time": self.bars[0]["time"],
                "price": self.buy_ext - (self.buy_ext - self.pt["price"]) / 0.618}
        sigs, _ = self._collect(self._engine(fib_near_on=True), self.pt, prev=prev)
        self.assertEqual(len(sigs), 1)
        self.assertAlmostEqual(sigs[0]["fibLevel"], 0.618)
        self.assertAlmostEqual(sigs[0]["fibGap"], 0.0, places=3)
        self.assertIn("黄金分割0.618", sigs[0]["reason"])
        self.assertIn("黄金分割", sigs[0]["signalNote"])

    def test_fib_not_near_waits_not_consumed(self):
        # 前点 100：摆动段 22.2，最近档位(0.618)≈108.5，点价 94 距 14.5 > 5 → 拦下；
        # 等待语义：不消费点（fired 不落键）
        prev = {"type": "1买", "time": self.bars[0]["time"], "price": 100.0}
        sigs, st = self._collect(self._engine(fib_near_on=True), self.pt, prev=prev)
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])

    def test_fib_no_prev_rejects(self):
        # 无前一同侧点（黄金分割无锚点）→ 不触发、不消费
        sigs, st = self._collect(self._engine(fib_near_on=True), self.pt, prev=None)
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])

    def test_fib_class1_exempt_and_off_skip(self):
        far = {"type": "1买", "time": self.bars[0]["time"], "price": 100.0}
        e = self._engine(fib_near_on=True, strong_fx_on=False, ma_on=False,
                         ma_stand_on=False)
        # 1类点豁免：摆动段同样远离档位仍触发，fib 不进叙事
        pt1 = {"type": "1买", "time": self.pt_time, "price": self.pt["price"]}
        sigs, _ = self._collect(e, pt1, prev=far)
        self.assertEqual(len(sigs), 1)
        self.assertIsNone(sigs[0]["fibLevel"])
        # 同摆动段下 2 类点不在档位 → 拦下
        sigs2, _ = self._collect(e, self.pt, prev=far)
        self.assertEqual(sigs2, [])
        # fibNearOn 关（默认）：2 类点直接放行、不进叙事
        sigs3, _ = self._collect(self._engine(strong_fx_on=False, ma_on=False,
                                              ma_stand_on=False), self.pt, prev=far)
        self.assertEqual(len(sigs3), 1)
        self.assertIsNone(sigs3[0]["fibLevel"])
        self.assertNotIn("黄金分割", sigs3[0]["reason"])

    def test_fib_sell_side_symmetric(self):
        # 卖侧对称：前一同侧卖点价 → 其后最低价摆动，档位 L+r×(H−L)，点价=0.618 位
        closes = [100 + 2.0 * i for i in range(15)] + [126 - 2.0 * (i + 1) for i in range(15)]
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, fib_near_on=True, strong_fx_on=False, ma_on=False)
        for b in bars:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        pt = {"type": "2卖", "time": bars[14]["time"], "price": closes[14]}
        ext = min(b["low"] for b in bars[1:15])
        prev = {"type": "1卖", "time": bars[0]["time"],
                "price": ext + (pt["price"] - ext) / 0.618}
        st = e._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (None, pt)), \
             patch.object(FxMaEngine, "_fx_prev_point", lambda self, P, p, side: prev):
            sigs = e._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                 bars[-1]["time"] + SEC3)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "fx2Sell")
        self.assertAlmostEqual(sigs[0]["fibLevel"], 0.618)

    # ---------------- ⑤ 上级周期同向 ----------------

    def test_upper_dir_gate(self):
        # 反向（上级当前笔 down）→ 拦下、不消费点（fired 不落键=点存活，等待上级转同向）
        e = self._engine(upper_dir_on=True)
        sigs, st = self._collect(e, self.pt, upper_dir="down")
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])
        # 上级同向（up）的相同行情：同一点触发，证据字段/叙事齐全
        # （同引擎二次 collect 会被 _fx_sync_ma 水位挡住，故用等价新引擎验证）
        e2 = self._engine(upper_dir_on=True)
        sigs2, _ = self._collect(e2, self.pt, upper_dir="up")
        self.assertEqual(len(sigs2), 1)
        self.assertEqual(sigs2[0]["upperDir"], "up")
        self.assertIn("上级15同向", sigs2[0]["reason"])
        self.assertIn("上级 15", sigs2[0]["signalNote"])

    def test_upper_no_bi_rejects_and_off_skips(self):
        e = self._engine(upper_dir_on=True)
        sigs, st = self._collect(e, self.pt, upper_dir=None)  # 上级无笔 → 不触发
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])
        # 默认关：直接放行、不进叙事
        sigs2, _ = self._collect(self.engine, self.pt)
        self.assertEqual(len(sigs2), 1)
        self.assertIsNone(sigs2[0]["upperDir"])
        self.assertNotIn("上级", sigs2[0]["reason"])

    def test_fx_upper_dir_helper(self):
        # 上级方向 = 结构笔列表末笔 type（含形成中）；无笔 None
        e = self._engine()
        e._structure_bis = {"15": [{"type": "down"}, {"type": "up"}]}
        self.assertEqual(e._fx_upper_dir("3"), "up")
        e._structure_bis = {"15": []}
        self.assertIsNone(e._fx_upper_dir("3"))


class DivLowerGateTests(unittest.TestCase):
    """⑤次级别/次次级别背驰条件（divLower，2026-10-08 增，默认关）。

    语义：缠论V1 lowerDiverge 同源（下沉链自动归属次/次次级别）+ 点锚定·粘性窗口
    [pt.time − 1根P周期K线, 评估时刻]。候选经猴子补丁 fx_ma.lowerDiverge 注入
    （背驰/下沉链判定本身由 test_mark_entry_sink.py 锁定）；「无更低级别数据」
    用真实路径（P=3 且 30S 未加载 → levelsBelow 空 → 条件永不通过）。
    """

    def setUp(self):
        self.closes = signal_bars()
        self.bars = bars_from_closes(0, self.closes)
        self.pt_time = self.bars[len(self.closes) - 12]["time"]
        self.pt = {"type": "2买", "time": self.pt_time,
                   "price": self.closes[len(self.closes) - 12]}
        self.t_end = self.bars[-1]["time"] + SEC3

    def _engine(self, **kw):
        e = mk_engine(self.bars, **kw)
        for b in self.bars:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        return e

    def _collect(self, engine, point, cands=None, lower=None):
        """cands=None 走真实路径；否则注入 long 向候选与更低级别链。"""
        st = engine._fx_init_state()
        with ExitStack() as es:
            es.enter_context(patch.object(FxMaEngine, "_fx_latest_points",
                                          lambda self, P: (point, None)))
            if cands is not None:
                es.enter_context(patch("py_chain.fx_ma.lowerDiverge",
                                       lambda pd, X, d: cands if d == "long" else []))
                es.enter_context(patch("py_chain.fx_ma.levelsBelow",
                                       lambda pd, X: lower if lower is not None else []))
            sigs = engine._fx_collect(st["allSignals"], st["stats"], st["fired"],
                                      self.t_end)
        return sigs, st

    def _cand(self, t, res="15"):
        return {"res": res, "point": {"time": t, "price": 90.0, "direction": "long"}}

    def test_div_lower_schema_and_kwargs(self):
        sch = param_center.schema_of("fxma")
        self.assertEqual(sch["divLowerOn"]["type"], "bool")
        self.assertIs(sch["divLowerOn"]["default"], True)
        self.assertIs(param_center.normalize("fxma", {"divLowerOn": True})["divLowerOn"], True)
        kw = FxMaEngine.kwargs_from_params(
            dict(FXMA_DEFAULTS, entryRes="3", divLowerOn=True))
        self.assertIs(kw["div_lower_on"], True)
        eng = FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])}, **kw)
        self.assertIs(eng.div_lower_on, True)

    def test_default_off_unchanged(self):
        # 默认关：不进计票不进叙事，与旧基线一致
        sigs, _ = self._collect(self._engine(), self.pt)
        self.assertEqual(len(sigs), 1)
        self.assertIsNone(sigs[0]["divLowerRes"])
        self.assertNotIn("次级别", sigs[0]["reason"])
        self.assertFalse(self._engine().div_lower_on)

    def test_div_lower_pass_fires_with_evidence(self):
        # 点后 60s 出现 15m 底背驰候选 → 背驰=唯一触发（免计票），证据字段/叙事齐全
        sigs, _ = self._collect(self._engine(div_lower_on=True), self.pt,
                                cands=[self._cand(self.pt_time + 60)], lower=["15", "3"])
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual(s["divLowerRes"], "15")
        self.assertEqual(s["divLowerTime"], self.pt_time + 60)
        self.assertIn("次级别15背驰", s["reason"])
        self.assertIn("次级别背驰（15", s["signalNote"])
        # 背驰路径免计票：条件组合不参与（pickNeed/pickGot=0）
        self.assertEqual(s["pickNeed"], 0)
        self.assertEqual(s["pickGot"], 0)
        self.assertEqual(s["markRes"], "15")   # 行「背驰级别」= 背驰候选所在级别

    def test_div_lower_boundary_tolerance_included(self):
        # 窗口下界含 1 根 P 周期容差：候选恰在 pt.time − SEC3（次级别与P级极值边界差）→ 通过
        sigs, _ = self._collect(self._engine(div_lower_on=True), self.pt,
                                cands=[self._cand(self.pt_time - SEC3)], lower=["15"])
        self.assertEqual(len(sigs), 1)

    def test_div_lower_old_candidate_blocked_waits(self):
        # 候选早于窗口下界（点前 2 根）→ 未过 → 拦截且点存活（等待语义）
        sigs, st = self._collect(self._engine(div_lower_on=True), self.pt,
                                 cands=[self._cand(self.pt_time - 2 * SEC3)], lower=["15"])
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])

    def test_div_lower_no_candidate_blocks_despite_votes(self):
        # 互斥：启用背驰且背驰不过 → 即使三条件全过也不触发（不回退条件组合）
        sigs, st = self._collect(self._engine(div_lower_on=True), self.pt,
                                 cands=[], lower=["15", "3"])
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])

    def test_div_on_fib_gate_still_applies(self):
        # 基本门槛不互斥：启用背驰（候选通过）+ 黄金分割硬门槛未过 → 仍拦截
        e = self._engine(div_lower_on=True, fib_near_on=True)
        e.fib_near_pts = -1.0          # 黄金分割恒未过（2类点）
        sigs, st = self._collect(e, self.pt, cands=[self._cand(self.pt_time + 60)],
                                 lower=["15"])
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])

    def test_div_lower_sticky_same_extreme_divergence(self):
        # 粘性：候选=点同刻背驰（pt.time−100，同极值口径）——首拍无背驰拦下，
        # 点存活；次拍背驰出现（时间仍在窗口内）即触发
        closes = self.closes + [self.closes[-1] + 2.0]
        e = mk_engine(bars_from_closes(0, closes), div_lower_on=True)
        for b in e.bars["3"]["_list"][:-1]:
            e._advance_cut(b["time"] + SEC3)
        e._fx_align()
        st = e._fx_init_state()
        last = e.bars["3"]["_list"][-1]
        cand = self._cand(self.pt_time - 100)
        with patch.object(FxMaEngine, "_fx_latest_points", lambda s, P: (self.pt, None)), \
             patch("py_chain.fx_ma.lowerDiverge", lambda pd, X, d: []), \
             patch("py_chain.fx_ma.levelsBelow", lambda pd, X: ["15"]):
            self.assertEqual(e._fx_collect(st["allSignals"], st["stats"],
                                           st["fired"], last["time"]), [])
            self.assertFalse(st["fired"])   # 无背驰：点存活等待
        with patch.object(FxMaEngine, "_fx_latest_points", lambda s, P: (self.pt, None)), \
             patch("py_chain.fx_ma.lowerDiverge",
                   lambda pd, X, d: [cand] if d == "long" else []), \
             patch("py_chain.fx_ma.levelsBelow", lambda pd, X: ["15"]):
            e._advance_cut(last["time"] + SEC3)
            e._fx_align()
            sigs = e._fx_collect(st["allSignals"], st["stats"],
                                 st["fired"], last["time"] + SEC3)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["divLowerTime"], self.pt_time - 100)

    def test_div_lower_no_lower_data_real_path(self):
        # 真实路径（不补丁）：P=3 且 30S 未加载 → levelsBelow 空、无候选 → 条件永不通过
        e = self._engine(div_lower_on=True)
        cands, chain = e._fx_lower_diverge_cands("3", "long", {})
        self.assertEqual(cands, [])
        self.assertEqual(chain, [])
        sigs, st = self._collect(e, self.pt)
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])

    # ---- 与条件组合互斥（2026-10-09）+ 窗口调松/防未来 ----

    def test_div_lower_window_schema_and_kwargs(self):
        # 窗口参数：schema + normalize + kwargs + 引擎落属性；divLowerReq 已删
        # （构造不再接受该形参，传入即 TypeError）
        sch = param_center.schema_of("fxma")
        self.assertEqual(sch["divLowerWinBars"]["type"], "int")
        self.assertEqual(sch["divLowerWinBars"]["default"], 3)
        self.assertEqual(param_center.normalize("fxma", {"divLowerWinBars": 4}),
                         {"divLowerWinBars": 4})
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"divLowerWinBars": 0})   # 下界 1
        kw = FxMaEngine.kwargs_from_params(
            dict(FXMA_DEFAULTS, entryRes="3", divLowerOn=True, divLowerWinBars=2))
        self.assertEqual(kw["div_lower_win_bars"], 2)
        eng = FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])}, **kw)
        self.assertEqual(eng.div_lower_win_bars, 2)
        with self.assertRaises(ValueError):
            FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])},
                       entry_res="3", div_lower_win_bars=0)
        with self.assertRaises(TypeError):
            FxMaEngine({"3": bars_from_closes(0, [100 + i for i in range(40)])},
                       entry_res="3", div_lower_req="standalone")   # 形参已删

    def test_div_on_pass_fires_without_votes(self):
        # 互斥：背驰过 → 免计票直接触发（条件组合挂了也过）
        e = self._engine(div_lower_on=True, strong_fx_on=False, ma_stand_on=False,
                         fib_near_on=False)   # 只留均线分离（必选）
        e.cross_min_pts = 1e6           # 均线分离必不过 → 若走条件组合路径必挂
        sigs, _ = self._collect(e, self.pt, cands=[self._cand(self.pt_time + 60)],
                                lower=["15"])
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["divLowerRes"], "15")
        self.assertIn("次级别15背驰", sigs[0]["reason"])
        self.assertEqual(sigs[0]["markRes"], "15")   # 行「背驰级别」= 背驰候选所在级别

    def test_div_on_fail_no_fallback_to_votes(self):
        # 互斥：背驰不过 → 不回退条件组合（对照：同配置关掉背驰即按三选N触发）
        e = self._engine(div_lower_on=True, strong_fx_on=True, ma_stand_on=True,
                         ma_on=False, fib_near_on=False)
        sigs, st = self._collect(e, self.pt, cands=[], lower=["15"])
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])
        e2 = self._engine(strong_fx_on=True, ma_stand_on=True, ma_on=False,
                          fib_near_on=False)
        sigs2, _ = self._collect(e2, self.pt, cands=[], lower=["15"])
        self.assertEqual(len(sigs2), 1)   # 强分型+站线 2/2 达标（三选N路径）
        self.assertEqual(sigs2[0]["pickNeed"], 2)
        self.assertEqual(sigs2[0]["pickGot"], 2)
        self.assertIsNone(sigs2[0]["markRes"])      # 条件组合路径 → 「背驰级别」为空

    def test_div_on_fail_blocks_despite_votes_dead(self):
        # 背驰不过 + 三条件也挂 → 拦截且点存活（等待语义）
        e = self._engine(div_lower_on=True, strong_fx_on=False, ma_stand_on=False,
                         fib_near_on=False)
        e.cross_min_pts = 1e6
        sigs, st = self._collect(e, self.pt, cands=[], lower=["15"])
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])

    def test_div_lower_window_widened_passes(self):
        # 窗口调松（divLowerWinBars=3）：点前 2 根的候选通过（默认 1 根时被拦）
        sigs, _ = self._collect(
            self._engine(div_lower_on=True, div_lower_win_bars=3), self.pt,
            cands=[self._cand(self.pt_time - 2 * SEC3)], lower=["15"])
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["divLowerTime"], self.pt_time - 2 * SEC3)

    def test_div_lower_future_candidate_blocked(self):
        # 防未来：候选时间晚于评估拍 t → 不采用（即使调松窗口也不取未来数据）；
        # 背驰不过即拦截（互斥，不走条件组合）→ 若背驰取了未来候选则此处会出信号
        e = self._engine(div_lower_on=True, div_lower_win_bars=3,
                         strong_fx_on=False, ma_stand_on=False, fib_near_on=False)
        e.cross_min_pts = 1e6
        sigs, st = self._collect(e, self.pt, cands=[self._cand(self.t_end + SEC3)],
                                 lower=["15"])
        self.assertEqual(sigs, [])
        self.assertNotIn(("3", "2买", self.pt_time), st["fired"])


# ============================================================
# 出场状态机
# ============================================================

def mk_pos(direction, entry, stop_pts=10.0, tp_pts=30.0, periodX="3", cut=0, **extra):
    short = direction == "short"
    return {
        "tradeNo": 1, "periodX": periodX, "markRes": periodX, "direction": direction,
        "strategyKey": "fx2Buy" if direction == "long" else "fx2Sell",
        "signalTime": 0, "signalPrice": entry, "entryTime": 0, "entryPrice": entry,
        "lots": 1, "mult": 1.0,
        "stopRef": entry + stop_pts if short else entry - stop_pts,
        "tpRef": entry - tp_pts if short else entry + tp_pts,
        "state": "open", "exits": [], "_evalCutFine": cut, **extra,
    }


class ExitTests(unittest.TestCase):
    def _engine_with_tail(self, closes):
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, stop_pts=10.0, tp_pts=30.0)
        e._advance_cut(bars[-1]["time"] + SEC3)
        return e, bars

    def _check(self, e, direction, closes, **extra):
        """cut 指到末根前 → _fx_check_exits 只评末根（盘中触价即成交口径）。"""
        pos = mk_pos(direction, 100.0, cut=len(closes) - 1, **extra)
        st = e._fx_init_state()
        st["open_pos"][direction] = [pos]
        stats = {"closed": 0, "stopped": 0}
        return pos, st, stats, e._fx_check_exits(st["open_pos"], stats)

    def test_long_stop_hit_fills_at_trigger_price(self):
        # 多单 100：止损 90 / 止盈 130；末根 low 88.6 ≤ 90 → 即时按止损价 90 成交
        closes = [100.0] * 5 + [95.0, 89.0]
        e, bars = self._engine_with_tail(closes)
        pos, st, stats, exits = self._check(e, "long", closes)
        self.assertEqual(len(exits), 1)
        tr = exits[0]
        self.assertEqual(tr["exitType"], "stop")
        self.assertEqual(tr["exitPrice"], 90.0)           # 触发价成交（=stopRef）
        self.assertEqual(tr["exitTime"], bars[-1]["time"])  # 触发根时间
        self.assertAlmostEqual(tr["pnl"], (90.0 - 100.0) * 1 * 1.0)
        self.assertEqual(tr["state"], "closed")
        self.assertEqual(st["open_pos"]["long"], [])       # 容量即时释放
        self.assertEqual(stats["closed"], 1)
        self.assertEqual(stats["stopped"], 1)
        self.assertIn("盘中触发", tr["exitWhy"])

    def test_long_take_profit(self):
        # 末根 high 131.7 ≥ 130 → 即时按止盈价 130 成交
        closes = [100.0] * 5 + [120.0, 131.5]
        e, _ = self._engine_with_tail(closes)
        _pos, _st, _stats, exits = self._check(e, "long", closes)
        self.assertEqual(len(exits), 1)
        tr = exits[0]
        self.assertEqual(tr["exitType"], "takeProfit")
        self.assertEqual(tr["exitPrice"], 130.0)
        self.assertAlmostEqual(tr["pnl"], (130.0 - 100.0) * 1 * 1.0)

    def test_same_bar_both_priority(self):
        # 单根大振幅K盘中同时触及止损(90下方)与止盈(130上方)：默认 stop@90；tp 优先 → takeProfit@130
        head = bars_from_closes(0, [100.0] * 5)
        wild = bar(head[-1]["time"] + SEC3, 100.0, 132.0, 88.0, 99.0)
        def mk_exit(priority):
            e = mk_engine(head + [dict(wild)], same_bar_priority=priority)
            e._advance_cut(wild["time"] + SEC3)
            pos = mk_pos("long", 100.0, cut=len(head))
            st = e._fx_init_state()
            st["open_pos"]["long"] = [pos]
            return e._fx_check_exits(st["open_pos"], {"closed": 0})[0]
        self.assertEqual(mk_exit("stop")["exitType"], "stop")
        self.assertEqual(mk_exit("stop")["exitPrice"], 90.0)
        self.assertEqual(mk_exit("tp")["exitType"], "takeProfit")
        self.assertEqual(mk_exit("tp")["exitPrice"], 130.0)

    def test_entry_bar_not_evaluated(self):
        # _evalCutFine=进场时 cut：进场前的历史根不参与出场判定
        closes = [100.0] * 3 + [85.0]           # 末根（=进场前一根）下破止损
        e, _ = self._engine_with_tail(closes)
        pos = mk_pos("long", 100.0, cut=len(closes))   # 进场时已含末根
        st = e._fx_init_state()
        st["open_pos"]["long"] = [pos]
        exits = e._fx_check_exits(st["open_pos"], {"closed": 0})
        self.assertEqual(exits, [])
        self.assertIsNone(pos.get("exitType"))

    def test_short_side_mirror(self):
        closes = [100.0] * 5 + [105.0, 111.0]   # 空单 100：止损 110 盘中击穿 → @110 成交
        e, _ = self._engine_with_tail(closes)
        _pos, _st, _stats, exits = self._check(e, "short", closes)
        self.assertEqual(len(exits), 1)
        tr = exits[0]
        self.assertEqual(tr["exitType"], "stop")
        self.assertEqual(tr["exitPrice"], 110.0)
        self.assertAlmostEqual(tr["pnl"], (110.0 - 100.0) * -1 * 1.0)

    def test_period15_position_exits_on_fine_bars(self):
        # P=15 持仓同样按 fine=3 根判盘中触发（3m 根下破即成交，不等 15m 收盘确认）
        closes = [100.0] * 5 + [95.0, 89.0]
        bars = bars_from_closes(0, closes)
        e = FxMaEngine({"3": bars}, entry_res="3,15")    # fine=3、持仓 periodX=15
        self.assertEqual(e.fine_res, "3")
        e._advance_cut(bars[-1]["time"] + SEC3)
        pos = mk_pos("long", 100.0, periodX="15", cut=len(closes) - 1)
        st = e._fx_init_state()
        st["open_pos"]["long"] = [pos]
        exits = e._fx_check_exits(st["open_pos"], {"closed": 0})
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["exitType"], "stop")
        self.assertEqual(exits[0]["exitPrice"], 90.0)
        self.assertEqual(exits[0]["exitTime"], bars[-1]["time"])


# ============================================================
# 互斥 / 共振
# ============================================================

class MutexTests(unittest.TestCase):
    def _sig(self, P, d, t=1000):
        return {"periodX": P, "markRes": P, "time": t, "price": 100.0,
                "direction": d, "strategyKey": f"fx2{'Buy' if d == 'long' else 'Sell'}"}

    def test_global_mutex_and_resonance_big_period_wins(self):
        e = mk_engine(bars_from_closes(0, [100.0] * 30), entry_res="3,15")
        st = e._fx_init_state()
        st["open_pos"]["long"] = [mk_pos("long", 100.0)]  # 已有多单（容量占用）
        out = []
        stats = {"executed": 0, "suppressed": 0}
        e._fx_fill_pending(st["trades"], [self._sig("3", "long"), self._sig("15", "long")],
                           st["open_pos"], stats, 1000, 1000, 100.0, sup_out=out)
        self.assertEqual(stats["suppressed"], 2)
        self.assertEqual(len(st["trades"]), 0)
        # 无持仓：同拍 3m+15m 同向共振 → 大周期 15 成交，3m 被过滤
        st["open_pos"]["long"] = []
        e._fx_fill_pending(st["trades"], [self._sig("3", "long"), self._sig("15", "long")],
                           st["open_pos"], stats, 1000, 1000, 100.0, sup_out=out)
        self.assertEqual(len(st["trades"]), 1)
        self.assertEqual(st["trades"][0]["periodX"], "15")
        self.assertEqual(stats["executed"], 1)

    def test_per_period_mutex_allows_each_period(self):
        e = mk_engine(bars_from_closes(0, [100.0] * 30), entry_res="3,15",
                      mutex_scope="perPeriod")
        st = e._fx_init_state()
        st["open_pos"]["3"]["long"] = [mk_pos("long", 100.0)]  # 3m 已有多单
        stats = {"executed": 0, "suppressed": 0}
        e._fx_fill_pending(st["trades"], [self._sig("3", "long")],
                           st["open_pos"], stats, 1000, 1000, 100.0)
        e._fx_fill_pending(st["trades"], [self._sig("15", "long")],
                           st["open_pos"], stats, 1000, 1000, 100.0)
        self.assertEqual(len(st["trades"]), 1)      # 15m 独立成交
        self.assertEqual(st["trades"][0]["periodX"], "15")
        self.assertEqual(stats["suppressed"], 1)    # 3m 被本周期互斥


# ============================================================
# tpMode=structure：半仓容量 / 主动止盈分批 / 提损跟踪 / 3类全平
# ============================================================

class StructureTests(unittest.TestCase):
    def _sig(self, P, d, t=1000, pt_type="2买", pt_time=900):
        return {"periodX": P, "markRes": P, "time": t, "price": 100.0,
                "direction": d, "strategyKey": f"fx2{'Buy' if d == 'long' else 'Sell'}",
                "pointType": pt_type, "pointTime": pt_time, "pointPrice": 100.0}

    def test_same_class_gate_no_stack_until_closed(self):
        # 同粗类闸门：1类持仓中 2类可叠（不同粗类）、同类信号压制；
        # 前一笔终局（移出容量槽）后同类可再开
        e = mk_engine(bars_from_closes(0, [100.0] * 30), tp_mode="structure", lots=4.0)
        st = e._fx_init_state()
        stats = {"executed": 0, "suppressed": 0}
        e._fx_fill_pending(st["trades"], [self._sig("3", "long", pt_type="1买")],
                           st["open_pos"], stats, 1000, 1000, 100.0)
        # 2买=不同粗类 → 成交（容量 2+2=4）
        e._fx_fill_pending(st["trades"], [self._sig("3", "long", t=1100, pt_type="2买",
                                                    pt_time=1000)],
                           st["open_pos"], stats, 1100, 1100, 100.0)
        self.assertEqual(len(st["trades"]), 2)
        # 1买再来 → 同类（粗类1）压制
        e._fx_fill_pending(st["trades"], [self._sig("3", "long", t=1200, pt_type="1买",
                                                    pt_time=1100)],
                           st["open_pos"], stats, 1200, 1200, 100.0)
        self.assertEqual(stats["suppressed"], 1)
        # 类2买 → 粗类2 已持有（第二笔是 2买）→ 压制
        e._fx_fill_pending(st["trades"], [self._sig("3", "long", t=1300, pt_type="类2买",
                                                    pt_time=1200)],
                           st["open_pos"], stats, 1300, 1300, 100.0)
        self.assertEqual(stats["suppressed"], 2)
        self.assertEqual(len(st["trades"]), 2)
        # 第一笔（1买）终局：移出容量槽 → 同类释放，新 1买 可开（容量 2+2=4）
        st["open_pos"]["long"] = [p for p in st["open_pos"]["long"]
                                  if p is not st["trades"][0]]
        e._fx_fill_pending(st["trades"], [self._sig("3", "long", t=1400, pt_type="1买",
                                                    pt_time=1300)],
                           st["open_pos"], stats, 1400, 1400, 100.0)
        self.assertEqual(len(st["trades"]), 3)
        self.assertEqual(stats["suppressed"], 2)

    def test_half_lots_and_capacity_stack(self):
        # structure：lots=4 → 每笔半仓 2 手、不同粗类可叠两笔、第三笔容量压制
        e = mk_engine(bars_from_closes(0, [100.0] * 30), tp_mode="structure", lots=4.0)
        st = e._fx_init_state()
        stats = {"executed": 0, "suppressed": 0}
        e._fx_fill_pending(st["trades"], [self._sig("3", "long", pt_type="2买")],
                           st["open_pos"], stats, 1000, 1000, 100.0)
        e._fx_fill_pending(st["trades"], [self._sig("3", "long", t=1100, pt_type="3买",
                                                    pt_time=1000)],
                           st["open_pos"], stats, 1100, 1100, 100.0)
        e._fx_fill_pending(st["trades"], [self._sig("3", "long", t=1200, pt_type="1买",
                                                    pt_time=1100)],
                           st["open_pos"], stats, 1200, 1200, 100.0)
        self.assertEqual(len(st["trades"]), 2)
        self.assertTrue(all(t["lots"] == 2.0 and t["lotsLeft"] == 2.0
                            for t in st["trades"]))
        self.assertEqual(stats["suppressed"], 1)   # 容量满（2+2=4，再开 2 手超限）
        self.assertEqual(len(st["open_pos"]["long"]), 2)
        # points：满仓一笔（旧行为），第二笔压制
        e2 = mk_engine(bars_from_closes(0, [100.0] * 30), lots=4.0)
        st2 = e2._fx_init_state()
        stats2 = {"executed": 0, "suppressed": 0}
        e2._fx_fill_pending(st2["trades"], [self._sig("3", "long")],
                            st2["open_pos"], stats2, 1000, 1000, 100.0)
        self.assertEqual(st2["trades"][0]["lots"], 4.0)
        e2._fx_fill_pending(st2["trades"], [self._sig("3", "long", t=1100)],
                            st2["open_pos"], stats2, 1100, 1100, 100.0)
        self.assertEqual(stats2["suppressed"], 1)
        self.assertEqual(len(st2["open_pos"]["long"]), 1)

    def test_prev_bi_end_lookup(self):
        # 主动止盈目标=入场点前最近已确认笔端点：多头取上笔终点（前高）、空头取
        # 下笔终点（前低）；_forming 跳过；endTime==point_time 排除（严格早于）
        e = mk_engine(bars_from_closes(0, [100.0] * 30), tp_mode="structure")
        e._bis["3"] = [
            {"type": "up", "startTime": 100, "startPrice": 90.0,
             "endTime": 200, "endPrice": 130.0},
            {"type": "down", "startTime": 200, "startPrice": 130.0,
             "endTime": 300, "endPrice": 110.0},
            {"type": "up", "startTime": 300, "startPrice": 110.0,
             "endTime": 400, "endPrice": 140.0, "_forming": True},
        ]
        got = e._fx_prev_bi_end("3", 350, "long")
        self.assertEqual(got, {"type": "前高", "time": 200, "price": 130.0})  # 跳过 _forming@140
        self.assertEqual(e._fx_prev_bi_end("3", 350, "short"),
                         {"type": "前低", "time": 300, "price": 110.0})
        self.assertEqual(e._fx_prev_bi_end("3", 200, "long"), None)   # endTime==200 不算严格早于
        self.assertEqual(e._fx_prev_bi_end("3", 150, "short"), None)  # 更早处无下笔终点

    def test_class12_active_tp_partial_then_stop(self):
        # 2买入场 @100：前卖点 130 → 主动止盈半份 1 手 @130；剩余 1 手 @90 止损出全，
        # 盈亏按手数加权 (130−100)×1 + (90−100)×1
        closes = [100.0] * 5 + [120.0, 131.5, 95.0, 89.0]
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, tp_mode="structure", lots=4.0, stop_pts=10.0)
        e._advance_cut(bars[6]["time"] + SEC3)   # cut=7：先评 bar5/6（止盈触发）
        with patch.object(FxMaEngine, "_fx_prev_bi_end",
                          lambda self, P, pt_time, d:
                          {"type": "前高", "time": 500, "price": 130.0}):
            st = e._fx_init_state()
            stats = {"executed": 0, "suppressed": 0, "closed": 0, "stopped": 0}
            e._fx_fill_pending(st["trades"], [self._sig("3", "long")],
                               st["open_pos"], stats, 1000, 1000, 100.0)
        tr = st["trades"][0]
        self.assertEqual(tr["lots"], 2.0)
        self.assertAlmostEqual(tr["tpRef"], 130.0)      # 容差默认 0=精确触价
        self.assertEqual(tr["tpLots"], 1.0)             # 1/4 仓主动止盈
        tr["_evalCutFine"] = 5
        self.assertEqual(e._fx_check_exits(st["open_pos"], stats), [])  # 部分平仓不终局
        ev = tr["exits"][0]
        self.assertEqual(ev["type"], "activeTp")
        self.assertEqual(ev["lots"], 1.0)
        self.assertAlmostEqual(ev["price"], 130.0)
        self.assertEqual(tr["lotsLeft"], 1.0)
        self.assertIsNone(tr["tpRef"])                  # 一次性
        self.assertEqual(len(st["open_pos"]["long"]), 1)  # 容量槽未释放
        e._advance_cut(bars[-1]["time"] + SEC3)         # cut=9：评 bar7/8（止损触发）
        exits = e._fx_check_exits(st["open_pos"], stats)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["exitType"], "stop")
        self.assertEqual(exits[0]["exitTime"], bars[-1]["time"])
        self.assertAlmostEqual(exits[0]["exitPrice"], 90.0)
        self.assertAlmostEqual(exits[0]["pnl"], (130.0 - 100.0) * 1 + (90.0 - 100.0) * 1)
        self.assertEqual(st["open_pos"]["long"], [])    # 终局释放容量

    def test_structure_odd_lots_integer(self):
        # 整手口径（2026-10-08）：lots=5 奇数 → 半仓 ⌊5/2⌋=2 手、1/2类主动止盈
        # max(1, ⌊2/2⌋)=1 手，全程无小数
        closes = [100.0] * 5 + [120.0, 131.5, 95.0, 89.0]
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, tp_mode="structure", lots=5, stop_pts=10.0)
        self.assertEqual(e.lots, 5)
        e._advance_cut(bars[6]["time"] + SEC3)
        with patch.object(FxMaEngine, "_fx_prev_bi_end",
                          lambda self, P, pt_time, d:
                          {"type": "前高", "time": 500, "price": 130.0}):
            st = e._fx_init_state()
            stats = {"executed": 0, "suppressed": 0, "closed": 0, "stopped": 0}
            e._fx_fill_pending(st["trades"], [self._sig("3", "long")],
                               st["open_pos"], stats, 1000, 1000, 100.0)
        tr = st["trades"][0]
        self.assertEqual(tr["lots"], 2)
        self.assertEqual(tr["lotsLeft"], 2)
        self.assertEqual(tr["tpLots"], 1)
        self.assertIsInstance(tr["tpLots"], int)

    def test_structure_lots_lt2_rejected(self):
        # structure 半仓整手须 lots≥2（⌊lots/2⌋≥1）
        bars = bars_from_closes(0, [100.0] * 30)
        with self.assertRaises(ValueError):
            mk_engine(bars, tp_mode="structure", lots=1)

    def test_class12_trail_raise_only_upward(self):
        # 无前反向点 → 无主动止盈；新3买 @105 → 止损上移 105−滑点1=104；
        # 更低 4买 @98 不下移（水位推进）；末根 low ≤104 → trailStop @104
        closes = [100.0] * 5 + [110.0, 111.0, 104.0, 103.5]
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, tp_mode="structure", lots=4.0, stop_pts=10.0,
                      tp_trail_slip_pts=1.0)
        e._advance_cut(bars[-1]["time"] + SEC3)
        pt3 = {"type": "3买", "time": bars[6]["time"], "price": 105.0}
        pt4 = {"type": "4买", "time": bars[7]["time"], "price": 98.0}
        with patch.object(FxMaEngine, "_fx_prev_bi_end",
                          lambda self, P, pt_time, d: None):
            st = e._fx_init_state()
            stats = {"executed": 0, "suppressed": 0, "closed": 0, "stopped": 0}
            e._fx_fill_pending(st["trades"], [self._sig("3", "long")],
                               st["open_pos"], stats, 1000, 1000, 100.0)
            tr = st["trades"][0]
            self.assertIsNone(tr["tpRef"])
            self.assertIsNone(tr["tpLots"])
            self.assertAlmostEqual(tr["stopRef"], 90.0)
            with patch.object(FxMaEngine, "_fx_all_points",
                              lambda self, P: ([pt3], [])):
                e._fx_trail_raise(st["open_pos"], 1000)
            self.assertTrue(tr["trailRaised"])
            self.assertAlmostEqual(tr["stopRef"], 104.0)
            self.assertEqual(len(tr["trailMoves"]), 1)
            self.assertEqual(tr["trailMoves"][0]["pointType"], "3买")
            # 重复扫描水位去重 + 更低 4买 只推进水位不下移
            with patch.object(FxMaEngine, "_fx_all_points",
                              lambda self, P: ([pt3, pt4], [])):
                e._fx_trail_raise(st["open_pos"], 1010)
            self.assertAlmostEqual(tr["stopRef"], 104.0)
            self.assertEqual(len(tr["trailMoves"]), 1)
            tr["_evalCutFine"] = 6
            exits = e._fx_check_exits(st["open_pos"], stats)
            self.assertEqual(len(exits), 1)
            self.assertEqual(exits[0]["exitType"], "trailStop")
            self.assertAlmostEqual(exits[0]["exitPrice"], 104.0)
            self.assertAlmostEqual(exits[0]["pnl"], (104.0 - 100.0) * 2.0)

    def test_short_mirror_trail(self):
        # 空头镜像：3卖 @95 → 止损下移 95+1=96；high ≥96 → trailStop @96
        closes = [100.0] * 5 + [90.0, 89.0, 96.0, 96.5]
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, tp_mode="structure", lots=4.0, stop_pts=10.0,
                      tp_trail_slip_pts=1.0)
        e._advance_cut(bars[-1]["time"] + SEC3)
        pt3 = {"type": "3卖", "time": bars[6]["time"], "price": 95.0}
        with patch.object(FxMaEngine, "_fx_prev_bi_end",
                          lambda self, P, pt_time, d: None), \
             patch.object(FxMaEngine, "_fx_all_points",
                          lambda self, P: ([], [pt3])):
            st = e._fx_init_state()
            stats = {"executed": 0, "suppressed": 0, "closed": 0, "stopped": 0}
            e._fx_fill_pending(st["trades"], [self._sig("3", "short")],
                               st["open_pos"], stats, 1000, 1000, 100.0)
            e._fx_trail_raise(st["open_pos"], 1000)
            tr = st["trades"][0]
            self.assertAlmostEqual(tr["stopRef"], 96.0)
            tr["_evalCutFine"] = 6
            exits = e._fx_check_exits(st["open_pos"], stats)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["exitType"], "trailStop")
        self.assertAlmostEqual(exits[0]["exitPrice"], 96.0)
        self.assertAlmostEqual(exits[0]["pnl"], (96.0 - 100.0) * -1 * 2.0)

    def test_class3_active_tp_full_close_no_trail(self):
        # 3类入场：主动止盈=整笔（entryLots 全平）、不提损（3买点出现止损不动）
        closes = [100.0] * 5 + [120.0, 131.5, 104.0]
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, tp_mode="structure", lots=4.0, stop_pts=10.0)
        e._advance_cut(bars[-1]["time"] + SEC3)
        pt3 = {"type": "3买", "time": bars[5]["time"], "price": 120.0}
        with patch.object(FxMaEngine, "_fx_prev_bi_end",
                          lambda self, P, pt_time, d:
                          {"type": "前高", "time": 500, "price": 130.0}), \
             patch.object(FxMaEngine, "_fx_all_points",
                          lambda self, P: ([pt3], [])):
            st = e._fx_init_state()
            stats = {"executed": 0, "suppressed": 0, "closed": 0, "stopped": 0}
            e._fx_fill_pending(st["trades"], [self._sig("3", "long", pt_type="3买")],
                               st["open_pos"], stats, 1000, 1000, 100.0)
            tr = st["trades"][0]
            self.assertEqual(tr["tpLots"], 2.0)         # 3类=整笔全平
            e._fx_trail_raise(st["open_pos"], 1000)
            self.assertFalse(tr["trailRaised"])         # 3类不提损
            self.assertAlmostEqual(tr["stopRef"], 90.0)
            tr["_evalCutFine"] = 5
            exits = e._fx_check_exits(st["open_pos"], stats)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["exitType"], "activeTp")
        self.assertAlmostEqual(exits[0]["exitPrice"], 130.0)
        self.assertAlmostEqual(exits[0]["pnl"], (130.0 - 100.0) * 2.0)
        self.assertEqual(st["open_pos"]["long"], [])

    def test_losing_side_target_no_tp(self):
        # 前反向点在亏损侧（前卖点 95 < 进场 100）→ 不设主动止盈，仅止损
        closes = [100.0] * 5 + [120.0, 95.0, 89.0]
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, tp_mode="structure", lots=4.0, stop_pts=10.0)
        e._advance_cut(bars[-1]["time"] + SEC3)
        with patch.object(FxMaEngine, "_fx_prev_bi_end",
                          lambda self, P, pt_time, d:
                          {"type": "前高", "time": 500, "price": 95.0}):
            st = e._fx_init_state()
            e._fx_fill_pending(st["trades"], [self._sig("3", "long")],
                               st["open_pos"], {"executed": 0, "suppressed": 0}, 1000, 1000, 100.0)
        tr = st["trades"][0]
        self.assertIsNone(tr["tpRef"])
        self.assertIsNone(tr["tpLots"])
        self.assertAlmostEqual(tr["stopRef"], 90.0)

    def test_run_integration_structure_smoke(self):
        # 全链路：run + structure → 半仓成交；合成结构无前反向点/无3买 → 走固定止损
        closes = signal_bars()
        closes = closes + [closes[-1] - 8.0, closes[-1] - 8.0 - 16.0]
        e, result, _pt = RunIntegrationTests()._run_with_point(closes, tp_mode="structure")
        trades = result["trades"]
        self.assertGreaterEqual(len(trades), 1)
        self.assertTrue(all(t["lots"] == 2.0 for t in trades))
        self.assertTrue(all(t.get("tpMode") == "structure" for t in trades))
        for t in trades:
            if t.get("exitType"):
                self.assertIn(t["exitType"], ("stop", "trailStop", "activeTp"))


# ============================================================
# run / step_to 集成
# ============================================================

class RunIntegrationTests(unittest.TestCase):
    def _run_with_point(self, closes, pt_type="2买", **kw):
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, **kw)
        cut = len(closes) // 2  # 点位置：行情中段
        pt = {"type": pt_type, "time": bars[cut]["time"], "price": closes[cut]}
        with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
            # start_ts 前移（默认预热60根会吃掉全部短样本）：从第3根起交易
            result = e.run(start_ts=bars[2]["time"], log=None)
        return e, result, pt

    def test_full_lifecycle_signal_fill_stop(self):
        closes = signal_bars()
        # 信号在尾部齐备拍触发；随后追加急跌触发止损（多单 止损=进场−10）
        closes = closes + [closes[-1] - 8.0, closes[-1] - 8.0 - 16.0]
        e, result, pt = self._run_with_point(closes)
        trades = result["trades"]
        self.assertGreaterEqual(len(trades), 1)
        tr = trades[0]
        self.assertEqual(tr["direction"], "long")
        self.assertEqual(tr["strategyKey"], "fx2Buy")
        # 止损/止盈价位精确 = 进场 ∓ 10 / ± 30
        self.assertAlmostEqual(tr["stopRef"], tr["entryPrice"] - 10.0)
        self.assertAlmostEqual(tr["tpRef"], tr["entryPrice"] + 30.0)
        # 成交口径：信号拍 = 决策拍 = 下一根 '3' K线开盘时刻（当下确认、开盘成交同拍），
        # 成交价 = 该根K线开盘价
        sig_idx = next(i for i, b in enumerate(e.bars["3"]["_list"])
                       if b["time"] == tr["signalTime"])
        self.assertEqual(tr["entryTime"], tr["signalTime"])
        self.assertEqual(tr["entryPrice"], e.bars["3"]["_list"][sig_idx]["open"])
        # 出场口径：盘中触及 → 成交价 = 对应触发价（止损 stopRef / 止盈 tpRef）
        for t in trades:
            if t.get("exitType"):
                self.assertAlmostEqual(
                    t["exitPrice"], t["stopRef"] if t["exitType"] == "stop" else t["tpRef"])
        # 集合口径
        self.assertIn("signals", result)
        self.assertEqual(result["stats"]["executed"], len(
            [t for t in trades if t.get("entryTime") is not None]))

    def test_no_signal_when_never_ready(self):
        closes = [100.0 + (i % 3) for i in range(30)]  # 无趋势：均线分离不足
        e, result, _ = self._run_with_point(closes)
        self.assertEqual(result["stats"]["signals"], 0)
        self.assertEqual(result["trades"], [])

    def test_step_to_warmup_and_execute(self):
        closes = signal_bars()
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars)
        pt = {"type": "2买", "time": bars[len(closes) // 2]["time"],
              "price": closes[len(closes) // 2]}
        with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
            init = e.step_to(bars[-1]["time"])          # 预热：忽略历史信号
            self.assertEqual(init, [])
            out = e.step_to(bars[-1]["time"] + SEC3, execute=True)
        self.assertIsInstance(out, dict)
        self.assertIn("signals", out)
        self.assertIn("suppressed", out)

    def test_30s_engine_runs(self):
        closes = signal_bars()
        bars = bars_from_closes(0, closes, sec=30)
        e = FxMaEngine({"30S": bars}, entry_res="30S")
        self.assertEqual(e.fine_res, "30S")
        pt = {"type": "2买", "time": bars[len(closes) // 2]["time"],
              "price": closes[len(closes) // 2]}
        with patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (pt, None)):
            result = e.run(log=None)
        self.assertLessEqual(result["stats"]["signals"], 2)   # 每点一次
        self.assertIn("30S", result["periods"])

    def test_run_fxma_backtest_entry(self):
        closes = signal_bars()
        bars = bars_from_closes(0, closes)
        with patch.object(FxMaEngine, "_fx_latest_points",
                          lambda self, P: (None, None)):
            result = run_fxma_backtest({"3": bars}, entry_res="3")
        self.assertEqual(result["stats"]["signals"], 0)


# ============================================================
# pred2 预判点（2026-10-09：本级规则提前入列，默认关）
# ============================================================

def _bi(type_, st, sp, et, ep, **extra):
    """合成笔（结构视图形状：type/起终点 + 可选 _forming/enough）。"""
    return dict({"type": type_, "startTime": st, "startPrice": sp,
                 "endTime": et, "endPrice": ep}, **extra)


class _FakeJournal:
    """journal 桩：记录 reject 调用（state 静默）。"""
    enabled = True

    def __init__(self):
        self.rejects = []

    def reject(self, *a, **kw):
        self.rejects.append((a, kw))

    def state(self, *a, **kw):
        pass


class Pred2Tests(unittest.TestCase):
    """10-5 案例形状（价位原样）：前低 4166.015 → 顶 4227.53 → 下跌笔 D 破前低至
    4125.275 → 反弹。"""

    def setUp(self):
        closes = [100 + i for i in range(30)]
        self.engine = mk_engine(bars_from_closes(0, closes), pred2_on=True)
        self.t = lambda i: i * 900
        self._set_view()

    def _set_view(self, *tail):
        base = [
            _bi("down", self.t(0), 4170.0, self.t(1), 4166.015),   # D_prev（前低）
            _bi("up", self.t(1), 4166.015, self.t(2), 4227.53),
            _bi("down", self.t(2), 4227.53, self.t(3), 4125.275),  # D（破前低）
        ]
        self.engine._structure_bis = {"3": base + list(tail)}

    def test_default_off_returns_none(self):
        eng = mk_engine(bars_from_closes(0, [100 + i for i in range(30)]))
        eng._structure_bis = self.engine._structure_bis
        self.assertEqual(eng._fx_pred2_points("3"), (None, None))

    def test_forming_enough_drifting_anchor(self):
        # 相位1：反弹 forming 且 enough → 锚=当前极值；延伸 → 漂移上移
        self._set_view(_bi("up", self.t(3), 4125.275, self.t(4), 4149.9,
                           _forming=True, enough=True))
        buy, sell = self.engine._fx_pred2_points("3")
        self.assertIsNone(buy)
        self.assertEqual(sell, {"type": "2卖", "time": self.t(4), "price": 4149.9,
                                "_prov": True, "_segStart": self.t(2)})
        self._set_view(_bi("up", self.t(3), 4125.275, self.t(5), 4163.375,
                           _forming=True, enough=True))
        _, sell = self.engine._fx_pred2_points("3")
        self.assertEqual((sell["time"], sell["price"]), (self.t(5), 4163.375))

    def test_forming_not_enough_hidden(self):
        # 反弹未够笔（<5 合并块）→ 不可见（10-7 19:30 案例）
        self._set_view(_bi("up", self.t(3), 4125.275, self.t(4), 4149.9,
                           _forming=True, enough=False))
        self.assertEqual(self.engine._fx_pred2_points("3"), (None, None))

    def test_confirmed_up_phase2_fixed_anchor(self):
        # 相位2：反弹笔已确认 → 锚固定；其后 forming 下跌不改变锚（10-5 10:15 案例）
        self._set_view(_bi("up", self.t(3), 4125.275, self.t(4), 4163.375),
                       _bi("down", self.t(4), 4163.375, self.t(5), 4152.7,
                           _forming=True, enough=True))
        _, sell = self.engine._fx_pred2_points("3")
        self.assertEqual((sell["time"], sell["price"]), (self.t(4), 4163.375))

    def test_reclaim_kills_permanently(self):
        # 收复前低（≥4166.015）→ 一次性判死；此后再回落也不复活
        self._set_view(_bi("up", self.t(3), 4125.275, self.t(4), 4170.0),
                       _bi("down", self.t(4), 4170.0, self.t(5), 4150.0),
                       _bi("up", self.t(5), 4150.0, self.t(6), 4155.0))
        self.assertIsNone(self.engine._fx_pred2_points("3")[1])

    def test_no_break_no_context(self):
        # D 未破前低 → 无上下文
        self.engine._structure_bis = {"3": [
            _bi("down", self.t(0), 4170.0, self.t(1), 4166.015),
            _bi("up", self.t(1), 4166.015, self.t(2), 4227.53),
            _bi("down", self.t(2), 4227.53, self.t(3), 4170.0),   # 未破
        ]}
        self.assertIsNone(self.engine._fx_pred2_points("3")[1])

    def test_newer_down_resupersedes_context(self):
        # 更近确认下跌笔 D2 取代 D：D2 也破其前低（4124.75<4125.275），但其后
        # 反弹已收复 D2 前低（4133>4125.275）→ 判死（破位失败不预判）
        self._set_view(_bi("up", self.t(3), 4125.275, self.t(4), 4163.375),
                       _bi("down", self.t(4), 4163.375, self.t(5), 4124.75),
                       _bi("up", self.t(5), 4124.75, self.t(6), 4133.615,
                           _forming=True, enough=True))
        self.assertIsNone(self.engine._fx_pred2_points("3")[1])
        # 反弹仍在 D2 前低之下（未收复）→ 上下文换锚生效
        self._set_view(_bi("up", self.t(3), 4125.275, self.t(4), 4163.375),
                       _bi("down", self.t(4), 4163.375, self.t(5), 4124.75),
                       _bi("up", self.t(5), 4124.75, self.t(6), 4125.0,
                           _forming=True, enough=True))
        _, sell = self.engine._fx_pred2_points("3")
        self.assertEqual((sell["time"], sell["price"]), (self.t(6), 4125.0))

    def test_buy_side_symmetric(self):
        # 买入侧：上涨笔 U 突破前高（4227.53>4170）→ 回调确认笔终点 = 预判2买
        self.engine._structure_bis = {"3": [
            _bi("up", self.t(0), 4125.275, self.t(1), 4170.0),     # U_prev（前高）
            _bi("down", self.t(1), 4170.0, self.t(2), 4150.0),
            _bi("up", self.t(2), 4150.0, self.t(3), 4227.53),      # U（破前高）
            _bi("down", self.t(3), 4227.53, self.t(4), 4190.0),
        ]}
        buy, sell = self.engine._fx_pred2_points("3")
        self.assertIsNone(sell)
        self.assertEqual((buy["type"], buy["time"], buy["price"]),
                         ("2买", self.t(4), 4190.0))

    def test_merge_point_rules(self):
        real = {"type": "类2卖", "time": 100, "price": 1.0}
        prov = {"type": "2卖", "time": 200, "price": 2.0, "_prov": True}
        self.assertIs(FxMaEngine._fx_merge_point(real, None), real)
        self.assertIs(FxMaEngine._fx_merge_point(None, prov), prov)
        self.assertIs(FxMaEngine._fx_merge_point(real, prov), prov)        # 预判更新
        same = {"type": "2卖", "time": 100, "price": 1.0}
        self.assertIs(FxMaEngine._fx_merge_point(real, same), real)        # 同刻真点接管
        older = {"type": "2卖", "time": 50, "price": 1.0}
        self.assertIs(FxMaEngine._fx_merge_point(real, older), real)       # 真点更新

    def test_collect_prov_signal_and_dedup(self):
        # 全条件停用 + 预判2卖注入 → 当拍触发，pred2 标记齐备；再评不重复
        closes = signal_bars()
        bars = bars_from_closes(0, closes)
        eng = mk_engine(bars, pred2_on=True, strong_fx_on=False, ma_on=False,
                        ma_stand_on=False, point_valid_bars=0)
        for b in bars:
            eng._advance_cut(b["time"] + SEC3)
        prov = {"type": "2卖", "time": bars[len(bars) // 2]["time"],
                "price": closes[-1] + 3.0, "_prov": True, "_segStart": 0}
        t_end = bars[-1]["time"] + SEC3
        st = eng._fx_init_state()
        with patch.object(FxMaEngine, "_fx_sync_ma", lambda self, P: True), \
             patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (None, None)), \
             patch.object(FxMaEngine, "_fx_pred2_points", lambda self, P: (None, prov)):
            sigs = eng._fx_collect(st["allSignals"], st["stats"], st["fired"], t_end,
                                   pred2=st["pred2"])
            self.assertEqual(len(sigs), 1)
            s = sigs[0]
            self.assertEqual(s["strategyKey"], "fx2Sell")
            self.assertEqual(s["pointType"], "2卖")           # 原标签（兼容）
            self.assertTrue(s["pred2"])                       # 预判旗标
            self.assertIn("预判", s["strategyLabel"])
            self.assertTrue(s["signalNote"].startswith("预判｜"))
            self.assertEqual(st["pred2"]["3"]["sell"]["time"], prov["time"])
            # 再评：fired 键 (P,'2卖',time) 已落 → 不双发
            self.assertEqual(eng._fx_collect(st["allSignals"], st["stats"],
                                             st["fired"], t_end + SEC3,
                                             pred2=st["pred2"]), [])

    def test_prov_disappearance_logged_once(self):
        # 预判消失（收复/重算）→ fx_prov_invalidated 一次性落盘（按旧锚去重）
        closes = signal_bars()
        bars = bars_from_closes(0, closes)
        eng = mk_engine(bars, pred2_on=True, strong_fx_on=False, ma_on=False,
                        ma_stand_on=False)
        for b in bars:
            eng._advance_cut(b["time"] + SEC3)
        jr = _FakeJournal()
        eng._journal = jr
        prov = {"type": "2卖", "time": bars[len(bars) // 2]["time"],
                "price": closes[-1] + 3.0, "_prov": True, "_segStart": 0}
        t_end = bars[-1]["time"] + SEC3
        st = eng._fx_init_state()
        with patch.object(FxMaEngine, "_fx_sync_ma", lambda self, P: True), \
             patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (None, None)), \
             patch.object(FxMaEngine, "_fx_pred2_points", lambda self, P: (None, prov)):
            eng._fx_collect(st["allSignals"], st["stats"], st["fired"], t_end,
                            pred2=st["pred2"])
        with patch.object(FxMaEngine, "_fx_sync_ma", lambda self, P: True), \
             patch.object(FxMaEngine, "_fx_latest_points", lambda self, P: (None, None)), \
             patch.object(FxMaEngine, "_fx_pred2_points", lambda self, P: (None, None)):
            eng._fx_collect(st["allSignals"], st["stats"], st["fired"], t_end + SEC3,
                            pred2=st["pred2"])
            eng._fx_collect(st["allSignals"], st["stats"], st["fired"], t_end + 2 * SEC3,
                            pred2=st["pred2"])
        invalid = [r for r in jr.rejects if r[0][1] == "fx_prov_invalidated"]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0][0][3], prov["time"])       # segStart=旧锚时间
        self.assertEqual(invalid[0][1].get("ptType"), "2卖")

    def test_off_keeps_collect_signature_compatible(self):
        # pred2On 关 + 旧签名调用（不传 pred2）→ 行为不变（既有调用/测试兼容）
        closes = signal_bars()
        bars = bars_from_closes(0, closes)
        eng = mk_engine(bars)
        for b in bars:
            eng._advance_cut(b["time"] + SEC3)
        st = eng._fx_init_state()
        self.assertEqual(eng._fx_collect(st["allSignals"], st["stats"],
                                         st["fired"], bars[-1]["time"] + SEC3), [])


# ============================================================
# 增量分型批处理窗口（2026-10-10 吞笔修复：updateFractalsTail since= 参数）
# ============================================================

class IncrementalFractalWindowTests(unittest.TestCase):
    """fine=15m 驱动、3m 每拍 5 根——修复前 [old-2, n-2) 内部分型被永久跳过
    （10-5 早盘吞笔案例：上涨段不拆笔、顶背驰无参照段）。修复后增量分型须与
    findFractals 全量逐位一致（身份键；dict 上的 macdCross/macdRaw 注释键除外）。"""

    @staticmethod
    def _identity(fractals):
        return [(f["mergedIdx"], f["type"], f["high"], f["low"], f["time"])
                for f in fractals]

    def test_chunked_feed_matches_full_fractals(self):
        import random
        from py_chain.chan_core import findFractals
        rng = random.Random(20261010)
        # 3m 随机游走（方向感知影线），每 5 根聚成 1 根 15m
        n3 = 320
        closes = [4000.0]
        for _ in range(n3 - 1):
            closes.append(closes[-1] + rng.uniform(-3.0, 3.0))
        bars3 = bars_from_closes(0, closes)
        bars15 = []
        for i in range(0, n3, 5):
            seg = bars3[i:i + 5]
            bars15.append(bar(seg[0]["time"], seg[0]["open"],
                              max(b["high"] for b in seg), min(b["low"] for b in seg),
                              seg[-1]["close"]))
        eng = FxMaEngine({"15": bars15}, entry_res="15")
        # 引擎 periods 固定含 "3"：补 3m 数据源（构造期缺省为空列表；结构同
        # __init__ 归一化 {"_list","_times"}，_advance_cut 按 cut 差额取新K——
        # fine=15 时 3m 每拍并入 5 根，即吞笔 bug 的喂入形态）
        eng.bars["3"] = {"_list": bars3, "_times": [b["time"] for b in bars3]}
        eng._times["3"] = eng.bars["3"]["_times"]
        sec15 = 900
        for b in bars15:
            eng._advance_cut(b["time"] + sec15)
            self.assertEqual(self._identity(eng._fractals["3"]),
                             self._identity(findFractals(eng._merged["3"])),
                             msg=f"3m 分型增量≠全量 @拍 {b['time']}")
            self.assertEqual(self._identity(eng._fractals["15"]),
                             self._identity(findFractals(eng._merged["15"])))
        # 笔层不变量：增量 bis 与强制重同步（批量口径）一致（末笔延伸状态也重建）
        before = [(x["type"], x["startTime"], x["startPrice"],
                   x["endTime"], x["endPrice"]) for x in eng._bis["3"]]
        eng._resync_bis("3")
        after = [(x["type"], x["startTime"], x["startPrice"],
                  x["endTime"], x["endPrice"]) for x in eng._bis["3"]]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
