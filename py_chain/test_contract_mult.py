# -*- coding: utf-8 -*-
"""合约乘数（2026-09-23 统一 MT4/MT5 经纪商口径：1 手 = 0.01 标准手）单元测试。

覆盖：contract_mult_of 各品种/未知/None；引擎 contract_mult 成交时快照进 trade 行、
close_trade / _finish 盈亏 = 价差 × 方向 × lots × mult；缺省 1.0 与旧口径一致。
配套：test_exit_rules.TestCloseTradeLots（close_trade 公式细拆）、
test_param_center（按品种手数 lots_of 解析）、test_bt_runs（存档半平重算乘 mult）。

运行：python -m unittest py_chain.test_contract_mult -v
"""

import unittest

from py_chain.mark_entry import contract_mult_of, symbol_suffix
from py_chain.backtest import BacktestEngine, close_trade


def bar(time, open_, high, low, close):
    return {"time": time, "open": open_, "high": high, "low": low, "close": close}


class TestContractMultOf(unittest.TestCase):
    def test_symbol_suffix(self):
        self.assertEqual(symbol_suffix("OANDA:XAUUSD"), "XAUUSD")
        self.assertEqual(symbol_suffix("xauusd"), "XAUUSD")
        self.assertEqual(symbol_suffix("XAUUSD"), "XAUUSD")
        self.assertIsNone(symbol_suffix(None))

    def test_known_symbols(self):
        self.assertEqual(contract_mult_of("OANDA:XAUUSD"), 1.0)     # 100盎司 → 1.0
        self.assertEqual(contract_mult_of("OANDA:XAGUSD"), 50.0)    # 5000盎司 → 50.0
        self.assertEqual(contract_mult_of("TVC:USOIL"), 10.0)       # 1000桶 → 10.0（2026-09-23 实单核对修正）
        self.assertEqual(contract_mult_of("BITSTAMP:BTCUSD"), 0.01)  # 1 BTC → 0.01
        self.assertEqual(contract_mult_of("FX:NAS100"), 0.01)        # 1合约 → 0.01
        # 无前缀/小写同口径
        self.assertEqual(contract_mult_of("xagusd"), 50.0)
        self.assertEqual(contract_mult_of("USOIL"), 10.0)

    def test_unknown_and_none_default_one(self):
        # 未知/None → 1.0：与升级前纯价差口径一致（旧行为保持）
        self.assertEqual(contract_mult_of("FOO:BAR"), 1.0)
        self.assertEqual(contract_mult_of(None), 1.0)
        self.assertEqual(contract_mult_of(""), 1.0)


class TestEngineContractMult(unittest.TestCase):
    """引擎层：contract_mult 快照与结算（合成最小 bars，不跑全链路信号）。"""

    def _engine(self, lots=2, contract_mult=50.0):
        bars = {"3": [bar(0, 100.0, 101.0, 99.0, 100.0),
                      bar(60, 100.0, 102.0, 98.0, 101.0)]}
        return BacktestEngine(bars, periods=["3"], lots=lots, contract_mult=contract_mult)

    def _fill_one(self, eng):
        """成交一笔合成信号（confirm 口径：nextOpen 成交），返回 trade dict。"""
        trades = []
        pending = [{"direction": "short", "time": 60, "periodX": "3", "markRes": "3",
                    "strategyKey": "wait2Sell", "price": 100.0, "nearSr": None}]
        eng._fill_pending(trades, pending, nextOpen=101.0, nextTime=120,
                          stats={"executed": 0, "suppressed": 0})
        self.assertEqual(len(trades), 1)
        return trades[0]

    def test_fill_snapshots_mult(self):
        eng = self._engine(lots=2, contract_mult=50.0)
        tr = self._fill_one(eng)
        self.assertEqual(tr["lots"], 2)
        self.assertEqual(tr["mult"], 50.0)   # 白银口径快照

    def test_close_trade_scales_by_mult(self):
        eng = self._engine(lots=2, contract_mult=50.0)
        tr = self._fill_one(eng)             # entry=101.0（nextOpen，short）
        tr = close_trade(tr, "close", 1000, 95.0)
        self.assertAlmostEqual(tr["pnl"], (95.0 - 101.0) * (-1) * 2 * 50.0)

    def test_finish_mark_to_market_scales(self):
        eng = self._engine(lots=2, contract_mult=50.0)
        # 未平仓按最新收盘（末根 close=101）mark-to-market × lots × mult
        trades = [{"direction": "long", "entryPrice": 100.0, "lots": 2, "mult": 50.0,
                   "exits": [], "state": "open"}]
        res = eng._finish([], trades, {})
        self.assertAlmostEqual(res["trades"][0]["pnl"], (101.0 - 100.0) * 1 * 2 * 50.0)

    def test_default_mult_is_one(self):
        # 不传 contract_mult → 1.0：旧调用方（既有测试/监控旧路径）结果不变
        eng = self._engine(lots=4, contract_mult=1.0)
        self.assertEqual(eng.contract_mult, 1.0)
        tr = self._fill_one(eng)
        self.assertEqual(tr["mult"], 1.0)
        tr = close_trade(tr, "close", 1000, 95.0)
        self.assertAlmostEqual(tr["pnl"], (95.0 - 101.0) * (-1) * 4 * 1.0)


if __name__ == "__main__":
    unittest.main()
