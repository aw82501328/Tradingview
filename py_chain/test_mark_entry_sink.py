# -*- coding: utf-8 -*-
"""区间套下沉判定单元测试（对应 SPEC_divergence_chanset.md 块2 / 规则 1/2/3）

覆盖：
  - levelsBelow 连续链（缺中间级别截断，不跳级）
  - sinkChainRealtime：案例 A 形态（60 内 15 五笔、末段内 3 单笔）→ 停 15；
    案例B 形态（15 内 3 单笔）→ 停 15；次级别展开不足 → 停 X
  - sinkChainConfirm：P 无 X 级端点笔 → 无链；正常下沉
  - 规则 2：参照跨出所属上级笔 → 候选无效（lowerDiverge 过滤 / realtime 直接无候选）
  - realtimeLowerDiverge：只在停止级产候选（含 MACD 背驰构造）

运行：python -m unittest py_chain.test_mark_entry_sink -v
"""

import unittest

from py_chain.mark_entry import (
    levelsBelow, sinkChainRealtime, sinkChainConfirm,
    realtimeLowerDiverge, lowerDiverge, findDivergePoints,
)


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice,
            "span": abs(endPrice - startPrice)}


def macd(times_values):
    """构造 MACD 数组：[(time, macd, dif), ...]。"""
    return [{"time": t, "macd": m, "dif": d, "dea": 0} for t, m, d in times_values]


# 时间基准（秒）：60m bar=3600、15m bar=900、3m bar=180
T0 = 0
B60_UP = bi("up", 36000, 82800, 4400, 4461.7)          # 60m 形成中上涨段（案例 A 主笔）
B60_DOWN_BEFORE = bi("down", 0, 36000, 4500, 4400)

# 15m 五笔展开（案例 A：8-31 10:45 → 9-1 08:15）
B15 = [
    bi("down", 0, 35500, 4500, 4400),
    bi("up", 35500, 50100, 4400, 4440.0),     # #1
    bi("down", 50100, 60300, 4440.0, 4420.0),
    bi("up", 60300, 70200, 4420.0, 4455.93),  # #3（参照）
    bi("down", 70200, 78300, 4455.93, 4430.0),
    bi("up", 78300, 82800, 4430.0, 4461.7),   # 末段（形成中，终点即 P）
]

# 3m：15m 末段内部仅 1 笔（与末段同笔，未展开）
B3 = [
    bi("down", 0, 78200, 4500, 4430.0),
    bi("up", 78200, 82800, 4430.0, 4461.7),
]

# 3m 多笔版（levelsBelow 链测试用，≥3 笔）
B3_RICH = [
    bi("down", 0, 60000, 4500, 4420.0),
    bi("up", 60000, 70000, 4420.0, 4455.0),
    bi("down", 70000, 78200, 4455.0, 4430.0),
    bi("up", 78200, 82800, 4430.0, 4461.7),
]

# 15m K线时间轴（900s 一根，覆盖全窗口；_barsSince 够笔门槛用）
TIMES15 = list(range(0, 83700, 900))


def pd(bis, macdArr=None, macdTimes=None):
    return {"bis": list(bis), "macdArr": macdArr or [], "macdTimes": macdTimes}


class TestLevelsBelow(unittest.TestCase):
    def test_chain_contiguous(self):
        # 15 缺数据 → 链截断于 60（不得由 60 直接下到 3）
        data = {"60": pd(B15), "3": pd(B3_RICH)}
        self.assertEqual(levelsBelow(data, "60"), [])

    def test_chain_full(self):
        data = {"60": pd(B15), "15": pd(B15), "3": pd(B3_RICH)}
        self.assertEqual(levelsBelow(data, "60"), ["15", "3"])


