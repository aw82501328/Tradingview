# -*- coding: utf-8 -*-
"""进场背驰判据 v2 单元测试（2026-10-27：先斜率、后面积明显变小）。

覆盖三层：
  - entryDivergeOk 纯函数（斜率过 / 面积阈值边界（严格 <）/ 时长门 / 只开一项 /
    两开关全关 ≡ 旧三项 OR 恒等 / v2=False 强制旧口径 / 颜色方向 / 除零保护）；
  - findDivergePoints 确认制（新口径拦下旧 OR 放行的"仅面积变小"点；全关=旧行为）；
  - _evalRealtimeNode 当下制（同口径接线；9-25 21:30 动因案例数值复刻）。

9-25 动因案例数值（真实重放实测，见 _tmp_div_diag_0925.py）：
  参照笔 down 19:09@4315.795→20:18@4297.6（69min）：绿柱面积 35.87、DIF低点 -1.30、最长绿柱 3.06
  形成段 down 20:27@4309.49→21:30@4273.43（63min）：绿柱面积 25.41、DIF低点 -5.49、最长绿柱 3.71
  → 斜率 36.06/3780s > 18.195/4140s（没变小）；面积 25.41/35.87≈71% > 50%（没明显变小）
  → 新口径不开；旧三项 OR 靠"面积变小"一条放行（即被本批修掉的开单）。

运行：python -m unittest py_chain.test_entry_diverge
"""
import unittest

from py_chain import chan_core as cc
from py_chain import mark_entry as me


def bi(type_, t0, t1, p0, p1):
    return {"type": type_, "startTime": t0, "endTime": t1,
            "startPrice": p0, "endPrice": p1, "span": abs(p1 - p0)}


def macd(t, dif, hist):
    return {"time": t, "dif": dif, "macd": hist}


# ---- 9-25 21:30 案例数值复刻（时间平移到 0 起；窗口/数值同真实重放） ----
# 参照 down：69min=4140s，18.195 幅度；形成 down：63min=3780s，36.06 幅度
REF_DOWN = bi("down", 0, 4140, 4315.795, 4297.6)
F_DOWN = bi("down", 4320, 8100, 4309.49, 4273.43)
# MACD：参照窗 10 根绿柱 Σ=35.87（单根最大 3.06，DIF 最低 -1.2969）；
# 形成段窗 10 根绿柱 Σ=25.41（单根最大 3.71，DIF 最低 -5.4867）
CASE_MACD = (
    [macd(180 * (i + 1), -1.0 - 0.03 * i, -3.06 if i == 5 else -3.5) for i in range(10)]
    + [macd(4320 + 180 * (i + 1), -5.4867 if i == 9 else -2.0 - 0.3 * i,
            -3.71 if i == 4 else -2.541) for i in range(10)]
)


class _CfgIso(unittest.TestCase):
    """CHAN_CFG 开关键隔离基类：setUp 存快照、tearDown 还原。"""

    def setUp(self):
        self._saved = dict(cc.CHAN_CFG)

    def tearDown(self):
        cc.CHAN_CFG.clear()
        cc.CHAN_CFG.update(self._saved)

    def set_cfg(self, **kv):
        cc.CHAN_CFG.update(kv)


