# -*- coding: utf-8 -*-
"""顺势参考周期方向判定（trading_plan.trend_direction）与进出场过滤单元测试。

覆盖（2026-09-15 与用户确认的口径，规则见 WEB 参数页交易计划页签）：
  - 强分型 strong_fractal_after：右肩收盘穿左肩最高/最低；t 之前的不算；肩不完整不算
  - 2买出现即判多（结构底路径真实构造，无 monkeypatch）
  - 破坏闩锁：点确立后收盘跌破买点端点价 → 下跌延续；价格收回不翻回（直到新点）
  - 1买须强分型：未出现 → 回退末笔方向；出现 → 确立（monkeypatch 点与强分型）
  - 卖点镜像（2卖即判空、涨破 → 上涨延续）
  - trend_state_of：关闭 None / 参考周期无笔 dir=None / 上级周期自动选取
  - compute_entries / evaluateRealtimeEntries：参考周期及以上被剔出检测周期、
    逆参考周期方向信号被滤、信号附 trendDirection/trendReason（trend_state 由
    调用方传入）；trend_res=""（关闭）时行为不变
  - 相位树（2026-09-17 图片决策树，rebound 开启；闩锁优先）：
    1卖后 9 叶（下跌不够笔/够笔×近中枢上下沿×角度强弱/其他位置/反弹三岔/下跌未开始）、
    2\3卖后（前低附近/够笔角度强弱/反弹中默认观望）、1买与2\3买镜像、
    rebound=None / enabled=False 退旧行为、trend_state_of cfg 组装与缓存、
    观望态（dir=None + reason）双向放行且信号带注记

运行：python -m unittest py_chain.test_trend_filter -v
"""

import unittest
from unittest import mock

from py_chain import trading_plan as tp
from py_chain import mark_entry as me


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice,
            "span": abs(endPrice - startPrice)}


def bar(t, o, c, h=None, l=None):
    return {"time": t, "open": o, "close": c,
            "high": h if h is not None else max(o, c), "low": l if l is not None else min(o, c)}


# 4h bar 时长（秒）
S240 = 14400


def bottom_fixture_bis():
    """结构底路径产出 2买 的 4h 笔序列（无上级笔）：
    b1 低点 90 = 结构底；b3 低点 95 > 90 → 2买 @ b3.endTime（价格 95）。
    同窗口 2卖 @ b2.endTime（108 < 结构顶 110）早于 2买 → 最近点 = 2买。
    """
    return [
        bi("up", 0, S240, 100, 110),            # b0 结构顶 110
        bi("down", S240, 2 * S240, 110, 90),    # b1 结构底 90
        bi("up", 2 * S240, 3 * S240, 90, 108),  # b2（2卖锚：108 < 110）
        bi("down", 3 * S240, 4 * S240, 108, 95)  # b3 末笔 down → 2买 @ 4*S240 价格 95
    ]


class TestStrongFractalAfter(unittest.TestCase):
    def merged3(self, close_right=107.0):
        """三根合并K：中心为底分型；右肩收盘可调（107 > 左肩最高 106 → 强）。"""
        return [
            {"high": 106.0, "low": 96.0, "close": 100.0, "time": 0,
             "highTime": 0, "lowTime": 0},
            {"high": 104.0, "low": 90.0, "close": 98.0, "time": 1,
             "highTime": 1, "lowTime": 1},
            {"high": 108.0, "low": 92.0, "close": close_right, "time": 2,
             "highTime": 2, "lowTime": 2},
        ]

    def test_bottom_strong_and_weak(self):
        merged = self.merged3(close_right=107.0)
        frs = tp.findFractals(merged)
        self.assertEqual(len(frs), 1)
        self.assertEqual(frs[0]["type"], "bottom")
        self.assertTrue(tp.strong_fractal_after(merged, frs, 1, "bottom"))
        # 右肩收盘 103 ≤ 左肩最高 106 → 非强分型
        merged2 = self.merged3(close_right=103.0)
        frs2 = tp.findFractals(merged2)
        self.assertFalse(tp.strong_fractal_after(merged2, frs2, 1, "bottom"))

    def test_before_t_not_counted(self):
        merged = self.merged3()
        frs = tp.findFractals(merged)
        self.assertFalse(tp.strong_fractal_after(merged, frs, 2, "bottom"))

    def test_incomplete_shoulder(self):
        # 右肩缺（分型中心是最后一根合并K）→ 不计
        merged = self.merged3()[:2]
        frs = [{"type": "bottom", "mergedIdx": 1, "time": 1, "high": 104, "low": 90}]
        self.assertFalse(tp.strong_fractal_after(merged, frs, 1, "bottom"))

    def test_top_mirror(self):
        # 顶分型三根：左肩 low 95、中心 high 106、右肩收盘 93 < 95 → 强
        merged = [
            {"high": 104.0, "low": 95.0, "close": 100.0, "time": 0,
             "highTime": 0, "lowTime": 0},
            {"high": 106.0, "low": 96.0, "close": 102.0, "time": 1,
             "highTime": 1, "lowTime": 1},
            {"high": 103.0, "low": 91.0, "close": 93.0, "time": 2,
             "highTime": 2, "lowTime": 2},
        ]
        frs = tp.findFractals(merged)
        self.assertEqual(frs[0]["type"], "top")
        self.assertTrue(tp.strong_fractal_after(merged, frs, 1, "top"))


