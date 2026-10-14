# -*- coding: utf-8 -*-
"""同笔端点不标、未成笔尾段多空都标（2026-10-13，与 JS chan_core.test.js 对拍）。

  - 同笔：该上级笔两个端点在本级别都不标 1买/1卖；
  - 非同笔下跌笔终点仍是 1买，不看本级背驰开关；
  - 大级别未成笔尾段内，小级别创新低/创新高的一类多空都标；
  - isSameAsUpperBi 返回值契约：命中笔对象（同引用）/ None；
  - 落点兜底：上级端点价在本级无同价顶点时，一类点落到窗口内该方向
    最极端的本级顶点（必须落在小级别），仅窗口内无任何顶点才保持大级别时刻。

运行：python -m unittest py_chain.test_same_bi_mark -v（仓库根、逐模块，不 discover）
"""

import unittest

import py_chain.chan_core as cc


def b(ty, t0, t1, p0, p1, span):
    return {"type": ty, "startTime": t0, "endTime": t1,
            "startPrice": p0, "endPrice": p1, "span": span}


# ---------- 买侧（镜像 8-17~8-20 实例，barSec=900 与 JS 测试同口径） ----------
BI_BUY = [
    b("down", 1000, 2000, 4440.0, 4390.0, 60.0),
    b("up", 2000, 3000, 4390.0, 4436.0, 46.0),
    b("down", 3000, 4000, 4436.0, 4324.68, 111.32),   # 同笔下跌段（k=1 评估）
    b("up", 4000, 5000, 4324.68, 4524.34, 199.66),
    b("down", 5000, 6000, 4524.34, 4450.74, 73.6),    # 回调段（k=2，普通路径）
]
UPPER_HIT_DOWN = b("down", 3000, 4000, 4436.0, 4324.68, 111.32)
UPPER_ENDED = [                       # 命中笔后有反向 up（末笔为形成中的 up）→ 上级笔已结束
    b("up", 0, 1000, 4300.0, 4440.0, 140.0),
    UPPER_HIT_DOWN,
    b("up", 4000, 5000, 4324.68, 4524.34, 199.66),
]
UPPER_LAST = [UPPER_ENDED[0], UPPER_HIT_DOWN]          # 命中笔是末笔（延伸中）→ 护栏
UPPER_MISS = [UPPER_ENDED[0],
              b("down", 1900, 5000, 4436.0, 4324.68, 111.32)]  # 起点偏移>1 bar 破坏重合
# 无背驰：后段 [3000,4000] 绿柱面积/DIF 低点/最大绿柱全面强于参照段 [1000,2000]
MACD_NO_DIVERGE = [
    {"time": 1000, "macd": -3.0, "dif": -1.0},
    {"time": 1500, "macd": -3.0, "dif": -1.0},
    {"time": 2000, "macd": -3.0, "dif": -1.0},
    {"time": 3000, "macd": -18.0, "dif": -15.0},
    {"time": 3500, "macd": -20.0, "dif": -17.0},
    {"time": 4000, "macd": -18.0, "dif": -15.0},
    {"time": 5000, "macd": 8.0, "dif": 3.0},
    {"time": 6000, "macd": 4.0, "dif": 1.0},
]

# ---------- 卖侧镜像（同笔上涨段 4324.68→4436.23） ----------
BI_SELL = [
    b("up", 1000, 2000, 4320.0, 4370.0, 60.0),
    b("down", 2000, 3000, 4370.0, 4324.68, 45.32),
    b("up", 3000, 4000, 4324.68, 4436.23, 111.55),    # 同笔上涨段（k=1 评估）
    b("down", 4000, 5000, 4436.23, 4330.0, 106.23),
    b("up", 5000, 6000, 4330.0, 4400.0, 70.0),
]
UPPER_SELL_ENDED = [
    b("down", 0, 1000, 4370.0, 4320.0, 50.0),
    b("up", 3000, 4000, 4324.68, 4436.23, 111.55),    # 命中，非末笔
    b("down", 4000, 5000, 4436.23, 4330.0, 106.23),   # 末笔（反向笔）
]
UPPER_SELL_LAST = [UPPER_SELL_ENDED[0], UPPER_SELL_ENDED[1]]
MACD_NO_DIVERGE_SELL = [
    {"time": 1000, "macd": 3.0, "dif": 1.0},
    {"time": 1500, "macd": 3.0, "dif": 1.0},
    {"time": 2000, "macd": 3.0, "dif": 1.0},
    {"time": 3000, "macd": 18.0, "dif": 15.0},
    {"time": 3500, "macd": 20.0, "dif": 17.0},
    {"time": 4000, "macd": 18.0, "dif": 15.0},
    {"time": 5000, "macd": -8.0, "dif": -3.0},
    {"time": 6000, "macd": -4.0, "dif": -1.0},
]


