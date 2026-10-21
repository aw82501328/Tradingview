import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志

# -*- coding: utf-8 -*-
"""统一点源回归测试（2026-10-21）：缠论V1 与 fxma_v1 共用同一套买卖点

覆盖：
  - compute_period_points 全量笔视图：不再截最近 60 笔（与 fxma _fx_all_points /
    图上标记层 compute_all_marks 同口径；旧窗口曾使老锚投影退化/序列截断）；
  - class2ZsTol/thirdZsTol 透传：points 品种桶容差原样进 findBuy/SellPoints
    （此前引擎调用点漏传恒 0.0，与 fxma/标记层分歧；class2ZsTol 已停用仅签名兼容）；
  - BacktestEngine 链路把 module_params marks（=points 品种桶）容差传进
    compute_period_points。

运行：python -m unittest py_chain.test_unified_points -v
"""

import random
import unittest
from unittest import mock

from py_chain import mark_entry
from py_chain.backtest import BacktestEngine

SEC = {"3": 180, "15": 900, "60": 3600, "240": 14400, "D": 86400}
PERIODS = ("D", "240", "60", "15", "3")


def _mk_bis(n, t0=1_800_000_000, step=180, p0=4500.0, seed=11):
    """合成已确认笔序列（无 _forming 全部合格；字段满足 pointEligibleBis/
    _bis_fingerprint 只读需求）。"""
    rnd = random.Random(seed)
    t, p = t0, p0
    out = []
    for i in range(n):
        typ = "up" if i % 2 == 0 else "down"
        e = p + rnd.uniform(-30, 30)
        out.append({"type": typ, "startTime": t, "startPrice": round(p, 2),
                    "endTime": t + step, "endPrice": round(e, 2)})
        t += step
        p = e
    return out


def gen_bars(res, n, t0=1_800_000_000, seed=7):
    """随机游走合成K线（仅驱动引擎推进，不要求产生信号）。"""
    rnd = random.Random(seed)
    t, price = t0, 4500.0
    out = []
    for _ in range(n):
        o = price
        c = o + rnd.uniform(-6, 6)
        hi = max(o, c) + rnd.uniform(0, 3)
        lo = min(o, c) - rnd.uniform(0, 3)
        out.append({"time": t, "open": round(o, 2), "high": round(hi, 2),
                    "low": round(lo, 2), "close": round(c, 2)})
        t += SEC[res]
        price = c
    return out


def bars_all(n3=2400):
    """各周期同一总跨度的合成数据（跨度对齐保证 fine_res=3）。"""
    total = n3 * SEC["3"]
    return {res: gen_bars(res, total // SEC[res]) for res in PERIODS}


class TestComputePeriodPoints(unittest.TestCase):
    def test_no_60_window_and_tol_passthrough(self):
        """80 笔全量进 findBuyPoints（旧实现截 [-60:]），容差参数原样透传。"""
        periodBis = {"3": _mk_bis(80), "15": _mk_bis(10, step=900)}
        cap = {}

        def fake_buy(bis, upper, macd, sec, class2ZsTol=0.0, thirdZsTol=0.0, **kw):
            cap["buy"] = (len(bis), class2ZsTol, thirdZsTol, sec)
            return []

        def fake_sell(bis, upper, macd, sec, class2ZsTol=0.0, thirdZsTol=0.0, **kw):
            cap.setdefault("sell", len(bis))
            return []

        with mock.patch.object(mark_entry, "findBuyPoints", fake_buy), \
             mock.patch.object(mark_entry, "findSellPoints", fake_sell), \
             mock.patch.object(mark_entry, "pointContext",
                               lambda bis, ub, sec: (bis, None, None, None, None)):
            out = mark_entry.compute_period_points(
                periodBis, ["3"], class2ZsTol=1.5, thirdZsTol=2.5)
        self.assertEqual(out.get("3"), {"buy": [], "sell": []})
        n, c2, c3, sec = cap["buy"]
        self.assertEqual(n, 80, "compute_period_points 须用全量笔视图（2026-10-21 撤 60 笔窗口）")
        self.assertEqual(cap["sell"], 80)
        self.assertEqual((c2, c3), (1.5, 2.5), "points 桶容差须透传 findBuy/SellPoints")
        self.assertEqual(sec, 180)


class TestEnginePlumbing(unittest.TestCase):
    def test_marks_tol_reaches_compute_period_points(self):
        """BacktestEngine 链路把 module_params marks（points 品种桶）容差传进
        compute_period_points（此前漏传恒 0.0）。"""
        captured = []

        def spy(periodBis, periods, **kw):
            captured.append(kw)
            return {}

        with mock.patch("py_chain.backtest.compute_period_points", spy):
            eng = BacktestEngine(
                bars_all(), warmup_bars=40,
                module_params={"marks": {"class2ZsTol": 1.5, "thirdZsTol": 2.5}})
            eng.run(journal=False)
        self.assertTrue(captured, "链路未调用 compute_period_points")
        for kw in captured:
            self.assertEqual(kw.get("class2ZsTol"), 1.5)
            self.assertEqual(kw.get("thirdZsTol"), 2.5)


if __name__ == "__main__":
    unittest.main()
