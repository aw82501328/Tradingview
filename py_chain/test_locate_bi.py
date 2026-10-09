"""locate_bi 单测：大周期覆盖检测、逐级区间套锁定、断口治理与路由门控。

compute_lower_chain 依赖 bars.db 与参数中心，均以假件替换（load_store /
chan_cfg_effective）；画图（draw_level/wait_resolution）需真图表不在单测范围。
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from . import chan_core, locate_bi
from .signal_locator import _upper_res_of


def _legs_bars(sec, t0, legs, per_leg=8):
    """合成K线：每腿 per_leg 根单调K线，腿端点为精确极值（无影线）。"""
    bars, t = [], t0
    for a, b in legs:
        for k in range(per_leg):
            o = a + (b - a) * k / per_leg
            cl = a + (b - a) * (k + 1) / per_leg
            bars.append({"time": t, "open": o, "high": max(o, cl),
                         "low": min(o, cl), "close": cl})
            t += sec
    return bars


def _bi(stype, st, sp, et, ep, **kw):
    return {"type": stype, "startTime": st, "startPrice": sp,
            "endTime": et, "endPrice": ep, "span": abs(ep - sp),
            "rawCount": kw.get("rawCount", 8), "gapLocked": kw.get("gapLocked", False),
            "macdCross": kw.get("macdCross", False)}


class FindCoveringBiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.symbol = "TEST:SYMBOL"
        self.path = os.path.join(self.dir, "bis_TEST_SYMBOL.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"symbol": self.symbol, "periods": {
                "240": [_bi("up", 0, 100, 1000, 120), _bi("down", 1000, 120, 2000, 90)]}}, f)

    def test_cover_and_not_cover(self):
        hit = locate_bi.find_covering_bi(self.symbol, "240", 500, cache_dir=self.dir)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["startTime"], 0)
        self.assertIsNone(locate_bi.find_covering_bi(self.symbol, "240", 2500, cache_dir=self.dir))

    def test_boundary_takes_later_stroke(self):
        # t 恰在两笔交界（前笔endTime==后笔startTime）→ 取后一根
        hit = locate_bi.find_covering_bi(self.symbol, "240", 1000, cache_dir=self.dir)
        self.assertEqual(hit["type"], "down")

    def test_missing_cache_or_period(self):
        self.assertIsNone(locate_bi.find_covering_bi(self.symbol, "60", 500, cache_dir=self.dir))
        self.assertIsNone(locate_bi.find_covering_bi("NO:SUCH", "240", 500, cache_dir=self.dir))
        os.unlink(self.path)
        self.assertIsNone(locate_bi.find_covering_bi(self.symbol, "240", 500, cache_dir=self.dir))


class AlignBiToUpperTests(unittest.TestCase):
    def test_snapshot_time_and_price_when_less_extreme(self):
        lower = [_bi("up", 100, 10.0, 200, 20.0), _bi("down", 200, 20.0, 300, 12.0)]
        upper = [_bi("up", 105, 10.0, 195, 21.0)]
        out = chan_core.alignBiToUpper(lower, upper, 3600)
        # 下级顶 20 漏掉上级真顶 21 → 时间+价格快照到上级（195, 21）
        self.assertEqual(out[0]["endTime"], 195)
        self.assertEqual(out[0]["endPrice"], 21.0)
        self.assertEqual(out[1]["startTime"], 195)
        self.assertEqual(out[1]["startPrice"], 21.0)
        self.assertEqual(out[1]["endPrice"], 12.0)
        self.assertAlmostEqual(out[0]["span"], 11.0)

    def test_price_only_when_equally_extreme(self):
        lower = [_bi("up", 100, 10.0, 200, 20.0), _bi("down", 200, 20.0, 300, 12.0)]
        upper = [_bi("up", 105, 10.0, 200, 20.0)]
        out = chan_core.alignBiToUpper(lower, upper, 3600)
        # 下级已找到相同极值 → 只对齐价格，保留下级更精确的时间
        self.assertEqual(out[0]["endTime"], 200)
        self.assertEqual(out[0]["endPrice"], 20.0)

    def test_ghost_endpoint_defense(self):
        lower = [_bi("up", 100, 10.0, 200, 20.0), _bi("down", 200, 20.0, 300, 12.0)]
        upper = [_bi("up", 105, 10.0, 195, 25.0)]
        # 本级局部K线最高 20.5：上级顶 25 是跨周期聚合差异的幽灵端点 → 跳过对齐
        bars = [{"time": t, "high": 20.5, "low": 10.0} for t in range(105, 105 + 3600, 900)]
        out = chan_core.alignBiToUpper(lower, upper, 3600, bars)
        self.assertEqual(out[0]["endTime"], 200)
        self.assertEqual(out[0]["endPrice"], 20.0)

    def test_no_match_beyond_tolerance_keeps_original(self):
        lower = [_bi("up", 100, 10.0, 200, 20.0), _bi("down", 200, 20.0, 300, 12.0)]
        upper = [_bi("up", 100 + 7200, 10.0, 200 + 7200, 21.0)]  # 超出 3600s 容差
        out = chan_core.alignBiToUpper(lower, upper, 3600)
        self.assertEqual(out[0]["endPrice"], 20.0)


class MergeAlignedGapsTests(unittest.TestCase):
    def test_merge_same_direction_and_refilter(self):
        bis = [_bi("up", 0, 10.0, 100, 20.0), _bi("up", 100, 20.0, 200, 30.0),
               _bi("down", 200, 30.0, 300, 5.0)]
        out = locate_bi._merge_aligned_gaps(bis, 5.0)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["type"], "up")
        self.assertEqual(out[0]["startTime"], 0)
        self.assertEqual(out[0]["endPrice"], 30.0)
        self.assertEqual(out[0]["rawCount"], 16)
        self.assertAlmostEqual(out[0]["span"], 20.0)

    def test_bridge_below_threshold_removed(self):
        bis = [_bi("up", 0, 10.0, 100, 20.0), _bi("up", 100, 20.0, 200, 22.0),
               _bi("down", 200, 22.0, 300, 5.0)]
        out = locate_bi._merge_aligned_gaps(bis, 5.0)  # 合并后桥接幅度 12 ≥ 5 保留
        self.assertEqual(len(out), 2)
        out2 = locate_bi._merge_aligned_gaps(
            [_bi("up", 0, 10.0, 100, 20.0), _bi("up", 100, 20.0, 200, 21.0),
             _bi("down", 200, 21.0, 300, 5.0)], 15.0)
        # 合并后 up 幅度 11 < 15 清除；down 幅度 16 ≥ 15 保留
        self.assertEqual([b["type"] for b in out2], ["down"])


class UpperResRoutingTests(unittest.TestCase):
    def test_periodx_priority_and_landed_fallback(self):
        self.assertEqual(_upper_res_of({"periodX": "240"}, "15"), "240")
        self.assertEqual(_upper_res_of({"periodX": "60"}, "15"), "60")
        self.assertEqual(_upper_res_of({}, "15"), "60")
        self.assertEqual(_upper_res_of({}, "60"), "240")
        self.assertEqual(_upper_res_of({}, "3"), "")
        self.assertEqual(_upper_res_of({"periodX": "15"}, "15"), "60")


class ComputeLowerChainTests(unittest.TestCase):
    SEC15 = 900

    def setUp(self):
        # t0 前留 3 腿（24根）分型缓冲；覆盖笔 = 60级 up：95.0@t0 → 112.0@t0+24*900
        self.t0 = 1_700_000_000 // 900 * 900
        legs15 = [(100, 97), (97, 99), (99, 95), (95, 110), (110, 95.5),
                  (95.5, 112), (112, 96), (96, 111), (111, 97)]
        self.bars15 = _legs_bars(self.SEC15, self.t0 - 24 * self.SEC15, legs15)
        # 3m 锯齿需铺满整个绘制窗口（t0 前 24 根缓冲 → t0+80 根 15m 之后）
        need3 = 80 * self.SEC15 + 24 * 180
        pairs = int(need3 // (16 * 180)) + 2
        legs3 = [(96, 99), (99, 95)] + [(95, 108), (108, 95)] * pairs
        self.bars3 = _legs_bars(180, self.t0 - 24 * 180, legs3)
        self.cover = _bi("up", self.t0, 95.0, self.t0 + 24 * self.SEC15, 112.0)
        store = {"15": self.bars15, "3": self.bars3}

        def fake_load(symbol, periods=None, from_ts=0, to_ts=None):
            out = {}
            for res in periods or []:
                if res not in store:
                    raise ValueError(f"本地存储没有品种 {symbol} 的 {res} 周期数据")
                out[res] = [b for b in store[res]
                            if b["time"] >= int(from_ts or 0)
                            and (to_ts is None or b["time"] <= int(to_ts))]
                if not out[res]:
                    raise ValueError(f"{res} 级在窗口内无数据")
            return out

        self._load = patch("py_chain.data_store.load_store", side_effect=fake_load)
        self._load.start()
        self._cfg = patch("py_chain.param_center.chan_cfg_effective", return_value={})
        self._cfg.start()
        self.addCleanup(self._load.stop)
        self.addCleanup(self._cfg.stop)

    def test_chain_levels_and_upper_lock(self):
        entry = self.t0 + 20 * self.SEC15
        t_to = self.t0 + 60 * self.SEC15
        out = locate_bi.compute_lower_chain("TEST:SYMBOL", "60", self.cover, entry, t_to)
        self.assertEqual(sorted(out["levels"]), ["15", "3"])
        for res, bis in out["levels"].items():
            self.assertGreater(len(bis), 0)
            for a, b in zip(bis, bis[1:]):
                self.assertNotEqual(a["type"], b["type"])
                self.assertEqual(a["endTime"], b["startTime"])
        # 首级（15m）区间套锁定覆盖笔两端点：95.0@t0 与 112.0 快照复现
        bis15 = out["levels"]["15"]
        locked_bottom = [b for b in bis15
                         if b["startPrice"] == 95.0 and abs(b["startTime"] - self.t0) <= 3600]
        self.assertTrue(locked_bottom, f"未锁定覆盖笔底端点: {bis15[:3]}")
        locked_top = [b for b in bis15 if b["endPrice"] == 112.0]
        self.assertTrue(locked_top, f"未锁定覆盖笔顶端点: {bis15[:3]}")

    def test_draw_window_filters_early_strokes(self):
        entry = self.t0 + 54 * self.SEC15
        t_to = self.t0 + 60 * self.SEC15
        out = locate_bi.compute_lower_chain("TEST:SYMBOL", "60", self.cover, entry, t_to)
        draw_from = max(self.cover["startTime"], entry - locate_bi.DRAW_BARS * self.SEC15)
        for b in out["levels"]["15"]:
            self.assertGreaterEqual(b["endTime"], draw_from)

    def test_missing_level_blocks_deeper_chain(self):
        # 240 链第一级 60 无数据 → levels 空、errors 记 60，不继续 15/3
        out = locate_bi.compute_lower_chain("TEST:SYMBOL", "240", self.cover,
                                            self.t0 + 20 * self.SEC15, self.t0 + 60 * self.SEC15)
        self.assertEqual(out["levels"], {})
        self.assertIn("60", out["errors"])


if __name__ == "__main__":
    unittest.main()
