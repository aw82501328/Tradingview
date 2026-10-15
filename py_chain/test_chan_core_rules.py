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
    for k in ("_topCand", "_topCandTime", "_origLow", "_origLowTime", "_wickHigh", "_wickHighTime"):
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

    def test_wick_min_len_absolute(self):
        # wickMinLen 绝对长度下限（2026-10-02 起为具体数值，默认 0.5；原 wickAtrK×ATR 口径废除）：
        # 影线长度 < 该值时不压平（即使占比达标、振幅过 wickMinRange 前提）
        bars = [bar(1, 110, 100, 105), bar(2, 125, 100, 101), bar(3, 115, 101, 112)]
        # bar2：amp=25 > 15，上影 24/25=0.96 ≥ 0.70 → 默认 wickMinLen=0.5 时压平
        self.assertEqual(cc.CHAN_CFG_DEFAULTS["wickMinLen"], 0.5)
        self.assertEqual(cc.markWickBars(bars)[1]["high"], 101)
        try:
            cc.apply_cfg({"wickMinLen": 30})
            self.assertEqual(cc.markWickBars(bars)[1]["high"], 125)  # 24 < 30 → 不压平
            self.assertNotIn("_preHigh", cc.markWickBars(bars)[1])
        finally:
            cc.reset_cfg()

    def test_min_range_gate(self):
        # wickMinRange 前提（2026-10-02，默认 15）：整根价差（高-低）须 > 该值才判插针压平
        self.assertEqual(cc.CHAN_CFG_DEFAULTS["wickMinRange"], 15.0)
        big = [bar(1, 110, 100, 105), bar(2, 122, 98, 101), bar(3, 115, 99, 112)]
        # bar2 amp=24 > 15，上影 21/24=0.875 ≥ 0.70 且 ≥ minWick → 压平
        self.assertEqual(cc.markWickBars(big)[1]["high"], 101)
        small = [bar(1, 55, 50, 52.5), bar(2, 61, 49, 51), bar(3, 57.5, 49.5, 56)]
        # 同构缩半：amp=12 ≤ 15 → 不压平（占比 0.83 / minWick 均达标，仅前提不过）
        out = cc.markWickBars(small)
        self.assertEqual(out[1]["high"], 61)
        self.assertNotIn("_preHigh", out[1])
        try:
            cc.apply_cfg({"wickMinRange": 0})
            self.assertEqual(cc.markWickBars(small)[1]["high"], 51)  # 0=不限，回退旧口径
        finally:
            cc.reset_cfg()


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
        # 真实高点 120 与左右没有包含，bar2 独立成块；未再合并时端点时间用 highTime
        self.assertEqual(cands[0].get("_topCandTime", cands[0]["highTime"]), 2)
        # _origLow 同理：bar2 真实低点 60 包住 bar1，包含合并后原低仍在
        bars2 = [
            bar(1, 100, 90, 95),
            bar(2, 100, 60, 96),   # 长下影 _origLow=60
            bar(3, 99, 92, 93),    # 真实低点 92 低于合并后的低点，不再并入
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
        # 底分型右 bar「实体」冲高破起点顶（bodyTop=103 > 100）→ 顶后崩盘反向吞没，下跌笔不成立
        # （2026-10-01 起反向贯穿证据为实体口径：开盘/收盘越过起点顶才算吞没）
        pierce = dict(sbar(6, 103, 83), open=101, close=103)
        bis = build(self._base_bars(end_high=103)[:-1] + [pierce])
        self.assertEqual(bis, [])

    def test_wick_pierce_above_start_top_passes(self):
        # 底分型右 bar 仅影线刺穿起点顶（high=102 > 100 但实体顶 92.5 < 100）
        # → 影线插针不构成反向贯穿证据，下跌笔成立（例：15m 10-1 09:00 长阳
        # 高 4161.385 刺穿 04:30 顶 4160.41 但实体顶 4159.67 未越过）
        bis = build(self._base_bars(end_high=102))
        self.assertEqual(len(bis), 1)
        self.assertEqual(bis[0]["type"], "down")
        self.assertEqual(bis[0]["startPrice"], 100)
        self.assertEqual(bis[0]["endPrice"], 80)


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


class TestFixBiExtremesInnerRecover(unittest.TestCase):
    """端点内部极值恢复（endInnerRecoverOn，2026-10-09）：终点分型**之前**的笔内部块
    里被包含合并吞掉的更极端真低/真高恢复为笔端点（例：60m 2026-10-07 19:00 被 20:00
    插针K线包含、升序合并取高低后 4066.53 从结构消失，底分型落在 21:00 4082.42）。"""

    def _bis(self):
        return [{
            "type": "down", "startIdx": 0, "endIdx": 5,
            "startTime": 0, "endTime": 5,
            "startPrice": 100, "endPrice": 86, "span": 14, "rawCount": 5,
            "gapLocked": False, "macdCross": False,
        }, {
            "type": "up", "startIdx": 5, "endIdx": 7,
            "startTime": 5, "endTime": 7,
            "startPrice": 86, "endPrice": 95, "span": 9, "rawCount": 2,
            "gapLocked": False, "macdCross": False,
        }]

    def test_inner_masked_low_recovered(self):
        # 内部块 idx=3 的 rawLow=78 被包含合并吞掉（低于终点分型价 86）→ 恢复为端点
        merged = [
            mk(0, 100, 90), mk(1, 96, 88), mk(2, 94, 86),
            mk(3, 92, 87, rawLow=78, rawLowTime=33),
            mk(4, 90, 85),
            mk(5, 91, 86),
            mk(6, 93, 88), mk(7, 95, 90),
        ]
        out = cc.fixBiExtremes(self._bis(), merged)
        self.assertEqual((out[0]["endPrice"], out[0]["endTime"], out[0]["endIdx"]), (78, 33, 3))
        self.assertEqual((out[1]["startPrice"], out[1]["startTime"], out[1]["startIdx"]), (78, 33, 3))

    def test_inner_masked_low_off_keeps_old(self):
        # 开关关 → 只扫「终点分型中心及其后」：内部块不参与（旧行为）
        merged = [
            mk(0, 100, 90), mk(1, 96, 88), mk(2, 94, 86),
            mk(3, 92, 87, rawLow=78, rawLowTime=33),
            mk(4, 90, 85),
            mk(5, 91, 86),
            mk(6, 93, 88), mk(7, 95, 90),
        ]
        old = cc.CHAN_CFG.get("endInnerRecoverOn", True)
        cc.CHAN_CFG["endInnerRecoverOn"] = False
        try:
            out = cc.fixBiExtremes(self._bis(), merged)
        finally:
            cc.CHAN_CFG["endInnerRecoverOn"] = old
        self.assertEqual((out[0]["endPrice"], out[0]["endTime"], out[0]["endIdx"]), (86, 5, 5))

    def test_inner_masked_high_recovered(self):
        # 上涨笔对称：内部块 rawHigh=112 > 终点分型价 103 → 恢复为端点；中心不扫
        merged = [
            mk(0, 90, 80), mk(1, 94, 84), mk(2, 96, 85),
            mk(3, 102, 86, rawHigh=112, rawHighTime=33),
            mk(4, 104, 88),
            mk(5, 103, 89),
            mk(6, 100, 86), mk(7, 98, 84),
        ]
        bis = [{
            "type": "up", "startIdx": 0, "endIdx": 5,
            "startTime": 0, "endTime": 5,
            "startPrice": 90, "endPrice": 103, "span": 13, "rawCount": 5,
            "gapLocked": False, "macdCross": False,
        }, {
            "type": "down", "startIdx": 5, "endIdx": 7,
            "startTime": 5, "endTime": 7,
            "startPrice": 103, "endPrice": 98, "span": 5, "rawCount": 2,
            "gapLocked": False, "macdCross": False,
        }]
        out = cc.fixBiExtremes(bis, merged)
        self.assertEqual((out[0]["endPrice"], out[0]["endTime"], out[0]["endIdx"]), (112, 33, 3))
        self.assertEqual((out[1]["startPrice"], out[1]["startTime"]), (112, 33))

    def test_inner_not_below_endpoint_noop(self):
        # 内部块被吞真低不低于终点分型价 → 不动
        merged = [
            mk(0, 100, 90), mk(1, 96, 88), mk(2, 94, 86),
            mk(3, 92, 87, rawLow=88, rawLowTime=33),
            mk(4, 90, 85),
            mk(5, 91, 86),
            mk(6, 93, 88), mk(7, 95, 90),
        ]
        out = cc.fixBiExtremes(self._bis(), merged)
        self.assertEqual((out[0]["endPrice"], out[0]["endIdx"]), (86, 5))


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

    def test_gold_september_9_high_stays_at_2130(self):
        import json
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        bars = json.loads((Path(__file__).parent / "fixtures/macd_xauusd_20260909.json").read_text())["15"]
        merged = cc.mergeBars(cc.markWickBars(bars))
        bis = cc.buildBi(cc.findFractals(merged), merged, cc.calcATR(bars, 14), cc.calcMACD(bars), res="15")
        cc.fixBiExtremes(bis, merged)
        self.assert_structure(bis, merged)
        # 9-10 14:00 顶只比 21:30 高不到 1 点，但 21:30→23:15 下跌已经够笔，
        # 高点留在 21:30 的 4434.175，不再退回 19:45。
        # 23:15→9-10 2:00 上涨也够笔，2:00 顶 4421.65 虽更低，仍是端点。
        tz = timezone(timedelta(hours=8))
        t2130 = int(datetime(2026, 9, 9, 21, 30, tzinfo=tz).timestamp())
        t2315 = int(datetime(2026, 9, 9, 23, 15, tzinfo=tz).timestamp())
        t0200 = int(datetime(2026, 9, 10, 2, 0, tzinfo=tz).timestamp())
        up = next(b for b in bis if b["endTime"] == t2130)
        self.assertEqual(up["type"], "up")
        self.assertEqual(up["endPrice"], 4434.175)
        down = next(b for b in bis if b["startTime"] == t2130)
        self.assertEqual(down["type"], "down")
        self.assertEqual(down["endTime"], t2315)
        self.assertEqual(down["endPrice"], 4375.155)
        up2 = next(b for b in bis if b["startTime"] == t2315)
        self.assertEqual(up2["type"], "up")
        self.assertEqual(up2["endTime"], t0200)
        self.assertEqual(up2["endPrice"], 4421.65)


class TestMasterSwitches(unittest.TestCase):
    """参数页总开关（2026-10-02）：wickMarkOn / wideBarOn 关闭时功能整体停用。"""

    def _spiky(self):
        # 长上影 + 长下影各一根（默认参数下都会被压平）
        return [bar(1, 100, 90, 95), bar(2, 120, 90.5, 96), bar(3, 110, 90, 100),
                bar(4, 100, 70, 96), bar(5, 99, 92, 93)]

    def test_wick_mark_on_off_disables_processing(self):
        bars = self._spiky()
        on = cc.markWickBars(bars)
        self.assertEqual(on[1]["high"], 96)      # 默认开：长上影压平
        self.assertEqual(on[3]["low"], 96)       # 默认开：长下影压平
        try:
            cc.apply_cfg({"wickMarkOn": False})
            off = cc.markWickBars(bars)
            for i, src in enumerate(bars):
                self.assertEqual(off[i]["high"], src["high"])
                self.assertEqual(off[i]["low"], src["low"])
                for k in ("_topCand", "_preHigh", "_origLow", "_origLowTime", "_preLow"):
                    self.assertNotIn(k, off[i])
            # 输出仍是浅拷贝新数组，不污染原始K线
            self.assertIsNot(off[0], bars[0])
        finally:
            cc.reset_cfg()
        self.assertEqual(cc.markWickBars(bars)[1]["high"], 96)

    def test_wide_bar_on_off_gates_points(self):
        # 默认开：五周期 30 点
        for res in ("3", "15", "60", "240", "D"):
            self.assertEqual(cc.wideBarPointsOf(res), 30.0)
        try:
            cc.apply_cfg({"wideBarOn": False})
            for res in ("3", "15", "60", "240", "D", 180, 86400):
                self.assertEqual(cc.wideBarPointsOf(res), 0.0)
        finally:
            cc.reset_cfg()
        self.assertEqual(cc.wideBarPointsOf("3"), 30.0)
        # 总开关与各周期点数独立：单周期 0 仍是不豁免
        try:
            cc.apply_cfg({"wideBarPoints15": 0.0})
            self.assertEqual(cc.wideBarPointsOf("15"), 0.0)
            self.assertEqual(cc.wideBarPointsOf("60"), 30.0)
        finally:
            cc.reset_cfg()


class TestBuildZsPassThroughLeave(unittest.TestCase):
    """2026-10-07 离开判定放宽（终点破边即离开，起点不论）+ 挤空回退。

    复现 7-9 15m 实盘结构（XAUUSD 2026-07-10 08:00 信号归因案例）：深回踩
    4056.45 跌破 zd 后直破上沿（穿越式离开），回踩 4093.86 悬在中枢上方
    （标准 3买 形态）。旧规则下穿越式离开被误当延伸、零重叠回踩笔被误记为
    离开笔并挤空重叠 → 低中枢整枢丢弃 → 3买消失、后续点被误标类2买。"""

    def _bis(self):
        def b(t0, t1, typ, p0, p1):
            return {"type": typ, "startTime": t0, "endTime": t1,
                    "startPrice": p0, "endPrice": p1, "span": abs(p1 - p0)}
        return [
            b(1000, 1100, "up",   4021.82, 4091.67),
            b(1100, 1200, "down", 4091.67, 4071.64),  # 2买
            b(1200, 1300, "up",   4071.64, 4090.02),
            b(1300, 1400, "down", 4090.02, 4056.45),  # 深回踩跌破 zd（下一笔回枢 → 延伸）
            b(1400, 1500, "up",   4056.45, 4118.01),  # 穿越式离开：起点 < zd，终点 > zg
            b(1500, 1600, "down", 4118.01, 4093.86),  # 回踩悬在 zg 上方（零重叠 → 3买）
            b(1600, 1700, "up",   4093.86, 4138.06),
            b(1700, 1800, "down", 4138.06, 4118.91),
            b(1800, 1900, "up",   4118.91, 4127.81),
            b(1900, 2000, "down", 4127.81, 4121.06),
        ]

    def test_low_zs_survives_pass_through_leave(self):
        zss = cc.buildZS(self._bis(), 0)
        low = [z for z in zss
               if abs(z["zd"] - 4071.64) < 1e-6 and abs(z["zg"] - 4090.02) < 1e-6]
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0]["exitTime"], 1400)      # 穿越式离开笔起点结束中枢
        self.assertEqual(low[0]["enterEndTime"], 1100)

    def test_zero_overlap_exit_bi_falls_back_to_constituents(self):
        # 离开/回踩笔与构成笔零重叠时不再挤空丢弃：中间中枢回退为不含离开笔
        # （1700→1800）的重叠（旧规则整枢被删）
        zss = cc.buildZS(self._bis(), 0)
        mid = [z for z in zss
               if abs(z["zd"] - 4093.86) < 1e-6 and abs(z["zg"] - 4118.01) < 1e-6]
        self.assertEqual(len(mid), 1)
        self.assertEqual(mid[0]["exitTime"], 1700)

    def test_buy_points_third_class_series(self):
        bis = self._bis()
        upper = [{"type": "up", "startTime": 900, "endTime": 1650,
                  "startPrice": 4021.82, "endPrice": 4138.06,
                  "coverageEnd": 2000, "span": 116.24}]
        macd = [{"time": t, "dif": 0.0, "dea": 0.0, "macd": 0.0}
                for t in range(950, 2001, 50)]
        pts = cc.findBuyPoints(bis, upper, macd, 900, 0.0, 0.0)
        got = [(p["type"], p["time"], p["price"]) for p in pts]
        # 2026-10-14 规则v2：
        #   1买@1000 = 上涨笔起点 (900,4021.82) 投影到 ±1 根K内同价小级别起点；
        #   2买@1200(4071.64) = 锚后首个抬低（锚前无顶点 → strong=None 弱先行）；
        #   1400@4056.45 低于前低但仍高于 1买锚 4021.82、且与 2买同中枢 → 类2买；
        #   低中枢被向上离开后 1600 回踩不进 → 3买；1800 起回踩归第二中枢生命周期：
        #   第二中枢的上破笔（终点4138.06）恰结束于其 rawEnd=1700、被 buildZS 吸收为延伸笔
        #   ——结束于 rawEnd 的笔参与离开判定（修正）→ 1800 回踩 4118.91>4118.01 不进 →
        #   4买（窗口内第2个被离开的中枢）；2000 撞下一中枢 trioEnd 边界 → 停。
        self.assertEqual(got, [
            ("1买", 1000, 4021.82),
            ("2买", 1200, 4071.64),
            ("类2买", 1400, 4056.45),
            ("3买", 1600, 4093.86),
            ("4买", 1800, 4118.91),
        ])
        self.assertIsNone(next(p for p in pts if p["type"] == "2买").get("strong"))


