# -*- coding: utf-8 -*-
"""bt_runs 单测：compute_summary 汇总口径（对齐前端 renderSummary）与 BtRunStore 读写。

覆盖：pnl=None/0（信号/保本单）不进盈亏与胜负、half 出场计数、四状态行数、
UNIQUE 重名拦截（转 409 的来源）、save/list/get 往返保序、rename/delete。
"""
import os
import sqlite3
import tempfile
import time
import unittest

from py_chain.bt_runs import BtRunStore, build_cfg_summary, compute_summary, default_name


def row(status, pnl=None, exitType=None, exits=None):
    return {"id": 1, "mode": "backtest", "symbol": "OANDA:XAUUSD", "time": 1000,
            "direction": "long", "periodX": "15", "strategyKey": "waitBuy",
            "markRes": "15", "price": 4200.0, "nearSr": 4210.0,
            "fallback": False, "nearEqual": False, "expectBi": False,
            "status": status, "entryTime": 1010, "entryPrice": 4201.0,
            "lots": 4, "stopRef": 4190.0, "state": "closed" if status == "已平仓" else None,
            "exitTime": 1100, "exitPrice": 4230.0, "exitType": exitType,
            "exits": exits or [], "pnl": pnl}


def sample_rows():
    """两盈一亏一保本一浮亏 + 未成交/同向过滤/无 pnl 已平仓，共 8 行。"""
    return [
        row("已平仓", pnl=12.0, exitType="close"),                       # 盈
        row("已平仓", pnl=8.0, exitType="stopSr"),                        # 盈
        row("已平仓", pnl=-4.0, exitType="stopBe"),                       # 亏
        row("已平仓", pnl=0.0, exitType="stopBe"),                        # 保本：不计胜负
        row("已平仓", pnl=None),                                          # 异常行：不进已平仓盈亏
        row("持仓中", pnl=-2.5, exits=[{"type": "half", "time": 1050, "price": 4220.0}]),
        row("信号"),                                                      # 未成交
        row("同向过滤"),                                                  # 互斥过滤
    ]


CFG = {"symbol": "OANDA:XAUUSD", "periods": ["D", "240", "60", "15", "3"],
       "from": "2026-07-02", "from_ts": 1751404800, "data_source": "store",
       "warmup": 60, "lots": 4, "slip_stop": 3.0, "slip_fallback": 10.0,
       "slip_be": 3.0, "diverge_confirm": False, "expect_bi": True,
       "with_30s": False, "signal_mode": "realtime"}


class TestComputeSummary(unittest.TestCase):

    def test_summary_metrics(self):
        s = compute_summary(sample_rows())
        # 已平仓盈亏口径：只算 status=已平仓 且 pnl!=null → 12+8-4+0
        self.assertEqual(s["closed"], 4)
        self.assertEqual(s["win"], 2)
        self.assertEqual(s["lose"], 1)
        self.assertEqual(s["win_rate"], 67)              # round(100*2/3)
        self.assertEqual(s["realized"], 16.0)
        self.assertEqual(s["floating"], -2.5)
        self.assertEqual(s["total"], 13.5)
        self.assertEqual(s["avg_win"], 10.0)             # (12+8)/2
        self.assertEqual(s["avg_loss"], 4.0)             # |-4|/1
        self.assertEqual(s["payoff_ratio"], 2.5)
        self.assertEqual(s["exits"], {"stopBe": 2, "stopSr": 1, "close": 1, "half": 1})
        self.assertEqual(s["rows_total"], 8)
        # cnt_closed 按状态计数（含 pnl=None 的异常行，5），区别于盈亏口径 closed=4
        self.assertEqual((s["cnt_signal"], s["cnt_open"], s["cnt_closed"], s["cnt_filtered"]),
                         (1, 1, 5, 1))

    def test_payoff_ratio_none_without_loss(self):
        s = compute_summary([row("已平仓", pnl=5.0)])
        self.assertIsNone(s["payoff_ratio"])             # 无亏损样本 → 前端显示 ∞
        self.assertEqual(s["win_rate"], 100)             # 1 胜 0 亏 → 胜率 100%

    def test_empty(self):
        s = compute_summary([])
        self.assertEqual(s, {"closed": 0, "win": 0, "lose": 0, "win_rate": None,
                             "avg_win": 0.0, "avg_loss": 0.0, "payoff_ratio": None,
                             "realized": 0.0, "floating": 0.0, "total": 0.0,
                             "exits": {"stopBe": 0, "stopSr": 0, "close": 0, "half": 0},
                             "rows_total": 0, "cnt_signal": 0, "cnt_open": 0,
                             "cnt_closed": 0, "cnt_filtered": 0})

    def test_cfg_summary_and_default_name(self):
        cs = build_cfg_summary(CFG)
        self.assertEqual(cs, {"symbol": "OANDA:XAUUSD",
                              "periods": "D+240+60+15+3", "from": "2026-07-02"})
        self.assertTrue(default_name(CFG).startswith("OANDA:XAUUSD 2026-07-02·"))


class TestBtRunStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BtRunStore(os.path.join(self.tmp.name, "bars.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_list_get_roundtrip(self):
        rows = sample_rows()
        meta = self.store.save("方案A", CFG, rows, worker_state="done")
        self.assertEqual(meta["name"], "方案A")
        self.assertEqual(meta["signal_count"], 8)
        self.assertEqual(meta["cfg"], CFG)
        self.assertEqual(meta["summary"]["closed"], 4)
        self.assertEqual(meta["worker_state"], "done")

        listed = self.store.list()
        self.assertEqual([r["name"] for r in listed], ["方案A"])
        self.assertNotIn("signals", listed[0])           # 列表轻量，不含信号行

        detail = self.store.get(meta["id"])
        self.assertEqual(detail["id"], meta["id"])
        self.assertEqual(detail["signals"], rows)        # 行原样、保序

    def test_list_order_desc(self):
        first = self.store.save("早", CFG, [])
        time.sleep(1.1)                                  # saved_at 秒级，保证可排序
        second = self.store.save("晚", CFG, [])
        self.assertEqual([r["name"] for r in self.store.list()], ["晚", "早"])
        self.assertEqual(self.store.get(first["id"])["name"], "早")

    def test_duplicate_name_rejected(self):
        self.store.save("唯一", CFG, [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save("唯一", CFG, [])
        self.assertEqual(len(self.store.list()), 1)      # 失败写入不残留

    def test_rename(self):
        meta = self.store.save("旧名", CFG, [])
        self.store.save("占位", CFG, [])
        self.assertIsNone(self.store.rename("不存在", "任意"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.rename(meta["id"], "占位")
        updated = self.store.rename(meta["id"], "新名")
        self.assertEqual(updated["name"], "新名")
        self.assertEqual(self.store.get(meta["id"])["name"], "新名")

    def test_delete(self):
        meta = self.store.save("待删", CFG, sample_rows())
        self.assertFalse(self.store.delete("不存在"))
        self.assertTrue(self.store.delete(meta["id"]))
        self.assertIsNone(self.store.get(meta["id"]))
        self.assertEqual(self.store.list(), [])           # 信号行一并删除（get 已 404 源）


if __name__ == "__main__":
    unittest.main()
