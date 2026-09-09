"""Calibration regression tests; synthetic data, no TradingView or live cache writes."""
import copy
import itertools
import json
import math
from http.server import ThreadingHTTPServer
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from . import sr_tune as tune
from .sr_flip import cluster_candidates, compute_srflip
from .sr_service import build_chain_result
from . import test_sr_flip as sr_fixtures
from . import webapp


def bars(count=500, start=1700000000, interval=3600):
    out = []
    for i in range(count):
        c = 100 + math.sin(i / 6) * 12 + .015 * i
        out.append(dict(time=start + i * interval, open=c-.1, high=c+1, low=c-1, close=c))
    return out


def config():
    return webapp.ControlApp.normalize_sr_cfg({"symbol": "TEST:FUT", "from": "2023-11-14",
        "periods": ["60"], "srTypes": ["cluster"], "clusterParts": ["flip", "recent"],
        "minTouchs": {"60": 3}, "clusterAtr": .5, "recentClusterAtr": 1., "recentBiCount": 20})


class MatchingTests(unittest.TestCase):
    def test_no_duplicate_match_and_unlabelled_extra_penalty_is_small(self):
        r = tune.match_prices([100, 101], [{"price":100.5}, {"price":100.5}], 1)
        self.assertEqual(r["matchedCount"], 1)
        self.assertEqual(r["candidateCount"], 1)
        r = tune.match_prices([100], [{"price":100}, {"price":1000}], 1)
        self.assertEqual(r["matchRate"], 1)
        self.assertLess(r["extraPenalty"], .01)

    def test_global_assignment_matches_brute_force(self):
        # Cost discontinuity at tolerance means monotone/greedy matching is unsafe.
        targets, candidates, tol = [0., 1.1, 2.], [-.9, .7, 2.3], 1.
        rows = [[(.7 if abs(t-c)>tol else 0) + .1*min(abs(t-c)/tol,3)
                 for c in candidates] + [1.]*3 for t in targets]
        want = min(sum(rows[i][j] for i,j in enumerate(p)) for p in itertools.permutations(range(6),3))
        result = tune.match_prices(targets,[{"price":c} for c in candidates],tol)
        self.assertAlmostEqual(result["loss"]*3,want)

    def test_empty_out_of_range_and_boundary(self):
        self.assertEqual(tune.match_prices([100],[],2)["loss"],1)
        self.assertEqual(tune.match_prices([100],[{"price":200}],2)["matches"][0]["price"],None)
        self.assertEqual(tune.match_prices([100],[{"price":102}],2)["matchedCount"],1)

    def test_price_input_and_invalid_numbers(self):
        self.assertEqual(tune.price_list('100，101\n100; -1'),[-1.,100.,101.])
        for value in ('nan','inf','abc','', True):
            with self.assertRaises(ValueError):
                tune.price_list(value if isinstance(value,str) else [value])


