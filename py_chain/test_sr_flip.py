# -*- coding: utf-8 -*-
"""
sr_flip 支阻位单元测试（与 .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.test.js 对齐）

覆盖三类来源（密集区 cluster / 黄金分割 fib / BOLL boll）的纯函数逻辑与
compute_srflip 集成行为：
  - fib：pickLatestFibPoint / referBiOfPoint / fibLevelsOf / buildFibCandidates
    （逻辑未动，纯函数用例保持原样作回归证明）
  - boll：calcBOLL / buildBollCandidates
  - 全量候选池：flatten_candidates 展平（不合并，同价位各自独立、保留标记）
  - 按显示周期选取：pickNearestForDisplay（各周期独立）与来源标注 sourceLabelOf/labelOf
  - compute_srflip 集成：不合并候选池、drawnByPeriod 取代 drawn/drawnFib

运行：python -m unittest py_chain.test_sr_flip -v
"""
import unittest

from py_chain.sr_flip import (
    FIB_BUY_TYPES,
    FIB_SELL_TYPES,
    buildBollCandidates,
    buildFibCandidates,
    buildManualCandidates,
    calcBOLL,
    compute_srflip,
    fibLevelsOf,
    flatten_candidates,
    flipScore,
    labelOf,
    pendingReferOf,
    periodNameOf,
    pickLatestFibPoint,
    pickNearestForDisplay,
    referBiOfPoint,
    sourceLabelOf,
)


def bar(t, h, l, c):
    return {"time": t, "open": c, "high": h, "low": l, "close": c, "volume": 100}


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice,
            "span": abs(endPrice - startPrice)}


class TestPickLatestFibPoint(unittest.TestCase):
    def test_latest_within_whitelist_and_first_kind_excluded(self):
        points = [
            {"type": "2买", "time": 100, "price": 90},
            {"type": "1买", "time": 500, "price": 80},   # 一类，排除
            {"type": "类2买", "time": 300, "price": 95},
            {"type": "3买", "time": 200, "price": 98},
        ]
        p = pickLatestFibPoint(points, FIB_BUY_TYPES)
        self.assertEqual(p["type"], "类2买")
        self.assertEqual(p["time"], 300)

    def test_same_time_takes_later_in_array(self):
        points = [
            {"type": "2买", "time": 300, "price": 90},
            {"type": "3买", "time": 300, "price": 95},
        ]
        self.assertEqual(pickLatestFibPoint(points, FIB_BUY_TYPES)["type"], "3买")

    def test_empty_or_all_first_kind_returns_none(self):
        self.assertIsNone(pickLatestFibPoint([], FIB_BUY_TYPES))
        self.assertIsNone(
            pickLatestFibPoint([{"type": "1买", "time": 100, "price": 90}], FIB_BUY_TYPES))


class TestReferBiOfPoint(unittest.TestCase):
    def test_buy_point_hits_down_bi_and_refer_is_previous_up(self):
        bis = [
            bi("up", 100, 200, 90, 120),    # 参照笔
            bi("down", 200, 300, 120, 100),  # 回调笔，终点即 2买
        ]
        found = referBiOfPoint(bis, 300, "down")
        self.assertEqual(found["pullback"]["type"], "down")
        self.assertEqual(found["pullback"]["endTime"], 300)
        self.assertEqual(found["refer"]["type"], "up")
        self.assertEqual(found["refer"]["startPrice"], 90)
        self.assertEqual(found["refer"]["endPrice"], 120)

    def test_no_match_returns_none(self):
        bis = [bi("up", 100, 200, 90, 120), bi("down", 200, 300, 120, 100)]
        self.assertIsNone(referBiOfPoint(bis, 999, "down"))
        self.assertIsNone(referBiOfPoint(bis, 200, "down"))  # time 命中的是 up 笔

    def test_pullback_is_first_bi_returns_none(self):
        bis = [bi("down", 100, 200, 120, 100)]
        self.assertIsNone(referBiOfPoint(bis, 200, "down"))

    def test_previous_bi_same_direction_dirty_data_returns_none(self):
        bis = [
            bi("down", 100, 200, 120, 110),
            bi("down", 200, 300, 110, 100),  # 脏数据：连续同向
        ]
        self.assertIsNone(referBiOfPoint(bis, 300, "down"))


class TestFibLevelsOf(unittest.TestCase):
    def test_buy_up_bi_100_to_200(self):
        refer = bi("up", 100, 200, 100, 200)
        levels = fibLevelsOf(refer, "buy", [0.382, 0.5, 0.618])
        self.assertEqual([l["ratio"] for l in levels], [0.382, 0.5, 0.618])
        self.assertAlmostEqual(levels[0]["price"], 161.8, places=9)
        self.assertAlmostEqual(levels[1]["price"], 150.0, places=9)
        self.assertAlmostEqual(levels[2]["price"], 138.2, places=9)

    def test_sell_down_bi_200_to_100(self):
        refer = bi("down", 100, 200, 200, 100)
        levels = fibLevelsOf(refer, "sell", [0.382, 0.5, 0.618])
        self.assertAlmostEqual(levels[0]["price"], 138.2, places=9)
        self.assertAlmostEqual(levels[1]["price"], 150.0, places=9)
        self.assertAlmostEqual(levels[2]["price"], 161.8, places=9)

    def test_zero_span_returns_empty(self):
        self.assertEqual(fibLevelsOf(bi("up", 100, 200, 100, 100), "buy", [0.5]), [])


