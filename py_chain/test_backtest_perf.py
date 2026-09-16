# -*- coding: utf-8 -*-
"""链路缓存失效白盒测试（SPEC_backtest_perf 第四/五批收尾修复，2026-09-16）

覆盖两处正确性隐患：
  - 重同步后 _chain_work_cache 清空：批量重建可能修正中段笔而 _bis_fingerprint
    （len+末笔端点）不变，sr/plan 按周期复用会拿到漂移期旧结果；
  - 实时bar覆盖/整周期重放后 bars 前缀缓存失效：覆盖是整槽替换（bl[-1]=dict(b)），
    前缀列表仍持旧 dict 引用，cut 已含末根时 _prefix_bars 返回过期窗口。

运行：python -m unittest py_chain.test_backtest_perf -v
"""

import copy
import random
import unittest

from py_chain.backtest import BacktestEngine

SEC = {"3": 180, "15": 900, "60": 3600, "240": 14400, "D": 86400}
PERIODS = ("D", "240", "60", "15", "3")


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


def bars_all(n3=600):
    """各周期同一总跨度（3m 600 根 ≈ 108000s）的合成数据，跨度对齐保证 fine_res=3。"""
    total = n3 * SEC["3"]
    return {res: gen_bars(res, total // SEC[res]) for res in PERIODS}


def advance_all(eng):
    """逐根推进全部 fine bar（与 run() 同节奏；跨过 RESYNC_EVERY=200 的重同步边界）。"""
    fine = eng.bars["3"]["_list"]
    for b in fine:
        eng._advance_cut(b["time"] + SEC["3"])


class TestResyncClearsWorkCache(unittest.TestCase):
    def test_resync_clears_and_repopulates(self):
        eng = BacktestEngine(copy.deepcopy(bars_all()), warmup_bars=60)
        advance_all(eng)
        self.assertEqual(eng._cut["3"], len(eng.bars["3"]["_list"]))  # 全部已收盘
        eng._rebuild_chain(include_entries=False)
        self.assertTrue(eng._chain_work_cache)  # sr/plan 按周期条目已填充
        eng.resync_all()
        self.assertFalse(eng._chain_work_cache)  # 重同步后必须清空
        eng._rebuild_chain(include_entries=False)
        self.assertTrue(eng._chain_work_cache)  # 随后重算正常重填


class TestOverrideInvalidatesPrefix(unittest.TestCase):
    def setUp(self):
        self.bars = bars_all()
        self.eng = BacktestEngine(copy.deepcopy(self.bars), warmup_bars=60)
        advance_all(self.eng)
        # cut == len：末根已收盘、已入前缀——覆盖它才能构成前缀过期场景
        self.assertEqual(self.eng._cut["3"], len(self.bars["3"]))
        last = self.bars["3"][-1]
        self.new_last = dict(last, close=4999.0, high=5000.0)
        ret = self.eng.append_bars("3", [self.new_last])
        self.assertEqual(ret, "override")

    def test_prefix_sees_overridden_bar(self):
        prefix = self.eng._prefix_bars("3")
        bl = self.eng.bars["3"]["_list"]
        self.assertIs(prefix[-1], bl[-1])          # 引用即覆盖后的新 dict
        self.assertEqual(prefix[-1]["close"], 4999.0)

    def test_rewind_rebuilds_prefix(self):
        self.eng._rewind_res("3")
        prefix = self.eng._prefix_bars("3")
        self.assertIs(prefix[-1], self.eng.bars["3"]["_list"][-1])
        self.assertEqual(prefix[-1]["close"], 4999.0)


if __name__ == "__main__":
    unittest.main()
