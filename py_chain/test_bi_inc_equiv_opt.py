# -*- coding: utf-8 -*-
"""bi_inc 等价优化机制测试（2026-10-20）：

_atr_gap_rewind_to 的档位 bisect 检测 vs HEAD 原逐对全扫暴力版随机对拍
（锁变化续算/回卷提前退出两机制已退役，见 bi_inc 模块注释；本测试锁定
翻转检测与暴力口径逐位一致）。"""
import random
import unittest

from py_chain.bi_inc import BiIncBuilder
from py_chain.chan_core import CHAN_CFG

GF = CHAN_CFG["gapFilter"]


def _brute_rewind(gap_diffs, old_th, new_th, seq_midx):
    """HEAD 原版 _atr_gap_rewind_to 的判定（逐对全扫 + 逐元素扫）。"""
    if old_th is None or old_th == new_th or not gap_diffs:
        return None
    flip_at = None
    for i, (up, dn) in enumerate(gap_diffs):
        if (up >= old_th or dn >= old_th) != (up >= new_th or dn >= new_th):
            flip_at = i
            break
    if flip_at is None:
        return None
    for k, m in enumerate(seq_midx):
        if m > flip_at:
            return max(0, k - 1)
    return 0


class TestAtrFlipBisect(unittest.TestCase):
    def test_random_matches_bruteforce(self):
        rnd = random.Random(20261020)
        for trial in range(500):
            b = BiIncBuilder("3")
            n = rnd.randrange(0, 40)
            diffs = [(rnd.uniform(-5, 5), rnd.uniform(-5, 5)) for _ in range(n)]
            gap_final = max(0, n - 1)
            b._gap_diffs = diffs
            b._gap_final = gap_final
            b._gap_levels = sorted(
                ((max(u, d), i) for i, (u, d) in enumerate(diffs[:gap_final])))
            b._seq_midx = sorted(rnd.randrange(0, 300)
                                 for _ in range(rnd.randrange(0, 40)))
            old_th = rnd.choice([None, 0.0, rnd.uniform(0, 5)])
            b._gap_threshold = old_th
            new_th = rnd.uniform(0, 5)
            got = b._atr_gap_rewind_to(new_th / GF)
            want = _brute_rewind(diffs, old_th, new_th, b._seq_midx)
            self.assertEqual(got, want, f"trial={trial} diffs={diffs} "
                             f"old={old_th} new={new_th} midx={b._seq_midx}")


if __name__ == "__main__":
    unittest.main()