class TestBuildFibCandidates(unittest.TestCase):
    def setUp(self):
        # 结构底（无上级笔）夹具：最低 down 笔后抬高低点 → 2买@1300 / 类2买@1500；
        # 最高 up 笔后次高点 → 2卖@1800 / 类2卖@2000
        self.bis = [
            bi("down", 1000, 1100, 100, 80),   # 结构底 80
            bi("up", 1100, 1200, 80, 120),
            bi("down", 1200, 1300, 120, 90),   # 2买@1300（90 > 80）
            bi("up", 1300, 1400, 90, 130),
            bi("down", 1400, 1500, 130, 100),  # 类2买@1500（100 > 90）
            bi("up", 1500, 1600, 100, 140),    # 结构顶 140
            bi("down", 1600, 1700, 140, 110),
            bi("up", 1700, 1800, 110, 135),    # 2卖@1800（135 < 140）
            bi("down", 1800, 1900, 135, 105),
            bi("up", 1900, 2000, 105, 128),    # 类2卖@2000（128 < 135）
        ]
        self.bars = [bar(t, 115, 105, 110) for t in range(1000, 2050, 50)]

    def test_latest_point_per_side_times_all_ratios(self):
        buyPts = [
            {"type": "2买", "time": 100, "price": 105},   # 更早的 2买，不产生线
            {"type": "2买", "time": 1300, "price": 90},
            {"type": "类2买", "time": 1500, "price": 100},  # 最新买点
        ]
        sellPts = [{"type": "类2卖", "time": 2000, "price": 128}]
        cands = buildFibCandidates(self.bis, buyPts, sellPts, [0.382, 0.5, 0.618], self.bars, 5)
        self.assertEqual(len(cands), 6)
        buys = [c for c in cands if c["type"] == "SUP"]
        sells = [c for c in cands if c["type"] == "RES"]
        self.assertEqual(len(buys), 3)
        self.assertEqual(len(sells), 3)
        # 买点参照笔 up 90→130：0.382 位 = 130-0.382×40 = 114.72
        self.assertAlmostEqual(buys[0]["price"], 114.72, places=9)
        # 卖点参照笔 down 135→105：0.382 位 = 105+0.382×30 = 116.46
        self.assertAlmostEqual(sells[0]["price"], 116.46, places=9)

    def test_candidate_fields(self):
        # 买向已形成类2买@1500（1 条）+ 卖向无已形成点走预期回退（1 条）= 2
        cands = buildFibCandidates(
            self.bis, [{"type": "类2买", "time": 1500, "price": 100}], [], [0.5], self.bars, 5)
        self.assertEqual(len(cands), 2)
        f = next(c for c in cands if "pending" not in c)
        self.assertTrue(f["fib"])
        self.assertEqual(f["ratio"], 0.5)
        self.assertEqual(f["type"], "SUP")
        self.assertEqual(f["fromPoint"], {"type": "类2买", "time": 1500, "price": 100})
        self.assertEqual(f["referBi"]["startTime"], 1300)
        self.assertEqual(f["referBi"]["endTime"], 1400)
        self.assertEqual(f["referBi"]["startPrice"], 90)
        self.assertEqual(f["referBi"]["endPrice"], 130)
        self.assertEqual(f["touchCount"], 1)
        self.assertEqual(f["firstTouch"], 1300)   # 参照笔起点
        self.assertEqual(f["lastTouch"], 1500)    # 信号点时间
        self.assertEqual(f["breakTime"], 1500)    # 绘线锚点 = 信号点时间
        self.assertTrue(isinstance(f["barsPassed"], int))
        # 卖向预期位：形成笔 up@2000 高点 128 < 前方 down 笔起点 135 → 回退触发
        pend = next(c for c in cands if c.get("pending"))
        self.assertEqual(pend["fromPoint"]["type"], "预期2卖")

    def test_first_kind_only_no_formed_fib_but_sell_pending(self):
        # 一类点不算已形成非一类点 → 买向无（末笔 up，买向回退需形成中 down）；
        # 卖向走预期回退：末笔 up 高点 128 < 前方 down 笔起点 135 → 1 条 pending 阻力
        cands = buildFibCandidates(
            self.bis,
            [{"type": "1买", "time": 1500, "price": 100}],
            [{"type": "1卖", "time": 2000, "price": 128}],
            [0.5], self.bars, 5)
        self.assertEqual(len(cands), 1)
        self.assertIs(cands[0]["pending"], True)
        self.assertEqual(cands[0]["type"], "RES")
        # 参照 down 135→105：0.5 → 105+0.5×30 = 120
        self.assertAlmostEqual(cands[0]["price"], 120, places=9)

    def test_latest_point_without_refer_skips_side_no_fallback(self):
        # 最新 2买 在 time=2500（晚于所有笔，匹配不到回调笔）→ 买方向整体跳过
        # （有已形成点不走预期回退），不回退用 time=1300 的更早 2买 → 无任何 SUP 候选；
        # 卖向无已形成点 → 预期回退触发（末笔 up 128 < 前方 down 笔起点 135）→ 1 条 pending RES
        cands = buildFibCandidates(
            self.bis,
            [{"type": "2买", "time": 1300, "price": 90}, {"type": "2买", "time": 2500, "price": 110}],
            [], [0.5], self.bars, 5)
        self.assertEqual([c["type"] for c in cands], ["RES"])   # 无买向候选
        self.assertIs(cands[0]["pending"], True)


