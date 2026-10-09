import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志

# -*- coding: utf-8 -*-
"""出场规则单元测试（出场阶梯重构 2026-09-09，对应 mark-entry SPEC §2.4）

覆盖（与 .cursor/skills/mark-entry/scripts/mark_entry.test.js 的「出场规则」describe 成对同构）：
  - stop_ref_of：支阻位 ± 滑点 / nearSr 错侧重选 / 最大止损硬上限（返回值永不为 None）
  - forming_seg_ready：合并后 ≥5 块门槛 / 末笔方向 / 延伸归零
  - forming_break_ref：预期过前高参照（末笔不利才有形成段 / 无同向笔 None）
  - advance_exit_decision：TP1→beStop→stopBe / TP2 顺势（检测周期有利方向够笔，无需 TP1）→ half 后止损=beStop /
    TP3a 顺势 breakPrev / TP3b 逆势 seg5 全平（无 half）/ stopSr / 同拍顺序 half 优先 /
    预期口径（2026-10-09 严进宽出，默认开）：检测够笔=末笔不利+形成段够块、
    过高低点=收盘K极值越前一同向笔端点，exitExpectOn=False 回退确认笔旧口径
  - 跟踪止盈双参照（2026-10-09 exitTrailSrc）：detect=检测周期 3/类3/4/类4 结构点流
    （默认：保本后出3买提损至3买位置，跌破记 trailStop）；mark=背驰周期信号流
    同向3类点（wait3Buy/waitBuy/…，旧口径）；门控保本（2026-10-09）——beDone 前
    不接管止损/不提损，参照点自保本当拍起算不追溯
  - 最大止损硬上限兜底（2026-10-09）：止损类部分平仓 latch 后剩余手数由 maxLoss
    全平兜底（记 stopSr，永不 latch），单手亏损封顶最大止损
  - execute_pending_exit：下一开盘成交、half 补 beDone
  - close_trade：lots 盈亏公式（half 加权 / 无 half）

运行：python -m unittest py_chain.test_exit_rules -v
"""

import unittest

from py_chain.mark_entry import (stop_ref_of, forming_seg_ready, forming_break_ref,
                                 trend_following_of, EXIT_MODE_DEFAULTS)
from py_chain.backtest import (advance_exit_decision, execute_pending_exit,
                               close_trade, BacktestEngine)


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
        # short：上方最近支阻 4460+3=4463，最大止损 4450+10=4460 → 收到 4460
        self.assertEqual(stop_ref_of("short", 4450.0, None, [{"price": 4460.0}, {"price": 4420.0}]), 4460.0)

    def test_near_sr_wrong_side_reselect(self):
        # nearSr=4420 在 short 的错误侧（下方）→ 从 sr_levels 重选 4460+3，再夹到 4460
        self.assertEqual(stop_ref_of("short", 4450.0, 4420.0, [{"price": 4460.0}]), 4460.0)

    def test_near_sr_correct_side_direct(self):
        # nearSr 已在正确侧 → 4465+3=4468，最大止损 4460 → 收到 4460
        self.assertEqual(stop_ref_of("short", 4450.0, 4465.0, [{"price": 4460.0}]), 4460.0)

    def test_no_correct_side_fallback(self):
        # 无正确侧位 → 止损 = 进场价 ± 最大止损（永不为 None）
        self.assertEqual(stop_ref_of("short", 4450.0, None, [{"price": 4400.0}]), 4460.0)
        self.assertEqual(stop_ref_of("short", 4450.0, None, []), 4460.0)

    def test_long_symmetric(self):
        # long：4430−3=4427，最大止损 4440 → 抬到 4440；无正确侧同为 4440
        self.assertEqual(stop_ref_of("long", 4450.0, None, [{"price": 4430.0}, {"price": 4470.0}]), 4440.0)
        self.assertEqual(stop_ref_of("long", 4450.0, None, []), 4440.0)

    def test_custom_slip(self):
        # 最大止损 20：4460+5=4465 < 4470 → 保持支阻；无正确侧 = 4470
        self.assertEqual(stop_ref_of("short", 4450.0, None, [{"price": 4460.0}],
                                     slip_stop=5.0, slip_fallback=20.0), 4465.0)
        self.assertEqual(stop_ref_of("short", 4450.0, None, [], slip_stop=5.0, slip_fallback=20.0), 4470.0)

    def test_sr_closer_than_max_loss(self):
        # 支阻更近：空 4455+3=4458 < 4460 → 保持；多 4445−3=4442 > 4440 → 保持
        self.assertEqual(stop_ref_of("short", 4450.0, None, [{"price": 4455.0}]), 4458.0)
        self.assertEqual(stop_ref_of("long", 4450.0, None, [{"price": 4445.0}]), 4442.0)

    def test_atr_component(self):
        # ATR 分量：有效滑点 = 固定值 + 系数×ATR，再按有效最大止损夹紧
        # 支阻 4460+(3+0.5×2)=4464，最大止损 4450+(10+1×2)=4462 → 4462
        self.assertEqual(stop_ref_of("short", 4450.0, None, [{"price": 4460.0}],
                                     slip_stop=3.0, slip_fallback=10.0,
                                     atr=2.0, k_stop=0.5, k_fallback=1.0), 4462.0)
        # 无正确侧：4450 + (10 + 1×2) = 4462
        self.assertEqual(stop_ref_of("short", 4450.0, None, [],
                                     slip_stop=3.0, slip_fallback=10.0,
                                     atr=2.0, k_stop=0.5, k_fallback=1.0), 4462.0)
        # nearSr 4465+(3+1×2)=4470，最大止损 4450+10=4460 → 4460
        self.assertEqual(stop_ref_of("short", 4450.0, 4465.0, [{"price": 4460.0}],
                                     slip_stop=3.0, slip_fallback=10.0,
                                     atr=2.0, k_stop=1.0, k_fallback=0.0), 4460.0)
        # long：4430−(3+0.5×2)=4426，最大止损 4440 → 4440
        self.assertEqual(stop_ref_of("long", 4450.0, None, [{"price": 4430.0}],
                                     atr=2.0, k_stop=0.5), 4440.0)

    def test_atr_zero_regression(self):
        # 系数默认 0 / atr=0 → 与无 ATR 分量一致（仍受默认最大止损 10 夹紧）
        self.assertEqual(stop_ref_of("short", 4450.0, None, [{"price": 4460.0}],
                                     atr=0.0, k_stop=0.7, k_fallback=0.9), 4460.0)
        self.assertEqual(stop_ref_of("short", 4450.0, None, [],
                                     atr=2.0, k_stop=0.0, k_fallback=0.0), 4460.0)


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


