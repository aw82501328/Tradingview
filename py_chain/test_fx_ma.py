# -*- coding: utf-8 -*-
"""强分型均线V1（fxma_v1）策略引擎单测。

覆盖：
  - 组件：MaAcc（SMA/EMA 增量与就绪）、strong_fx_after（实体口径 + minPts 落差）、
    parse_multi / fx_strategy_key / 参数中心 fxma 模块（multi 校验、默认值、kwargs 映射）
  - 注册表与引擎分发（module_registry fxma_v1 / engine_dispatch / fxma_load_periods）
  - 信号评估（_fx_collect，猴子补丁 _fx_latest_points 注入合成买卖点）：
    点→强分型→均线分离齐备才触发 / 每点一次 / crossMinPts 间距不足不出 /
    pointValidBars 超时作废 / 反向点作废 / 类别过滤 / SMA 与 EMA 两口径
  - 出场状态机：止损/止盈触及（下一开盘成交）、同根双触 stop 优先与 tp 口径、
    进场K线不判出场（_evalCut 边界）
  - 互斥：global 同向一笔 + 同拍共振取大周期；perPeriod 每周期独立
  - run()/step_to() 集成：合成K线 + 注入点 → 信号→成交→出场全链路（含 30S 周期）

运行：python -m unittest py_chain.test_fx_ma -v
"""

import unittest
from unittest.mock import patch

from py_chain import param_center, module_registry
from py_chain.engine_dispatch import engine_class_of, fxma_load_periods
from py_chain.fx_ma import (
    FxMaEngine, MaAcc, parse_multi, fx_strategy_key, strong_fx_after,
    FXMA_DEFAULTS, run_fxma_backtest,
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
    kw.setdefault("point_classes", "1,2,3")
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


class ParamCenterFxmaTests(unittest.TestCase):
    def test_defaults_and_schema(self):
        d = param_center.defaults_of("fxma")
        self.assertEqual(d, FXMA_DEFAULTS)
        sch = param_center.schema_of("fxma")
        self.assertEqual(sch["entryRes"]["type"], "multi")
        self.assertEqual(sch["entryRes"]["choices"], ["30S", "3", "15", "60"])
        self.assertEqual(sch["maType"]["type"], "str")
        self.assertEqual(sch["maType"]["choices"], ["SMA", "EMA"])

    def test_normalize_multi_and_ranges(self):
        self.assertEqual(param_center.normalize("fxma", {"entryRes": "60,3,60"})["entryRes"], "60,3")
        with self.assertRaises(ValueError):
            param_center.normalize("fxma", {"pointClasses": "1,4"})
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
        # 同一点再评：每点只触发一次（fired 已落键）
        self.assertEqual(self.engine._fx_collect(st["allSignals"], st["stats"],
                                                 st["fired"], self.t_end + SEC3), [])

    def test_cross_min_pts_blocks_insufficient_gap(self):
        # 间距阈值拉到不可能的大值 → 不出信号（点未作废，仅条件不满足）
        self.engine.cross_min_pts = 1e6
        sigs, _ = self._collect_with(self.pt)
        self.assertEqual(sigs, [])

    def test_point_valid_bars_expiry(self):
        # 点有效期 1 根：点后已走多根 → 作废不出信号
        self.engine.point_valid_bars = 1
        sigs, _ = self._collect_with(self.pt)
        self.assertEqual(sigs, [])

    def test_opposite_point_invalidates(self):
        # 反向（卖点）出现在点之后 → 点作废
        opp = {"type": "1卖", "time": self.pt_time + 5 * SEC3, "price": 120.0}
        e, pt = self.engine, self.pt
        st = e._fx_init_state()
        with patch.object(FxMaEngine, "_fx_latest_points",
                          lambda self, P: (pt, opp)):
            self.assertEqual(e._fx_collect(st["allSignals"], st["stats"], st["fired"], self.t_end), [])

    def test_class_filter(self):
        self.engine.point_classes = {3}  # 只交易3类
        sigs, _ = self._collect_with(self.pt)
        self.assertEqual(sigs, [])

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
        "state": "open", "exits": [], "_evalCut": cut, **extra,
    }