class TestTrendDirection(unittest.TestCase):
    def test_insufficient_bis(self):
        self.assertEqual(tp.trend_direction("240", [], [], None, []), (None, ""))
        self.assertEqual(tp.trend_direction("240", [bi("up", 0, S240, 1, 2)], [], None, []),
                         (None, ""))

    def test_second_buy_immediate_long(self):
        # 结构底路径真实构造：最近点 2买 → 即判多（无需强分型）
        bars = [bar(t * S240, 100, 96) for t in range(5)]
        d, r = tp.trend_direction("240", bottom_fixture_bis(), bars, None, [])
        self.assertEqual((d, r), ("long", "4小时2买"))

    def test_break_latch(self):
        # b3 之后（t=4*S240 起）任一收盘 < 95 → 下跌延续；价格收回不翻回
        bis = bottom_fixture_bis()
        bars = [bar(3 * S240, 100, 96), bar(4 * S240, 95, 94), bar(5 * S240, 94, 99)]
        d, r = tp.trend_direction("240", bis, bars, None, [])
        self.assertEqual((d, r), ("short", "4小时下跌延续"))

    def test_no_points_fallback_last_bi(self):
        # 无任何买卖点 → 回退末笔方向（末笔 down → 空）
        bis = [
            bi("up", 0, S240, 100, 110),
            bi("down", S240, 2 * S240, 110, 105),  # 低点 105 高于结构底? 无更高低点结构
        ]
        # 两笔太少（<3 findBuyPoints 直接返回 []）→ 回退末笔
        bars = [bar(t * S240, 100, 103) for t in range(3)]
        d, r = tp.trend_direction("240", bis, bars, None, [])
        self.assertEqual((d, r), ("short", "4小时末笔向下"))
        # 末笔 up → 多
        bis_up = [bi("down", 0, S240, 110, 100), bi("up", S240, 2 * S240, 100, 108)]
        d, r = tp.trend_direction("240", bis_up, bars, None, [])
        self.assertEqual((d, r), ("long", "4小时末笔向上"))

    def test_first_buy_needs_strong_fractal(self):
        bis = bottom_fixture_bis()
        bars = [bar(t * S240, 100, 96) for t in range(5)]
        pt = [{"type": "1买", "time": 4 * S240, "price": 95}]
        with mock.patch.object(tp, "findBuyPoints", return_value=pt), \
                mock.patch.object(tp, "findSellPoints", return_value=[]), \
                mock.patch.object(tp, "strong_fractal_after", return_value=False):
            d, r = tp.trend_direction("240", bis, bars, None, [])
        self.assertEqual((d, r), ("short", "4小时末笔向下"))  # 强分型未出现 → 回退末笔
        with mock.patch.object(tp, "findBuyPoints", return_value=pt), \
                mock.patch.object(tp, "findSellPoints", return_value=[]), \
                mock.patch.object(tp, "strong_fractal_after", return_value=True):
            d, r = tp.trend_direction("240", bis, bars, None, [])
        self.assertEqual((d, r), ("long", "4小时1买"))  # 强分型出现 → 确立

    def test_class2_and_third_buy_same_as_second(self):
        bis = bottom_fixture_bis()
        bars = [bar(t * S240, 100, 96) for t in range(5)]
        for t_ in ("类2买", "3买"):
            with mock.patch.object(tp, "findBuyPoints",
                                   return_value=[{"type": t_, "time": 4 * S240, "price": 95}]), \
                    mock.patch.object(tp, "findSellPoints", return_value=[]):
                d, r = tp.trend_direction("240", bis, bars, None, [])
            self.assertEqual((d, r), ("long", f"4小时{t_}"))

    def test_sell_mirror(self):
        # 结构顶路径镜像：结构顶 111（b1 高点），其后更高的?——b3 高点 108 < 111 →
        # 2卖锚定 b3.endTime 价格 108；末笔 up（b3）本身不构成确立条件外的干扰
        bis = [
            bi("down", 0, S240, 110, 100),
            bi("up", S240, 2 * S240, 100, 111),    # b1 结构顶 111
            bi("down", 2 * S240, 3 * S240, 111, 105),
            bi("up", 3 * S240, 4 * S240, 105, 108),  # 末笔 up，2卖 @ 4*S240 价格 108
        ]
        bars_ok = [bar(t * S240, 107, 106) for t in range(5)]  # 收盘均 < 108
        d, r = tp.trend_direction("240", bis, bars_ok, None, [])
        self.assertEqual((d, r), ("short", "4小时2卖"))
        # 涨破卖点端点价（收盘 > 108）→ 上涨延续
        bars_break = [bar(4 * S240, 108, 108.5), bar(5 * S240, 108.5, 110)]
        d, r = tp.trend_direction("240", bis, bars_break, None, [])
        self.assertEqual((d, r), ("long", "4小时上涨延续"))


def rb_cfg(enabled=True, near=5.0, angle=5.0, min_bars=5):
    """相位判定参数（trend_direction 的 rebound 形参）。"""
    return {"enabled": enabled, "near_pts": near, "angle_ref": angle,
            "min_bars": min_bars}


