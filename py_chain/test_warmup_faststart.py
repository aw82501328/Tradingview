import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志

# -*- coding: utf-8 -*-
"""预热批量热启动（run(fast_warmup=True)，默认）与旧逐根预热（False）的行为等价对拍。

契约（backtest.run 预热段，2026-10-01）：批量热启动只允许改变预热段的推进方式——
一把 _advance_cut 到最近 RESYNC_EVERY 网格点（该点状态=batch(前缀)，与逐步路径
重同步点同口径，见 RESYNC_EVERY 注释与 test_engine_lock_parity），其后 <200 根
照旧逐步推进、重同步节拍对齐。两种模式在同输入下 trades/signals/stats 与终点
内部状态（各周期笔列表、MACD 序列）必须逐字节一致。

覆盖：
  - 合成数据多起点：start_i 落在网格点/网格后 1 根/网格后 199 根/跨多个网格，
    以及 start_ts=None 旧口径（warmup_bars 根预热，≥200 同样触发批量段）；
  - 真实数据小窗口（data/bars.db 有 OANDA:XAUUSD 才跑，否则跳过）：3 分钟 fine、
    含支阻/计划全链路重算路径，start_i 跨两个网格。

运行：python -m unittest py_chain.test_warmup_faststart -v
"""
import copy
import datetime
import os
import random
import sqlite3
import unittest

from py_chain.backtest import BacktestEngine, RESYNC_EVERY

SEC = {"3": 180, "15": 900, "60": 3600, "240": 14400, "D": 86400}
PERIODS = ("D", "240", "60", "15", "3")
SYN_N3 = 2600            # 3 分钟根数（合成）：够跨多个 200 网格 + 交易段
BI_KEY = ("type", "startTime", "startPrice", "endTime", "endPrice")


def gen_bars(res, n, t0=1_800_000_000, seed=7):
    """随机游走合成K线（结构足够复杂：分型/笔/合并都会产生）。"""
    rnd = random.Random(seed)
    t, price = t0, 4500.0
    out = []
    for _ in range(n):
        o = price
        c = o + rnd.uniform(-9, 9)
        hi = max(o, c) + rnd.uniform(0, 4)
        lo = min(o, c) - rnd.uniform(0, 4)
        out.append({"time": t, "open": round(o, 2), "high": round(hi, 2),
                    "low": round(lo, 2), "close": round(c, 2)})
        t += SEC[res]
        price = c
    return out


SEEDS = {"D": 1, "240": 2, "60": 3, "15": 4, "3": 5}


def bars_all(n3=SYN_N3):
    total = n3 * SEC["3"]
    return {res: gen_bars(res, total // SEC[res], seed=SEEDS[res]) for res in PERIODS}


def engine_state(eng):
    """终点内部状态指纹：各周期笔列表 + MACD 序列（重复追加/漂移都会现形）。"""
    state = {}
    for res in eng.periods:
        bis = eng._bis.get(res) or []
        state[f"bis:{res}"] = [tuple(b.get(k) for k in BI_KEY) for b in bis]
        state[f"macd:{res}"] = eng._macd[res].to_list()
    return state


def run_mode(bars, start_ts=None, warmup=60, fast=True, to_ts=None):
    eng = BacktestEngine(copy.deepcopy(bars), warmup_bars=warmup,
                         signal_mode="realtime")
    res = eng.run(start_ts=start_ts, to_ts=to_ts, journal=False,
                  fast_warmup=fast)
    return eng, res


def assert_parity(tc, bars, start_ts=None, warmup=60, to_ts=None):
    eng_a, res_a = run_mode(bars, start_ts, warmup, fast=True, to_ts=to_ts)
    eng_b, res_b = run_mode(bars, start_ts, warmup, fast=False, to_ts=to_ts)
    tc.assertEqual(res_a["trades"], res_b["trades"], "trades 不一致")
    tc.assertEqual(res_a["signals"], res_b["signals"], "signals 不一致")
    tc.assertEqual(res_a["stats"], res_b["stats"], "stats 不一致")
    sa, sb = engine_state(eng_a), engine_state(eng_b)
    tc.assertEqual(sorted(sa), sorted(sb))
    for k in sa:
        tc.assertEqual(sa[k], sb[k], f"内部状态 {k} 不一致")


class TestWarmupFastStartSynthetic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = bars_all()
        cls.fine = cls.bars["3"]

    def test_start_i_on_grid(self):
        # start_i 恰在网格点上（批量段直接到位，尾部逐步段为空）
        k = 2 * RESYNC_EVERY
        assert_parity(self, self.bars, start_ts=self.fine[k]["time"])

    def test_start_i_grid_plus_one(self):
        k = 3 * RESYNC_EVERY + 1
        assert_parity(self, self.bars, start_ts=self.fine[k]["time"])

    def test_start_i_grid_plus_199(self):
        # 网格后 199 根 = 尾部逐步段最长的情况（差 1 根触发下一次重同步）
        k = RESYNC_EVERY + (RESYNC_EVERY - 1)
        assert_parity(self, self.bars, start_ts=self.fine[k]["time"])

    def test_start_i_far_multi_grid(self):
        k = 2200
        assert_parity(self, self.bars, start_ts=self.fine[k]["time"])

    def test_default_warmup_path(self):
        # start_ts=None 旧口径：warmup_bars=300 ≥ RESYNC_EVERY 同样走批量段
        assert_parity(self, self.bars, warmup=300)

    def test_below_threshold_unchanged(self):
        # start_i < RESYNC_EVERY：批量段不触发，两模式走完全相同代码路径
        k = 150
        assert_parity(self, self.bars, start_ts=self.fine[k]["time"])


def _load_real():
    """真实小窗口：XAUUSD，2026-07-28 → 2026-08-05（约 2 天预热 + 2 天交易）。"""
    con = sqlite3.connect("data/bars.db")
    try:
        out = {}
        for res in PERIODS:
            rows = con.execute(
                "SELECT time,open,high,low,close FROM bars "
                "WHERE symbol='OANDA:XAUUSD' AND res=? AND time>=? AND time<? ORDER BY time",
                (res, 1785148800, 1785576000)).fetchall()
            if not rows:
                return None
            out[res] = [dict(zip(("time", "open", "high", "low", "close"), r)) for r in rows]
    finally:
        con.close()
    return out


@unittest.skipIf(not os.path.exists("data/bars.db"), "无 data/bars.db")
class TestWarmupFastStartReal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = _load_real()
        if cls.bars is None:
            raise unittest.SkipTest("bars.db 无 XAUUSD 窗口数据")

    def test_real_parity(self):
        # 交易起点 2026-08-01 00:00Z：3 分钟 fine 约 900 根预热，跨 4 个网格，
        # 交易窗 2 天——覆盖支阻/计划链路重算与信号评估的完整路径
        start_ts = int(datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc).timestamp())
        to_ts = int(datetime.datetime(2026, 8, 3, tzinfo=datetime.timezone.utc).timestamp())
        assert_parity(self, self.bars, start_ts=start_ts, to_ts=to_ts)


if __name__ == "__main__":
    import sys
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    unittest.main(verbosity=2)
