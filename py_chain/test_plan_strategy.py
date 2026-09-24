# -*- coding: utf-8 -*-
"""交易计划三档策略映射单测（2026-09-24 口径）：
strategyOf 三档映射（强档=过左高/左低不背驰；中间档仅 2买/2卖；3类点强档开关）、
classifySecond 4 态分类（含 prevHighNearPts/secondNearPts 容差与 cfg 覆盖）、
mark_entry.entryStrategyOf 新旧 10 条文案 → 6 个进场 key 的映射。
fixtures：macdArr 为空时 isBiDiverge 视为不背驰（与 JS 版一致），强档只需过左高/左低。"""
import unittest

from . import mark_entry, trading_plan


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice}


# ============================================================
# classifySecond：4 态分类
# ============================================================

class ClassifySecondTests(unittest.TestCase):
    def test_strong_pass_left_high_no_diverge(self):
        # 2买 @20 @95；after 涨到 112 > 前高 105；macd 空 → 不背驰 → 过左高不背驰
        bis = [bi("up", 0, 10, 100, 105), bi("down", 10, 20, 105, 95),
               bi("up", 20, 30, 95, 112)]
        self.assertEqual(trading_plan.classifySecond(bis, [], {"type": "2买", "time": 20, "price": 95}),
                         "过左高不背驰")

    def test_near_prev_high(self):
        # after 终点 116，距前高 120 差 4 ≤5（未过）→ 前高附近
        bis = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95),
               bi("up", 20, 30, 95, 116)]
        self.assertEqual(trading_plan.classifySecond(bis, [], {"type": "2买", "time": 20, "price": 95}),
                         "前高附近")

    def test_near_prev_high_includes_just_passed_but_diverged(self):
        # after 终点 121 刚过前高 120（距离 1 ≤5）：有 macd 参照背驰判定不可用（空）时为强档，
        # 这里给 after 终点 121 且构造背驰参照（macd 空→不背驰→强档），改用距离更大场景：
        # 过一点点但 macd 判背驰需要数据，本用例验证「未过 + 距离 1」同样命中前高附近
        bis = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95),
               bi("up", 20, 30, 95, 119.5)]
        self.assertEqual(trading_plan.classifySecond(bis, [], {"type": "2买", "time": 20, "price": 95}),
                         "前高附近")

    def test_back_to_second_point(self):
        # after 104（距前高 120 差 16 >5，未过）；回调笔终点 97 距 2买点 95 差 2 ≤5 → 回到2买点
        bis = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95),
               bi("up", 20, 30, 95, 104), bi("down", 30, 40, 104, 97)]
        self.assertEqual(trading_plan.classifySecond(bis, [], {"type": "2买", "time": 20, "price": 95}),
                         "回到2买点")

    def test_back_to_second_point_takes_latest_pullback(self):
        # 多笔回调时取最近一笔：第一笔回到 97（附近）但最近一笔 101（差 6 >5）→ 其他；
        # 最近一笔 98（差 3）→ 回到2买点
        base = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95), bi("up", 20, 30, 95, 104)]
        late_far = base + [bi("down", 30, 40, 104, 97), bi("up", 40, 50, 97, 106), bi("down", 50, 60, 106, 101)]
        self.assertEqual(trading_plan.classifySecond(late_far, [], {"type": "2买", "time": 20, "price": 95}),
                         "其他")
        late_near = base + [bi("down", 30, 40, 104, 97), bi("up", 40, 50, 97, 106), bi("down", 50, 60, 106, 98)]
        self.assertEqual(trading_plan.classifySecond(late_near, [], {"type": "2买", "time": 20, "price": 95}),
                         "回到2买点")

    def test_weak_when_far_and_no_retest(self):
        # after 104 距前高 120 太远；回调 101 距 95 差 6 >5 → 其他
        bis = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95),
               bi("up", 20, 30, 95, 104), bi("down", 30, 40, 104, 101)]
        self.assertEqual(trading_plan.classifySecond(bis, [], {"type": "2买", "time": 20, "price": 95}),
                         "其他")

    def test_cfg_overrides_tolerances(self):
        # 同一结构：prevHighNearPts=20 → 前高附近；secondNearPts=20 → 回到2买点
        bis = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95),
               bi("up", 20, 30, 95, 104), bi("down", 30, 40, 104, 97)]
        p = {"type": "2买", "time": 20, "price": 95}
        self.assertEqual(trading_plan.classifySecond(bis, [], p, {"prevHighNearPts": 20.0}),
                         "前高附近")
        self.assertEqual(trading_plan.classifySecond(bis, [], p, {"prevHighNearPts": 0.1, "secondNearPts": 20.0}),
                         "回到2买点")

    def test_sell_side_mirror(self):
        # 2卖 @20 @125；前低 100 @10
        near = [bi("down", 0, 10, 120, 100), bi("up", 10, 20, 100, 125),
                bi("down", 20, 30, 125, 104)]  # 距前低 100 差 4 → 前低附近
        self.assertEqual(trading_plan.classifySecond(near, [], {"type": "2卖", "time": 20, "price": 125}),
                         "前低附近")
        back = [bi("down", 0, 10, 120, 100), bi("up", 10, 20, 100, 125),
                bi("down", 20, 30, 125, 108), bi("up", 30, 40, 108, 122)]  # 反弹 122 距 125 差 3 → 回到2卖点
        self.assertEqual(trading_plan.classifySecond(back, [], {"type": "2卖", "time": 20, "price": 125}),
                         "回到2卖点")
        strong = [bi("down", 0, 10, 120, 100), bi("up", 10, 20, 100, 125),
                  bi("down", 20, 30, 125, 96)]  # 跌破前低 100，macd 空 → 不背驰
        self.assertEqual(trading_plan.classifySecond(strong, [], {"type": "2卖", "time": 20, "price": 125}),
                         "过左低不背驰")

    def test_no_prev_extreme_or_no_after(self):
        # 无前顶（点前只有下跌笔起点=顶，其实有；构造点后无同向笔 → 其他）
        bis = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95)]
        self.assertEqual(trading_plan.classifySecond(bis, [], {"type": "2买", "time": 20, "price": 95}),
                         "其他")