def sell1_down_bis():
    """1卖锚 @ 2*S240 价格 111；末笔 down（span 10，极值 96），锚点后有确认下跌笔。"""
    return [
        bi("down", 0, S240, 120, 100),
        bi("up", S240, 2 * S240, 100, 111),
        bi("down", 2 * S240, 3 * S240, 111, 104),  # 确认下跌（合并K够笔）
        bi("up", 3 * S240, 4 * S240, 104, 106),
        bi("down", 4 * S240, 5 * S240, 106, 96),
    ]


def sell1_fresh_bis():
    """1卖锚 @ 2*S240；末笔 down 未确认、锚点后无确认下跌笔（够笔只看K线数）。"""
    return [
        bi("down", 0, S240, 120, 100),
        bi("up", S240, 2 * S240, 100, 111),
        bi("down", 2 * S240, 3 * S240, 111, 104),
    ]


def sell1_up_bis():
    """1卖锚 @ 2*S240；末笔 up = 反弹段（span 6），下跌已确认。"""
    return [
        bi("down", 0, S240, 120, 100),
        bi("up", S240, 2 * S240, 100, 111),
        bi("down", 2 * S240, 3 * S240, 111, 98),
        bi("up", 3 * S240, 4 * S240, 98, 104),
    ]


def sell1_up_multileg_bis():
    """1卖锚 @ 2*S240；末笔 up（span 6）且锚点后有确认反弹笔（反弹合并K够笔）。"""
    return [
        bi("down", 0, S240, 120, 100),
        bi("up", S240, 2 * S240, 100, 111),
        bi("down", 2 * S240, 3 * S240, 111, 98),
        bi("up", 3 * S240, 4 * S240, 98, 110),
        bi("down", 4 * S240, 5 * S240, 110, 105),
        bi("up", 5 * S240, 6 * S240, 105, 111),
    ]


def sell23_down_bis():
    """2卖锚 @ 2*S240 价格 111；前低 95；末笔 down（span 8，极值 96）合并K够笔。"""
    return [
        bi("down", 0, S240, 120, 95),
        bi("up", S240, 2 * S240, 95, 111),
        bi("down", 2 * S240, 3 * S240, 111, 100),
        bi("up", 3 * S240, 4 * S240, 100, 104),
        bi("down", 4 * S240, 5 * S240, 104, 96),
    ]


def sell23_fresh_bis():
    """2卖锚 @ 2*S240；前低 80（远离末笔极值 100）；末笔 down 未确认（不够笔）。"""
    return [
        bi("down", 0, S240, 120, 80),
        bi("up", S240, 2 * S240, 80, 111),
        bi("down", 2 * S240, 3 * S240, 111, 100),
    ]


def sell23_up_bis():
    """2卖锚 @ 2*S240；末笔 up（反弹中，未涨破卖点价）。"""
    return [
        bi("down", 0, S240, 120, 95),
        bi("up", S240, 2 * S240, 95, 111),
        bi("down", 2 * S240, 3 * S240, 111, 100),
        bi("up", 3 * S240, 4 * S240, 100, 106),
    ]


def buy1_up_bis():
    """1买锚 @ 2*S240 价格 89；末笔 up（span 10，极值 104），锚点后有确认上涨笔。"""
    return [
        bi("up", 0, S240, 100, 120),
        bi("down", S240, 2 * S240, 120, 89),
        bi("up", 2 * S240, 3 * S240, 89, 96),
        bi("down", 3 * S240, 4 * S240, 96, 94),
        bi("up", 4 * S240, 5 * S240, 94, 104),
    ]


def buy1_fresh_pullback_bis():
    """1买锚 @ 2*S240；末笔 down 回调未确认（回调不够笔）。"""
    return [
        bi("up", 0, S240, 100, 120),
        bi("down", S240, 2 * S240, 120, 89),
        bi("up", 2 * S240, 3 * S240, 89, 96),
        bi("down", 3 * S240, 4 * S240, 96, 94),
    ]


def buy1_pullback_multileg_bis():
    """1买锚 @ 2*S240；末笔 down（span 6）回调且锚点后有确认回调笔（够笔）。"""
    return [
        bi("up", 0, S240, 100, 120),
        bi("down", S240, 2 * S240, 120, 89),
        bi("up", 2 * S240, 3 * S240, 89, 116),
        bi("down", 3 * S240, 4 * S240, 116, 110),
        bi("up", 4 * S240, 5 * S240, 110, 111),
        bi("down", 5 * S240, 6 * S240, 111, 105),
    ]


def buy23_up_bis():
    """2买锚 @ 2*S240 价格 105；前高 125；末笔 up（span 16，极值 124）合并K够笔。"""
    return [
        bi("up", 0, S240, 100, 125),
        bi("down", S240, 2 * S240, 125, 105),
        bi("up", 2 * S240, 3 * S240, 105, 114),
        bi("down", 3 * S240, 4 * S240, 114, 108),
        bi("up", 4 * S240, 5 * S240, 108, 124),
    ]


