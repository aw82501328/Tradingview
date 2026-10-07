import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志

# -*- coding: utf-8 -*-
"""fxma 预热批量热启动（run(fast_warmup=True)，默认）与旧逐根预热（False）对拍。

契约（fx_ma.run 预热段，2026-10-07，同缠论V1 fast_warmup）：批量热启动只改变
预热段推进方式——一把 _advance_cut 到最近 RESYNC_EVERY 网格点（状态=batch(前缀)，
与逐步路径重同步点同口径），其后 <200 根照旧逐步推进。两种模式同输入下
trades/signals/stats 与终点内部状态（各周期笔/MACD/均线累计器/去重集）必须一致。

与缠论V1 test_warmup_faststart 的差异：fxma warmup_bars 固定 60（<RESYNC_EVERY），
批量段只在 start_ts 起点口径（lead_days 前移）触发，故无「旧口径 warmup≥200」用例。

运行：python -m unittest py_chain.test_fx_ma_warmup_faststart -v
"""
import copy
import datetime
import os
import random
import sqlite3
import unittest

from py_chain.backtest import RESYNC_EVERY
from py_chain.fx_ma import FxMaEngine

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
    """终点内部状态指纹：笔/MACD/各 P 均线累计器内部值/信号去重集。"""
    state = {}
    for res in eng.periods:
        bis = eng._bis.get(res) or []
        state[f"bis:{res}"] = [tuple(b.get(k) for k in BI_KEY) for b in bis]
        state[f"macd:{res}"] = eng._macd[res].to_list()
    for P, st in eng._fx_ma.items():
        for j, acc in enumerate(st["mas"]):
            state[f"ma:{P}:{j}"] = (acc.kind, acc.period, list(acc.buf),
                                    acc.total, acc.ema)
    state["fired"] = sorted(eng._fx_fired)
    return state


def run_mode(bars, start_ts=None, to_ts=None, fast=True):
    eng = FxMaEngine(copy.deepcopy(bars), entry_res="3,15,60")
    res = eng.run(start_ts=start_ts, to_ts=to_ts, journal=False,
                  fast_warmup=fast)
    return eng, res


def assert_parity(tc, bars, start_ts=None, to_ts=None):
    eng_a, res_a = run_mode(bars, start_ts, to_ts, fast=True)
    eng_b, res_b = run_mode(bars, start_ts, to_ts, fast=False)
    tc.assertEqual(res_a["trades"], res_b["trades"], "trades 不一致")
    tc.assertEqual(res_a["signals"], res_b["signals"], "signals 不一致")
    tc.assertEqual(res_a["stats"], res_b["stats"], "stats 不一致")
    sa, sb = engine_state(eng_a), engine_state(eng_b)
    tc.assertEqual(sorted(sa), sorted(sb))
    for k in sa:
        tc.assertEqual(sa[k], sb[k], f"内部状态 {k} 不一致")


class TestFxMaWarmupFastStartSynthetic(unittest.TestCase):
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
class TestFxMaWarmupFastStartReal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = _load_real()
        if cls.bars is None:
            raise unittest.SkipTest("bars.db 无 XAUUSD 窗口数据")

    def test_real_parity(self):
        # 交易起点 2026-08-01 00:00Z：3 分钟 fine 约 900 根预热（跨 4 个网格），
        # 交易窗 2 天——覆盖 fxma 全链路（信号评估/成交/出场/去重）
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
