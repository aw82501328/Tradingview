# -*- coding: utf-8 -*-
"""锚点窗口闸（anchor_bar_window）单元测试（2026-10-18 用户规则；2026-10-21 重定义；
2026-10-22 trailing 重开）。

规则（重定义）：时间窗=锚点（买卖点）所在检测周期K线起 anchorWinBars 根（默认 2，
旧口径=1），当拍与背驰点所在K都须落窗、锚K之前仍拒；价距窗=触发价距窗口极值
（买侧最低/卖侧最高=锚点价与锚K开盘起细周期已收K极值取小/大）≤ anchorWinPts
（0=不限价距；bars=1+pts=0 复现旧单K规则）。无永久闩锁——锚随新高/新低漂移或
换新点时，每拍按当前锚现算，新锚所在K线自动重开窗。当下制（evaluateRealtimeEntries）
三重校验：触发拍所在K、当拍现价、背驰点（K线+价距）；确认制（evaluateEntry）只
约束背驰点（K线落窗+价距）。

trailing 重开（2026-10-22 anchorWinTrailOn 默认开）：窗口内创新高/新低时时间窗
锚K=窗口极值所在K（价距基准本就随极值漂移，时间窗此前只认锚点固定时间戳——
连续新高不可能产生新压低2卖锚，窗口卡死，9-8 12:00 案例）；极值仍落原锚K时
与旧口径逐位一致；False=2026-10-21 固定锚K口径（A/B 基准）。

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

# 卖侧 fixture：末笔 up 终点 10800@108.0 挂 2卖 strong → 2Sell（wantType=up 匹配）
BIS60_SELL = [
    bi("up", 0, 3600, 100, 110),
    bi("down", 3600, 7200, 110, 105),
    bi("up", 7200, 10800, 105, 108),
]
# 买侧镜像：末笔 down 终点 10800@102.0 挂 2买 strong → 2Buy（wantType=down 匹配）
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

# 细周期（3m）已收K fixture：锚K起点 10800 起（价距基准数据源）
# 卖侧反弹走高后回落：窗口最高 = max(锚价108, 各K high) = 120，末K收 108 → 距离 12
FINE_UP_PULLBACK = [
    {"time": 10800, "high": 110.0, "low": 106.0, "close": 109.0},
    {"time": 11100, "high": 113.0, "low": 108.5, "close": 112.5},
    {"time": 11400, "high": 116.0, "low": 112.0, "close": 115.5},
    {"time": 11700, "high": 120.0, "low": 114.0, "close": 119.0},
    {"time": 12000, "high": 118.0, "low": 108.0, "close": 108.0},
]
# 同上但末K只回落到 112 → 距离 8（放行）
FINE_UP_NEAR = FINE_UP_PULLBACK[:-1] + [
    {"time": 12000, "high": 120.0, "low": 111.0, "close": 112.0}]
# 买侧镜像：下探后反弹，窗口最低 = min(锚价102, 各K low) = 96，末K收 108.5 → 距离 12.5
FINE_DOWN_BOUNCE = [
    {"time": 10800, "high": 102.5, "low": 100.0, "close": 100.5},
    {"time": 11100, "high": 100.0, "low": 97.5, "close": 98.0},
    {"time": 11400, "high": 98.0, "low": 96.0, "close": 96.5},
    {"time": 11700, "high": 97.0, "low": 96.0, "close": 96.8},
    {"time": 12000, "high": 109.0, "low": 97.0, "close": 108.5},
]
# trailing fixture（9-8 12:00 形态）：锚 bar 3（10800@108，锚价即窗口极值），bar 5 才
# 创新高 115 → trailing 锚K=bar 5 窗口重开；固定锚K口径（bar 3 起 2 根）则已超窗
FINE_NEWHIGH_LATE = [
    {"time": 10800, "high": 107.5, "low": 105.0, "close": 106.5},   # bar 3（high<锚价）
    {"time": 14400, "high": 107.0, "low": 106.0, "close": 106.8},   # bar 4（无新高）
    {"time": 18000, "high": 115.0, "low": 108.0, "close": 114.0},   # bar 5 新高 115
    {"time": 18300, "high": 112.0, "low": 109.0, "close": 112.0},   # bar 5 回落收 112
]
# trailing 买侧镜像：锚 bar 3（10800@102），bar 5 创新低 95
FINE_NEWLOW_LATE = [
    {"time": 10800, "high": 104.0, "low": 102.5, "close": 103.0},   # bar 3（low>锚价）
    {"time": 14400, "high": 103.5, "low": 102.8, "close": 103.2},   # bar 4（无新低）
    {"time": 18000, "high": 101.0, "low": 95.0, "close": 95.5},     # bar 5 新低 95
    {"time": 18300, "high": 99.0, "low": 96.0, "close": 98.5},      # bar 5 反弹收 98.5
]
# trailing 再失效：新高只发生在 bar 4，bar 6 触发时距 trailing 锚（bar 4）已 2 根 → 超窗
FINE_EARLY_HIGH_THEN_NONE = [
    {"time": 10800, "high": 109.0, "low": 105.0, "close": 108.5},   # bar 3
    {"time": 14400, "high": 115.0, "low": 107.0, "close": 113.0},   # bar 4 新高 115
    {"time": 18000, "high": 114.0, "low": 108.0, "close": 110.0},   # bar 5 无新高
    {"time": 21600, "high": 113.0, "low": 109.0, "close": 111.0},   # bar 6 无新高
]


def _run(bis60, point, tCut, divTime, times=True, periodBars=None, divPrice=105.0):
    """mock 屏蔽背驰/支阻细节，只驱动窗口闸：返回 (信号列表, 拒绝记录列表)。
    periodBars 注入细周期已收K（价距基准）；divPrice=mock 背驰极值价。"""
    periodBis = {"240": bis60, "60": bis60, "15": bis60, "3": bis60}
    points = {"60": {"buy": [], "sell": []}}
    points["60"]["buy" if point["type"].endswith("买") else "sell"].append(point)
    rejects = []
    with mock.patch.object(me, "realtimeLowerDiverge",
                           return_value=[{"res": "15", "segStart": 7200,
                                          "point": {"time": divTime, "price": divPrice}}]), \
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
            periodBars=periodBars,
            on_reject=lambda gate, period, seg, skey, ctx, t:
                rejects.append({"gate": gate, "period": period, "skey": skey, "ctx": ctx}))
    return sigs, rejects


class TestRealtimeAnchorBarWindow(unittest.TestCase):
    """当下制：当拍与背驰点都须落在锚K起 N 根检测周期K内 + 价距在限内。"""

    SELL_PT = {"type": "2卖", "time": 10800, "price": 108.0, "strong": True}
    BUY_PT = {"type": "2买", "time": 10800, "price": 102.0, "strong": True}

    def test_same_bar_fires(self):
        # 锚 10800（bar 3）、触发拍与背驰点 11000（同 bar 3）→ 放行
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=11000, divTime=11000)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "2Sell")
        self.assertFalse(any(r["gate"] == "anchor_bar_window" for r in rejects))

    def test_second_bar_fires(self):
        # 2026-10-21 重定义核心场景（trade#3 型）：锚 bar 3、触发拍与背驰点 15000
        # （bar 4 = 锚K后第 2 根）→ 旧口径拒、新口径（2根K）放行
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=15000, divTime=15000)
        self.assertEqual(len(sigs), 1)
        self.assertFalse(any(r["gate"] == "anchor_bar_window" for r in rejects))

    def test_third_bar_rejected(self):
        # 锚 bar 3、触发拍 18600（bar 5，超 2 根K窗口）→ 主闸拒绝、不出信号
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
                    tCut=18600, fired=set(),
                    periodTimes={"60": TIMES60, "15": TIMES60, "3": TIMES60},
                    on_reject=lambda gate, period, seg, skey, ctx, t:
                        rejects.append({"gate": "gate", "ctx": ctx}))
        self.assertEqual(sigs, [])
        win = [r for r in rejects if r["ctx"].get("anchorTime") == 10800]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["ctx"]["nowBarTime"], 18000)

    def test_stale_divergence_point_rejected(self):
        # 次级保险：触发拍 11000 在窗内（bar 3），但背驰极值 9000（bar 2，锚K之前）
        # 是早前K线陈旧极值 → 同 gate 拒绝（ctx 带 markRes/divergeTime）
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=11000, divTime=9000)
        self.assertEqual(sigs, [])
        win = [r for r in rejects if r["gate"] == "anchor_bar_window"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["ctx"]["markRes"], "15")
        self.assertEqual(win[0]["ctx"]["divergeTime"], 9000)

    def test_stale_divergence_price_rejected(self):
        # 次级价距：拍与背驰点都在窗内（bar 3），但背驰极值价 96 距窗口最高 108
        # 超过 10 点（无细周期K → 基准=锚点价）→ 拒绝（ctx 带 price/winExt/dist）
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=11000, divTime=11000,
                             divPrice=96.0)
        self.assertEqual(sigs, [])
        win = [r for r in rejects if r["gate"] == "anchor_bar_window"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["ctx"]["price"], 96.0)
        self.assertEqual(win[0]["ctx"]["winExt"], 108.0)
        self.assertEqual(win[0]["ctx"]["dist"], 12.0)

    def test_buy_side_mirror(self):
        # 买侧镜像：锚 bar 3、触发拍 bar 5 → 拒；同 bar → 放行
        sigs, rejects = _run(BIS60_BUY, self.BUY_PT, tCut=18600, divTime=18600)
        self.assertEqual(sigs, [])
        self.assertTrue(any(r["gate"] == "anchor_bar_window" for r in rejects))
        sigs, rejects = _run(BIS60_BUY, self.BUY_PT, tCut=11000, divTime=11000)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "2Buy")

    def test_sell_pullback_from_high_rejected(self):
        # 主闸价距（卖侧）：窗口最高被反弹推到 120，当拍现价 108 已从高点回落 12 点
        # → 距离超限拒绝（防「已离极值太远还进场」）
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=12300, divTime=12300,
                             periodBars={"3": FINE_UP_PULLBACK}, divPrice=118.0)
        self.assertEqual(sigs, [])
        win = [r for r in rejects if r["gate"] == "anchor_bar_window"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["ctx"]["price"], 108.0)
        self.assertEqual(win[0]["ctx"]["winExt"], 120.0)
        self.assertEqual(win[0]["ctx"]["dist"], 12.0)

    def test_sell_pullback_within_pts_passes(self):
        # 同场景但末K收 112（距高点 8 点 ≤ 10）→ 放行
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=12300, divTime=12300,
                             periodBars={"3": FINE_UP_NEAR}, divPrice=118.0)
        self.assertEqual(len(sigs), 1)
        self.assertFalse(any(r["gate"] == "anchor_bar_window" for r in rejects))

    def test_buy_bounce_from_low_rejected(self):
        # 主闸价距（买侧镜像）：窗口最低 96，当拍现价 108.5 已反弹 12.5 点 → 拒绝
        sigs, rejects = _run(BIS60_BUY, self.BUY_PT, tCut=12300, divTime=12300,
                             periodBars={"3": FINE_DOWN_BOUNCE}, divPrice=98.0)
        self.assertEqual(sigs, [])
        win = [r for r in rejects if r["gate"] == "anchor_bar_window"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["ctx"]["winExt"], 96.0)
        self.assertEqual(win[0]["ctx"]["dist"], 12.5)

    def test_pts_zero_disables_price_gate(self):
        # anchorWinPts=0 → 不限价距只看时间窗：回落 12 点的场景放行
        with mock.patch.dict(me.CHAN_CFG, {"anchorWinPts": 0.0}):
            sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=12300, divTime=12300,
                                 periodBars={"3": FINE_UP_PULLBACK}, divPrice=105.0)
        self.assertEqual(len(sigs), 1)
        self.assertFalse(any(r["gate"] == "anchor_bar_window" for r in rejects))

    def test_old_rule_single_bar_zero_pts(self):
        # anchorWinBars=1 + anchorWinPts=0 → 完整复现旧单K规则：
        # 同 bar 放行、锚K后第 2 根（bar 4）拒绝
        with mock.patch.dict(me.CHAN_CFG, {"anchorWinBars": 1, "anchorWinPts": 0.0}):
            sigs, _ = _run(BIS60_SELL, self.SELL_PT, tCut=11000, divTime=11000)
            self.assertEqual(len(sigs), 1)
            sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=15000, divTime=15000)
        self.assertEqual(sigs, [])
        win = [r for r in rejects if r["gate"] == "anchor_bar_window"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["ctx"]["nowBarTime"], 14400)

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

    # ---- trailing 重开（2026-10-22 anchorWinTrailOn 默认开）----

    def test_trailing_new_high_reopens_window(self):
        # 9-8 12:00 形态：锚 bar 3、bar 5 才创新高 115 且触发同 bar——固定锚K口径
        # （bar 3 起 2 根）已超窗拒；trailing 锚K=极值所在 bar 5 → 窗口重开放行
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=18600, divTime=18300,
                             periodBars={"3": FINE_NEWHIGH_LATE}, divPrice=115.0)
        self.assertEqual(len(sigs), 1)
        self.assertFalse(any(r["gate"] == "anchor_bar_window" for r in rejects))

    def test_trailing_off_falls_back_fixed_anchor(self):
        # anchorWinTrailOn=False → 同场景回到 2026-10-21 固定锚K口径：超窗拒
        # （ctx 的 effAnchorTime=原锚K，A/B 基准用）
        with mock.patch.dict(me.CHAN_CFG, {"anchorWinTrailOn": False}):
            sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=18600, divTime=18300,
                                 periodBars={"3": FINE_NEWHIGH_LATE}, divPrice=115.0)
        self.assertEqual(sigs, [])
        win = [r for r in rejects if r["gate"] == "anchor_bar_window"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["ctx"]["anchorTime"], 10800)
        self.assertEqual(win[0]["ctx"]["nowBarTime"], 18000)
        self.assertEqual(win[0]["ctx"]["effAnchorTime"], 10800)

    def test_trailing_expires_again_after_no_new_extreme(self):
        # 新高只发生在 bar 4（trailing 锚K=bar 4），bar 6 触发距其 2 根 → 再次超窗拒
        sigs, rejects = _run(BIS60_SELL, self.SELL_PT, tCut=22200, divTime=21900,
                             periodBars={"3": FINE_EARLY_HIGH_THEN_NONE}, divPrice=114.0)
        self.assertEqual(sigs, [])
        win = [r for r in rejects if r["gate"] == "anchor_bar_window"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["ctx"]["effAnchorTime"], 14400)   # trailing 锚K=新高所在 bar 4
        self.assertEqual(win[0]["ctx"]["nowBarTime"], 21600)
        self.assertEqual(win[0]["ctx"]["extPrice"], 115.0)

    def test_trailing_buy_side_mirror(self):
        # 买侧镜像：锚 bar 3、bar 5 创新低 95 且触发同 bar → trailing 重开放行
        sigs, rejects = _run(BIS60_BUY, self.BUY_PT, tCut=18600, divTime=18300,
                             periodBars={"3": FINE_NEWLOW_LATE}, divPrice=95.0)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "2Buy")
        self.assertFalse(any(r["gate"] == "anchor_bar_window" for r in rejects))


class TestConfirmAnchorBarWindow(unittest.TestCase):
    """确认制（evaluateEntry）：只约束背驰点落窗+价距，不查评估拍。"""

    def _ctx(self, anchorTime, bars, anchorPrice=108.0):
        return {
            "res": "60",
            "bis": [
                bi("up", 0, 3600, 100, 110),
                bi("down", 3600, 7200, 110, 105),
                bi("up", 7200, 10800, 105, 108),
            ],
            "upperBis": None,
            "macdArr": [],
            "atr": 0,
            "barSec": 3600,
            "near": 30.0,
            "srLevels": [],
            "periodData": {"60": {"bis": [], "bars": bars, "macdArr": [], "atr": 0}},
            "anchorPoint": {"type": "2卖", "time": anchorTime, "price": anchorPrice,
                            "strong": True},
        }

    def _eval(self, ctx, div):
        strategy = {"key": "2Sell", "direction": "short", "label": "二卖",
                    "pointCls": "强二卖"}
        with mock.patch.object(me, "lastBiOk", return_value=True), \
                mock.patch.object(me, "strategyExtraOk", return_value=None), \
                mock.patch.object(me, "lowerDiverge", return_value=div), \
                mock.patch.object(me, "near_zone", return_value={"sr": {"price": 105.0}}):
            return me.evaluateEntry(ctx, strategy)

    def test_in_window_passes_out_of_window_rejected(self):
        bars = [{"time": t, "open": 105, "close": 106, "high": 107, "low": 104}
                for t in TIMES60]
        div = [{"res": "15", "point": {"time": 11000, "price": 105.0}}]
        res = self._eval(self._ctx(10800, bars), div)
        self.assertTrue(res["ok"])  # 背驰点 11000 与锚 10800 同 bar 3
        div_out = [{"res": "15", "point": {"time": 18600, "price": 105.0}}]
        res = self._eval(self._ctx(10800, bars), div_out)
        self.assertFalse(res["ok"])
        self.assertIn("锚点窗口", res["reason"])

    def test_diverge_next_bar_in_window(self):
        # 重定义：背驰点在锚K后第 2 根（bar 4）→ 确认制放行
        bars = [{"time": t, "open": 105, "close": 106, "high": 107, "low": 104}
                for t in TIMES60]
        div = [{"res": "15", "point": {"time": 15000, "price": 105.0}}]
        res = self._eval(self._ctx(10800, bars), div)
        self.assertTrue(res["ok"])

    def test_diverge_price_far_rejected(self):
        # 价距：背驰点 96 距窗口最高 108（各K high=107 不及锚价）超 10 点 → 拒
        bars = [{"time": t, "open": 105, "close": 106, "high": 107, "low": 104}
                for t in TIMES60]
        div = [{"res": "15", "point": {"time": 11000, "price": 96.0}}]
        res = self._eval(self._ctx(10800, bars), div)
        self.assertFalse(res["ok"])
        self.assertIn("锚点窗口", res["reason"])

    def test_window_extreme_includes_bar_highs(self):
        # 窗口极值含检测周期K线高低：锚 bar 3 的 high=120 抬高基准 → 背驰点 105
        # 距 120 达 15 点 → 拒（防陈旧极值悬空在远低于窗口高点处）
        bars = [{"time": t, "open": 105, "close": 106, "high": 107, "low": 104}
                for t in TIMES60]
        bars[3]["high"] = 120.0
        div = [{"res": "15", "point": {"time": 11000, "price": 105.0}}]
        res = self._eval(self._ctx(10800, bars), div)
        self.assertFalse(res["ok"])
        self.assertIn("锚点窗口", res["reason"])

    def test_confirm_trailing_new_high_reopens(self):
        # trailing（2026-10-22）：锚 bar 3（锚价 108 高于各K high），bar 5 冲新高 120、
        # 背驰点同 bar → trailing 锚K=极值所在 bar 5 落窗放行；False=固定锚K超窗拒
        bars = [{"time": t, "open": 105, "close": 106, "high": 107, "low": 104}
                for t in TIMES60]
        bars[5]["high"] = 120.0
        div = [{"res": "15", "point": {"time": 18900, "price": 120.0}}]
        res = self._eval(self._ctx(10800, bars), div)
        self.assertTrue(res["ok"])
        with mock.patch.dict(me.CHAN_CFG, {"anchorWinTrailOn": False}):
            res = self._eval(self._ctx(10800, bars), div)
        self.assertFalse(res["ok"])
        self.assertIn("锚点窗口", res["reason"])

    def test_no_anchor_degrades_open(self):
        # 旧调用方/最小 fixture 不传 anchorPoint → 降级放行
        bars = [{"time": t} for t in TIMES60]
        ctx = self._ctx(10800, bars)
        ctx.pop("anchorPoint")
        div = [{"res": "15", "point": {"time": 15000, "price": 105.0}}]
        res = self._eval(ctx, div)
        self.assertTrue(res["ok"])


if __name__ == "__main__":
    unittest.main()
