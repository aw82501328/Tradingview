# -*- coding: utf-8 -*-
"""data_store 单测：stats() 缺口口径——间隔 > FILL_GAP_SEC（5 天）才计入，
周末/假日长度（≤5 天，含恰好 5 天）的正常休市不计入（7 年深库周末约 400 个，
混入会淹没真缺段）；周期级 updated_at 互不影响（单周期更新不再带亮全品种行）。
"""
import os
import tempfile
import unittest
from unittest import mock

from py_chain import data_store


def bar(t):
    return {"time": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}


class TempDbTestCase(unittest.TestCase):

    def setUp(self):
        # stats()/_connect() 读模块级 DB_PATH：指到临时库，测完恢复
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = data_store.DB_PATH
        data_store.DB_PATH = os.path.join(self._tmp.name, "bars.db")

    def tearDown(self):
        data_store.DB_PATH = self._orig
        self._tmp.cleanup()


class TestStatsGaps(TempDbTestCase):

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


class TestPerPeriodTouch(TempDbTestCase):

    def test_touch_only_written_period(self):
        # _touch_store 每次入库调一次 time.time()：固定时间序列断言各周期独立
        with mock.patch.object(data_store.time, "time", return_value=1000.0):
            data_store.upsert_bars("TEST:TOUCH", "D", [bar(1_800_000_000)])
        with mock.patch.object(data_store.time, "time", return_value=2000.0):
            data_store.upsert_bars("TEST:TOUCH", "60", [bar(1_800_000_000)])
        with mock.patch.object(data_store.time, "time", return_value=3000.0):
            data_store.upsert_bars("TEST:TOUCH", "D", [bar(1_800_000_361)])

        sym = {s["symbol"]: s for s in data_store.stats()["symbols"]}["TEST:TOUCH"]
        self.assertEqual(sym["periods"]["D"]["updated_at"], 3000)
        self.assertEqual(sym["periods"]["60"]["updated_at"], 2000)  # 未再写，不动
        self.assertEqual(sym["updated_at"], 3000)  # 品种级仍取最近一次写入

    def test_delete_res_clears_touch(self):
        data_store.upsert_bars("TEST:DEL", "D", [bar(1_800_000_000)])
        data_store.upsert_bars("TEST:DEL", "60", [bar(1_800_000_000)])

        self.assertTrue(data_store.delete_res("TEST:DEL", "60"))

        sym = {s["symbol"]: s for s in data_store.list_stores()}["TEST:DEL"]
        self.assertNotIn("60", sym["periods"])
        self.assertIn("updated_at", sym["periods"]["D"])  # 剩余周期时间戳保留


if __name__ == "__main__":
    unittest.main()
