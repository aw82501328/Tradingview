# -*- coding: utf-8 -*-
"""双周期链单测（2026-10-23）：链推导（lowerResOf 数据驱动/levelsBelow/
filterDetectPeriods/detectPeriodsOf 勾选口径）、sr 级别扩展、mt5 重采样 1/5/30、
SignalLog 链行键隔离。引擎级 A/B 对拍（旧口径 vs 链口径逐位一致）见
_tmp_chain_smoke.py（真实数据手动跑，不进套件）。"""
import unittest

from . import chan_core
from .chan_core import intervalSecOf, lowerResOf
from .mark_entry import detectPeriodsOf, filterDetectPeriods, levelsBelow
from .mt5_feed import resample
from .sr_flip import LEVEL_ORDER, UPPER_OF, minTouchFor, periodNameOf
from .sr_service import CANONICAL_LEVELS, normalize_periods


CHAIN_A = ["D", "240", "60", "15", "3"]
CHAIN_A_30S = ["D", "240", "60", "15", "3", "30S"]
CHAIN_B = ["D", "240", "30", "5", "1"]


class ChainMappingTests(unittest.TestCase):
    """lowerResOf 数据驱动：链A 完整集=旧映射逐位一致；链B 按链推导；旧口径零变化。"""

    def test_interval_sec_supports_1m(self):
        self.assertEqual(intervalSecOf("1"), 60)
        self.assertEqual(intervalSecOf(1), 60)     # int 入参按周期码
        self.assertEqual(intervalSecOf("5"), 300)
        self.assertEqual(intervalSecOf("30"), 1800)
        self.assertEqual(intervalSecOf("30S"), 30)

    def test_lower_res_chain_a_equals_legacy(self):
        for res, want in (("D", "240"), ("240", "60"), ("60", "15"), ("15", "3")):
            self.assertEqual(lowerResOf(res, CHAIN_A), want)
            self.assertEqual(lowerResOf(res, CHAIN_A_30S), want)
        self.assertIsNone(lowerResOf("3", CHAIN_A))          # 30S 未加载
        self.assertEqual(lowerResOf("3", CHAIN_A_30S), "30S")  # 30S 加载 → 3→30S
        self.assertIsNone(lowerResOf("30S", CHAIN_A_30S))

    def test_lower_res_chain_b(self):
        self.assertEqual(lowerResOf("D", CHAIN_B), "240")
        self.assertEqual(lowerResOf("240", CHAIN_B), "30")   # 链B：240 的下级是 30
        self.assertEqual(lowerResOf("30", CHAIN_B), "5")
        self.assertEqual(lowerResOf("5", CHAIN_B), "1")
        self.assertIsNone(lowerResOf("1", CHAIN_B))

    def test_lower_res_legacy_no_loaded_unchanged(self):
        # 旧固定映射（loaded=None）：与历史行为逐位一致
        for res, want in (("D", "240"), ("240", "60"), ("60", "15"), ("15", "3")):
            self.assertEqual(lowerResOf(res), want)
        self.assertIsNone(lowerResOf("3"))
        self.assertIsNone(lowerResOf("30"))
        self.assertIsNone(lowerResOf("1"))

    def test_levels_below_chain_b(self):
        pd = {r: {"bis": [{"x": i} for i in range(5)]} for r in CHAIN_B}
        self.assertEqual(levelsBelow(pd, "240", CHAIN_B), ["30", "5", "1"])
        self.assertEqual(levelsBelow(pd, "30", CHAIN_B), ["5", "1"])
        self.assertEqual(levelsBelow(pd, "1", CHAIN_B), [])
        # 数据截断（5 无笔数据）链止于 5——区间套不可跳级
        pd2 = dict(pd, **{"5": {"bis": []}})
        self.assertEqual(levelsBelow(pd2, "240", CHAIN_B), ["30"])

    def test_filter_and_detect_periods(self):
        # 链A 默认（无 30S）：检测=240/60/15（3 无下级）
        self.assertEqual(filterDetectPeriods(CHAIN_A, CHAIN_A), ["240", "60", "15"])
        self.assertEqual(filterDetectPeriods(CHAIN_A_30S, CHAIN_A_30S),
                         ["240", "60", "15", "3"])
        # 链B：240/30/5 为检测周期、1 不作检测（无下级）
        self.assertEqual(filterDetectPeriods(CHAIN_B, CHAIN_B), ["240", "30", "5"])
        # 勾选口径：240 不勾 → 检测剔 240（开单周期=60/15 之类）
        self.assertEqual(detectPeriodsOf(CHAIN_A, ["60", "15", "3"], CHAIN_A),
                         ["60", "15"])
        self.assertEqual(detectPeriodsOf(CHAIN_A_30S, ["60", "15", "3", "30S"],
                                         CHAIN_A_30S), ["60", "15", "3"])
        self.assertEqual(detectPeriodsOf(CHAIN_B, ["1", "5", "30"], CHAIN_B), ["30", "5"])
        # open_res=None=旧口径不过滤
        self.assertEqual(detectPeriodsOf(CHAIN_B, None, CHAIN_B), ["240", "30", "5"])


