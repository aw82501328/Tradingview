import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志

import gzip
import json
import unittest
from pathlib import Path
from . import chan_core as c

# 9-4 本案真实切片（2026-06-30 ~ 09-13 的 15m K线，含 09-04 12:00/14:45 双底结构）
DATA = json.loads(gzip.decompress(
    Path(__file__).with_name('fixtures').joinpath('inner_extreme_xauusd_20260904.json.gz').read_bytes()))
T_0900, T_1200, T_1445, T_1545 = 1788483600, 1788494400, 1788504300, 1788507900


def _build_15m():
    trimmed = c.markWickBars(DATA)
    merged = c.mergeBars(trimmed)
    bis = c.buildBi(c.findFractals(merged), merged, c.calcATR(DATA, 14), c.calcMACD(DATA),
                    None, c.nearDoubleOn('15'), '15')
    bis = c.fixBiExtremes(bis, merged) or bis
    return c.extendLastBi(bis, trimmed)


def _mk_bar(i, high, low, body_hi=None, body_lo=None):
    """合成合并K：open/close 供实体口径（_nearBodyPx）与分型范围检查。"""
    bh = body_hi if body_hi is not None else (high + low) / 2 + 0.5
    bl = body_lo if body_lo is not None else (high + low) / 2 - 0.5
    return {"mergedIdx": i, "time": i * 100, "high": high, "low": low,
            "open": bl, "close": bh}


def _mk_frac(i, kind, hi, lo, t=None):
    return {"mergedIdx": i, "type": kind, "high": hi, "low": lo,
            "time": t if t is not None else i * 100}


def _run_steps(bars, fracs, near_double=False):
    # atr=0：关闭跳空成笔通道（gapThreshold=0），合成序列只走分型/间隔规则
    ctx = c.BiBuildCtx(bars, 0.0, [], nearDouble=near_double, res=None)
    head = None
    for f in fracs:
        head = c.biStep(ctx, head, f)
    return c.biListFromHead(head)


def _g1_bars():
    """同型替换场景合成序列：T0@0(100) B1@4(90/实体91) T2@9(95) B3@12(87/实体87.5) T4@17(97)。
    B3 与 T2 间隔 3 被丢、B1→T2 恰成有效笔且反弹非浅（12:00 案镜像），
    T4 同型替换 T2 时新配对 (B1, T4) 笔内藏 87 < 90。idx18 为 T4 右邻（分型三根用）。"""
    bars = []
    for i in range(19):
        if i == 0:
            bars.append(_mk_bar(i, 100, 97, 99.5, 98.5))
        elif i == 4:
            bars.append(_mk_bar(i, 93, 90, 92.5, 91.0))
        elif i == 9:
            bars.append(_mk_bar(i, 95, 93, 94.5, 93.5))
        elif i == 12:
            bars.append(_mk_bar(i, 90, 87, 89.5, 87.5))
        elif i == 17:
            bars.append(_mk_bar(i, 97, 94, 96.5, 95.0))
        else:
            bars.append(_mk_bar(i, 92 + (i % 3) * 0.2, 91 + (i % 3) * 0.2))
    return bars


