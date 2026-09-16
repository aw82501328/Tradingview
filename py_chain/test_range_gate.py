# -*- coding: utf-8 -*-
"""震荡判定参考周期（range_res）与 2买/2卖 DIF 0 轴容差（macdZeroTol）单元测试
（2026-09-16 最终口径：小周期只听门、参考周期及以上只作锚、rangeRes 必填；
对应 SPEC §2.1.2.1 / §2.1.2.4）

覆盖：
  - macdAboveZero / macdBelowZero：容差内/外、tol=0 回退严格口径、空数组
  - predictPlan(range_gate=...)：regime 震荡 → 观望（strategy 注明来源周期）；
    regime 非震荡 → 跳过自身 A/B 支直接走趋势分支（替换语义）；
    regime 带 insufficient（参考周期笔 <2）→ 观望·笔数据不足
  - compute_plan(range_res=...)：参考周期 regime 震荡 → 更低周期观望；非震荡 →
    更低周期直接走趋势分支；参考周期及以上（240/D）固定观望只作锚；参考周期
    笔 <2（含无数据）→ 更低周期观望·笔数据不足；"" 未配置 → 全部周期观望；
    work_cache 二次调用复用一致

运行：python -m unittest py_chain.test_range_gate -v
"""

import unittest

from py_chain import chan_core
from py_chain.mark_entry import macdAboveZero, macdBelowZero
from py_chain.trading_plan import predictPlan, compute_plan, RANGE_RES


def macd(t, dif):
    return {"time": t, "dif": dif, "dea": 0.0, "macd": 0.0}


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice,
            "span": abs(endPrice - startPrice)}


def bar(time, open_, high, low, close):
    return {"time": time, "open": open_, "high": high, "low": low, "close": close}


def alt_bis(t0, sec, n, lo, hi):
    """n 笔涨跌交替的小区间笔（端点都在 lo~hi 内）——isRangeBound A 支素材。"""
    out, t, up = [], t0, True
    for _ in range(n):
        s, e = (lo, hi) if up else (hi, lo)
        out.append(bi("up" if up else "down", t, t + sec, s, e))
        t += sec
        up = not up
    return out


def flat_bars(t0, sec, n, lo, hi):
    """n 根小区间K线（高低都在 lo~hi 内）。"""
    mid = (lo + hi) / 2
    return [bar(t0 + i * sec, mid, hi, lo, mid) for i in range(n)]


def trend_bars(t0, sec, n, p0, step):
    """单边上行K线（每根递增 step，区间远超 rangeKMult×小 ATR）。"""
    out, p = [], p0
    for i in range(n):
        out.append(bar(t0 + i * sec, p, p + step + 1, p - 1, p + step))
        p += step
    return out


def trend_bis(t0, sec, n, p0, step):
    """单边上行笔序列（up 大步 / down 小回撤交替，不断创新高）。"""
    out, t, p, up = [], t0, p0, True
    for _ in range(n):
        if up:
            out.append(bi("up", t, t + sec, p, p + step))
            p += step
        else:
            out.append(bi("down", t, t + sec, p, p - step / 4))
            p -= step / 4
        t += sec
        up = not up
    return out


class TestMacdZeroTol(unittest.TestCase):
    def setUp(self):
        self._old = chan_core.CHAN_CFG.get("macdZeroTol")
        chan_core.apply_cfg({"macdZeroTol": 5.0})

    def tearDown(self):
        chan_core.apply_cfg({"macdZeroTol": 5.0 if self._old is None else self._old})

    def test_default_registered(self):
        self.assertEqual(chan_core.CHAN_CFG_DEFAULTS.get("macdZeroTol"), 5.0)

    def test_above_zero_with_tol(self):
        self.assertTrue(macdAboveZero([macd(0, -3.0)]))    # 破0轴但在容差内
        self.assertTrue(macdAboveZero([macd(0, 0.5)]))     # 0轴上方
        self.assertFalse(macdAboveZero([macd(0, -6.0)]))   # 破太多
        self.assertFalse(macdAboveZero([]))

    def test_below_zero_with_tol(self):
        self.assertTrue(macdBelowZero([macd(0, 3.0)]))     # 反弹过0轴但在容差内
        self.assertTrue(macdBelowZero([macd(0, -0.5)]))    # 0轴下方
        self.assertFalse(macdBelowZero([macd(0, 6.0)]))    # 反弹太多
        self.assertFalse(macdBelowZero([]))

    def test_strict_when_tol_zero(self):
        chan_core.apply_cfg({"macdZeroTol": 0.0})
        self.assertFalse(macdAboveZero([macd(0, -0.1)]))
        self.assertTrue(macdAboveZero([macd(0, 0.1)]))
        self.assertFalse(macdBelowZero([macd(0, 0.1)]))
        self.assertTrue(macdBelowZero([macd(0, -0.1)]))