class TestPendingReferOf(unittest.TestCase):
    """预期回退参照笔（SPEC 2.4 预期回退）。"""

    def setUp(self):
        self.bis = [
            bi("up", 100, 200, 100, 150),
            bi("down", 200, 300, 150, 110),   # 卖向参照笔：H=150 L=110
            bi("up", 300, 400, 110, 140),     # 形成中上涨笔（140 < 150 次高点成立）
        ]

    def test_sell_side_returns_previous_down_bi(self):
        p = pendingReferOf(self.bis, "sell")
        self.assertEqual(p["refer"]["type"], "down")
        self.assertEqual(p["refer"]["startPrice"], 150)
        self.assertEqual(p["forming"]["endPrice"], 140)

    def test_buy_side_symmetric(self):
        bis = [
            bi("down", 100, 200, 150, 110),
            bi("up", 200, 300, 110, 150),
            bi("down", 300, 400, 150, 120),   # 形成中（120 > 110 次低点成立）
        ]
        p = pendingReferOf(bis, "buy")
        self.assertEqual(p["refer"]["type"], "up")
        self.assertEqual(p["refer"]["startPrice"], 110)

    def test_structure_broken_returns_none(self):
        broken = [
            bi("up", 100, 200, 100, 150),
            bi("down", 200, 300, 150, 110),
            bi("up", 300, 400, 110, 155),     # 155 ≥ 150：次高点结构被否定
        ]
        self.assertIsNone(pendingReferOf(broken, "sell"))

    def test_wrong_forming_direction_returns_none(self):
        self.assertIsNone(pendingReferOf(self.bis, "buy"))

    def test_single_bi_or_dirty_data_returns_none(self):
        self.assertIsNone(pendingReferOf([bi("up", 100, 200, 90, 120)], "sell"))
        dirty = [bi("up", 100, 200, 90, 120), bi("up", 200, 300, 120, 140)]
        self.assertIsNone(pendingReferOf(dirty, "sell"))


class TestBuildFibPendingFallback(unittest.TestCase):
    """buildFibCandidates 预期回退：无已形成非一类点时用形成中笔补位。"""

    def setUp(self):
        self.bis = [
            bi("up", 100, 200, 100, 150),
            bi("down", 200, 300, 150, 110),   # 卖向参照笔
            bi("up", 300, 400, 110, 140),     # 形成中上涨笔
        ]
        self.bars = [bar(350, 160, 100, 130)]

    def test_no_formed_point_pending_sell(self):
        cands = buildFibCandidates(self.bis, [], [], [0.382, 0.5, 0.618], self.bars, 5)
        self.assertEqual(len(cands), 3)
        self.assertTrue(all(c["pending"] is True and c["type"] == "RES" and c["fib"] for c in cands))
        self.assertEqual(cands[0]["fromPoint"], {"type": "预期2卖", "time": 400, "price": 140})
        self.assertEqual(cands[0]["breakTime"], 400)      # 形成笔极值时间 = 锚点
        self.assertEqual(cands[0]["lastTouch"], 400)
        self.assertEqual(cands[0]["firstTouch"], 200)     # 参照笔起点
        self.assertEqual(cands[0]["referBi"]["startPrice"], 150)
        # 参照 down 150→110：0.382 → 110+0.382×40 = 125.28
        self.assertAlmostEqual(cands[0]["price"], 125.28, places=9)

    def test_buy_side_pending(self):
        bis = [
            bi("down", 100, 200, 150, 110),
            bi("up", 200, 300, 110, 150),
            bi("down", 300, 400, 150, 120),   # 形成中，120 > 110 ✓
        ]
        cands = buildFibCandidates(bis, [], [], [0.5], self.bars, 5)
        self.assertEqual(len(cands), 1)
        self.assertIs(cands[0]["pending"], True)
        self.assertEqual(cands[0]["type"], "SUP")
        self.assertEqual(cands[0]["fromPoint"]["type"], "预期2买")
        # 参照 up 110→150：0.5 → 150-0.5×40 = 130
        self.assertAlmostEqual(cands[0]["price"], 130, places=9)

    def test_formed_point_wins_no_pending_for_that_side(self):
        # 买向有已形成 2买@300 → 用已形成点；卖向无 → 预期回退（末笔 up 140 < 150 ✓）
        cands = buildFibCandidates(
            self.bis, [{"type": "2买", "time": 300, "price": 110}], [], [0.5], self.bars, 5)
        self.assertEqual(len(cands), 2)
        formed = next(c for c in cands if "pending" not in c)
        pend = next(c for c in cands if c.get("pending"))
        self.assertEqual(formed["type"], "SUP")          # 已形成点：参照 up 100→150 → 125
        self.assertAlmostEqual(formed["price"], 125, places=9)
        self.assertEqual(formed["fromPoint"]["type"], "2买")
        self.assertEqual(pend["type"], "RES")            # 预期：参照 down 150→110 → 130
        self.assertAlmostEqual(pend["price"], 130, places=9)
        self.assertEqual(pend["fromPoint"]["type"], "预期2卖")


