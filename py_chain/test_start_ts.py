# -*- coding: utf-8 -*-
"""run(start_ts) 交易开始时刻口径单元测试（SPEC §2.1.7，2026-09-15）

覆盖：
  - start_ts 生效：预热边界 = 首根 time >= start_ts 的 fine bar（「预热完成」日志定位），
    start_ts 之前只推进状态——无任何 entryTime < start_ts 的成交（空仓起步）；
  - start_ts=None 旧口径：预热边界仍为 warmup_bars 根（默认路径零改动）；
  - start_ts 早于数据起点：不崩，回退为至少 1 根预热 bar；
  - 确定性：同输入重跑两次 trades/signals/stats 逐笔一致。

运行：python -m unittest py_chain.test_start_ts -v
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


def bars_all(n3=2400):
    """各周期同一总跨度（3m 2400 根 ≈ 432000s）的合成数据，跨度对齐保证 fine_res=3。"""
    total = n3 * SEC["3"]
    return {res: gen_bars(res, total // SEC[res]) for res in PERIODS}


def run_engine(bars, start_ts=None, warmup=60):
    """跑一次引擎，收集日志行（预热边界断言用）。"""
    logs = []
    eng = BacktestEngine(copy.deepcopy(bars), warmup_bars=warmup)
    res = eng.run(start_ts=start_ts,
                  log=lambda *a: logs.append(" ".join(str(x) for x in a)))
    return eng, res, logs


def warm_line(logs):
    return next(l for l in logs if l.startswith("预热完成"))


class TestStartTs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = bars_all()
        cls.fine = cls.bars["3"]

    def test_start_ts_boundary(self):
        # 交易起点 = 第 1201 根 fine bar（0-based 1200）的时刻：bisect_left → start_i=1200
        mid = self.fine[1200]["time"]
        _, res, logs = run_engine(self.bars, start_ts=mid)
        self.assertIn("已到第 1200 根", warm_line(logs))
        for tr in res["trades"]:
            self.assertGreaterEqual(tr["entryTime"], mid)  # start_ts 前无任何成交

    def test_default_warmup_unchanged(self):
        # start_ts=None（缺省）：预热边界仍为 warmup_bars 根，旧口径行为不变
        _, _, logs = run_engine(self.bars)
        self.assertIn("已到第 60 根", warm_line(logs))

    def test_start_ts_before_data(self):
        # start_ts 早于数据起点：回退为至少 1 根预热 bar，不崩
        t0 = self.fine[0]["time"]
        _, res, logs = run_engine(self.bars, start_ts=t0 - 3600)
        self.assertIn("已到第 1 根", warm_line(logs))
        for tr in res["trades"]:
            self.assertGreaterEqual(tr["entryTime"], t0)

    def test_deterministic(self):
        # 同数据 + 同 start_ts 重跑两次：trades/signals/stats 逐笔一致
        mid = self.fine[1200]["time"]
        _, r1, _ = run_engine(self.bars, start_ts=mid)
        _, r2, _ = run_engine(self.bars, start_ts=mid)
        self.assertEqual(r1["trades"], r2["trades"])
        self.assertEqual(r1["stats"], r2["stats"])
        self.assertEqual(r1["signals"], r2["signals"])


if __name__ == "__main__":
    unittest.main()