class TestPredictPlanRangeGate(unittest.TestCase):
    """15m 自身构成 A 支震荡（小区间K线 + 交替笔），用于对照 range_gate 的替换语义。"""

    def setUp(self):
        self.sec = 900
        self.bis = alt_bis(0, self.sec, 8, 4200.0, 4210.0)
        self.bars = flat_bars(0, self.sec, 40, 4199.0, 4211.0)
        self.atr = 100.0

    def _plan(self, **kw):
        return predictPlan("15", self.bis, None, [], 4205.0, self.bars,
                           atr=self.atr, barSec=self.sec, **kw)

    def test_own_judgment_range(self):
        row = self._plan()
        self.assertEqual(row["direction"], "观望")
        self.assertIn("震荡整理", row["strategy"])

    def test_gate_range_blocks_with_source_name(self):
        row = self._plan(range_gate={"range": True, "resName": "4小时", "reason": "4小时：xxx"})
        self.assertEqual(row["direction"], "观望")
        self.assertIn("震荡整理（4小时）", row["strategy"])
        self.assertEqual(row["reason"], "4小时：xxx")

    def test_gate_not_range_skips_own_range(self):
        row = self._plan(range_gate={"range": False, "resName": "4小时", "reason": ""})
        # 替换语义：自身 A/B 支被跳过，不再出震荡观望（合成数据无买卖点 → 趋势分支兜底行）
        self.assertNotIn("震荡整理", row["strategy"])

    def test_gate_insufficient_waits(self):
        # 参考周期笔 <2 的不足闸 → 直接观望·笔数据不足（不回退自身判定）
        row = self._plan(range_gate={"range": True, "insufficient": True,
                                     "resName": "4小时", "reason": "4小时笔数据不足"})
        self.assertEqual(row["direction"], "观望")
        self.assertIn("4小时笔数据不足", row["strategy"])
        self.assertNotIn("震荡整理", row["strategy"])