class InnerExtremeGuardTests(unittest.TestCase):
    def setUp(self):
        c.reset_cfg()

    def tearDown(self):
        c.reset_cfg()

    def test_real_9_4_endpoint_lands_at_1445(self):
        # 守卫开：下跌笔 09-04 09:00 4487.55 → 14:45 4460.155（笔内极值落地），
        # 上涨笔 14:45 → 15:45 4490.90。
        bis = _build_15m()
        down = next(b for b in bis if b["startTime"] == T_0900 and b["type"] == "down")
        self.assertEqual(down["endTime"], T_1445)
        self.assertAlmostEqual(down["endPrice"], 4460.155, places=3)
        up = next(b for b in bis if b["startTime"] == T_1445)
        self.assertEqual(up["type"], "up")
        self.assertEqual(up["endTime"], T_1545)
        self.assertAlmostEqual(up["endPrice"], 4490.90, places=2)

    def test_guard_off_reverts_to_old_1200(self):
        # 守卫关：逐位回退旧口径——端点停在 12:00@4465.49（上涨笔内藏 14:45 更低点）。
        try:
            c.apply_cfg({"innerExtremeGuardOn": False})
            bis = _build_15m()
            down = next(b for b in bis if b["startTime"] == T_0900 and b["type"] == "down")
            self.assertEqual(down["endTime"], T_1200)
            self.assertAlmostEqual(down["endPrice"], 4465.49, places=2)
            up = next(b for b in bis if b["startTime"] == T_1200)
            self.assertEqual(up["endTime"], T_1545)
        finally:
            c.reset_cfg()

    def test_same_type_replace_landing(self):
        # 合成场景①：T4 同型替换 T2 时落地被吞 B3 极值为端点 → [T0, B3@12, T4]。
        bars = _g1_bars()
        fracs = [_mk_frac(0, "top", 100, 97), _mk_frac(4, "bottom", 93, 90),
                 _mk_frac(9, "top", 95, 93), _mk_frac(12, "bottom", 90, 87),
                 _mk_frac(17, "top", 97, 94)]
        res = _run_steps(bars, fracs)
        self.assertEqual([f["mergedIdx"] for f in res], [0, 12, 17])
        self.assertEqual([f["type"] for f in res], ["top", "bottom", "top"])
        self.assertAlmostEqual(res[1]["low"], 87, places=6)

    def test_same_type_replace_guard_off_keeps_old(self):
        bars = _g1_bars()
        fracs = [_mk_frac(0, "top", 100, 97), _mk_frac(4, "bottom", 93, 90),
                 _mk_frac(9, "top", 95, 93), _mk_frac(12, "bottom", 90, 87),
                 _mk_frac(17, "top", 97, 94)]
        try:
            c.apply_cfg({"innerExtremeGuardOn": False})
            res = _run_steps(bars, fracs)
            # 旧口径：B1 保留为端点（上涨笔内藏 87）
            self.assertEqual([f["mergedIdx"] for f in res], [0, 4, 17])
        finally:
            c.reset_cfg()

    def test_locked_prev_blocks_landing(self):
        # 锁定端点（区间套优先）不参与：B1.locked → 维持旧直接替换。
        bars = _g1_bars()
        b1 = _mk_frac(4, "bottom", 93, 90)
        b1["locked"] = True
        fracs = [_mk_frac(0, "top", 100, 97), b1,
                 _mk_frac(9, "top", 95, 93), _mk_frac(12, "bottom", 90, 87),
                 _mk_frac(17, "top", 97, 94)]
        res = _run_steps(bars, fracs)
        self.assertEqual([f["mergedIdx"] for f in res], [0, 4, 17])

    def test_bridge_to_k_gap_too_small_falls_back(self):
        # 拆不开（bridge→k 间隔 3 且无 MACD 变色）→ 维持原直接替换（旧行为）。
        bars = _g1_bars()
        fracs = [_mk_frac(0, "top", 100, 97), _mk_frac(4, "bottom", 93, 90),
                 _mk_frac(9, "top", 95, 93), _mk_frac(12, "bottom", 90, 87),
                 _mk_frac(15, "top", 97, 94)]
        res = _run_steps(bars, fracs)
        self.assertEqual([f["mergedIdx"] for f in res], [0, 4, 15])

    def test_near_equal_body_tolerance_not_touched(self):
        # 影线差/实体差未双超容差（近等族语义内）→ 守卫不触发，维持旧直接替换
        # （例：60m 2026-08-06 取后底 4229.875 vs 前底影线 4223.505 实体差 0.915）。
        bars = _g1_bars()
        # B3 改为 88.5：wick差 1.5 ≤ 2 → 非真违例
        bars[12] = _mk_bar(12, 90, 88.5, 89.5, 89.0)
        fracs = [_mk_frac(0, "top", 100, 97), _mk_frac(4, "bottom", 93, 90),
                 _mk_frac(9, "top", 95, 93), _mk_frac(12, "bottom", 90, 88.5),
                 _mk_frac(17, "top", 97, 94)]
        res = _run_steps(bars, fracs)
        self.assertEqual([f["mergedIdx"] for f in res], [0, 4, 17])

    def test_push_start_side_landing(self):
        # 合成场景②（push 起点侧）：T0@0 B1@4(90)，中间 idx8 更低 85（双超容差），
        # T2@12 push 时起点被替换为 bridge@8 → [T0, bridge@8, T2]。
        bars = []
        for i in range(14):
            if i == 0:
                bars.append(_mk_bar(i, 100, 97, 99.5, 98.5))
            elif i == 4:
                bars.append(_mk_bar(i, 93, 90, 92.5, 91.0))
            elif i == 8:
                bars.append(_mk_bar(i, 92, 85, 91.5, 86.0))
            elif i == 12:
                bars.append(_mk_bar(i, 96, 93, 95.5, 94.0))
            else:
                bars.append(_mk_bar(i, 92 + (i % 3) * 0.2, 91 + (i % 3) * 0.2))
        fracs = [_mk_frac(0, "top", 100, 97), _mk_frac(4, "bottom", 93, 90),
                 _mk_frac(12, "top", 96, 93)]
        res = _run_steps(bars, fracs)
        self.assertEqual([f["mergedIdx"] for f in res], [0, 8, 12])
        self.assertEqual(res[1]["type"], "bottom")
        self.assertAlmostEqual(res[1]["low"], 85, places=6)


if __name__ == "__main__":
    unittest.main()