class TestFormingBreakRef(unittest.TestCase):
    def test_long_returns_prev_up_endpoint(self):
        # 多单：末笔 down（不利）→ 参照 = 最近一根 up 笔端点
        bis = [bi("up", 0, 50, 4400, 4460), bi("down", 50, 300, 4460, 4450)]
        ref = forming_break_ref(bis, is_short=False)
        self.assertEqual(ref["price"], 4460.0)
        self.assertEqual(ref["time"], 50)

    def test_last_bi_favorable_returns_none(self):
        # 末笔已是有利方向 → 无形成段，走确认笔口径
        bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        self.assertIsNone(forming_break_ref(bis, is_short=False))

    def test_no_same_direction_bi(self):
        # 无同向笔 / 空列表 → None（不触发预期分路）
        self.assertIsNone(forming_break_ref([bi("up", 0, 300, 4440, 4450)], is_short=False))
        self.assertIsNone(forming_break_ref([], is_short=False))


class TestTrendFollowingOf(unittest.TestCase):
    def test_plan_direction(self):
        self.assertTrue(trend_following_of("多头多"))
        self.assertTrue(trend_following_of("空头空"))
        self.assertFalse(trend_following_of("多头空"))
        self.assertFalse(trend_following_of("空头多"))

    def test_strategy_key_fallback(self):
        self.assertTrue(trend_following_of(None, "wait2Buy"))
        self.assertTrue(trend_following_of(None, "waitSell"))
        # 2026-09-25 键拆分：3买点/类2买点/3卖点/类2卖点 独立键，均为顺势
        self.assertTrue(trend_following_of(None, "wait3Buy"))
        self.assertTrue(trend_following_of(None, "waitLike2Buy"))
        self.assertTrue(trend_following_of(None, "wait3Sell"))
        self.assertTrue(trend_following_of(None, "waitLike2Sell"))
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
        # 末笔 up（空单不利方向）+ 其后下跌形成段：
        # 预期口径（默认开，2026-10-09 严进宽出）下合并K≥5 块（T8）即「检测周期
        # 够笔」半平，不等确认笔；未够块（T7）不半平
        px_up = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444), mark_bis, px_up, T8)
        self.assertEqual(r, "half")
        self.assertTrue(pos["halfDone"])
        self.assertIn("预期", pos["pendingWhy"])
        pos2 = make_pos()
        r2 = advance_exit_decision(pos2, 700, bar(700, 4445, 4448, 4440, 4444), mark_bis, px_up, T7)
        self.assertIsNone(r2)
        self.assertFalse(pos2["halfDone"])

    def test_tp2_expected_half_long_forming_segment(self):
        # 多单、末笔 down（不利）+ 其后上涨形成段合并K≥5（T8）→ 预期够笔半平
        pos = make_pos(direction="long", planDirection="多头多", stopRef=4437.0, beStop=4445.0)
        mark_bis = [bi("down", 0, 500, 4460, 4440)]  # 无 signalTime 后的 up 笔 → 无 TP1
        px_bis = [bi("up", 0, 50, 4400, 4460), bi("down", 50, 300, 4460, 4450)]
        r = advance_exit_decision(pos, 700, bar(700, 4455, 4462, 4451, 4458), mark_bis, px_bis, T8)
        self.assertEqual(r, "half")
        self.assertTrue(pos["halfDone"])
        self.assertIn("预期", pos["pendingWhy"])
        # exitExpectOn=False：回退确认笔旧口径——末笔 down 不算多单够笔，不半平
        pos2 = make_pos(direction="long", planDirection="多头多", stopRef=4437.0, beStop=4445.0)
        pos2["exitCfg"] = dict(EXIT_MODE_DEFAULTS, exitExpectOn=False)
        r2 = advance_exit_decision(pos2, 700, bar(700, 4455, 4462, 4451, 4458), mark_bis, px_bis, T8)
        self.assertIsNone(r2)
        self.assertFalse(pos2["halfDone"])

    def test_tp3_expected_close_on_crossing(self):
        # 预期口径：多单、末笔 down、当根收盘K最高越过前一同向（up）笔端点
        # → 过高低点止盈，不等收笔确认（T7 形成段未够块；无 tp3a 确认笔事件）
        pos = make_pos(direction="long", planDirection="多头多", stopRef=4437.0, beStop=4445.0)
        mark_bis = [bi("down", 0, 500, 4460, 4440)]
        px_bis = [bi("up", 0, 50, 4400, 4460), bi("down", 50, 300, 4460, 4450)]
        r = advance_exit_decision(pos, 700, bar(700, 4455, 4462, 4451, 4458), mark_bis, px_bis, T7)
        self.assertEqual(r, "close")
        self.assertIn("预期", pos["pendingWhy"])
        # 最高 4458 未越过 4460 → 不触发
        pos2 = make_pos(direction="long", planDirection="多头多", stopRef=4437.0, beStop=4445.0)
        r2 = advance_exit_decision(pos2, 700, bar(700, 4455, 4458, 4451, 4456), mark_bis, px_bis, T7)
        self.assertIsNone(r2)
        # exitExpectOn=False：同样未越过不触发口径下关闭预期分路（回到确认笔 tp3a）

    def test_tp2_trend_half_when_down_bi_enough(self):
        pos = make_pos()
        mark_bis = [bi("up", 0, 500, 4440, 4455)]  # 无 TP1
        # 末笔下跌且已完成 = 够笔 → 半平（无需保本先触发）
        px_bis = [bi("up", 0, 50, 4440, 4460), bi("down", 50, 300, 4460, 4440)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444), mark_bis, px_bis, T8)
        self.assertEqual(r, "half")
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

    def test_tp2_forming_down_needs_merged_count(self):
        # 形成中的下跌笔：合并块数未到门槛不够笔；达到门槛才半平
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        forming = bi("down", 50, 300, 4460, 4440)
        forming["_forming"] = True
        forming["mergedCount"] = 4
        px = [bi("up", 0, 50, 4440, 4460), forming]
        pos = make_pos()
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444), mark_bis, px, T8)
        self.assertIsNone(r)
        self.assertFalse(pos["halfDone"])
        forming["mergedCount"] = 5
        pos = make_pos()
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444), mark_bis, px, T8)
        self.assertEqual(r, "half")

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
        # 末笔下跌够笔，且该笔破前低（tp3a）同拍满足 → 半平先于全平挂起
        pos = make_pos()
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                  bi("down", 150, 250, 4450, 4435)]
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

    def test_contract_mult_multiplier(self):
        # 合约乘数快照（2026-09-23：1手=0.01标准手）：pos 带 mult → 盈亏再 × mult
        pos = make_pos()
        pos["mult"] = 10.0
        tr = close_trade(pos, "close", 1000, 4430.0)
        self.assertEqual(tr["pnl"], (4430 - 4450) * (-1) * 4 * 10.0)
        pos2 = make_pos()
        pos2["mult"] = 0.01   # BTC/纳指口径
        pos2["exits"].append({"type": "half", "time": 800, "price": 4440.0})
        tr2 = close_trade(pos2, "close", 1000, 4454.0)
        self.assertAlmostEqual(tr2["pnl"], 12.0 * 0.01)

    def test_long_direction(self):
        pos = make_pos(direction="long", entryPrice=4450.0, lots=4)
        pos["exits"].append({"type": "half", "time": 800, "price": 4460.0})
        tr = close_trade(pos, "close", 1000, 4440.0)
        # (0.5*(4460-4450) + 0.5*(4440-4450)) * 1 * 4 = 0
        self.assertEqual(tr["pnl"], 0.0)


