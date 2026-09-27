# -*- coding: utf-8 -*-
"""三处规则修复 + 跨级下沉的开关语义单测（2026-09-26；锚点案例=07-07 06:30 逐拍调查）。

覆盖：
- A anchorUndecidedSkip / anchorUndecidedMinBars：classifySecond「未定型」哨兵、
  predictPlan 跳过未定型锚点回退前锚、真弱（after 存在未破左低）保留翻多；
- B divergeReferByZs：pickDivergeRefer 中枢内部段不参与比较、参照=入中枢段；
- C pointEnoughForming：形成中段 enough 计数到极值块；synthIntrabarBars 的
  provisional 累加器与 append 一致性；
- D sinkSkipLevel：下沉链方向不符/含糊区/展开不足时跳级。
全部开关默认关=原行为（各用例先断言关侧行为再开侧）。
"""

import unittest

from py_chain import chan_core
from py_chain.chan_core import (
    CHAN_CFG, MacdAccumulator, AtrAccumulator, buildStructureContext,
)
from py_chain import trading_plan as TP
from py_chain import mark_entry as ME


def _bi(typ, t0, t1, p0, p1, **kw):
    b = {"type": typ, "startTime": t0, "endTime": t1, "startPrice": p0, "endPrice": p1,
         "span": abs(p1 - p0)}
    b.update(kw)
    return b