def buy23_down_bis():
    """2买锚 @ 2*S240；末笔 down（回调中，未跌破买点价）。"""
    return [
        bi("up", 0, S240, 100, 125),
        bi("down", S240, 2 * S240, 125, 105),
        bi("up", 2 * S240, 3 * S240, 105, 114),
        bi("down", 3 * S240, 4 * S240, 114, 104),
    ]


class TestPhaseSell1Anchor(unittest.TestCase):
    """1卖锚点相位树（rebound 开启；强分型已过、闩锁未触发；闩锁优先）。"""

    ANCHOR = [{"type": "1卖", "time": 2 * S240, "price": 111.0}]

    def _run(self, bis, bars, rebound, zs=None, upper=None, merged_count=5):
        with mock.patch.object(tp, "findBuyPoints", return_value=[]), \
                mock.patch.object(tp, "findSellPoints", return_value=self.ANCHOR), \
                mock.patch.object(tp, "strong_fractal_after", return_value=True), \
                mock.patch.object(tp, "buildZS", return_value=zs or []), \
                mock.patch.object(tp, "buildZSByUpper", return_value=zs or []), \
                mock.patch.object(tp, "mergedSegmentCount", return_value=merged_count):
            return tp.trend_direction("240", bis, bars, upper, [], rebound=rebound)

    def test_down_not_enough(self):
        # 末笔 down 未确认、锚点后无确认下跌笔、K线数 < 5 → 空（1卖进行中）
        bars = [bar(t * S240, 100, 100) for t in range(4)]
        self.assertEqual(self._run(sell1_fresh_bis(), bars, rb_cfg(), merged_count=4),
                         ("short", "4小时1卖进行中"))

    def test_down_enough_near_zs_weak_strong(self):
        # 已满足5块合并K（独立测试覆盖真实计数）；极值 96 近中枢（zg 99/zd 97，容差 5）：角度弱（10/2=5≤5）→ 转多预期
        near_zs = [{"startTime": 0, "zg": 99.0, "zd": 97.0}]
        bars = [bar(t * S240, 100, 100) for t in range(7)]
        self.assertEqual(self._run(sell1_down_bis(), bars, rb_cfg(), zs=near_zs),
                         ("long", "4小时1卖转多预期"))
        # 角度强（10/1=10>5）→ 近中枢观望
        bars_strong = [bar(t * S240, 100, 100) for t in range(6)]
        self.assertEqual(self._run(sell1_down_bis(), bars_strong, rb_cfg(), zs=near_zs),
                         (None, "4小时1卖近中枢观望"))

    def test_down_enough_far_zs(self):
        # 不近中枢（zg 90/zd 85 距极值 96 > 5）→ 其他位置：强 → 空进行中；弱 → 观望
        far_zs = [{"startTime": 0, "zg": 90.0, "zd": 85.0}]
        bars_weak = [bar(t * S240, 100, 100) for t in range(7)]      # 10/2=5 → 弱
        bars_strong = [bar(t * S240, 100, 100) for t in range(6)]    # 10/1=10 → 强
        self.assertEqual(self._run(sell1_down_bis(), bars_weak, rb_cfg(), zs=far_zs),
                         (None, "4小时1卖后方向不明"))
        self.assertEqual(self._run(sell1_down_bis(), bars_strong, rb_cfg(), zs=far_zs),
                         ("short", "4小时1卖进行中"))

    def test_down_enough_by_bars(self):
        # 够笔也可由K线数满足（锚点后 6 根 > 5）；近中枢弱角度 → 转多预期
        near_zs = [{"startTime": 0, "zg": 106.0, "zd": 101.0}]  # 极值 104：|104-106|=2 ≤ 5
        bars = [bar(t * S240, 100, 100) for t in range(3, 9)]    # (2*S240, …] 共 6 根
        self.assertEqual(self._run(sell1_fresh_bis(), bars, rb_cfg(), zs=near_zs),
                         ("long", "4小时1卖转多预期"))

    def test_zs_after_anchor_ignored(self):
        # 中枢 startTime 晚于锚点 → 不算锚点前中枢 → 其他位置（弱 → 观望）
        late_zs = [{"startTime": 3 * S240, "zg": 99.0, "zd": 97.0}]
        bars = [bar(t * S240, 100, 100) for t in range(7)]
        self.assertEqual(self._run(sell1_down_bis(), bars, rb_cfg(), zs=late_zs),
                         (None, "4小时1卖后方向不明"))

    def test_zs_via_upper(self):
        # 有上级笔时中枢走 buildZSByUpper（mock 同返回值）→ 近中枢弱角度 → 转多预期
        near_zs = [{"startTime": 0, "zg": 99.0, "zd": 97.0}]
        bars = [bar(t * S240, 100, 100) for t in range(7)]
        self.assertEqual(self._run(sell1_down_bis(), bars, rb_cfg(), zs=near_zs,
                                   upper=[bi("up", 0, 8 * S240, 90, 130)]),
                         ("long", "4小时1卖转多预期"))

    def test_up_rebound_not_enough(self):
        # 反弹段不够笔（K线 1 根 < 5，锚点后无确认反弹笔）→ 多（1卖反弹）
        bars = [bar(t * S240, 100, 100) for t in range(5)]
        self.assertEqual(self._run(sell1_up_bis(), bars, rb_cfg(), merged_count=4),
                         ("long", "4小时1卖反弹"))

    def test_up_rebound_enough_weak(self):
        # 反弹够笔（6 根）+ 力度弱（6/6=1≤5）→ 空（2卖预期）
        bars = [bar(t * S240, 100, 100) for t in range(3, 10)]
        self.assertEqual(self._run(sell1_up_bis(), bars, rb_cfg()),
                         ("short", "4小时2卖预期"))

    def test_up_rebound_enough_strong(self):
        # 反弹合并K够笔 + 力度强（6/1=6>5）→ 多（1卖强反）
        bars = [bar(t * S240, 100, 100) for t in range(7)]
        self.assertEqual(self._run(sell1_up_multileg_bis(), bars, rb_cfg()),
                         ("long", "4小时1卖强反"))

    def test_up_decline_not_started(self):
        # 末笔 up 起点 ≤ 锚点（下跌未开始）→ 空（1卖进行中）
        bis = [
            bi("down", 0, S240, 120, 100),
            bi("up", S240, 3 * S240, 100, 111),
        ]
        bars = [bar(t * S240, 100, 100) for t in range(4)]
        self.assertEqual(self._run(bis, bars, rb_cfg()),
                         ("short", "4小时1卖进行中"))

    def test_latch_priority(self):
        # 闩锁优先：收盘涨破卖点价 111 → 上涨延续（相位树不再参与）
        bars = [bar(t * S240, 100, 100) for t in range(7)]
        bars.append(bar(8 * S240, 111, 112))
        self.assertEqual(self._run(sell1_down_bis(), bars, rb_cfg(),
                                   zs=[{"startTime": 0, "zg": 99.0, "zd": 97.0}]),
                         ("long", "4小时上涨延续"))

    def test_rebound_off_legacy(self):
        # rebound=None / enabled=False → 旧行为：确立返回 空（4小时1卖）
        bars = [bar(t * S240, 100, 100) for t in range(7)]
        self.assertEqual(self._run(sell1_down_bis(), bars, None),
                         ("short", "4小时1卖"))
        self.assertEqual(self._run(sell1_down_bis(), bars, rb_cfg(enabled=False)),
                         ("short", "4小时1卖"))


