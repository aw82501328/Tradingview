# -*- coding: utf-8 -*-
"""出场规则单元测试（出场阶梯重构 2026-09-09，对应 mark-entry SPEC §2.4）

覆盖（与 .cursor/skills/mark-entry/scripts/mark_entry.test.js 的「出场规则」describe 成对同构）：
  - stop_ref_of：支阻位 ± 滑点 / nearSr 错侧重选 / 无正确侧兜底（返回值永不为 None）
  - forming_seg_ready：合并后 ≥5 块门槛 / 末笔方向 / 延伸归零
  - advance_exit_decision：TP1→beStop→stopBe / TP2 顺势（无需 TP1）→ half 后止损=beStop /
    TP3a 顺势 breakPrev / TP3b 逆势 seg5 全平（无 half）/ stopSr / 同拍顺序 half 优先
  - execute_pending_exit：下一开盘成交、half 补 beDone
  - close_trade：lots 盈亏公式（half 加权 / 无 half）

运行：python -m unittest py_chain.test_exit_rules -v
"""

import unittest

from py_chain.mark_entry import stop_ref_of, forming_seg_ready, trend_following_of
from py_chain.backtest import advance_exit_decision, execute_pending_exit, close_trade


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice,
            "span": abs(endPrice - startPrice)}


def bar(time, open_, high, low, close):
    return {"time": time, "open": open_, "high": high, "low": low, "close": close}


def make_pos(direction="short", signalTime=100, entryPrice=4450.0, stopRef=4463.0,
             beStop=4455.0, planDirection="空头空", lots=4):
    """构造持仓 dict（_fill_pending 产出的出场状态机字段子集）。"""
    return {"direction": direction, "signalTime": signalTime, "entryPrice": entryPrice,
            "stopRef": stopRef, "beStop": beStop, "planDirection": planDirection,
            "strategyKey": "wait2Sell", "lots": lots,
            "beDone": False, "halfDone": False, "exits": [], "pendingExit": None,
            "state": "open"}


# 合并块截止时间（升序）：与引擎 _merged_times 同构
T8 = [0, 100, 200, 300, 400, 500, 600, 700]   # 末笔 endTime=300 → 锚点 idx3，其后 4 块 → 就绪
T7 = [0, 100, 200, 300, 400, 500, 600]        # 其后 3 块 → 未就绪


class TestStopRefOf(unittest.TestCase):
    def test_short_upper_sr_plus_slip(self):
        # short：上方最近支阻 4460 + 滑点3 = 4463；下方 4420 不参与
        self.assertEqual(stop_ref_of("short", 4450.0, None, [{"price": 4460.0}, {"price": 4420.0}]), 4463.0)

    def test_near_sr_wrong_side_reselect(self):
        # nearSr=4420 在 short 的错误侧（下方）→ 从 sr_levels 重选 4460+3
        self.assertEqual(stop_ref_of("short", 4450.0, 4420.0, [{"price": 4460.0}]), 4463.0)

    def test_near_sr_correct_side_direct(self):
        # nearSr 已在正确侧 → 直接沿用 + 滑点
        self.assertEqual(stop_ref_of("short", 4450.0, 4465.0, [{"price": 4460.0}]), 4468.0)

    def test_no_correct_side_fallback(self):
        # 无正确侧位 → 兜底 进场价 ± slip_fallback（永不为 None）
        self.assertEqual(stop_ref_of("short", 4450.0, None, [{"price": 4400.0}]), 4460.0)
        self.assertEqual(stop_ref_of("short", 4450.0, None, []), 4460.0)

    def test_long_symmetric(self):
        self.assertEqual(stop_ref_of("long", 4450.0, None, [{"price": 4430.0}, {"price": 4470.0}]), 4427.0)
        self.assertEqual(stop_ref_of("long", 4450.0, None, []), 4440.0)

    def test_custom_slip(self):
        self.assertEqual(stop_ref_of("short", 4450.0, None, [{"price": 4460.0}],
                                     slip_stop=5.0, slip_fallback=20.0), 4465.0)
        self.assertEqual(stop_ref_of("short", 4450.0, None, [], slip_stop=5.0, slip_fallback=20.0), 4470.0)


