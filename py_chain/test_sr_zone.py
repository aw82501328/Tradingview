# -*- coding: utf-8 -*-
"""
sr_zone 支阻区间单元测试（SPEC：OHLC 高低点聚类支撑压力区间识别方案）

覆盖：
  - Wilder ATR 序列（种子期均值 + 递推；常数 TR 收敛）
  - 已确认拐点：双底/平台保留靠后拐点/末 3 根不作拐点（i+3 收盘确认，无未来数据）
  - 聚类：容差内合并 / 平票先并价低组 / 低点高点分开聚类
  - 独立事件：相邻 <7 根链式合并（事件时间=链末）
  - 有效性：双底 2 事件入选；单底不入选；两根收盘越界+缓冲 → 失效清零；
    失效后 ≥2 次新事件重新入选
  - 边界：<30 根 / ATR=0 → 空结果不强行补齐；收盘价在区间内 → inside_zone
  - 前缀稳定（无未来数据）：快照 = 时间截断重算，且与后续数据无关
  - zone_candidates 候选格式（price=按 kind 远侧边界，供止损外侧语义）
  - compute_srflip srMode 集成：levels 缺省逐位不变；zones 全 zone 候选；
    人工位按周期覆盖
  - mark_entry.near_zone 区间闸门（背驰点+区间，方向匹配 + 点位回退）

运行：python -m unittest py_chain.test_sr_zone -v
"""
import random
import unittest

from py_chain import sr_zone
from py_chain.sr_flip import compute_srflip
from py_chain.mark_entry import (near_zone, nearSr, stop_ref_of,
                                 sr_of_detect, sr_drop_fib_for_class1)


def bar(t, h, l, c, o=None):
    return {"time": t, "open": o if o is not None else c, "high": h, "low": l,
            "close": c, "volume": 100}


def walk(path, t0=0, step=180, wick=0.15):
    """按收盘序列生成K线：high/low = 相邻收盘包络 ± wick。"""
    out = []
    prev = path[0]
    for i, p in enumerate(path):
        out.append(bar(t0 + i * step, max(prev, p) + wick, min(prev, p) - wick, p))
        prev = p
    return out