class EngineTests(unittest.TestCase):
    def test_period_override_matches_direct_config_without_changing_other_cycle(self):
        fixture=sr_fixtures.TestSrParamExtension()
        bis=fixture._rising_bis()
        bb=fixture._bars_simple(len(bis)*20)
        kw=dict(periodBis={'60':bis,'15':bis},barsByPeriod={'60':bb,'15':bb},
                periods=['60','15'],srTypes=['cluster'],periodAtrsIn={'60':.5,'15':.5})
        base=compute_srflip(**kw)
        override={'clusterAtr':1.7,'recentClusterAtr':.25,'recentBiCount':4}
        got=compute_srflip(**kw,clusterParamsByPeriod={'60':override})
        direct=compute_srflip(**kw,**override)
        self.assertEqual(got['periods']['60'],direct['periods']['60'])
        self.assertEqual(got['periods']['15'],base['periods']['15'])
        self.assertEqual(base,compute_srflip(**kw,clusterParamsByPeriod={}))

    def test_normalizer_preserves_empty_parts_and_rejects_invalid_overrides(self):
        cfg=config();cfg['clusterParts']=[]
        self.assertEqual(webapp.ControlApp.normalize_sr_cfg(cfg)['clusterParts'],[])
        for override in ({'60':{'clusterAtr':'nan'}},{'60':{'recentBiCount':1.2}},{'bogus':{}},[]):
            with self.assertRaises(ValueError):
                tune.normalize_overrides(override)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=tune.Store(self.temp.name)
        self.raw=bars()
        self.snap=tune.closed_snapshot(self.raw,'TEST:FUT','60',self.raw[0]['time'],
                                      self.raw[-1]['time']+3600,now=self.raw[-1]['time']+3600)
        self.store.put('snapshots',self.snap['id'],self.snap)
        self.sample=tune.save_sample(self.store,{'snapshotId':self.snap['id'],
                                               'prices':[92,112], 'tolerance':2})

    def manager(self):
        manager=tune.TuneManager(self.store)
        def cleanup():
            manager.stop_event.set()
            if manager.thread: manager.thread.join(5)
        self.addCleanup(cleanup)
        return manager

    def test_cutoff_isolation_and_fingerprint(self):
        cutoff=self.raw[200]['time']+1800
        before=tune.closed_snapshot(self.raw,'TEST:FUT','60',self.raw[0]['time'],cutoff)
        future=copy.deepcopy(self.raw)
        for b in future[200:]: b.update(high=999,close=888)
        after=tune.closed_snapshot(future,'TEST:FUT','60',self.raw[0]['time'],cutoff)
        self.assertEqual(before,after)
        self.assertEqual(before['actualTo'],self.raw[200]['time'])
        self.assertLess(before['barCount'],len(self.raw))

    def test_samples_versions_scope_and_immutable_snapshot(self):
        changed=tune.save_sample(self.store,{'id':self.sample['id'],'version':1,'prices':[99]})
        self.assertEqual(changed['version'],2)
        self.assertEqual(changed['snapshotId'],self.snap['id'])
        with self.assertRaises(tune.Conflict):
            tune.save_sample(self.store,{'id':self.sample['id'],'version':1,'prices':[100]})
        self.assertEqual(self.store.samples('OTHER:FUT'),[])
        with self.assertRaises(ValueError): self.store.get('samples','../../escape')
        tune.delete_sample(self.store,changed['id'],2)
        self.assertEqual(self.store.samples('TEST:FUT'),[])
        self.assertTrue(self.store.get('samples',changed['id'])['deleted'])

    def test_snapshot_rejects_short_flat_and_future(self):
        for raw in (self.raw[:5],[dict(b,open=1,high=1,low=1,close=1) for b in self.raw]):
            with self.assertRaises(ValueError):
                tune.closed_snapshot(raw,'TEST:FUT','60',self.raw[0]['time'],self.raw[-1]['time'])
        with self.assertRaises(ValueError):
            tune.closed_snapshot(self.raw,'TEST:FUT','60',self.raw[0]['time'],int(time.time())+10000)

    def test_evaluator_equals_normal_generator_and_samples_equal_weight(self):
        evaluator=tune.Evaluator(self.store,[self.sample],['flip','recent'])
        params=tune.effective_params(config(),'60')
        fast=evaluator.candidates(self.sample,params)
        full=evaluator.candidates(self.sample,params,True)
        self.assertEqual(fast,[{k:v for k,v in c.items() if k!='barsPassed'} for c in full])
        report=evaluator.evaluate(params,True,config())[1][0]
        self.assertTrue(all('keptAfterCap' in r for r in report['matches']))
        other=tune.save_sample(self.store,{'snapshotId':self.snap['id'],'prices':[800,801,802,803,804], 'tolerance':.1})
        ev=tune.Evaluator(self.store,[self.sample,other],['flip','recent'])
        metrics,reports=ev.evaluate(params)
        losses=[r['loss'] for r in reports]
        expected=.8*sum(losses)/2+.2*max(losses)+sum(r['extraPenalty'] for r in reports)/2
        self.assertAlmostEqual(metrics['score'],expected)

    def test_search_resume_apply_and_version_invalidation(self):
        manager=self.manager(); cfg=config()
        released=[]
        started=manager.start_search(cfg,'60',.12,release=lambda:released.append(True))
        manager.thread.join(5)
        job=self.store.get('jobs',started['id'])
        self.assertEqual(job['status'],'completed',job.get('error'))
        self.assertLessEqual(job['best']['metrics']['score'],job['baseline']['metrics']['score'])
        self.assertTrue(released)
        old_cursor=job['search']['cursor']
        manager.start_search(cfg,'60',.12,resume=job['id'])
        manager.thread.join(5)
        job=self.store.get('jobs',job['id'])
        self.assertGreater(job['search']['cursor'],old_cursor)
        applied=manager.apply(job['id'],cfg)
        self.assertEqual(applied['clusterParamsByPeriod']['60']['clusterAtr'],job['best']['params']['clusterAtr'])
        self.assertEqual(applied['srTypes'],cfg['srTypes'])
        tune.save_sample(self.store,{'id':self.sample['id'],'version':1,'prices':[99]})
        with self.assertRaises(tune.Conflict):manager.apply(job['id'],cfg)
        with self.assertRaises(tune.Conflict):manager.start_search(cfg,'60',.1,resume=job['id'])

    def test_stop_and_restart_recovery(self):
        manager=self.manager()
        start=manager.start_search(config(),'60',10)
        manager.stop(start['id']);manager.thread.join(5)
        self.assertIsNone(manager.active)
        self.assertEqual(self.store.get('jobs',start['id'])['status'],'stopped')
        job=self.store.get('jobs',start['id']);job['status']='running'
        self.store.put('jobs',job['id'],job)
        new=self.manager()
        self.assertEqual(new.store.get('jobs',job['id'])['status'],'interrupted')
        self.assertIsNone(new.thread)

    def test_proposals_reproducible_and_disabled_parameters_fixed(self):
        base=tune.effective_params(config(),'60')
        st={'cursor':0,'baselineParams':base,'top':[{'params':base,'score':1}]}
        other=copy.deepcopy(st)
        for _ in range(200):
            a=tune.next_params(st,tune.DEFAULT_RANGES,['recent'])
            b=tune.next_params(other,tune.DEFAULT_RANGES,['recent'])
            self.assertEqual(a,b)
            self.assertEqual(a['minTouch'],base['minTouch'])
            self.assertEqual(a['clusterAtr'],base['clusterAtr'])

    def test_capture_uses_verified_symbol_source(self):
        cfg=config();cfg['from']='2023-11-14T22:13:20'
        with patch.object(tune.data_loader,'fetch_bars',return_value={'60':self.raw}) as fetch:
            preview=tune.capture_snapshot(self.store,cfg,'60',self.snap['cutoff'],refresh=True)
            self.assertTrue(fetch.call_args.kwargs['verify_symbol'])
            self.assertFalse(fetch.call_args.kwargs['cache'])
            self.assertEqual(preview['snapshot']['symbol'],'TEST:FUT')

    def test_sample_snapshot_tampering_is_rejected(self):
        altered=copy.deepcopy(self.snap)
        altered['bars'][10]['high'] += 1
        self.store.put('snapshots',altered['id'],altered)
        with self.assertRaises(ValueError):
            tune.Evaluator(self.store,[self.sample],['recent'])

    def test_symbol_cache_filters_start_and_does_not_trust_legacy(self):
        from . import sr_service
        key=tune.digest({'symbol':'TEST:FUT','period':'60'})
        self.store.put('market',key,{'symbol':'TEST:FUT','period':'60','bars':self.raw})
        with patch.object(tune,'Store',return_value=self.store), patch.object(tune.data_loader,'fetch_bars') as fetch:
            result=sr_service.ensure_data(['60'],self.raw[100]['time'],symbol='TEST:FUT')
            self.assertEqual(result['60'][0]['time'],self.raw[100]['time'])
            fetch.assert_not_called()
            fetch.return_value={'60':self.raw}
            sr_service.ensure_data(['60'],self.raw[0]['time'],symbol='OTHER:FUT')
            self.assertTrue(fetch.call_args.kwargs['verify_symbol'])


