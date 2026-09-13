# -*- coding: utf-8 -*-
"""data_store 单测：stats() 缺口口径——间隔 > FILL_GAP_SEC（5 天）才计入，
周末/假日长度（≤5 天，含恰好 5 天）的正常休市不计入（7 年深库周末约 400 个，
混入会淹没真缺段）。
"""
import os
import tempfile
import unittest

from py_chain import data_store


def bar(t):
    return {"time": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}


class TestStatsGaps(unittest.TestCase):

    def setUp(self):
        # stats()/_connect() 读模块级 DB_PATH：指到临时库，测完恢复
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = data_store.DB_PATH
        data_store.DB_PATH = os.path.join(self._tmp.name, "bars.db")

    def tearDown(self):
        data_store.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_gap_threshold_5d(self):
        day = 86400
        t0 = 1_800_000_000  # 任意时刻起按日刻度排布
        times = (
            [t0 + i * day for i in range(5)]       # 周一~周五连续
            + [t0 + 7 * day]                        # 下周一：3 天周末间隔，不计入
            + [t0 + 9 * day]                        # 2 天假日长度间隔，不计入
            + [t0 + 16 * day, t0 + 17 * day]        # 9→16 相隔 7 天：真缺段，计入
            + [t0 + 22 * day]                       # 恰好 5 天：边界不计入
        )
        data_store.upsert_bars("TEST:GAP", "D", [bar(t) for t in times])

        st = data_store.stats()["symbols"][0]["periods"]["D"]

        self.assertEqual(len(st["gaps"]), 1)
        self.assertEqual(st["gaps"][0]["from"], t0 + 9 * day)
        self.assertEqual(st["gaps"][0]["to"], t0 + 16 * day)
        self.assertEqual(st["gaps"][0]["days"], 7.0)


if __name__ == "__main__":
    unittest.main()
