// 历史方案参数对比（2026-10-28 全参数对比·只显示差异）纯函数冒烟（node vm；
// 与 test_signal_locator.cjs 同法只加载 bt_common.js，无 DOM 依赖）：
// btCompareParamRows 键并集/排序、只留差异行、排除键、缺失=null 同值、
// 结构化值格式化（chain_snapshot/seg_bounds/布尔/对象）、参数组（param-src
// 快照/决策日志）展开与「—（无快照）」语义；btCfgVal 新键格式化。
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const common = fs.readFileSync('py_chain/web/bt_common.js', 'utf8');
const ctx = { console, setTimeout: () => 1, clearTimeout() {},
              location: { search: '', origin: 'http://localhost' },
              window: { addEventListener() {} },
              document: { getElementById: () => null, querySelector: () => null,
                          querySelectorAll: () => [], addEventListener() {} } };
vm.createContext(ctx);
vm.runInContext(common, ctx);
const run = code => vm.runInContext(code, ctx);
// vm 上下文对象的原型与宿主不同，结构化结果经 JSON 桥接后再 deepEqual
const jrun = code => JSON.parse(vm.runInContext(`JSON.stringify(${code})`, ctx));

// ---------- btCfgVal 新键格式化 ----------
assert.equal(run(`btCfgVal('signal_mode', 'realtime')`), '当下背驰');
assert.equal(run(`btCfgVal('signal_mode', 'confirm')`), '确认制');
assert.equal(run(`btCfgVal('signal_mode', undefined)`), '—');
assert.equal(run(`btCfgVal('chain_snapshot', [{id:'A',periods:['D','240','60','15','3'],open_res:['60','15','3']}])`),
  '链A 结构D+240+60+15+3 开单60+15+3');
assert.equal(run(`btCfgVal('seg_bounds', [['2026-07-02','2026-08-15'],['2026-08-15','']])`),
  '2026-07-02→2026-08-15；2026-08-15→最新');
assert.equal(run(`btCfgVal('auto_split', true)`), '开');           // 布尔
assert.equal(run(`btCfgVal('zzz', [3, 15])`), '3+15');             // 通用数组
assert.equal(run(`btCfgVal('zzz', {a: 1})`), '{"a":1}');           // 通用对象（不再 [object Object]）

// ---------- btCompareParamRows ----------
run(`G_runs = [
  {cfg: {strategy: 'chan_v1', symbol: 'OANDA:XAUUSD', lots: 4, auto_split: true,
         journal: 'data/journal/x.ndjson', from_ts: 100, to_ts: null,
         param_snapshot: {entry: {lots: 4}}, zzz_extra: 'x'},
   params: {画笔: {gapFilter: 0.5, wickRatio: 0.66}, 进出场: {pointClasses: '1,2s'}}},
  {cfg: {strategy: 'chan_v1', symbol: 'OANDA:XAUUSD', lots: 4, auto_split: false,
         to_ts: null},
   params: {画笔: {gapFilter: 0.0, wickRatio: 0.66}, 进出场: {pointClasses: '1,2s'}}},
]; rows = btCompareParamRows(G_runs);`);
let rows = jrun('rows');
const byId = Object.fromEntries(rows.map(r => [r.id, r]));

// ① 只留差异行：lots/symbol 相同不出；journal/from_ts/to_ts/param_snapshot 排除
assert.deepEqual(rows.map(r => r.id),
  ['auto_split', 'zzz_extra', 'param:画笔/gapFilter'],
  '差异行集合与顺序（已知序在前→字母序→参数组）');
assert.ok(!('lots' in byId) && !('journal' in byId) && !('from_ts' in byId)
  && !('param_snapshot' in byId), '相同项与排除键不出行');
// 缺失键 vs 显式值 = 差异：auto_split true/缺失(—)；to_ts 两方 null 同值不出
assert.deepEqual(byId['auto_split'].vals, ['开', '关']);
assert.equal(byId['zzz_extra'].vals[1], '—');
// 参数组：同组同键差异行，label=组·键
assert.deepEqual(byId['param:画笔/gapFilter'].vals, [0.5, 0]);
assert.equal(byId['param:画笔/gapFilter'].label, '画笔·gapFilter');
assert.ok(!('param:画笔/wickRatio' in byId), '参数组相同键不出行');

// ② 值显示走 btCfgVal：布尔→开/关、数字原样
assert.deepEqual(byId['auto_split'].vals, ['开', '关']);
assert.deepEqual(byId['param:画笔/gapFilter'].vals, [0.5, 0]);

// ③ 无参数源的 run（老方案且日志已清）：缺值显示「—（无快照）」；
//    有参数源但缺该组该键：显示「—」
run(`M_runs = [
  {cfg: {lots: 1, near: 5}, params: {画笔: {gapFilter: 0.5}, 进出场: {pointClasses: '1'}}},
  {cfg: {lots: 1, near: 6}, params: {画笔: {gapFilter: 0.5}}},
  {cfg: {lots: 1, near: 6}},
]; M = btCompareParamRows(M_runs);`);
const m = jrun("M");
const mById = Object.fromEntries(m.map(r => [r.id, r]));
assert.deepEqual(mById['near'].vals, [5, 6, 6]);
assert.deepEqual(mById['param:进出场/pointClasses'].vals, ['1', '—', '—（无快照）'],
  '缺键=—、无参数源=—（无快照）');
// run3 无参数源 → gapFilter 缺值即差异，行保留并显示「—（无快照）」
assert.deepEqual(mById['param:画笔/gapFilter'].vals, [0.5, 0.5, '—（无快照）']);

// ④ 结构化值差异（chain_snapshot/seg_bounds/periods 数组按序列化比较）
run(`S_runs = [
  {cfg: {periods: ['D','60','15','3'], chain_snapshot: [{id:'A',periods:['D','60'],open_res:['60']}],
         seg_bounds: [['2026-07-02','2026-08-15']]}},
  {cfg: {periods: ['D','60','15','3'], chain_snapshot: [{id:'A',periods:['D','60','15'],open_res:['60','15']}],
         seg_bounds: [['2026-07-02','2026-09-30']]}},
]; S = btCompareParamRows(S_runs);`);
const s = jrun("S");
const sById = Object.fromEntries(s.map(r => [r.id, r]));
assert.equal(sById['chain_snapshot'].vals[0],
  '链A 结构D+60 开单60');
assert.equal(sById['seg_bounds'].vals[1], '2026-07-02→2026-09-30');
assert.ok(!('periods' in sById), '相同数组不出行');

// ⑤ 全部一致 → 空行（前端显示「参数完全一致」提示）
assert.deepEqual(jrun(`btCompareParamRows([
  {cfg: {lots: 4, near: 5}, params: {画笔: {gapFilter: 0.5}}},
  {cfg: {lots: 4, near: 5}, params: {画笔: {gapFilter: 0.5}}}])`), []);

console.log('test_bt_compare.cjs 全部断言通过');