class TestVirtualBi(unittest.TestCase):
    """案例 A 型：X 末段端点已过（近等双顶平台，图表最终结构取后顶）、后续反向结构
    未确认为笔——虚拟形成笔（末段端点→P 开放段）作容器，段内展开 ≥3 同样下沉。"""

    def test_stale_top_sinks_via_virtual_bi(self):
        # 60m 末段 up 止于 8-31 19:00@4464.23；19:00 后 15m 走出 4 段（≥3）到 9-1 08:15 顶
        # （纪元：8-31 00:00=0 → 10:00=36000、19:00=68400、19:15=68700、21:45=77700、
        #   9-1 04:00=100800、07:15=112500、08:15=116100）
        b60 = [bi("down", 0, 36000, 4500, 4400),
               bi("up", 36000, 68400, 4400, 4464.23)]   # 8-31 10:00 → 19:00（平台前顶）
        b15 = [
            bi("up", 38700, 68700, 4400, 4460.0),        # 10:45 → 19:15（平台内）
            bi("down", 68700, 77700, 4460.0, 4415.75),   # 19:15 → 21:45
            bi("up", 77700, 100800, 4415.75, 4455.93),   # 21:45 → 04:00（#3 参照）
            bi("down", 100800, 112500, 4455.93, 4441.85),
            bi("up", 112500, 116100, 4441.85, 4461.7),   # 末段（终点即 P，越过 60m 端点）
        ]
        data = {"60": pd(b60), "15": pd(b15), "3": pd(B3)}
        stop, parent = sinkChainRealtime(data, "60", "short")
        self.assertEqual(stop, "15")
        self.assertEqual(parent["startTime"], 68400)   # 虚拟形成笔起点 = 60m 末段端点 19:00

    def test_stale_top_confirm_chain(self):
        b60 = [bi("down", 0, 36000, 4500, 4400),
               bi("up", 36000, 68400, 4400, 4464.23)]
        b15 = [
            bi("up", 38700, 68700, 4400, 4460.0),
            bi("down", 68700, 77700, 4460.0, 4415.75),
            bi("up", 77700, 100800, 4415.75, 4455.93),
            bi("down", 100800, 112500, 4455.93, 4441.85),
            bi("up", 112500, 116100, 4441.85, 4461.7),
        ]
        data = {"60": pd(b60), "15": pd(b15), "3": pd(B3)}
        stop, parent = sinkChainConfirm(data, "60", 116100, "short")
        self.assertEqual(stop, "15")
        self.assertEqual(parent["startTime"], 68400)

    def test_p_before_last_end_no_chain(self):
        # P 早于 X 末段端点且无精确匹配 → 无链
        b60 = [bi("down", 0, 36000, 4500, 4400), bi("up", 36000, 82800, 4400, 4461.7)]
        b15 = [bi("down", 0, 35500, 4500, 4400),
               bi("up", 35500, 60300, 4400, 4455.0)]
        data = {"60": pd(b60), "15": pd(b15)}
        stop, _ = sinkChainConfirm(data, "60", 60300 + 100, "short")
        self.assertIsNone(stop)


