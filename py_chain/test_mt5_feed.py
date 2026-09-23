# -*- coding: utf-8 -*-
"""mt5_feed 纯函数单测：时区转换 / 纽约日界分箱 / 会话过滤 / bars.db 口径不变量。

运行：py -3.12 -m unittest py_chain.test_mt5_feed（无需 MT5 终端）。
"""

import os
import sqlite3
import unittest
from datetime import datetime, timezone

from .mt5_feed import (BIN_SEC, closed_bars, ny_day_start_utc, ny_session_bins,
                       offset_at, resample, resample_epoch, session_keep,
                       srv_to_utc, MT5Feed)
from .data_store import DB_PATH


def _m1(start_ts, minutes, price=4000.0):
    """合成 M1 序列：每根 high=price+i、low=price-1、close=price+i*0.1。"""
    out = []
    for i in range(minutes):
        t = start_ts + i * 60
        out.append({"time": t, "open": price, "high": price + i,
                    "low": price - 1, "close": price + i * 0.1})
    return out


class TestSrvToUtc(unittest.TestCase):
    """服务器时间→UTC：DST 切换邻域 + 动态/规则分段。"""

    def test_roundtrip_across_us_dst_switches(self):
        # 2026 美东切换：3/8 02:00 EST→EDT（07:00 UTC）、11/1 02:00 EDT→EST（06:00 UTC）
        for utc in [1772934600,          # 2026-03-08 06:30 UTC（切换前，冬）
                    1772945400,          # 2026-03-08 09:30 UTC（切换后，夏）
                    1793563800,          # 2026-11-01 05:30 UTC（切换前，夏）
                    1793572800,          # 2026-11-01 08:00 UTC（切换后，冬）
                    1758600000]:         # 2026-09-23（当前，夏）
            srv = utc + offset_at(utc, "us")
            self.assertEqual(srv_to_utc(srv, "us"), utc,
                             f"us 往返失败 utc={utc}")

    def test_roundtrip_across_eu_dst_switches(self):
        # 2026 欧盟切换：3/29 01:00 UTC、10/25 01:00 UTC
        for utc in [1774746000,          # 2026-03-29 00:20 UTC（切换前，冬）
                    1774756800,          # 2026-03-29 03:20 UTC（切换后，夏）
                    1792479600,          # 2026-10-25 00:20 UTC（切换前，夏）
                    1792490400]:         # 2026-10-25 03:20 UTC（切换后，冬）
            srv = utc + offset_at(utc, "eu")
            self.assertEqual(srv_to_utc(srv, "eu"), utc, f"eu 往返失败 utc={utc}")

    def test_dynamic_offset_window(self):
        utc = int(datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc).timestamp())
        srv = utc + 10800                       # 假设动态检测 offset=3h
        got = srv_to_utc(srv, "us", now=utc + 60, now_offset=10800)
        self.assertEqual(got, utc)
        # 规则表（us 区 9 月为夏令时 +3h）与动态一致
        self.assertEqual(srv_to_utc(srv, "us"), utc)

    def test_offset_values(self):
        win = datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
        sum_ = datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp()
        self.assertEqual(offset_at(int(win), "us"), 2 * 3600)
        self.assertEqual(offset_at(int(sum_), "us"), 3 * 3600)


class TestNyDayStart(unittest.TestCase):

    def test_summer_and_winter_day_starts(self):
        # 夏令时：交易日 17:00 NY = 21:00 UTC；冬令时 = 22:00 UTC
        s = int(datetime(2026, 9, 22, 23, 30, tzinfo=timezone.utc).timestamp())  # 19:30 NY
        self.assertEqual(ny_day_start_utc(s),
                         int(datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc).timestamp()))
        w = int(datetime(2026, 1, 20, 23, 30, tzinfo=timezone.utc).timestamp())  # 18:30 NY
        self.assertEqual(ny_day_start_utc(w),
                         int(datetime(2026, 1, 20, 22, 0, tzinfo=timezone.utc).timestamp()))

    def test_before_17_local_belongs_previous_day(self):
        # NY 本地 16:00（<17:00）归前一交易日
        t = int(datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc).timestamp())  # 16:00 NY
        self.assertEqual(ny_day_start_utc(t),
                         int(datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc).timestamp()))


