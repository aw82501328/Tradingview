// 方案明细独立页（bt_detail.html）的异步会话与交互回归。真实布局在浏览器验收，不用 DOM 桩断言 CSS。
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('py_chain/web/bt_detail.html', 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].find(m => m[1].includes('DETAIL_PAGE'))[1].replace(/boot\(\);\s*$/, '');
// 共享渲染核心先于页面脚本加载（与真实页面 <script src="/bt_common.js"> 顺序一致）
const common = fs.readFileSync('py_chain/web/bt_common.js', 'utf8');
const nodes = new Map(), events = {}, requests = [], storage = {};
const node = id => {
  if (!nodes.has(id)) {
    const attrs = {}, classes = new Set();
    nodes.set(id, {id, value:'', textContent:'', innerHTML:'', style:{}, hidden:false, scrollTop:0, scrollLeft:0,
      isConnected:true, open:false, tabIndex:0,
      setAttribute(k,v){attrs[k]=v}, getAttribute(k){return attrs[k]},
      addEventListener(){}, removeEventListener(){},
      querySelectorAll(){return []}, querySelector(){return null},
      focus(){context.document.activeElement=this},
      classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),toggle(c,on){on?classes.add(c):classes.delete(c)}},
    });
  }
  return nodes.get(id);
};
const context = {console,URLSearchParams,setTimeout:()=>1,clearTimeout(){},requestAnimationFrame(){},
  location:{search:'?id=m',origin:'http://localhost'},window:{addEventListener(){}},
  document:{getElementById:node,body:{style:{}},activeElement:null,fullscreenElement:null,
    addEventListener:(type,fn)=>events[type]=fn,querySelector:()=>null},
  localStorage:{getItem:k=>(k in storage?storage[k]:null),setItem:(k,v)=>{storage[k]=String(v)}},   // 保留K线持久化的可读写桩
  fetch:(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve})),
};
vm.createContext(context);vm.runInContext(common,context);vm.runInContext(script,context);
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
  run('bindDetailPage()');
  // 迟到的明细响应不得覆盖后发起的加载（token 守卫）
  const a=run(`loadDetail('a')`), b=run(`loadDetail('b')`);
  respond(requests[1],{ok:true,run:fixture('b')});await b;
  respond(requests[0],{ok:true,run:fixture('a')});await a;
  assert.equal(run('btDetailRunId'),'b','a late detail fetch must not replace b');
  assert.equal(run('btDetailTab'),'overview');
  assert.equal(node('bt-detail-tab-overview').getAttribute('aria-selected'),'true');
  assert.equal(node('bt-detail-locatebar').hidden,true,'locatebar hidden on overview tab');
  // Keyboard selection updates both tabs and panel visibility.
  node('bt-detail-overview').scrollTop=210;
  node('bt-detail-tabs').onkeydown({key:'End',preventDefault(){}});
  assert.equal(run('btDetailTab'),'records');
  assert.equal(node('bt-detail-overview').hidden,true);
  assert.equal(node('bt-detail-records').hidden,false);
  assert.equal(node('bt-detail-locatebar').hidden,false,'locatebar visible on records tab');
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
  assert.equal(context.document.title,'方案 b · 缠论回测','title follows the plan name');
  assert.deepEqual(JSON.parse(JSON.stringify(node('bt-detail-equity')._eqSeries[0].points)),response.summary.equity);
  const rendered=node('bt-detail-sig-body').innerHTML;
  assert.ok(rendered.indexOf('data-signal-id="21"')<rendered.indexOf('data-signal-id="22"'));
  assert.match(rendered,/data-signal-id="22"[^>]*class="selected"/);
  // 点行定位：POST 快照分支 → 轮询完成写状态条（点击委托在 #bt-detail-body 上）
  const rowClick = sid => node('bt-detail-body').onclick({type:'click',target:{closest:sel=>sel==='#bt-detail-sig-body tr[data-signal-id]'?{dataset:{signalId:String(sid)}}:null}});
  rowClick(21);
  assert.equal(requests.at(-1).url,'/api/signals/locate');
  assert.equal(JSON.parse(requests.at(-1).opts.body).id,21);
  assert.equal(JSON.parse(requests.at(-1).opts.body).after_bars,60,'default after_bars when nothing stored');
  respond(requests.at(-1),{ok:true,job:{jobId:'j1',state:'running'}});
  await settle();
  respond(requests.at(-1),{ok:true,job:{jobId:'j1',state:'done',result:{symbol:'TEST:X',markRes:'3',time:100,mark:{drawn:2}}}});
  await settle();await settle();
  assert.match(node('bt-detail-locate').textContent,/#1 已定位并标记 2 个/);
  assert.equal(run('locatePending'),null);
  // 服务端拒绝（图表被占用 409）：状态条提示原文；持久化的保留K线驱动请求值（vm 桩无输入框 → 回退读 localStorage）
  run('storeAfterBars(5)');
  rowClick(22);
  assert.equal(JSON.parse(requests.at(-1).opts.body).after_bars,5,'stored after_bars drives locate request');
  respond(requests.at(-1),{ok:false,error:'图表正被任务占用，请结束运行或暂停中的任务，并等待图表操作完成后再定位'},false);
  await settle();
  assert.match(node('bt-detail-locate').textContent,/图表正被任务占用/);
  // exitDisplayRows 合约乘数（2026-09-23）：行带 mult → 半平重算同乘；旧行无 mult 缺省 1
  const scaled=run(`exitDisplayRows({direction:'long',entryPrice:100,lots:2,mult:50,pnl:900,
    exits:[{type:'half',time:1050,price:130}],exitTime:1100,exitPrice:110,exitType:'close',state:'closed'})`);
  assert.equal(scaled[0].pnl,(130-100)*1*50);            // 半平 (130-100)×halfLots(1)×mult(50)
  assert.equal(scaled[1].pnl,900-(130-100)*1*50);        // 终局 = 整笔 − 半平
  const legacy=run(`exitDisplayRows({direction:'long',entryPrice:100,lots:2,pnl:900,
    exits:[{type:'half',time:1050,price:130}],exitTime:1100,exitPrice:110,exitType:'close',state:'closed'})`);
  assert.equal(legacy[0].pnl,(130-100)*1);               // 旧存档行：mult 缺省 1，数字不变
  // 旧方案的分析在途中加载新方案：旧分析完成不得复位新会话的 busy，也不得覆盖内容
  const c=run(`loadDetail('c')`);respond(requests.at(-1),{ok:true,run:fixture('c')});await c;
  const cAnalysis=run('analyzeBtDetail()');const cReq=requests.at(-1);
  const d=run(`loadDetail('d')`);respond(requests.at(-1),{ok:true,run:fixture('d',false)});await settle();
  const dReq=requests.at(-1);
  respond(cReq,{ok:true,run:fixture('c')});await cAnalysis;
  assert.equal(run('btAnalyzeBusy'),true,'d auto-analysis still busy');
  assert.equal(run('btDetailRunId'),'d');
  respond(dReq,{ok:false,error:'测试网络失败'},false);await d;
  assert.match(node('bt-detail-analysis-status').textContent,/测试网络失败/);
  assert.equal(run('btAnalyzeBusy'),false);
  // Invalid input does not issue a request.
  node('bt-detail-lookahead').value='1.5';const n=requests.length;await run('analyzeBtDetail()');
  assert.equal(requests.length,n);
  assert.match(node('bt-detail-analysis-status').textContent,/正整数/);
  // Loading error supports retry; a superseding load invalidates the pending retry.
  const error=run(`loadDetail('error')`);respond(requests.at(-1),{ok:false,error:'未找到方案'},false);await error;
  assert.match(node('bt-detail-body').innerHTML,/重新加载/);
  const retry=node('bt-detail-retry').onclick();
  const f=run(`loadDetail('f')`);
  respond(requests.at(-2),{ok:true,run:fixture('error')});   // retry 的迟到响应：token 已过期，丢弃
  respond(requests.at(-1),{ok:true,run:fixture('f')});await f;await retry;
  assert.equal(run('btDetailRunId'),'f');
  // 旧会话的定位结果（换方案后到达）不得改写新方案的状态条
  run(`locatePending={mode:'backtest',id:21,runId:'old',jobId:'old'};
    finishLocate({jobId:'old',state:'error',error:'old failure'});`);
  assert.equal(run('locatePending'),null);
  assert.doesNotMatch(node('bt-detail-locate').textContent,/old failure/);
  // 外来 jobId 一律忽略
  run(`locatePending={mode:'backtest',id:21,runId:'f',jobId:'cur'};
    finishLocate({jobId:'other',state:'done',result:{symbol:'X',markRes:'3',time:1,mark:null}});`);
  assert.equal(run('locatePending')?.jobId,'cur');
  console.log('PASS: detail page load/token races, retained view state, snapshot equity, locate flow + busy rejection, stale analysis/location, retry superseded');
})().catch(e=>{console.error(e);process.exitCode=1});
