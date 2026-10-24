// 周期链页签渲染冒烟（node vm；无 DOM 依赖的纯函数部分，2026-10-23 双周期链）：
// chainRowsHtml 渲染两条链的 开关+开单周期勾选 卡；30S/1 细层标签、勾选语义说明、
// TAB_GROUPS 页签注册。与 test_params_view.cjs 同加载方式（该文件在 HEAD 上已有
// exitModeRowsHtml 陈旧断言失败，本文件独立运行不受影响）。
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('py_chain/web/params.html', 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {
    value: '', textContent: '', innerHTML: '', style: {}, hidden: false,
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    setAttribute() {}, getAttribute: () => null,
    addEventListener() {}, removeEventListener() {},
    querySelector: () => ({ textContent: '', classList: { toggle() {} }, querySelectorAll: () => [] }),
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

// ① TAB_GROUPS：缠论V1 组含「周期链」页签
const groups = vm.runInContext('TAB_GROUPS', ctx);
const strat = groups.find(g => g.id === 'strategy');
assert.ok(strat.tabs.some(([id, label]) => id === 'chain' && label === '周期链'),
  '缠论V1 组缺少周期链页签');

// ② 假模块数据（与 param_center chain 模块同形）默认渲染：链A 开+60/15/3 勾、
//    链B 关+1/5/30 勾；240 均不勾；30S 带细层标注
const schema = {
  chainAOn: { label: '链A总开关', desc: 'd', default: true, type: 'bool' },
  chainA: { label: '链A开单周期', desc: 'd', default: '60,15,3', type: 'multi',
            choices: ['240', '60', '15', '3', '30S'] },
  chainBOn: { label: '链B总开关', desc: 'd', default: false, type: 'bool' },
  chainB: { label: '链B开单周期', desc: 'd', default: '1,5,30', type: 'multi',
            choices: ['240', '30', '5', '1'] },
};
const m = {
  schema, effective: { chainAOn: true, chainA: '60,15,3', chainBOn: false, chainB: '1,5,30' },
  overrides: {},
};
const h = vm.runInContext('chainRowsHtml', ctx)(m);
// 链开关卡
assert.ok(h.includes('data-k="chainAOn" checked'), '链A 开关应默认勾选');
assert.ok(h.includes('data-k="chainBOn"') && !h.includes('data-k="chainBOn" checked'),
  '链B 开关应默认不勾');
// 开单周期复选框：60/15/3 勾、240 不勾；1/5/30 勾
for (const c of ['60', '15', '3']) {
  assert.ok((h.match(new RegExp(`data-k="chainA" data-multi value="${c}" checked`, 'g')) || []).length === 1,
    `链A ${c} 应勾选`);
}
assert.ok(h.includes('data-k="chainA" data-multi value="240"') &&
          !h.includes('data-k="chainA" data-multi value="240" checked'), '链A 240 默认不勾');
assert.ok(!h.includes('data-k="chainA" data-multi value="30S" checked'), '链A 30S 默认不勾');
for (const c of ['1', '5', '30']) {
  assert.ok((h.match(new RegExp(`data-k="chainB" data-multi value="${c}" checked`, 'g')) || []).length === 1,
    `链B ${c} 应勾选`);
}
// 细层标签与勾选语义说明
assert.ok(h.includes('30秒(细层·仅回测)'), '30S 细层标签缺失');
assert.ok(h.includes('1分钟(细层)'), '1分钟细层标签缺失');
assert.ok(h.includes('勾=该周期作为开单周期'), '勾选语义说明缺失');
assert.ok(h.includes('互不影响'), '双链并行说明缺失');

// ③ 覆盖态：链B 开+240 勾 → 渲染勾选状态翻转（multi 解析逗号串）
const h2 = vm.runInContext('chainRowsHtml', ctx)({
  schema,
  effective: { chainAOn: true, chainA: '240,60', chainBOn: true, chainB: '240,30' },
  overrides: { chainBOn: true },
});
assert.ok(h2.includes('data-k="chainBOn" checked'), '链B 开关应勾选');
assert.ok(h2.includes('data-k="chainA" data-multi value="240" checked'), '链A 240 应勾选');
assert.ok(!h2.includes('data-k="chainA" data-multi value="15" checked'), '链A 15 应不勾');
assert.ok(h2.includes('data-k="chainB" data-multi value="240" checked'), '链B 240 应勾选');
assert.ok(!h2.includes('data-k="chainB" data-multi value="5" checked'), '链B 5 应不勾');

console.log('test_chain_view.cjs: 全部断言通过');