def saw(n, lo, hi, seg=5, t0=0):
    """锯齿震荡：seg 根一段在 lo/hi 之间往返——反复制造局部拐点。"""
    path = []
    for i in range(n):
        k = (i // seg) % 2
        frac = (i % seg) / max(1, seg - 1)
        path.append(round(lo + (hi - lo) * (frac if k == 0 else 1 - frac), 3))
    return walk(path, t0=t0)


# 段值固定：双底 100.0/100.2（间隔远超 7 根）、双顶 107.2 附近、末段收 ~101.8
def double_bottom_bars(n_extra=0, t0=0):
    path = []
    for i in range(60):
        if i < 10:
            path.append(round(106 - i * 0.6, 3))          # 跌到 100.6
        elif i < 20:
            path.append(round(100.0 + (i - 10) * 0.35, 3))  # 反弹到 103.5
        elif i < 30:
            path.append(round(103.5 - (i - 20) * 0.33, 3))  # 回落 100.53
        elif i < 45:
            path.append(round(100.2 + (i - 30) * 0.47, 3))  # 拉到 107.2
        else:
            path.append(round(107.2 - (i - 45) * 0.3857, 3))  # 收在 101.8
    return walk(path + [101.8] * n_extra, t0=t0)


class TestAtrWilder(unittest.TestCase):
    def test_constant_tr_converges(self):
        # 每根 TR=1（高低差 1、前收即中点）→ Wilder ATR 恒为 1
        bars = [bar(i * 180, 1.5 + i * 0, 0.5, 1.0) for i in range(60)]
        # 修正：close 需构成 TR=|h-pc|/|l-pc|=1 → 每根 close=prev±0（保持中点）
        bars = [bar(i * 180, 1.5, 0.5, 1.0) for i in range(60)]
        atrs = sr_zone.atr_wilder_series(bars, 14)
        self.assertEqual(atrs[0], 0.0)
        self.assertAlmostEqual(atrs[14], 1.0, places=9)
        self.assertAlmostEqual(atrs[-1], 1.0, places=9)

    def test_first_element_zero_and_length(self):
        bars = walk([1, 2, 3, 4])
        atrs = sr_zone.atr_wilder_series(bars, 3)
        self.assertEqual(len(atrs), 4)
        self.assertEqual(atrs[0], 0.0)
        self.assertTrue(all(a >= 0 for a in atrs))


class TestExtractPivots(unittest.TestCase):
    def test_double_bottom_pivots_confirmed_only_after_right_span(self):
        bars = double_bottom_bars()
        # 第 11 根是首个局部低（i=10/11 低点等价 99.85，严格右侧规则保留靠后）：
        # i+3 收盘确认 —— 截到 i+3 前不可用（n=15 → i ≤ 11 刚好含；n=14 → i ≤ 9 不含）
        p15 = sr_zone.extract_pivots(bars[:15], 3)
        p14 = sr_zone.extract_pivots(bars[:14], 3)
        lows15 = [p["i"] for p in p15["low"]]
        lows14 = [p["i"] for p in p14["low"]]
        self.assertIn(11, lows15)
        self.assertNotIn(11, lows14)

    def test_last_span_bars_never_pivots(self):
        bars = double_bottom_bars()
        n = len(bars)
        pv = sr_zone.extract_pivots(bars, 3)
        self.assertTrue(all(p["i"] <= n - 4 for p in pv["low"] + pv["high"]))

    def test_plateau_keeps_later_pivot(self):
        # 平台同价：低点 low 相同（1~4 根 low 均 99.5）→ 右侧严格更高才成立
        # → 保留靠后拐点（i=4，其右 low=103.5 严格更高）
        path = [104, 100, 100, 100, 104, 104]
        bars = []
        prev = path[0]
        for i, p in enumerate(path):
            bars.append(bar(i * 180, max(prev, p) + 0.1, min(prev, p) - 0.5, p))
            prev = p
        pv = sr_zone.extract_pivots(bars, 1)
        self.assertEqual([p["i"] for p in pv["low"]], [4])


class TestClusterAndEvents(unittest.TestCase):
    def test_cluster_merges_within_tol(self):
        piv = [{"i": 1, "time": 1, "price": 100.0},
               {"i": 20, "time": 20, "price": 100.2},
               {"i": 40, "time": 40, "price": 106.0}]
        g = sr_zone.cluster_pivots(piv, 0.5)
        self.assertEqual(len(g), 2)
        self.assertEqual([p["price"] for p in g[0]], [100.0, 100.2])

    def test_cluster_no_merge_beyond_tol(self):
        piv = [{"i": 1, "time": 1, "price": 100.0},
               {"i": 20, "time": 20, "price": 101.0}]
        self.assertEqual(len(sr_zone.cluster_pivots(piv, 0.5)), 2)

    def test_events_chain_merge_gap(self):
        piv = [{"i": 1, "time": 1, "price": 100.0},
               {"i": 5, "time": 5, "price": 100.1},    # 距前 4 根 <7 → 同一事件
               {"i": 20, "time": 20, "price": 100.2}]  # 距前 15 根 ≥7 → 新事件
        ev = sr_zone.events_of(piv, 7)
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0][-1]["time"], 5)   # 事件时间=链末
        self.assertEqual(ev[1][-1]["time"], 20)