class EntryDivergeOkTests(_CfgIso):
    """纯函数判据：斜率优先、面积阈值（严格 <）、时长门、全关=旧 OR。"""

    def test_case_925_new_criteria_blocks(self):
        """动因案例：斜率更大+面积 71% → 新口径 False。"""
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=True, divergeAreaShrinkPct=50.0)
        self.assertFalse(me.entryDivergeOk(F_DOWN, REF_DOWN, CASE_MACD))

    def test_case_925_old_or_passes(self):
        """同案例：旧三项 OR 靠面积变小放行（历史行为），全关与 v2=False 都复现。"""
        self.set_cfg(divergeSlopeOn=False, divergeAreaShrinkOn=False)
        self.assertTrue(me.entryDivergeOk(F_DOWN, REF_DOWN, CASE_MACD))
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=True)
        self.assertTrue(me.entryDivergeOk(F_DOWN, REF_DOWN, CASE_MACD, v2=False))
        # 旧 OR 三项核对：面积 25.41<35.87 过；DIF 更低不过；单根柱更高不过
        self.assertTrue(cc.isBiDiverge(F_DOWN, REF_DOWN, CASE_MACD))

    def test_slope_smaller_passes_even_area_bigger(self):
        """斜率变小即背驰（面积再大也不拦——斜率优先）。"""
        slow = bi("down", 0, 4000, 100.0, 50.0)      # 斜率 50/4000=0.0125
        fast = bi("down", 5000, 6000, 49.0, 42.0)    # 斜率 7/1000=0.007 更缓，创新低
        arr = [macd(100, -5.0, -1.0), macd(200, -6.0, -1.0),
               macd(5100, -7.0, -5.0), macd(5500, -7.0, -5.0)]  # 面积 10 > 2 反而变大
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=True, divergeAreaShrinkPct=50.0)
        self.assertTrue(me.entryDivergeOk(fast, slow, arr))

    def test_area_threshold_strict_boundary(self):
        """面积阈值严格 <：缩到恰 50% 不过，49.9% 过。"""
        slow = bi("down", 0, 1000, 100.0, 90.0)      # 斜率 10/1000=0.01
        fast = bi("down", 2000, 3200, 89.0, 70.0)    # 斜率 19/1200≈0.0158 更陡（斜率不过）
        # 参照绿柱 Σ=10；形成段 Σ=5.0（恰 50%）→ 不过；Σ=4.99（49.9%）→ 过
        arr_eq = [macd(100, -1.0, -10.0), macd(2100, -2.0, -2.5), macd(3100, -2.0, -2.5)]
        arr_lt = [macd(100, -1.0, -10.0), macd(2100, -2.0, -2.5), macd(3100, -2.0, -2.49)]
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=True, divergeAreaShrinkPct=50.0)
        self.assertFalse(me.entryDivergeOk(fast, slow, arr_eq))
        self.assertTrue(me.entryDivergeOk(fast, slow, arr_lt))
        # pct=100 退化成"任意变小即可"；pct=0 恒不过
        self.set_cfg(divergeAreaShrinkPct=100.0)
        self.assertTrue(me.entryDivergeOk(fast, slow, arr_eq))
        self.set_cfg(divergeAreaShrinkPct=0.0)
        self.assertFalse(me.entryDivergeOk(fast, slow, arr_lt))

    def test_area_duration_gate(self):
        """时长比 > divergeDurRatio → 面积项不可用（只剩斜率），面积再小也不过。"""
        slow = bi("down", 0, 1000, 100.0, 90.0)        # 1000s
        fast = bi("down", 2000, 3200, 89.0, 70.0)      # 1200s，斜率更陡
        self.assertGreater(1200 / 1000, 0)             # 占位可读性
        long_ref = bi("down", 0, 6000, 100.0, 90.0)    # 6000s：比值 5>3 面积不可用
        arr = [macd(100, -1.0, -10.0), macd(2100, -2.0, -1.0), macd(3100, -2.0, -1.0)]
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=True, divergeAreaShrinkPct=50.0)
        self.assertFalse(me.entryDivergeOk(fast, long_ref, arr))
        self.assertTrue(me.entryDivergeOk(fast, slow, arr))  # 时长可比（1.2）且面积 2<10·50%

    def test_area_only_mode(self):
        """只开面积（斜率关）：跳过斜率判定直接比面积。"""
        steep_ok = bi("down", 0, 1000, 100.0, 90.0)    # 斜率 0.01
        sharper = bi("down", 2000, 2900, 89.0, 70.0)   # 斜率 19/900≈0.021 更陡
        arr = [macd(100, -1.0, -10.0), macd(2100, -2.0, -1.0), macd(2800, -2.0, -1.0)]
        self.set_cfg(divergeSlopeOn=False, divergeAreaShrinkOn=True, divergeAreaShrinkPct=50.0)
        self.assertTrue(me.entryDivergeOk(sharper, steep_ok, arr))

    def test_slope_only_mode(self):
        """只开斜率（面积关）：斜率没变小则面积再小也不过。"""
        slow = bi("down", 0, 1000, 100.0, 90.0)
        fast = bi("down", 2000, 2900, 89.0, 70.0)      # 更陡 → 斜率不过
        arr = [macd(100, -1.0, -10.0), macd(2100, -2.0, -0.1), macd(2800, -2.0, -0.1)]
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=False)
        self.assertFalse(me.entryDivergeOk(fast, slow, arr))

    def test_up_stroke_uses_red_area(self):
        """顶背驰镜像：up 笔比红柱面积（绿柱面积不参与）。"""
        slow = bi("up", 0, 1000, 90.0, 100.0)          # 斜率 0.01
        fast = bi("up", 2000, 3200, 101.0, 120.0)      # 斜率更陡，创新高
        # 红柱 Σ 2 < 10×50% → 面积过
        arr_red_small = [macd(100, 1.0, 10.0), macd(2100, 2.0, 1.0), macd(3100, 2.0, 1.0)]
        # 红柱 Σ 12（不过）但绿柱 Σ 0.2（若错用绿柱会误过）
        arr_red_big = [macd(100, 1.0, 10.0), macd(2100, 2.0, 6.0), macd(3100, 2.0, 6.0)]
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=True, divergeAreaShrinkPct=50.0)
        self.assertTrue(me.entryDivergeOk(fast, slow, arr_red_small))
        self.assertFalse(me.entryDivergeOk(fast, slow, arr_red_big))

    def test_zero_duration_guard(self):
        """时长 0（退化笔）→ 斜率不可用不抛除零；时长门同时不过 → 保守拒（False）。"""
        degenerate = bi("down", 0, 0, 100.0, 90.0)     # 时长 0
        fast = bi("down", 2000, 3200, 89.0, 70.0)
        arr = [macd(0, -1.0, -10.0), macd(2100, -2.0, -1.0), macd(3100, -2.0, -1.0)]
        self.assertIsNone(me._slopeOf(degenerate))
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=True, divergeAreaShrinkPct=50.0)
        # refer 退化：斜率 None（跳过）+ _areaDurComparable 时长 0 不过（面积跳过）→ False
        self.assertFalse(me.entryDivergeOk(fast, degenerate, arr))