class SrLevelTests(unittest.TestCase):
    """sr 级别扩展：30/5 入排序与白名单；UPPER_OF 两链共存。"""

    def test_level_order_and_canonical(self):
        self.assertLess(LEVEL_ORDER.index("30"), LEVEL_ORDER.index("15"))
        self.assertLess(LEVEL_ORDER.index("15"), LEVEL_ORDER.index("5"))
        self.assertLess(LEVEL_ORDER.index("5"), LEVEL_ORDER.index("3"))
        self.assertIn("30", CANONICAL_LEVELS)
        self.assertIn("5", CANONICAL_LEVELS)
        # 链B 级别集规范化：大→小排序
        self.assertEqual(normalize_periods(["5", "30", "240", "D"]), ["D", "240", "30", "5"])

    def test_level_1_not_canonical(self):
        # 1 分钟不是支阻级别（不作检测周期、无支阻闸门需求）
        with self.assertRaises(ValueError):
            normalize_periods(["1"])

    def test_upper_of_both_chains(self):
        self.assertEqual(UPPER_OF["240"], "D")   # 链A/链B 共用 240→D
        self.assertEqual(UPPER_OF["60"], "240")
        self.assertEqual(UPPER_OF["30"], "240")  # 链B
        self.assertEqual(UPPER_OF["5"], "30")    # 链B
        self.assertEqual(minTouchFor("30"), 4)
        self.assertEqual(minTouchFor("5"), 8)
        self.assertEqual(periodNameOf("1"), "1分钟")
        self.assertEqual(periodNameOf("30"), "30分钟")


class Mt5ResampleTests(unittest.TestCase):
    """M1 → 1/5/30 重采样（链B 实盘行情）：epoch 分箱 + 只写已收盘桶口径不变。"""

    def _m1(self, t0, n):
        # n 根 M1：收盘价 = 序号（便于断言桶内极值）
        return [{"time": t0 + i * 60, "open": 100.0, "high": 100.0 + i,
                 "low": 100.0 - i, "close": 100.0 + i}
                for i in range(n)]

    def test_resample_1m_passthrough(self):
        bars = self._m1(0, 5)
        out = resample(bars, "1")
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0]["time"], 0)
        self.assertEqual(out[-1]["close"], 104.0)

    def test_resample_5m(self):
        bars = self._m1(0, 10)   # 10 根 M1 → 2 个 5m 桶
        out = resample(bars, "5")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["time"], 0)
        self.assertEqual(out[0]["high"], 104.0)   # 桶1 高=前5根最高
        self.assertEqual(out[1]["time"], 300)
        self.assertEqual(out[1]["high"], 109.0)

    def test_resample_30m(self):
        bars = self._m1(0, 90)   # 90 根 M1 → 3 个 30m 桶
        out = resample(bars, "30")
        self.assertEqual(len(out), 3)
        self.assertEqual([b["time"] for b in out], [0, 1800, 3600])
        self.assertEqual(out[1]["low"], 100.0 - 59)   # 桶2 低=第30~59根最低（i=59）


class _Sig:
    """SignalLog 冒烟用最小 broadcaster。"""


class SignalLogChainTests(unittest.TestCase):
    """SignalLog 行键含 chain：两链同拍同周期同策略信号互不串写。"""

    def test_chain_row_isolation(self):
        from .webapp import SignalLog
        sl = SignalLog()
        base = {"strategy": "chan_v1", "symbol": "X", "time": 100, "periodX": "240",
                "direction": "long", "strategyKey": "2Buy"}
        r1 = sl.append_signal("backtest", dict(base, chain="A"))
        r2 = sl.append_signal("backtest", dict(base, chain="B"))
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertEqual(r1["chain"], "A")
        self.assertEqual(r2["chain"], "B")
        # 成交回填按链匹配：链A 的成交只回填链A 行
        row = sl.fill_trade("backtest", dict(base, chain="A", signalTime=100,
                                             entryTime=110, entryPrice=1.0))
        self.assertEqual(row["id"], r1["id"])
        self.assertEqual(sl.rows[r2["id"] - 1]["entryTime"], None)
        # 无 chain 的旧行为：同键（chain=None）成交不串写到带链行
        row3 = sl.fill_trade("backtest", dict(base, signalTime=100, entryTime=120))
        self.assertNotEqual(row3["id"], r1["id"])
        self.assertNotEqual(row3["id"], r2["id"])


if __name__ == "__main__":
    unittest.main()