class CfgSwitch:
    """用例内临时开关注入（结束恢复默认）。"""

    def __init__(self, **kw):
        self.kw = kw
        self._saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self._saved[k] = CHAN_CFG.get(k)
            CHAN_CFG[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            if v is None:
                CHAN_CFG.pop(k, None)
            else:
                CHAN_CFG[k] = v


class AnchorUndecidedTests(unittest.TestCase):
    """A：未定型不接管。"""

    def setUp(self):
        # 结构：down(前底) -> up(形成中，2卖挂在端点) —— 点后无任何下跌笔
        self.bis = [
            _bi("down", 0, 50, 10.0, 8.0),
            _bi("up", 50, 100, 8.0, 12.0, _forming=True, phase="running", mergedCount=6),
        ]
        self.p2sell = {"type": "2卖", "time": 100, "price": 12.0}

    def test_off_keeps_other(self):
        self.assertEqual(TP.classifySecond(self.bis, [], self.p2sell), "其他")

    def test_on_after_missing_marks_undecided(self):
        with CfgSwitch(anchorUndecidedSkip=True):
            self.assertEqual(TP.classifySecond(self.bis, [], self.p2sell), "未定型")

    def test_on_forming_after_below_minbars(self):
        bis = self.bis + [_bi("down", 100, 130, 12.0, 11.0, _forming=True,
                              phase="expected", mergedCount=1)]
        with CfgSwitch(anchorUndecidedSkip=True, anchorUndecidedMinBars=2):
            self.assertEqual(TP.classifySecond(bis, [], self.p2sell), "未定型")

    def test_true_weak_keeps_flip(self):
        # after 存在（形成中、达 2 块）且未破左低 8.0 → 真弱仍走「其他」→ 弱档翻多语义不变
        bis = self.bis + [_bi("down", 100, 160, 12.0, 11.0, _forming=True,
                              phase="running", mergedCount=2)]
        with CfgSwitch(anchorUndecidedSkip=True, anchorUndecidedMinBars=2):
            self.assertEqual(TP.classifySecond(bis, [], self.p2sell), "前低附近")

    def test_predict_plan_falls_back_to_prev_anchor(self):
        # 端点 100 上挂未定型 2卖；前一笔端点 50 上挂 1买 → 开关开应锚定 1卖（wait2Buy 语义）
        bis = [
            _bi("up", -100, 0, 8.5, 10.0),
            _bi("down", 0, 50, 10.0, 8.0),
            _bi("up", 50, 100, 8.0, 12.0, _forming=True, phase="running", mergedCount=6),
        ]
        buy_pts = [{"type": "1买", "time": 50, "price": 8.0}]
        sell_pts = [{"type": "2卖", "time": 100, "price": 12.0}]
        orig_buy = ME.findBuyPoints if hasattr(ME, "findBuyPoints") else None
        import py_chain.chan_core as cc
        saved = (cc.findBuyPoints, cc.findSellPoints)
        cc.findBuyPoints = lambda *a, **k: buy_pts
        cc.findSellPoints = lambda *a, **k: sell_pts
        try:
            TP.findBuyPoints = cc.findBuyPoints
            TP.findSellPoints = cc.findSellPoints
            with CfgSwitch(anchorUndecidedSkip=True, anchorUndecidedMinBars=2):
                out = TP.predictPlan(res="60", bis=bis, upperBis=[], macdArr=[],
                                     lastPrice=11.0, bars=[], atr=2.0, barSec=3600)
            self.assertIn("等待回调后做2买", out["strategy"])
            self.assertIn("1买", out["pointDesc"])
        finally:
            cc.findBuyPoints, cc.findSellPoints = saved
            TP.findBuyPoints, TP.findSellPoints = saved


class DivergeReferZsTests(unittest.TestCase):
    """B：背驰参照=入中枢段，中枢内部段不参与比较。"""

    def setUp(self):
        # 3m 型结构：入中枢段(0→30, 高 12) → 中枢三笔(30→90, [10.5,11.5]) → 出中枢段 F(90→120, 高 12.3)
        self.bis = [
            _bi("up", 0, 30, 9.0, 12.0),
            _bi("down", 30, 50, 12.0, 10.5),
            _bi("up", 50, 70, 10.5, 11.5),
            _bi("down", 70, 90, 11.5, 10.6),
            _bi("up", 90, 120, 10.6, 12.3),
        ]
        self.F = self.bis[-1]
        self.barSec = 180

    def test_off_takes_nearest(self):
        ref = ME.pickDivergeRefer(self.bis, self.F, self.barSec)
        self.assertEqual(ref["startTime"], 50)  # 紧邻前一同向笔（中枢内部 50→70）

    def test_on_takes_entering_stroke(self):
        with CfgSwitch(divergeReferByZs=True):
            ref = ME.pickDivergeRefer(self.bis, self.F, self.barSec)
        self.assertIsNotNone(ref)
        self.assertEqual(ref["startTime"], 0)   # 入中枢段（结束于中枢起点 30/enterEndTime）

    def test_on_even_zs_without_same_dir_entering(self):
        # 偶数笔中枢（b1 反向）：无同向入中枢段 → 无参照（也不回退中枢内部段）
        bis = [
            _bi("down", 0, 30, 12.0, 9.0),
            _bi("up", 30, 60, 9.0, 11.0),
            _bi("down", 60, 90, 11.0, 8.0),
            _bi("up", 90, 120, 8.0, 11.5),
        ]
        with CfgSwitch(divergeReferByZs=True):
            ref = ME.pickDivergeRefer(bis, bis[-1], self.barSec)
        self.assertIsNone(ref)


class PointEnoughFormingTests(unittest.TestCase):
    """C-2：够笔只计成笔可能（数到极值块）。"""

    BARS = [
        {"time": 0, "open": 9.5, "high": 10.1, "low": 9.2, "close": 9.9},   # 上涨（左邻块）
        {"time": 60, "open": 9.9, "high": 10.5, "low": 9.9, "close": 10.2},  # 顶块（up 笔端点 10.5）
        {"time": 120, "open": 10.2, "high": 10.2, "low": 9.5, "close": 9.7},
        {"time": 180, "open": 9.7, "high": 10.0, "low": 9.3, "close": 9.5},
        {"time": 240, "open": 9.5, "high": 9.8, "low": 9.1, "close": 9.3},   # 极值块（低 9.1）
        {"time": 300, "open": 9.3, "high": 10.0, "low": 9.4, "close": 9.8},  # 抬低点（反向确认）
        {"time": 360, "open": 9.8, "high": 10.3, "low": 9.7, "close": 10.1},
    ]

    def _ctx(self):
        # 已确认 up 笔（端点=第2块顶 10.5@60），其后为下跌形成段
        known = [_bi("up", 0, 60, 9.0, 10.5)]
        return buildStructureContext(known, self.BARS, 60, tCut=420)

    def test_off_counts_to_now(self):
        ctx = self._ctx()
        cur = ctx["bis"][-1]
        self.assertEqual(cur.get("type"), "down")     # 预期下跌段已生成
        self.assertEqual(cur.get("mergedCount"), 6)   # 端点块(60)→当下共 6 块（含抬低点后 2 块）
        self.assertTrue(cur.get("enough"))
        self.assertEqual(cur.get("phase"), "running")

    def test_on_counts_to_extreme(self):
        with CfgSwitch(pointEnoughForming=True):
            ctx = self._ctx()
        cur = ctx["bis"][-1]
        # 极值块=第5块（索引4，低 9.1）——抬低点的第6、7块不计入 → 4 块 <5 → 不够笔
        self.assertFalse(cur.get("enough"))
        self.assertEqual(cur.get("phase"), "expected")


class ProvisionalAccumulatorTests(unittest.TestCase):
    """C-1：盘中合成K的 provisional 与收盘后 append 完全一致。"""

    BARS = [
        {"time": 0, "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2},
        {"time": 60, "open": 10.2, "high": 10.8, "low": 10.0, "close": 10.6},
        {"time": 120, "open": 10.6, "high": 11.0, "low": 10.3, "close": 10.4},
    ]

    def test_macd_provisional_matches_append(self):
        acc = MacdAccumulator()
        acc.append(self.BARS[0])
        acc.append(self.BARS[1])
        prov = acc.provisional(self.BARS[2])
        acc.append(self.BARS[2])
        real = acc.entries[-1]
        for k in ("macd", "dif", "dea"):
            self.assertAlmostEqual(prov[k], real[k], places=10)
        self.assertEqual(prov["time"], real["time"])

    def test_macd_provisional_does_not_mutate(self):
        acc = MacdAccumulator()
        acc.append(self.BARS[0])
        n0 = len(acc.entries)
        acc.provisional(self.BARS[1])
        self.assertEqual(len(acc.entries), n0)

    def test_atr_provisional_matches_append(self):
        acc = AtrAccumulator(2)
        acc.append(self.BARS[0])
        acc.append(self.BARS[1])
        prov = acc.provisional(self.BARS[2])
        acc.append(self.BARS[2])
        self.assertAlmostEqual(prov, acc.value, places=10)


class SinkSkipLevelTests(unittest.TestCase):
    """D：下沉链跳级（方向不符/展开不足）。"""

    def _pd(self):
        # 60 末段 up；15 末段 down（方向不符）；3 末段 up 且 60 段内 ≥3 笔
        bis60 = [
            _bi("down", 0, 60, 12.0, 9.0),
            _bi("up", 60, 400, 9.0, 11.8, _forming=True, phase="running"),
        ]
        bis15 = [
            _bi("up", 60, 150, 9.0, 10.8),
            _bi("down", 150, 200, 10.8, 10.0),
            _bi("up", 200, 260, 10.0, 11.0),
            _bi("down", 260, 300, 11.0, 10.2, _forming=True, phase="running"),
        ]
        bis3 = [
            _bi("down", 60, 120, 10.8, 9.8),
            _bi("up", 120, 200, 9.8, 11.0),
            _bi("down", 200, 260, 11.0, 10.0),
            _bi("up", 260, 400, 10.0, 11.8, _forming=True, phase="running"),
        ]
        merged60 = [{"time": t} for t in (60, 120, 180, 240, 300, 360)]  # 形成段够笔（≥5 块）
        return {"60": {"bis": bis60, "merged": merged60},
                "15": {"bis": bis15}, "3": {"bis": bis3}}

    def test_off_breaks_at_mismatch(self):
        nodes = ME._sinkChainRealtimeNodes(self._pd(), "60", "short")
        self.assertEqual([n["res"] for n in nodes], ["60"])

    def test_on_skips_to_3(self):
        with CfgSwitch(sinkSkipLevel=True):
            nodes = ME._sinkChainRealtimeNodes(self._pd(), "60", "short")
        self.assertEqual([n["res"] for n in nodes], ["60", "3"])


if __name__ == "__main__":
    unittest.main()
