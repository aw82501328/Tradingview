# -*- coding: utf-8 -*-
"""锚点K线窗口闸（anchor_bar_window）单元测试（2026-10-18 用户规则）。

规则：大周期某根K线满足买卖点条件（锚点所在K线）→ 去小周期找背驰仅限该根K线内，
K线走完窗口即关、此后不再触发；无永久闩锁——锚随新高/新低漂移或换新点时，每拍按
当前锚现算，新锚所在K线自动重开窗。当下制（evaluateRealtimeEntries）双重校验：
触发拍 tCut 与背驰点都必须落在锚点所在K线内；确认制（evaluateEntry）只约束背驰点。

运行：python -m unittest py_chain.test_anchor_bar_window -v
"""

import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")

import unittest
from unittest import mock

import py_chain.mark_entry as me


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice,
            "span": abs(endPrice - startPrice)}


# 60m K线时间轴（3600s 一根）：bar idx = t // 3600
TIMES60 = list(range(0, 40000, 3600))

# 卖侧 fixture：末笔 up 终点 10800@108.0 挂 2卖 strong → wait2Sell（wantType=up 匹配）
BIS60_SELL = [
    bi("up", 0, 3600, 100, 110),
    bi("down", 3600, 7200, 110, 105),
    bi("up", 7200, 10800, 105, 108),
]
# 买侧镜像：末笔 down 终点 10800@102.0 挂 2买 strong → wait2Buy（wantType=down 匹配）
BIS60_BUY = [
    bi("down", 0, 3600, 110, 100),
    bi("up", 3600, 7200, 100, 105),
    bi("down", 7200, 10800, 105, 102),
]
# 锚漂移 fixture：新高后末笔 up 终点 14400@109.0（锚K线 = bar 4）
BIS60_DRIFT = [
    bi("up", 0, 3600, 100, 110),
    bi("down", 3600, 7200, 110, 105),
    bi("up", 7200, 14400, 105, 109),
]


def _run(bis60, point, tCut, divTime, times=True):
    """mock 屏蔽背驰/支阻细节，只驱动窗口闸：返回 (信号列表, 拒绝记录列表)。"""
    periodBis = {"240": bis60, "60": bis60, "15": bis60, "3": bis60}
    points = {"60": {"buy": [], "sell": []}}
    points["60"]["buy" if point["type"].endswith("买") else "sell"].append(point)
    rejects = []
    with mock.patch.object(me, "realtimeLowerDiverge",
                           return_value=[{"res": "15", "segStart": 7200,
                                          "point": {"time": divTime, "price": 105.0}}]), \
            mock.patch.object(me, "counterMoveQualifies", return_value=True), \
            mock.patch.object(me, "mergedSegmentCount", return_value=99), \
            mock.patch.object(me, "strategyExtraOk", return_value=None), \
            mock.patch.object(me, "near_zone",
                              return_value={"sr": {"price": 105.0}}):
        sigs = me.evaluateRealtimeEntries(
            periodBis, {}, {}, points, [{"price": 105.0}],
            ["240", "60", "15", "3"], trend_res="",
            tCut=tCut, fired=set(),
            periodTimes={"60": TIMES60, "15": TIMES60, "3": TIMES60} if times else None,
            on_reject=lambda gate, period, seg, skey, ctx, t:
                rejects.append({"gate": gate, "period": period, "skey": skey, "ctx": ctx}))
    return sigs, rejects


