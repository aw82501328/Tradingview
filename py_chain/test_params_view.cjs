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
  exitLockOn: { label: '锁定盈利', desc: 'd', default: true },
  exitLockPct: { label: '锁定盈利平仓比例', desc: 'd', min: 1, max: 100, default: 100 },
  exitLockPts: { label: '锁定盈利门槛', desc: 'd', min: 0, max: 10000, default: 10 },
  exitStopBeOn: { label: '保本止损', desc: 'd', default: true },
  exitStopBePct: { label: '保本止损平仓比例', desc: 'd', min: 1, max: 100, default: 100 },
  exitStopBeMode: {
    label: '保本位模式', desc: '保本位=…', default: 'extreme',
    choices: ['extreme', 'entry', 'bi'],
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
assert.ok(sel.includes('0亏损(进场价)') && sel.includes('极值±滑点') && sel.includes('笔端点±滑点'),
  '三口径选项展示名缺失');

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
assert.equal(vm.runInContext('choiceLabel', ctx)('exitStopBeMode', 'extreme'), '极值±滑点');
assert.equal(vm.runInContext('choiceLabel', ctx)('exitStopBeMode', 'x'), 'x');

// ⑤ 近等双顶整合卡（2026-10-26）：容差+5周期开关+两行为开关并一张卡；
//    chanRowsHtml 不再输出近等分组横线，近等键只渲染一次（卡内）
const chanSchema = (() => {
  const bool = (label, dflt) => ({ label, desc: label, default: dflt });
  const num = (label, dflt, min, max) => ({ label, desc: label, default: dflt, min, max });
  return {
    gapFilter: num('跳空成笔阈值', 0.5, 0, 5),
    wickMarkOn: bool('长影线修复', true),
    wickRatio: num('长影线比例阈值', 0.6, 0, 1),
    wickMinLen: num('长影线长度下限', 0.5, 0, 500),
    wickMinRange: num('长影线最小价差', 15, 0, 500),
    fractalSideRealWick: bool('分型邻侧影线真实价', true),
    endInnerRecoverOn: bool('端点内部极值恢复', true),
    innerExtremeGuardOn: bool('笔内极值守卫', true),
    wideBarOn: bool('单根长K豁免开关', true),
    wideBarPointsD: num('日线', 0, 0, 500),
    wideBarPoints240: num('4小时', 0, 0, 500),
    wideBarPoints60: num('1小时', 0, 0, 500),
    wideBarPoints15: num('15分钟', 0, 0, 500),
    wideBarPoints3: num('3分钟', 0, 0, 500),
    nearDoubleFixed: num('近等双顶固定容差', 2.0, 0.0, null),
    nearDoubleD: bool('近等双顶·日线', true),
    nearDouble240: bool('近等双顶·4小时', true),
    nearDouble60: bool('近等双顶·1小时', true),
    nearDouble15: bool('近等双顶·15分钟', true),
    nearDouble3: bool('近等双顶·3分钟', true),
    nearDoubleShiftLocked: bool('近等取后让位锁定', false),
    absorbMarkFast: bool('吸收成笔即标', true),
    synthIntrabarBars: bool('盘中合成K', true),
    pointEnoughForming: bool('够笔即出点', true),
    windowDays3: num('3分钟', 20, 1, 365),
    windowDays15: num('15分钟', 50, 1, 365),
    windowDays30S: num('30秒', 10, 1, 365),
  };
})();
const mkChan = (ovr = {}) => ({
  schema: chanSchema,
  effective: Object.fromEntries(Object.entries(chanSchema).map(([k, s]) => [k, s.default])),
  overrides: ovr,
});
let nd = vm.runInContext('nearDoubleRowHtml', ctx)(mkChan());
assert.ok(nd.includes('prow span-all'), '近等卡应占满一行');
assert.ok(nd.includes('近等双顶/双底（平台取后顶/后底）'), '近等卡标题缺失');
assert.ok(nd.includes('data-k="nearDoubleFixed"') && nd.includes('aria-label="近等双顶容差"'), '容差输入缺失');
for (const k of ['nearDoubleD', 'nearDouble240', 'nearDouble60', 'nearDouble15', 'nearDouble3',
                 'nearDoubleShiftLocked', 'absorbMarkFast']) {
  assert.ok(nd.includes(`data-k="${k}"`), `近等卡缺 ${k} 控件`);
}
assert.ok(!nd.includes(' mod'), '默认无 override 不应亮已修改徽章');
nd = vm.runInContext('nearDoubleRowHtml', ctx)(mkChan({ nearDouble3: false }));
assert.ok(nd.includes('prow span-all mod'), 'nearDouble3 覆盖应点亮已修改徽章');
const chanRows = vm.runInContext('chanRowsHtml', ctx)(mkChan());
assert.ok(!chanRows.includes('>近等双顶/双底（平台取后顶/后底）</div>'), '近等分组横线应移除');
assert.ok(!chanRows.includes('>近等双顶·3分钟<span class="badge">'), '近等周期开关不应再独立成行');
for (const k of ['nearDoubleFixed', 'nearDouble3', 'nearDoubleShiftLocked', 'absorbMarkFast']) {
  const n = (chanRows.match(new RegExp(`data-k="${k}"`, 'g')) || []).length;
  assert.equal(n, 1, `${k} 应只渲染一次（整合卡内）`);
}
console.log('params.html 保本位模式+近等双顶整合卡渲染冒烟 OK');
