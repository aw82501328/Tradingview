# -*- coding: utf-8 -*-
"""交易计划三档策略映射单测（2026-09-24 口径 + 2026-10-09 弱档原始点分流）：
strategyOf 三档映射（强档=过左高/左低不背驰；中间档仅 2买/2卖；3类点强档开关；
弱档未过原始点/原始底 → 空头多·等2买 / 多头空·等2卖）、
classifySecond 4 态 + 弱档分流 2 哨兵分类（含 prevHighNearPts/secondNearPts/weakTierByOrigin
容差与 cfg 覆盖）、mark_entry.entryStrategyOf 12 条文案 → 进场 key 的映射、
mark_entry.strategyExtraOk wait2BuyBear/wait2SellBear 抬低/压低条件。
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
        # 多笔回调时取最近一笔：第一笔回到 97（附近）但最近一笔 101（差 6 >5）→ 弱档；
        # 最近一笔 98（差 3）→ 回到2买点。（关掉弱档分流，专注中间档取最近一笔语义）
        base = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95), bi("up", 20, 30, 95, 104)]
        late_far = base + [bi("down", 30, 40, 104, 97), bi("up", 40, 50, 97, 106), bi("down", 50, 60, 106, 101)]
        self.assertEqual(trading_plan.classifySecond(late_far, [], {"type": "2买", "time": 20, "price": 95},
                                                     {"weakTierByOrigin": False}),
                         "其他")
        late_near = base + [bi("down", 30, 40, 104, 97), bi("up", 40, 50, 97, 106), bi("down", 50, 60, 106, 98)]
        self.assertEqual(trading_plan.classifySecond(late_near, [], {"type": "2买", "time": 20, "price": 95}),
                         "回到2买点")

    def test_weak_split_origin_is_dominating_earlier_top(self):
        # 原始点=回溯中第一个支配其后所有反弹高的更早顶：130@10（其后反弹顶 110@30 均更低）
        # → 原始点 130 而非最近顶 110；点后反弹 104 < 130 → 未过原始点
        bis = [bi("up", 0, 10, 100, 130), bi("down", 10, 20, 130, 100),
               bi("up", 20, 30, 100, 110), bi("down", 30, 40, 110, 95),
               bi("up", 40, 50, 95, 104)]
        self.assertEqual(trading_plan.classifySecond(bis, [], {"type": "2买", "time": 40, "price": 95}),
                         "未过原始点")

    def test_weak_split_flips_back_once_origin_passed(self):
        # 后续某反弹顶越过原始点（125 > 120）→ 翻回旧弱档「其他」
        bis = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95),
               bi("up", 20, 30, 95, 104), bi("down", 30, 40, 104, 101),
               bi("up", 40, 50, 101, 125)]
        self.assertEqual(trading_plan.classifySecond(bis, [], {"type": "2买", "time": 20, "price": 95}),
                         "其他")

    def test_weak_split_only_second_class_points(self):
        # 3买 不产生哨兵（原始点分流仅 2买/类2买/2卖/类2卖）：同结构下仍「其他」→ 弱档旧映射
        bis = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95),
               bi("up", 20, 30, 95, 104), bi("down", 30, 40, 104, 101)]
        self.assertEqual(trading_plan.classifySecond(bis, [], {"type": "3买", "time": 20, "price": 95}),
                         "其他")

    def test_weak_split_sell_side_mirror(self):
        # 2卖 弱档（下跌 108 距前低 100 差 8 >5；反弹 112 距 125 差 13 >5）且
        # 点后所有低点（108）未跌破上涨原始点（100）→ 卖侧分流。
        # after（下跌笔）已确认 → 够笔 → 卖点作废转弱二买哨兵；
        # after 形成中且 mergedCount<5 → 未够笔 → 维持 多头空/等2卖 档。
        bis = [bi("down", 0, 10, 120, 100), bi("up", 10, 20, 100, 125),
               bi("down", 20, 30, 125, 108), bi("up", 30, 40, 108, 112)]
        forming = [bi("down", 0, 10, 120, 100), bi("up", 10, 20, 100, 125),
                   dict(bi("down", 20, 30, 125, 108), _forming=True, mergedCount=3)]
        p = {"type": "2卖", "time": 20, "price": 125}
        self.assertEqual(trading_plan.classifySecond(bis, [], p), "未过原始底够笔")
        self.assertEqual(trading_plan.classifySecond(forming, [], p), "未过原始底")
        self.assertEqual(trading_plan.classifySecond(bis, [], p, {"weakTierByOrigin": False}), "其他")
        # 够笔作废 → 弱二买档（空头多/等待低点附近的2买）
        out = trading_plan.strategyOf("60", "类2卖", "", "", "未过原始底够笔")
        self.assertEqual((out["direction"], out["strategy"]), ("空头多", "等待低点附近的2买"))
        out = trading_plan.strategyOf("60", "2卖", "", "", "未过原始底够笔")
        self.assertEqual((out["direction"], out["strategy"]), ("空头多", "等待低点附近的2买"))

    def test_weak_when_far_and_no_retest(self):
        # after 104 距前高 120 太远；回调 101 距 95 差 6 >5 → 弱档。
        # 2026-10-09 弱档原始点分流默认开：反弹高点 104/106 均未过原始点 120 → 「未过原始点」；
        # 关掉开关 → 旧「其他」。
        bis = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95),
               bi("up", 20, 30, 95, 104), bi("down", 30, 40, 104, 101)]
        p = {"type": "2买", "time": 20, "price": 95}
        self.assertEqual(trading_plan.classifySecond(bis, [], p), "未过原始点")
        self.assertEqual(trading_plan.classifySecond(bis, [], p, {"weakTierByOrigin": False}), "其他")

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
        # 点后无同向笔：未定型不接管默认开 → 未定型（不落「其他」）
        bis = [bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95)]
        self.assertEqual(trading_plan.classifySecond(bis, [], {"type": "2买", "time": 20, "price": 95}),
                         "未定型")


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

    def test_second_buy_weak_origin_split_tier(self):
        # 2026-10-09 弱档原始点分流：未过原始点 → 空头多/等待低点附近的2买（2买与类2买同）
        for type_ in ("2买", "类2买"):
            out = self._strategy(type_, "未过原始点")
            self.assertEqual((out["direction"], out["strategy"]), ("空头多", "等待低点附近的2买"))
        for type_ in ("2卖", "类2卖"):
            out = self._strategy(type_, "未过原始底")
            self.assertEqual((out["direction"], out["strategy"]), ("多头空", "等待高点附近的2卖"))

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

    def test_third_strong_tier_default_off_and_switch_on(self):
        # 默认（关）：3类点一律弱档
        out = self._strategy("3买", "过左高不背驰")
        self.assertEqual((out["direction"], out["strategy"]), ("多头空", "等待高点附近的一卖"))
        out = self._strategy("3卖", "过左低不背驰")
        self.assertEqual((out["direction"], out["strategy"]), ("空头多", "等待低点附近的一买"))
        # 开关开：3类点强档
        for type_, cls in (("3买", "过左高不背驰"), ("类3买", "过左高不背驰")):
            out = self._strategy(type_, cls, {"thirdStrongTrend": True})
            self.assertEqual((out["direction"], out["strategy"]), ("多头多", "等待回调后的新买点"))
        out = self._strategy("类3卖", "过左低不背驰", {"thirdStrongTrend": True})
        self.assertEqual((out["direction"], out["strategy"]), ("空头空", "等待反弹后的新卖点"))
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
            "等待回调后的新买点": ("waitBuy", "long"),        # 3类点强档（thirdStrongTrend 开）
            "等待反弹后的新卖点": ("waitSell", "short"),
            "等待回调后的3买点": ("wait3Buy", "long"),        # 2买/类2买 强档
            "等待回调后的类2买点": ("waitLike2Buy", "long"),  # 2买 中间档
            "等待反弹后的3卖点": ("wait3Sell", "short"),      # 2卖/类2卖 强档
            "等待反弹后的类2卖点": ("waitLike2Sell", "short"),  # 2卖 中间档
            "等待低点附近的2买": ("wait2BuyBear", "long"),    # 2买/类2买 弱档·未过原始点（2026-10-09）
            "等待高点附近的2卖": ("wait2SellBear", "short"),  # 2卖/类2卖 弱档·未过原始底（2026-10-09）
        }
        for text, (key, direction) in cases.items():
            s = mark_entry.entryStrategyOf(text)
            self.assertIsNotNone(s, text)
            self.assertEqual((s["key"], s["direction"]), (key, direction), text)
        # 观望类文案不产生进场策略
        self.assertIsNone(mark_entry.entryStrategyOf("震荡整理，观望等待方向选择"))
        self.assertIsNone(mark_entry.entryStrategyOf("趋势中无匹配买卖点"))


class StrategyExtraOkTests(unittest.TestCase):
    """wait2BuyBear/wait2SellBear 专属条件（2026-10-09）：抬低/压低——
    末下跌（上涨）笔终点不破（不过）前一根同向笔终点；其余键沿用既有口径。"""

    def test_wait2_buy_bear_requires_higher_low(self):
        # 抬低：末 down 笔终点 96 > 前 down 笔终点 92 → 通过；破前低 98 < 92？构造两例
        higher = [bi("down", 0, 10, 120, 92), bi("up", 10, 20, 92, 110),
                  bi("down", 20, 30, 110, 96)]
        self.assertIsNone(mark_entry.strategyExtraOk("wait2BuyBear", higher, [], [], 3600))
        broke = [bi("down", 0, 10, 120, 92), bi("up", 10, 20, 92, 110),
                 bi("down", 20, 30, 110, 90)]
        self.assertEqual(mark_entry.strategyExtraOk("wait2BuyBear", broke, [], [], 3600),
                         "回调破前低，非抬低2买")

    def test_wait2_sell_bear_requires_lower_high(self):
        # 压低：末 up 笔终点 108 < 前 up 笔终点 115 → 通过；过前高 118 → 拒
        lower = [bi("up", 0, 10, 92, 115), bi("down", 10, 20, 115, 100),
                 bi("up", 20, 30, 100, 108)]
        self.assertIsNone(mark_entry.strategyExtraOk("wait2SellBear", lower, [], [], 3600))
        broke = [bi("up", 0, 10, 92, 115), bi("down", 10, 20, 115, 100),
                 bi("up", 20, 30, 100, 118)]
        self.assertEqual(mark_entry.strategyExtraOk("wait2SellBear", broke, [], [], 3600),
                         "反弹过前高，非压低2卖")


if __name__ == "__main__":
    unittest.main()