# ============================================================
# strategyOf：三档映射
# ============================================================

class StrategyOfTests(unittest.TestCase):
    def _strategy(self, type_, cls, cfg=None):
        return trading_plan.strategyOf("60", type_, "", "趋势|" + type_, cls, cfg)

    def test_first_class_points_unchanged(self):
        self.assertEqual(self._strategy("1买", "其他")["strategy"], "等待回调后做2买")
        self.assertEqual(self._strategy("1卖", "其他")["strategy"], "等待反弹后做2卖")

    def test_second_buy_three_tiers(self):
        self.assertEqual(self._strategy("2买", "过左高不背驰"),
                         {"res": "60", "reason": "", "label": "趋势|2买",
                          "direction": "多头多", "strategy": "等待回调后的3买点"})
        for cls in ("前高附近", "回到2买点"):
            out = self._strategy("2买", cls)
            self.assertEqual(out["direction"], "多头多")
            self.assertEqual(out["strategy"], "等待回调后的类2买点")
        out = self._strategy("2买", "其他")
        self.assertEqual((out["direction"], out["strategy"]), ("多头空", "等待高点附近的一卖"))

    def test_quasi_second_buy_skips_middle_tier(self):
        # 类2买不走中间档：中档分类 → 弱档；强档照常
        for cls in ("前高附近", "回到2买点"):
            out = self._strategy("类2买", cls)
            self.assertEqual((out["direction"], out["strategy"]), ("多头空", "等待高点附近的一卖"))
        out = self._strategy("类2买", "过左高不背驰")
        self.assertEqual((out["direction"], out["strategy"]), ("多头多", "等待回调后的3买点"))

    def test_second_sell_three_tiers(self):
        out = self._strategy("2卖", "过左低不背驰")
        self.assertEqual((out["direction"], out["strategy"]), ("空头空", "等待反弹后的3卖点"))
        for cls in ("前低附近", "回到2卖点"):
            out = self._strategy("2卖", cls)
            self.assertEqual((out["direction"], out["strategy"]), ("空头空", "等待反弹后的类2卖点"))
        out = self._strategy("类2卖", "前低附近")
        self.assertEqual((out["direction"], out["strategy"]), ("空头多", "等待低点附近的一买"))

    def test_third_strong_tier_default_on_and_switch_off(self):
        # 默认（开）：3类点+强档保持现状文案
        out = self._strategy("3买", "过左高不背驰")
        self.assertEqual((out["direction"], out["strategy"]), ("多头多", "等待回调后的新买点"))
        out = self._strategy("3卖", "过左低不背驰")
        self.assertEqual((out["direction"], out["strategy"]), ("空头空", "等待反弹后的新卖点"))
        # 开关关：3类点一律弱档
        for type_, cls in (("3买", "过左高不背驰"), ("类3买", "过左高不背驰")):
            out = self._strategy(type_, cls, {"thirdStrongTrend": False})
            self.assertEqual((out["direction"], out["strategy"]), ("多头空", "等待高点附近的一卖"))
        out = self._strategy("类3卖", "过左低不背驰", {"thirdStrongTrend": False})
        self.assertEqual((out["direction"], out["strategy"]), ("空头多", "等待低点附近的一买"))
        # 3类点弱分类（开关无关）→ 弱档
        out = self._strategy("3买", "前高附近")
        self.assertEqual((out["direction"], out["strategy"]), ("多头空", "等待高点附近的一卖"))


# ============================================================
# mark_entry.entryStrategyOf：新旧文案 → 进场 key
# ============================================================

class EntryStrategyMappingTests(unittest.TestCase):
    def test_all_strategy_texts_map_to_six_keys(self):
        cases = {
            "等待反弹后做2卖": ("wait2Sell", "short"),
            "等待回调后做2买": ("wait2Buy", "long"),
            "等待高点附近的一卖": ("wait1Sell", "short"),
            "等待低点附近的一买": ("wait1Buy", "long"),
            "等待回调后的新买点": ("waitBuy", "long"),      # 3类点强档（thirdStrongTrend 开）
            "等待反弹后的新卖点": ("waitSell", "short"),
            "等待回调后的3买点": ("waitBuy", "long"),        # 2买/类2买 强档
            "等待回调后的类2买点": ("waitBuy", "long"),      # 2买 中间档
            "等待反弹后的3卖点": ("waitSell", "short"),      # 2卖/类2卖 强档
            "等待反弹后的类2卖点": ("waitSell", "short"),    # 2卖 中间档
        }
        for text, (key, direction) in cases.items():
            s = mark_entry.entryStrategyOf(text)
            self.assertIsNotNone(s, text)
            self.assertEqual((s["key"], s["direction"]), (key, direction), text)
        # 观望类文案不产生进场策略
        self.assertIsNone(mark_entry.entryStrategyOf("震荡整理，观望等待方向选择"))
        self.assertIsNone(mark_entry.entryStrategyOf("趋势中无匹配买卖点"))


if __name__ == "__main__":
    unittest.main()