class TestComputeSrflipFib(unittest.TestCase):
    """compute_srflip 集成：fib 进全量候选池（保留自身标记），drawnByPeriod 取代 drawn/drawnFib。"""

    def setUp(self):
        # 同 TestBuildFibCandidates 夹具；无上级笔 → findBuyPoints/findSellPoints
        # 走结构底/结构顶分支产出 2买@1300/类2买@1500、2卖@1800/类2卖@2000
        self.periodBis = {"3": [
            bi("down", 1000, 1100, 100, 80),
            bi("up", 1100, 1200, 80, 120),
            bi("down", 1200, 1300, 120, 90),
            bi("up", 1300, 1400, 90, 130),
            bi("down", 1400, 1500, 130, 100),
            bi("up", 1500, 1600, 100, 140),
            bi("down", 1600, 1700, 140, 110),
            bi("up", 1700, 1800, 110, 135),
            bi("down", 1800, 1900, 135, 105),
            bi("up", 1900, 2000, 105, 128),
        ]}
        self.barsByPeriod = {"3": [bar(t, 115, 105, 110) for t in range(1000, 2050, 50)]}

    def test_fib_only_merged(self):
        # 不合并：6 条 fib 线各自独立进候选池（比率间距 1.74 无并簇概念）
        out = compute_srflip(self.periodBis, self.barsByPeriod, ["3"],
                             srTypes=("fib",), periodAtrsIn={"3": 1})
        fibs = [f for f in out["merged"] if f.get("fib")]
        self.assertEqual(len(fibs), 6)  # 每方向最新点 × 3 比率
        self.assertTrue(all(f.get("srcType") == "fib" for f in fibs))  # 单来源独立线保留 fib 标记
        self.assertNotIn("drawnFib", out)
        self.assertNotIn("drawn", out)
        self.assertIn("3", out["drawnByPeriod"])
        buys = sorted(f["price"] for f in fibs if f["type"] == "SUP")
        # 参照笔 up 90→130：130-0.382×40 / 130-0.5×40 / 130-0.618×40
        self.assertAlmostEqual(buys[0], 105.28, places=9)
        self.assertAlmostEqual(buys[1], 110.0, places=9)
        self.assertAlmostEqual(buys[2], 114.72, places=9)
        sells = sorted(f["price"] for f in fibs if f["type"] == "RES")
        # 参照笔 down 135→105：105+0.382×30 / +0.5×30 / +0.618×30
        self.assertAlmostEqual(sells[0], 116.46, places=9)
        self.assertAlmostEqual(sells[1], 120.0, places=9)
        self.assertAlmostEqual(sells[2], 123.54, places=9)
        # level 与落盘口径一致（无 sources 合并字段）
        self.assertTrue(all(f["level"] == "3" for f in fibs))
        self.assertTrue(all("sources" not in f for f in fibs))
        # periods 含 fib 追加条目
        self.assertEqual(len(out["periods"]["3"]), 6)

    def test_cluster_only_has_no_fib(self):
        out = compute_srflip(self.periodBis, self.barsByPeriod, ["3"], srTypes=("cluster",))
        self.assertFalse(any(f.get("fib") for f in out["merged"]))
        self.assertNotIn("drawnFib", out)
        self.assertNotIn("drawn", out)

    def test_single_ratio_one_per_side(self):
        out = compute_srflip(self.periodBis, self.barsByPeriod, ["3"],
                             srTypes=("fib",), fibLevels=[0.5], periodAtrsIn={"3": 1})
        fibs = [f for f in out["merged"] if f.get("fib")]
        self.assertEqual(len(fibs), 2)
        prices = sorted(f["price"] for f in fibs)
        self.assertAlmostEqual(prices[0], 110.0, places=9)
        self.assertAlmostEqual(prices[1], 120.0, places=9)


class TestCalcBOLL(unittest.TestCase):
    """calcBOLL 布林带（已收盘口径，总体标准差 ÷N，与 JS 对齐）。"""

    def test_constant_series_all_bands_equal(self):
        bars = [bar(t, 100, 100, 100) for t in range(27)]
        band = calcBOLL(bars, 26, 2)
        self.assertAlmostEqual(band["mid"], 100, places=9)
        self.assertAlmostEqual(band["upper"], 100, places=9)
        self.assertAlmostEqual(band["lower"], 100, places=9)

    def test_manual_series(self):
        # 已收盘 4 根 [1,3,1,3]，末根形成中排除 → mid=2, σ=1, mult=2 → upper=4 lower=0
        bars = [bar(1, 1, 1, 1), bar(2, 3, 3, 3), bar(3, 1, 1, 1), bar(4, 3, 3, 3), bar(5, 9, 9, 9)]
        band = calcBOLL(bars, 4, 2)
        self.assertAlmostEqual(band["mid"], 2, places=9)
        self.assertAlmostEqual(band["upper"], 4, places=9)
        self.assertAlmostEqual(band["lower"], 0, places=9)

    def test_last_forming_bar_excluded(self):
        a = calcBOLL([bar(t, 100, 100, 100) for t in range(27)] + [bar(99, 200, 200, 200)], 26, 2)
        b = calcBOLL([bar(t, 100, 100, 100) for t in range(27)] + [bar(99, 5, 5, 5)], 26, 2)
        self.assertAlmostEqual(a["mid"], b["mid"], places=9)
        self.assertAlmostEqual(a["upper"], b["upper"], places=9)

    def test_insufficient_closed_bars_returns_none(self):
        self.assertIsNone(calcBOLL([bar(t, 100, 100, 100) for t in range(26)], 26, 2))  # 仅 25 根已收盘
        self.assertIsNone(calcBOLL([], 26, 2))