class FindDivergePointsV2Tests(_CfgIso):
    """确认制 findDivergePoints：新口径拦"仅面积变小"点；全关=旧行为。"""

    def _fixture(self):
        # up/down/up/down：第二根 down 创新低（9-25 案例数值），中间 up 幅度足够
        # （span 16.09 ≥ 36.06×0.5=18.03？不足 → 参照会跳过 up 找更早——这里直接
        # 用 4 笔、refer=第一根 down，up span 调大为 20 保证不被跳过逻辑影响
        # （pickDivergeRefer 只看同向笔，up 幅度不参与参照选取，无需条件）。
        up0 = bi("up", -1000, 0, 4290.0, 4315.795)
        return [up0, REF_DOWN, bi("up", 4140, 4320, 4297.6, 4309.49), F_DOWN], CASE_MACD

    def test_v2_blocks_old_or_point(self):
        bis, arr = self._fixture()
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=True, divergeAreaShrinkPct=50.0,
                     divergeReferByZs=False)
        self.assertEqual(me.findDivergePoints(bis, arr), [])
        # 全关=旧口径：同 fixture 出一个做多背驰点
        self.set_cfg(divergeSlopeOn=False, divergeAreaShrinkOn=False)
        pts = me.findDivergePoints(bis, arr)
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0]["direction"], "long")
        self.assertEqual(pts[0]["time"], F_DOWN["endTime"])
        self.assertEqual(pts[0]["price"], F_DOWN["endPrice"])

    def test_v2_slope_point_emitted(self):
        """斜率变小的背驰点新口径照常产出。"""
        slow = bi("down", 0, 4000, 100.0, 50.0)       # 斜率 50/4000=0.0125
        fast = bi("down", 5000, 6000, 49.0, 42.0)     # 斜率 7/1000=0.007 更缓，创新低 42<50
        bis = [bi("up", -500, 0, 40.0, 100.0), slow, bi("up", 4000, 5000, 50.0, 49.0), fast]
        arr = [macd(100, -5.0, -1.0), macd(200, -6.0, -1.0), macd(5100, -7.0, -5.0)]
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=True, divergeAreaShrinkPct=50.0,
                     divergeReferByZs=False)
        pts = me.findDivergePoints(bis, arr)
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0]["direction"], "long")
        self.assertEqual(pts[0]["price"], 42.0)


class EvalRealtimeNodeV2Tests(_CfgIso):
    """当下制 _evalRealtimeNode 接线：9-25 案例数值直评（新口径拒/旧口径过）。"""

    def _pd_and_node(self):
        bis = [bi("up", -600, 0, 4280.0, 4315.795), REF_DOWN,
               bi("up", 4140, 4320, 4297.6, 4309.49), F_DOWN]
        bars = [{"time": i * 180, "open": 4300.0, "high": 4310.0, "low": 4290.0, "close": 4300.0}
                for i in range(24, 46)]  # 4320..8100：覆盖形成段窗口的 3m K（tCut=8280 已收）
        pd = {"3": {"bis": bis, "bars": bars, "macdArr": CASE_MACD,
                    "macdTimes": [m["time"] for m in CASE_MACD]}}
        parent = bi("down", 0, 8100, 4315.795, 4273.43)  # 上级笔容器罩住两段
        node = {"res": "3", "F": F_DOWN, "parentBi": parent}
        return pd, node

    def test_realtime_new_reject_old_pass(self):
        pd, node = self._pd_and_node()
        self.set_cfg(divergeSlopeOn=True, divergeAreaShrinkOn=True, divergeAreaShrinkPct=50.0,
                     divergeReferByZs=False)
        cand, code, _ = me._evalRealtimeNode(pd, node, "long", 8280, None, 1)
        self.assertIsNone(cand)
        self.assertEqual(code, "macd_no_diverge")
        # 全关=旧口径：候选命中，点=形成段终点
        self.set_cfg(divergeSlopeOn=False, divergeAreaShrinkOn=False)
        cand, code, _ = me._evalRealtimeNode(pd, node, "long", 8280, None, 1)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["point"]["time"], F_DOWN["endTime"])
        self.assertEqual(cand["point"]["price"], F_DOWN["endPrice"])
        self.assertEqual(cand["segStart"], F_DOWN["startTime"])


if __name__ == "__main__":
    unittest.main()
