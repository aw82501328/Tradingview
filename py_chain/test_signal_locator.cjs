const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('py_chain/web/index.html', 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].find(m=>m[1].includes('const MODES'))[1].replace(/init\(\);\s*$/, '');
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {value:'', textContent:'', innerHTML:'', style:{}});
  return nodes.get(id);
};
let resolvePost, calls = [], job = {jobId:'job-1',id:2,mode:'backtest',state:'running'};
const context = {
  console, URLSearchParams, setTimeout:()=>1, clearTimeout(){},
  location:{search:'',origin:'http://localhost'}, window:{addEventListener(){}},
  document:{getElementById:node,querySelector:()=>({firstChild:{textContent:''}})},
  fetch: async (url, opts) => {
    calls.push({url,opts});
    if (opts?.method === 'POST') return new Promise(resolve=>{resolvePost=resolve});
    return {ok:true,json:async()=>({ok:true,job})};
  }
};
vm.createContext(context); vm.runInContext(script,context);
const run = code => vm.runInContext(code,context);
const flush = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  run(`sigRows=[{id:1,mode:'backtest',status:'信号',pnl:null},{id:2,mode:'backtest',status:'已平仓',pnl:-10},{id:3,mode:'backtest',status:'已平仓',pnl:100},{id:4,mode:'live',status:'信号'}]; bindSignalRows(); togglePnlSort();`);
  assert.ok(node('sig-body').innerHTML.indexOf('data-signal-id="3"') < node('sig-body').innerHTML.indexOf('data-signal-id="2"'));
  node('sig-body').onclick({type:'click',target:{closest:()=>({dataset:{signalId:'2'}})}});
  assert.deepEqual(JSON.parse(calls[0].opts.body),{mode:'backtest',id:2});
  assert.match(node('sig-body').innerHTML,/data-signal-id="2"[^>]*class="selected"/);
  assert.equal(node('btn-mark-draw').disabled,true);
  await run('locateRow(3)'); assert.equal(calls.length,1);
  run(`togglePnlSort();el('filter-status').value='已平仓';sigRows[1].pnl=200;renderTable();`);
  assert.match(node('sig-body').innerHTML,/data-signal-id="2"[^>]*class="selected"/);
  // Completion before POST returns is recovered by the immediate status query.
  job = {...job,state:'done',result:{symbol:'OANDA:XAUUSD',markRes:'3',time:100}};
  resolvePost({ok:true,json:async()=>({ok:true,job:{...job,state:'running'}})});
  await flush(); await flush();
  assert.match(node('locate-status').textContent,/#2 已定位/);
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
  run(`sigRows=[];renderTable();`);
  assert.equal(run('selectedSignal.backtest'),null);
  console.log('PASS: sorted row IDs, selection across updates/filter/modes, duplicate clicks, keyboard, busy state, fast completion recovery and errors');
})().catch(error=>{console.error(error);process.exitCode=1});
