"""Closed-prefix structure, merged sufficiency and Python/JS parity regression."""
import copy
import gzip
import json
import pathlib
import subprocess
import unittest

from . import chan_core as core
from . import trading_plan as plan
from .mark_entry import counterMoveQualifies, lastBiOk

ROOT = pathlib.Path(__file__).resolve().parents[1]
SEC = 60


def sample():
    lows = [18, 16, 14, 12, 10, 12, 14, 16, 18]
    bars = [dict(time=i*SEC, open=x+.4, close=x+1.6, low=x, high=x+2)
            for i, x in enumerate(lows)]
    bis = [dict(type="down", startTime=0, endTime=4*SEC,
                startPrice=20, endPrice=10, span=10)]
    return bis, bars


def mirror(bis, bars):
    return ([dict(b, type="up" if b["type"] == "down" else "down",
                  startPrice=100-b["startPrice"], endPrice=100-b["endPrice"]) for b in bis],
            [dict(b, open=100-b["open"], close=100-b["close"],
                  high=100-b["low"], low=100-b["high"]) for b in bars])


class TestStructureContext(unittest.TestCase):
    def test_expected_immediately_then_fifth_block(self):
        for reflected in (False, True):
            bis, bars = sample()
            if reflected:
                bis, bars = mirror(bis, bars)
            original = copy.deepcopy(bis)
            for n, phase, count in [(5, "confirmed", 5), (6, "expected", 2),
                                     (8, "expected", 4), (9, "running", 5)]:
                ctx = core.buildStructureContext(bis, bars[:n], SEC, n*SEC)
                self.assertEqual(ctx["current"]["phase"], phase)
                self.assertEqual(ctx["current"]["mergedCount"], count)
                if phase != "confirmed":
                    self.assertEqual(ctx["current"]["type"], "down" if reflected else "up")
                    self.assertEqual(lastBiOk(ctx["bis"], ctx["current"]["type"]), n == 9)
                self.assertEqual(ctx["confirmedBis"], original)
            self.assertEqual(bis, original)

    def test_unclosed_shoulder_and_new_extreme(self):
        bis, bars = sample()
        no_shoulder = core.buildStructureContext(bis, bars[:6], SEC, 5*SEC+30)
        self.assertFalse(any(b.get("_forming") for b in no_shoulder["bis"]))
        bars[6] = dict(bars[6], high=11, low=8, open=9, close=10)
        ctx = core.buildStructureContext(bis, bars[:7], SEC, 7*SEC)
        self.assertFalse(any(b.get("_forming") for b in ctx["bis"]))

    def test_inclusion_calibration_and_raw_count_not_enough(self):
        bis, bars = sample()
        raw = bars[:6] + [dict(bars[5], time=t*SEC, high=13.8, low=12.2) for t in range(6, 14)]
        merged = core.mergeBars(core.markWickBars(raw))
        self.assertEqual(core.mergedSegmentCount(merged, 4*SEC+30, SEC), 2)
        self.assertFalse(counterMoveQualifies([], bis[-1], 14*SEC, True, merged, SEC))
        merged = core.mergeBars(core.markWickBars(bars))
        self.assertFalse(counterMoveQualifies([], bis[-1], 8*SEC, True, merged[:-1], SEC))
        self.assertTrue(counterMoveQualifies([], bis[-1], 9*SEC, True, merged, SEC))

    def test_confirmed_replacement_no_duplicate(self):
        bis, bars = sample()
        ctx = core.buildStructureContext(bis, bars, SEC, 9*SEC)
        up = {k:v for k,v in ctx["current"].items() if k in
              ("type", "startTime", "endTime", "startPrice", "endPrice", "span")}
        bars.append(dict(time=9*SEC, high=18, low=16, open=16.4, close=17.6))
        confirmed = core.buildStructureContext(bis+[up], bars, SEC, 10*SEC)
        self.assertEqual(len([b for b in confirmed["bis"] if b["type"] == "up"]), 1)
        self.assertEqual(confirmed["current"]["type"], "down")
        self.assertEqual(confirmed["current"]["phase"], "expected")

    def test_forming_endpoint_cannot_anchor_first_class(self):
        bis, bars = sample()
        ctx = core.buildStructureContext(bis, bars, SEC, 9*SEC)
        cand = dict(time=7*SEC, price=18)
        self.assertEqual(core.anchorFirstSell(cand, ctx["bis"]), core.anchorFirstSell(cand, bis))
        self.assertEqual(core.anchorFirstBuy(cand, ctx["bis"]), core.anchorFirstBuy(cand, bis))

    def test_future_prefix_and_js_parity(self):
        bis, bars = sample()
        cases = []
        for n in (5,6,8,9):
            for bb, rr in (sample(), mirror(*sample())):
                cases.append(dict(bis=bb, bars=rr[:n], sec=SEC, cut=n*SEC))
        with gzip.open(ROOT/'py_chain/fixtures/structure_xauusd_20260806.json.gz', 'rt', encoding='utf8') as f:
            fixture = json.load(f)
        for r, data in fixture['periods'].items():
            cases.append(dict(bis=data['bis'], bars=data['bars'], sec=core.intervalSecOf(r), cut=fixture['cutoff']))
            prefix = [b for b in data['bars'] if b['time']+core.intervalSecOf(r)<=fixture['cutoff']]
            # Both calls rebuild from raw history before the exact same cutoff.
            a=core.buildStructureContext(data['bis'], data['bars'], core.intervalSecOf(r), fixture['cutoff'])
            b=core.buildStructureContext(a['confirmedBis'], prefix, core.intervalSecOf(r), fixture['cutoff'])
            self.assertEqual(a['bis'],b['bis'])
        code = """
const fs=require('fs'), c=require('./.cursor/skills/chan-core/scripts/chan_core.js');
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify(cases.map(x=>c.buildStructureContext(x.bis,x.bars,x.sec,x.cut))));
"""
        js=json.loads(subprocess.check_output(['node','-e',code], input=json.dumps(cases).encode(), cwd=ROOT))
        for case,actual in zip(cases,js):
            expected=core.buildStructureContext(case['bis'],case['bars'],case['sec'],case['cut'])
            self.assertEqual(actual['bis'],expected['bis'])
            self.assertEqual(actual['current'],expected['current'])

    def test_august_6_points_phase_and_js_parity(self):
        with gzip.open(ROOT/'py_chain/fixtures/structure_xauusd_20260806.json.gz', 'rt', encoding='utf8') as f:
            fixture=json.load(f)
        cut=fixture['cutoff']; views={}; bars={}
        for r,data in fixture['periods'].items():
            sec=core.intervalSecOf(r)
            views[r]=core.buildStructureContext(data['bis'],data['bars'],sec,cut)['bis']
            bars[r]=[b for b in data['bars'] if b['time']+sec<=cut]
        self.assertEqual(views['D'][-1]['phase'],'running')
        pts=core.findBuyPoints(views['240'],views['D'],core.calcMACD(bars['240']),14400)
        self.assertIn(('2买',3996.055),[(p['type'],p['price']) for p in pts])
        self.assertIn(('类2买',4019.24),[(p['type'],p['price']) for p in pts])
        state=plan.trend_state_of(views,bars,'240')
        self.assertEqual(state,dict(dir=None,reason='4小时类2买回调中（预期段）',res='240'))
        code="""
const fs=require('fs'), c=require('./.cursor/skills/chan-core/scripts/chan_core.js');
const p=require('./.cursor/skills/trading-plan/scripts/trading_plan.js');
const x=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify({points:c.findBuyPoints(x.views['240'],x.views.D,c.calcMACD(x.bars['240']),14400),state:p.trendStateOf(x.views,x.bars,'240')}));
"""
        actual=json.loads(subprocess.check_output(['node','-e',code],cwd=ROOT,input=json.dumps(dict(views=views,bars=bars)).encode()))
        self.assertEqual(actual['points'],pts)
        self.assertEqual(actual['state'],state)


if __name__ == '__main__':
    unittest.main()
