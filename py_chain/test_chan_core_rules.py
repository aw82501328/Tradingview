# -*- coding: utf-8 -*-
"""chan_core 回填规则单元测试（对应 SPEC_divergence_chanset.md 块1）

覆盖 JS 近几轮迭代回填 py 的规则：
  - markWickBars：长上影压平 + _topCand（low 条件）/ 长下影压平 + _origLow / 窄幅免疫
  - _mergeStep：_topCand/_origLow 旁路传播
  - fractalAt：顶分型端点用 _topCand 影线价
  - buildBi：fractalRangeClear 双向（起点侧 2 根 + 终点侧防反向吞没）、最小间隔脆弱笔例外
  - fixBiExtremes：底端点含分型中心、_origLow 恢复通道（中心只认 _origLow）

运行：python -m unittest py_chain.test_chan_core_rules -v
"""

import unittest

import py_chain.chan_core as cc


def bar(t, h, l, c):
    return {"time": t, "open": c, "high": h, "low": l, "close": c}


def sbar(t, h, l):
    """结构测试用K线：收盘取区间中点（上下影各 50%，不触发 markWickBars 压平），
    保证 highs/lows 原样进入合并/分型，结构完全可预期。"""
    c = (h + l) / 2.0
    return {"time": t, "open": c, "high": h, "low": l, "close": c}


def mk(t, h, l, **kw):
    """构造合并K线（含跳空检测/端点修正所需字段，与 chan_core.test.js 的 mk 对齐）。"""
    m = {"time": t, "high": h, "low": l, "_rawCount": kw.get("rawCount", 1),
         "highTime": kw.get("highTime", t), "lowTime": kw.get("lowTime", t),
         "rawHigh": kw.get("rawHigh", h), "rawLow": kw.get("rawLow", l),
         "rawHighTime": kw.get("rawHighTime", t), "rawLowTime": kw.get("rawLowTime", t)}
    for k in ("_topCand", "_topCandTime", "_origLow", "_origLowTime"):
        if kw.get(k) is not None:
            m[k] = kw[k]
    return m


def build(bars, macd=None):
    """markWickBars→mergeBars→findFractals→buildBi→fixBiExtremes 全管线（ATR=0 关跳空/MACD）。"""
    trimmed = cc.markWickBars(bars)
    merged = cc.mergeBars(trimmed)
    fractals = cc.findFractals(merged)
    bis = cc.buildBi(fractals, merged, 0, macd or [])
    return cc.fixBiExtremes(bis, merged) or bis


class TestMarkWickBars(unittest.TestCase):
    def _bars_with_spike(self):
        # 3 根K线，第 2 根长上影冲高（影线占比 24/29 ≥ 0.70），avgAtr 足够大使 minWick 很小
        return [bar(1, 100, 90, 95), bar(2, 120, 91, 96), bar(3, 110, 92, 100)]

    def test_upper_wick_flattened_without_topcand_when_low_breaks_right(self):
        out = cc.markWickBars(self._bars_with_spike())
        # 长上影一律压平 high 至实体顶
        self.assertEqual(out[1]["high"], 96)
        # low=91 < 右邻 low=92 → 压平会消灭的顶分型中心不成立，不记 _topCand
        self.assertNotIn("_topCand", out[1])
        # 原始数组不被修改
        self.assertEqual(self._bars_with_spike()[1]["high"], 120)

    def test_upper_wick_topcand_when_low_not_below_neighbors(self):
        # bar2 low=90.5 ≥ 左邻 90 且 ≥ 右邻 90 → 压平消灭顶分型中心，记 _topCand
        bars = [bar(1, 100, 90, 95), bar(2, 120, 90.5, 96), bar(3, 110, 90, 100)]
        out = cc.markWickBars(bars)
        self.assertEqual(out[1]["high"], 96)          # 结构压平
        self.assertEqual(out[1]["_topCand"], 120)     # 影线可成端点（时间在合并传播时补记）

    def test_lower_wick_flattened_with_origlow(self):
        bars = [bar(1, 100, 90, 95), bar(2, 100, 70, 96), bar(3, 99, 92, 93)]
        out = cc.markWickBars(bars)
        # 长下影：low 压平至实体底，原低记入 _origLow（端点恢复通道，不进 rawLow）
        self.assertEqual(out[1]["low"], 96)
        self.assertEqual(out[1]["_origLow"], 70)
        self.assertEqual(out[1]["_origLowTime"], 2)

    def test_small_bar_immune_by_min_wick(self):
        # 影线占比达标但绝对长度极小（avgAtr 大）→ 不压平（窄幅小K线免疫）
        big = [bar(1, 200, 80, 140), bar(2, 190, 90, 150)]
        tiny = bar(3, 100.6, 100.0, 100.5)  # 上影 0.1，amp 0.6，占比 0.17... 构造占比达标的窄幅：
        tiny = bar(3, 100.6, 100.0, 100.1)  # 上影 0.5 / amp 0.6 = 0.83
        out = cc.markWickBars(big + [tiny])
        self.assertEqual(out[2]["high"], 100.6)  # 未压平


