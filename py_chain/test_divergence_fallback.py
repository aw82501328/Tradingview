# -*- coding: utf-8 -*-
"""M1/M2 进场背驰扩展单测（SPEC_divergence_fallback）。

覆盖：
  - M1 下沉链回退：停止级（3m）动能加速不背驰、链上一级（15m）真背驰 →
    开关关 = 原行为（无候选）；开关开 = markRes=15 回退候选（fallback=True）；
    上级也不背驰 → 无候选；
  - M2 近等容差：未严格创新低但价差在容差带内，三判据 AND 成立 → nearEqual 候选；
    动能不全面衰竭（仅 OR 不足）→ 拒；
  - rearm：同向持仓终局后按 (periodX, strategyKey, markRes) 三元组重置段去重。
"""
import unittest

from py_chain.chan_core import CHAN_CFG
from py_chain.mark_entry import realtimeLowerDiverge, counterMoveQualifies
from py_chain.backtest import BacktestEngine


def bi(t0, p0, t1, p1, tp):
    return {"type": tp, "startTime": t0, "startPrice": p0, "endTime": t1,
            "endPrice": p1, "span": abs(p1 - p0), "rawCount": 10,
            "gapLocked": False, "macdCross": False}


def macd(ts, m, dif):
    return {"time": ts, "macd": m, "dif": dif}


def buildPeriodData(diverge15=True, f15End=90.0):
    """60m 末段下跌（600→1300）内：15m 五笔（展开 ≥3 下沉至 15），15m 末段内部
    3m 又 ≥3 笔（继续下沉至 3 = 停止级）。3m 末段创新低但动能加速（不背驰）；
    diverge15 控制 15m 末段相对其参照是否背驰。"""
    return {
        "60": {"bis": [bi(0, 80, 600, 110, "up"), bi(600, 110, 1300, 90, "down")],
               "macdArr": [], "atr": 20.0, "bars": [],
               "merged": [{"time":t,"_firstTime":t} for t in range(600,1301,100)]},
        "15": {"bis": [bi(600, 110, 700, 100, "down"), bi(700, 100, 800, 105, "up"),
                       bi(800, 110, 900, 98, "down"), bi(900, 98, 1000, 110, "up"),
                       bi(1000, 110, 1300, f15End, "down")],
               "macdArr": [macd(810, -8, -8), macd(850, -8, -8),           # 15m 参照段（800→900）：深绿
                           macd(1050, -0.5 if diverge15 else -8, -0.5 if diverge15 else -8),
                           macd(1200, -0.5 if diverge15 else -8, -0.5 if diverge15 else -8)],
               "atr": 10.0, "bars": [],
               "merged": [{"time":t,"_firstTime":t} for t in range(1000,1301,50)]},
        "3": {"bis": [bi(1000, 110, 1100, 100, "up"), bi(1100, 100, 1150, 95, "down"),
                      bi(1150, 95, 1200, 102, "up"), bi(1200, 102, 1250, 94, "down"),
                      bi(1250, 94, 1260, 100, "up"), bi(1260, 100, 1300, 93, "down")],
              "macdArr": [macd(1210, -1, -1), macd(1240, -1, -1),          # 3m 参照段：浅绿
                          macd(1270, -5, -5), macd(1290, -5, -5)],         # 3m 末段：深绿（加速）
              "atr": 5.0, "bars": [],
              "merged": [{"time":t,"_firstTime":t} for t in range(1260,1301,10)]},
    }


PERIOD_TIMES = {"15": [1000, 1050, 1100, 1150, 1200, 1250, 1300],
                "3": [1260, 1270, 1280, 1290, 1300]}


