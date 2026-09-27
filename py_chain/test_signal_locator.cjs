const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('py_chain/web/index.html', 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].find(m=>m[1].includes('const MODES'))[1].replace(/init\(\);\s*$/, '');
// 共享渲染核心（el/esc/signalRowsHtml/exitDisplayRows 等）已拆到 bt_common.js，先于页面脚本加载
const common = fs.readFileSync('py_chain/web/bt_common.js', 'utf8');
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {value:'', textContent:'', innerHTML:'', style:{}, hidden:false, setAttribute(){}, getAttribute:()=>null, querySelector:()=>({textContent:''}), querySelectorAll:()=>[]});
  return nodes.get(id);
};
let resolvePost, calls = [], job = {jobId:'job-1',id:2,mode:'backtest',state:'running'};
const storage = {};   // localStorage 桩：保留K线持久化（bt_common.js）在 vm 里走这
const context = {
  console, URLSearchParams, setTimeout:()=>1, clearTimeout(){},
  location:{search:'',origin:'http://localhost'}, window:{addEventListener(){}},
  document:{getElementById:node,querySelector:()=>({firstChild:{textContent:''}})},
  localStorage:{getItem:k=>(k in storage?storage[k]:null),setItem:(k,v)=>{storage[k]=String(v)}},
  fetch: async (url, opts) => {
    calls.push({url,opts});
    if (opts?.method === 'POST') return new Promise(resolve=>{resolvePost=resolve});
    return {ok:true,json:async()=>({ok:true,job})};
  }
};
vm.createContext(context); vm.runInContext(common, context); vm.runInContext(script,context);
const run = code => vm.runInContext(code,context);
const flush = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  run(`sigRows=[{id:1,mode:'backtest',status:'信号',pnl:null},{id:2,mode:'backtest',status:'已平仓',pnl:-10},{id:3,mode:'backtest',status:'已平仓',pnl:100},{id:4,mode:'live',status:'信号'}]; bindSignalRows(); togglePnlSort();`);
  assert.ok(node('sig-body').innerHTML.indexOf('data-signal-id="3"') < node('sig-body').innerHTML.indexOf('data-signal-id="2"'));
  node('sig-body').onclick({type:'click',target:{closest:()=>({dataset:{signalId:'2'}})}});
  assert.deepEqual(JSON.parse(calls[0].opts.body),{mode:'backtest',id:2,after_bars:60,colors:{buy:'',sell:'',exit:'',sr:''}});
  node('mark-color-buy').value='#F23645'; node('mark-color-sell').value='#089981';
  node('mark-color-exit').value='#FFEB3B'; node('mark-color-sr').value='#787B86';
  assert.match(node('sig-body').innerHTML,/data-signal-id="2"[^>]*class="selected"/);
  assert.equal(node('btn-mark-draw').disabled,true);
  await run('locateRow(3)'); assert.equal(calls.length,1);
  run(`togglePnlSort();el('filter-status').value='已平仓';sigRows[1].pnl=200;renderTable();`);
  assert.match(node('sig-body').innerHTML,/data-signal-id="2"[^>]*class="selected"/);
  // Completion before POST returns is recovered by the immediate status query.
  job = {...job,state:'done',result:{symbol:'OANDA:XAUUSD',markRes:'3',time:100,mark:{drawn:2,cleared:1}}};
  resolvePost({ok:true,json:async()=>({ok:true,job:{...job,state:'running'}})});
  await flush(); await flush();
  assert.match(node('locate-status').textContent,/#2 已定位并标记 2 个/);
  assert.equal(run('locatePending'),null);
  run(`selectMode('live');selectMode('backtest');`);
  assert.match(node('sig-body').innerHTML,/data-signal-id="2"[^>]*class="selected"/);
  run(`state.modes.replay={state:'paused'};locateRow(3);`);
  assert.match(node('locate-status').textContent,/任务占用/);
  run(`state.modes.replay={state:'idle'};`);
  let prevented=false;
  node('sig-body').onkeydown({type:'keydown',key:'Enter',preventDefault(){prevented=true},target:{closest:()=>({dataset:{signalId:'1'}})}});
  assert.equal(prevented,true);
  resolvePost({ok:false,json:async()=>({ok:false,error:'旧记录缺少品种，请重新回测后定位'})});
  await flush();
  assert.match(node('locate-status').textContent,/旧记录缺少品种/);
  assert.equal(run('locatePending'),null);
  // 定位后保留K线持久化：存储值驱动请求值（vm 桩无输入框 → locateAfterBars 回退读 localStorage）
  // calls 序列：[0]=首次定位 POST [1]=轮询 GET [2]=id1 错误 POST，本次点击是 [3]
  run('storeAfterBars(7)');
  node('sig-body').onclick({type:'click',target:{closest:()=>({dataset:{signalId:'2'}})}});
  assert.equal(JSON.parse(calls[3].opts.body).after_bars,7);
  assert.equal(storage['bt-locate-after-bars'],'7');
  resolvePost({ok:true,json:async()=>({ok:true,job:{...job,state:'done',result:{symbol:'TEST:X',markRes:'3',time:1,mark:{drawn:0}}}})});
  await flush(); await flush();
  assert.equal(run('locatePending'),null);
  run(`sigRows=[];renderTable();`);
  assert.equal(run('selectedSignal.backtest'),null);
  console.log('PASS: sorted row IDs, selection across updates/filter/modes, duplicate clicks, keyboard, busy state, fast completion recovery and errors, persisted after_bars');
})().catch(error=>{console.error(error);process.exitCode=1});