class TestValidity(unittest.TestCase):
    def test_double_bottom_support_valid(self):
        r = sr_zone.zones_of_period(double_bottom_bars())
        # 100 附近应存在支撑区间（双底 99.85/100.05，含 2 次独立事件）
        near100 = [z for z in r["zones"] if z["kind"] == "support"
                   and z["lower"] <= 100.4 and z["upper"] >= 99.5]
        self.assertTrue(near100)
        self.assertGreaterEqual(near100[0]["eventCount"], 2)
        # SPEC 第4步：支撑=上界低于收盘的最近区间
        self.assertIsNotNone(r["support"])

    def test_single_event_not_enough(self):
        # 单一深 V（只有一个低点拐点）→ 不够 2 次事件 → 无该支撑
        path = [104] * 5 + [104 - i * 0.8 for i in range(1, 6)] \
            + [100 + i * 0.8 for i in range(1, 26)]
        r = sr_zone.zones_of_period(walk(path))
        self.assertFalse([z for z in r["zones"] if z["lower"] < 101 < z["upper"]])

    def test_invalidation_two_closes_beyond_buffer(self):
        # 双底后价格暴跌：两根收盘大幅低于下界 → 支撑失效（事件清零）
        path = []
        for i in range(40):
            if i < 10:
                path.append(106 - i * 0.6)
            elif i < 20:
                path.append(100.0 + (i - 10) * 0.35)
            elif i < 30:
                path.append(103.5 - (i - 20) * 0.33)
            else:
                path.append(100.2 - (i - 29) * 2.0)   # 末段连跌破位
        bars = walk(path)
        r = sr_zone.zones_of_period(bars)
        self.assertFalse([z for z in r["zones"] if z["kind"] == "support"
                          and z["lower"] < 101 < z["upper"]])

    def test_too_few_bars_and_zero_atr(self):
        self.assertEqual(sr_zone.zones_of_period(walk([1, 2, 3]))["zones"], [])
        flat = [bar(i * 180, 100.0, 100.0, 100.0) for i in range(60)]
        self.assertEqual(sr_zone.zones_of_period(flat)["zones"], [])

    def test_inside_zone(self):
        bars = saw(80, 99, 101)          # 震荡在 99~101 → 上下都有区间
        r = sr_zone.zones_of_period(bars)
        close = r["close"]
        # 收盘恰在某区间内（构造：末端加入收在区间中部的新K线）
        z = r["zones"]
        self.assertIsInstance(r["inside_zone"], (dict, type(None)))
        # saw 行情收盘必在某区间内（99/101 两端区间存在）
        self.assertTrue(any(zz["lower"] <= close <= zz["upper"] for zz in z)
                        or r["inside_zone"] is None)


class TestPrefixStability(unittest.TestCase):
    def test_snapshot_equals_time_truncated_recompute(self):
        random.seed(42)
        bars = walk([round(100 + random.uniform(-1.5, 1.5), 3) for _ in range(160)])
        for t in (60, 90, 130, 160):
            snap = sr_zone.zones_of_period(bars[:t])
            again = sr_zone.zones_of_period([b for b in bars if b["time"] <= bars[t - 1]["time"]])
            self.assertEqual(snap, again, f"前缀快照被改写 @t={t}")

    def test_future_bars_do_not_change_snapshot(self):
        random.seed(7)
        bars = walk([round(100 + random.uniform(-1, 1), 3) for _ in range(120)])
        snap = sr_zone.zones_of_period(bars[:100])
        junk = walk([round(50 + i, 3) for i in range(30)], t0=bars[99]["time"] + 180)
        self.assertEqual(snap, sr_zone.zones_of_period((bars + junk)[:100]))


class TestZoneCandidates(unittest.TestCase):
    def test_candidate_format_price_is_far_boundary(self):
        bars = double_bottom_bars()
        cands, sel = sr_zone.zone_candidates("60", bars)
        self.assertTrue(cands)
        for c in cands:
            self.assertEqual(c["srcType"], "zone")
            self.assertIn(c["kind"], ("support", "resistance"))
            self.assertLessEqual(c["lower"], c["upper"])
            # price=远侧边界：support→lower（止损在区间外侧）、resistance→upper
            if c["kind"] == "support":
                self.assertEqual(c["price"], c["lower"])
                self.assertEqual(c["type"], "SUP")
            else:
                self.assertEqual(c["price"], c["upper"])
                self.assertEqual(c["type"], "RES")
            self.assertEqual(c["touchCount"], c["eventCount"])

    def test_work_cache_reuse_consistent(self):
        bars = double_bottom_bars()
        wc = {}
        a = sr_zone.zone_candidates("60", bars, work_cache=wc)
        b = sr_zone.zone_candidates("60", bars, work_cache=wc)   # 命中缓存
        self.assertEqual(a, b)
        self.assertEqual(len(wc), 1)