class TestEntryBarFloor(unittest.TestCase):
    """进场K线止损下限（stopEntryBarFloor）：至少在进场背驰周期K线极值外侧加滑点。

    进场K线窗口内运行极值外推（只放松、当根恒不触发），越出窗口自然冻结；
    与支阻位止损取更宽者；beDone 后止损=beStop 不再外推。"""

    def _floor_pos(self):
        # long：窗口 [900,1800)（markRes=15m），fill 时种子 ext=90、支阻位止损 93
        pos = make_pos(direction="long", entryPrice=95.0, stopRef=93.0, beStop=94.5,
                       planDirection="多头多")
        pos.update({"entryBarStart": 900, "entryBarEnd": 1800,
                    "entryBarExt": 90.0, "slipStopEff": 3.0})
        return pos

    def test_no_stop_inside_entry_bar(self):
        # 进场K线内逐根新低：止损同步外推，当根恒在其自身极值−滑点内侧 → 不触发
        pos = self._floor_pos()
        r = advance_exit_decision(pos, 930, bar(900, 95, 96, 88, 89), [], [])
        self.assertIsNone(r)                       # 旧口径此处 88<93 会被扫掉
        self.assertEqual(pos["entryBarExt"], 88.0)
        self.assertEqual(pos["stopRef"], 85.0)     # min(93, 88−3)
        r = advance_exit_decision(pos, 1110, bar(1080, 89, 90, 84, 85), [], [])
        self.assertIsNone(r)
        self.assertEqual(pos["stopRef"], 81.0)     # min(85, 84−3)

    def test_frozen_after_entry_bar(self):
        pos = self._floor_pos()
        advance_exit_decision(pos, 930, bar(900, 95, 96, 88, 89), [], [])
        advance_exit_decision(pos, 1110, bar(1080, 89, 90, 84, 85), [], [])
        # 窗口外：冻结在 81，跌破才触发
        r = advance_exit_decision(pos, 2010, bar(1800, 85, 86, 80, 81), [], [])
        self.assertEqual(r, "stopSr")
        self.assertEqual(pos["stopRef"], 81.0)

    def test_sr_stop_wider_kept(self):
        # 支阻位止损已比K线低点下限更宽 → 维持（"至少"=取更宽者）
        pos = self._floor_pos()
        pos["stopRef"] = 80.0
        advance_exit_decision(pos, 930, bar(900, 95, 96, 88, 89), [], [])
        self.assertEqual(pos["stopRef"], 80.0)

    def test_max_loss_reclamps_floor(self):
        # 外推到 85 后抬回最大止损 90；当根 low 88 已破 90 → 立即支阻位止损
        pos = self._floor_pos()
        pos["maxLoss"] = 90.0
        r = advance_exit_decision(pos, 930, bar(900, 95, 96, 88, 89), [], [])
        self.assertEqual(r, "stopSr")
        self.assertEqual(pos["entryBarExt"], 88.0)
        self.assertEqual(pos["stopRef"], 90.0)

    def test_short_symmetric(self):
        pos = make_pos(direction="short", entryPrice=4450.0, stopRef=4463.0, beStop=4445.0)
        pos.update({"entryBarStart": 900, "entryBarEnd": 1800,
                    "entryBarExt": None, "slipStopEff": 3.0})
        r = advance_exit_decision(pos, 930, bar(900, 4450, 4468, 4449, 4466), [], [])
        self.assertIsNone(r)                       # high 4468 < 新止损 4471
        self.assertEqual(pos["entryBarExt"], 4468.0)
        self.assertEqual(pos["stopRef"], 4471.0)   # max(4463, 4468+3)
        r = advance_exit_decision(pos, 2010, bar(1800, 4470, 4475, 4465, 4472), [], [])
        self.assertEqual(r, "stopSr")

    def test_bestop_precedence_after_breakeven(self):
        # beDone 后止损=beStop，下限不再外推（stopRef 保持原值）
        pos = self._floor_pos()
        pos["beDone"] = True
        r = advance_exit_decision(pos, 930, bar(900, 95, 96, 88, 89), [], [])
        self.assertEqual(r, "stopBe")              # low 88 < beStop 94.5 → 走保本位
        self.assertEqual(pos["stopRef"], 93.0)     # 下限未外推

    def test_no_window_legacy_pos_unchanged(self):
        # 旧口径持仓（无 entryBar 字段）行为不变
        pos = make_pos(direction="long", entryPrice=95.0, stopRef=93.0)
        r = advance_exit_decision(pos, 930, bar(900, 95, 96, 88, 89), [], [])
        self.assertEqual(r, "stopSr")


