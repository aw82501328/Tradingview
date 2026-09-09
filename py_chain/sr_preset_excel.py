"""Portable preset XLSX interchange, using the application's standard-library runtime.

One visible worksheet contains all configuration leaves, including unknown fields.
Explicit types preserve strings, booleans, empty collections and numeric precision.
Only this parameter workbook format is accepted; formulas are never evaluated.
"""
import io
import json
import math
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
LIMIT = 2 * 1024 * 1024
HEADERS = ['参数路径', '参数说明', '数据类型', '参数值']
LABELS = dict(symbol='品种', **{'from': '计算起始日期'}, periods='参与计算的周期',
              minTouchs='各周期触及次数', clusterParamsByPeriod='周期独立参数',
              clusterAtr='flip 聚类容差 × ATR', recentClusterAtr='recent 聚类容差 × ATR',
              recentBiCount='recent 最近笔数', srTypes='类型开关', clusterParts='密集区子类型',
              mergeAtr='跨周期合并容差', maxDistAtr='距离上限 × ATR',
              maxPerPeriod='每周期候选上限', sideCount='每侧显示条数',
              touchWeight='触及次数权重', barsWeight='经过 K 线权重',
              fibLevels='黄金分割比率', bollLength='BOLL 周期', bollMult='BOLL 标准差倍数',
              color='线色', draw_text='文本标注', draw_raw='叠加原始线')


def _leaves(obj, prefix=''):
    for key, value in obj.items():
        if not isinstance(key, str) or not key or '.' in key:
            raise ValueError('配置字段名无效')
        path = f'{prefix}.{key}' if prefix else key
        if isinstance(value, dict) and value:
            yield from _leaves(value, path)
        else:
            yield path, value


def _type(v):
    if isinstance(v, bool):
        return 'boolean'
    if isinstance(v, (int, float)):
        return 'number'
    return 'text' if isinstance(v, str) else 'json'


def _cell(ref, value, style=0):
    attr = f'r="{ref}" s="{style}"'
    if isinstance(value, bool):
        return f'<c {attr} t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError('参数不能包含 NaN 或 Infinity')
        return f'<c {attr}><v>{value}</v></c>'
    # inlineStr ensures names or strings starting with = are plain text.
    text = str(value)
    if len(text) > 30000 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', text):
        raise ValueError('参数文本过长或包含非法字符')
    return f'<c {attr} t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def export_preset(name, cfg):
    rows = [['SR_PRESET_XLSX', 1, '预设名称', name],
            ['编辑 D 列参数值，保留路径和数据类型。列表使用 JSON 格式；导入后需重新计算。'], HEADERS]
    for path, value in _leaves(cfg):
        kind = _type(value)
        label = LABELS.get(path.split('.')[-1], LABELS.get(path.split('.')[0], path))
        if path.startswith('clusterParamsByPeriod.'):
            label = path.split('.')[1] + ' 周期 / ' + label
        elif path.startswith('minTouchs.'):
            label = path.split('.')[-1] + ' 周期 / minTouch'
        rows.append([path, label, kind, json.dumps(value, ensure_ascii=False, allow_nan=False) if kind == 'json' else value])
    data = ''.join('<row r="%s" ht="24" customHeight="1">%s</row>' %
                   (i, ''.join(_cell(f'{chr(65+j)}{i}', v, 1 if i in (1, 3) else 0) for j, v in enumerate(row)))
                   for i, row in enumerate(rows, 1))
    sheet = f'<worksheet xmlns="{NS}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="3" topLeftCell="A4" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="1" width="49" customWidth="1"/><col min="2" max="2" width="38" customWidth="1"/><col min="3" max="3" width="15" customWidth="1"/><col min="4" max="4" width="66" customWidth="1"/></cols><sheetData>{data}</sheetData><autoFilter ref="A3:D{len(rows)}"/></worksheet>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        z.writestr('_rels/.rels', f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{NS}" xmlns:r="{REL}"><sheets><sheet name="预设参数" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{REL}/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml', f'<styleSheet xmlns="{NS}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF24476B"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return out.getvalue()


