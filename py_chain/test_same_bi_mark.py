# -*- coding: utf-8 -*-
"""同笔 1买/1卖 结构标记单元测试（2026-09-15 规则微调）

覆盖 findBuyPoints/findSellPoints 的同笔分支（与 JS chan_core.test.js 对拍，同输入双语言同输出）：
  - 同笔 + 命中的上级笔已结束（非末笔）→ 纯结构标记 1买/1卖（不选参照笔、不比创新低/新低、不比背驰）；
  - 同笔 + 命中笔是上级末笔（延伸中，反向笔未确认进列表）→ 维持跳过（时序护栏）；
  - 非同笔 → 普通路径仍需背驰（纯结构分支无泄漏）；
  - isSameAsUpperBi 返回值契约：命中笔对象（同引用）/ None。

数据镜像 2026-08-17~08-20 XAUUSD 60m 实例（同笔下跌段 4436.23→4324.68 与 240m 下跌笔
四字段重合）；macdArr 故意造成「无背驰」（后段绿柱面积更大、DIF 更低），锁定同笔分支
不依赖背驰——若日后往同笔分支加回背驰/创新低要求，本案将重新漏标、测试即挂。

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

    def test_same_bi_upper_ended_marks_first_buy(self):
        """同笔 + 上级笔已结束 → 纯结构标记 1买，且无 2买/3买 混入（分支隔离）。"""
        pts = cc.findBuyPoints(BI_BUY, UPPER_ENDED, MACD_NO_DIVERGE, 900)
        self.assertEqual(pts, [{"type": "1买", "time": 4000, "price": 4324.68}])

    def test_same_bi_upper_last_still_skipped(self):
        """同笔 + 命中笔是上级末笔（延伸中）→ 跳过不标（时序护栏）。"""
        pts = cc.findBuyPoints(BI_BUY, UPPER_LAST, MACD_NO_DIVERGE, 900)
        self.assertFalse([p for p in pts if p["type"] == "1买"])

    def test_non_same_bi_still_requires_divergence(self):
        """非同笔（破坏重合）→ 参照/创新低/背驰普通路径：无背驰数据下不产 1买。"""
        pts = cc.findBuyPoints(BI_BUY, UPPER_MISS, MACD_NO_DIVERGE, 900)
        self.assertFalse([p for p in pts if p["type"] == "1买"])

    def test_isSameAsUpperBi_returns_matched_object(self):
        """新契约：命中返回 upperBis 内同一 dict 引用；未命中/空表返回 None。"""
        self.assertIs(cc.isSameAsUpperBi(BI_BUY[2], UPPER_ENDED, 900), UPPER_HIT_DOWN)
        self.assertIsNone(cc.isSameAsUpperBi(BI_BUY[2], UPPER_MISS, 900))
        self.assertIsNone(cc.isSameAsUpperBi(BI_BUY[2], [], 900))


class TestSameBiFirstSell(unittest.TestCase):

    def test_same_bi_upper_ended_marks_first_sell(self):
        """同笔 + 上级笔已结束 → 纯结构标记 1卖；函数内锚定锚回同一端点（无漂移）。"""
        pts = cc.findSellPoints(BI_SELL, UPPER_SELL_ENDED, MACD_NO_DIVERGE_SELL, 900)
        self.assertEqual(pts, [{"type": "1卖", "time": 4000, "price": 4436.23}])

    def test_same_bi_upper_last_sell_skipped(self):
        """同笔 + 命中笔是上级末笔（延伸中）→ 跳过不标。"""
        pts = cc.findSellPoints(BI_SELL, UPPER_SELL_LAST, MACD_NO_DIVERGE_SELL, 900)
        self.assertFalse([p for p in pts if p["type"] == "1卖"])


if __name__ == "__main__":
    unittest.main()
