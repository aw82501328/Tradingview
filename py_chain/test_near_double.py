import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志

import copy
import datetime
import gzip
import json
import unittest
from unittest.mock import patch
from pathlib import Path
from . import chan_core as c
from .backtest import BacktestEngine, build_bis

DATA = json.loads(gzip.decompress(Path(__file__).with_name('fixtures').joinpath('near_double_xauusd_20260910.json.gz').read_bytes()))
OLD, NEW = 1786032000, 1786060800


def _chain_build(switches=None):
    """复刻 build_bis 的外→内链式输入（上级 lockedPivots + 后处理）直构各周期笔。
    switches: {res: nearDouble}，缺省按 nearDoubleOn(res)。
    2026-10-01 结构修正（标准低低/实体终点侧/平台取后回退）后上级端点变化会经
    锁定传导到下级，无锁定直构不再与引擎一致，基线必须复刻链式输入。"""
    out = {}
    prev = None
    for res in ['D', '240', '60', '15', '3']:
        bars = DATA.get(res)
        if not bars or len(bars) < 6:
            prev = out.get(res)
            continue
        trimmed = c.markWickBars(bars)
        merged = c.mergeBars(trimmed)
        near = c.nearDoubleOn(res) if switches is None else switches.get(res, c.nearDoubleOn(res))
        locks = c.lockedPivotsOf(prev) if prev else None
        bis = c.buildBi(c.findFractals(merged), merged, c.calcATR(bars, 14), c.calcMACD(bars),
                        locks, near, res)
        bis = c.extendLastBi(c.fixBiExtremes(bis, merged) or bis, trimmed)
        if bis:
            out[res] = bis
        prev = out.get(res)
    return out