class TestPhaseSell23Anchor(unittest.TestCase):
    """2\3卖锚点相位树（非1类点也走相位；前低附近/够笔角度/反弹中默认观望）。"""

    ANCHOR = [{"type": "2卖", "time": 2 * S240, "price": 111.0}]

    def _run(self, bis, bars, rebound, anchor=None, merged_count=5):
        with mock.patch.object(tp, "findBuyPoints", return_value=[]), \
                mock.patch.object(tp, "findSellPoints",
                                  return_value=anchor or self.ANCHOR), \
                mock.patch.object(tp, "buildZS", return_value=[]), \
                mock.patch.object(tp, "buildZSByUpper", return_value=[]), \
                mock.patch.object(tp, "mergedSegmentCount", return_value=merged_count):
            return tp.trend_direction("240", bis, bars, None, [], rebound=rebound)

    def test_near_prev_low(self):
        # 极值 96 距前低 95 ≤ 5 → 观望（前低附近）
        bars = [bar(t * S240, 100, 100) for t in range(7)]
        self.assertEqual(self._run(sell23_down_bis(), bars, rb_cfg()),
                         (None, "4小时2卖前低附近"))

    def test_far_prev_low_angle(self):
        # 容差收紧（0.5）→ 其他位置；合并K够笔：弱（8/2=4≤5）→ 观望；强（8/1=8>5）→ 空进行中
        bars_weak = [bar(t * S240, 100, 100) for t in range(7)]
        bars_strong = [bar(t * S240, 100, 100) for t in range(6)]
        self.assertEqual(self._run(sell23_down_bis(), bars_weak, rb_cfg(near=0.5)),
                         (None, "4小时2卖后方向不明"))
        self.assertEqual(self._run(sell23_down_bis(), bars_strong, rb_cfg(near=0.5)),
                         ("short", "4小时2卖后下跌"))

    def test_not_enough(self):
        # 末笔 down 未确认、远离前低（100 vs 80）→ 不够笔 → 空进行中
        bars = [bar(t * S240, 100, 100) for t in range(4)]
        self.assertEqual(self._run(sell23_fresh_bis(), bars, rb_cfg(), merged_count=4),
                         ("short", "4小时2卖后下跌"))

    def test_up_rebound_wait(self):
        # 形成段向上（反弹中、未涨破卖点价）→ 观望（图未覆盖，2026-09-17 用户确认）
        bars = [bar(t * S240, 100, 100) for t in range(6)]
        self.assertEqual(self._run(sell23_up_bis(), bars, rb_cfg()),
                         (None, "4小时2卖反弹中"))

    def test_class2_and_third_same(self):
        # 类2卖/3卖/类3卖 与 2卖 同树
        bars = [bar(t * S240, 100, 100) for t in range(7)]
        for t_ in ("类2卖", "3卖", "类3卖"):
            self.assertEqual(
                self._run(sell23_down_bis(), bars, rb_cfg(),
                          anchor=[{"type": t_, "time": 2 * S240, "price": 111.0}]),
                (None, f"4小时{t_}前低附近"))

    def test_latch_priority(self):
        # 闩锁优先：收盘涨破 111 → 上涨延续
        bars = [bar(t * S240, 100, 100) for t in range(7)]
        bars.append(bar(8 * S240, 111, 112))
        self.assertEqual(self._run(sell23_down_bis(), bars, rb_cfg()),
                         ("long", "4小时上涨延续"))


