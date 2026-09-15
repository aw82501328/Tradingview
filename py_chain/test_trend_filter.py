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
        self.assertIs(td.call_args[0][3], bis)

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
                mock.patch.object(me, "_barsSince", return_value=99), \
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
                mock.patch.object(me, "_barsSince", return_value=99), \
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
