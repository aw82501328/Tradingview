# -*- coding: utf-8 -*-
"""SignalLog tradeNo 锚定回填单元测试（2026-10-28）。

动因（run 20261028-114131-fce5 #44 实例）：同锚点第二次尝试信号与第一笔行键全同
（signalTime/periodX/direction/strategyKey/chain），事件序
signal(16)→fill(#44)→signal(17 同键)→suppressed(17) 后 _key_to_idx 键被 pop，
#44 的出场 fill_exit 按键找不到行 → 行永远停在「持仓中」（引擎侧 04-24 01:03
stopBe 已平、pnl 0）。修复后 tradeNo 锚定索引优先、行键回退。

字段值取自该 run 的 journal 原始事件（bt_chanA_s3of4_*.ndjson 8079-8102 行），
非构造数据。"""

import unittest

from . import webapp


def _sig(time, **kw):
    base = {"symbol": "OANDA:XAUUSD", "time": time, "periodX": "15",
            "direction": "long", "strategyKey": "2Buy", "chain": "A",
            "markRes": "3", "price": 4716.545, "nearSr": 4713.7325,
            "pointType": "2买", "pointStrong": True}
    base.update(kw)
    return base


def _fill(trade_no, time, **kw):
    base = {"symbol": "OANDA:XAUUSD", "chain": "A", "tradeNo": trade_no,
            "signalTime": time, "periodX": "15", "direction": "long",
            "strategyKey": "2Buy", "markRes": "3", "signalPrice": 4716.545,
            "nearSr": 4713.7325, "entryTime": 1776960000,
            "entryPrice": 4720.685, "fillMode": "confirm", "lots": 4,
            "state": "open", "pnl": None}
    base.update(kw)
    return base