class TestPhaseBuyMirror(unittest.TestCase):
    """1买 / 2\3买 完全镜像抽查。"""

    def _run(self, bis, bars, anchor, rebound, zs=None, merged_count=5):
        with mock.patch.object(tp, "findBuyPoints", return_value=anchor), \
                mock.patch.object(tp, "findSellPoints", return_value=[]), \
                mock.patch.object(tp, "strong_fractal_after", return_value=True), \
                mock.patch.object(tp, "buildZS", return_value=zs or []), \
                mock.patch.object(tp, "buildZSByUpper", return_value=zs or []), \
                mock.patch.object(tp, "mergedSegmentCount", return_value=merged_count):
            return tp.trend_direction("240", bis, bars, None, [], rebound=rebound)

    def test_buy1_up_enough_near_zs(self):
        # 够笔+近中枢（极值 104 vs zg 101）：角度弱（10/2=5）→ 转空预期；强 → 近中枢观望
        anchor = [{"type": "1买", "time": 2 * S240, "price": 89.0}]
        near_zs = [{"startTime": 0, "zg": 101.0, "zd": 99.0}]
        self.assertEqual(self._run(buy1_up_bis(),
                                   [bar(t * S240, 100, 100) for t in range(7)],
                                   anchor, rb_cfg(), zs=near_zs),
                         ("short", "4小时1买转空预期"))
        self.assertEqual(self._run(buy1_up_bis(),
                                   [bar(t * S240, 100, 100) for t in range(6)],
                                   anchor, rb_cfg(), zs=near_zs),
                         (None, "4小时1买近中枢观望"))

    def test_buy1_up_other_position(self):
        # 其他位置：角度弱 → 观望；强 → 多（1买进行中）
        anchor = [{"type": "1买", "time": 2 * S240, "price": 89.0}]
        far_zs = [{"startTime": 0, "zg": 90.0, "zd": 85.0}]
        self.assertEqual(self._run(buy1_up_bis(),
                                   [bar(t * S240, 100, 100) for t in range(7)],
                                   anchor, rb_cfg(), zs=far_zs),
                         (None, "4小时1买后方向不明"))
        self.assertEqual(self._run(buy1_up_bis(),
                                   [bar(t * S240, 100, 100) for t in range(6)],
                                   anchor, rb_cfg(), zs=far_zs),
                         ("long", "4小时1买进行中"))

    def test_buy1_pullback(self):
        # 回调不够笔 → 空（1买回调）；够笔+力度弱（6/2=3）→ 多（2买预期）；强（6/1=6）→ 强回
        anchor = [{"type": "1买", "time": 2 * S240, "price": 89.0}]
        self.assertEqual(self._run(buy1_fresh_pullback_bis(),
                                   [bar(t * S240, 100, 100) for t in range(5)],
                                   anchor, rb_cfg(), merged_count=4),
                         ("short", "4小时1买回调"))
        self.assertEqual(self._run(buy1_pullback_multileg_bis(),
                                   [bar(t * S240, 100, 100) for t in range(8)],
                                   anchor, rb_cfg()),
                         ("long", "4小时2买预期"))
        self.assertEqual(self._run(buy1_pullback_multileg_bis(),
                                   [bar(t * S240, 100, 100) for t in range(7)],
                                   anchor, rb_cfg()),
                         ("short", "4小时1买强回"))

    def test_buy1_up_not_started(self):
        # 末笔 down 起点不晚于买点（上涨未开始）→ 多（1买进行中）
        bis = [
            bi("up", 0, S240, 100, 120),
            bi("down", S240, 3 * S240, 120, 89),
        ]
        anchor = [{"type": "1买", "time": 3 * S240, "price": 89.0}]
        self.assertEqual(self._run(bis, [bar(t * S240, 100, 100) for t in range(5)],
                                   anchor, rb_cfg()),
                         ("long", "4小时1买进行中"))

    def test_buy23(self):
        # 前高附近（极值 124 vs 前高 125）→ 观望；远离后角度强（16/2=8）→ 多进行中；
        # 角度弱（16/4=4）→ 观望；回调中 → 观望（收盘 ≥ 买点价 105，不触发闩锁）
        anchor = [{"type": "2买", "time": 2 * S240, "price": 105.0}]
        self.assertEqual(self._run(buy23_up_bis(),
                                   [bar(t * S240, 106, 106) for t in range(7)],
                                   anchor, rb_cfg()),
                         (None, "4小时2买前高附近"))
        self.assertEqual(self._run(buy23_up_bis(),
                                   [bar(t * S240, 106, 106) for t in range(7)],
                                   anchor, rb_cfg(near=0.5)),
                         ("long", "4小时2买后上涨"))
        self.assertEqual(self._run(buy23_up_bis(),
                                   [bar(t * S240, 106, 106) for t in range(9)],
                                   anchor, rb_cfg(near=0.5)),
                         (None, "4小时2买后方向不明"))
        self.assertEqual(self._run(buy23_down_bis(),
                                   [bar(t * S240, 106, 106) for t in range(6)],
                                   anchor, rb_cfg()),
                         (None, "4小时2买回调中"))