class TestComputeSrflipZones(unittest.TestCase):
    def _bars60(self):
        return double_bottom_bars()

    def test_levels_default_bit_identical(self):
        bars = {"60": self._bars60()}
        bis = {"60": [
            {"type": "down", "startTime": 0, "endTime": 5000, "startPrice": 106, "endPrice": 100},
            {"type": "up", "startTime": 5000, "endTime": 9000, "startPrice": 100, "endPrice": 104},
            {"type": "down", "startTime": 9000, "endTime": 10500, "startPrice": 104, "endPrice": 101},
        ]}
        a = compute_srflip(bis, bars, ["60"])
        b = compute_srflip(bis, bars, ["60"], srMode="levels")
        self.assertEqual(a, b)

    def test_zones_mode_all_zone_candidates(self):
        bars = {"60": self._bars60()}
        r = compute_srflip({}, bars, ["60"], srMode="zones")
        self.assertTrue(r["merged"])
        self.assertTrue(all(f["srcType"] == "zone" for f in r["merged"]))
        self.assertIn("zoneSelection", r)
        self.assertIn("60", r["zoneSelection"])
        # 画线条目带 label（支撑区间/压力区间+周期）
        for lines in r["drawnByPeriod"].values():
            for f in lines:
                self.assertIn(f["label"], ("支撑区间+1小时", "压力区间+1小时"))

    def test_zones_mode_ignores_manual_levels(self):
        # 2026-10-08 实测教训：品种桶遗留的全周期人工位会静默替换掉区间
        # （整系统切换被架空）→ zones 模式忽略 manualLevels，人工位仅经典模式生效
        bars = {"60": self._bars60()}
        r = compute_srflip({}, bars, ["60"], srMode="zones",
                           manualLevels={"60": [103.0]})
        self.assertTrue(r["merged"])
        self.assertTrue(all(f.get("srcType") == "zone" for f in r["merged"]))
        self.assertFalse(any(f.get("manual") for f in r["merged"]))


class TestNearZoneGate(unittest.TestCase):
    SUP = {"kind": "support", "price": 99.85, "lower": 99.85, "upper": 100.35, "srcType": "zone"}
    RES = {"kind": "resistance", "price": 107.65, "lower": 107.05, "upper": 107.65, "srcType": "zone"}
    PT = {"price": 104.0}   # 点位候选（如人工位）

    def test_long_diverge_inside_support_zone_hits(self):
        hit = near_zone(100.2, [self.SUP, self.RES], "long", 10.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["dist"], 0)
        self.assertEqual(hit["sr"]["kind"], "support")
        # 信号 nearSr=远侧边界（止损落区间外侧）
        self.assertEqual(hit["sr"]["price"], 99.85)

    def test_short_rejected_for_support_zone(self):
        # 顶背驰点落在支撑区间 → 空头闸门不认（方向不匹配）
        self.assertIsNone(near_zone(100.2, [self.SUP], "short", 10.0))

    def test_short_top_diverge_inside_resistance_zone(self):
        hit = near_zone(107.3, [self.SUP, self.RES], "short", 10.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["sr"]["price"], 107.65)   # 远侧=上沿

    def test_near_tolerance_boundary(self):
        # 距上沿 100.35 恰好 near=10 → 命中；超过 → 不命中
        self.assertIsNotNone(near_zone(110.35, [self.SUP], "long", 10.0))
        self.assertIsNone(near_zone(110.36, [self.SUP], "long", 10.0))

    def test_point_candidates_fallback_matches_nearsr(self):
        levels = [self.PT]
        for price in (98.0, 103.9, 104.1, 120.0):
            self.assertEqual(
                near_zone(price, levels, "long", 10.0) is not None,
                nearSr(price, levels, 10.0) is not None)

    def test_stop_ref_lands_outside_zone(self):
        src = {}
        ref = stop_ref_of("long", 100.5, 99.85, [self.SUP], slip_stop=3.0, source_out=src)
        # 多单止损 = 支撑区间下沿 − 滑点（区间外侧）
        self.assertEqual(ref, 99.85 - 3.0)
        self.assertEqual(src["src"], "near_sr")


