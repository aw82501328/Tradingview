// 明细视图的异步会话与交互回归。真实布局在浏览器验收，不用 DOM 桩断言 CSS。
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('py_chain/web/index.html', 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].find(m => m[1].includes('const MODES'))[1].replace(/init\(\);\s*$/, '');
const nodes = new Map(), events = {}, requests = [];
const node = id => {
  if (!nodes.has(id)) {
    const attrs = {}, classes = new Set();
    nodes.set(id, {id, value:'', textContent:'', innerHTML:'', style:{}, hidden:false, scrollTop:0, scrollLeft:0,
      isConnected:true, open:false, tabIndex:0,
      setAttribute(k,v){attrs[k]=v}, getAttribute(k){return attrs[k]},
      querySelectorAll(){return []}, querySelector(){return null},
      focus(){context.document.activeElement=this},
      classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),toggle(c,on){on?classes.add(c):classes.delete(c)}},
    });
  }
  return nodes.get(id);
};
const context = {console,URLSearchParams,setTimeout:()=>1,clearTimeout(){},requestAnimationFrame(){},
  location:{search:'',origin:'http://localhost'},window:{addEventListener(){}},
  document:{getElementById:node,body:{style:{overflow:'auto'}},activeElement:null,fullscreenElement:null,
    addEventListener:(type,fn)=>events[type]=fn,querySelector:()=>({firstChild:{textContent:''}})},
  fetch:(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve})),
};
vm.createContext(context);vm.runInContext(script,context);
const run = code => vm.runInContext(code,context);
// 图表的 SVG DOM 不属于本状态测试；保留输入序列供核对。
run(`drawEquityChart=(host,series,opts)=>{host._eqSeries=series;host._eqOpts=opts};`);
const fixture = (id, analyzed=true) => ({id,name:'方案 '+id,cfg:{symbol:'TEST:X',periods:['15','3'],from:'2026-01-01'},
  signals:[{id:21,status:'已平仓',direction:'long',pnl:10,time:100,entryPrice:100,entryTime:110,exitTime:150,exitPrice:110,exitType:'close',lots:1},
    {id:22,status:'已平仓',direction:'short',pnl:-5,time:120,entryPrice:100,exitTime:160,exitPrice:105,exitType:'stopSr',lots:1}],
  summary:{closed:2,win:1,lose:1,realized:5,floating:0,win_rate:50,payoff_ratio:2,avg_win:10,avg_loss:5,
    equity:[{t:100,v:0},{t:150,v:10},{t:160,v:5}],
    ...(analyzed?{loss_analysis:{lookahead:20,lose:1,direction:{count:1,pnl:-5},entry_exit:{count:0,pnl:0}}}:{})},
});
const respond = (request, data, ok=true) => request.resolve({ok,status:ok?200:500,json:async()=>data});
const settle = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  node('bt-detail-modal').hidden=true;
  context.document.activeElement=node('launch-detail');
  run('bindBtRuns()');
  const a=run(`openBtDetail('a')`), b=run(`openBtDetail('b')`);
  respond(requests[1],{ok:true,run:fixture('b')});await b;
  respond(requests[0],{ok:true,run:fixture('a')});await a;
  assert.equal(run('btDetailRunId'),'b','a late detail fetch must not replace b');
  assert.equal(run('btDetailTab'),'overview');
  assert.equal(context.document.body.style.overflow,'hidden');
  assert.equal(node('bt-detail-tab-overview').getAttribute('aria-selected'),'true');
  // Keyboard selection updates both tabs and panel visibility.
  node('bt-detail-overview').scrollTop=210;
  node('bt-detail-tabs').onkeydown({key:'End',preventDefault(){}});
  assert.equal(run('btDetailTab'),'records');
  assert.equal(node('bt-detail-overview').hidden,true);
  assert.equal(node('bt-detail-records').hidden,false);
  assert.equal(context.document.activeElement.id,'bt-detail-tab-records');
  node('bt-detail-lookahead').value='20';
  run(`btDetailSelectedId=22;toggleBtDetailPnlSort()`);
  node('bt-detail-table-scroll').scrollTop=180;
  node('bt-detail-table-scroll').scrollLeft=450;
  node('bt-detail-params').open=true;
  const analysis=run('analyzeBtDetail()');
  assert.equal(run('btAnalyzeBusy'),true);
  const response=fixture('b');response.summary.loss_analysis.lookahead=30;
  respond(requests.at(-1),{ok:true,run:response});await analysis;
  assert.equal(run('btDetailTab'),'records');
  assert.equal(run('btDetailPnlSort'),'descending');
  assert.equal(run('btDetailSelectedId'),22);
  assert.equal(node('bt-detail-table-scroll').scrollTop,180);
  assert.equal(node('bt-detail-table-scroll').scrollLeft,450);
  assert.equal(node('bt-detail-overview').scrollTop,210);
  assert.equal(node('bt-detail-params').open,true);
  assert.equal(node('bt-detail-lookahead').value,'20','keep an edited input separate from result lookahead');
  assert.deepEqual(JSON.parse(JSON.stringify(node('bt-detail-equity')._eqSeries[0].points)),response.summary.equity);
  const rendered=node('bt-detail-sig-body').innerHTML;
  assert.ok(rendered.indexOf('data-signal-id="21"')<rendered.indexOf('data-signal-id="22"'));
  assert.match(rendered,/data-signal-id="22"[^>]*class="selected"/);
  // Close while analysis is pending; its completion must not reopen the dialog.
  const old=run('analyzeBtDetail()');const oldReq=requests.at(-1);
  run('closeBtDetail()');
  assert.equal(context.document.activeElement.id,'launch-detail');
  assert.equal(context.document.body.style.overflow,'auto');
  respond(oldReq,{ok:true,run:fixture('b')});await old;
  assert.equal(node('bt-detail-modal').hidden,true);
  assert.equal(run('btDetailRunId'),null);
  // Old analysis finally must not clear the new session's busy flag.
  const c=run(`openBtDetail('c')`);respond(requests.at(-1),{ok:true,run:fixture('c')});await c;
  const cAnalysis=run('analyzeBtDetail()');const cReq=requests.at(-1);
  const d=run(`openBtDetail('d')`);respond(requests.at(-1),{ok:true,run:fixture('d',false)});await settle();
  const dReq=requests.at(-1);
  respond(cReq,{ok:true,run:fixture('c')});await cAnalysis;
  assert.equal(run('btAnalyzeBusy'),true);
  assert.equal(run('btDetailRunId'),'d');
  respond(dReq,{ok:false,error:'测试网络失败'},false);await d;
  assert.match(node('bt-detail-analysis-status').textContent,/测试网络失败/);
  assert.equal(run('btAnalyzeBusy'),false);
  // Invalid input does not issue a request.
  node('bt-detail-lookahead').value='1.5';const n=requests.length;await run('analyzeBtDetail()');
  assert.equal(requests.length,n);
  assert.match(node('bt-detail-analysis-status').textContent,/正整数/);
  // Loading error supports retry; closing invalidates a pending load.
  const error=run(`openBtDetail('error')`);respond(requests.at(-1),{ok:false,error:'未找到方案'},false);await error;
  assert.match(node('bt-detail-body').innerHTML,/重新加载/);
  const retry=node('bt-detail-retry').onclick();run('closeBtDetail()');
  respond(requests.at(-1),{ok:true,run:fixture('error')});await retry;
  assert.equal(node('bt-detail-modal').hidden,true);
  // Late chart-location jobs still release global busy state without touching the new detail.
  run(`btDetailLocateMsg='新方案';locatePending={mode:'backtest',id:21,source:'detail',detailEpoch:btDetailEpoch-1,jobId:'old'};
    finishLocate({jobId:'old',state:'error',error:'old failure'});`);
  assert.equal(run('btDetailLocateMsg'),'新方案');assert.equal(run('locatePending'),null);
  console.log('PASS: detail tabs, retained view state, snapshot equity, stale fetch/analysis/location, retry and focus restore');
})().catch(e=>{console.error(e);process.exitCode=1});