class TestTrendStateOfReboundCfg(unittest.TestCase):
    """trend_state_of 的相位参数组装（cfg → rebound）与缓存。"""

    def setUp(self):
        self.bis = bottom_fixture_bis()
        self.bars = [bar(t * S240, 100, 96) for t in range(5)]

    def test_cfg_values_passed(self):
        with mock.patch.object(tp, "trend_direction",
                               return_value=(None, "4小时1卖后方向不明")) as td:
            st = tp.trend_state_of({"240": self.bis}, {"240": self.bars}, "240",
                                   cfg={"trendRebound": True, "reboundNearPts": 3.0,
                                        "reboundAngleRef": 7.0})
        self.assertEqual(st, {"dir": None, "reason": "4小时1卖后方向不明", "res": "240"})
        self.assertEqual(td.call_args[1]["rebound"],
                         {"enabled": True, "near_pts": 3.0, "angle_ref": 7.0,
                          "min_bars": 5})

    def test_cfg_off_passes_none(self):
        with mock.patch.object(tp, "trend_direction",
                               return_value=("short", "4小时1卖")) as td:
            tp.trend_state_of({"240": self.bis}, {"240": self.bars}, "240",
                              cfg={"trendRebound": False})
        self.assertIsNone(td.call_args[1]["rebound"])

    def test_no_cfg_module_defaults(self):
        with mock.patch.object(tp, "trend_direction",
                               return_value=("short", "4小时1卖")) as td:
            tp.trend_state_of({"240": self.bis}, {"240": self.bars}, "240")
        self.assertEqual(td.call_args[1]["rebound"],
                         {"enabled": True, "near_pts": tp.REBOUND_NEAR_PTS,
                          "angle_ref": tp.REBOUND_ANGLE_REF, "min_bars": 5})

    def test_cache_reuses_and_cfg_sensitive(self):
        wc = {}
        with mock.patch.object(tp, "trend_direction",
                               return_value=("short", "4小时1卖")) as td:
            tp.trend_state_of({"240": self.bis}, {"240": self.bars}, "240",
                              work_cache=wc, cfg={"reboundNearPts": 3.0})
            tp.trend_state_of({"240": self.bis}, {"240": self.bars}, "240",
                              work_cache=wc, cfg={"reboundNearPts": 3.0})
            self.assertEqual(td.call_count, 1)  # 同参 → 缓存命中
            tp.trend_state_of({"240": self.bis}, {"240": self.bars}, "240",
                              work_cache=wc, cfg={"reboundNearPts": 9.0})
            self.assertEqual(td.call_count, 2)  # 参数变化 → 重算


class TestTrendStateOf(unittest.TestCase):
    def test_disabled(self):
        self.assertIsNone(tp.trend_state_of({}, {}, ""))
        self.assertIsNone(tp.trend_state_of({}, {}, None))

    def test_no_bis_pass_all(self):
        st = tp.trend_state_of({"240": []}, {"240": []}, "240")
        self.assertEqual(st, {"dir": None, "reason": "", "res": "240"})

    def test_upper_auto_pick(self):
        # 240 的上级取 D（periodBis 中比 240 大一级的最小周期）
        bis = bottom_fixture_bis()
        bars = [bar(t * S240, 100, 96) for t in range(5)]
        with mock.patch.object(tp, "trend_direction",
                               return_value=("long", "4小时2买")) as td:
            st = tp.trend_state_of({"240": bis, "D": bis, "60": bis},
                                   {"240": bars, "D": bars, "60": bars}, "240")
        self.assertEqual(st, {"dir": "long", "reason": "4小时2买", "res": "240"})
        # 上级笔 = D 笔（findBuyPoints 区间套入参）
        self.assertEqual([{k:b[k] for k in original} for b,original in zip(td.call_args[0][3],bis)], bis)

    def test_daily_anchor_name(self):
        bis = bottom_fixture_bis()
        bars = [bar(t * 86400, 100, 96) for t in range(5)]
        with mock.patch.object(tp, "trend_direction",
                               return_value=("short", "日线1卖")):
            st = tp.trend_state_of({"D": bis, "240": bis}, {"D": bars}, "D")
        self.assertEqual(st["res"], "D")
        self.assertEqual(st["reason"], "日线1卖")