class TestFillEntryBarSeed(unittest.TestCase):
    """_fill_pending 的下限种子：窗口内已收 fine bar 极值 + 与支阻位止损取更宽者；
    再按最大止损硬上限夹紧。"""

    def _fill(self, **kw):
        lows = kw.pop("lows", [100, 98, 97, 96, 90, 88])     # 0,180,...,900（900 低点 88）
        bars = {"3": [bar(i * 180, 100, 101, lo, 100)
                      for i, lo in enumerate(lows)]}
        eng = BacktestEngine(bars, periods=["3"], slip_stop=3.0,
                             slip_fallback=kw.pop("slip_fallback", 10.0),
                             stop_entry_bar_floor=kw.pop("floor", True))
        sig = {"direction": "long", "periodX": "60", "markRes": "15",
               "time": 880, "price": 94.0, "nearSr": 94.0, "realtime": True,
               "strategyKey": "wait2Buy"}
        trades, stats = [], {"suppressed": 0, "executed": 0}
        eng._fill_pending(trades, [sig], 95.0, kw.pop("nextTime", 1080), stats, collectT=880,
                          open_pos={})
        return trades[0]

    def test_seed_extends_stop(self):
        # 进场价=nextOpen=95；支阻 94−3=91；已收 low=88 → 下限 85；最大止损 95−10=85
        # → 外推后夹在 85（与最大止损重合）
        tr = self._fill()
        self.assertEqual(tr["entryBarStart"], 900)
        self.assertEqual(tr["entryBarEnd"], 1800)
        self.assertEqual(tr["entryBarExt"], 88.0)
        self.assertEqual(tr["slipStopEff"], 3.0)
        self.assertEqual(tr["maxLoss"], 85.0)
        self.assertEqual(tr["stopRef"], 85.0)

    def test_seed_floor_when_max_loss_wider(self):
        # 最大止损放宽到 20 → maxLoss=75，外推 85 不必夹回
        tr = self._fill(slip_fallback=20.0)
        self.assertEqual(tr["maxLoss"], 75.0)
        self.assertEqual(tr["stopRef"], 85.0)

    def test_boundary_entry_no_seed(self):
        # 进场恰在 15m 边界 900：窗口 [900,900) 空 → 无种子，下限暂不生效
        tr = self._fill(nextTime=900, lows=[100, 98, 97, 96, 88, 90])
        self.assertIsNone(tr["entryBarExt"])
        self.assertEqual(tr["stopRef"], 91.0)     # 纯支阻位 94−3（大于最大止损 85）
        self.assertEqual(tr["maxLoss"], 85.0)

    def test_floor_off_legacy(self):
        tr = self._fill(floor=False)
        self.assertIsNone(tr["entryBarStart"])
        self.assertEqual(tr["stopRef"], 91.0)
        self.assertEqual(tr["maxLoss"], 85.0)


def ecfg(**over):
    """出场方式配置快照（EXIT_MODE_DEFAULTS 覆盖）"""
    from py_chain.mark_entry import EXIT_MODE_DEFAULTS
    c = dict(EXIT_MODE_DEFAULTS)
    c.update(over)
    return c