class TestPointRulesV2(unittest.TestCase):
    """2026-10-14 规则v2 二类/三类族（与 JS chan_core.test.js「买卖点规则v2 二类/三类族」同名组同步）：
    二类族 = 一类锚后低点/高点逐一抬高/压低——首个=2买/2卖、与主点同一个中枢（按时间归属）
    的后续=类2买/类2卖；强弱 = 候选之后的反弹笔端点 vs 之前最近前高/前低（过=强、未过=弱、
    未走完=None 弱先行）。三类族 = 同向离开中枢后回踩不进，逐中枢序号（窗口内第1个被离开
    的中枢出 3类/类3、第2个出 4类/类4、≥3 封顶类4）。背驰不再参与买卖点判定。"""

    def b(self, t0, t1, typ, p0, p1):
        return {"type": typ, "startTime": t0, "endTime": t1,
                "startPrice": p0, "endPrice": p1, "span": abs(p1 - p0)}

    MACD_LOW = [{"time": t, "dif": 0.0, "dea": 0.0, "macd": 0.0} for t in range(100, 1001, 50)]

    def test_first_raised_low_and_same_zs_class2(self):
        # 上级：up(0,500] 买侧窗 + down(500,1000] 卖侧窗。锚=1买@0(440)，锚前顶=(-50,460)=前高。
        # 低点 200@441（抬低→2买，锚后反弹最高 455 < 460 → 弱）、400@444（抬高且与2买同中枢→类2买，
        # 反弹最高仍 455 < 460 → 弱）、600@445（抬高但不在同中枢，且在下跌窗→不标）。
        bis = [
            self.b(-50, 0, "down", 460.0, 440.0),
            self.b(0, 100, "up", 440.0, 455.0),
            self.b(100, 200, "down", 455.0, 441.0),   # 2买@200（弱）
            self.b(200, 300, "up", 441.0, 450.0),
            self.b(300, 400, "down", 450.0, 444.0),   # 类2买@400（弱）
            self.b(400, 500, "up", 444.0, 470.0),
            self.b(500, 600, "down", 470.0, 445.0),   # 下跌窗内 → 方向滤除
            self.b(600, 700, "up", 445.0, 448.0),
        ]
        upper = [
            self.b(0, 500, "up", 440.0, 470.0),
            self.b(500, 1000, "down", 470.0, 430.0),
        ]
        pts = cc.findBuyPoints(bis, upper, self.MACD_LOW, 900, 0.0, 0.0)
        got = [(p["type"], p["time"], p["price"], p.get("strong")) for p in pts
               if p["type"] in ("2买", "类2买")]
        self.assertEqual(got, [
            ("2买", 200, 441.0, False),
            ("类2买", 400, 444.0, False),
        ])

    def test_not_raised_updates_ref_only(self):
        # 400@435 低于前低 441 → 不标；其后 600@445 抬高但在下跌窗被滤 → 二类族只有 2买@200
        bis = [
            self.b(-50, 0, "down", 460.0, 440.0),
            self.b(0, 100, "up", 440.0, 455.0),
            self.b(100, 200, "down", 455.0, 441.0),
            self.b(200, 300, "up", 441.0, 450.0),
            self.b(300, 400, "down", 450.0, 435.0),   # 未抬高
            self.b(400, 500, "up", 435.0, 470.0),
            self.b(500, 600, "down", 470.0, 445.0),   # 抬高但下跌窗 → 滤除
        ]
        upper = [
            self.b(0, 500, "up", 440.0, 470.0),
            self.b(500, 1000, "down", 470.0, 430.0),
        ]
        pts = cc.findBuyPoints(bis, upper, self.MACD_LOW, 900, 0.0, 0.0)
        got = [(p["type"], p["time"], p["price"]) for p in pts
               if p["type"] in ("2买", "类2买")]
        self.assertEqual(got, [("2买", 200, 441.0)])

    def test_third_family_leave_and_reentry_count_kept(self):
        # 中枢 zd95/zg110（enterEnd 10、trio完成30、exit 40）：b4 上破 zg=110 离开 → b5 回踩 112 不进 → 3买@60；
        # b7 回踩 98 重进 → 本轮结束；其后 b4~b6 组成的新中枢（zd113/zg130）三笔重叠在 70 完成
        # → 旧枢序列从 70 起永久关闭，b9 回踩 100@113 不再由旧枢认领（新中枢未离开亦不产点）。
        bis = [
            self.b(0, 10, "up", 80.0, 110.0),
            self.b(10, 20, "down", 110.0, 90.0),
            self.b(20, 30, "up", 90.0, 115.0),
            self.b(30, 40, "down", 115.0, 95.0),
            self.b(40, 50, "up", 95.0, 130.0),        # 上破 zg=110 → 离开
            self.b(50, 60, "down", 130.0, 112.0),     # 回踩不进 → 3买@60
            self.b(60, 70, "up", 112.0, 135.0),
            self.b(70, 80, "down", 135.0, 98.0),      # 重进中枢 → 本轮离开结束
            self.b(80, 90, "up", 98.0, 140.0),
            self.b(90, 100, "down", 140.0, 113.0),    # 旧枢已关闭、新中枢未离开 → 不标
        ]
        upper = [self.b(0, 100, "up", 80.0, 140.0)]
        pts = cc.findBuyPoints(bis, upper, self.MACD_LOW, 900, 0.0, 0.0)
        import re as _re
        got = [(p["type"], p["time"], p["price"]) for p in pts
               if _re.search(r"3买|4买", p["type"])]
        self.assertEqual(got, [
            ("3买", 60, 112.0),
        ])

    def test_two_boxes_fourth_buy_and_bound(self):
        # 两盒4买：ZS1 zd102/zg110 上破离开 → 3买@60(115)。ZS2 在 70 完成三笔重叠，
        # 但要到离开笔起点 100 才真正离开——这之前不回进 ZS1 的回踩仍是 ZS1 的类3买
        # （80@118、100@130）。ZS2 上破140离开后的回踩 155>140 → 4买@120。
        bis = [
            self.b(0, 10, "up", 80.0, 110.0),
            self.b(10, 20, "down", 110.0, 100.0),     # 2买@20
            self.b(20, 30, "up", 100.0, 112.0),
            self.b(30, 40, "down", 112.0, 102.0),     # 类2买@40（与2买同中枢 ZS1）
            self.b(40, 50, "up", 102.0, 140.0),       # 上破 zg110 → ZS1 离开
            self.b(50, 60, "down", 140.0, 115.0),     # 回踩不进 → 3买@60
            self.b(60, 70, "up", 115.0, 150.0),
            self.b(70, 80, "down", 150.0, 118.0),     # ZS2 已形成但还没离开：118>110 → 类3买@80
            self.b(80, 90, "up", 118.0, 152.0),
            self.b(90, 100, "down", 152.0, 130.0),    # 离开笔起点：130>110 → 类3买@100
            self.b(100, 110, "up", 130.0, 170.0),     # 上破 ZS2 zg140 → 离开
            self.b(110, 120, "down", 170.0, 155.0),   # 回踩不进 → 4买@120
        ]
        upper = [self.b(0, 120, "up", 80.0, 170.0)]
        pts = cc.findBuyPoints(bis, upper, self.MACD_LOW, 900, 0.0, 0.0)
        got = [(p["type"], p["time"], p["price"]) for p in pts]
        self.assertEqual(got, [
            ("1买", 0, 80.0),
            ("2买", 20, 100.0),
            ("类2买", 40, 102.0),
            ("3买", 60, 115.0),
            ("类3买", 80, 118.0),
            ("类3买", 100, 130.0),
            ("4买", 120, 155.0),
        ])

    def test_unformed_tail_allows_both_sides(self):
        # 未收笔尾段双向放行：上级仅一根已收笔上涨笔（tailStart=500），
        # 其后卖点照标（2卖@700，反弹450不过前低445 → 弱）
        bis = [
            self.b(-50, 0, "down", 460.0, 440.0),
            self.b(0, 100, "up", 440.0, 455.0),
            self.b(100, 200, "down", 455.0, 441.0),
            self.b(200, 300, "up", 441.0, 450.0),
            self.b(300, 400, "down", 450.0, 444.0),
            self.b(400, 500, "up", 444.0, 470.0),     # 顶 (500,470) = 1卖
            self.b(500, 600, "down", 470.0, 445.0),   # 尾段（tailStart=500 之后）
            self.b(600, 700, "up", 445.0, 460.0),     # 460 < 470 压低 → 2卖@700
            self.b(700, 800, "down", 460.0, 450.0),   # 反弹450不过前低445 → 弱
        ]
        upper = [self.b(0, 500, "up", 440.0, 470.0)]
        pts = cc.findSellPoints(bis, upper, self.MACD_LOW, 900, 0.0, 0.0)
        got = [(p["type"], p["time"], p["price"], p.get("strong")) for p in pts]
        self.assertEqual(got, [
            ("1卖", 500, 470.0, None),
            ("2卖", 700, 460.0, False),
        ])

    def test_undetermined_strong_null(self):
        # 强弱当下即可判定：反弹=锚后最高点455 vs 前高=锚前顶460 → 弱。
        # 候选是末笔（其后无反弹笔）也不影响——强弱只看锚到候选之间的已成结构。
        bis = [
            self.b(-50, 0, "down", 460.0, 440.0),
            self.b(0, 100, "up", 440.0, 455.0),
            self.b(100, 200, "down", 455.0, 441.0),   # 2买@200 是末笔 → 当下即判 弱
        ]
        upper = [self.b(0, 500, "up", 440.0, 470.0)]
        pts = cc.findBuyPoints(bis, upper, self.MACD_LOW, 900, 0.0, 0.0)
        two = next(p for p in pts if p["type"] == "2买")
        self.assertFalse(two.get("strong"))

    def test_no_upper_returns_empty(self):
        # 无上级（日线）不标（2026-10-14 规则v2）
        bis = [self.b(-50, 0, "down", 460.0, 440.0), self.b(0, 100, "up", 440.0, 455.0)]
        self.assertEqual(cc.findBuyPoints(bis, [], self.MACD_LOW, 900), [])
        self.assertEqual(cc.findSellPoints(bis, None, self.MACD_LOW, 900), [])