class TestRealtimeAnchorBarWindow(unittest.TestCase):
    """当下制：触发拍与背驰点都必须落在锚点所在大周期K线内。"""

    SELL_PT = {"type": "2卖", "time": 10800, "price": 108.0, "strong": True}
    BUY_PT = {"type": "2买", "time": 10800, "price": 102.0, "strong": True}

    def test_same_bar_fires(self):
        # 锚 10800（bar 3）、触发拍与背驰点 11000（同 bar 3）→ 放行
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=11000, divTime=11000)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "wait2Sell")
        self.assertFalse(any(r["gate"] == "anchor_bar_window" for r in rejects))

    def test_late_beat_rejected(self):
        # trade#2 型：锚 bar 3、触发拍 15000（bar 4）→ 主闸拒绝、不出信号
        # （realtimeLowerDiverge 均不被调用——窗口关后跳过整段背驰链计算）
        with mock.patch.object(me, "realtimeLowerDiverge",
                               side_effect=AssertionError("窗口外不应评估背驰链")):
            periodBis = {"240": BIS60_SELL, "60": BIS60_SELL, "15": BIS60_SELL, "3": BIS60_SELL}
            points = {"60": {"buy": [], "sell": [dict(self.SELL_PT)]}}
            rejects = []
            with mock.patch.object(me, "counterMoveQualifies", return_value=True), \
                    mock.patch.object(me, "mergedSegmentCount", return_value=99), \
                    mock.patch.object(me, "strategyExtraOk", return_value=None), \
                    mock.patch.object(me, "near_zone", return_value={"sr": {"price": 105.0}}):
                sigs = me.evaluateRealtimeEntries(
                    periodBis, {}, {}, points, [{"price": 105.0}],
                    ["240", "60", "15", "3"], trend_res="",
                    tCut=15000, fired=set(),
                    periodTimes={"60": TIMES60, "15": TIMES60, "3": TIMES60},
                    on_reject=lambda gate, period, seg, skey, ctx, t:
                        rejects.append({"gate": gate, "ctx": ctx}))
        self.assertEqual(sigs, [])
        win = [r for r in rejects if r["gate"] == "anchor_bar_window"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["ctx"]["anchorTime"], 10800)
        self.assertEqual(win[0]["ctx"]["nowBarTime"], 14400)

    def test_stale_divergence_point_rejected(self):
        # 次级保险：触发拍 11000 在窗内（bar 3），但背驰极值 9000（bar 2）是早前
        # K线陈旧极值 → 同 gate 拒绝（ctx 带 markRes/divergeTime）
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=11000, divTime=9000)
        self.assertEqual(sigs, [])
        win = [r for r in rejects if r["gate"] == "anchor_bar_window"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["ctx"]["markRes"], "15")
        self.assertEqual(win[0]["ctx"]["divergeTime"], 9000)

    def test_buy_side_mirror(self):
        # 买侧镜像：锚 bar 3、触发拍 bar 4 → 拒；同 bar → 放行
        sigs, rejects = _run(BIS60_BUY, self.BUY_PT, tCut=15000, divTime=15000)
        self.assertEqual(sigs, [])
        self.assertTrue(any(r["gate"] == "anchor_bar_window" for r in rejects))
        sigs, rejects = _run(BIS60_BUY, self.BUY_PT, tCut=11000, divTime=11000)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "wait2Buy")

    def test_anchor_drift_reopens_window(self):
        # 用户补充语义：旧锚窗口失败后，价格涨过旧锚、新锚落到新K线 → 新窗口重开、
        # 可再次触发（闸无状态、每拍按当前锚现算，无「此点已试过」闩锁）
        drift_pt = {"type": "2卖", "time": 14400, "price": 109.0, "strong": True}
        sigs, rejects = _run(BIS60_DRIFT, drift_pt, tCut=15000, divTime=15000)
        self.assertEqual(len(sigs), 1)
        self.assertFalse(any(r["gate"] == "anchor_bar_window" for r in rejects))

    def test_no_times_degrades_open(self):
        # times 缺失（无 periodTimes 且无 bars 的最小 fixture）→ 降级放行（向后兼容）
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=15000, divTime=15000, times=False)
        self.assertEqual(len(sigs), 1)
        self.assertFalse(any(r["gate"] == "anchor_bar_window" for r in rejects))


class TestConfirmAnchorBarWindow(unittest.TestCase):
    """确认制（evaluateEntry）：只约束背驰点落窗，不查评估拍。"""

    def _ctx(self, anchorTime, bars):
        return {
            "res": "60",
            "bis": bars,
            "upperBis": None,
            "macdArr": [],
            "atr": 0,
            "barSec": 3600,
            "near": 30.0,
            "srLevels": [],
            "periodData": {"60": {"bis": bars, "bars": bars, "macdArr": [], "atr": 0}},
            "anchorPoint": {"type": "2卖", "time": anchorTime, "price": 108.0, "strong": True},
        }

    def test_in_window_passes_out_of_window_rejected(self):
        bars = [{"time": t, "open": 105, "close": 106, "high": 107, "low": 104}
                for t in TIMES60]
        strategy = {"key": "wait2Sell", "direction": "short", "label": "二卖", "pointCls": "强二卖"}
        div = [{"res": "15", "point": {"time": 11000, "price": 105.0}}]
        with mock.patch.object(me, "lastBiOk", return_value=True), \
                mock.patch.object(me, "strategyExtraOk", return_value=None), \
                mock.patch.object(me, "lowerDiverge", return_value=div), \
                mock.patch.object(me, "near_zone", return_value={"sr": {"price": 105.0}}):
            res = me.evaluateEntry(self._ctx(10800, bars), strategy)
            self.assertTrue(res["ok"])  # 背驰点 11000 与锚 10800 同 bar 3
            div_out = [{"res": "15", "point": {"time": 15000, "price": 105.0}}]
            with mock.patch.object(me, "lowerDiverge", return_value=div_out):
                res = me.evaluateEntry(self._ctx(10800, bars), strategy)
                self.assertFalse(res["ok"])
                self.assertIn("锚点所在大周期K线", res["reason"])

    def test_no_anchor_degrades_open(self):
        # 旧调用方/最小 fixture 不传 anchorPoint → 降级放行
        bars = [{"time": t} for t in TIMES60]
        strategy = {"key": "wait2Sell", "direction": "short", "label": "二卖", "pointCls": "强二卖"}
        ctx = self._ctx(10800, bars)
        ctx.pop("anchorPoint")
        div = [{"res": "15", "point": {"time": 15000, "price": 105.0}}]
        with mock.patch.object(me, "lastBiOk", return_value=True), \
                mock.patch.object(me, "strategyExtraOk", return_value=None), \
                mock.patch.object(me, "lowerDiverge", return_value=div), \
                mock.patch.object(me, "near_zone", return_value={"sr": {"price": 105.0}}):
            res = me.evaluateEntry(ctx, strategy)
            self.assertTrue(res["ok"])


if __name__ == "__main__":
    unittest.main()
