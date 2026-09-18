# -*- coding: utf-8 -*-
"""find* 冻结前缀缓存白盒测试（SPEC_backtest_perf 第七批 S1/S3，2026-09-18）

核心性质（adopt 教训的直接检验——不只验单点，任意后续输入下续算=全量）：
逐根推进的引擎序列上，每拍对同一输入分别跑「带 cache（跨拍持久）」与
「cache=None（全量重算）」的 findBuyPoints/findSellPoints，输出须逐位一致；
中途触发 resync、手工替换中段笔 dict（模拟深改写）后仍须一致。

运行：python -m unittest py_chain.test_find_perf_cache -v
"""

import copy
import random
import unittest

from py_chain.backtest import BacktestEngine
from py_chain.chan_core import (findBuyPoints, findSellPoints, structurePeriods,
                                intervalSecOf)
from py_chain.sr_flip import UPPER_OF

SEC = {"3": 180, "15": 900, "60": 3600, "240": 14400, "D": 86400}
PERIODS = ("D", "240", "60", "15", "3")


def gen_bars(res, n, t0=1_800_000_000, seed=7):
    """碎行情随机游走（±25 大幅震荡 → 分型密集，保证各周期笔数足够触发缓存路径）。"""
    rnd = random.Random(seed)
    t, price = t0, 4500.0
    out = []
    for _ in range(n):
        o = price
        c = o + rnd.uniform(-25, 25)
        hi = max(o, c) + rnd.uniform(0, 6)
        lo = min(o, c) - rnd.uniform(0, 6)
        out.append({"time": t, "open": round(o, 2), "high": round(hi, 2),
                    "low": round(lo, 2), "close": round(c, 2)})
        t += SEC[res]
        price = c
    return out


def bars_all(n3=1200):
    total = n3 * SEC["3"]
    return {res: gen_bars(res, total // SEC[res], seed=11 + i)
            for i, res in enumerate(PERIODS)}


class TestFindPerfCacheStepwise(unittest.TestCase):
    def _compare_step(self, eng, cache):
        """对当前引擎状态跑 find* 双路对照（结构视图与生产链路同构）。"""
        barsByPeriod = {res: eng._prefix_bars(res) for res in eng.periods}
        views = structurePeriods(eng._bis, barsByPeriod, eng._decision_time,
                                 eng._merged, eng._fractals, cache)
        for res in eng.periods:
            view = views.get(res) or []
            if len(view) < 3:
                continue
            upperRes = UPPER_OF.get(str(res).upper())
            upperBis = views.get(upperRes) if upperRes else None
            macd = eng._macd[res].to_list()
            sec = intervalSecOf(res)
            a = findBuyPoints(view, upperBis, macd, sec, cache=None)
            b = findBuyPoints(view, upperBis, macd, sec, cache=cache)
            self.assertEqual(a, b, f"{res} findBuyPoints 带缓存与全量不一致")
            a = findSellPoints(view, upperBis, macd, sec, cache=None)
            b = findSellPoints(view, upperBis, macd, sec, cache=cache)
            self.assertEqual(a, b, f"{res} findSellPoints 带缓存与全量不一致")

    def test_stepwise_equivalence(self):
        bars = bars_all()
        eng = BacktestEngine(copy.deepcopy(bars), warmup_bars=60)
        cache = {}
        fine = eng.bars[eng.fine_res]["_list"]
        checked = 0
        for i, b in enumerate(fine):
            eng._advance_cut(b["time"] + SEC["3"])
            if i % 9 != 0:
                continue
            self._compare_step(eng, cache)
            checked += 1
        self.assertGreater(checked, 100)
        # 对抗：中途整表重同步（resync 换列表对象 → 身份失配全量重算）
        eng.resync_all()
        self._compare_step(eng, cache)
        # 对抗：手工替换中段笔 dict（模拟深改写 → 必须失配回退）
        for res in eng.periods:
            bl = eng._bis[res]
            if len(bl) > 40:
                j = len(bl) // 2
                bl[j] = dict(bl[j])
        self._compare_step(eng, cache)

    def test_record_cache_actually_hits(self):
        """确认缓存真的在复用（非每拍全量重建）：上级确认笔 ≥ _FIND1_MARGIN(26) 时
        记录槽跨拍只增不减（尾部续算），并出现严格增长。"""
        bars = bars_all(n3=9600)
        eng = BacktestEngine(copy.deepcopy(bars), warmup_bars=60)
        cache = {}
        fine = eng.bars[eng.fine_res]["_list"]
        lens = []
        for i, b in enumerate(fine):
            eng._advance_cut(b["time"] + SEC["3"])
            if i % 9 != 0 or i < len(fine) // 2:
                continue
            barsByPeriod = {res: eng._prefix_bars(res) for res in eng.periods}
            views = structurePeriods(eng._bis, barsByPeriod, eng._decision_time,
                                     eng._merged, eng._fractals, cache)
            res = "15"
            view = views.get(res) or []
            if len(view) < 3:
                continue
            upperBis = views.get(UPPER_OF.get(res))
            macd = eng._macd[res].to_list()
            findSellPoints(view, upperBis, macd, intervalSecOf(res), cache=cache)
            slot = cache.get(("find1sell", id(macd)))
            lens.append(len(slot[1]) if slot and slot[0] is macd else 0)
        self.assertTrue(any(l2 > l1 for l1, l2 in zip(lens, lens[1:])),
                        f"记录槽从未增长（缓存未复用）：{lens[:8]}")
        self.assertGreater(max(lens), 0)


if __name__ == "__main__":
    unittest.main()