class TestBuildBollCandidates(unittest.TestCase):
    """buildBollCandidates 三轨组装（上 RES / 下 SUP / 中按现价侧）。"""

    def test_three_track_types_and_mid_side(self):
        bars = [bar(t, 100, 100, 100) for t in range(27)]
        above = buildBollCandidates(bars, 26, 2, 120)
        self.assertEqual(len(above), 3)
        self.assertEqual(above[0]["boll"], "upper")
        self.assertEqual(above[0]["type"], "RES")
        self.assertEqual(above[1]["boll"], "mid")
        self.assertEqual(above[1]["type"], "SUP")  # 现价 120 ≥ mid 100
        self.assertEqual(above[2]["boll"], "lower")
        self.assertEqual(above[2]["type"], "SUP")
        below = buildBollCandidates(bars, 26, 2, 90)
        self.assertEqual(below[1]["type"], "RES")  # 现价 90 < mid 100

    def test_fields_touch_anchor(self):
        bars = [bar(t, 100, 100, 100) for t in range(27)]
        cands = buildBollCandidates(bars, 26, 2, 100)
        anchor = bars[-2]["time"]
        for c in cands:
            self.assertEqual(c["touchCount"], 1)
            self.assertEqual(c["barsPassed"], 0)
            self.assertEqual(c["firstTouch"], anchor)
            self.assertEqual(c["lastTouch"], anchor)
            self.assertEqual(c["breakTime"], anchor)

    def test_insufficient_bars_empty(self):
        self.assertEqual(buildBollCandidates([bar(t, 100, 100, 100) for t in range(10)], 26, 2, 100), [])


class TestFlattenCandidates(unittest.TestCase):
    """全量候选池：flatten_candidates 展平不合并——同价位不同周期/类型各自独立、保留标记。"""

    @staticmethod
    def _flip(price, touch, bars, **kw):
        f = {"price": price, "touchCount": touch, "barsPassed": bars, "type": "R2S",
             "breakTime": 1000, "firstTouch": 500, "lastTouch": 900}
        f.update(kw)
        return f

    def test_same_price_no_merge_keeps_markers(self):
        # 同价位（价差 0.2 < 旧合并容差 5）的 fib 与 cluster 不合并：两条独立线、价格原样
        fibCand = self._flip(100.0, 1, 5, fib=True, ratio=0.5,
                             fromPoint={"type": "2买", "time": 1, "price": 1}, referBi={})
        clusterCand = self._flip(100.2, 4, 20)
        merged = flatten_candidates({"60": [fibCand], "15": [clusterCand]})
        self.assertEqual(len(merged), 2)
        bySrc = {f["srcType"]: f for f in merged}
        self.assertEqual(bySrc["fib"]["price"], 100.0)      # 价格=原始识别价，无加权平均
        self.assertEqual(bySrc["fib"]["ratio"], 0.5)        # fib 标记保留
        self.assertEqual(bySrc["cluster"]["price"], 100.2)
        self.assertEqual(bySrc["cluster"]["touchCount"], 4)  # touchCount 不累加
        self.assertTrue(all("sources" not in f for f in merged))  # 无合并字段

    def test_level_and_srctype_attached(self):
        bollCand = self._flip(100, 1, 0, boll="upper")
        merged = flatten_candidates({"60": [bollCand], "3": [self._flip(90, 2, 3)]})
        b60 = [f for f in merged if f["level"] == "60"][0]
        c3 = [f for f in merged if f["level"] == "3"][0]
        self.assertEqual(b60["srcType"], "boll")
        self.assertEqual(b60["boll"], "upper")
        self.assertEqual(c3["srcType"], "cluster")

    def test_sort_level_then_price(self):
        # 排序：LEVEL_ORDER 级别序（大→小）优先，同级别按 price 升序
        merged = flatten_candidates({
            "3": [self._flip(101, 1, 1), self._flip(100, 1, 1)],
            "60": [self._flip(105, 1, 1)],
        })
        self.assertEqual([(f["level"], f["price"]) for f in merged],
                         [("60", 105), ("3", 100), ("3", 101)])


class TestPickNearestForDisplay(unittest.TestCase):
    """按显示周期选取：各周期独立（不继承）/ 就近上下各 N / 距离上限 / 不对称。"""

    @staticmethod
    def _flip(price, touch, bars, **kw):
        f = {"price": price, "touchCount": touch, "barsPassed": bars, "type": "R2S",
             "breakTime": 1000, "firstTouch": 500, "lastTouch": 900}
        f.update(kw)
        return f

    def test_no_cross_period_inheritance(self):
        # 各周期独立：240 线只进 240 图，3 图只看 3 自己的线（不再继承高级别线）
        merged = [self._flip(105, 8, 20, level="240"), self._flip(98, 3, 5, level="3")]
        drawn = pickNearestForDisplay(merged, ["240", "3"], 100, 1, 3.0, {"240": 10, "3": 2})
        self.assertEqual(drawn["240"][0]["level"], "240")
        self.assertEqual(drawn["3"][0]["level"], "3")

    def test_own_period_empty_draws_nothing(self):
        # 本周期无候选 → 该图空（即使其它周期线在距离内也不继承）
        merged = [self._flip(105, 8, 20, level="240")]
        drawn = pickNearestForDisplay(merged, ["240", "3"], 100, 1, 3.0, {"240": 10, "3": 2})
        self.assertEqual(len(drawn["240"]), 1)
        self.assertEqual(drawn["3"], [])

    def test_nearest_above_below_n(self):
        merged = [self._flip(102, 1, 5, level="60"), self._flip(105, 1, 5, level="60"),
                  self._flip(98, 1, 5, level="60"), self._flip(95, 1, 5, level="60")]
        drawn = pickNearestForDisplay(merged, ["60"], 100, 2, 3.0, {"60": 10})
        lines = drawn["60"]
        above = [f["price"] for f in lines if f["price"] >= 100]
        below = [f["price"] for f in lines if f["price"] < 100]
        self.assertEqual(above, [102, 105])
        self.assertEqual(below, [98, 95])

    def test_distance_cap_and_asymmetry(self):
        merged = [self._flip(150, 9, 30, level="60"), self._flip(105, 3, 10, level="60")]
        drawn = pickNearestForDisplay(merged, ["60"], 100, 2, 3.0, {"60": 10})
        self.assertEqual(len(drawn["60"]), 1)
        self.assertEqual(drawn["60"][0]["price"], 105)

    def test_missing_atr_infinity(self):
        merged = [self._flip(500, 1, 5, level="60")]
        drawn = pickNearestForDisplay(merged, ["60"], 100, 1, 3.0, {})
        self.assertEqual(len(drawn["60"]), 1)
        self.assertEqual(drawn["60"][0]["price"], 500)


