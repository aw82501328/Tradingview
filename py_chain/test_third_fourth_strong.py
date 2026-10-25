# -*- coding: utf-8 -*-
"""3/4 类族强弱标注测试（2026-10-24 用户口径：点后同向腿背驰=弱）。

口径：弱3=2类点后首根上推腿(R2)对1类点后首根反弹腿(R1)顶背驰；弱4=3类点后
首根上推腿(R3)对2类点后首根上推腿(R2)顶背驰（卖侧镜像=点后首根下推腿底背驰）；
isBiDiverge 三判据（面积[时长可比时]/DIF/极值柱）本周期 macd；前点=同侧更早最近点
（3族取(1类,2族含类2)、4族取(2族,3族含类3)）；前点/腿缺失或 macd 空=判不出即强。

JS 镜像用例在 .cursor/skills/chan-core/scripts/chan_core.test.js
「3/4族强弱 markThirdFourthStrong（2026-10-24）」describe（成对镜像）。

运行：python -m unittest py_chain.test_third_fourth_strong -v
"""

import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")

import unittest

from py_chain.chan_core import _mark_third_fourth_strong, _leg_after_point
from py_chain import mark_entry as me


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice,
            "span": abs(endPrice - startPrice)}


def m(t, macd, dif):
    return {"time": t, "macd": macd, "dif": dif}


BAR = 3600  # 1h 检测周期

# 买侧基础笔结构：D0 隐含，R1=首根反弹腿，D1 回调到 2买，R2 上推，D2 回踩到 3买，
# R3 再上推，D3 回踩到 4买
BIS_BUY = [
    bi("up", 0, BAR, 100.0, 110.0),          # R1（1买@0 后首根上推腿）
    bi("down", BAR, 2 * BAR, 110.0, 105.0),  # D1（终点=2买@3600）
    bi("up", 2 * BAR, 3 * BAR, 105.0, 112.0),   # R2（2买后首根上推腿）
    bi("down", 3 * BAR, 4 * BAR, 112.0, 108.0),  # D2（终点=3买@10800）
    bi("up", 4 * BAR, 5 * BAR, 108.0, 114.0),    # R3（3买后首根上推腿）
    bi("down", 5 * BAR, 6 * BAR, 114.0, 110.0),  # D3（终点=4买@18000）
]
PTS_BUY = [
    {"type": "1买", "time": 0, "price": 100.0},
    {"type": "2买", "time": 2 * BAR, "price": 105.0},
    {"type": "3买", "time": 4 * BAR, "price": 108.0},
    {"type": "4买", "time": 6 * BAR, "price": 110.0},
]


def _pts(types_times):
    """按 (type, time) 子集重建点表（不共享可变 fixture）。"""
    return [{"type": t, "time": tt, "price": 100.0} for t, tt in types_times]