class NearDoubleTests(unittest.TestCase):
    def test_frozen_target_and_unchanged_other_periods(self):
        # 60m 冻结目标（8-7 08:00 4229.875）仍在，且引擎输出与链式复构基线逐笔一致。
        # 3m/15m 近等开关关掉以免下级上下文改笔数（与旧口径同因）。
        # 2026-10-02 新规则下的到达路径：00:00 前底影线更极端（4223.505<4230.46）走
        # 更极端替换、不带 nearDouble 封顶（先影线闸门），08:00 后底实体 4232.90 低于
        # 前底实体 4233.815（diff=-0.915）→ 直接后移。
        try:
            c.apply_cfg({"nearDouble3": False, "nearDouble15": False})
            new = build_bis(DATA)
            base = _chain_build()
            self.assertEqual(sorted(new.keys()), sorted(base.keys()))
            for res in base:
                self.assertEqual(new[res], base[res], f'{res} 引擎与链式基线不一致')
            target = next(b for b in new['60'] if b['endTime'] == NEW)
            self.assertEqual(target['endPrice'], 4229.875)
            self.assertEqual(next(b for b in new['60'] if b['startTime'] == NEW)['type'], 'up')
        finally:
            c.reset_cfg()

    def test_near_double_on_helper(self):
        # 默认：五周期都开（3m/15m 于 2026-09-28 收成默认）
        self.assertTrue(c.nearDoubleOn('3') and c.nearDoubleOn('15'))
        self.assertTrue(c.nearDoubleOn('60') and c.nearDoubleOn('240') and c.nearDoubleOn('D'))
        # barSec 秒数形式（buildStructureContext 只有秒数）与别名
        self.assertTrue(c.nearDoubleOn(180) and c.nearDoubleOn(900))
        self.assertTrue(c.nearDoubleOn(3600) and c.nearDoubleOn(14400) and c.nearDoubleOn(86400))
        self.assertTrue(c.nearDoubleOn('1H') and c.nearDoubleOn('4H') and c.nearDoubleOn('1D'))
        # 五周期之外失败安全：'30S'/'5'/'30'/'W'/未知秒数/bool 一律 False
        for bad in ('30S', '5', '30', 'W', 30, 0, True, None):
            self.assertFalse(c.nearDoubleOn(bad), repr(bad))
        # 开关经 apply_cfg 翻转（CHAN_CFG 进程级全局，finally 复原）
        try:
            c.apply_cfg({'nearDouble60': False})
            self.assertFalse(c.nearDoubleOn('60') or c.nearDoubleOn(3600))
            self.assertTrue(c.nearDoubleOn('240'))
        finally:
            c.reset_cfg()
        self.assertTrue(c.nearDoubleOn('60'))

    def test_switch_off_reverts_60m(self):
        # 关 nearDouble60 → 60m 笔与「nearDouble=False 链式直构」基线逐笔相等
        # （引擎/批量同口径；2026-10-01 起基线须带上级锁定与 15m 上下文）
        baseline = _chain_build({'60': False})['60']
        try:
            c.apply_cfg({'nearDouble60': False})
            self.assertEqual(build_bis(DATA)['60'], baseline)
        finally:
            c.reset_cfg()

    def test_switch_on_15m_restructures(self):
        # 开 nearDouble15 → 15m 结构重排（关掉时与无开关构建对照；不断言具体笔数，
        # 结构对开关语义敏感、对计数不承诺）
        merged = c.mergeBars(c.markWickBars(DATA['15']))
        try:
            c.apply_cfg({'nearDouble15': False})
            default = c.buildBi(c.findFractals(merged), merged, c.calcATR(DATA['15'], 14), c.calcMACD(DATA['15']), None, False)
            c.apply_cfg({'nearDouble15': True})
            altered = c.buildBi(c.findFractals(merged), merged, c.calcATR(DATA['15'], 14), c.calcMACD(DATA['15']), None, True)
            self.assertNotEqual(altered, default)
        finally:
            c.reset_cfg()

    def test_fixed_tolerance_boundary(self):
        # 固定容差边界：thr = nearDoubleFixed（2026-10-02 起取消 ATR 项/价格比例项）。
        # 合成K线验证：B1 底(实体 bb1) 与 B2 底(实体 bb2=bb1+D) 近等平台，中间 T2 顶
        # 反弹不成笔（两侧 gap=1）、回调深度 0.8 » thr；fixed < D 时不后移（上涨笔
        # 起点停 B1），fixed ≥ D 后移到 B2。影线闸门：B2 low 5.30 > B1 low 4.90，
        # 影线不满足才比实体。
        def bar_(t, h, l):
            m = (h + l) / 2.0
            return {"time": t * 3600, "open": m, "high": h, "low": l, "close": m}

        bars = [
            bar_(0, 5.10, 4.80), bar_(1, 5.00, 4.40),   # B0 底
            bar_(2, 5.50, 4.60), bar_(3, 6.00, 5.00), bar_(4, 6.30, 5.20),
            bar_(5, 6.60, 5.50),                          # T1 顶
            bar_(6, 6.20, 5.30), bar_(7, 5.80, 5.10), bar_(8, 5.60, 4.95),
            bar_(9, 5.35, 4.90),                          # B1 底（实体 5.125）
            bar_(10, 5.70, 5.35),                         # T2 顶（反弹不成笔）
            bar_(11, 5.65, 5.30),                         # B2 底（实体 5.475，D=0.35）
            bar_(12, 5.90, 5.40), bar_(13, 6.30, 5.65), bar_(14, 6.80, 5.90),
            bar_(15, 7.40, 6.20),                         # T3 顶
            bar_(16, 6.90, 6.00),
        ]
        trimmed = c.markWickBars(bars)
        merged = c.mergeBars(trimmed)
        f = c.findFractals(merged)
        b1 = next(x for x in f if x['type'] == 'bottom' and abs(x['low'] - 4.90) < 1e-9)
        b2 = next(x for x in f if x['type'] == 'bottom' and abs(x['low'] - 5.30) < 1e-9)
        t3 = next(x for x in f if x['type'] == 'top' and abs(x['high'] - 7.40) < 1e-9)
        diff = merged[b2['mergedIdx']]['bodyBottom'] - merged[b1['mergedIdx']]['bodyBottom']
        self.assertAlmostEqual(diff, 0.35, places=9)
        atr = c.calcATR(bars, 14)
        macd = c.calcMACD(bars)
        try:
            for fixed, expected_start in [(0.0, b1['time']), (diff - 1e-9, b1['time']),
                                          (diff, b2['time']), (diff + 0.10, b2['time'])]:
                c.apply_cfg({'nearDoubleFixed': fixed})
                bis = c.buildBi(copy.deepcopy(f), merged, atr, macd, None, True)
                stroke = next(x for x in bis if x['endTime'] == t3['time'])
                self.assertEqual(stroke['startTime'], expected_start, f'nearDoubleFixed={fixed}')
        finally:
            c.reset_cfg()

    def test_body_not_lower_direct_shift(self):
        # 2026-10-02 新语义：影线不满足（后影线未触发更极端替换）时比实体——
        # 后实体价更极端（底更低/顶更高，diff < 0）在 fixed=0 下也直接后移；
        # nearDouble 关闭则停在原端点。底=平台路径（同类型分支），顶=价格镜像(−x)。
        def bar_(t, h, l):
            m = (h + l) / 2.0
            return {"time": t * 3600, "open": m, "high": h, "low": l, "close": m}

        bars_bot = [
            bar_(0, 5.10, 4.80), bar_(1, 5.00, 4.40),   # B0 底
            bar_(2, 5.50, 4.60), bar_(3, 6.00, 5.00), bar_(4, 6.30, 5.20),
            bar_(5, 6.60, 5.50),                          # T1 顶
            bar_(6, 6.20, 5.30), bar_(7, 5.80, 5.10), bar_(8, 5.60, 4.95),
            bar_(9, 5.45, 4.90),                          # B1 底（实体 5.175，影线 4.90）
            bar_(10, 5.70, 5.35),                         # T2 顶（反弹不成笔）
            bar_(11, 5.25, 5.00),                         # B2 底（实体 5.125 更低，low 5.00 > 4.90）
            bar_(12, 5.90, 5.40), bar_(13, 6.30, 5.65), bar_(14, 6.80, 5.90),
            bar_(15, 7.40, 6.20),                         # T3 顶
            bar_(16, 6.90, 6.00),
        ]
        bars_top = [dict(time=b['time'], open=-b['open'], high=-b['low'],
                         low=-b['high'], close=-b['close']) for b in bars_bot]

        def end_at(bars, cfg=None, near=True):
            trimmed = c.markWickBars(bars)
            merged = c.mergeBars(trimmed)
            try:
                if cfg:
                    c.apply_cfg(cfg)
                bis = c.buildBi(c.findFractals(merged), merged,
                                c.calcATR(bars, 14), c.calcMACD(bars), None, near)
                return [x['endTime'] for x in bis]
            finally:
                if cfg:
                    c.reset_cfg()

        # 底：fixed=0 → 下跌笔终点后移到 B2(11h)；nearDouble 关 → 停 B1(9h)
        self.assertIn(11 * 3600, end_at(bars_bot, cfg={'nearDoubleFixed': 0.0}))
        self.assertIn(9 * 3600, end_at(bars_bot, near=False))
        # 顶（镜像）：fixed=0 → 上涨笔终点后移到后顶(11h)；nearDouble 关 → 停前顶(9h)
        self.assertIn(11 * 3600, end_at(bars_top, cfg={'nearDoubleFixed': 0.0}))
        self.assertIn(9 * 3600, end_at(bars_top, near=False))

    def test_engine_rewind_and_replay_prefix(self):
        # （15m 双动能确认已删，2026-10-02）保留引擎回放语义：决策拍无未来数据、
        # rewind 不越界、补 15m 触发重放且重放后前缀稳定。08:00 端点需等 09:00
        # 60m K 确认分型，决策时刻（cut=NEW）不出现。
        engine = BacktestEngine(DATA, periods=['60', '15', '3'])
        engine._advance_cut(NEW)
        cuts = dict(engine._cut)
        for res in ['15', '60']:
            engine._rewind_res(res)
        self.assertEqual(engine._cut, cuts)
        self.assertFalse(any(b['endTime'] == NEW and b['endPrice'] == 4229.875 for b in engine._bis['60']))
        engine.append_bars('15', [DATA['15'][-1]])
        self.assertEqual(engine._replay_needed, {'15', '60'})
        with patch.object(engine, '_rebuild_chain'), patch.object(engine, '_step_execute', return_value={}) as execute:
            engine.step_to(NEW, execute=True)
            execute.assert_called_once_with(NEW)
        self.assertEqual(engine._cut, cuts)
        self.assertFalse(engine._replay_needed)

    def test_incremental_period_order_and_closed_prefix(self):
        snapshots = []
        for periods in (['60', '15', '3'], ['3', '15', '60']):
            engine = BacktestEngine(DATA, periods=periods)
            for cutoff in (NEW, NEW+2700, NEW+3600, NEW+7200):
                engine._advance_cut(cutoff)
                for res in periods:
                    self.assertTrue(all(b['time'] + c.intervalSecOf(res) <= cutoff
                                        for b in engine.bars[res]['_list'][:engine._cut[res]]))
                prefix = {res: engine.bars[res]['_list'][:engine._cut[res]] for res in periods}
                engine.resync_all()
                self.assertEqual(engine._bis['60'], build_bis(prefix)['60'])
                if cutoff < NEW+3600:
                    self.assertFalse(any(b['endTime'] == NEW and b['endPrice'] == 4229.875 for b in engine._bis['60']))
            snapshots.append(engine._bis['60'])
        self.assertEqual(*snapshots)