class HttpTests(unittest.TestCase):
    setUp = StoreTests.setUp
    def test_http_jobs_and_busy_conflicts(self):
        app=webapp.ControlApp(tune_store=self.store)
        server=ThreadingHTTPServer(('127.0.0.1',0),webapp.make_handler(app))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        def call(path,body=None,method=None):
            req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                        headers={'Content-Type':'application/json'},method=method)
            try:
                with urlopen(req,timeout=10) as response:return json.load(response)
            except HTTPError as exc:
                exc.msg = exc.read().decode()
                exc.close()
                raise
        loaded=call('/api/sr/tune/samples?symbol=TEST:FUT')
        self.assertEqual(len(loaded['samples']),1)
        started=call('/api/sr/tune/jobs',{'cfg':config(),'period':'60','budgetSeconds':10})
        ident=started['job']['id']
        try:
            with self.assertRaises(HTTPError) as conflict:
                call('/api/sr/compute',{'cfg':config()})
            self.assertEqual(conflict.exception.code,409)
            call('/api/sr/tune/jobs/'+ident+'/stop',{})
        finally:
            app.sr_tune.stop_event.set();app.sr_tune.thread.join(5)
        self.assertIsNone(webapp.active_mode())
        self.assertIsNone(webapp._sr_busy_snapshot())
        self.assertEqual(call('/api/sr/tune/jobs/'+ident)['job']['status'],'stopped')
        self.assertTrue(call('/api/sr/tune/jobs/'+ident+'/validate',{'cfg':config()})['compatible'])
        changed=config();changed['clusterAtr']=1.5
        self.assertFalse(call('/api/sr/tune/jobs/'+ident+'/validate',{'cfg':changed})['compatible'])
        with self.assertRaises(HTTPError) as invalid:
            call('/api/sr/tune/jobs',{'cfg':config(),'period':'60','ranges':{'clusterAtr':[2,1]}})
        self.assertEqual(invalid.exception.code,400)
        self.assertIsNone(webapp.active_mode())


if __name__=='__main__':
    unittest.main()