def import_preset(data):
    if not data or len(data) > LIMIT:
        raise ValueError('请选择不超过 2 MB 的 .xlsx 预设文件')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if len(z.infolist()) > 100 or sum(i.file_size for i in z.infolist()) > 8 * LIMIT:
                raise ValueError('Excel 内容过大')
            def xml(path):
                raw = z.read(path)
                if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                    raise ValueError('不支持含实体声明的文件')
                return ET.fromstring(raw)
            workbook = xml('xl/workbook.xml')
            sheet = next((s for s in workbook.findall(f'{{{NS}}}sheets/{{{NS}}}sheet') if s.get('name') == '预设参数'), None)
            if sheet is None:
                raise ValueError('缺少“预设参数”工作表，请先从本页导出模板')
            rels = xml('xl/_rels/workbook.xml.rels')
            rel = next((r for r in rels if r.get('Id') == sheet.get(f'{{{REL}}}id')), None)
            if rel is None or rel.get('TargetMode') == 'External':
                raise ValueError('工作表引用无效')
            path = posixpath.normpath(posixpath.join('xl', rel.get('Target', ''))) if not rel.get('Target', '').startswith('/') else rel.get('Target')[1:]
            if not path.startswith('xl/'):
                raise ValueError('工作表路径无效')
            strings = []
            if 'xl/sharedStrings.xml' in z.namelist():
                strings = [''.join(t.text or '' for t in si.iter(f'{{{NS}}}t')) for si in xml('xl/sharedStrings.xml')]
            cells = {}
            for c in xml(path).iter(f'{{{NS}}}c'):
                ref = c.get('r', '')
                if c.find(f'{{{NS}}}f') is not None:
                    raise ValueError(f'{ref} 含公式，请粘贴为值后再导入')
                kind = c.get('t')
                v = c.findtext(f'{{{NS}}}v', '')
                if kind == 's':
                    v = strings[int(v)]
                elif kind == 'inlineStr':
                    v = ''.join(t.text or '' for t in c.iter(f'{{{NS}}}t'))
                elif kind == 'b':
                    v = 'true' if v == '1' else 'false'
                cells[ref] = v
    except (zipfile.BadZipFile, KeyError, IndexError, ET.ParseError, RuntimeError, OverflowError) as e:
        raise ValueError('无法读取 Excel 预设文件，请使用本页导出的 .xlsx 文件') from e
    if cells.get('A1') != 'SR_PRESET_XLSX' or cells.get('B1') != '1' or [cells.get(f'{c}3') for c in 'ABCD'] != HEADERS:
        raise ValueError('文件格式或版本不匹配，请使用本页导出的模板')
    cfg, seen = {}, set()
    row_numbers = sorted({int(re.sub(r'^[A-Z]+', '', r)) for r in cells if re.fullmatch(r'[A-D][0-9]+', r) and int(r[1:]) >= 4})
    if len(row_numbers) > 1000:
        raise ValueError('参数行数过多')
    for i in row_numbers:
        path, kind, raw = (cells.get(f'{c}{i}', '') for c in 'ACD')
        if not path and not kind and not raw:
            continue
        parts = path.split('.')
        if len(parts) > 8 or any(not p or len(p) > 100 for p in parts) or path in seen or any(path.startswith(p+'.') or p.startswith(path+'.') for p in seen):
            raise ValueError(f'第 {i} 行：参数路径重复或无效')
        try:
            if kind == 'text':
                v = raw
            elif kind == 'boolean':
                if raw.lower() not in ('true', 'false', '1', '0'):
                    raise ValueError('开关须为 TRUE/FALSE')
                v = raw.lower() in ('true', '1')
            elif kind in ('number', 'json'):
                v = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('不支持非有限数字')))
                if kind == 'number' and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
                    raise ValueError('须为有效数字')
            else:
                raise ValueError('未知数据类型')
        except (ValueError, TypeError) as e:
            raise ValueError(f'第 {i} 行 {path}：{e}') from e
        target = cfg
        for p in parts[:-1]:
            target = target.setdefault(p, {})
        target[parts[-1]] = v
        seen.add(path)
    if not {'symbol', 'from', 'periods', 'minTouchs', 'srTypes'} <= cfg.keys():
        raise ValueError('缺少必要参数，请保留完整预设配置')
    return cells.get('D1', '').strip(), cfg
