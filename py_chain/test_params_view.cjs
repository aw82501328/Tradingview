// params.html 出场方式卡渲染冒烟（node vm；无 DOM 依赖的纯函数部分）：
// 保本位模式 exitStopBeMode（2026-10-10）内嵌进保本止损卡，两选项 + 默认 extreme 选中
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('py_chain/web/params.html', 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
// 顶层立即执行的 init IIFE / 事件绑定被假 DOM 吞掉（node() 全兼容），渲染函数可直接调用
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {
    value: '', textContent: '', innerHTML: '', style: {}, hidden: false,
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    setAttribute() {}, getAttribute: () => null,
    addEventListener() {}, removeEventListener() {},
    querySelector: () => ({ textContent: '', classList, querySelectorAll: () => [] }),
    querySelectorAll: () => [], appendChild() {},
  });
  return nodes.get(id);
};
const ctx = {
  console, setTimeout: () => 1, clearTimeout() {},
  fetch: () => Promise.reject(new Error('no-network')),
  location: { search: '', origin: 'http://localhost' },
  window: { addEventListener() {} },
  document: {
    getElementById: node,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
  },
};
vm.createContext(ctx);
vm.runInContext(script, ctx);

// 假模块数据（schema/effective/overrides 三件套，形状同 /api/params）
const schema = {
  exitStopBeOn: { label: '保本止损', desc: 'd', default: true },
  exitStopBePct: { label: '保本止损平仓比例', desc: 'd', min: 1, max: 100, default: 100 },
  exitStopBeMode: {
    label: '保本位模式', desc: '保本位=…', default: 'extreme',
    choices: ['extreme', 'entry'],
  },
  exitTrailOn: { label: '跟踪止盈', desc: 'd', default: false },
  exitTrailPct: { label: '跟踪止盈平仓比例', desc: 'd', min: 1, max: 100, default: 100 },
  exitTrailSrc: { label: '跟踪止盈参照', desc: 'd', default: 'detect', choices: ['detect', 'mark'] },
  exitStopSrOn: { label: '支阻止损', desc: 'd', default: true },
  exitStopSrPct: { label: '支阻止损平仓比例', desc: 'd', min: 1, max: 100, default: 100 },
  exitHalfMrOn: { label: '背驰周期够笔', desc: 'd', default: false },
  exitHalfMrPct: { label: '背驰周期够笔平仓比例', desc: 'd', min: 1, max: 100, default: 50 },
  exitHalfOn: { label: '检测周期够笔', desc: 'd', default: true },
  exitHalfPct: { label: '检测周期够笔平仓比例', desc: 'd', min: 1, max: 100, default: 50 },
  exitCloseOn: { label: '过高低点止盈', desc: 'd', default: true },
  exitClosePct: { label: '过高低点止盈平仓比例', desc: 'd', min: 1, max: 100, default: 100 },
};
const mk = (eff, ovr = {}) => ({ schema, effective: eff, overrides: ovr });

// ① 默认 extreme：保本止损卡内渲染下拉，extreme 选中、两选项齐全
let h = vm.runInContext('exitModeRowsHtml', ctx)(mk({
  ...Object.fromEntries(Object.entries(schema).map(([k, s]) => [k, s.default])),
  exitStopBeMode: 'extreme',
}));
const cards = h.match(/<div class="prow[^"]*"[^>]*title="d">/g) || [];
assert.ok(h.includes('data-k="exitStopBeMode"'), 'stopBe 卡缺 exitStopBeMode 下拉');
const sel = h.match(/<select data-k="exitStopBeMode"[\s\S]*?<\/select>/)[0];
assert.ok(sel.includes('value="extreme" selected'), 'extreme 应选中');
assert.ok(sel.includes('value="entry"') && !sel.includes('value="entry" selected'), 'entry 应存在且未选中');
assert.ok(sel.includes('0亏损(进场价)') && sel.includes('当前(极值±滑点)'), '选项展示名缺失');

// ② 覆盖为 entry：entry 选中 + 卡片亮「已修改」徽章（isMod 含 exitStopBeMode override）
h = vm.runInContext('exitModeRowsHtml', ctx)(mk({
  ...Object.fromEntries(Object.entries(schema).map(([k, s]) => [k, s.default])),
  exitStopBeMode: 'entry',
}, { exitStopBeMode: 'entry' }));
const sel2 = h.match(/<select data-k="exitStopBeMode"[\s\S]*?<\/select>/)[0];
assert.ok(sel2.includes('value="entry" selected'), 'entry 覆盖后应选中');
const beCard = h.split('data-k="exitStopBeOn"')[1].split('</div>')[0];
assert.ok(beCard.includes('badge'), 'exitStopBeMode 覆盖应点亮已修改徽章');

// ③ 下拉不单独成行（grouped 拦截）：entryRowsHtml 不含独立的保本位模式参数行
let rows = vm.runInContext('entryRowsHtml', ctx)(mk({
  ...Object.fromEntries(Object.entries(schema).map(([k, s]) => [k, s.default])),
  exitStopBeMode: 'extreme',
  near: 5, lots: 4, slip_stop: 3, slip_stop_atr_k: 0, slip_fallback: 10,
  slip_fallback_atr_k: 0, slip_be: 3, slip_be_atr_k: 0, exitTrailSlip: 1,
  exit_min_merged: 5, realtime_min_bars: 5, zs_exit_weak_ratio: 1,
}));
const standalone = [...rows.matchAll(/<span class="plabel">(保本位模式|跟踪止盈参照)</g)];
assert.equal(standalone.length, 0, '内嵌下拉不应再渲染独立参数行');

// ④ choiceLabel 展示名兜底：未知值原样展示
assert.equal(vm.runInContext('choiceLabel', ctx)('exitStopBeMode', 'extreme'), '当前(极值±滑点)');
assert.equal(vm.runInContext('choiceLabel', ctx)('exitStopBeMode', 'x'), 'x');
console.log('params.html 保本位模式卡渲染冒烟 OK');
