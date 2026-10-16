import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志

# -*- coding: utf-8 -*-
"""震荡判定参考周期（range_res）与 2买/2卖 DIF 0 轴容差（macdZeroTol）单元测试
（2026-09-16 闸门口径 + 2026-10-08 关闭项；对应 SPEC §2.1.2.1 / §2.1.2.4）

2026-10-16 起交易计划策略行取消：本文件只测 compute_regime（震荡 regime 唯一入口，
原 predictPlan/compute_plan 的闸门部分抽出）与 macdZeroTol。
mark_entry 的 range_watch/res_ge_range 消费口径见 test_plan_strategy / test_rule_fixes。

覆盖：
  - macdAboveZero / macdBelowZero：容差内/外、tol=0 回退严格口径、空数组
  - compute_regime(range_res=...)：参考周期震荡 → {range:True}；强趋势 → {range:False}；
    参考周期笔 <2（含整行无数据）→ {range:True, insufficient:True}；
    "" 关闭 → None；work_cache 二次调用复用一致

运行：python -m unittest py_chain.test_range_gate -v
"""

import unittest

from py_chain import chan_core
from py_chain.mark_entry import macdAboveZero, macdBelowZero
from py_chain.trading_plan import compute_regime, RANGE_RES


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


class TestComputeRegime(unittest.TestCase):
    """compute_regime：震荡 regime 唯一入口（2026-10-16 从 compute_plan 抽出；
    更低周期的 range_watch / 参考周期及以上 res_ge_range 剔除由 mark_entry 执行）。"""

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

    def _regime(self, *, range_res="240", work_cache=None, **kw):
        periodBis, barsByPeriod, periodAtr = self._build(**kw)
        return compute_regime(periodBis, barsByPeriod, self.PERIODS, periodAtr=periodAtr,
                              work_cache=work_cache, range_res=range_res)

    def test_default_range_res(self):
        self.assertEqual(RANGE_RES, "")

    def test_off_returns_none(self):
        # "" 关闭 → None（mark_entry 无震荡闸，直接走买卖点）
        self.assertIsNone(self._regime(range_res="", **self._RANGE_240))

    # 常用素材
    _RANGE_240 = dict(bis240=alt_bis(0, 14400, 6, 4100.0, 4110.0),
                      bars240=flat_bars(0, 14400, 40, 4099.0, 4111.0),
                      bis_low=trend_bis(0, 900, 6, 4200.0, 30.0),
                      bars_low=trend_bars(0, 900, 40, 4200.0, 8.0),
                      atr240=100.0, atr_low=1.0)
    _TREND_240 = dict(bis240=trend_bis(0, 14400, 6, 5000.0, 100.0),
                      bars240=trend_bars(0, 14400, 40, 5000.0, 25.0),
                      bis_low=alt_bis(0, 900, 8, 4200.0, 4210.0),
                      bars_low=flat_bars(0, 900, 40, 4199.0, 4211.0),
                      atr240=1.0, atr_low=100.0)

    def test_ref_range_gate_true(self):
        # 240 自身 A 支震荡（小区间K线+交替笔）→ regime 震荡
        gate = self._regime(range_res="240", **self._RANGE_240)
        self.assertTrue(gate["range"])
        self.assertFalse(gate.get("insufficient"))
        self.assertEqual(gate["resName"], "4小时")
        self.assertIn("判定为震荡整理", gate["reason"])

    def test_ref_trend_gate_false(self):
        # 240 强趋势 → regime 非震荡
        gate = self._regime(range_res="240", **self._TREND_240)
        self.assertFalse(gate["range"])
        self.assertEqual(gate["reason"], "")

    def test_ref_insufficient(self):
        # 240 只有 1 笔（<2）→ 不足闸；整行无数据 → 同样不足闸
        gate = self._regime(range_res="240",
                            bis240=alt_bis(0, 14400, 1, 4100.0, 4110.0),
                            bars240=flat_bars(0, 14400, 40, 4099.0, 4111.0),
                            bis_low=alt_bis(0, 900, 8, 4200.0, 4210.0),
                            bars_low=flat_bars(0, 900, 40, 4199.0, 4211.0),
                            atr240=100.0, atr_low=100.0)
        self.assertTrue(gate["range"])
        self.assertTrue(gate.get("insufficient"))
        self.assertIn("笔数据不足", gate["reason"])
        gate2 = self._regime(range_res="240",
                             bis240=[],
                             bars240=[],
                             bis_low=alt_bis(0, 900, 8, 4200.0, 4210.0),
                             bars_low=flat_bars(0, 900, 40, 4199.0, 4211.0),
                             atr240=100.0, atr_low=100.0)
        self.assertTrue(gate2["range"])
        self.assertTrue(gate2.get("insufficient"))

    def test_work_cache_reuse_consistent(self):
        cache = {}
        g1 = self._regime(range_res="240", work_cache=cache, **self._RANGE_240)
        g2 = self._regime(range_res="240", work_cache=cache, **self._RANGE_240)
        self.assertEqual(g1, g2)
        self.assertIn(("rangeGate", "240"), cache)
        # 关闭模式不落缓存键、返回 None
        g3 = self._regime(range_res="", work_cache=cache, **self._RANGE_240)
        self.assertIsNone(g3)


if __name__ == "__main__":
    unittest.main()