class TestMergePropagation(unittest.TestCase):
    def test_topcand_and_origlow_propagate_into_merged(self):
        # bar2 长上影 _topCand=120（low 90.5 ≥ 左 90、≥ 右 90）；bar3 被包含合并进 bar2'
        bars = [
            bar(1, 100, 90, 95),
            bar(2, 120, 90.5, 96),
            bar(3, 110, 90, 100),
        ]
        out = cc.markWickBars(bars)
        merged = cc.mergeBars(out)
        cands = [m for m in merged if m.get("_topCand") is not None]
        self.assertTrue(cands, "合并后 _topCand 应传播")
        self.assertEqual(cands[0]["_topCand"], 120)
        self.assertEqual(cands[0]["_topCandTime"], 2)
        # _origLow 同理：bar2 长下影 _origLow=60，bar3 包含合并进同一合并bar
        bars2 = [
            bar(1, 100, 90, 95),
            bar(2, 100, 60, 96),   # 长下影 _origLow=60
            bar(3, 99, 92, 93),    # 与 bar2' 包含合并
        ]
        merged2 = cc.mergeBars(cc.markWickBars(bars2))
        lows = [m for m in merged2 if m.get("_origLow") is not None]
        self.assertTrue(lows)
        self.assertEqual(lows[0]["_origLow"], 60)
        self.assertEqual(lows[0]["_origLowTime"], 2)


class TestFractalTopCand(unittest.TestCase):
    def test_top_fractal_uses_topcand_price_and_time(self):
        # 构造合并K线序列：中心 bar high=96 但带 _topCand=105（更早冲高的影线价）
        merged = [mk(1, 90, 80), mk(2, 96, 82, _topCand=105, _topCandTime=2), mk(3, 95, 81)]
        f = cc.fractalAt(merged, 1)
        self.assertIsNotNone(f)
        self.assertEqual(f["type"], "top")
        self.assertEqual(f["high"], 105)     # 端点价用影线价
        self.assertEqual(f["time"], 2)       # 端点时间用影线所在原始K线时间


class TestFractalRangeClearBidirectional(unittest.TestCase):
    """buildBi 的 fractalRangeClear 双向检查：起点侧 2 根 + 终点侧 3 根防反向吞没。"""

    def _base_bars(self, end_high):
        """i1 顶 100 → i5 底 80 的下跌笔；i6 的 high 决定终点侧检查是否被吞没。"""
        return [
            sbar(0, 95, 85),
            sbar(1, 100, 90),    # 顶分型中心
            sbar(2, 97, 88),
            sbar(3, 95, 86),
            sbar(4, 94, 84),
            sbar(5, 92, 80),     # 底分型中心
            sbar(6, end_high, 83),
        ]

    def test_healthy_down_bi_passes(self):
        bis = build(self._base_bars(end_high=93))
        self.assertEqual(len(bis), 1)
        self.assertEqual(bis[0]["type"], "down")
        self.assertEqual(bis[0]["startPrice"], 100)
        self.assertEqual(bis[0]["endPrice"], 80)

    def test_reverse_engulfing_bottom_rejected(self):
        # 底分型右 bar 冲高破起点顶（endHigh=102 > 100）→ 顶后崩盘反向吞没，下跌笔不成立
        bis = build(self._base_bars(end_high=102))
        self.assertEqual(bis, [])


def _fragile_bars(fall_lows):
    """B0(20)→T1(100)→B1(fall_lows[-1])→T2(102) 结构（相邻合并K线均无包含）：
    B0@i1、T1@i5、B1@i9、T2@i12。T1→B1 间隔恰 4（最小有效笔），回调深度由
    fall_lows（i6..i9 的低点，单调下降）控制；rise = T1.high - B0.low = 80。"""
    l6, l7, l8, l9 = fall_lows
    return [
        sbar(0, 78, 68),
        sbar(1, 72, 20),        # B0
        sbar(2, 80, 55),
        sbar(3, 90, 62),
        sbar(4, 97, 70),
        sbar(5, 100, 80),       # T1
        sbar(6, 96, l6),
        sbar(7, 93, l7),
        sbar(8, 91, l8),
        sbar(9, 88, l9),        # B1
        sbar(10, 90, 75),
        sbar(11, 99, 79),
        sbar(12, 102, 82),      # T2（更高顶）
        sbar(13, 97, 78),
    ]


class TestFragileMinimal(unittest.TestCase):
    def test_shallow_minimal_gap_top_replaced_by_new_extreme(self):
        # pull = 100-68 = 32 < rise*0.5 = 40 → 脆弱笔：T1 被 T2 顶替，上涨笔延伸到 102
        bis = build(_fragile_bars(fall_lows=(72, 70, 69, 68)))
        self.assertTrue(bis)
        self.assertEqual(bis[-1]["type"], "up")
        self.assertEqual(bis[-1]["endPrice"], 102)

    def test_deep_pullback_not_replaced(self):
        # pull = 100-58 = 42 ≥ 40 → 坚实笔：T1 保留（T2 不并入），最后一段是下跌到 B1 的笔
        bis = build(_fragile_bars(fall_lows=(66, 64, 59, 58)))
        self.assertTrue(bis)
        self.assertEqual(bis[-1]["type"], "down")
        self.assertEqual(bis[-1]["endPrice"], 58)