class ExitTests(unittest.TestCase):
    def _engine_with_tail(self, closes):
        bars = bars_from_closes(0, closes)
        e = mk_engine(bars, stop_pts=10.0, tp_pts=30.0)
        e._advance_cut(bars[-1]["time"] + SEC3)
        return e, bars

    def test_long_stop_hit_fills_next_open(self):
        # 多单 100：止损 90 / 止盈 130；随后一根 long 下破 90 → 挂起，下一开盘成交
        closes = [100.0] * 5 + [95.0, 89.0]      # 第7根 low<90 触发止损
        e, bars = self._engine_with_tail(closes)
        pos = mk_pos("long", 100.0, cut=len(closes) - 1)
        st = e._fx_init_state()
        st["open_pos"]["long"] = pos
        e._fx_check_exits(st["open_pos"])
        self.assertEqual(pos["pendingExit"], "stop")
        # 下一拍：新 bar（回补一根）开盘价成交
        nxt = bar(bars[-1]["time"] + SEC3, 88.0, 89.0, 87.0, 88.5)
        e.bars["3"]["_list"].append(nxt)
        e._times["3"].append(nxt["time"])
        out = {"signals": [], "fills": [], "exits": [], "suppressed": []}
        e._advance_cut(nxt["time"] + SEC3)
        e._fx_tick(st, nxt["time"] + SEC3, nxt, out)
        self.assertEqual(len(out["exits"]), 1)
        tr = out["exits"][0]
        self.assertEqual(tr["exitType"], "stop")
        self.assertEqual(tr["exitPrice"], nxt["open"])      # 下一根开盘成交
        self.assertAlmostEqual(tr["pnl"], (88.0 - 100.0) * 1 * 1.0)
        self.assertEqual(tr["state"], "closed")
        self.assertIsNone(st["open_pos"]["long"])           # 互斥解锁

    def test_long_take_profit(self):
        closes = [100.0] * 5 + [120.0, 131.5]   # 第7根 high>130 触发止盈
        e, bars = self._engine_with_tail(closes)
        pos = mk_pos("long", 100.0, cut=len(closes) - 1)
        st = e._fx_init_state()
        st["open_pos"]["long"] = pos
        e._fx_check_exits(st["open_pos"])
        self.assertEqual(pos["pendingExit"], "takeProfit")

    def test_same_bar_both_priority(self):
        # 单根大振幅K同时触及止损(90下方)与止盈(130上方)：默认 stop；sameBarPriority=tp → takeProfit
        # （bars_from_closes 方向影线构造不出双触形态，手工造最后一根：开100 高132 低88 收99）
        head = bars_from_closes(0, [100.0] * 5)
        wild = bar(head[-1]["time"] + SEC3, 100.0, 132.0, 88.0, 99.0)
        def mk_bars():
            return head + [dict(wild)]
        def mk_pending(priority):
            e = mk_engine(mk_bars(), same_bar_priority=priority)
            e._advance_cut(wild["time"] + SEC3)
            pos = mk_pos("long", 100.0, cut=len(head))  # 持仓已评到 wild 之前
            st = e._fx_init_state()
            st["open_pos"]["long"] = pos
            e._fx_check_exits(st["open_pos"])
            return pos
        self.assertEqual(mk_pending("stop")["pendingExit"], "stop")
        self.assertEqual(mk_pending("tp")["pendingExit"], "takeProfit")

    def test_entry_bar_not_evaluated(self):
        # _evalCut=进场时 cut：进场那根（历史）不参与出场判定
        closes = [100.0] * 3 + [85.0]           # 末根（=进场前一根）下破止损
        e, _ = self._engine_with_tail(closes)
        pos = mk_pos("long", 100.0, cut=len(closes))   # 进场时已含末根
        st = e._fx_init_state()
        st["open_pos"]["long"] = pos
        e._fx_check_exits(st["open_pos"])
        self.assertIsNone(pos.get("pendingExit"))

    def test_short_side_mirror(self):
        closes = [100.0] * 5 + [105.0, 111.0]   # 空单 100：止损 110 盘中击穿
        e, _ = self._engine_with_tail(closes)
        pos = mk_pos("short", 100.0, cut=len(closes) - 1)
        st = e._fx_init_state()
        st["open_pos"]["short"] = pos
        e._fx_check_exits(st["open_pos"])
        self.assertEqual(pos["pendingExit"], "stop")


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
        st["open_pos"]["long"] = mk_pos("long", 100.0)  # 已有多单
        out = []
        stats = {"executed": 0, "suppressed": 0}
        e._fx_fill_pending(st["trades"], [self._sig("3", "long"), self._sig("15", "long")],
                           st["open_pos"], stats, 1000, 1000, 100.0, sup_out=out)
        self.assertEqual(stats["suppressed"], 2)
        self.assertEqual(len(st["trades"]), 0)
        # 无持仓：同拍 3m+15m 同向共振 → 大周期 15 成交，3m 被过滤
        st["open_pos"]["long"] = None
        e._fx_fill_pending(st["trades"], [self._sig("3", "long"), self._sig("15", "long")],
                           st["open_pos"], stats, 1000, 1000, 100.0, sup_out=out)
        self.assertEqual(len(st["trades"]), 1)
        self.assertEqual(st["trades"][0]["periodX"], "15")
        self.assertEqual(stats["executed"], 1)

    def test_per_period_mutex_allows_each_period(self):
        e = mk_engine(bars_from_closes(0, [100.0] * 30), entry_res="3,15",
                      mutex_scope="perPeriod")
        st = e._fx_init_state()
        st["open_pos"]["3"]["long"] = mk_pos("long", 100.0)  # 3m 已有多单
        stats = {"executed": 0, "suppressed": 0}
        e._fx_fill_pending(st["trades"], [self._sig("3", "long")],
                           st["open_pos"], stats, 1000, 1000, 100.0)
        e._fx_fill_pending(st["trades"], [self._sig("15", "long")],
                           st["open_pos"], stats, 1000, 1000, 100.0)
        self.assertEqual(len(st["trades"]), 1)      # 15m 独立成交
        self.assertEqual(st["trades"][0]["periodX"], "15")
        self.assertEqual(stats["suppressed"], 1)    # 3m 被本周期互斥


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


if __name__ == "__main__":
    unittest.main()