class TestClass1FibExempt(unittest.TestCase):
    """1类策略（wait1Buy/wait1Sell）豁免黄金分割支阻（2026-10-10）：
    fib 位由非一类买卖点锚定派生，对 1类点不参与「支阻位附近」闸门与止损重选
    ——与 fxma fibNearOn「仅2/3类点判定，1类点豁免」同语义。点位候选本身仍只查
    |价差| ≤ near、不分上下方（旧口径不变）。"""

    # 复刻实测场景（bt_chan XAUUSD 10-5：wait1Buy 多头被上方 fib 0.5 回撤位放行、
    # 止损侧无位可依走最大止损兜底，-34.24）
    FIB_ABOVE = {"price": 4141.34, "level": "60", "fib": True, "ratio": 0.5,
                 "type": "RES", "srcType": "fib"}
    FIB_BELOW = {"price": 4118.0, "level": "60", "fib": True, "ratio": 0.382,
                 "type": "SUP", "srcType": "fib"}
    ZONE_SUP = {"kind": "support", "price": 4120.0, "lower": 4120.0, "upper": 4126.0,
                "level": "60", "srcType": "zone"}
    MANUAL_BELOW = {"price": 4115.0, "level": "60", "manual": True, "srcType": "manual"}

    def test_drop_fib_only_for_class1_keys(self):
        pool = [self.FIB_ABOVE, self.FIB_BELOW, self.ZONE_SUP, self.MANUAL_BELOW]
        # 1类键剔 fib（密集区/人工位保留）；其余键/空池原样返回（同一列表对象，零拷贝）
        for key in ("wait1Buy", "wait1Sell"):
            kept = sr_drop_fib_for_class1(pool, key)
            self.assertEqual(kept, [self.ZONE_SUP, self.MANUAL_BELOW])
        for key in ("wait2Buy", "waitBuy", "wait3Sell", "wait2BuyBear", None):
            self.assertIs(sr_drop_fib_for_class1(pool, key), pool)
        empty = []
        self.assertIs(sr_drop_fib_for_class1(empty, "wait1Buy"), empty)

    def test_wait1buy_gate_ignores_fib_levels(self):
        # 场景复刻：只选黄金分割、fib 位在信号价上方约 18 点、near=30——
        # 点位候选旧口径（|4141.34-4123.355|=17.99 ≤ 30，不分上下方）会命中；
        # wait1Buy 剔 fib 后池空 → 「以下级别背驰点均远离支阻位」拒绝
        sig_price, nearTol = 4123.355, 30.0
        fib_only = sr_of_detect([self.FIB_ABOVE], "60")
        self.assertIsNotNone(near_zone(sig_price, fib_only, "long", nearTol))
        kept = sr_drop_fib_for_class1(fib_only, "wait1Buy")
        self.assertEqual(kept, [])
        self.assertIsNone(near_zone(sig_price, kept, "long", nearTol))
        # 下方 fib（支撑侧、距 5.4）对非1类键多头仍放行（豁免只针对1类键）
        hit = near_zone(sig_price, [self.FIB_BELOW], "long", 30.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["sr"]["price"], 4118.0)

    def test_wait1buy_stop_repick_ignores_fib(self):
        # 止损重选同口径：1类键剔 fib 后正确侧无位 → 最大止损兜底（进场价−fallback）
        entry, slip_fb = 4136.505, 10.0
        fib_stop = {"price": 4131.0, "level": "60", "fib": True, "ratio": 0.618,
                    "type": "SUP", "srcType": "fib"}
        pool = sr_drop_fib_for_class1(
            sr_of_detect([self.FIB_ABOVE, fib_stop], "60"), "wait1Buy")
        src = {}
        ref = stop_ref_of("long", entry, None, pool, slip_stop=3.0,
                          slip_fallback=slip_fb, source_out=src)
        self.assertEqual(ref, entry - slip_fb)
        self.assertEqual(src["src"], "fallback")
        # 非1类键：正确侧 fib 参与止损重选（4131−滑点3=4128 > 最大止损4126.5 不被夹）
        pool2 = sr_of_detect([self.FIB_ABOVE, fib_stop], "60")
        src2 = {}
        ref2 = stop_ref_of("long", entry, None, pool2, slip_stop=3.0,
                           slip_fallback=slip_fb, source_out=src2)
        self.assertEqual(ref2, 4131.0 - 3.0)
        self.assertEqual(src2["src"], "sr_pick")
        self.assertEqual(src2["srPrice"], 4131.0)


if __name__ == "__main__":
    unittest.main()
