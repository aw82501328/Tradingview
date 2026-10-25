# -*- coding: utf-8 -*-
"""买卖点 → 进场策略直映射单测（2026-10-16 取消交易计划文案中转）：
pointStrategyOf 点类型+strong 直映射（1类直接档统一、二买族强弱二分、3类/类3类4类分流）、
point_cls_of 2族强弱档派生、_side_anchor/side_candidates 最近笔端点双侧锚
（原 trading_plan.predictPlan.matchSide 平移）、
mark_entry.strategyExtraOk 2BuyBear/2SellBear 抬低/压低条件。
原 strategyOf 三档路由/classifySecond 四态/entryStrategyOf 文案映射随计划策略行删除（历史用例移除）。"""
import unittest

from . import mark_entry


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice}


def pt(type_, time, price, strong="__unset__"):
    p = {"type": type_, "time": time, "price": price}
    if strong != "__unset__":
        p["strong"] = strong
    return p


# ============================================================
# pointStrategyOf：点类型+strong → 进场键直映射
# ============================================================

class PointStrategyOfTests(unittest.TestCase):
    def test_first_class_direct_entry(self):
        # 1类直接档（用户确认 2026-10-16）：形成中与已确认统一走 1Buy/1Sell
        for type_, key, direction in (("1买", "1Buy", "long"), ("1卖", "1Sell", "short")):
            out = mark_entry.pointStrategyOf(pt(type_, 20, 95.0))
            self.assertEqual((out["key"], out["direction"]), (key, direction))
            self.assertIsNone(out["pointCls"])  # 非2族无强弱档

    def test_second_family_strong_weak_split(self):
        # 2买/类2买：strong=True → 2Buy；False/None（未定型弱先行）→ 2BuyBear
        for type_ in ("2买", "类2买"):
            out = mark_entry.pointStrategyOf(pt(type_, 20, 95.0, strong=True))
            self.assertEqual((out["key"], out["direction"]), ("2Buy", "long"))
            self.assertEqual(out["pointCls"], "强" + ("二买" if type_ == "2买" else "类2买"))
            for strong in (False, None):
                out = mark_entry.pointStrategyOf(pt(type_, 20, 95.0, strong=strong))
                self.assertEqual((out["key"], out["direction"]), ("2BuyBear", "long"))
                self.assertEqual(out["pointCls"], "弱" + ("二买" if type_ == "2买" else "类2买"))
        # 卖侧镜像
        for type_ in ("2卖", "类2卖"):
            out = mark_entry.pointStrategyOf(pt(type_, 20, 95.0, strong=True))
            self.assertEqual((out["key"], out["direction"]), ("2Sell", "short"))
            out = mark_entry.pointStrategyOf(pt(type_, 20, 95.0, strong=False))
            self.assertEqual((out["key"], out["direction"]), ("2SellBear", "short"))

    def test_third_family(self):
        # 3/4族拆强弱（2026-10-24）：强→3Buy/3Sell/Buy/Sell、弱→3BuyBear/3SellBear/
        # BuyBear/SellBear；pt() 默认不带 strong=按弱
        out = mark_entry.pointStrategyOf(pt("3买", 20, 95.0, strong=True))
        self.assertEqual((out["key"], out["direction"]), ("3Buy", "long"))
        out = mark_entry.pointStrategyOf(pt("3买", 20, 95.0))
        self.assertEqual((out["key"], out["direction"]), ("3BuyBear", "long"))
        out = mark_entry.pointStrategyOf(pt("3卖", 20, 95.0, strong=True))
        self.assertEqual((out["key"], out["direction"]), ("3Sell", "short"))
        out = mark_entry.pointStrategyOf(pt("3卖", 20, 95.0, strong=False))
        self.assertEqual((out["key"], out["direction"]), ("3SellBear", "short"))
        for type_ in ("类3买", "4买", "类4买"):
            out = mark_entry.pointStrategyOf(pt(type_, 20, 95.0, strong=True))
            self.assertEqual((out["key"], out["direction"]), ("Buy", "long"))
            out = mark_entry.pointStrategyOf(pt(type_, 20, 95.0, strong=False))
            self.assertEqual((out["key"], out["direction"]), ("BuyBear", "long"))
        for type_ in ("类3卖", "4卖", "类4卖"):
            out = mark_entry.pointStrategyOf(pt(type_, 20, 95.0, strong=True))
            self.assertEqual((out["key"], out["direction"]), ("Sell", "short"))
            out = mark_entry.pointStrategyOf(pt(type_, 20, 95.0, strong=False))
            self.assertEqual((out["key"], out["direction"]), ("SellBear", "short"))

    def test_unknown_type_returns_none(self):
        self.assertIsNone(mark_entry.pointStrategyOf(pt("观望", 20, 95.0)))
        self.assertIsNone(mark_entry.pointStrategyOf(pt("", 20, 95.0)))

    def test_point_cls_of_non_second_family(self):
        # 1类无 strong 不产生强弱档；3/4族带 strong 产生强弱档（2026-10-24 起）
        self.assertIsNone(mark_entry.point_cls_of(pt("1买", 20, 95.0)))
        self.assertIsNone(mark_entry.point_cls_of(pt("3买", 20, 95.0)))  # 无 strong 键
        self.assertEqual(mark_entry.point_cls_of(pt("3买", 20, 95.0, strong=True)), "强三买")
        # 有 strong 字段的 2族才派生
        self.assertEqual(mark_entry.point_cls_of(pt("2卖", 20, 95.0, strong=True)), "强二卖")
        self.assertEqual(mark_entry.point_cls_of(pt("类2卖", 20, 95.0, strong=None)), "弱类2卖")


