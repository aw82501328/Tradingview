# -*- coding: utf-8 -*-
"""进场买卖点类别白名单（CHAN_CFG.pointClasses，2026-10-24 参数中心 entry 桶）单元测试。
同日三次口径：三类/四类族也拆强弱（3s/3w/3xs/3xw/4s/4w/4xs/4xw）+策略键去 wait
前缀+弱 3/4 走 Bear 键（3BuyBear/3SellBear/BuyBear/SellBear）。

口径（用户拍板）：类别 13 项·买卖对称（1/2s/2w/2xs/2xw/3s/3w/3xs/3xw/4s/4w/4xs/4xw）；
强弱全族拆分（strong 字段，真值=强、False/None 按弱，判定与 pointStrategyOf 同判）——
二类族=1买后反弹过/未过前高；3/4族=点后同向腿背驰=弱（chan_core
_mark_third_fourth_strong，判不出即强）；
浅过滤——锚点选取（side_candidates）不变，最新锚点类别未勾→本拍跳过不开单、
不回溯更早的点（最新点代表当前市场状态，被未勾类型占住时观望）；
2026-10-25 起默认 "1,2s,2xs,3s,4s"（一类/强二类/强类二/强三类/强四类；
此前默认 13 项全选）。
旧值 "2"/"2x"/"3"/"3x"/"4"/"4x"（同日上午 7 项与二次 9 项口径）运行期别名展开。

覆盖：
  - _selected_classes：默认全选/合法子集解析/旧值别名/空串与全无效 fail-open=全选/
    换串重算
  - _point_class_of：二类族强弱分档（strong 真值=强、缺失/None=弱）、非2族原样
  - 当下制 evaluateRealtimeEntries：默认放行（基线不变）、未勾类别拒绝落
    point_class_not_selected（bt_query「为何没开单」可查）、浅过滤不回溯更早的
    已勾点、强弱分闸（强 2卖→2Sell 只认 2s、弱 2卖→2SellBear 只认 2w）、
    买侧镜像
  - 确认制 compute_entries：同一类别闸——未勾类别时 evaluateEntry 根本不被调用

运行：python -m unittest py_chain.test_point_classes -v
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

# 卖侧 fixture（同 test_anchor_bar_window）：末笔 up 终点 10800@108.0 挂 2卖 strong
BIS60_SELL = [
    bi("up", 0, 3600, 100, 110),
    bi("down", 3600, 7200, 110, 105),
    bi("up", 7200, 10800, 105, 108),
]
# 买侧镜像：末笔 down 终点 10800@102.0 挂 2买 strong
BIS60_BUY = [
    bi("down", 0, 3600, 110, 100),
    bi("up", 3600, 7200, 100, 105),
    bi("down", 7200, 10800, 105, 102),
]

PT_2SELL = {"type": "2卖", "time": 10800, "price": 108.0, "strong": True}
PT_2BUY = {"type": "2买", "time": 10800, "price": 102.0, "strong": True}
# 弱 2卖（strong 缺失=未定型按弱 → 2SellBear；BIS60_SELL 末笔 up 同样匹配 wantType）
PT_2SELL_WEAK = {"type": "2卖", "time": 10800, "price": 108.0}
# 更早的 3卖：落在倒数第 2 笔终点 7200@105.0 上（浅过滤验证用——锚点扫描从末笔
# 向前，末笔终点有 2卖 时锚=2卖，不应回溯到这个更早的已勾点）
PT_3SELL_OLD = {"type": "3卖", "time": 7200, "price": 105.0}

ALL = frozenset(("1", "2s", "2w", "2xs", "2xw",
                 "3s", "3w", "3xs", "3xw", "4s", "4w", "4xs", "4xw"))


def _cfg(point_classes):
    """临时覆盖 CHAN_CFG.pointClasses 的上下文管理器；None=保持现状。"""
    saved = me.CHAN_CFG.get("pointClasses")

    class _Ctx(object):
        def __enter__(self):
            if point_classes is not None:
                me.CHAN_CFG["pointClasses"] = point_classes
            return self

        def __exit__(self, *a):
            if saved is None:
                me.CHAN_CFG.pop("pointClasses", None)
            else:
                me.CHAN_CFG["pointClasses"] = saved
            return False
    return _Ctx()


def _run_rt(bis, sell_pts, buy_pts, point_classes=None, tCut=11000, divTime=11000):
    """当下制（evaluateRealtimeEntries）：mock 屏蔽背驰/支阻细节，只驱动类别闸。
    返回 (信号列表, 拒绝记录列表)。"""
    periodBis = {"240": bis, "60": bis, "15": bis, "3": bis}
    points = {"60": {"buy": list(buy_pts), "sell": list(sell_pts)}}
    rejects = []
    with _cfg(point_classes), \
            mock.patch.object(me, "realtimeLowerDiverge",
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
            periodTimes={"60": TIMES60, "15": TIMES60, "3": TIMES60},
            on_reject=lambda gate, period, seg, skey, ctx, t:
                rejects.append({"gate": gate, "period": period, "skey": skey, "ctx": ctx}))
    return sigs, rejects


class TestSelectedClasses(unittest.TestCase):
    def test_default_all_selected(self):
        # 2026-10-25 起默认五项（一类/强二类/强类二/强三类/强四类），此前默认 13 项全选
        with _cfg(None):
            self.assertEqual(me._selected_classes(),
                             frozenset(("1", "2s", "2xs", "3s", "4s")))

    def test_parse_subset(self):
        with _cfg("1,3s"):
            self.assertEqual(me._selected_classes(), frozenset(("1", "3s")))

    def test_legacy_alias_expansion(self):
        # 旧值 "2"/"2x"（同日上午 7 项口径）与 "3"/"3x"/"4"/"4x"（同日二次 9 项口径）
        # 运行期展开：已导出 chan_cfg json/回测快照零迁移可用
        with _cfg("2,3"):
            self.assertEqual(me._selected_classes(), frozenset(("2s", "2w", "3s", "3w")))
        with _cfg("1,2x"):
            self.assertEqual(me._selected_classes(), frozenset(("1", "2xs", "2xw")))
        with _cfg("3,4x"):
            self.assertEqual(me._selected_classes(),
                             frozenset(("3s", "3w", "4xs", "4xw")))

    def test_fail_open_on_empty_or_invalid(self):
        # 手传 --chan-cfg 坏值不弄丢全部信号：空串/全无效 → 全选
        with _cfg(""):
            self.assertEqual(me._selected_classes(), ALL)
        with _cfg("9,zz,2"):
            # 注意：混合串只忽略无效项，"2" 仍按别名展开（非全无效 → 不 fail-open）
            self.assertEqual(me._selected_classes(), frozenset(("2s", "2w")))
        with _cfg("9,zz"):
            self.assertEqual(me._selected_classes(), ALL)

    def test_memo_recomputes_on_new_string(self):
        with _cfg("1"):
            self.assertEqual(me._selected_classes(), frozenset(("1",)))
        with _cfg("2s"):
            self.assertEqual(me._selected_classes(), frozenset(("2s",)))

    def test_point_class_of_strong_weak_split(self):
        # 二类族强弱分档：与 pointStrategyOf 同判（真值=强、缺失/None=弱）
        self.assertEqual(me._point_class_of({"type": "2买", "strong": True}), "2s")
        self.assertEqual(me._point_class_of({"type": "2买", "strong": False}), "2w")
        self.assertEqual(me._point_class_of({"type": "2买"}), "2w")            # 未定型按弱
        self.assertEqual(me._point_class_of({"type": "2买", "strong": None}), "2w")
        self.assertEqual(me._point_class_of({"type": "类2卖", "strong": 1}), "2xs")
        self.assertEqual(me._point_class_of({"type": "类2卖"}), "2xw")
        # 3/4 族同拆（2026-10-24 三次口径：strong 由 _mark_third_fourth_strong 写入，
        # 缺失/None 同样按弱——与 pointStrategyOf 同判）
        self.assertEqual(me._point_class_of({"type": "3买", "strong": True}), "3s")
        self.assertEqual(me._point_class_of({"type": "3买", "strong": False}), "3w")
        self.assertEqual(me._point_class_of({"type": "3买"}), "3w")
        self.assertEqual(me._point_class_of({"type": "类3买", "strong": 1}), "3xs")
        self.assertEqual(me._point_class_of({"type": "4卖", "strong": False}), "4w")
        self.assertEqual(me._point_class_of({"type": "类4卖", "strong": True}), "4xs")
        self.assertEqual(me._point_class_of({"type": "类4卖"}), "4xw")
        # 1类不拆；未知类型 None（闸放行，由 no_strategy_map 兜底）
        self.assertEqual(me._point_class_of({"type": "1买"}), "1")
        self.assertIsNone(me._point_class_of({"type": "类4"}))


class TestRealtimePointClassGate(unittest.TestCase):
    """当下制：最新锚点类别未勾 → 跳过 + point_class_not_selected 拒绝记录。"""

    def test_default_all_fires_baseline(self):
        # 默认全选=2026-10-24 前口径：2卖 strong fixture 照常出 2Sell
        sigs, _rejects = _run_rt(BIS60_SELL, [dict(PT_2SELL)], [])
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "2Sell")

    def test_unselected_class_skips_with_reject_log(self):
        sigs, rejects = _run_rt(BIS60_SELL, [dict(PT_2SELL)], [], point_classes="1,4s")
        self.assertEqual(sigs, [])
        hit = [r for r in rejects if r["gate"] == "point_class_not_selected"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["ctx"]["pointType"], "2卖")
        self.assertEqual(hit[0]["ctx"]["pointClass"], "2s")   # strong=True → 强二类
        self.assertEqual(hit[0]["ctx"]["classes"], "1,4s")

    def test_strong_weak_split_gate(self):
        # 强 2卖（2Sell）只认 2s：不勾 2s → 拒；勾 2s → 放行
        sigs, rejects = _run_rt(BIS60_SELL, [dict(PT_2SELL)], [], point_classes="2w,3")
        self.assertEqual(sigs, [])
        hit = [r for r in rejects if r["gate"] == "point_class_not_selected"]
        self.assertEqual(hit[0]["ctx"]["pointClass"], "2s")
        sigs2, _ = _run_rt(BIS60_SELL, [dict(PT_2SELL)], [], point_classes="2s")
        self.assertEqual(len(sigs2), 1)
        self.assertEqual(sigs2[0]["strategyKey"], "2Sell")
        # 弱 2卖（strong 缺失=未定型按弱 → 2SellBear）只认 2w
        sigs3, rejects3 = _run_rt(BIS60_SELL, [dict(PT_2SELL_WEAK)], [], point_classes="2s,3")
        self.assertEqual(sigs3, [])
        hit3 = [r for r in rejects3 if r["gate"] == "point_class_not_selected"]
        self.assertEqual(hit3[0]["ctx"]["pointClass"], "2w")
        sigs4, _ = _run_rt(BIS60_SELL, [dict(PT_2SELL_WEAK)], [], point_classes="2w")
        self.assertEqual(len(sigs4), 1)
        self.assertEqual(sigs4[0]["strategyKey"], "2SellBear")

    def test_third_fourth_strong_weak_gate(self):
        # 3/4 族强弱闸（2026-10-24 三次口径）：弱 3卖（strong=False→3SellBear）只认 3w；
        # 强 3卖（strong=True→3Sell）只认 3s；弱 类4买（BuyBear）只认 4xw
        pt_3sell_weak = {"type": "3卖", "time": 10800, "price": 108.0, "strong": False}
        sigs, rejects = _run_rt(BIS60_SELL, [dict(pt_3sell_weak)], [], point_classes="3s,4")
        self.assertEqual(sigs, [])
        hit = [r for r in rejects if r["gate"] == "point_class_not_selected"]
        self.assertEqual(hit[0]["ctx"]["pointClass"], "3w")
        sigs2, _ = _run_rt(BIS60_SELL, [dict(pt_3sell_weak)], [], point_classes="3w")
        self.assertEqual(len(sigs2), 1)
        self.assertEqual(sigs2[0]["strategyKey"], "3SellBear")
        pt_3sell_strong = {"type": "3卖", "time": 10800, "price": 108.0, "strong": True}
        sigs3, rejects3 = _run_rt(BIS60_SELL, [dict(pt_3sell_strong)], [], point_classes="3w")
        self.assertEqual(sigs3, [])
        hit3 = [r for r in rejects3 if r["gate"] == "point_class_not_selected"]
        self.assertEqual(hit3[0]["ctx"]["pointClass"], "3s")
        sigs4, _ = _run_rt(BIS60_SELL, [dict(pt_3sell_strong)], [], point_classes="3s")
        self.assertEqual(len(sigs4), 1)
        self.assertEqual(sigs4[0]["strategyKey"], "3Sell")
        # 买侧 4族：弱 类4买 → BuyBear，只认 4xw；旧值 "4x" 别名同放行
        pt_4xbuy_weak = {"type": "类4买", "time": 10800, "price": 102.0, "strong": False}
        sigs5, rejects5 = _run_rt(BIS60_BUY, [], [dict(pt_4xbuy_weak)], point_classes="4xs")
        self.assertEqual(sigs5, [])
        hit5 = [r for r in rejects5 if r["gate"] == "point_class_not_selected"]
        self.assertEqual(hit5[0]["ctx"]["pointClass"], "4xw")
        sigs6, _ = _run_rt(BIS60_BUY, [], [dict(pt_4xbuy_weak)], point_classes="4xw")
        self.assertEqual(len(sigs6), 1)
        self.assertEqual(sigs6[0]["strategyKey"], "BuyBear")
        sigs7, _ = _run_rt(BIS60_BUY, [], [dict(pt_4xbuy_weak)], point_classes="4x")
        self.assertEqual(len(sigs7), 1)

    def test_shallow_no_backtrack_to_older_selected_point(self):
        # 浅过滤核心语义：末笔终点最新点是 2卖（类别 2s 未勾），更早 7200 有已勾的
        # 3卖——锚点仍取 2卖、跳过，不回溯改用 3卖 → 全程零信号
        sigs, rejects = _run_rt(BIS60_SELL, [dict(PT_3SELL_OLD), dict(PT_2SELL)], [],
                                point_classes="3")
        self.assertEqual(sigs, [])
        hit = [r for r in rejects if r["gate"] == "point_class_not_selected"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["ctx"]["pointType"], "2卖")
        # 对照：勾「强二类」同一 fixture 放行（证明拦住的是类别闸而非 fixture 本身）；
        # 旧值 "2" 别名含 2s 同样放行
        sigs2, _ = _run_rt(BIS60_SELL, [dict(PT_3SELL_OLD), dict(PT_2SELL)], [],
                           point_classes="2s")
        self.assertEqual(len(sigs2), 1)
        self.assertEqual(sigs2[0]["strategyKey"], "2Sell")
        sigs3, _ = _run_rt(BIS60_SELL, [dict(PT_3SELL_OLD), dict(PT_2SELL)], [],
                           point_classes="2")
        self.assertEqual(len(sigs3), 1)

    def test_buy_side_mirror(self):
        sigs, rejects = _run_rt(BIS60_BUY, [], [dict(PT_2BUY)], point_classes="1,3")
        self.assertEqual(sigs, [])
        hit = [r for r in rejects if r["gate"] == "point_class_not_selected"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["ctx"]["pointType"], "2买")


class TestComputeEntriesPointClassGate(unittest.TestCase):
    """确认制：类别未勾时 evaluateEntry 根本不被调用（闸在策略评估之前）。"""

    BARS60 = [{"time": t, "open": 100.0, "close": 105.0,
               "high": 106.0, "low": 99.0} for t in TIMES60]

    def _run_ce(self, sell_pts, point_classes):
        periodBis = {"60": BIS60_SELL}
        points = {"60": {"buy": [], "sell": list(sell_pts)}}
        rejects = []
        with _cfg(point_classes), \
                mock.patch.object(me, "structurePeriods",
                                  side_effect=lambda pb, bb, res_chain=None: pb), \
                mock.patch.object(me, "evaluateEntry",
                                  return_value={"ok": False, "reason": "unit_fixture"}) as eev:
            out = me.compute_entries(
                periodBis, {"60": self.BARS60}, points,
                [{"price": 105.0, "level": "60"}], ["60"],
                trend_res="", range_res="",
                on_reject=lambda gate, period, seg, skey, ctx, t:
                    rejects.append({"gate": gate, "skey": skey, "ctx": ctx}))
        return out, eev, rejects

    def test_selected_class_proceeds_to_evaluate(self):
        out, eev, rejects = self._run_ce([dict(PT_2SELL)], None)
        eev.assert_called_once()   # 类别勾选（默认全选）→ 进入策略评估
        self.assertEqual(out, {})  # fixture 故意让评估不过 → 无信号
        self.assertTrue(any(r["gate"] == "eval_reason" for r in rejects))

    def test_unselected_class_never_evaluates(self):
        out, eev, rejects = self._run_ce([dict(PT_2SELL)], "1,3")
        eev.assert_not_called()    # 类别未勾 → 策略评估根本不发生
        self.assertEqual(out, {})
        self.assertEqual(rejects, [])  # 确认制路径静默跳过（同 no_strategy_map 惯例）


if __name__ == "__main__":
    unittest.main(verbosity=2)