class TestSinkFallback(unittest.TestCase):

    def setUp(self):
        self.saved = {k: CHAN_CFG.get(k) for k in
                      ("sinkFallback", "sinkFallbackRearm", "nearEqualAtrK", "nearEqualPct",
                       "expectBiEnough", "divergeConfirm")}
        CHAN_CFG["sinkFallback"] = False
        CHAN_CFG["sinkFallbackRearm"] = False
        CHAN_CFG["nearEqualAtrK"] = 0.0
        CHAN_CFG["nearEqualPct"] = 0.0
        CHAN_CFG["expectBiEnough"] = False
        CHAN_CFG["divergeConfirm"] = False

    def tearDown(self):
        CHAN_CFG.update(self.saved)

    def test_switch_off_keeps_original(self):
        pd = buildPeriodData()
        out = realtimeLowerDiverge(pd, "60", "long", 1305, periodTimes=PERIOD_TIMES)
        self.assertEqual(out, [])  # 停止级 3m 加速不背驰 → 原行为无候选（15m 不评）

    def test_fallback_hits_upper_level(self):
        pd = buildPeriodData()
        CHAN_CFG["sinkFallback"] = True
        out = realtimeLowerDiverge(pd, "60", "long", 1305, periodTimes=PERIOD_TIMES)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["res"], "15")           # 回退命中 15m（严格低于检测周期 60）
        self.assertTrue(out[0]["fallback"])             # 非停止级命中标记
        self.assertEqual(out[0]["point"]["price"], 90.0)
        self.assertEqual(out[0]["segStart"], 1000)

    def test_fallback_upper_level_not_divergent(self):
        pd = buildPeriodData(diverge15=False)
        CHAN_CFG["sinkFallback"] = True
        out = realtimeLowerDiverge(pd, "60", "long", 1305, periodTimes=PERIOD_TIMES)
        self.assertEqual(out, [])  # 停止级与上级均无背驰 → 无候选

    def test_near_equal_band_requires_all_criteria(self):
        # 15m 末段 98.5 vs 参照 98（未严格创新低，价差 0.5 ∈ 容差带 max(0.3×10, 0.001×98)=3）
        pd = buildPeriodData(f15End=98.5)
        # 末段 MACD：三判据全面衰竭（浅绿 + DIF 抬高 + 峰值小）→ AND 过
        pd["15"]["macdArr"] = [macd(810, -8, -8), macd(850, -8, -8),
                               macd(1050, -0.5, -0.5), macd(1200, -0.5, -0.5)]
        CHAN_CFG["sinkFallback"] = True
        CHAN_CFG["nearEqualAtrK"] = 0.3
        CHAN_CFG["nearEqualPct"] = 0.001
        out = realtimeLowerDiverge(pd, "60", "long", 1305, periodTimes=PERIOD_TIMES)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["res"], "15")
        self.assertTrue(out[0]["nearEqual"])

    def test_near_equal_rejects_partial_weakness(self):
        # 近等带内但 DIF 更低（动能未全面衰竭，仅面积/峰值项过 OR）→ AND 拒
        pd = buildPeriodData(f15End=98.5)
        pd["15"]["macdArr"] = [macd(810, -8, -8), macd(850, -8, -8),
                               macd(1050, -0.5, -9.0), macd(1200, -0.5, -9.0)]
        CHAN_CFG["sinkFallback"] = True
        CHAN_CFG["nearEqualAtrK"] = 0.3
        CHAN_CFG["nearEqualPct"] = 0.001
        out = realtimeLowerDiverge(pd, "60", "long", 1305, periodTimes=PERIOD_TIMES)
        self.assertEqual(out, [])