class TestSourceLabel(unittest.TestCase):
    """来源标注：periodNameOf/sourceLabelOf/labelOf。"""

    def test_period_names(self):
        self.assertEqual(periodNameOf("3"), "3分钟")
        self.assertEqual(periodNameOf("15"), "15分钟")
        self.assertEqual(periodNameOf("60"), "1小时")
        self.assertEqual(periodNameOf("240"), "4小时")
        self.assertEqual(periodNameOf("D"), "日线")

    def test_boll_labels(self):
        self.assertEqual(sourceLabelOf({"boll": "upper"}), "BOLL上轨")
        self.assertEqual(sourceLabelOf({"boll": "mid"}), "BOLL中轨")
        self.assertEqual(sourceLabelOf({"boll": "lower"}), "BOLL下轨")

    def test_fib_labels(self):
        self.assertEqual(sourceLabelOf({"fib": True, "ratio": 0.5}), "黄金分割0.5")
        self.assertEqual(sourceLabelOf({"fib": True, "pending": True, "fromPoint": {"type": "预期2卖"}}), "预期2卖")

    def test_cluster_label(self):
        self.assertEqual(sourceLabelOf({"type": "R2S"}), "密集区")

    def test_label_of(self):
        self.assertEqual(labelOf({"boll": "upper", "level": "240"}), "BOLL上轨+4小时")
        self.assertEqual(labelOf({"type": "R2S", "level": "15"}), "密集区+15分钟")
        self.assertEqual(labelOf({"fib": True, "ratio": 0.5, "level": "60"}), "黄金分割0.5+1小时")


class TestComputeSrflipBoll(unittest.TestCase):
    """compute_srflip 集成：boll 进全量候选池与 drawnByPeriod。"""

    def setUp(self):
        # 上升趋势K线（30 根，26 已收盘 + 末根形成中）；3 周期单周期夹具（cluster/fib 关闭）
        self.periodBis = {"3": [
            bi("up", 1000, 1010, 100, 110),
            bi("down", 1010, 1020, 110, 105),
            bi("up", 1020, 1030, 105, 115),
        ]}
        self.barsByPeriod = {"3": [bar(t, 200, 80, 100 + i) for i, t in enumerate(range(1000, 1030))]}

    def test_boll_only_enters_merged_and_drawnByPeriod(self):
        out = compute_srflip(self.periodBis, self.barsByPeriod, ["3"],
                             srTypes=("boll",), periodAtrsIn={"3": 0.1})
        bolls = [f for f in out["merged"] if f.get("boll")]
        self.assertEqual(len(bolls), 3)
        self.assertTrue(all(f.get("srcType") == "boll" for f in bolls))
        tags = sorted(f["boll"] for f in bolls)
        self.assertEqual(tags, ["lower", "mid", "upper"])
        self.assertIn("3", out["drawnByPeriod"])
        self.assertNotIn("drawnFib", out)
        self.assertNotIn("drawn", out)
        self.assertEqual(len(out["periods"]["3"]), 3)

    def test_cluster_boll_default_together(self):
        # 默认 srTypes 现为 cluster+boll；无上级笔夹具下 cluster 也可能产出候选，boll 必进 merged
        out = compute_srflip(self.periodBis, self.barsByPeriod, ["3"], periodAtrsIn={"3": 0.1})
        self.assertTrue(any(f.get("boll") for f in out["merged"]))