class NearDoubleReboundTests(unittest.TestCase):
    """近等后顶/后底（反弹不成笔）取后（2026-09-26）。

    案例：60m 2026-09-18 顶 4399.67(15:00+8) → 底 4342.73(22:00+8，与23:00包含合并)
    → 顶 4397.045(9-19 01:00+8)，反弹腿仅 3 根合并K（gap=2<4）；近等差 2.625 ≤ thr。
    240 层平台规则已把 4h 顶后移到 9-19 01:00 → 60m 须复现（locked 路径=区间套落地）。"""

    FIX = json.loads(gzip.decompress(
        Path(__file__).with_name('fixtures').joinpath('near_double_rebound_xauusd_20260919.json.gz').read_bytes()))

    def _ts(self, s):
        return int(datetime.datetime.fromisoformat(f'2026-{s}+08:00').timestamp())

    def _build(self, locked=None, near_double=True, cfg=None):
        bars = self.FIX['60']
        merged = c.mergeBars(c.markWickBars(bars))
        try:
            if cfg:
                c.apply_cfg(cfg)
            bis = c.buildBi(c.findFractals(merged), merged, c.calcATR(bars, 14),
                            c.calcMACD(bars), locked, near_double)
            return c.fixBiExtremes(bis, merged)
        finally:
            if cfg:
                c.reset_cfg()

    def test_rebound_takes_later_top(self):
        start, old_t, new_t = (self._ts(x) for x in ('09-18T11:00:00', '09-18T15:00:00', '09-19T01:00:00'))
        # 默认（nearDoubleRebound=True + nearDoubleOn('60')）：端点后移到后顶
        up = next(b for b in self._build() if b['startTime'] == start)
        self.assertEqual((up['endTime'], up['endPrice']), (new_t, 4397.045))
        self.assertEqual(next(b for b in self._build() if b['startTime'] == new_t)['endPrice'], 4322.81)
        # 关 nearDoubleRebound 且无锁定 → 旧行为：端点停在前顶 4399.67
        up = next(b for b in self._build(cfg={'nearDoubleRebound': False}) if b['startTime'] == start)
        self.assertEqual((up['endTime'], up['endPrice']), (old_t, 4399.67))
        # 关 nearDoubleRebound 但 k 为上级锁定端点（如 240 层已后移的 4397.045）→ 区间套落地不受开关限制
        up = next(b for b in self._build(locked=[{'dir': 'top', 'price': 4397.045}],
                                         cfg={'nearDoubleRebound': False}) if b['startTime'] == start)
        self.assertEqual((up['endTime'], up['endPrice']), (new_t, 4397.045))
        # 无锁定且非近等周期（模拟 15m 口径）→ 不后移
        up = next(b for b in self._build(near_double=False) if b['startTime'] == start)
        self.assertEqual((up['endTime'], up['endPrice']), (old_t, 4399.67))

    def test_rebound_mirrors_for_double_bottom(self):
        # 10000-x 镜像（顶↔底互换、MACD 取反）。注：上影 _topCand/下影 _origLow 的处理
        # 本身不对称，镜像的中间结构与原窗口不同——只断言本规则本身：后底（较高、较晚，
        # 同类型 <= 替换不可能产生）成为端点 ⇔ 开关开。
        bars = [dict(time=b['time'], open=10000 - b['open'], high=10000 - b['low'],
                     low=10000 - b['high'], close=10000 - b['close']) for b in self.FIX['60']]
        new_t = self._ts('09-19T01:00:00')

        def build():
            merged = c.mergeBars(c.markWickBars(bars))
            return c.fixBiExtremes(c.buildBi(c.findFractals(merged), merged, c.calcATR(bars, 14),
                                             c.calcMACD(bars), None, True), merged)

        down = next(b for b in build() if b['endTime'] == new_t)
        self.assertEqual(down['type'], 'down')
        self.assertEqual(down['endPrice'], 10000 - 4397.045)  # 后底 5602.955 > 前底 5600.330
        try:
            c.apply_cfg({'nearDoubleRebound': False})
            self.assertFalse(any(b['endTime'] == new_t for b in build()))
        finally:
            c.reset_cfg()


