# -*- coding: utf-8 -*-
"""mt5_broker 单测：纯函数（归一化/clamp/filling）+ MockBroker 生命周期与失败注入。

运行：py -3.12 -m unittest py_chain.test_mt5_broker（无需 MT5 终端）。
"""

import unittest

from .mt5_broker import (MockBroker, clamp_sl, filling_candidates,
                         normalize_price, normalize_volume, OrderResult)


SPEC = dict(MockBroker.DEFAULT_SPEC)


class TestPure(unittest.TestCase):

    def test_normalize_volume(self):
        self.assertEqual(normalize_volume(0.02, SPEC), 0.02)
        self.assertEqual(normalize_volume(0.019, SPEC), 0.01)      # 向下取整
        self.assertEqual(normalize_volume(0.2 + 1e-9, SPEC), 0.2)  # 浮点噪声免疫
        self.assertEqual(normalize_volume(0.005, SPEC), 0.01)      # 夹到 min
        spec = dict(SPEC, volume_max=0.05)
        self.assertEqual(normalize_volume(1.0, spec), 0.05)        # 夹到 max

    def test_normalize_price(self):
        self.assertEqual(normalize_price(2650.567, SPEC), 2650.57)
        self.assertEqual(normalize_price(2650.561, SPEC), 2650.56)

    def test_clamp_sl(self):
        spec = dict(SPEC, stops_level=100)          # 100点=1.00
        self.assertEqual(clamp_sl(2650.0, 2649.5, "long", spec), 2649.0)   # 距离不足→clamp
        self.assertEqual(clamp_sl(2650.0, 2640.0, "long", spec), 2640.0)   # 足够→原样
        self.assertEqual(clamp_sl(2650.0, 2650.5, "short", spec), 2651.0)
        self.assertEqual(clamp_sl(2650.0, 2649.5, "long", SPEC), 2649.5)   # stops=0 不 clamp

    def test_filling_candidates(self):
        self.assertEqual(filling_candidates(1), [0])      # 仅 FOK
        self.assertEqual(filling_candidates(2), [1])      # 仅 IOC
        self.assertEqual(filling_candidates(3), [0, 1])   # FOK→IOC
        self.assertEqual(filling_candidates(0), [2])      # 兜底 RETURN


class TestMockLifecycle(unittest.TestCase):
    """进场 → SL 盘中触发 → 半平 → 改SL clamp → 失败注入。"""

    def setUp(self):
        self.b = MockBroker(fill_policy=lambda d, ref: ref)   # 零滑点

    def test_market_order_and_position(self):
        r = self.b.market_order("long", 0.02, sl=2640.0, comment="chai_long_1")
        self.assertTrue(r.ok)
        self.assertEqual(r.deal_volume, 0.02)
        self.assertEqual(r.deal_price, 4000.20)               # 按 ask 成交
        ps = self.b.positions()
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0]["sl"], 2640.0)

    def test_sl_hit_via_inject_bar(self):
        r = self.b.market_order("long", 0.02, sl=2640.0)
        self.b.inject_bar({"time": 1, "high": 4001.0, "low": 2639.0})  # 下破 SL
        self.assertEqual(self.b.positions(), [])
        deals = self.b.deals_since(0)
        self.assertEqual(len(deals), 2)                       # 进场+止损出场
        self.assertEqual(deals[1]["price"], 2640.0)           # 按 SL 价成交
        self.assertEqual(deals[1]["volume"], 0.02)

    def test_sl_not_hit(self):
        self.b.market_order("short", 0.02, sl=4010.0)
        self.b.inject_bar({"time": 1, "high": 4005.0, "low": 3998.0})
        self.assertEqual(len(self.b.positions()), 1)

    def test_partial_close(self):
        r = self.b.market_order("long", 0.02, sl=2640.0)
        c = self.b.close_position(r.position_ticket, volume=0.01)
        self.assertTrue(c.ok)
        self.assertEqual(c.deal_volume, 0.01)
        ps = self.b.positions()
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0]["volume"], 0.01)               # 剩半仓
        c2 = self.b.close_position(r.position_ticket)         # 全平
        self.assertTrue(c2.ok)
        self.assertEqual(self.b.positions(), [])

    def test_modify_sl(self):
        r = self.b.market_order("long", 0.02, sl=2640.0)
        m = self.b.modify_sl(r.position_ticket, 3995.0)
        self.assertTrue(m.ok)
        self.assertEqual(self.b.positions()[0]["sl"], 3995.0)

    def test_modify_sl_clamp_with_stops(self):
        b = MockBroker()
        b._spec = dict(SPEC, stops_level=100)                 # 1.00 距离
        b.set_quote(4000.00, 4000.20)
        r = b.market_order("long", 0.02)
        m = b.modify_sl(r.position_ticket, 4000.0, clamp=True)  # 距离=bid-4000=0 → clamp
        self.assertTrue(m.ok)
        self.assertEqual(b.positions()[0]["sl"], 3999.0)      # bid-1.00

    def test_slippage_policy(self):
        b = MockBroker(fill_policy=lambda d, ref: ref + (0.30 if d == "long" else -0.30))
        r = b.market_order("long", 0.01)
        self.assertEqual(r.deal_price, 4000.50)               # ask+0.30 滑点

    def test_fail_queue_retcode(self):
        self.b.fail_queue = [10030, 10018]
        r = self.b.market_order("long", 0.01)
        self.assertFalse(r.ok)
        self.assertEqual(r.retcode, 10030)
        r2 = self.b.market_order("long", 0.01)
        self.assertFalse(r2.ok)
        self.assertEqual(r2.retcode, 10018)
        r3 = self.b.market_order("long", 0.01)                # 队列耗尽→正常
        self.assertTrue(r3.ok)

    def test_fail_queue_exception(self):
        self.b.fail_queue = [ConnectionError("IPC 断开")]
        with self.assertRaises(ConnectionError):
            self.b.market_order("long", 0.01)

    def test_close_missing_position(self):
        r = self.b.close_position(999999)
        self.assertFalse(r.ok)
        self.assertEqual(r.retcode, 10036)


if __name__ == "__main__":
    unittest.main()