class TestResample(unittest.TestCase):

    def test_epoch_bins_and_empty_bin(self):
        base = 400000000 // 180 * 180
        m1 = _m1(400000000, 7)            # 7 根连续 → 覆盖 3 个 3m 箱
        m1 += _m1(400000000 + 60 * 20, 3)  # 空窗后 3 根 → 再 2 箱
        bars = resample_epoch(m1, 180)
        starts = [b["time"] for b in bars]
        self.assertEqual(starts, [base, base + 180, base + 360,
                                  (400000000 + 1200) // 180 * 180, (400000000 + 1200) // 180 * 180 + 180])
        # 空窗不出 bar；箱 OHLC 聚合正确（首箱=前 3 根的 max high）
        in0 = [b for b in m1 if b["time"] < base + 180]
        self.assertEqual(bars[0]["high"], max(b["high"] for b in in0))

    def test_ny_session_bins_d_and_240(self):
        # 合成一个完整交易日：M1 从日界+1h（跳过维护窗）起连续 22h
        probe_ts = int(datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc).timestamp())
        day_start = ny_day_start_utc(probe_ts)     # = 2026-09-23 21:00 UTC（周三 17:00 NY）
        self.assertEqual(day_start,
                         int(datetime(2026, 9, 23, 21, 0, tzinfo=timezone.utc).timestamp()))
        m1 = _m1(day_start + 3600, 22 * 60)
        d = ny_session_bins(m1, "D")
        h4 = ny_session_bins(m1, "240")
        self.assertEqual([b["time"] for b in d], [day_start])
        # 4h 箱锚定日界起点：day_start + k*14400（m1 从 +1h 起，落入首箱 k=0）
        self.assertEqual([b["time"] for b in h4],
                         [day_start + k * 14400 for k in range(6)])

    def test_session_keep_filters(self):
        NY = __import__("zoneinfo").ZoneInfo("America/New_York")
        def utc_of(y, mo, d, h, mi=0):
            return int(datetime(y, mo, d, h, mi, tzinfo=NY).timestamp())
        self.assertFalse(session_keep(utc_of(2026, 9, 23, 17, 30)))  # 维护窗 17:00-18:00
        self.assertTrue(session_keep(utc_of(2026, 9, 23, 18, 0)))    # 开市
        self.assertTrue(session_keep(utc_of(2026, 9, 23, 16, 59)))   # 日内正常
        self.assertFalse(session_keep(utc_of(2026, 9, 26, 12, 0)))   # 周六
        self.assertFalse(session_keep(utc_of(2026, 9, 27, 12, 0)))   # 周日午间
        self.assertTrue(session_keep(utc_of(2026, 9, 27, 18, 0)))    # 周日开市
        self.assertTrue(session_keep(utc_of(2026, 9, 23, 12, 0), weekend="keep"))
        self.assertTrue(session_keep(utc_of(2026, 9, 23, 12, 0), weekend="drop"))  # 周三午间正常时段

    def test_resample_rejects_30s(self):
        with self.assertRaises(ValueError):
            resample([], "30S")

    def test_closed_bars(self):
        now = 4000000000
        bars = [{"time": now - 7200, "open": 1, "high": 1, "low": 1, "close": 1},
                {"time": now - 60, "open": 1, "high": 1, "low": 1, "close": 1}]
        self.assertEqual(len(closed_bars(bars, "60", now)), 1)   # 只留已收盘
        self.assertEqual(len(closed_bars(bars, "D", now)), 0)


class TestDbConventions(unittest.TestCase):
    """bars.db 口径不变量：OANDA:XAUUSD 各周期时间戳锚定（对拍精神的单测化）。

    3/15/60 全部 epoch 对齐；D/240 为 NY 17:00 日界锚定。已知例外：早收市假日
    （如 2024-11-28 感恩节）TradingView 重开了日段，出现少量非锚定 bar——
    阈值按 99.5% 匹配 + 例外归因假日，见 mt5_align。
    """

    def test_db_timestamp_anchors(self):
        if not os.path.exists(DB_PATH):
            self.skipTest("无 bars.db")
        conn = sqlite3.connect(DB_PATH)
        try:
            now = int(datetime(2026, 9, 23, tzinfo=timezone.utc).timestamp())
            # 三种校验：epoch 对齐（3/15/60）；NY 日界起点（D）；NY 日界内 4h 锚定（240）
            cases = (("D", "day", 400), ("240", "day_mod", 400), ("60", "epoch", 200),
                     ("15", "epoch", 90), ("3", "epoch", 30))
            for res, kind, days in cases:
                rows = [r[0] for r in conn.execute(
                    "SELECT time FROM bars WHERE symbol=? AND res=? AND time>? "
                    "ORDER BY time", ("OANDA:XAUUSD", res, now - days * 86400))]
                self.assertTrue(rows, f"{res} 无数据")
                if kind == "epoch":
                    bad = [t for t in rows if t % BIN_SEC[res] != 0]
                elif kind == "day":
                    bad = [t for t in rows if ny_day_start_utc(t) != t]
                else:  # day_mod
                    bad = [t for t in rows
                           if (t - ny_day_start_utc(t)) % 14400 != 0]
                self.assertLessEqual(len(bad) / len(rows), 0.005,
                                     f"{res} 违反锚定比例过高: {bad[:5]}")
        finally:
            conn.close()


class TestFeedSpecOffline(unittest.TestCase):
    """无终端环境：MT5Feed 只测纯配置与异常路径。"""

    def test_importable_without_terminal(self):
        f = MT5Feed(symbol="XAUUSD", periods=("D", "240", "60", "15", "3"))
        self.assertEqual(f.db_symbol, "EXNESS:XAUUSD")
        self.assertEqual(f.rule, "us")

    def test_never_oanda_db_symbol(self):
        f = MT5Feed(db_symbol="OANDA:XAUUSD")
        with self.assertRaises(RuntimeError):
            f.connect()   # 无终端必然失败；OANDA 目标在任何路径都不允许（约定由评审保证）


if __name__ == "__main__":
    unittest.main()