class TestEntryFiltering(unittest.TestCase):
    """compute_entries / evaluateRealtimeEntries 的顺势过滤行为。

    完整链路 fixture 较重，这里用 mock 屏蔽评估细节、只验证过滤层：
      - 参考周期及以上被剔出检测周期（即使无周期数据也结构性剔除）
      - trend_dir 与策略方向相反 → 不产生信号；一致 → 信号附 trend 字段
      - trend_res=""（关闭）→ 无剔除、不过滤、无 trend 字段
    """

    def _entries_with(self, trend_res, trend_state, plan_direction="空头空",
                      plan_strategy="等待反弹后做2卖"):
        """构造 60m 检测周期一笔空头计划，收集 evaluateRealtimeEntries 输出。"""
        bis60 = [
            bi("up", 0, 3600, 100, 110),
            bi("down", 3600, 7200, 110, 105),
            bi("up", 7200, 10800, 105, 108),
        ]
        periodBis = {"240": bottom_fixture_bis(), "60": bis60, "15": bis60, "3": bis60}
        periodTimes = {r: [b["startTime"], b["endTime"]] for r, b in
                       ((r, periodBis[r][0]) for r in periodBis)}
        plan = {"60": {"direction": plan_direction, "strategy": plan_strategy}}
        with mock.patch.object(me, "realtimeLowerDiverge",
                               return_value=[{"res": "15", "segStart": 7200,
                                              "point": {"time": 9000, "price": 105.0}}]), \
                mock.patch.object(me, "counterMoveQualifies", return_value=True), \
                mock.patch.object(me, "mergedSegmentCount", return_value=99), \
                mock.patch.object(me, "strategyExtraOk", return_value=None), \
                mock.patch.object(me, "nearSr",
                                  return_value={"sr": {"price": 105.0}}):
            sigs = me.evaluateRealtimeEntries(
                periodBis, {}, {}, plan, [{"price": 105.0}],
                ["240", "60", "15", "3"], trend_res=trend_res,
                trend_state=trend_state, fired=set())
        return sigs

    def test_anchor_period_excluded(self):
        # 240（参考周期）被结构性剔除：即使其 plan 有策略也不出信号
        # （fixture 的 plan 只给 60，240 无 plan 本身也跳过——这里验证剔除优先级：
        #  detectPeriods 含 240 时输出无 240 检测周期的信号）
        sigs = self._entries_with("240", {"dir": "short", "reason": "4小时2卖", "res": "240"})
        self.assertTrue(all(s["periodX"] != "240" for s in sigs))

    def test_direction_gating_and_fields(self):
        # 参考周期判空（4小时2卖 reason 但 dir=short 的合成态）→ 60m 空头信号放行并附 trend 字段
        sigs = self._entries_with("240", {"dir": "short", "reason": "4小时2卖", "res": "240"})
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["trendDirection"], "short")
        self.assertEqual(sigs[0]["trendReason"], "4小时2卖")
        # 参考周期判多 → 空头信号被滤
        sigs = self._entries_with("240", {"dir": "long", "reason": "4小时2买", "res": "240"})
        self.assertEqual(sigs, [])

    def test_no_dir_pass_all(self):
        # 参考周期无方向（dir=None）→ 不过滤；信号无 trend 字段
        sigs = self._entries_with("240", {"dir": None, "reason": "", "res": "240"})
        self.assertEqual(len(sigs), 1)
        self.assertNotIn("trendReason", sigs[0])

    def test_wait_state_passes_with_note(self):
        # 观望态（dir=None 但 reason 非空）→ 双向放行且信号带注记（2026-09-17 相位树）
        sigs = self._entries_with("240", {"dir": None, "reason": "4小时1卖后方向不明",
                                          "res": "240"})
        self.assertEqual(len(sigs), 1)
        self.assertIsNone(sigs[0]["trendDirection"])
        self.assertEqual(sigs[0]["trendReason"], "4小时1卖后方向不明")

    def test_disabled_no_filter(self):
        sigs = self._entries_with("", None)
        self.assertEqual(len(sigs), 1)
        self.assertNotIn("trendReason", sigs[0])

    def test_daily_anchor_keeps_240_detect(self):
        # trendRes=D：240 恢复检测（周期剔除只针对参考周期及以上）——
        # 用 plan 含 240 验证 240 信号可产生
        bis60 = [
            bi("up", 0, 3600, 100, 110),
            bi("down", 3600, 7200, 110, 105),
            bi("up", 7200, 10800, 105, 108),
        ]
        periodBis = {"D": bottom_fixture_bis(), "240": bis60, "60": bis60,
                     "15": bis60, "3": bis60}
        plan = {"240": {"direction": "空头空", "strategy": "等待反弹后做2卖"}}
        with mock.patch.object(me, "realtimeLowerDiverge",
                               return_value=[{"res": "60", "segStart": 7200,
                                              "point": {"time": 9000, "price": 105.0}}]), \
                mock.patch.object(me, "counterMoveQualifies", return_value=True), \
                mock.patch.object(me, "mergedSegmentCount", return_value=99), \
                mock.patch.object(me, "strategyExtraOk", return_value=None), \
                mock.patch.object(me, "nearSr",
                                  return_value={"sr": {"price": 105.0}}):
            sigs = me.evaluateRealtimeEntries(
                periodBis, {}, {}, plan, [{"price": 105.0}],
                ["240", "60", "15", "3"], trend_res="D",
                trend_state={"dir": "short", "reason": "日线2卖", "res": "D"},
                fired=set())
        self.assertEqual([s["periodX"] for s in sigs], ["240"])
        self.assertEqual(sigs[0]["trendReason"], "日线2卖")


if __name__ == "__main__":
    unittest.main()
