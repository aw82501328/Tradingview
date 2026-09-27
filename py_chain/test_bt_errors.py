# -*- coding: utf-8 -*-
"""bt_errors 单测：derive_source_key 去重键口径与 BtErrorStore 读写。

覆盖：live 内容键拼接 / None 渲染空串 / 缺关键字段拦截、run 键格式、
add/list 往返（默认 pending、row 快照解析回 dict、倒序）、UNIQUE 重复拦截
（转 409 的来源）且失败写入不残留、set_status 往返与未知 id、delete、
AUTOINCREMENT 删除后 id 不复用。
"""
import os
import sqlite3
import tempfile
import time
import unittest

from py_chain.bt_errors import BtErrorStore, derive_source_key


def sample_row(**over):
    r = {"id": 7, "mode": "backtest", "symbol": "OANDA:XAUUSD", "time": 1000,
         "direction": "long", "periodX": "15", "strategyKey": "waitBuy",
         "markRes": "15", "price": 4200.0, "nearSr": 4210.0,
         "status": "已平仓", "entryTime": 1010, "entryPrice": 4201.0,
         "lots": 4, "pnl": 12.0, "exits": []}
    r.update(over)
    return r


class TestDeriveSourceKey(unittest.TestCase):

    def test_live_content_key(self):
        # 与 SignalLog._row_key 同口径的六字段内容键
        self.assertEqual(
            derive_source_key("live", sample_row()),
            "live|backtest|OANDA:XAUUSD|1000|15|long|waitBuy")

    def test_live_none_fields_render_empty(self):
        k1 = derive_source_key("live", sample_row(nearSr=None))
        self.assertEqual(k1, "live|backtest|OANDA:XAUUSD|1000|15|long|waitBuy")
        # 行内多余字段（nearSr 等）不进键；None 的键字段渲染空串
        k2 = derive_source_key("live", sample_row(mode=None))
        self.assertEqual(k2, "live||OANDA:XAUUSD|1000|15|long|waitBuy")

    def test_live_missing_time_raises(self):
        with self.assertRaises(ValueError):
            derive_source_key("live", sample_row(time=None))

    def test_run_key_format(self):
        self.assertEqual(
            derive_source_key("run", sample_row(), run_id="20260926-101010-ab12"),
            "run|20260926-101010-ab12|7")

    def test_run_missing_run_id_or_row_id_raises(self):
        with self.assertRaises(ValueError):
            derive_source_key("run", sample_row())                    # 缺 run_id
        with self.assertRaises(ValueError):
            derive_source_key("run", sample_row(id=None), run_id="r1")
        with self.assertRaises(ValueError):
            derive_source_key("run", sample_row(id=0), run_id="r1")   # 非正整数

    def test_unknown_source_type_raises(self):
        with self.assertRaises(ValueError):
            derive_source_key("other", sample_row())


class TestBtErrorStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BtErrorStore(os.path.join(self.tmp.name, "bars.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_list_roundtrip(self):
        row = sample_row()
        e = self.store.add("live", derive_source_key("live", row), row,
                           copy_text="#3 信号时间：…")
        self.assertTrue(e["id"])                       # AUTOINCREMENT 回填
        self.assertEqual(e["status"], "pending")       # 默认待处理
        self.assertEqual(e["symbol"], "OANDA:XAUUSD")  # 冗余列从行快照提取
        self.assertEqual(e["signal_time"], 1000)
        entries = self.store.list()
        self.assertEqual(len(entries), 1)
        got = entries[0]
        self.assertEqual(got["id"], e["id"])
        self.assertEqual(got["source_key"], e["source_key"])
        self.assertEqual(got["row"], row)              # row_json 解析回 dict
        self.assertEqual(got["copy_text"], "#3 信号时间：…")

    def test_list_desc_by_created_at(self):
        for i, t in enumerate((1000, 2000, 3000)):
            row = sample_row(time=t, strategyKey=f"k{i}")
            self.store.add("live", derive_source_key("live", row), row)
        # 倒序：最后加入的（time=3000）在最前
        self.assertEqual([e["signal_time"] for e in self.store.list()],
                         [3000, 2000, 1000])

    def test_duplicate_source_key_rejected(self):
        row = sample_row()
        key = derive_source_key("live", row)
        self.store.add("live", key, row)
        with self.assertRaises(sqlite3.IntegrityError):   # 调用方转 409
            self.store.add("live", key, sample_row(pnl=-5.0))
        self.assertEqual(len(self.store.list()), 1)       # 失败写入不残留

    def test_set_status_roundtrip_and_unknown(self):
        row = sample_row()
        e = self.store.add("live", derive_source_key("live", row), row)
        self.assertIsNone(self.store.set_status(99999, "done"))   # 未知 id
        with self.assertRaises(ValueError):
            self.store.set_status(e["id"], "bad")                 # 非法状态
        done = self.store.set_status(e["id"], "done")
        self.assertEqual(done["status"], "done")
        back = self.store.set_status(e["id"], "pending")          # 可回切
        self.assertEqual(back["status"], "pending")

    def test_delete_and_id_not_reused(self):
        e = self.store.add("live", derive_source_key("live", sample_row()), sample_row())
        self.assertTrue(self.store.delete(e["id"]))
        self.assertFalse(self.store.delete(e["id"]))              # 再删返回 False
        row2 = sample_row(time=2000)
        e2 = self.store.add("live", derive_source_key("live", row2), row2)
        self.assertGreater(e2["id"], e["id"])                     # AUTOINCREMENT 不复用
        self.assertEqual(len(self.store.list()), 1)


if __name__ == "__main__":
    unittest.main()