class TestSinkChainRealtime(unittest.TestCase):
    def test_case_a_sinks_to_15(self):
        # 案例A：60 末段内部 15 五笔（≥3）→ 下沉；15 末段内部 3 仅 1 笔 → 停 15
        data = {"60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(B15), "3": pd(B3)}
        stop, parent = sinkChainRealtime(data, "60", "short")
        self.assertEqual(stop, "15")
        self.assertEqual(parent["startTime"], 36000)  # parent = 60m 主笔

    def test_case_b_stops_at_15(self):
        # 案例B：X=15，末段内 3 仅 1 笔 → 停 15（不产 3 级候选）
        data = {"15": pd(B15), "3": pd(B3)}
        stop, _ = sinkChainRealtime(data, "15", "short")
        self.assertEqual(stop, "15")

    def test_no_expansion_stops_at_x(self):
        # 60 末段内部 15 只有 2 段（首段起点在 60 笔起点之前，不计）→ 停在 60（在本级判定）
        b15_2 = [bi("down", 0, 30000, 4500, 4400),
                 bi("up", 30000, 34000, 4400, 4440.0),     # 起点在 60 笔外 → 不计
                 bi("down", 34000, 70200, 4440.0, 4420.0),
                 bi("up", 70200, 82800, 4420.0, 4461.7)]   # 内部仅 2 段 <3
        data = {"60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(b15_2), "3": pd(B3_RICH)}
        stop, _ = sinkChainRealtime(data, "60", "short")
        self.assertEqual(stop, "60")

    def test_direction_mismatch_breaks_chain(self):
        # 15 末段为 down（P 已过、正在回落）→ 15 不是「以 P 为终点的段」→ 停 60
        b15_down_end = B15[:-1] + [bi("down", 78300, 82800, 4461.7, 4430.0)]
        data = {"60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(b15_down_end)}
        stop, _ = sinkChainRealtime(data, "60", "short")
        self.assertEqual(stop, "60")


class TestSinkChainConfirm(unittest.TestCase):
    def test_no_x_level_endpoint_no_chain(self):
        # P 不是 X 级任何笔的端点 → 无链（候选无效）
        data = {"60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(B15), "3": pd(B3)}
        stop, _ = sinkChainConfirm(data, "60", 70000, "short")  # 70000 不是 60 端点
        self.assertIsNone(stop)

    def test_case_a_confirm_sinks_to_15(self):
        data = {"60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(B15), "3": pd(B3)}
        stop, parent = sinkChainConfirm(data, "60", 82800, "short")
        self.assertEqual(stop, "15")

    def test_3m_point_maps_to_15(self):
        # 3m 级端点（07:21→08:12 案例A微段）：下沉停止级是 15 → 3 级候选将被过滤
        data = {"60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(B15), "3": pd(B3)}
        stop, _ = sinkChainConfirm(data, "60", 82800 - 180, "short")
        self.assertEqual(stop, "15")


class TestRealtimeLowerDiverge(unittest.TestCase):
    def _macd_for_diverge(self):
        """15m：参照段（60300..70200）红柱强，末段（78300..82800）红柱弱 → 顶背驰成立。"""
        entries = []
        t = 60300
        while t <= 70200:
            entries.append((t, 5.0, 3.0))
            t += 900
        t = 78300
        while t <= 82800:
            entries.append((t, 1.0, 1.0))
            t += 900
        return macd(entries)

    def test_only_stop_level_candidate(self):
        macdArr = self._macd_for_diverge()
        data = {"60": pd([B60_DOWN_BEFORE, B60_UP]),
                "15": pd(B15, macdArr),
                "3": pd(B3)}
        cands = realtimeLowerDiverge(data, "60", "short", 82900,
                                     periodTimes={"15": TIMES15, "3": list(range(78000, 83100, 180))})
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["res"], "15")          # 案例A：markRes=15，不再是 3
        self.assertEqual(cands[0]["point"]["price"], 4461.7)
        self.assertEqual(cands[0]["segStart"], 78300)

    def test_cross_parent_refer_invalid(self):
        # 内部小同向段 span 不足被跳过 → 最近合格参照在 60m 主笔之外（跨上级笔）→ 规则2 无效
        b15_cross = [
            bi("down", 0, 30000, 4500, 4400),
            bi("up", 30000, 60000, 4300.0, 4460.0),     # 跨笔参照：起点在 60m 主笔外
            bi("down", 60000, 65000, 4460.0, 4435.0),
            bi("up", 65000, 70000, 4435.0, 4445.0),     # span 10 < 末段 span*0.5 → 被跳过
            bi("down", 70000, 78300, 4445.0, 4430.0),
            bi("up", 78300, 82800, 4430.0, 4461.7),
        ]
        data = {"60": pd([B60_DOWN_BEFORE, B60_UP]),
                "15": pd(b15_cross, self._macd_for_diverge()),
                "3": pd(B3)}
        stop, _ = sinkChainRealtime(data, "60", "short")
        self.assertEqual(stop, "15")  # 内部展开 ≥3 段，正常下沉
        cands = realtimeLowerDiverge(data, "60", "short", 82900,
                                     periodTimes={"15": TIMES15})
        self.assertEqual(cands, [])   # 参照跨上级笔 → 候选无效

    def test_no_new_high_no_candidate(self):
        # 案例B：末段未创新高（4450 < 参照 4455.93）→ 无候选
        b15_b = B15[:-1] + [bi("up", 78300, 82800, 4430.0, 4450.0)]
        data = {"60": pd([B60_DOWN_BEFORE, B60_UP]),
                "15": pd(b15_b, self._macd_for_diverge()),
                "3": pd(B3)}
        stop, _ = sinkChainRealtime(data, "60", "short")
        self.assertEqual(stop, "15")
        cands = realtimeLowerDiverge(data, "60", "short", 82900,
                                     periodTimes={"15": TIMES15})
        self.assertEqual(cands, [])


class TestLowerDivergeFiltering(unittest.TestCase):
    def test_3m_candidate_filtered_when_sink_stops_at_15(self):
        # 3m 有顶背驰候选（创新高+背驰），但下沉停止级是 15 → lowerDiverge 过滤掉
        macd3 = macd([(60300, 5.0, 3.0), (61200, 5.0, 3.0), (78200, 1.0, 1.0), (82800, 1.0, 1.0)])
        data = {"60": pd([B60_DOWN_BEFORE, B60_UP]),
                "15": pd(B15),
                "3": pd(B3, macd3)}
        # 3m 末段 4461.7 > 前同向段（78200 前无同向 up…用 B3 只有 1 根 up，构造背驰参照：
        b3_rich = [bi("down", 0, 60000, 4500, 4420.0),
                   bi("up", 60000, 70000, 4420.0, 4455.0),
                   bi("down", 70000, 78200, 4455.0, 4430.0),
                   bi("up", 78200, 82800, 4430.0, 4461.7)]
        data["3"] = pd(b3_rich, macd3)
        pts = findDivergePoints(b3_rich, macd3)
        self.assertTrue(any(p["direction"] == "short" for p in pts), "3m 应有裸背驰点")
        cands = lowerDiverge(data, "60", "short")
        self.assertEqual(cands, [], "下沉停止级=15 → 3m 候选应被过滤（15 又未创新高无候选）")


if __name__ == "__main__":
    unittest.main()