# ============================================================
# 双侧锚：_side_anchor / side_candidates（原 predictPlan.matchSide 平移）
# ============================================================

class SideAnchorTests(unittest.TestCase):
    BAR = 180  # 3 分钟

    def test_match_at_last_bi_endpoint(self):
        bis = [bi("up", 0, 60, 100, 110), bi("down", 60, 120, 110, 95)]
        points = {"buy": [pt("1买", 120, 95.0)], "sell": []}
        p, anchor_bi = mark_entry._side_anchor(bis, points, mark_entry.BUY_FAMILY, self.BAR, 0.05)
        self.assertIsNotNone(p)
        self.assertEqual(p["type"], "1买")
        self.assertEqual(anchor_bi["endTime"], 120)

    def test_family_filter_and_backward_scan(self):
        # 末笔端点上只有卖点；买族向前扫描到更早端点上的买点
        bis = [bi("up", 0, 60, 100, 110), bi("down", 60, 120, 110, 95), bi("up", 120, 180, 95, 115)]
        points = {"buy": [pt("2买", 120, 95.0, strong=True)],
                  "sell": [pt("1卖", 180, 115.0)]}
        p, _ = mark_entry._side_anchor(bis, points, mark_entry.BUY_FAMILY, self.BAR, 0.05)
        self.assertEqual(p["type"], "2买")
        p, _ = mark_entry._side_anchor(bis, points, mark_entry.SELL_FAMILY, self.BAR, 0.05)
        self.assertEqual(p["type"], "1卖")

    def test_tolerance_time_and_price(self):
        bis = [bi("down", 0, 120, 110, 95)]
        # 时间差 1 bar 内、价格差 max(ATR×0.2, 0.05) 内可匹配
        points = {"buy": [pt("1买", 120 + self.BAR - 1, 95.04)], "sell": []}
        p, _ = mark_entry._side_anchor(bis, points, mark_entry.BUY_FAMILY, self.BAR, 0.05)
        self.assertIsNotNone(p)
        # 价格差超容差 → 不匹配
        points = {"buy": [pt("1买", 120, 95.06)], "sell": []}
        p, _ = mark_entry._side_anchor(bis, points, mark_entry.BUY_FAMILY, self.BAR, 0.05)
        self.assertIsNone(p)

    def test_side_candidates_both_sides(self):
        bis = [bi("up", 0, 60, 100, 110), bi("down", 60, 120, 110, 95)]
        points = {"buy": [pt("1买", 120, 95.0)], "sell": [pt("1卖", 60, 110.0)]}
        cands = mark_entry.side_candidates(bis, points, 0, self.BAR)
        self.assertEqual({c["type"] for c in cands}, {"1买", "1卖"})
        # 双侧都无锚 → 空列表（no_point）
        self.assertEqual(mark_entry.side_candidates(bis, {"buy": [], "sell": []}, 0, self.BAR), [])


class StrategyExtraOkTests(unittest.TestCase):
    """2BuyBear/2SellBear 专属条件（2026-10-09）：抬低/压低——
    末下跌（上涨）笔终点不破（不过）前一根同向笔终点；其余键沿用既有口径。"""

    def test_2buy_bear_requires_higher_low(self):
        # 抬低：末 down 笔终点 96 > 前 down 笔终点 92 → 通过；破前低 98 < 92？构造两例
        higher = [bi("down", 0, 10, 120, 92), bi("up", 10, 20, 92, 110),
                  bi("down", 20, 30, 110, 96)]
        self.assertIsNone(mark_entry.strategyExtraOk("2BuyBear", higher, [], [], 3600))
        broke = [bi("down", 0, 10, 120, 92), bi("up", 10, 20, 92, 110),
                 bi("down", 20, 30, 110, 90)]
        self.assertEqual(mark_entry.strategyExtraOk("2BuyBear", broke, [], [], 3600),
                         "回调破前低，非抬低2买")

    def test_2sell_bear_requires_lower_high(self):
        # 压低：末 up 笔终点 108 < 前 up 笔终点 115 → 通过；过前高 118 → 拒
        lower = [bi("up", 0, 10, 92, 115), bi("down", 10, 20, 115, 100),
                 bi("up", 20, 30, 100, 108)]
        self.assertIsNone(mark_entry.strategyExtraOk("2SellBear", lower, [], [], 3600))
        broke = [bi("up", 0, 10, 92, 115), bi("down", 10, 20, 115, 100),
                 bi("up", 20, 30, 100, 118)]
        self.assertEqual(mark_entry.strategyExtraOk("2SellBear", broke, [], [], 3600),
                         "反弹过前高，非压低2卖")


if __name__ == "__main__":
    unittest.main()