class TestComputePlanRangeRes(unittest.TestCase):
    PERIODS = ["D", "240", "60", "15", "3"]

    def _build(self, *, bis240, bars240, bis_low, bars_low, atr240, atr_low, atr_d=100.0):
        periodBis = {
            "D": alt_bis(0, 86400, 4, 4000.0, 4100.0),
            "240": bis240,
            "60": bis_low,
            "15": bis_low,
            "3": bis_low,
        }
        barsByPeriod = {
            "D": flat_bars(0, 86400, 40, 3999.0, 4101.0),
            "240": bars240,
            "60": bars_low,
            "15": bars_low,
            "3": bars_low,
        }
        periodAtr = {"D": atr_d, "240": atr240,
                     "60": atr_low, "15": atr_low, "3": atr_low}
        return periodBis, barsByPeriod, periodAtr

    def test_default_range_res(self):
        self.assertEqual(RANGE_RES, "240")

    def test_ref_range_blocks_lower_periods(self):
        # 240 自身 A 支震荡（小区间）；更低周期给强趋势数据（自身不会震荡）
        periodBis, barsByPeriod, periodAtr = self._build(
            bis240=alt_bis(0, 14400, 6, 4100.0, 4110.0),
            bars240=flat_bars(0, 14400, 40, 4099.0, 4111.0),
            bis_low=trend_bis(0, 900, 6, 4200.0, 30.0),
            bars_low=trend_bars(0, 900, 40, 4200.0, 8.0),
            atr240=100.0, atr_low=1.0)
        rows = compute_plan(periodBis, barsByPeriod, self.PERIODS, periodAtr=periodAtr,
                            range_res="240")
        for res in ("60", "15", "3"):
            self.assertEqual(rows[res]["direction"], "观望", res)
            self.assertIn("震荡整理（4小时）", rows[res]["strategy"], res)
        # 参考周期自身与更高周期（D）固定观望只作锚，不再做自身判定
        for res in ("240", "D"):
            self.assertEqual(rows[res]["direction"], "观望", res)
            self.assertIn("参考周期", rows[res]["strategy"], res)
            self.assertNotIn("震荡整理", rows[res]["strategy"], res)

    def test_ref_not_range_unlocks_own_range(self):
        # 240 强趋势（自身非震荡）；15m 自身数据本会判 A 支震荡 → 替换语义下不再封锁
        periodBis, barsByPeriod, periodAtr = self._build(
            bis240=trend_bis(0, 14400, 6, 5000.0, 100.0),
            bars240=trend_bars(0, 14400, 40, 5000.0, 25.0),
            bis_low=alt_bis(0, 900, 8, 4200.0, 4210.0),
            bars_low=flat_bars(0, 900, 40, 4199.0, 4211.0),
            atr240=1.0, atr_low=100.0)
        rows = compute_plan(periodBis, barsByPeriod, self.PERIODS, periodAtr=periodAtr,
                            range_res="240")
        self.assertNotIn("震荡整理", rows["15"]["strategy"])
        # 对照："" 未配置（必填项防御语义）→ 全部周期观望
        rows_off = compute_plan(periodBis, barsByPeriod, self.PERIODS, periodAtr=periodAtr,
                                range_res="")
        for res in ("15", "240", "D"):
            self.assertEqual(rows_off[res]["direction"], "观望", res)
            self.assertIn("未配置", rows_off[res]["strategy"], res)
        # 更低周期一致解锁
        self.assertNotIn("震荡整理", rows["3"]["strategy"])

    def test_ref_insufficient_blocks_lower_periods(self):
        # 240 只有 1 笔（不足 2 笔）→ 更低周期观望·笔数据不足（不再回退自身判定）
        periodBis, barsByPeriod, periodAtr = self._build(
            bis240=alt_bis(0, 14400, 1, 4100.0, 4110.0),
            bars240=flat_bars(0, 14400, 40, 4099.0, 4111.0),
            bis_low=alt_bis(0, 900, 8, 4200.0, 4210.0),   # 自身本会判 A 支震荡
            bars_low=flat_bars(0, 900, 40, 4199.0, 4211.0),
            atr240=100.0, atr_low=100.0)
        rows = compute_plan(periodBis, barsByPeriod, self.PERIODS, periodAtr=periodAtr,
                            range_res="240")
        for res in ("60", "15", "3"):
            self.assertEqual(rows[res]["direction"], "观望", res)
            self.assertIn("笔数据不足", rows[res]["strategy"], res)
            self.assertNotIn("震荡整理", rows[res]["strategy"], res)  # 未走自身 A/B
        # 240 整行无笔数据（行被 continue 跳过）→ 同样观望·笔数据不足
        periodBis["240"] = []
        rows2 = compute_plan(periodBis, barsByPeriod, self.PERIODS, periodAtr=periodAtr,
                             range_res="240")
        self.assertEqual(rows2["15"]["direction"], "观望")
        self.assertIn("笔数据不足", rows2["15"]["strategy"])
        self.assertNotIn("震荡整理", rows2["15"]["strategy"])

    def test_ref_and_above_anchor_only(self):
        # 240/D 给强趋势数据（若判定会出趋势策略）→ 计划行仍固定观望只作锚
        periodBis, barsByPeriod, periodAtr = self._build(
            bis240=trend_bis(0, 14400, 8, 5000.0, 100.0),
            bars240=trend_bars(0, 14400, 40, 5000.0, 25.0),
            bis_low=trend_bis(0, 900, 6, 4200.0, 30.0),
            bars_low=trend_bars(0, 900, 40, 4200.0, 8.0),
            atr240=1.0, atr_low=1.0)
        rows = compute_plan(periodBis, barsByPeriod, self.PERIODS, periodAtr=periodAtr,
                            range_res="240")
        for res in ("240", "D"):
            self.assertEqual(rows[res]["direction"], "观望", res)
            self.assertIn("参考周期", rows[res]["strategy"], res)

    def test_work_cache_reuse_consistent(self):
        periodBis, barsByPeriod, periodAtr = self._build(
            bis240=alt_bis(0, 14400, 6, 4100.0, 4110.0),
            bars240=flat_bars(0, 14400, 40, 4099.0, 4111.0),
            bis_low=trend_bis(0, 900, 6, 4200.0, 30.0),
            bars_low=trend_bars(0, 900, 40, 4200.0, 8.0),
            atr240=100.0, atr_low=1.0)
        cache = {}
        r1 = compute_plan(periodBis, barsByPeriod, self.PERIODS, periodAtr=periodAtr,
                          work_cache=cache, range_res="240")
        r2 = compute_plan(periodBis, barsByPeriod, self.PERIODS, periodAtr=periodAtr,
                          work_cache=cache, range_res="240")
        self.assertEqual(r1, r2)
        self.assertIn(("planGate", "240"), cache)


if __name__ == "__main__":
    unittest.main()