class TestFormingSegReady(unittest.TestCase):
    def setUp(self):
        # 末笔 up（short 的不利方向），endTime=300
        self.px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]

    def test_four_blocks_after_anchor_not_ready(self):
        self.assertFalse(forming_seg_ready(self.px_bis, T7, is_short=True))

    def test_five_blocks_ready(self):
        self.assertTrue(forming_seg_ready(self.px_bis, T8, is_short=True))

    def test_last_bi_favorable_not_ready(self):
        # 末笔 down（short 的有利方向）→ 其后形成段为不利方向，不触发
        bis = [bi("up", 0, 50, 4440, 4460), bi("down", 50, 300, 4460, 4450)]
        self.assertFalse(forming_seg_ready(bis, T8, is_short=True))

    def test_extension_resets_count(self):
        # 末笔延伸（endTime 300→500）：锚点右移到 idx5，其后仅 2 块 → 归零
        bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 500, 4440, 4455)]
        self.assertFalse(forming_seg_ready(bis, T8, is_short=True))

    def test_long_direction(self):
        # long：末笔须为 down（不利）才可触发
        bis = [bi("up", 0, 50, 4440, 4460), bi("down", 50, 300, 4460, 4450)]
        self.assertTrue(forming_seg_ready(bis, T8, is_short=False))


class TestTrendFollowingOf(unittest.TestCase):
    def test_plan_direction(self):
        self.assertTrue(trend_following_of("多头多"))
        self.assertTrue(trend_following_of("空头空"))
        self.assertFalse(trend_following_of("多头空"))
        self.assertFalse(trend_following_of("空头多"))

    def test_strategy_key_fallback(self):
        self.assertTrue(trend_following_of(None, "wait2Buy"))
        self.assertTrue(trend_following_of(None, "waitSell"))
        self.assertFalse(trend_following_of(None, "wait1Buy"))
        self.assertFalse(trend_following_of(None, "wait1Sell"))


