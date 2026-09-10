import copy
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
            baseline = c.buildBi(c.findFractals(merged), merged, c.calcATR(bars, 14), c.calcMACD(bars), None, c.intervalSecOf(res) >= 3600)
            baseline = c.extendLastBi(c.fixBiExtremes(baseline, merged), c.markWickBars(bars))
            self.assertEqual(len(baseline), len(new[res]))
            changed = [(a, b) for a, b in zip(baseline, new[res]) if a != b]
            self.assertEqual(len(changed), 2 if res == '60' else 0)
            if res == '60':
                self.assertEqual((changed[0][1]['endTime'], changed[0][1]['endPrice']), (NEW, 4229.875))

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


if __name__ == '__main__':
    unittest.main()
