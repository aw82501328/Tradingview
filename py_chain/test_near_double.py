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


class NearDoubleTests(unittest.TestCase):
    def test_frozen_target_and_unchanged_other_periods(self):
        new = build_bis(DATA)
        for res, bars in DATA.items():
            merged = c.mergeBars(c.markWickBars(bars))
            baseline = c.buildBi(c.findFractals(merged), merged, c.calcATR(bars, 14), c.calcMACD(bars), None, c.nearDoubleOn(res))
            baseline = c.extendLastBi(c.fixBiExtremes(baseline, merged), c.markWickBars(bars))
            self.assertEqual(len(baseline), len(new[res]))
            changed = [(a, b) for a, b in zip(baseline, new[res]) if a != b]
            self.assertEqual(len(changed), 2 if res == '60' else 0)
            if res == '60':
                self.assertEqual((changed[0][1]['endTime'], changed[0][1]['endPrice']), (NEW, 4229.875))

    def test_near_double_on_helper(self):
        # 默认（=原硬编码 ≥1h）：60/240/D 开、3/15 关
        self.assertFalse(c.nearDoubleOn('3') or c.nearDoubleOn('15'))
        self.assertTrue(c.nearDoubleOn('60') and c.nearDoubleOn('240') and c.nearDoubleOn('D'))
        # barSec 秒数形式（buildStructureContext 只有秒数）与别名
        self.assertTrue(c.nearDoubleOn(3600) and c.nearDoubleOn(14400) and c.nearDoubleOn(86400))
        self.assertTrue(c.nearDoubleOn('1H') and c.nearDoubleOn('4H') and c.nearDoubleOn('1D'))
        # 五周期之外失败安全：'30S'/'5'/'30'/'W'/未知秒数/bool 一律 False
        for bad in ('30S', '5', '30', 'W', 180, 900, 30, 0, True, None):
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
        # 关 nearDouble60 → 60m 笔与「nearDouble=False 直构」基线逐笔相等（引擎/批量同口径）
        merged = c.mergeBars(c.markWickBars(DATA['60']))
        baseline = c.buildBi(c.findFractals(merged), merged, c.calcATR(DATA['60'], 14), c.calcMACD(DATA['60']), None, False)
        baseline = c.extendLastBi(c.fixBiExtremes(baseline, merged), c.markWickBars(DATA['60']))
        try:
            c.apply_cfg({'nearDouble60': False})
            self.assertEqual(build_bis(DATA)['60'], baseline)
        finally:
            c.reset_cfg()

    def test_switch_on_15m_restructures(self):
        # 开 nearDouble15 → 15m 结构重排（默认关时与无开关构建一致；不断言具体笔数，
        # 结构对开关语义敏感、对计数不承诺）
        merged = c.mergeBars(c.markWickBars(DATA['15']))
        default = c.buildBi(c.findFractals(merged), merged, c.calcATR(DATA['15'], 14), c.calcMACD(DATA['15']), None, False)
        try:
            c.apply_cfg({'nearDouble15': True})
            altered = c.buildBi(c.findFractals(merged), merged, c.calcATR(DATA['15'], 14), c.calcMACD(DATA['15']), None, True)
            self.assertNotEqual(altered, default)
        finally:
            c.reset_cfg()

    def test_fixed_tolerance_boundary(self):
        # 固定容差并集项边界：thr = max(ATR项, 比例项, 固定项)。用破坏 15m 动量确认的 ctx
        # （NEW±窗口柱高置 0 → lower_confirmed=False，纯阈值路径）——thr0≈4.65 < diff≈6.37：
        # fixed=0/diff-1e-6 停旧端点；fixed≥diff 后移到新端点（平台回调深度 27.88 » thr，
        # pull 条件在边界档位恒成立）。biStep 会给分型打 nearDouble 标记，每档用深拷贝。
        merged = c.mergeBars(c.markWickBars(DATA['60']))
        f = c.findFractals(merged)
        atr = c.calcATR(DATA['60'], 14)
        macd = c.calcMACD(DATA['60'])
        ctx = c.makeBiLowerContext('60', DATA['15'])
        for m in ctx['macd']:
            if NEW - 900 <= m['time'] <= NEW + 2700:
                m['macd'] = 0
        old = next(x for x in f if x['time'] == OLD)
        end = next(x for x in f if x['time'] == NEW)
        diff = end['low'] - old['low']
        start_anchor = OLD - 14 * 3600
        try:
            for fixed, expected in [(0.0, OLD), (diff - 1e-6, OLD), (diff, NEW), (diff + 10, NEW)]:
                c.apply_cfg({'nearDoubleFixed': fixed})
                bis = c.buildBi(copy.deepcopy(f), merged, atr, macd, None, True, ctx)
                stroke = next(x for x in bis if x['startTime'] == start_anchor)
                self.assertEqual(stroke['endTime'], expected, f'nearDoubleFixed={fixed}')
        finally:
            c.reset_cfg()

    def test_confirmation_and_no_future_lower_candles(self):
        merged = c.mergeBars(c.markWickBars(DATA['60']))
        f = c.findFractals(merged)
        old, end = (next(x for x in f if x['time'] == t) for t in (OLD, NEW))
        ctx = c.makeBiLowerContext('60', DATA['15'])
        self.assertTrue(c.lowerEndpointWeaker(old, end, f, ctx))
        for context in (None, c.makeBiLowerContext('240', DATA['15']), dict(ctx, cutoff=NEW+2700)):
            self.assertFalse(c.lowerEndpointWeaker(old, end, f, context))
        self.assertTrue(c.lowerEndpointWeaker(old, end, f, dict(ctx, cutoff=NEW+3600)))
        for field, value in [('macd', 0), ('macd', -10), ('dif', -10)]:
            altered = copy.deepcopy(ctx)
            for m in altered['macd']:
                if NEW-900 <= m['time'] <= NEW+2700:
                    m[field] = value
            self.assertFalse(c.lowerEndpointWeaker(old, end, f, altered))

    def test_momentum_boundary_and_rewind_prefix(self):
        merged = c.mergeBars(c.markWickBars(DATA['60']))
        f = c.findFractals(merged)
        old, end = (next(x for x in f if x['time'] == t) for t in (OLD, NEW))
        ctx = c.makeBiLowerContext('60', DATA['15'])
        for hist, dif, expected in [(-1, -2, True), (-1.00001, -2, False), (-1, -2.00001, False)]:
            altered = copy.deepcopy(ctx)
            for m in altered['macd']:
                if OLD-7200 <= m['time'] <= OLD+900:
                    m.update(macd=-2, dif=-4)
                elif NEW-900 <= m['time'] <= NEW+2700:
                    m.update(macd=hist, dif=dif)
            self.assertEqual(c.lowerEndpointWeaker(old, end, f, altered), expected)
        self.assertIsNone(c.makeBiLowerContext('60', DATA['15'], macd=[dict(time=b['time']) for b in DATA['15']]))
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


if __name__ == '__main__':
    unittest.main()