class TestSameBiFirstBuy(unittest.TestCase):

    def test_same_bi_endpoint_not_marked(self):
        """同笔端点不标；非同笔上涨笔起点仍是 1买。"""
        pts = cc.findBuyPoints(BI_BUY, UPPER_ENDED, MACD_NO_DIVERGE, 900)
        buys = [p for p in pts if p["type"] == "1买"]
        self.assertEqual(buys, [{"type": "1买", "time": 0, "price": 4300.0}])
        self.assertFalse([p for p in pts if p["type"] == "1买" and p["time"] == 4000])

    def test_same_bi_upper_last_still_skipped(self):
        """同笔即使是上级末笔也不标该端点。"""
        pts = cc.findBuyPoints(BI_BUY, UPPER_LAST, MACD_NO_DIVERGE, 900)
        self.assertFalse([p for p in pts if p["type"] == "1买" and p["time"] == 4000])
        self.assertTrue([p for p in pts if p["type"] == "1买" and p["time"] == 0])

    def test_non_same_down_end_is_first_buy(self):
        """非同笔下跌笔终点是 1买（落点=跨度内同价小级别顶点 4000@4324.68）；
        2026-10-14 规则v2 起买卖点判定不再使用背驰（firstNoDiverge 开关已移除）。"""
        pts = cc.findBuyPoints(BI_BUY, UPPER_MISS, MACD_NO_DIVERGE, 900)
        times = [p["time"] for p in pts if p["type"] == "1买"]
        self.assertIn(4000, times)
        self.assertNotIn(5000, times)

    def test_isSameAsUpperBi_returns_matched_object(self):
        """新契约：命中返回 upperBis 内同一 dict 引用；未命中/空表返回 None。"""
        self.assertIs(cc.isSameAsUpperBi(BI_BUY[2], UPPER_ENDED, 900), UPPER_HIT_DOWN)
        self.assertIsNone(cc.isSameAsUpperBi(BI_BUY[2], UPPER_MISS, 900))
        self.assertIsNone(cc.isSameAsUpperBi(BI_BUY[2], [], 900))


class TestSameBiFirstSell(unittest.TestCase):

    def test_same_up_end_not_marked(self):
        """同笔上涨笔终点不标 1卖。"""
        pts = cc.findSellPoints(BI_SELL, UPPER_SELL_ENDED, MACD_NO_DIVERGE_SELL, 900)
        self.assertFalse([p for p in pts if p["type"] == "1卖" and p["time"] == 4000])

    def test_same_bi_upper_last_sell_skipped(self):
        """同笔上涨笔是上级末笔时终点也不标 1卖。"""
        pts = cc.findSellPoints(BI_SELL, UPPER_SELL_LAST, MACD_NO_DIVERGE_SELL, 900)
        self.assertFalse([p for p in pts if p["type"] == "1卖" and p["time"] == 4000])

    def test_unformed_tail_marks_both_sides(self):
        """大级别未成笔尾段不再补一类点（2026-10-14 规则v2：一类只认已收笔上级笔端点）。

        上涨笔起点 (1000,100)：±1根K无同价顶点（小级别底 (2000,100) 差 1.1 根K），
        覆盖窗内同价命中 → 1买 落在小级别起点 (2000,100)，不是上级时刻 1000；
        尾段创新低/新高（4000/5000/6000）不再标一类。
        """
        lower = [
            b("down", 1000, 2000, 120, 100, 20),
            b("up", 2000, 3000, 100, 130, 30),
            b("down", 3000, 4000, 130, 90, 40),
            b("up", 4000, 5000, 90, 140, 50),
            b("down", 5000, 6000, 140, 80, 60),
        ]
        upper = [b("up", 1000, 3000, 100, 130, 30)]
        upper[0]["coverageEnd"] = 6000
        buys = [p["time"] for p in cc.findBuyPoints(lower, upper, [], 900) if p["type"] == "1买"]
        sells = [p["time"] for p in cc.findSellPoints(lower, upper, [], 900) if p["type"] == "1卖"]
        self.assertIn(2000, buys)
        self.assertNotIn(4000, buys)
        self.assertNotIn(6000, buys)
        self.assertIn(3000, sells)
        self.assertNotIn(5000, sells)