class TestMarkThirdFourthStrong(unittest.TestCase):

    def test_weak3_diverging_advance(self):
        # R1 红柱面积 8 vs R2 全零：面积/DIF/极值柱全衰竭 → 弱3；
        # R3 对 R2 全零对全零无衰竭 → 4买 强（两判独立）
        macd = [m(0, 8.0, 5.0)] + [m(t, 0.0, 0.0) for t in range(BAR, 7 * BAR, BAR)]
        pts = _pts([("1买", 0), ("2买", 2 * BAR), ("3买", 4 * BAR), ("4买", 6 * BAR)])
        _mark_third_fourth_strong(pts, BIS_BUY, macd, BAR, False)
        self.assertIs(pts[2]["strong"], False)   # 弱3
        self.assertIs(pts[3]["strong"], True)    # 强4
        self.assertNotIn("strong", pts[0])       # 一类不标
        self.assertNotIn("strong", pts[1])       # 二类族强弱归 _second_family_v2

    def test_strong3_no_divergence(self):
        # R2 红柱 10/DIF 6 全面强于 R1 的 8/5 → 强3；R3(全零) 对 R2(10/6) 衰竭 → 弱4
        macd = [m(0, 8.0, 5.0), m(2 * BAR, 10.0, 6.0)] + \
               [m(t, 0.0, 0.0) for t in (BAR, 3 * BAR, 4 * BAR, 5 * BAR, 6 * BAR)]
        pts = _pts([("1买", 0), ("2买", 2 * BAR), ("3买", 4 * BAR), ("4买", 6 * BAR)])
        _mark_third_fourth_strong(pts, BIS_BUY, macd, BAR, False)
        self.assertIs(pts[2]["strong"], True)    # 强3
        self.assertIs(pts[3]["strong"], False)   # 弱4（4族取(2族,3族)对=R3 vs R2）

    def test_duration_gate_blocks_area_criterion(self):
        # R2 时长 5×R1 → 面积项不计（时长闸），DIF/极值柱未衰竭 → 强3；
        # 对照：等时长同柱 → 面积项生效（7<8）→ 弱3
        r1 = [m(0, 4.0, 5.0), m(BAR, 4.0, 5.0)]                      # R1: area 8, max 4, dif 5
        long_bis = [BIS_BUY[0], BIS_BUY[1],
                    bi("up", 2 * BAR, 7 * BAR, 105.0, 112.0)]        # R2 拉长到 5 根
        tail = [m(t, 0.0, 0.0) for t in range(2 * BAR, 8 * BAR, BAR)]
        pts = _pts([("1买", 0), ("2买", 2 * BAR), ("3买", 8 * BAR)])
        _mark_third_fourth_strong(pts, long_bis, r1 + [m(2 * BAR, 7.0, 5.0)] + tail[1:],
                                  BAR, False)
        self.assertIs(pts[2]["strong"], True)     # 面积 7<8 被时长闸弃用 → 强
        _mark_third_fourth_strong(pts, BIS_BUY, r1 + [m(2 * BAR, 7.0, 5.0)] +
                                  [m(t, 0.0, 0.0) for t in (3 * BAR, 4 * BAR)], BAR, False)
        self.assertIs(pts[2]["strong"], False)    # 等时长 → 面积项生效 → 弱

    def test_missing_pred_or_leg_means_strong(self):
        macd = [m(0, 8.0, 5.0), m(2 * BAR, 0.0, 0.0), m(3 * BAR, 0.0, 0.0)]
        # 缺 1类前点 → 3族凑不齐两点 → 强
        pts = _pts([("2买", 2 * BAR), ("3买", 4 * BAR)])
        _mark_third_fourth_strong(pts, BIS_BUY, macd, BAR, False)
        self.assertIs(pts[1]["strong"], True)
        # 4族缺 3族前点 → 强
        pts = _pts([("2买", 2 * BAR), ("4买", 6 * BAR)])
        _mark_third_fourth_strong(pts, BIS_BUY, macd, BAR, False)
        self.assertIs(pts[1]["strong"], True)
        # 前点晚于全部笔（腿匹配不上）→ 强
        pts = _pts([("1买", 100 * BAR), ("2买", 101 * BAR), ("3买", 102 * BAR)])
        _mark_third_fourth_strong(pts, BIS_BUY, macd, BAR, False)
        self.assertIs(pts[2]["strong"], True)
        # macd 空 → isBiDiverge False → 强
        pts = _pts([("1买", 0), ("2买", 2 * BAR), ("3买", 4 * BAR)])
        _mark_third_fourth_strong(pts, BIS_BUY, [], BAR, False)
        self.assertIs(pts[2]["strong"], True)

    def test_latest_predecessors_used(self):
        # 前点取「最近」：更早的 1买/2买 不参与（类2买@2.5BAR 比 2买@2BAR 新 → 3买
        # 的二前点=类2买；其腿=R2 仍同根 → 结果同弱3，此处主要验证不取更早点）
        macd = [m(0, 8.0, 5.0)] + [m(t, 0.0, 0.0) for t in range(BAR, 7 * BAR, BAR)]
        pts = _pts([("1买", 0), ("1买", BAR), ("2买", 2 * BAR), ("类2买", 5 * BAR // 2),
                    ("3买", 4 * BAR)])
        _mark_third_fourth_strong(pts, BIS_BUY, macd, BAR, False)
        self.assertIs(pts[4]["strong"], False)    # R2(零) vs R1(8) 背驰 → 弱3

    def test_class3_and_class4_also_marked(self):
        # 类3/类4 同为 3/4 族主体，全带 strong
        macd = [m(0, 8.0, 5.0)] + [m(t, 0.0, 0.0) for t in range(BAR, 7 * BAR, BAR)]
        pts = _pts([("1买", 0), ("2买", 2 * BAR), ("类3买", 4 * BAR), ("类4买", 6 * BAR)])
        _mark_third_fourth_strong(pts, BIS_BUY, macd, BAR, False)
        self.assertIs(pts[2]["strong"], False)
        self.assertIs(pts[3]["strong"], True)

    def test_point_time_tolerance_one_bar(self):
        # 1类投影时间与笔起点差一根K内（300s 偏移）→ 容差内匹配 R1 → 弱3 照判
        macd = [m(0, 8.0, 5.0)] + [m(t, 0.0, 0.0) for t in range(BAR, 7 * BAR, BAR)]
        pts = _pts([("1买", 300), ("2买", 2 * BAR), ("3买", 4 * BAR)])
        _mark_third_fourth_strong(pts, BIS_BUY, macd, BAR, False)
        self.assertIs(pts[2]["strong"], False)

    def test_leg_after_point_helper(self):
        self.assertIs(_leg_after_point(BIS_BUY, 0, "up", BAR), BIS_BUY[0])
        self.assertIs(_leg_after_point(BIS_BUY, 2 * BAR, "up", BAR), BIS_BUY[2])
        # 无该方向后续笔 → None
        self.assertIsNone(_leg_after_point(BIS_BUY, 7 * BAR, "up", BAR))
        self.assertIsNone(_leg_after_point([], 0, "up", BAR))

    def test_sell_side_mirror(self):
        # 卖侧镜像：点后首根下推腿底背驰（绿柱面积衰竭）→ 弱3卖
        bis = [
            bi("down", 0, BAR, 110.0, 100.0),       # L1（1卖@0 后首根下推腿）
            bi("up", BAR, 2 * BAR, 100.0, 105.0),   # 反弹（终点=2卖@3600）
            bi("down", 2 * BAR, 3 * BAR, 105.0, 98.0),   # L2（2卖后首根下推腿）
            bi("up", 3 * BAR, 4 * BAR, 98.0, 104.0),    # 反抽（终点=3卖@10800）
        ]
        macd = [m(0, -8.0, -5.0), m(BAR, 0.0, 0.0), m(2 * BAR, -7.0, -4.0),
                m(3 * BAR, 0.0, 0.0)]
        pts = _pts([("1卖", 0), ("2卖", 2 * BAR), ("3卖", 4 * BAR)])
        _mark_third_fourth_strong(pts, bis, macd, BAR, True)
        self.assertIs(pts[2]["strong"], False)   # 绿面积 7<8 → 底背驰 → 弱3卖
        # 无背驰（L2 更强）→ 强3卖
        macd2 = [m(0, -8.0, -5.0), m(BAR, 0.0, 0.0), m(2 * BAR, -9.0, -6.0),
                 m(3 * BAR, 0.0, 0.0)]
        pts2 = _pts([("1卖", 0), ("2卖", 2 * BAR), ("3卖", 4 * BAR)])
        _mark_third_fourth_strong(pts2, bis, macd2, BAR, True)
        self.assertIs(pts2[2]["strong"], True)


class TestStrategyRouting(unittest.TestCase):
    """强弱 → 策略键路由（2026-10-24：弱走同位 Bear 键，全部键无 wait 前缀）。"""

    def test_third_fourth_routing(self):
        self.assertEqual(me.pointStrategyOf({"type": "3买", "strong": True})["key"], "3Buy")
        self.assertEqual(me.pointStrategyOf({"type": "3买", "strong": False})["key"], "3BuyBear")
        self.assertEqual(me.pointStrategyOf({"type": "3卖", "strong": True})["key"], "3Sell")
        self.assertEqual(me.pointStrategyOf({"type": "3卖"})["key"], "3SellBear")
        self.assertEqual(me.pointStrategyOf({"type": "类3买", "strong": True})["key"], "Buy")
        self.assertEqual(me.pointStrategyOf({"type": "类3买", "strong": False})["key"], "BuyBear")
        self.assertEqual(me.pointStrategyOf({"type": "4买", "strong": True})["key"], "Buy")
        self.assertEqual(me.pointStrategyOf({"type": "4买", "strong": False})["key"], "BuyBear")
        self.assertEqual(me.pointStrategyOf({"type": "类4卖", "strong": False})["key"], "SellBear")
        # 旧键名彻底退役（wait 前缀全库移除）
        for t_ in ("3买", "3卖", "类3买", "4买", "类4买", "类3卖", "4卖", "类4卖"):
            for sv in (True, False):
                self.assertFalse(me.pointStrategyOf(
                    {"type": t_, "strong": sv})["key"].startswith("wait"))

    def test_point_cls_labels(self):
        # journal/标记锚点标签：强三买/弱三买/强类3买/强四买/弱类4卖…
        self.assertEqual(me.point_cls_of({"type": "3买", "strong": True}), "强三买")
        self.assertEqual(me.point_cls_of({"type": "3买", "strong": False}), "弱三买")
        self.assertEqual(me.point_cls_of({"type": "类3买", "strong": False}), "弱类3买")
        self.assertEqual(me.point_cls_of({"type": "4买", "strong": True}), "强四买")
        self.assertEqual(me.point_cls_of({"type": "类4卖", "strong": False}), "弱类4卖")
        self.assertIsNone(me.point_cls_of({"type": "1买"}))

    def test_bear_keys_not_in_key_sets(self):
        # 新 Bear 键不进顺势/跟踪参照键集（照抄 2BuyBear 先例）
        for k in ("3BuyBear", "3SellBear", "BuyBear", "SellBear", "2BuyBear", "2SellBear"):
            self.assertNotIn(k, me.TREND_STRATEGY_KEYS)
            self.assertNotIn(k, me.EXIT_TRAIL_REF_KEYS)
        for k in ("3Buy", "Buy", "3Sell", "Sell"):
            self.assertIn(k, me.TREND_STRATEGY_KEYS)
            self.assertIn(k, me.EXIT_TRAIL_REF_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
