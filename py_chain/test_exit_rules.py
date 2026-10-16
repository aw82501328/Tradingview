import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志

# -*- coding: utf-8 -*-
"""出场规则单元测试（出场阶梯重构 2026-09-09，对应 mark-entry SPEC §2.4）

覆盖（与 .cursor/skills/mark-entry/scripts/mark_entry.test.js 的「出场规则」describe 成对同构）：
  - stop_ref_of：支阻位 ± 滑点 / nearSr 错侧重选 / 最大止损硬上限（返回值永不为 None）
  - forming_seg_ready：合并后 ≥5 块门槛 / 末笔方向 / 延伸归零
  - forming_break_ref：预期过前高参照（末笔不利才有形成段 / 无同向笔 None）
  - enough_fav_bars（2026-10-10 够笔统一口径）：向有利方向走出 5 块合并K即够笔，
    不分顺势逆势、不等确认笔；锚点=末笔延伸终点块与进场块较晚者（进场块下限防
    末笔延伸提交滞后把末笔自身的块计入=入场拍假就绪）；末笔已确认有利方向→False
  - advance_exit_decision：TP1→beStop→stopBe / TP2 检测周期够笔（统一口径：预期
    enough_fav_bars 或确认 lastBiOk，无需 TP1，不限顺势）→ half 后止损=beStop /
    TP3 过高低点（2026-10-10 起不分顺势逆势：确认 breakPrev / 预期收盘K极值越
    前一同向笔端点；旧逆势 seg5 快速离场口径废除——锚点无进场块下限、入场拍假
    就绪，trade#109 实证）/ stopSr / 同拍顺序 half 优先 /
    预期口径（2026-10-09 严进宽出，默认开）：检测够笔=末笔不利+形成段够块、
    过高低点=收盘K极值越前一同向笔端点，exitExpectOn=False 回退确认笔旧口径
  - 跟踪止盈双参照（2026-10-09 exitTrailSrc；2026-10-11 detect 改结构点）：
    detect=检测周期已破前低/前高的有利方向笔起点（够笔未破前低/前高不算；
    破坏该起点记 trailStop）；mark=背驰周期信号流
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
                                 enough_fav_bars, trend_following_of, EXIT_MODE_DEFAULTS)
from py_chain.backtest import (advance_exit_decision, execute_pending_exit,
                               close_trade, BacktestEngine)


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice,
            "span": abs(endPrice - startPrice)}


def bar(time, open_, high, low, close):
    return {"time": time, "open": open_, "high": high, "low": low, "close": close}


def make_pos(direction="short", signalTime=100, entryPrice=4450.0, stopRef=4463.0,
             beStop=4455.0, planDirection="空头空", lots=4, strategyKey=None):
    """构造持仓 dict（_fill_pending 产出的出场状态机字段子集）。
    2026-10-16 起顺势按 strategyKey 判定（计划方向口径取消）：planDirection 为
    逆势（多头空/空头多）时 strategyKey 缺省配逆势键 wait1Sell，与直连映射一致。"""
    if strategyKey is None:
        strategyKey = "wait2Sell" if planDirection in ("空头空", "多头多") else "wait1Sell"
    return {"direction": direction, "signalTime": signalTime, "entryPrice": entryPrice,
            "stopRef": stopRef, "beStop": beStop, "planDirection": planDirection,
            "strategyKey": strategyKey, "lots": lots,
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
        # 末笔已是有利方向且已破前一同向端点（up 4450 < 前高 4460 不成立→未破；
        # 本例改为已破形态）→ 破位事件属确认笔口径，返回 None
        # （down 4440→up 4470 已破前高 4460）
        bis = [bi("up", 0, 50, 4400, 4460), bi("down", 50, 150, 4460, 4450),
               bi("up", 150, 300, 4450, 4470)]
        self.assertIsNone(forming_break_ref(bis, is_short=False))

    def test_last_bi_favorable_not_broken_falls_back_prev(self):
        # 2026-10-10 补分路（trade#109 二段案例）：末笔已是利好方向但端点未破前
        # 一同向端点（空单：更高低点 4257.69 > 前低 4254.58）→ 参照退一级取前一同向
        # 端点，价格越过即触发——防 gapLocked/延伸滞后冻结确认口径端点致破位后
        # 迟迟不触发（9-28 08:00 破 4254.58、12:00 才 4196.81 平仓）
        bis = [bi("up", 0, 50, 4400, 4460), bi("down", 50, 150, 4460, 4454.58),
               bi("up", 150, 200, 4454.58, 4497.31),
               bi("down", 200, 300, 4497.31, 4457.69)]  # 末笔 down 未破 4454.58
        ref = forming_break_ref(bis, is_short=True)
        self.assertEqual(ref["price"], 4454.58)
        self.assertEqual(ref["time"], 150)
        # 多单对称：末笔 up 更低高点未破前高 → 参照 = 前一同向 up 端点
        bis_l = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4460),
                 bi("down", 150, 200, 4460, 4445), bi("up", 200, 300, 4445, 4455)]
        ref_l = forming_break_ref(bis_l, is_short=False)
        self.assertEqual(ref_l["price"], 4460.0)
        self.assertEqual(ref_l["time"], 150)

    def test_last_bi_favorable_no_prev_fav_returns_none(self):
        # 末笔有利但前面再无同向笔 → 无参照可退
        self.assertIsNone(forming_break_ref([bi("down", 0, 300, 4460, 4440)], is_short=True))

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


class TestEnoughFavBars(unittest.TestCase):
    """够笔统一口径（2026-10-10）：向有利方向走出 5 块合并K即够笔。"""

    def test_five_blocks_after_anchor_ready(self):
        # 空单：末笔 up（不利）终点 300，其后 4 块（含锚点共 5）→ 够笔
        px = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        self.assertTrue(enough_fav_bars(px, T8, 0, is_short=True))
        self.assertFalse(enough_fav_bars(px, T7, 0, is_short=True))  # 4 块不足

    def test_favorable_last_bi_not_ready(self):
        # 末笔已是有利方向（无形成段）→ False，走确认笔口径
        px = [bi("up", 0, 50, 4440, 4460), bi("down", 50, 300, 4460, 4440)]
        self.assertFalse(enough_fav_bars(px, T8, 0, is_short=True))

    def test_entry_floor_blocks_stale_anchor(self):
        # 2026-10-01 案例形态：空单进场时末笔（up 不利方向）延伸未提交（终点滞留
        # 200），锚点后虽有 5 块（全是末笔自身的上涨块）——进场块下限（650）更靠右
        # → 不够笔（纯 forming_seg_ready 口径会假就绪抢触发）
        px = [bi("up", 0, 200, 4440, 4450)]
        self.assertFalse(enough_fav_bars(px, T8, 650, is_short=True))
        # 进场早于锚点（100 < 200）→ 退化为纯末笔锚点口径，块数够 → 够笔
        self.assertTrue(enough_fav_bars(px, T8, 100, is_short=True))
        # entry_time=0：不设下限，同上
        self.assertTrue(enough_fav_bars(px, T8, 0, is_short=True))

    def test_entry_block_counts_toward_enough(self):
        # 进场所在块即锚点：其后 4 块 → 够笔（进场拍归零起步、防进场前块计入）
        px = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        self.assertTrue(enough_fav_bars(px, T8, 350, is_short=True))   # 进场在 300 块内
        self.assertFalse(enough_fav_bars(px, T8, 450, is_short=True))  # 进场在 400 块，其后 3 块

    def test_long_direction_symmetric(self):
        px = [bi("up", 0, 50, 4400, 4460), bi("down", 50, 300, 4460, 4450)]
        self.assertTrue(enough_fav_bars(px, T8, 0, is_short=False))
        self.assertFalse(enough_fav_bars(px, T8, 0, is_short=True))    # 方向反了：末笔 up=空单有利


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

    def test_tp2_counter_trend_half_then_close_on_seg5(self):
        # 2026-10-10 统一口径：逆势（多头空）预期口径 half 先触发（末笔 up=不利方向、
        # 其后下跌形成段 T8 够块，同拍顺序 half 在 close 前）；下一拍 close 经
        # exp_break 触发（bar low 4436 破前一同向 down 笔端点 4440——过高低点
        # 2026-10-10 起不分顺势逆势；旧 seg5 快速离场口径废除）——先到先 latch
        pos = make_pos(planDirection="多头空")
        pos["exitCfg"] = dict(EXIT_MODE_DEFAULTS, exitClosePct=25)
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444), mark_bis, px_bis, T8)
        self.assertEqual(r, "half")
        self.assertTrue(pos["halfDone"])
        self.assertIn("逆势", pos["pendingWhy"])
        self.assertIn("预期口径", pos["pendingWhy"])
        self.assertEqual(pos["pendingLots"], 2)   # 50% × 4
        self.assertIsNone(execute_pending_exit(pos, bar(800, 4440, 4445, 4435, 4442)))
        self.assertTrue(pos["beDone"])            # half 补 beDone
        # 下一拍：low 4436 破前低 4440 → close 25% × 4 = 1 手（预期口径 exp_break）
        r = advance_exit_decision(pos, 810, bar(810, 4440, 4445, 4436, 4442),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "close")
        self.assertEqual(pos["pendingLots"], 1)
        self.assertIsNone(execute_pending_exit(pos, bar(820, 4440, 4445, 4435, 4442)))
        self.assertEqual(pos["lotsLeft"], 1)
        # 两个 latch 都已消费：后续拍（未破 beStop=4455 的 bar）不再重复挂起
        self.assertIsNone(advance_exit_decision(pos, 900, bar(900, 4440, 4448, 4436, 4444),
                                                mark_bis, px_bis, T8))

    def test_tp3_countertrend_break_price_with_fav_last_bi(self):
        # 2026-10-10 补分路（trade#109 二段案例）：逆势空单、末笔已是利好方向（down）
        # 且端点未破前一同向端点（更高低点 4445 > 前低 4440）→ 预期口径参照退一级取
        # 前低，当根收盘K low 破位即触发 close，不等确认口径 tp3a——gapLocked（跨
        # 周末跳空）/延伸滞后会冻结确认端点，价格实际破位后迟迟不触发（9-28 08:00
        # 破 4254.58、12:00 才 4196.81 平仓）
        pos = make_pos(planDirection="多头空")
        pos["exitCfg"] = dict(EXIT_MODE_DEFAULTS, exitClosePct=25)
        pos["halfDone"] = True   # half 已消费，隔离 close 路径
        mark_bis = [bi("up", 0, 500, 4440, 4455)]  # 无 signalTime 后 down 笔 → 无 TP1
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                  bi("down", 150, 300, 4450, 4445)]  # tp3a：4445 未破 4440 → None
        r = advance_exit_decision(pos, 700, bar(700, 4448, 4452, 4448, 4450),
                                  mark_bis, px_bis, T8)
        self.assertIsNone(r)                     # low 4448 未破 4440 → 不触发
        r = advance_exit_decision(pos, 710, bar(710, 4444, 4448, 4436, 4440),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "close")             # low 4436 破 4440 → 触发
        self.assertIn("预期口径", pos["pendingWhy"])
        self.assertIn("4440", pos["pendingWhy"])

    def test_tp2_counter_trend_half_on_confirmed_fav_bi(self):
        # 确认口径兜底：逆势（空头多）检测周期末笔翻为确认的有利方向（up）笔 →
        # lastBiOk 触发 half 平 50%，剩余止损移保本位（用户案例 2026-10-01：
        # 空头多进场，检测周期上涨笔确认时本应再平一部分——旧规则仅顺势跳过）
        pos = make_pos(direction="long", planDirection="空头多", stopRef=4437.0,
                       beStop=4445.0)
        mark_bis = [bi("down", 0, 500, 4460, 4440)]  # 无 signalTime 后的 up 笔 → 无 TP1
        px_bis = [bi("down", 0, 50, 4460, 4450), bi("up", 50, 300, 4450, 4460)]
        r = advance_exit_decision(pos, 700, bar(700, 4455, 4462, 4451, 4458),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "half")
        self.assertTrue(pos["halfDone"])
        self.assertIn("逆势", pos["pendingWhy"])
        self.assertEqual(pos["pendingLots"], 2)   # 50% × 总仓位 4
        tr = execute_pending_exit(pos, bar(800, 4458, 4462, 4454, 4460))
        self.assertIsNone(tr)                     # 部分平仓非终局
        self.assertTrue(pos["beDone"])            # half 补 beDone：剩余 2 手止损 = beStop
        self.assertEqual(pos["lotsLeft"], 2)
        # 剩余跌破 beStop 4445 → stopBe 终局
        r = advance_exit_decision(pos, 900, bar(900, 4450, 4452, 4443, 4446),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "stopBe")
        tr = execute_pending_exit(pos, bar(1000, 4446, 4448, 4444, 4445))
        self.assertEqual(tr["exitType"], "stopBe")
        # half@4458×2 + 终局@4446×2：pnl = (8×2) + (-4×2) = 8
        self.assertEqual(tr["pnl"], 8.0)

    def test_half_ignores_fav_bi_ended_before_entry(self):
        # 2026-09-01 #1：空单进场时，检测周期末笔已是更早确认的下跌笔。
        # lastBiOk 只看方向会在进场当拍假就绪。endTime 不晚于进场 → 不触发。
        pos = make_pos(signalTime=1000, stopRef=4465.27, beStop=4455.27,
                       planDirection="多头空")
        pos["entryTime"] = 1200
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("up", 0, 100, 4400, 4460), bi("down", 100, 800, 4460, 4415.75)]
        r = advance_exit_decision(pos, 1300, bar(1300, 4455, 4459.79, 4453, 4454),
                                  mark_bis, px_bis, T8)
        self.assertIsNone(r)
        self.assertFalse(pos.get("halfDone"))
        self.assertFalse(pos.get("beDone"))

    def test_tp3a_trend_fav_break_prev(self):
        # 过高低点默认已是 25% 部分平仓；本场景钉全平，断言终局成交
        pos = make_pos()
        pos["exitCfg"] = dict(EXIT_MODE_DEFAULTS, exitClosePct=100.0)
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


class TestStopBeMode(unittest.TestCase):
    """保本位模式（exitStopBeMode，2026-10-10；2026-10-12 三口径）：
    entry（默认，0亏损）= 进场价（保本滑点不参与，滑点/ATR 系数均不生效）；
    extreme = 成交K线极值±保本滑点（含成交 bar 缺失时 开盘价±滑点 兜底）；
    bi = 笔端点±滑点（背驰周期首笔有利方向笔确认那拍 beStop=该笔起点±开仓时点
    快照滑点，成笔前无值——half 抢先置 beDone 时穿越暂回落支阻位）。"""

    def _fill(self, **kw):
        lows = kw.pop("lows", [100, 98, 97, 96, 88, 90])   # 0,180,...,900（900 低点 90）
        bars = {"3": [bar(i * 180, 100, 101, lo, 100)
                      for i, lo in enumerate(lows)]}
        eng = BacktestEngine(bars, periods=["3"], slip_stop=3.0, slip_be=3.0,
                             slip_fallback=kw.pop("slip_fallback", 10.0),
                             module_params=kw.pop("mp", None))
        sig = {"direction": "long", "periodX": "60", "markRes": "15",
               "time": 880, "price": 94.0, "nearSr": 94.0, "realtime": True,
               "strategyKey": "wait2Buy"}
        trades, stats = [], {"suppressed": 0, "executed": 0}
        eng._fill_pending(trades, [sig], 95.0, kw.pop("nextTime", 900), stats,
                          collectT=880, open_pos={})
        return trades[0]

    def test_extreme_default_uses_fill_bar_low(self):
        # 默认 entry：beStop = 进场价 95；极值口径须显式指定
        tr = self._fill()
        self.assertEqual(tr["beStop"], 95.0)
        self.assertIn("0亏损", tr["entryWhy"])
        tr2 = self._fill(mp={"exitStopBeMode": "extreme"})
        self.assertEqual(tr2["beStop"], 87.0)
        self.assertIn("保本滑点", tr2["entryWhy"])

    def test_extreme_fallback_when_bar_missing(self):
        # 成交 bar 不在 fine 轴（nextTime=1080 无K）→ 兜底 进场价−滑点 = 92
        tr = self._fill(mp={"exitStopBeMode": "extreme"}, nextTime=1080)
        self.assertEqual(tr["beStop"], 92.0)

    def test_entry_mode_bestop_equals_entry_price(self):
        # 0亏损：beStop = 进场价 95（滑点 3 不参与；成交K线 low 90 不影响）
        tr = self._fill(mp={"exitStopBeMode": "entry"})
        self.assertEqual(tr["beStop"], 95.0)
        self.assertEqual(tr["entryPrice"], 95.0)
        self.assertIn("0亏损", tr["entryWhy"])

    def test_entry_mode_bar_missing_same_price(self):
        tr = self._fill(mp={"exitStopBeMode": "entry"}, nextTime=1080)
        self.assertEqual(tr["beStop"], 95.0)

    def test_entry_mode_stop_be_exit_zero_pnl(self):
        # 0亏损语义端到端：beDone 后跌回进场价触发 stopBe，下一开盘恰为进场价 → 盈亏 0
        pos = make_pos(direction="long", entryPrice=95.0, stopRef=93.0, beStop=95.0,
                       planDirection="多头多")
        pos["beDone"] = True
        r = advance_exit_decision(pos, 400, bar(400, 95, 96, 94.5, 94.8), [], [])
        self.assertEqual(r, "stopBe")
        tr = execute_pending_exit(pos, bar(500, 95, 96, 94, 95))
        self.assertEqual(tr["exitPrice"], 95.0)
        self.assertEqual(tr["pnl"], 0.0)

    # ---- bi 口径（笔端点±滑点，2026-10-12）：成笔前无值、成笔那拍惰性落位 ----

    def test_bi_mode_pending_at_fill(self):
        # 开仓：beStop=None（待定）+ 滑点快照入 pos；进场叙事「笔端点待定」
        tr = self._fill(mp={"exitStopBeMode": "bi"})
        self.assertIsNone(tr["beStop"])
        self.assertEqual(tr["slipBeEff"], 3.0)
        self.assertIn("笔端点待定", tr["entryWhy"])

    def test_bi_mode_lands_at_bi_start_on_tp1(self):
        # 成笔那拍（TP1 同事件源）：beStop = 背驰周期首笔有利方向笔起点 ∓ 开仓快照滑点
        pos = make_pos(direction="long", entryPrice=95.0, stopRef=93.0, beStop=None,
                       planDirection="多头多")
        pos["exitCfg"] = ecfg(exitStopBeMode="bi", exitHalfOn=False)
        pos["slipBeEff"] = 3.0
        pos["lotsLeft"] = 4.0
        mark_bis = [bi("up", 0, 100, 100.0, 94.0), bi("down", 100, 300, 94.0, 90.0),
                    bi("up", 300, 500, 90.0, 96.0)]   # 首笔有利 up：起点 90 → beStop=87
        px_bis = [bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 500, bar(500, 95, 96, 94.5, 95.5), mark_bis, px_bis)
        self.assertIsNone(r)                       # TP1 仅状态迁移（breakeven），不成交
        self.assertEqual(pos["beStop"], 87.0)      # 笔起点 90 − 滑点 3
        self.assertTrue(pos["beDone"])
        be = [e for e in pos["exits"] if e["type"] == "breakeven"][-1]
        self.assertIn("笔端点 90.00-保本滑点 3.00", be["why"])

    def test_bi_mode_value_independent_of_be_on(self):
        # exitStopBeOn=False（TP1 不触发）：成笔后 beStop 仍落位——half「移保本」可用
        pos = make_pos(direction="long", entryPrice=95.0, stopRef=93.0, beStop=None,
                       planDirection="多头多")
        pos["exitCfg"] = ecfg(exitStopBeMode="bi", exitStopBeOn=False, exitHalfOn=False)
        pos["slipBeEff"] = 3.0
        pos["lotsLeft"] = 4.0
        mark_bis = [bi("down", 0, 100, 96.0, 94.0), bi("up", 100, 400, 90.0, 96.0)]
        r = advance_exit_decision(pos, 400, bar(400, 95, 96, 94.5, 95.5), mark_bis, [])
        self.assertIsNone(r)
        self.assertEqual(pos["beStop"], 87.0)
        self.assertFalse(pos["beDone"])            # TP1 关闭：只落值不迁移

    def test_bi_mode_half_before_bi_falls_back_to_sr(self):
        # half（检测周期够笔）抢在成笔前置 beDone：beStop 仍 None → 叙事打「待定」、
        # 穿越暂按支阻位 stopSr（be_active 回落）；成笔那拍 beStop 落位并迁 stopBe
        pos = make_pos(direction="long", entryPrice=95.0, stopRef=93.0, beStop=None,
                       planDirection="多头多")
        pos["exitCfg"] = ecfg(exitStopBeMode="bi", exitStopSrPct=25)  # half 默认开
        pos["slipBeEff"] = 3.0
        pos["lotsLeft"] = 4.0
        px_bis = [bi("down", 0, 100, 96.0, 94.0), bi("up", 100, 300, 94.0, 99.0)]
        mark_bis = [bi("down", 0, 400, 96.0, 91.0)]   # 背驰周期尚无有利方向确认笔
        # t=300：检测周期末笔有利（endTime 300 > 进场）→ half 触发；叙事含「待定」不崩
        r = advance_exit_decision(pos, 300, bar(300, 95, 96, 94.5, 95.5), mark_bis, px_bis)
        self.assertEqual(r, "half")
        self.assertIsNone(pos["beStop"])
        self.assertIn("待定", pos["pendingWhy"])
        execute_pending_exit(pos, bar(400, 95, 96, 94, 95))   # half 成交，置 beDone
        self.assertTrue(pos["beDone"])
        # t=420：低点 92.5 破支阻 93 → stopSr（若误判 stopBe 则 beStop=None 不触发）
        mark_bis.append(bi("up", 400, 450, 91.0, 97.0))       # endTime 450 > t，未成笔
        r2 = advance_exit_decision(pos, 420, bar(420, 95, 96, 92.5, 93.5), mark_bis, px_bis)
        self.assertEqual(r2, "stopSr")
        self.assertIsNone(pos["beStop"])
        execute_pending_exit(pos, bar(430, 93, 94, 92, 93))   # 25% 部分平，仓位续存
        # t=450：成笔那拍 beStop 落位 = 笔起点 91 − 3 = 88；低点 87.5 破位 → stopBe
        r3 = advance_exit_decision(pos, 450, bar(450, 95, 96, 87.5, 94.0), mark_bis, px_bis)
        self.assertEqual(pos["beStop"], 88.0)
        self.assertEqual(r3, "stopBe")

    def test_find_bi_event_returns_start_price(self):
        # 增量键 startPrice（bi 口径取「有利方向笔起点」，既有调用者不感知）
        from py_chain.mark_entry import find_bi_event
        bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430),
               bi("up", 200, 300, 4430, 4448)]
        ev = find_bi_event(bis, 100, "down")
        self.assertEqual(ev["startPrice"], 4450)
        self.assertEqual(ev["startTime"], 100)
        self.assertIsNone(find_bi_event(bis, 100, "up", break_prev=True))


def ecfg(**over):
    """出场方式配置快照。算法场景钉在 2026-10-10 收默认之前的口径
    （过高低点 100%、跟踪止盈关、保本位极值），调用方显式传入的键仍覆盖。"""
    from py_chain.mark_entry import EXIT_MODE_DEFAULTS
    c = dict(EXIT_MODE_DEFAULTS)
    c.update(exitClosePct=100.0, exitTrailOn=False, exitTrailPct=100.0,
             exitStopBeMode="extreme")
    c.update(over)
    return c


class TestExitModes(unittest.TestCase):
    """出场方式可配置（2026-10-08）：开关 / 平仓比例 / 跟踪止盈 / 部分平仓结算"""

    def make(self, **kw):
        pos = make_pos(**{k: v for k, v in kw.items()
                          if k in ("direction", "signalTime", "entryPrice", "stopRef",
                                   "beStop", "planDirection", "lots", "strategyKey")})
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
        # 过高低点止盈关闭：逆势破前低不挂起；统一口径下检测周期够笔（预期）照常触发
        pos = self.make(planDirection="多头空", exitCfg=ecfg(exitCloseOn=False))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "half")

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
        # 逆势（多头空）close 同 latch：统一口径下 half 先消费（预期口径），close 经
        # exp_break（破前低 4440）随后部分平仓，破前低条件持续成立不再重复挂起
        pos = self.make(planDirection="多头空", exitCfg=ecfg(exitClosePct=25))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)]
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "half")           # 统一口径：预期够笔先触发
        self.assertIsNone(execute_pending_exit(pos, bar(800, 4440, 4445, 4435, 4442)))
        self.assertEqual(pos["lotsLeft"], 2.0)
        r = advance_exit_decision(pos, 810, bar(810, 4440, 4445, 4436, 4442),
                                  mark_bis, px_bis, T8)
        self.assertEqual(r, "close")
        self.assertTrue(pos["closeDone"])
        self.assertIsNone(execute_pending_exit(pos, bar(820, 4440, 4445, 4435, 4442)))
        self.assertEqual(pos["lotsLeft"], 1.0)
        r = advance_exit_decision(pos, 900, bar(900, 4440, 4448, 4436, 4444),
                                  mark_bis, px_bis, T8)
        self.assertIsNone(r)                  # 破前低仍成立但两个 latch 均不重触发

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

    def test_half_mr_expected_path_five_blocks(self):
        # 2026-10-10 统一口径：背驰周期向有利方向走出 5 块合并K（enough_fav_bars
        # 预期口径）→ halfMr，不等首笔有利方向确认笔（tp1=None）
        pos = self.make(exitCfg=ecfg(exitHalfMrOn=True))
        mark_bis = [bi("up", 0, 300, 4440, 4450)]  # 无 signalTime 后 down 笔 → tp1 None
        px_bis = [bi("up", 50, 300, 4440, 4450)]  # 检测周期 3 块（T7）不触发 half
        r = advance_exit_decision(pos, 700, bar(700, 4445, 4448, 4440, 4444),
                                  mark_bis, px_bis, T7, T8)
        self.assertEqual(r, "halfMr")
        self.assertTrue(pos["halfMrDone"])
        self.assertIn("预期口径", pos["pendingWhy"])
        self.assertEqual(pos["pendingLots"], 2.0)
        self.assertFalse(pos["beDone"])   # exp_mr 不伴随 TP1 迁移；成交拍补 beDone
        self.assertIsNone(execute_pending_exit(pos, bar(800, 4440, 4445, 4435, 4442)))
        self.assertTrue(pos["beDone"])

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
        # 跟踪止盈（detect 默认）：门控保本——TP1 前的下跌笔不提损。此后只有
        # 破前低的下跌笔起点才是结构高点；未破前低的笔、以及破了但更松的高点
        # 都不收紧。冲上结构高点记 trailStop
        pos = self.make(exitCfg=ecfg(exitTrailOn=True, exitTrailSlip=1.0,
                                     exitHalfOn=False, exitCloseOn=False,
                                     exitHalfMrOn=False))
        mark_bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)]
        # t=300：TP1 保本，水位重置到 300。下跌笔起点 120 早于水位，不追溯
        pre = [
            bi("down", 120, 180, 4460.0, 4448.0),
            bi("up", 180, 300, 4448.0, 4450),
        ]
        r = advance_exit_decision(pos, 300, bar(300, 4440, 4445, 4435, 4441),
                                  mark_bis, pre, T7)
        self.assertIsNone(r)
        self.assertTrue(pos["beDone"])
        self.assertEqual(pos["trailMark"], 300)
        self.assertEqual(pos["stopRef"], 4463.0)
        self.assertFalse(pos["trailRaised"])
        px_bis = pre + [
            bi("down", 320, 340, 4442.0, 4430),   # 4430<4448 破前低，止损 4443
            bi("up", 340, 348, 4430, 4448),
            bi("down", 350, 360, 4445.0, 4425),   # 破前低但 4446 更松，不上移
            bi("down", 365, 375, 4440.0, 4438),   # 4438>4425 未破前低，忽略
            bi("up", 375, 378, 4438, 4450),
            bi("down", 380, 390, 4438.0, 4420),   # 4420<4438 破前低，止损 4439
            bi("up", 390, 500, 4420, 4450),
        ]
        r = advance_exit_decision(pos, 400, bar(400, 4450, 4451, 4448, 4450),
                                  mark_bis, px_bis, T7)
        self.assertIsNone(r)
        self.assertEqual(pos["stopRef"], 4439.0)
        self.assertTrue(pos["trailRaised"])
        raises = [e for e in pos["exits"] if e["type"] == "trailRaise"]
        self.assertEqual(len(raises), 1)
        self.assertEqual(raises[0]["moves"], 2)
        self.assertEqual(len(pos["trailMoves"]), 2)
        self.assertEqual(pos["trailMark"], 380)
        self.assertIn("检测周期结构点", raises[0]["why"])
        self.assertIn("下跌结构", raises[0]["why"])
        # 下一拍冲上 4439 → trailStop
        r = advance_exit_decision(pos, 500, bar(500, 4440, 4441, 4435, 4438),
                                  mark_bis, px_bis, T7)
        self.assertEqual(r, "trailStop")
        self.assertEqual(pos["pendingLots"], 4.0)
        tr = execute_pending_exit(pos, bar(600, 4438, 4442, 4430, 4435))
        self.assertEqual(tr["exitType"], "trailStop")
        self.assertEqual(tr["pnl"], (4438 - 4450) * (-1) * 4)

    def test_trail_mark_mode_uses_signal_flow(self):
        # mark 模式（旧口径）：背驰周期信号流 wait3Buy/waitBuy/… 同向3类点驱动；
        # 同拍的检测周期点流不消费；门控保本——TP1 前的信号点不追溯
        pos = self.make(exitCfg=ecfg(exitTrailOn=True, exitTrailSlip=1.0,
                                     exitTrailSrc="mark", exitHalfOn=False,
                                     exitCloseOn=False, exitHalfMrOn=False))
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
        # 同拍检测周期有破前低的下跌结构，止损会收到 4436，mark 模式不消费
        px_bis = [bi("down", 50, 200, 4460, 4450),
                  bi("up", 200, 300, 4450, 4455),
                  bi("down", 340, 360, 4435.0, 4430)]
        r = advance_exit_decision(pos, 400, bar(400, 4450, 4451, 4448, 4450),
                                  mark_bis, px_bis, T7, trail_sigs=sigs)
        self.assertIsNone(r)
        self.assertEqual(pos["stopRef"], 4443.0)   # 只上移到信号流点 4442+1
        raises = [e for e in pos["exits"] if e["type"] == "trailRaise"]
        self.assertEqual(len(raises), 1)
        self.assertIn("背驰周期信号流", raises[0]["why"])

    def test_trail_detect_long_structure_low(self):
        # 多单：保本后检测周期上涨笔起点（结构低点）→ 提损至该低点−滑点；跌破 → trailStop
        pos = self.make(direction="long", planDirection="多头多", entryPrice=4450.0,
                        stopRef=4437.0, beStop=4445.0,
                        exitCfg=ecfg(exitTrailOn=True, exitTrailSlip=1.0,
                                     exitHalfOn=False, exitCloseOn=False,
                                     exitHalfMrOn=False))
        mark_bis = [bi("down", 0, 100, 4460, 4440), bi("up", 100, 200, 4440, 4455),
                    bi("down", 200, 500, 4455, 4440)]  # up 笔(100→200) → TP1
        pre = [bi("up", 80, 160, 4440.0, 4465.0),
               bi("up", 200, 280, 4458.0, 4470),   # 4470>4465 但起点早于水位
               bi("down", 280, 300, 4470, 4450)]
        r = advance_exit_decision(pos, 300, bar(300, 4462, 4465, 4455, 4460),
                                  mark_bis, pre, T7)
        self.assertIsNone(r)
        self.assertTrue(pos["beDone"])
        self.assertFalse(pos["trailRaised"])
        px_bis = pre + [bi("up", 320, 400, 4458.0, 4480),  # 4480>4470 破前高，低点 4458
                        bi("down", 400, 500, 4480, 4460)]
        r = advance_exit_decision(pos, 400, bar(400, 4462, 4465, 4455, 4460),
                                  mark_bis, px_bis, T7)
        self.assertIsNone(r)
        self.assertEqual(pos["stopRef"], 4457.0)   # 结构低点 4458 − 滑点 1
        self.assertTrue(pos["trailRaised"])
        r = advance_exit_decision(pos, 500, bar(500, 4455, 4456, 4450, 4452),
                                  mark_bis, px_bis, T7)
        self.assertEqual(r, "trailStop")

    def test_trail_off_by_default(self):
        # 显式关闭跟踪止盈：下跌结构点不引起上移（全局默认已是开）
        pos = self.make(exitCfg=ecfg(exitTrailOn=False))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]
        px_bis = [bi("down", 100, 180, 4460, 4450),
                  bi("down", 200, 300, 4442.0, 4430),  # 破前低，关闭时仍不提损
                  bi("up", 300, 400, 4430, 4450)]
        advance_exit_decision(pos, 400, bar(400, 4450, 4451, 4448, 4450),
                              mark_bis, px_bis, T7)
        self.assertEqual(pos["stopRef"], 4463.0)
        self.assertFalse(pos["trailRaised"])

    def test_trail_gated_before_breakeven(self):
        # 门控保本（2026-10-09）：保本迁移（TP1）之前同向参照点不提损、不接管——
        # 穿越初始止损位记 stopSr 而非 trailStop（2026-08-19 XAUUSD 案例回归）。
        # beDone 由 TP1/half/halfMr 置位；此处 markRes 无 signalTime 后的有利方向笔
        pos = self.make(exitCfg=ecfg(exitTrailOn=True, exitTrailSlip=1.0,
                                     exitHalfOn=False, exitCloseOn=False,
                                     exitHalfMrOn=False))
        mark_bis = [bi("up", 0, 500, 4440, 4455)]  # 无 TP1 → beDone 恒 False
        px_bis = [bi("down", 50, 100, 4460, 4450),
                  bi("down", 150, 300, 4442.0, 4430),  # 已破前低，但未保本不接管
                  bi("up", 300, 400, 4430, 4450)]
        r = advance_exit_decision(pos, 400, bar(400, 4450, 4464, 4448, 4452),
                                  mark_bis, px_bis, T7)
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
                                     exitTrailPct=25, exitHalfOn=False,
                                     exitCloseOn=False, exitHalfMrOn=False))
        pos["maxLoss"] = 4460.0
        mark_bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)]
        # t=300 TP1 保本；t=400 下跌结构高点 4444 → stopRef 4445（收紧）
        advance_exit_decision(pos, 300, bar(300, 4440, 4445, 4435, 4441),
                              mark_bis, [bi("up", 50, 300, 4440, 4450)], T7)
        px_bis = [bi("down", 200, 280, 4460, 4450),
                  bi("up", 280, 340, 4450, 4460),
                  bi("down", 350, 380, 4444.0, 4430),  # 4430<4450 破前低，止损 4445
                  bi("up", 380, 500, 4430, 4450)]
        advance_exit_decision(pos, 400, bar(400, 4450, 4451, 4448, 4450),
                              mark_bis, px_bis, T7)
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