class TestFixBiExtremesOrigLow(unittest.TestCase):
    def test_center_origlow_recovered_as_bottom_endpoint(self):
        # down 笔终点分型中心带着被压平的真低 _origLow=80（< 端点价 85）→ 恢复为端点
        merged = [
            mk(0, 100, 90), mk(1, 96, 88), mk(2, 94, 86), mk(3, 92, 85),
            mk(4, 91, 84),
            mk(5, 90, 85, _origLow=80, _origLowTime=7, rawLow=86),
            mk(6, 92, 86), mk(7, 94, 88),
        ]
        bis = [{
            "type": "down", "startIdx": 0, "endIdx": 5,
            "startTime": 0, "endTime": 5,
            "startPrice": 100, "endPrice": 85, "span": 15, "rawCount": 5,
            "gapLocked": False, "macdCross": False,
        }, {
            "type": "up", "startIdx": 5, "endIdx": 7,
            "startTime": 5, "endTime": 7,
            "startPrice": 85, "endPrice": 94, "span": 9, "rawCount": 2,
            "gapLocked": False, "macdCross": False,
        }]
        out = cc.fixBiExtremes(bis, merged)
        # 中心 bar 只认 _origLow：端点下移到 80、时间用 _origLowTime；idx 不动（在中心上）
        self.assertEqual(out[0]["endPrice"], 80)
        self.assertEqual(out[0]["endTime"], 7)
        self.assertEqual(out[0]["endIdx"], 5)
        # 下一笔起点联动
        self.assertEqual(out[1]["startPrice"], 80)
        self.assertEqual(out[1]["startTime"], 7)

    def test_center_rawlow_not_recovered(self):
        # 分型中心只有 rawLow 更低（可能来自更早结构的老蜡烛）→ 不恢复（过度下移防御）
        merged = [
            mk(0, 100, 90), mk(1, 96, 88), mk(2, 94, 86), mk(3, 92, 85),
            mk(4, 91, 84),
            mk(5, 90, 85, rawLow=80, rawLowTime=7),
            mk(6, 92, 86), mk(7, 94, 88),
        ]
        bis = [{
            "type": "down", "startIdx": 0, "endIdx": 5,
            "startTime": 0, "endTime": 5,
            "startPrice": 100, "endPrice": 85, "span": 15, "rawCount": 5,
            "gapLocked": False, "macdCross": False,
        }, {
            "type": "up", "startIdx": 5, "endIdx": 7,
            "startTime": 5, "endTime": 7,
            "startPrice": 85, "endPrice": 94, "span": 9, "rawCount": 2,
            "gapLocked": False, "macdCross": False,
        }]
        out = cc.fixBiExtremes(bis, merged)
        self.assertEqual(out[0]["endPrice"], 85)  # 未被 rawLow 下移


class TestMacdReplacementExtremes(unittest.TestCase):
    def test_symmetric_boundaries_and_locks(self):
        import copy
        import json
        from pathlib import Path
        cases = json.loads((Path(__file__).parent / "fixtures/macd_replacement_cases.json").read_text())
        for case in cases:
            with self.subTest(case=case["name"]):
                bis = cc.buildBi(copy.deepcopy(case["fractals"]), case["merged"], 0,
                                 case["macd"], case["locked"])
                self.assertEqual([[b["startIdx"], b["endIdx"]] for b in bis], case["expected"])
                self.assert_structure(bis, case["merged"])

    def assert_structure(self, bis, merged):
        for i, b in enumerate(bis):
            self.assertLess(b["startTime"], b["endTime"])
            self.assertEqual(b["span"], abs(b["endPrice"] - b["startPrice"]))
            self.assertEqual(b["rawCount"], cc.countRaw(merged, b["startIdx"], b["endIdx"]))
            if i:
                self.assertEqual((bis[i-1]["endTime"], bis[i-1]["endPrice"]),
                                 (b["startTime"], b["startPrice"]))

    def test_gold_september_9_keeps_intermediate_top(self):
        import json
        from pathlib import Path
        bars = json.loads((Path(__file__).parent / "fixtures/macd_xauusd_20260909.json").read_text())["15"]
        merged = cc.mergeBars(cc.markWickBars(bars))
        bis = cc.buildBi(cc.findFractals(merged), merged, cc.calcATR(bars, 14), cc.calcMACD(bars))
        cc.fixBiExtremes(bis, merged)
        self.assert_structure(bis, merged)
        recent = [b for b in bis if 1788954300 <= b["startTime"] < 1788966900]
        self.assertEqual([(b["startTime"], b["endTime"]) for b in recent],
                         [(1788954300,1788957000),(1788957000,1788960600),(1788960600,1788966900)])
        self.assertTrue(recent[0]["macdCross"])
        self.assertEqual(recent[1]["endPrice"], 4434.175)


if __name__ == "__main__":
    unittest.main()