class TestSrParamExtension(unittest.TestCase):
    """调试页引擎扩展：clusterParts / minTouchsIn / recentBiCount / 评分权重 /
    sideCount。默认路径输出与扩展前逐键一致。"""

    # ---- 夹具 ----
    def _bars_simple(self, last_t, hi=112, lo=88, tail=1):
        """简单振荡K线 + 收尾两段突破（先上穿后下穿，触发强互换的突破分支）。"""
        import math
        bars = []
        for i in range(last_t + 30):
            c = 102 + 8 * math.sin(i / 7.0)
            bars.append(bar(i, c + 3, c - 3, round(c, 2)))
        if tail:
            for j in range(1, 22):  # 108 → 96 单调下行，横跨两个价位带
                c = 108 - 0.6 * j
                bars.append(bar(last_t + j, c + 1, c - 1, round(c, 2)))
        return bars

    def _zigzag_bis(self, cycles=6, base=100, top=105):
        """往复 zigzag：端点在 base/top 反复出现（触及次数累积、价位稳定成簇）。"""
        bis, t = [], 0
        for _ in range(cycles):
            bis.append(bi("up", t, t + 10, base, top))
            bis.append(bi("down", t + 10, t + 20, top, base))
            t += 20
        return bis

    def _rising_bis(self, n=24):
        """阶梯上升 zigzag：每次摆动端点价位都不同（近期笔数窗口越长簇越多）。"""
        bis, t, p = [], 0, 100.0
        for _ in range(n):
            top = p + 3.0
            bis.append(bi("up", t, t + 10, p, top))
            bis.append(bi("down", t + 10, t + 20, top, top - 2.5))
            p = top - 2.5
            t += 20
        return bis

    def _run(self, bis, parts=("flip", "recent"), recent_bi=20, min_touch=None,
             side_count=2, period="15", base=100, top=105, rising=False):
        """compute_srflip 便捷调用：cluster 单类型 + 固定 ATR，返回 out。"""
        bars = self._bars_simple(len(bis) * 20)
        return compute_srflip(
            {period: bis}, {period: bars}, [period],
            srTypes=("cluster",), clusterParts=parts, periodAtrsIn={period: 0.5},
            minTouchsIn={period: min_touch} if min_touch else None,
            recentBiCount=recent_bi, maxPerPeriod=500, sideCount=side_count)

    # ---- 用例 ----
    def test_cluster_parts_filter(self):
        bis = self._zigzag_bis()
        full = self._run(bis)["periods"]["15"]
        flip = self._run(bis, parts=("flip",))["periods"]["15"]
        recent = self._run(bis, parts=("recent",))["periods"]["15"]
        self.assertTrue(recent, "近期极值应恒有候选（无触及要求）")
        self.assertTrue(all(f.get("recent") for f in recent))
        self.assertTrue(all(not f.get("recent") for f in flip))
        self.assertEqual(len(full), len(flip) + len(recent))  # 两子集互斥且相加 = 全集

    def test_cluster_parts_none_is_empty(self):
        out = self._run(self._zigzag_bis(), parts=())
        self.assertEqual(out["periods"]["15"], [])

    def test_recent_bi_count_window(self):
        r20 = self._run(self._rising_bis(), parts=("recent",), recent_bi=20)["periods"]["15"]
        r2 = self._run(self._rising_bis(), parts=("recent",), recent_bi=2)["periods"]["15"]
        self.assertGreater(len(r20), len(r2))

    def test_min_touchs_in_per_level(self):
        bis = self._zigzag_bis()
        default = self._run(bis, parts=("flip",))["periods"]["15"]
        self.assertTrue(default, "突破尾段应触发强互换候选")
        high = self._run(bis, parts=("flip",), min_touch=99)["periods"]["15"]
        self.assertEqual(high, [])

    def test_side_count_caps_drawn(self):
        bars = [bar(t, 200, 80, 100 + i) for i, t in enumerate(range(1000, 1030))]
        bis = {"3": [bi("up", 1000, 1010, 100, 110), bi("down", 1010, 1020, 110, 105),
                     bi("up", 1020, 1030, 105, 115)]}
        for sc, cap in ((1, 2), (2, 4), (5, 10)):
            out = compute_srflip(bis, {"3": bars}, ["3"], srTypes=("boll",),
                                 periodAtrsIn={"3": 0.1}, sideCount=sc)
            self.assertLessEqual(len(out["drawnByPeriod"]["3"]), cap)

    def test_flip_score_explicit_weights(self):
        group = [{"touchCount": 1, "barsPassed": 1},
                 {"touchCount": 2, "barsPassed": 100},
                 {"touchCount": 3, "barsPassed": 101}]
        f = group[1]
        touch = flipScore(f, group, 1.0, 0.0)
        bars_w = flipScore(f, group, 0.0, 1.0)
        self.assertAlmostEqual(touch, 0.5)
        self.assertGreater(bars_w, touch)

    def test_default_kwargs_keep_old_output(self):
        bis = self._zigzag_bis()
        bars = self._bars_simple(len(bis) * 20)
        kw = dict(periodBis={"15": bis}, barsByPeriod={"15": bars}, periods=["15"],
                  srTypes=("cluster", "boll"), periodAtrsIn={"15": 0.5})
        a = compute_srflip(**kw)
        b = compute_srflip(**kw, clusterParts=("flip", "recent"), minTouchsIn=None,
                           recentBiCount=20, touchWeight=0.6, barsWeight=0.4,
                           sideCount=2)
        self.assertEqual(a, b)