class TestFirstPointFallbackLanding(unittest.TestCase):
    """落点阶梯（2026-10-13）：一类点必须落在大级别笔内部的小级别顶点上。

    _lower_vertex_of 阶梯：±1根K同价 → 覆盖窗同价 → 笔跨度同价 →
    跨度内同角色极值；仅跨度内无同角色顶点（小级别在该笔内无结构）才
    保持大级别时刻。
    """

    LOWER = [
        b("up", 0, 900, 102.0, 115.0, 13.0),
        b("down", 900, 1800, 115.0, 104.0, 11.0),
        b("up", 1800, 2700, 104.0, 118.0, 14.0),
        b("down", 2700, 3600, 118.0, 106.0, 12.0),
    ]

    def test_up_bi_extremes_land_on_lower_vertex(self):
        """上涨笔起点(100)→跨度内最低底角色顶点(0,102)=1买；终点(120)→最高顶角色顶点(2700,118)=1卖。"""
        upper = [b("up", 0, 2700, 100.0, 120.0, 20.0)]
        proj = cc.project_first_points(self.LOWER, upper, 900)
        self.assertEqual(proj["buys"], [{"type": "1买", "time": 0, "price": 102.0}])
        self.assertEqual(proj["sells"], [{"type": "1卖", "time": 2700, "price": 118.0}])

    def test_findBuyPoints_uses_fallback_vertex(self):
        """经 findBuyPoints 主流程：1买 落在小级别顶点价 102，不是上级价 100。"""
        upper = [b("up", 0, 2700, 100.0, 120.0, 20.0)]
        buys = [p for p in cc.findBuyPoints(self.LOWER, upper, [], 900) if p["type"] == "1买"]
        self.assertEqual(buys, [{"type": "1买", "time": 0, "price": 102.0}])

    def test_no_role_vertex_keeps_upper_time(self):
        """跨度内只有反向角色顶点时不受其污染；无任何顶点才保持大级别时刻。

        下跌笔 (2700,4500,118→98)：终点跨度内底角色顶点仅 (3600,106) → 1买落它；
        上涨笔 (5000,8000,90→150)：整个跨度小级别无结构 → 两端保持大级别时刻。
        """
        upper_down = [b("down", 2700, 4500, 118.0, 98.0, 20.0)]
        proj = cc.project_first_points(self.LOWER, upper_down, 900)
        self.assertEqual(proj["sells"], [{"type": "1卖", "time": 2700, "price": 118.0}])
        self.assertEqual(proj["buys"], [{"type": "1买", "time": 3600, "price": 106.0}])
        upper_no_structure = [b("up", 5000, 8000, 90.0, 150.0, 60.0)]
        proj2 = cc.project_first_points(self.LOWER, upper_no_structure, 900)
        self.assertEqual(proj2["buys"], [{"type": "1买", "time": 5000, "price": 90.0}])
        self.assertEqual(proj2["sells"], [{"type": "1卖", "time": 8000, "price": 150.0}])


if __name__ == "__main__":
    unittest.main()
