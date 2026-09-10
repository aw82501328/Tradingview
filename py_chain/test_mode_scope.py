import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from . import webapp

class ModeScopeTests(unittest.TestCase):
    def setUp(self):
        self.signals=webapp.SignalLog()
        self.sample={"time":100,"periodX":"3","direction":"long","strategyKey":"one","nearSr":2000}
        for mode in ("backtest","replay","live"):
            self.signals.append_signal(mode,self.sample)
        self.app=SimpleNamespace(signals=self.signals,broadcaster=SimpleNamespace(emit=Mock()))

    def request(self,path,body):
        reply=[]
        handler=SimpleNamespace(path=path,_read_body=lambda:body,_tune=lambda _:False,
                                _send_json=lambda obj,code=200:reply.append((obj,code)))
        with patch.object(webapp.analysis_api,"handle",return_value=False):
            webapp.make_handler(self.app).do_POST(handler)
        return reply

    def test_clear_preserves_other_rows_and_fill_index(self):
        self.assertEqual(self.signals.clear("backtest"),1)
        row=self.signals.fill_trade("live",{**self.sample,"signalTime":100,"entryPrice":2001})
        self.assertEqual(row["id"],3)
        self.assertEqual(len(self.signals.list()),2)
        new=self.signals.append_signal("backtest",{**self.sample,"time":200})
        self.assertEqual(new["id"],4)
        self.assertEqual(len(self.signals.list(mode="replay")),1)

    def test_scoped_clear_event_and_legacy_clear(self):
        self.request("/api/signals/clear",{"mode":"replay"})
        self.assertEqual([r["mode"] for r in self.signals.list()],["backtest","live"])
        self.app.broadcaster.emit.assert_called_with("signals_cleared",{"n":1,"mode":"replay"})
        self.request("/api/signals/clear",{})
        self.assertEqual(self.signals.list(),[])

    def test_invalid_modes_never_mutate(self):
        for path in ("/api/signals/clear","/api/marks/draw","/api/marks/sr_draw"):
            for mode in ("invalid",None,[],1):
                self.assertEqual(self.request(path,{"mode":mode})[0][1],400)
        self.assertEqual(len(self.signals.list()),3)

    def test_mark_inputs_are_scoped(self):
        class ImmediateThread:
            def __init__(self,target,**kwargs):self.target=target
            def start(self):self.target()
        for path,fn in (("/api/marks/draw","draw_signal_marks"),("/api/marks/sr_draw","draw_sr_marks")):
            with patch.object(webapp.threading,"Thread",ImmediateThread), patch.object(webapp,fn) as draw:
                self.request(path,{"mode":"live"})
                self.assertEqual([r["mode"] for r in draw.call_args.args[0]],["live"])
                self.request(path,{})
                self.assertEqual(len(draw.call_args.args[0]),3)

if __name__=="__main__":unittest.main()