class TestExpectBi(unittest.TestCase):
    """M4：检测周期预期够笔（固定口径）+ 背驰分型确认进场（页面可选）。"""

    def setUp(self):
        self.saved = {k: CHAN_CFG.get(k) for k in
                      ("sinkFallback", "expectBiEnough", "expectBiMinBars", "divergeConfirm")}
        CHAN_CFG["sinkFallback"] = True   # 预期模式下停止级 3m 失败需回退到 15
        CHAN_CFG["expectBiEnough"] = True
        CHAN_CFG["expectBiMinBars"] = 5
        CHAN_CFG["divergeConfirm"] = False

    def tearDown(self):
        CHAN_CFG.update(self.saved)

    def test_counter_move_threshold(self):
        times = [600, 700, 800, 900, 1000, 1100, 1200]
        last = bi(0, 80, 600, 110, "up")   # 末笔上涨，端点 600
        self.assertFalse(counterMoveQualifies(times, last, 900, merged=[{"time":t} for t in times[:4]]))  # (600,1000] = 4 根
        self.assertTrue(counterMoveQualifies(times, last, 1000, merged=[{"time":t} for t in times[:5]]))   # 5 根
        CHAN_CFG["expectBiEnough"] = False
        self.assertFalse(counterMoveQualifies(times, last, 1200))  # 开关关

    def test_sink_expect_mode(self):
        # 60m 末笔上涨（顶 600/110）+ 端点后 ≥5 根 + 其后 15m 五笔下行为 → 预期承载笔
        # （600→1300 虚拟段）下沉，3m 停止级加速不背驰 → 回退 15m 命中
        pd = buildPeriodData()
        pd["60"]["bis"] = [bi(0, 80, 600, 110, "up")]
        times = dict(PERIOD_TIMES)
        times["60"] = [0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1300]
        out = realtimeLowerDiverge(pd, "60", "long", 1305, periodTimes=times)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["res"], "15")
        self.assertTrue(out[0]["fallback"])
        # 开关关 → 末笔反向且无预期口径 → 无链（保持旧口径）
        CHAN_CFG["expectBiEnough"] = False
        self.assertEqual(realtimeLowerDiverge(pd, "60", "long", 1305, periodTimes=times), [])

    def test_sink_expect_mode_not_enough_bars(self):
        pd = buildPeriodData()
        pd["60"]["bis"] = [bi(0, 80, 600, 110, "up")]
        times = dict(PERIOD_TIMES)
        pd["60"]["merged"] = [{"time":t,"_firstTime":t} for t in [600,700,800]]
        times["60"] = [0, 300, 600, 700, 800]  # 端点 600 后仅 2 根
        self.assertEqual(realtimeLowerDiverge(pd, "60", "long", 800, periodTimes=times), [])

    def test_diverge_confirm_timing(self):
        pd = buildPeriodData()  # 60m 末笔 down（正常口径）：3m 停止级失败 → 回退 15 候选
        # 候选极值 = 15m F.endTime 1300（barSec 900）→ 右邻收盘确认时刻 = 1300 + 2×900 = 3100
        self.assertEqual(realtimeLowerDiverge(pd, "60", "long", 1305,
                                              periodTimes=PERIOD_TIMES, divergeConfirm=True), [])
        out = realtimeLowerDiverge(pd, "60", "long", 3100,
                                   periodTimes=PERIOD_TIMES, divergeConfirm=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["res"], "15")
        # divergeConfirm=None → 读 CHAN_CFG
        CHAN_CFG["divergeConfirm"] = True
        self.assertEqual(realtimeLowerDiverge(pd, "60", "long", 1305,
                                              periodTimes=PERIOD_TIMES), [])

    def test_engine_param_overrides_cfg(self):
        bars = {"3": [{"time": i * 180, "open": 100, "high": 101, "low": 99, "close": 100}
                      for i in range(6)]}
        eng = BacktestEngine(bars, periods=["3"], diverge_confirm=True)
        self.assertTrue(eng.diverge_confirm)
        self.assertFalse(BacktestEngine(bars, periods=["3"]).diverge_confirm)  # setUp 中 CHAN_CFG=False

    def test_expect_bi_param_overrides_cfg(self):
        # CHAN_CFG 开（默认）时显式 expectBiEnabled=False → 恢复旧口径（末笔反向 → 无链）
        pd = buildPeriodData()
        pd["60"]["bis"] = [bi(0, 80, 600, 110, "up")]
        times = dict(PERIOD_TIMES)
        times["60"] = [0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1300]
        CHAN_CFG["expectBiEnough"] = True
        self.assertEqual(realtimeLowerDiverge(pd, "60", "long", 1305, periodTimes=times,
                                              expectBiEnabled=False), [])
        # counterMoveQualifies 显式 enabled 覆盖 CHAN_CFG
        t2 = [600, 700, 800, 900, 1000, 1100, 1200]
        last = bi(0, 80, 600, 110, "up")
        self.assertFalse(counterMoveQualifies(t2, last, 1200, enabled=False))
        self.assertTrue(counterMoveQualifies(t2, last, 1200, enabled=True, merged=[{"time":t} for t in t2]))
        # 引擎 expect_bi 参数覆盖 CHAN_CFG
        bars = {"3": [{"time": i * 180, "open": 100, "high": 101, "low": 99, "close": 100}
                      for i in range(6)]}
        self.assertFalse(BacktestEngine(bars, periods=["3"], expect_bi=False).expect_bi)
        self.assertTrue(BacktestEngine(bars, periods=["3"]).expect_bi)  # setUp 中 CHAN_CFG=True


class TestRearm(unittest.TestCase):

    def _engine(self):
        bars = {"3": [{"time": i * 180, "open": 100, "high": 101, "low": 99, "close": 100}
                      for i in range(6)]}
        return BacktestEngine(bars, periods=["3"])

    def test_rearm_resets_matching_triple_only(self):
        CHAN_CFG["sinkFallbackRearm"] = True
        eng = self._engine()
        eng._rt_fired = {("60", "waitBuy", "15", 1000), ("60", "waitBuy", "15", 2000),
                         ("60", "waitBuy", "3", 3000), ("15", "waitSell", "3", 4000)}
        eng._rearm_fired({"periodX": "60", "strategyKey": "waitBuy", "markRes": "15"})
        self.assertEqual(eng._rt_fired,
                         {("60", "waitBuy", "3", 3000), ("15", "waitSell", "3", 4000)})
        CHAN_CFG["sinkFallbackRearm"] = False

    def test_rearm_disabled_keeps_fired(self):
        CHAN_CFG["sinkFallbackRearm"] = False
        eng = self._engine()
        fired = {("60", "waitBuy", "15", 1000)}
        eng._rt_fired = set(fired)
        eng._rearm_fired({"periodX": "60", "strategyKey": "waitBuy", "markRes": "15"})
        self.assertEqual(eng._rt_fired, fired)


if __name__ == "__main__":
    unittest.main()