class TestManualLevels(unittest.TestCase):
    """人工支阻位：键存在 = 该周期支阻位来源=人工——替换密集区、全部画出；
    叠加层（fib/BOLL）独立照常；空列表 = 该周期没有支阻位（不回退系统计算）。"""

    # ---- 夹具 ----
    def _bis(self, cycles=4, base=100, top=105):
        bis, t = [], 0
        for _ in range(cycles):
            bis.append(bi("up", t, t + 10, base, top))
            bis.append(bi("down", t + 10, t + 20, top, base))
            t += 20
        return bis

    def _bars(self, n=80):
        return [bar(t, 108, 92, 100 + (i % 3)) for i, t in enumerate(range(0, n * 5, 5))]

    def _run(self, manualLevels=None, srTypes=("cluster", "boll")):
        bis = {"15": self._bis(), "60": self._bis(3)}
        return compute_srflip(bis, {"15": self._bars(), "60": self._bars()},
                              ["60", "15"], srTypes=srTypes,
                              periodAtrsIn={"60": 1.0, "15": 0.5},
                              manualLevels=manualLevels)

    # ---- 用例 ----
    def test_manual_replaces_cluster_only(self):
        # 60 人工：无密集区候选，srcType=manual；叠加层 BOLL 照常生成
        out = self._run(manualLevels={"60": [4418, 4440, 4462.5]})
        m60 = [f for f in out["merged"] if f["level"] == "60"]
        self.assertTrue(m60)
        self.assertTrue(all(f["srcType"] == "manual" and f.get("manual") for f in m60
                            if f["srcType"] == "manual"))
        self.assertFalse(any(f["srcType"] == "cluster" for f in m60))
        self.assertTrue(any(f["srcType"] == "boll" for f in m60), "叠加层照常")
        self.assertTrue(any(f["srcType"] == "cluster" for f in out["merged"]
                            if f["level"] == "15"), "系统周期密集区不变")

    def test_candidate_fields_and_type_by_current_price(self):
        # 现价 = 15 周期末收盘（≈101）< 全部人工价（价位在现价上方 → 阻力 RES）；
        # 锚点 = 末根已收盘K线 time
        bars = self._bars()
        out = self._run(manualLevels={"60": [4400, 4420]})
        cands = [f for f in out["periods"]["60"] if f.get("manual")]
        self.assertEqual(sorted(c["price"] for c in cands), [4400.0, 4420.0])
        anchor = bars[-2]["time"]
        for c in cands:
            self.assertEqual(c["type"], "RES")
            self.assertEqual(c["touchCount"], 1)
            self.assertEqual(c["barsPassed"], 0)
            self.assertEqual(c["breakTime"], anchor)
            self.assertEqual(c["firstTouch"], anchor)
        # 纯函数：现价 ≥ 价位 → SUP（价位在现价下方=支撑）；currentPrice 未知 → 统一 RES
        below = buildManualCandidates([4400, 4420], bars, 4450)
        self.assertEqual([c["type"] for c in below], ["SUP", "SUP"])
        none_ = buildManualCandidates([4400], bars, None)
        self.assertEqual(none_[0]["type"], "RES")

    def test_draw_all_ignores_sidecount_and_distance(self):
        # 7 个价位横跨远超 maxDistAtr×ATR（1.0×3=3）与 sideCount=2，drawnByPeriod 全画
        prices = [4400 + i * 20 for i in range(7)]
        out = self._run(manualLevels={"60": prices})
        d60 = [f for f in out["drawnByPeriod"]["60"] if f.get("manual")]
        self.assertEqual(len(d60), 7)
        self.assertTrue(all(f["label"].startswith("手动位+1小时") for f in d60))

    def test_mixed_periods_system_untouched(self):
        # 60 人工 + 15 系统：15 的 merged 切片与不带 manualLevels 的运行逐项相等
        with_m = self._run(manualLevels={"60": [4400]})
        without = self._run()
        self.assertEqual([f for f in with_m["merged"] if f["level"] == "15"],
                         [f for f in without["merged"] if f["level"] == "15"])
        self.assertEqual(with_m["drawnByPeriod"]["15"], without["drawnByPeriod"]["15"])

    def test_manual_period_without_bis(self):
        # 人工周期不要求 bis≥3：60 无笔仍出人工候选与 ATR（进 displayPeriods）
        out = compute_srflip({"15": self._bis()}, {"15": self._bars(), "60": self._bars()},
                             ["60", "15"], srTypes=("cluster", "fib", "boll"),
                             periodAtrsIn={"60": 1.0, "15": 0.5},
                             manualLevels={"60": [4400, 4420]})
        m60 = [f for f in out["merged"] if f["level"] == "60"]
        self.assertEqual({f["price"] for f in m60 if f.get("manual")}, {4400.0, 4420.0})
        self.assertFalse(any(f["srcType"] == "fib" for f in m60), "无笔周期 fib 不生成")
        self.assertIn("60", out["drawnByPeriod"])

    def test_empty_list_means_no_levels(self):
        # 空列表 = 该周期没有支阻位：无 manual/cluster 候选、不画线；叠加层照常
        out = self._run(manualLevels={"60": []})
        m60 = [f for f in out["merged"] if f["level"] == "60"]
        self.assertFalse(any(f["srcType"] in ("manual", "cluster") for f in m60))
        self.assertTrue(any(f["srcType"] == "boll" for f in m60))
        self.assertFalse(any(f.get("manual") for f in out["drawnByPeriod"]["60"]))

    def test_current_price_none_no_draw(self):
        # 全部周期无 bars：currentPrice=None → drawnByPeriod={}（人工也不画，与 boll 中轨降级一致）
        bis = {"15": self._bis()}
        out = compute_srflip(bis, {}, ["60", "15"], manualLevels={"60": [4400]})
        self.assertEqual(out["drawnByPeriod"], {})

    def test_manual_label(self):
        self.assertEqual(labelOf({"manual": True, "level": "15"}), "手动位+15分钟")
        self.assertEqual(sourceLabelOf({"manual": True}), "手动位")


if __name__ == "__main__":
    unittest.main()