class NearDoubleShiftLockedTests(unittest.TestCase):
    """nearDoubleShiftLocked（2026-10-02）：锁定端点唯一放行「近等平台取后」，全周期统一。

    合成场景（小时K，无包含、无长影压平干扰）：顶 P(130) → 下跌笔 → 锁定底
    B1(100.0，实体底 103) → 反弹顶 T(112，间隔 2 块不成笔被弹回) → 近等后底
    B2(100.8，实体底 104，diff=1.0≤2.0) → 反转上行。开关关=B1 钉死（现行行为），
    开=B1 让位 B2。锁定由上级端点清单 [{"dir":"bottom","price":100.0}] 模拟。"""

    T0 = 1767225600  # 2026-01-01 00:00 UTC
    OHLC = [
        (120, 122, 119, 121), (121, 124, 120, 123), (123, 126, 122, 125),
        (125, 128, 124, 126), (126, 130, 125, 127),   # 4: 顶 P 130
        (126, 127, 124, 125), (125, 126, 122, 123), (123, 124, 119, 120),
        (120, 121, 116, 117), (117, 118, 112, 113), (113, 114, 108, 109),
        (109, 110, 104, 105), (105, 106, 100, 103),   # 12: 锁定底 B1 100.0
        (104, 108, 103, 107), (107, 112, 106, 111),   # 14: 反弹顶 T 112（间隔不足被弹回）
        (108, 109, 100.8, 104),                        # 15: 近等后底 B2 100.8
        (105, 110, 104, 109), (109, 115, 108, 114), (114, 120, 113, 119),
        (119, 125, 118, 124), (124, 127, 123, 126),    # 20: 反转顶（确认上涨笔）
        (125, 126, 122, 123), (123, 124, 120, 122),
    ]

    def _build(self, res, bar_sec):
        bars = [{'time': self.T0 + i * 3600, 'open': o, 'high': h, 'low': lo, 'close': cl}
                for i, (o, h, lo, cl) in enumerate(self.OHLC)]
        trimmed = c.markWickBars(bars)
        merged = c.mergeBars(trimmed)
        locks = [{'dir': 'bottom', 'price': 100.0}]
        bis = c.buildBi(c.findFractals(merged), merged, c.calcATR(bars, 14), c.calcMACD(bars),
                        locks, c.nearDoubleOn(bar_sec), res)
        return c.extendLastBi(c.fixBiExtremes(bis, merged) or bis, trimmed)

    def test_locked_bottom_shift(self):
        t_b1, t_b2 = self.T0 + 12 * 3600, self.T0 + 15 * 3600
        try:
            c.apply_cfg({'nearDoubleShiftLocked': False})
            bis = self._build('60', 3600)
            down = bis[-2]
            self.assertEqual(down['type'], 'down')
            self.assertEqual(down['endPrice'], 100.0)
            self.assertEqual(down['endTime'], t_b1)
            self.assertEqual(bis[-1]['startPrice'], 100.0)

            c.apply_cfg({'nearDoubleShiftLocked': True})
            bis = self._build('60', 3600)
            down = bis[-2]
            self.assertEqual(down['type'], 'down')
            self.assertEqual(down['endPrice'], 100.8)  # 近等后底 B2
            self.assertEqual(down['endTime'], t_b2)
            self.assertEqual(bis[-1]['startPrice'], 100.8)
        finally:
            c.reset_cfg()

    def test_same_logic_on_15m(self):
        # 全周期统一：同一合成场景在 15m 周期码下行为一致
        t_b2 = self.T0 + 15 * 3600
        try:
            c.apply_cfg({'nearDoubleShiftLocked': True})
            bis = self._build('15', 900)
            down = bis[-2]
            self.assertEqual(down['endPrice'], 100.8)
            self.assertEqual(down['endTime'], t_b2)
        finally:
            c.reset_cfg()


if __name__ == '__main__':
    unittest.main()