class TestExitModes(unittest.TestCase):
    """出场方式可配置（2026-10-08）：开关 / 平仓比例 / 跟踪止盈 / 部分平仓结算"""

    def make(self, **kw):
        pos = make_pos(**{k: v for k, v in kw.items()
                          if k in ("direction", "signalTime", "entryPrice", "stopRef",
                                   "beStop", "planDirection", "lots")})
        # _fill_pending 新增字段子集（advance_exit_decision 消费）
        pos["lotsLeft"] = float(pos["lots"])
        pos["trailMark"] = pos["signalTime"]
        pos["trailRaised"] = False
        pos["trailMoves"] = []
        pos["stopSrFired"] = pos["stopBeFired"] = pos["trailFired"] = False
        if "exitCfg" in kw:
            pos["exitCfg"] = kw["exitCfg"]
        return pos

    def test_exit_lots_helper(self):
        # 总仓位基数 + 整数手：⌊总手数×pct%⌋ 向下取整、不足1手平1手、封顶剩余
        from py_chain.backtest import _exit_lots
        self.assertEqual(_exit_lots(4, 4, 100), 4)
        self.assertEqual(_exit_lots(4, 4, 50), 2)
        self.assertEqual(_exit_lots(4, 3, 50), 2)    # 基数=总仓位4（非剩余3）
        self.assertEqual(_exit_lots(5, 5, 50), 2)    # floor：⌊2.5⌋ 不进位
        self.assertEqual(_exit_lots(2, 2, 10), 1)    # 不足1手至少平1手
        self.assertEqual(_exit_lots(4, 1, 50), 1)    # 封顶剩余手数

    def test_stop_sr_off_ignored(self):
        # 支阻止损关闭：穿越不挂起、位计算照旧
        pos = self.make(exitCfg=ecfg(exitStopSrOn=False))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 400, bar(400, 4450, 4464, 4448, 4452),
                                  mark_bis, px_bis, T7)
        self.assertIsNone(r)
        self.assertEqual(pos["stopRef"], 4463.0)

    def test_stop_be_off_no_tp1_and_no_stopbe(self):
        mark_bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        pos = self.make(exitCfg=ecfg(exitStopBeOn=False))
        advance_exit_decision(pos, 300, bar(300, 4440, 4445, 4435, 4441),
                              mark_bis, px_bis, T7)
        self.assertFalse(pos["beDone"])       # TP1 迁移随保本止损开关关闭
        self.assertEqual(len(pos["exits"]), 0)
        # beDone 状态下穿越保本位（如 half 已移保本）→ stopBe 关闭 → 不挂起
        pos2 = self.make(exitCfg=ecfg(exitStopBeOn=False))
        pos2["beDone"] = True
        r = advance_exit_decision(pos2, 400, bar(400, 4450, 4456, 4448, 4452),
                                  mark_bis, px_bis, T7)
        self.assertIsNone(r)

    def test_half_off(self):
        pos = self.make(exitCfg=ecfg(exitHalfOn=False))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("up", 0, 50, 4440, 4460), bi("down", 50, 300, 4460, 4440)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444),
                                  mark_bis, px_bis, T8)
        self.assertIsNone(r)                  # 够笔止盈关闭：不挂起
        self.assertFalse(pos["halfDone"])

    def test_close_off(self):
        pos = self.make(planDirection="多头空", exitCfg=ecfg(exitCloseOn=False))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444),
                                  mark_bis, px_bis, T8)
        self.assertIsNone(r)                  # 过高低点止盈关闭：逆势 seg5 不挂起

    def test_half_pct_partial_lots_settlement(self):
        # 够笔止盈 30%：整数手 ⌊4×30%⌋=1，剩余 3 由 stopBe 终局，pnl 按手数逐段结算
        pos = self.make(exitCfg=ecfg(exitHalfPct=30))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("up", 0, 50, 4440, 4460), bi("down", 50, 300, 4460, 4440)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "half")
        self.assertEqual(pos["pendingLots"], 1)
        self.assertIsNone(execute_pending_exit(pos, bar(800, 4440, 4445, 4435, 4442)))
        self.assertEqual(pos["lotsLeft"], 3)
        half_ev = [e for e in pos["exits"] if e["type"] == "half"][0]
        self.assertEqual(half_ev["lots"], 1)
        # 剩余打掉 beStop → stopBe 终局：pnl = 10×1 + (−4)×3 = −2
        r = advance_exit_decision(pos, 900, bar(900, 4450, 4456, 4448, 4452),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "stopBe")
        tr = execute_pending_exit(pos, bar(1000, 4454, 4458, 4450, 4452))
        self.assertAlmostEqual(tr["pnl"], -2)

    def test_pct_base_is_total_not_remaining(self):
        # 百分比基数=总仓位（初始手数，2026-10-08 整手口径）：half 50% 平 2 后，
        # close 25% 仍按总仓位平 1（旧口径按剩余 2×25%=0.5 小数手）
        pos = self.make(exitCfg=ecfg(exitHalfPct=50, exitClosePct=25))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("up", 0, 50, 4440, 4460), bi("down", 50, 300, 4460, 4440)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "half")
        self.assertEqual(pos["pendingLots"], 2)
        self.assertIsNone(execute_pending_exit(pos, bar(800, 4440, 4445, 4435, 4442)))
        self.assertEqual(pos["lotsLeft"], 2)
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                  bi("down", 150, 250, 4450, 4435), bi("up", 250, 300, 4435, 4445)]
        r = advance_exit_decision(pos, 900, bar(900, 4440, 4444, 4436, 4442),
                                  mark_bis, px_bis, T7)
        self.assertEqual(r, "close")
        self.assertEqual(pos["pendingLots"], 1)   # ⌊4×25%⌋=1：总仓位基数
        self.assertEqual(pos["lots"] - pos["pendingLots"] - 2, 1)  # 平完剩 1

    def test_stop_partial_latch(self):
        # 支阻止损 50%：部分平仓后 stopSrFired 一次性消费，后续穿越不重触发
        pos = self.make(exitCfg=ecfg(exitStopSrPct=50))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 400, bar(400, 4450, 4464, 4448, 4452),
                                  mark_bis, px_bis, T7)
        self.assertEqual(r, "stopSr")
        self.assertEqual(pos["pendingLots"], 2.0)
        self.assertTrue(pos["stopSrFired"])
        self.assertIsNone(execute_pending_exit(pos, bar(500, 4462, 4466, 4455, 4460)))
        self.assertEqual(pos["lotsLeft"], 2.0)
        r = advance_exit_decision(pos, 600, bar(600, 4460, 4465, 4455, 4458),
                                  mark_bis, px_bis, T7)
        self.assertIsNone(r)                  # latch：不再重触发

    def test_close_partial_latch(self):
        # 过高低点止盈 25%：部分平仓后 closeDone 一次性消费，同一 tp3a 事件后续拍
        # 不再重复挂起（2026-10-08 trade#6：曾每 3 分钟重复平 25% 直至数据结束）
        pos = self.make(exitCfg=ecfg(exitClosePct=25))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        # down(150→250) 终点 4435 破前一同向 down(0→50) 终点 4440（首个匹配事件）
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                  bi("down", 150, 250, 4450, 4435), bi("up", 250, 300, 4435, 4445)]
        r = advance_exit_decision(pos, 300, bar(300, 4440, 4444, 4436, 4442),
                                  mark_bis, px_bis, T7)
        self.assertEqual(r, "close")
        self.assertEqual(pos["pendingLots"], 1.0)   # 25% × 4
        self.assertTrue(pos["closeDone"])
        self.assertIsNone(execute_pending_exit(pos, bar(400, 4436, 4440, 4430, 4434)))
        self.assertEqual(pos["lotsLeft"], 3.0)
        r = advance_exit_decision(pos, 500, bar(500, 4440, 4444, 4436, 4442),
                                  mark_bis, px_bis, T7)
        self.assertIsNone(r)                  # latch：同一 tp3a 不再重触发
        self.assertEqual(len([e for e in pos["exits"] if e["type"] == "close"]), 1)

    def test_close_partial_latch_countertrend(self):
        # 逆势 seg5 分支同 latch：部分平仓后形成段持续就绪不再重复挂起
        pos = self.make(planDirection="多头空", exitCfg=ecfg(exitClosePct=25))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "close")
        self.assertTrue(pos["closeDone"])
        self.assertIsNone(execute_pending_exit(pos, bar(800, 4440, 4445, 4435, 4442)))
        self.assertEqual(pos["lotsLeft"], 3.0)
        r = advance_exit_decision(pos, 900, bar(900, 4445, 4448, 4440, 4444),
                                  mark_bis, px_bis, T8)
        self.assertIsNone(r)                  # seg5 仍就绪但不重触发

    def test_exit_lots_tiny_remaining_takes_all(self):
        # 剩余手数本身不足 1（旧数据残留小数）→ 封顶即全平剩余，防 0 手空事件与
        # 永远平不掉的尾仓；pct≤0 防御口径同全平（参数中心下限 1，正常不可达）
        from py_chain.backtest import _exit_lots
        self.assertEqual(_exit_lots(4, 0.01, 25), 0.01)
        self.assertEqual(_exit_lots(4, 0.04, 10), 0.04)
        self.assertEqual(_exit_lots(4, 4, 0), 4)

    def test_half_mr_fires_on_tp1_event_and_latches(self):
        # 背驰周期够笔：tp1（markRes 首笔有利方向笔）触发部分平仓，与 TP1 保本同拍
        # （breakeven 先迁移、halfMr 后挂起）；halfMrDone 一次性消费
        pos = self.make(exitCfg=ecfg(exitHalfMrOn=True, exitHalfMrPct=25))
        mark_bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 300, bar(300, 4440, 4445, 4435, 4441),
                                  mark_bis, px_bis, T7)
        self.assertEqual(r, "halfMr")
        self.assertEqual(pos["pendingLots"], 1.0)     # 25% × 4
        self.assertTrue(pos["halfMrDone"])
        self.assertTrue(pos["beDone"])                # TP1 保本同拍迁移
        self.assertEqual(pos["exits"][0]["type"], "breakeven")
        self.assertIsNone(execute_pending_exit(pos, bar(400, 4436, 4440, 4430, 4434)))
        self.assertEqual(pos["lotsLeft"], 3.0)
        r = advance_exit_decision(pos, 500, bar(500, 4440, 4445, 4435, 4441),
                                  mark_bis, px_bis, T7)
        self.assertIsNone(r)                  # latch：tp1 持续成立不重触发
        self.assertEqual(len([e for e in pos["exits"] if e["type"] == "halfMr"]), 1)

    def test_half_mr_not_trend_gated(self):
        # 逆势（多头空）同样触发（与 TP1 同口径，不限顺势）
        pos = self.make(planDirection="多头空", exitCfg=ecfg(exitHalfMrOn=True))
        mark_bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 300, bar(300, 4440, 4445, 4435, 4441),
                                  mark_bis, px_bis, T7)
        self.assertEqual(r, "halfMr")

    def test_half_mr_default_off(self):
        # 默认关：tp1 只触发 TP1 保本迁移，不产生 halfMr 事件（旧基线可比）
        pos = self.make()   # EXIT_MODE_DEFAULTS：exitHalfMrOn=False
        mark_bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 300, bar(300, 4440, 4445, 4435, 4441),
                                  mark_bis, px_bis, T7)
        self.assertIsNone(r)
        self.assertFalse(pos.get("halfMrDone"))
        self.assertTrue(pos["beDone"])

    def test_trail_raise_only_tighter_and_trail_stop(self):
        # 跟踪止盈（detect 默认，2026-10-09）：门控保本——TP1（t=300）前参照点不
        # 提损；门控开启后检测周期点流同向 3/类3/4/类4 点 → 止损只上移；异向/1-2类
        # 点忽略；穿越后记 trailStop
        pos = self.make(exitCfg=ecfg(exitTrailOn=True, exitTrailSlip=1.0))
        mark_bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        pre_pts = [
            {"time": 120, "price": 4442.0, "type": "3卖"},    # 门控前：不追溯
            {"time": 150, "price": 4444.0, "type": "2卖"},    # 1/2类点不参与跟踪
        ]
        # t=300：TP1 保本迁移（beDone），门控开启、水位重置到 300；此前的点全部忽略
        r = advance_exit_decision(pos, 300, bar(300, 4440, 4445, 4435, 4441),
                                  mark_bis, px_bis, T7, trail_pts=pre_pts)
        self.assertIsNone(r)
        self.assertTrue(pos["beDone"])
        self.assertEqual(pos["trailMark"], 300)   # 水位一次性推进到保本当拍
        self.assertEqual(pos["stopRef"], 4463.0)  # 未提损
        self.assertFalse(pos["trailRaised"])
        pts = [
            {"time": 320, "price": 4442.0, "type": "3卖"},    # 4443 < 4463 上移
            {"time": 350, "price": 4445.0, "type": "3卖"},    # 4446 > 4443 不上移（水位前进）
            {"time": 380, "price": 4438.0, "type": "类4卖"},  # 3/4类 4439 < 4443 上移
        ]
        r = advance_exit_decision(pos, 400, bar(400, 4450, 4451, 4448, 4450),
                                  mark_bis, px_bis, T7, trail_pts=pts)
        self.assertIsNone(r)                  # 当拍仅迁移
        self.assertEqual(pos["stopRef"], 4439.0)
        self.assertTrue(pos["trailRaised"])
        raises = [e for e in pos["exits"] if e["type"] == "trailRaise"]
        self.assertEqual(len(raises), 1)      # 同拍多次上移汇总为一条事件
        self.assertEqual(raises[0]["moves"], 2)
        self.assertEqual(len(pos["trailMoves"]), 2)
        self.assertEqual(pos["trailMark"], 380)  # 水位只进不退
        self.assertIn("检测周期点流", raises[0]["why"])
        # 下一拍穿越上移位 4439 → trailStop（全平默认 100%）
        r = advance_exit_decision(pos, 500, bar(500, 4440, 4441, 4435, 4438),
                                  mark_bis, px_bis, T7, trail_pts=pts)
        self.assertEqual(r, "trailStop")
        self.assertEqual(pos["pendingLots"], 4.0)
        tr = execute_pending_exit(pos, bar(600, 4438, 4442, 4430, 4435))
        self.assertEqual(tr["exitType"], "trailStop")
        self.assertEqual(tr["pnl"], (4438 - 4450) * (-1) * 4)

    def test_trail_mark_mode_uses_signal_flow(self):
        # mark 模式（旧口径）：背驰周期信号流 wait3Buy/waitBuy/… 同向3类点驱动；
        # 同拍的检测周期点流不消费；门控保本——TP1 前的信号点不追溯
        pos = self.make(exitCfg=ecfg(exitTrailOn=True, exitTrailSlip=1.0,
                                     exitTrailSrc="mark"))
        mark_bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        pre_sigs = [{"time": 200, "price": 4442.0, "direction": "short",
                     "strategyKey": "wait3Sell"}]               # 门控前：不追溯
        advance_exit_decision(pos, 300, bar(300, 4440, 4445, 4435, 4441),
                              mark_bis, px_bis, T7, trail_sigs=pre_sigs)
        self.assertTrue(pos["beDone"])
        self.assertEqual(pos["stopRef"], 4463.0)
        sigs = [
            {"time": 320, "price": 4442.0, "direction": "short", "strategyKey": "wait3Sell"},  # 4443 < 4463 上移
            {"time": 350, "price": 4438.0, "direction": "short", "strategyKey": "wait2Sell"},  # 非3类忽略
        ]
        pts = [{"time": 340, "price": 4435.0, "type": "类3卖"}]  # mark 模式不消费
        r = advance_exit_decision(pos, 400, bar(400, 4450, 4451, 4448, 4450),
                                  mark_bis, px_bis, T7, trail_sigs=sigs, trail_pts=pts)
        self.assertIsNone(r)
        self.assertEqual(pos["stopRef"], 4443.0)   # 只上移到信号流点 4442+1
        raises = [e for e in pos["exits"] if e["type"] == "trailRaise"]
        self.assertEqual(len(raises), 1)
        self.assertIn("背驰周期信号流", raises[0]["why"])

    def test_trail_detect_long_3buy_story(self):
        # 2买多单 → 保本迁移后检测周期 3买点出现 → 提损至 3买价−滑点；跌破 → trailStop
        pos = self.make(direction="long", planDirection="多头多", entryPrice=4450.0,
                        stopRef=4437.0, beStop=4445.0,
                        exitCfg=ecfg(exitTrailOn=True, exitTrailSlip=1.0))
        mark_bis = [bi("down", 0, 100, 4460, 4440), bi("up", 100, 200, 4440, 4455),
                    bi("down", 200, 500, 4455, 4440)]  # up 笔(100→200) → TP1
        px_bis = [bi("up", 0, 50, 4400, 4470), bi("down", 50, 300, 4470, 4450)]
        pre_pts = [{"time": 200, "price": 4458.0, "type": "3买"}]  # 门控前：不追溯
        r = advance_exit_decision(pos, 300, bar(300, 4462, 4465, 4455, 4460),
                                  mark_bis, px_bis, T7, trail_pts=pre_pts)
        self.assertIsNone(r)
        self.assertTrue(pos["beDone"])
        self.assertFalse(pos["trailRaised"])
        pts = [{"time": 320, "price": 4458.0, "type": "3买"}]  # 3买回踩低点 4458
        r = advance_exit_decision(pos, 400, bar(400, 4462, 4465, 4455, 4460),
                                  mark_bis, px_bis, T7, trail_pts=pts)
        self.assertIsNone(r)
        self.assertEqual(pos["stopRef"], 4457.0)   # 3买位 4458 − 滑点 1
        self.assertTrue(pos["trailRaised"])
        # 跌破 3买位 → trailStop
        r = advance_exit_decision(pos, 500, bar(500, 4455, 4456, 4450, 4452),
                                  mark_bis, px_bis, T7, trail_pts=pts)
        self.assertEqual(r, "trailStop")

    def test_trail_off_by_default(self):
        # 默认跟踪止盈关闭：同向3类点不引起上移
        pos = self.make()
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        pts = [{"time": 200, "price": 4442.0, "type": "3卖"}]
        advance_exit_decision(pos, 400, bar(400, 4450, 4451, 4448, 4450),
                              mark_bis, px_bis, T7, trail_pts=pts)
        self.assertEqual(pos["stopRef"], 4463.0)
        self.assertFalse(pos["trailRaised"])

    def test_trail_gated_before_breakeven(self):
        # 门控保本（2026-10-09）：保本迁移（TP1）之前同向参照点不提损、不接管——
        # 穿越初始止损位记 stopSr 而非 trailStop（2026-08-19 XAUUSD 案例回归）。
        # beDone 由 TP1/half/halfMr 置位；此处 markRes 无 signalTime 后的有利方向笔
        pos = self.make(exitCfg=ecfg(exitTrailOn=True, exitTrailSlip=1.0))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]  # 无 TP1 → beDone 恒 False
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        pts = [{"time": 150, "price": 4442.0, "type": "3卖"}]  # 入场时段参照点
        r = advance_exit_decision(pos, 400, bar(400, 4450, 4464, 4448, 4452),
                                  mark_bis, px_bis, T7, trail_pts=pts)
        self.assertEqual(r, "stopSr")            # 穿越初始止损 4463，非跟踪止盈
        self.assertFalse(pos["trailRaised"])
        self.assertEqual(pos["stopRef"], 4463.0)
        self.assertEqual(pos["trailMark"], 100)  # 门控未开，水位不推进

    def test_maxloss_fallback_after_trail_partial(self):
        # 最大止损硬上限兜底（2026-10-09）：跟踪止盈 25% 平 1 手 latch 死止损通道后，
        # 价格一路逆向——兜底通道在 maxLoss 全平剩余 3 手，单手亏损封顶 slip_fallback
        # （修复前：剩余 3 手无保护扛 121 点，-242/-107）
        pos = self.make(entryPrice=4450.0, stopRef=4460.0, beStop=4455.0,
                        exitCfg=ecfg(exitTrailOn=True, exitTrailSlip=1.0,
                                     exitTrailPct=25))
        pos["maxLoss"] = 4460.0
        mark_bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        # t=300 TP1 保本；t=400 参照点 4444 → stopRef 4445（收紧）
        advance_exit_decision(pos, 300, bar(300, 4440, 4445, 4435, 4441),
                              mark_bis, px_bis, T7)
        pts = [{"time": 350, "price": 4444.0, "type": "3卖"}]
        advance_exit_decision(pos, 400, bar(400, 4450, 4451, 4448, 4450),
                              mark_bis, px_bis, T7, trail_pts=pts)
        self.assertEqual(pos["stopRef"], 4445.0)
        self.assertTrue(pos["trailRaised"])
        # t=500 冲上 4445 → trailStop 25%（1 手），trailFired latch 关闭穿越判定
        r = advance_exit_decision(pos, 500, bar(500, 4444, 4446, 4440, 4445),
                                  mark_bis, px_bis, T7)
        self.assertEqual(r, "trailStop")
        self.assertEqual(pos["pendingLots"], 1.0)
        self.assertTrue(pos["trailFired"])
        self.assertIsNone(execute_pending_exit(pos, bar(600, 4445, 4447, 4442, 4446)))
        self.assertEqual(pos["lotsLeft"], 3.0)
        # t=700 反弹越过 maxLoss 4460 → 止损通道已 latch，兜底全平剩余 3 手
        r = advance_exit_decision(pos, 700, bar(700, 4455, 4461, 4450, 4459),
                                  mark_bis, px_bis, T7)
        self.assertEqual(r, "stopSr")
        self.assertEqual(pos["pendingLots"], 3.0)
        self.assertIn("兜底", pos["pendingWhy"])
        tr = execute_pending_exit(pos, bar(800, 4460, 4462, 4455, 4458))
        self.assertEqual(tr["exitType"], "stopSr")
        # trailStop 1 手@4445（+5）+ 兜底 3 手@4460（-30）= -25；
        # 单手最大亏损 10 = slip_fallback 口径
        self.assertAlmostEqual(tr["pnl"], -25.0)
        self.assertEqual([e["type"] for e in tr["exits"]],
                         ["breakeven", "trailRaise", "trailStop", "stopSr"])

    def test_trail_partial_latch(self):
        # 跟踪止盈 50%：部分平仓后 trailFired 一次性消费
        pos = self.make(exitCfg=ecfg(exitTrailOn=True, exitTrailSlip=1.0, exitTrailPct=50))
        pos["beDone"] = True                     # 门控保本：保本后跟踪止盈才接管
        pos["trailRaised"] = True
        pos["stopRef"] = 4439.0
        pos["trailMark"] = 300
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 500, bar(500, 4440, 4441, 4435, 4438),
                                  mark_bis, px_bis, T7)
        self.assertEqual(r, "trailStop")
        self.assertEqual(pos["pendingLots"], 2.0)
        self.assertTrue(pos["trailFired"])
        self.assertIsNone(execute_pending_exit(pos, bar(600, 4438, 4442, 4430, 4435)))
        self.assertEqual(pos["lotsLeft"], 2.0)
        r = advance_exit_decision(pos, 700, bar(700, 4440, 4442, 4436, 4440),
                                  mark_bis, px_bis, T7)
        self.assertIsNone(r)

    def test_legacy_pos_defaults_open_all(self):
        # 旧 pos（无 exitCfg/新字段）→ EXIT_MODE_DEFAULTS 兜底 = 现网行为
        pos = make_pos()   # 不带 exitCfg / lotsLeft
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("up", 0, 50, 4440, 4460), bi("down", 50, 300, 4460, 4440)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "half")
        self.assertEqual(pos["pendingLots"], 2.0)   # 默认 50% × lots 4


if __name__ == "__main__":
    unittest.main()