class TradeNoAnchorTests(unittest.TestCase):
    SYM = "OANDA:XAUUSD"

    def setUp(self):
        self.log = webapp.SignalLog()
        # 该 run 的信号/第二尝试/成交/终局时间（UTC+8 2026-04-23 23:54 / 04-24 00:00 / 01:03）
        self.t_sig = 1776959640
        self.t_entry = 1776960000
        self.t_exit = 1776963780

    def fill(self, tr, strategy="chan_v1"):
        return self.log.fill_trade("backtest", tr, symbol=self.SYM, strategy=strategy)

    def test_bug_sequence_exit_lands_on_filled_row(self):
        """run fce5 #44 原始事件序：出场必须回到已成交行，不再卡「持仓中」。"""
        log = self.log
        log.append_signal("backtest", _sig(self.t_sig), symbol=self.SYM, strategy="chan_v1")
        self.fill(_fill(44, self.t_sig))
        # 同键第二尝试信号（journal id 17）：append 新行并顶走键映射
        log.append_signal("backtest", _sig(self.t_sig), symbol=self.SYM, strategy="chan_v1")
        # 第二尝试被同向互斥过滤：隐藏的是新行（未成交），键映射被 pop
        log.fill_suppressed("backtest", {"signalTime": self.t_sig, "periodX": "15",
                                         "direction": "long", "strategyKey": "2Buy",
                                         "chain": "A"},
                            symbol=self.SYM, strategy="chan_v1")
        # #44 终局（stopBe 保本位触价）：键已不在映射里，须靠 tradeNo 找回成交行
        row = log.fill_exit("backtest", _fill(44, self.t_sig, exitTime=self.t_exit,
                                              exitPrice=4720.685, exitType="stopBe",
                                              pnl=0.0,
                                              exits=[{"type": "stopBe", "time": self.t_exit,
                                                      "price": 4720.685, "lots": 4}]),
                            symbol=self.SYM, strategy="chan_v1")
        self.assertIsNotNone(row)
        rows = log.list(mode="backtest")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "已平仓")
        self.assertEqual(rows[0]["state"], "closed")
        self.assertEqual(rows[0]["exitType"], "stopBe")
        self.assertEqual(rows[0]["exitTime"], self.t_exit)
        self.assertEqual(rows[0]["pnl"], 0.0)
        self.assertEqual(rows[0]["tradeNo"], 44)
        self.assertEqual(rows[0]["entryPrice"], 4720.685)

    def test_fill_guard_same_key_different_trade_appends_new_row(self):
        """行键命中的行已属另一笔交易：不覆盖，另起新行（键冲突在 fill 侧的镜像）。"""
        log = self.log
        log.append_signal("backtest", _sig(self.t_sig), symbol=self.SYM, strategy="chan_v1")
        r1 = self.fill(_fill(1, self.t_sig))
        r2 = self.fill(_fill(2, self.t_sig, entryPrice=4700.0))
        self.assertIsNot(r1["id"], r2["id"])
        rows = log.list(mode="backtest")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["tradeNo"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["entryPrice"], 4720.685)

    def test_exit_guard_does_not_write_other_trades_row(self):
        """出场 tr 的 tradeNo 与键命中行归属不符：放弃回填，不串写他人行。"""
        log = self.log
        log.append_signal("backtest", _sig(self.t_sig), symbol=self.SYM, strategy="chan_v1")
        self.fill(_fill(1, self.t_sig))
        row = log.fill_exit("backtest", _fill(2, self.t_sig, exitTime=self.t_exit,
                                              exitPrice=4720.685, exitType="stopSr", pnl=-5.0),
                            symbol=self.SYM, strategy="chan_v1")
        self.assertIsNone(row)
        r = log.list(mode="backtest")[0]
        self.assertEqual(r["status"], "持仓中")   # 行未被他人出场污染
        self.assertIsNone(r["exitType"])

    def test_suppressed_keeps_filled_row_visible(self):
        """同键 suppressed 只作废键映射：已成交的行不隐藏（过滤属于重复尝试信号）。"""
        log = self.log
        log.append_signal("backtest", _sig(self.t_sig), symbol=self.SYM, strategy="chan_v1")
        self.fill(_fill(5, self.t_sig))
        ret = log.fill_suppressed("backtest", {"signalTime": self.t_sig, "periodX": "15",
                                               "direction": "long", "strategyKey": "2Buy",
                                               "chain": "A"},
                                  symbol=self.SYM, strategy="chan_v1")
        self.assertIsNone(ret)
        rows = log.list(mode="backtest")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "持仓中")
        # 出场仍可回填（tradeNo 索引在键被 pop 后依然有效）
        row = log.fill_exit("backtest", _fill(5, self.t_sig, exitTime=self.t_exit,
                                              exitPrice=4720.685, exitType="stopBe", pnl=0.0),
                            symbol=self.SYM, strategy="chan_v1")
        self.assertEqual(row["status"], "已平仓")

    def test_open_pushback_updates_in_place_after_key_popped(self):
        """期末未平仓回推（done.open → fill_trade）：键失效后按 tradeNo 原位更新。"""
        log = self.log
        log.append_signal("backtest", _sig(self.t_sig), symbol=self.SYM, strategy="chan_v1")
        self.fill(_fill(7, self.t_sig))
        log.fill_suppressed("backtest", {"signalTime": self.t_sig, "periodX": "15",
                                         "direction": "long", "strategyKey": "2Buy",
                                         "chain": "A"},
                            symbol=self.SYM, strategy="chan_v1")
        row = self.fill(_fill(7, self.t_sig, pnl=91.0,
                              exits=[{"type": "trailStop", "lots": 1.0}]))
        rows = log.list(mode="backtest")
        self.assertEqual(len(rows), 1)            # 原位更新，不追加重复行
        self.assertEqual(rows[0]["pnl"], 91.0)

    def test_segpar_same_tradeno_different_signaltime(self):
        """分段并行：独立引擎 tradeNo 都从 1 编号，同 tradeNo 不同 signalTime 的
        两笔互不串写（signalTime 入 tradeNo 索引键）。"""
        log = self.log
        log.append_signal("backtest", _sig(self.t_sig), symbol=self.SYM, strategy="chan_v1")
        log.append_signal("backtest", _sig(self.t_sig + 600), symbol=self.SYM, strategy="chan_v1")
        self.fill(_fill(44, self.t_sig))
        self.fill(_fill(44, self.t_sig + 600, entryPrice=4700.0))
        row = log.fill_exit("backtest", _fill(44, self.t_sig, exitTime=self.t_exit,
                                              exitPrice=4720.685, exitType="stopBe", pnl=0.0),
                            symbol=self.SYM, strategy="chan_v1")
        self.assertEqual(row["entryPrice"], 4720.685)
        rows = log.list(mode="backtest")
        self.assertEqual([r["status"] for r in rows], ["已平仓", "持仓中"])
        self.assertEqual(rows[1]["exitType"], None)

    def test_clear_rebuilds_tradeno_index(self):
        """clear 按策略清行后，幸存行的 tradeNo 回填不受被清行残留索引影响。"""
        log = self.log
        log.append_signal("backtest", _sig(self.t_sig), symbol=self.SYM, strategy="chan_v1")
        self.fill(_fill(1, self.t_sig))
        log.append_signal("backtest", _sig(self.t_sig), symbol=self.SYM, strategy="fxma_v1")
        self.fill(_fill(1, self.t_sig), strategy="fxma_v1")
        log.clear("backtest", strategy="chan_v1")
        row = log.fill_exit("backtest", _fill(1, self.t_sig, exitTime=self.t_exit,
                                              exitPrice=4720.685, exitType="stopSr", pnl=3.0),
                            symbol=self.SYM, strategy="fxma_v1")
        self.assertIsNotNone(row)
        self.assertEqual(row["strategy"], "fxma_v1")
        self.assertEqual(row["status"], "已平仓")


if __name__ == "__main__":
    unittest.main()