class TestAdvanceExit(unittest.TestCase):
    def test_tp1_moves_stop_to_bestop_then_stopbe(self):
        pos = make_pos()
        # markRes：signalTime=100 后首笔有利方向（short→down）笔完成于 200
        mark_bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]  # 末笔 up，无 down 笔 → tp3a None
        r = advance_exit_decision(pos, 300, bar(300, 4440, 4445, 4435, 4441), mark_bis, px_bis, T7)
        self.assertIsNone(r)  # 仅保本（状态迁移）
        self.assertTrue(pos["beDone"])
        self.assertEqual(pos["exits"][0]["type"], "breakeven")
        # 之后盘中破坏 beStop=4455（非进场价）→ stopBe
        r = advance_exit_decision(pos, 400, bar(400, 4450, 4456, 4448, 4452), mark_bis, px_bis, T7)
        self.assertEqual(r, "stopBe")
        tr = execute_pending_exit(pos, bar(500, 4454, 4458, 4450, 4452))
        self.assertEqual(tr["exitType"], "stopBe")
        self.assertEqual(tr["exitPrice"], 4454.0)
        # 无 half → pnl = (4454-4450)*(-1)*4 = -16
        self.assertEqual(tr["pnl"], -16.0)

    def test_tp2_trend_half_without_tp1(self):
        pos = make_pos()
        mark_bis = [bi("up", 0, 500, 4440, 4455)]  # 无 signalTime 后的有利方向笔 → TP1 未触发
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444), mark_bis, px_bis, T8)
        self.assertEqual(r, "half")  # 顺势 seg5，无需 TP1
        self.assertTrue(pos["halfDone"])
        tr = execute_pending_exit(pos, bar(800, 4440, 4445, 4435, 4442))
        self.assertIsNone(tr)  # half 非终局
        self.assertTrue(pos["beDone"])  # half 补 beDone：剩余半仓止损 = beStop
        # 剩余半仓打掉 beStop → stopBe 终局
        r = advance_exit_decision(pos, 900, bar(900, 4450, 4456, 4448, 4452), mark_bis, px_bis, T8)
        self.assertEqual(r, "stopBe")
        tr = execute_pending_exit(pos, bar(1000, 4454, 4458, 4450, 4452))
        # half@4440 + 终局@4454：pnl = (0.5*(4440-4450) + 0.5*(4454-4450)) * (-1) * 4 = 12
        self.assertEqual(tr["pnl"], 12.0)

    def test_tp2_counter_trend_no_half_close_on_seg5(self):
        pos = make_pos(planDirection="多头空")  # 逆势（多头计划下的空单）
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444), mark_bis, px_bis, T8)
        self.assertEqual(r, "close")  # 逆势：seg5 直接全平，无 half
        self.assertFalse(pos["halfDone"])
        tr = execute_pending_exit(pos, bar(800, 4440, 4445, 4435, 4442))
        self.assertEqual(tr["exitType"], "close")
        # 无 half → pnl = (4440-4450)*(-1)*4 = 40
        self.assertEqual(tr["pnl"], 40.0)

    def test_tp3a_trend_fav_break_prev(self):
        pos = make_pos()
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        # px：down(150→250) 终点 4435 破前一同向 down(0→50) 终点 4440（破前底）；末笔 up
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                  bi("down", 150, 250, 4450, 4435), bi("up", 250, 300, 4435, 4445)]
        r = advance_exit_decision(pos, 300, bar(300, 4440, 4444, 4436, 4442), mark_bis, px_bis, T7)
        self.assertEqual(r, "close")  # 顺势 TP3a（seg5 未就绪也触发）
        tr = execute_pending_exit(pos, bar(400, 4436, 4440, 4430, 4434))
        self.assertEqual(tr["exitType"], "close")

    def test_stop_sr_before_breakeven(self):
        pos = make_pos()  # stopRef=4463，beStop 未生效
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 400, bar(400, 4450, 4464, 4448, 4452), mark_bis, px_bis, T7)
        self.assertEqual(r, "stopSr")
        tr = execute_pending_exit(pos, bar(500, 4462, 4466, 4455, 4460))
        self.assertEqual(tr["exitType"], "stopSr")
        self.assertEqual(tr["pnl"], (4462 - 4450) * (-1) * 4)

    def test_same_beat_half_takes_priority(self):
        # seg5 与 tp3a 同拍满足 → 保本→半平→全平顺序，half 先挂起
        pos = make_pos()
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                  bi("down", 150, 250, 4450, 4435), bi("up", 250, 300, 4435, 4445)]
        r = advance_exit_decision(pos, 300, bar(300, 4440, 4444, 4436, 4442), mark_bis, px_bis, T8)
        self.assertEqual(r, "half")
        self.assertEqual(pos["pendingExit"], "half")


class TestCloseTradeLots(unittest.TestCase):
    def test_with_half(self):
        pos = make_pos()
        pos["exits"].append({"type": "half", "time": 800, "price": 4440.0})
        tr = close_trade(pos, "close", 1000, 4454.0)
        # (0.5*(4440-4450) + 0.5*(4454-4450)) * (-1) * 4 = 12
        self.assertEqual(tr["pnl"], 12.0)

    def test_without_half(self):
        pos = make_pos()
        tr = close_trade(pos, "stopSr", 500, 4462.0)
        self.assertEqual(tr["pnl"], (4462 - 4450) * (-1) * 4)

    def test_lots_multiplier(self):
        pos = make_pos(lots=10)
        tr = close_trade(pos, "close", 1000, 4430.0)
        self.assertEqual(tr["pnl"], (4430 - 4450) * (-1) * 10)

    def test_long_direction(self):
        pos = make_pos(direction="long", entryPrice=4450.0, lots=4)
        pos["exits"].append({"type": "half", "time": 800, "price": 4460.0})
        tr = close_trade(pos, "close", 1000, 4440.0)
        # (0.5*(4460-4450) + 0.5*(4440-4450)) * 1 * 4 = 0
        self.assertEqual(tr["pnl"], 0.0)


if __name__ == "__main__":
    unittest.main()