class TestSecondFamilyRouting(unittest.TestCase):
    """strategyOf 二买族强弱二分路由（2026-10-11）：强→等2买、弱→等2买Bear。"""

    def test_routing(self):
        from py_chain.trading_plan import strategyOf
        strong_buy = strategyOf("15", "2买", "r", "l", "强二买")
        self.assertEqual((strong_buy["direction"], strong_buy["strategy"]),
                         ("多头多", "等待回调后做2买"))
        weak_buy = strategyOf("15", "类2买", "r", "l", "弱类2买")
        self.assertEqual((weak_buy["direction"], weak_buy["strategy"]),
                         ("空头多", "等待低点附近的2买"))
        strong_sell = strategyOf("15", "2卖", "r", "l", "强二卖")
        self.assertEqual((strong_sell["direction"], strong_sell["strategy"]),
                         ("空头空", "等待反弹后做2卖"))
        weak_sell = strategyOf("15", "类2卖", "r", "l", "弱类2卖")
        self.assertEqual((weak_sell["direction"], weak_sell["strategy"]),
                         ("多头空", "等待高点附近的2卖"))


class TestFixBiExtremesLastBiWick(unittest.TestCase):
    """末笔长影端点恢复 + 上影镜像 + 绝对极值守卫（2026-10-12 用户规则：
    长影压平后若无更低/更高的**实体**极值成笔（= 影线超过笔区间全部结构极值且低于/
    高于端点），长影即该笔最低/最高点；realtime 进场评估读末笔，末笔同样恢复）。"""

    def _up_down_bis(self):
        return [{
            "type": "up", "startIdx": 0, "endIdx": 2,
            "startTime": 0, "endTime": 2,
            "startPrice": 90, "endPrice": 96, "span": 6, "rawCount": 2,
            "gapLocked": False, "macdCross": False,
        }, {
            "type": "down", "startIdx": 2, "endIdx": 5,
            "startTime": 2, "endTime": 5,
            "startPrice": 96, "endPrice": 86, "span": 10, "rawCount": 3,
            "gapLocked": False, "macdCross": False,
        }]

    def test_last_down_bi_origlow_recovered(self):
        # 末笔（下跌）内部块 _origLow=78 < 端点 86 且 < 笔区间结构最低 85 → 恢复为端点
        merged = [
            mk(0, 90, 80), mk(1, 94, 84), mk(2, 96, 86),
            mk(3, 92, 87, _origLow=78, _origLowTime=33),
            mk(4, 90, 85),
            mk(5, 91, 86),
        ]
        out = cc.fixBiExtremes(self._up_down_bis(), merged)
        self.assertEqual((out[1]["endPrice"], out[1]["endTime"], out[1]["endIdx"]), (78, 33, 3))

    def test_last_down_bi_wick_guarded_by_structural_low(self):
        # 影线 85.5 低于端点 86 但未超过结构最低 85 → 非该笔绝对极值，不恢复
        merged = [
            mk(0, 90, 80), mk(1, 94, 84), mk(2, 96, 86),
            mk(3, 92, 87, _origLow=85.5, _origLowTime=33),
            mk(4, 90, 85),
            mk(5, 91, 86),
        ]
        out = cc.fixBiExtremes(self._up_down_bis(), merged)
        self.assertEqual((out[1]["endPrice"], out[1]["endIdx"]), (86, 5))

    def test_last_up_bi_wickhigh_recovered(self):
        # 上涨镜像：末笔（上涨）内部块 _wickHigh=112 > 端点 103 且 > 结构最高 104 → 恢复
        merged = [
            mk(0, 90, 80), mk(1, 94, 84), mk(2, 96, 85),
            mk(3, 102, 86, _wickHigh=112, _wickHighTime=33),
            mk(4, 104, 88),
            mk(5, 103, 89),
        ]
        bis = [{
            "type": "down", "startIdx": 0, "endIdx": 2,
            "startTime": 0, "endTime": 2,
            "startPrice": 96, "endPrice": 90, "span": 6, "rawCount": 2,
            "gapLocked": False, "macdCross": False,
        }, {
            "type": "up", "startIdx": 2, "endIdx": 5,
            "startTime": 2, "endTime": 5,
            "startPrice": 90, "endPrice": 103, "span": 13, "rawCount": 3,
            "gapLocked": False, "macdCross": False,
        }]
        out = cc.fixBiExtremes(bis, merged)
        self.assertEqual((out[1]["endPrice"], out[1]["endTime"], out[1]["endIdx"]), (112, 33, 3))

    def test_last_up_bi_wick_guarded_by_structural_high(self):
        # 影线 103.5 高于端点 103 但未超过结构最高 104 → 不恢复（近等取后顶不受影线干扰）
        merged = [
            mk(0, 90, 80), mk(1, 94, 84), mk(2, 96, 85),
            mk(3, 102, 86, _wickHigh=103.5, _wickHighTime=33),
            mk(4, 104, 88),
            mk(5, 103, 89),
        ]
        bis = [{
            "type": "down", "startIdx": 0, "endIdx": 2,
            "startTime": 0, "endTime": 2,
            "startPrice": 96, "endPrice": 90, "span": 6, "rawCount": 2,
            "gapLocked": False, "macdCross": False,
        }, {
            "type": "up", "startIdx": 2, "endIdx": 5,
            "startTime": 2, "endTime": 5,
            "startPrice": 90, "endPrice": 103, "span": 13, "rawCount": 3,
            "gapLocked": False, "macdCross": False,
        }]
        out = cc.fixBiExtremes(bis, merged)
        self.assertEqual((out[1]["endPrice"], out[1]["endIdx"]), (103, 5))


if __name__ == "__main__":
    unittest.main()
