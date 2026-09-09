import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
import zipfile

from . import sr_preset_excel as excel, sr_tune, webapp


def config():
    return dict(symbol='TEST:FUT', **{'from': '2026-07-02'}, periods=['60'],
                minTouchs={'W':4,'D':4,'240':4,'60':3,'15':3,'3':8},
                clusterParamsByPeriod={'W':{'clusterAtr':2.1},'60':{'clusterAtr':.5,'recentClusterAtr':1,'recentBiCount':20}},
                srTypes=['cluster'], clusterParts=['flip','recent'], clusterAtr='0.5', recentClusterAtr='1',
                recentBiCount='20', maxPerPeriod='50', touchWeight='0.6', barsWeight='0.4',
                fibLevels='0.382,0.5,0.618', bollLength='26', bollMult='2', mergeAtr='0.5',
                maxDistAtr='3', sideCount='2', color='#787b86', draw_text=True, draw_raw=False)


def rewrite(data, fn):
    out=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out,'w') as dst:
        for entry in src.infolist():
            raw=src.read(entry.filename)
            if entry.filename=='xl/worksheets/sheet1.xml':raw=fn(raw.decode()).encode()
            dst.writestr(entry.filename,raw)
    return out.getvalue()


class ExcelTests(unittest.TestCase):
    def test_roundtrip_all_types_and_unselected_period(self):
        cfg=config();cfg['future']={'empty':{},'list':[],'null':None,'text':'=SUM(A1:A5)'}
        name, result=excel.import_preset(excel.export_preset('中文方案',cfg))
        self.assertEqual(name,'中文方案');self.assertEqual(result,cfg)

    def test_excel_edited_value(self):
        data=excel.export_preset('edit',config())
        # D4 is the symbol value. Excel retains paths/types while values may change.
        data=rewrite(data,lambda s:s.replace('TEST:FUT','OTHER:FUT'))
        self.assertEqual(excel.import_preset(data)[1]['symbol'],'OTHER:FUT')

    def test_formula_rejected(self):
        data=rewrite(excel.export_preset('a',config()),lambda s:s.replace('<v>3</v>','<f>1+2</f><v>3</v>',1))
        with self.assertRaisesRegex(ValueError,'公式'):excel.import_preset(data)

    def test_invalid_file_and_version(self):
        for data in (b'not excel',b'x'*(excel.LIMIT+1),rewrite(excel.export_preset('a',config()),lambda s:s.replace('SR_PRESET_XLSX','WRONG'))):
            with self.assertRaises(ValueError):excel.import_preset(data)

    def test_duplicate_path_rejected(self):
        data=rewrite(excel.export_preset('a',config()),lambda s:s.replace('>from<','>symbol<'))
        with self.assertRaisesRegex(ValueError,'路径重复'):excel.import_preset(data)

    def test_http_roundtrip_and_failed_import_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(webapp,'SR_PRESETS_FILE',str(Path(tmp)/'presets.json')):
            cfg=config();webapp._presets_save([{'name':'原方案','cfg':cfg}])
            app=webapp.ControlApp(tune_store=sr_tune.Store(Path(tmp)/'tune'))
            server=ThreadingHTTPServer(('127.0.0.1',0),webapp.make_handler(app))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            def post(path,data):
                with urlopen(Request(base+path,data=data,method='POST'),timeout=10) as r:return r.read()
            try:
                data=post('/api/sr/presets/excel/export',json.dumps({'name':'原方案'}).encode())
                result=json.loads(post('/api/sr/presets/excel/import?name=Imported',data))
                self.assertEqual(result['cfg'],cfg)
                before=Path(webapp.SR_PRESETS_FILE).read_bytes()
                invalid=copy.deepcopy(cfg);invalid['clusterAtr']='NaN'
                for data in (b'invalid',excel.export_preset('bad',invalid)):
                    with self.assertRaises(HTTPError) as ctx:post('/api/sr/presets/excel/import?name=Imported',data)
                    self.assertEqual(ctx.exception.code,400)
                    self.assertEqual(Path(webapp.SR_PRESETS_FILE).read_bytes(),before)
            finally:
                server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
