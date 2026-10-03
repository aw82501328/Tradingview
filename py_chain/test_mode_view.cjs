const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('py_chain/web/index.html','utf8');
let script=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].find(m=>m[1].includes('const MODES'))[1].replace(/init\(\);\s*$/, '');
// 共享渲染核心（el/esc/signalRowsHtml 等）已拆到 bt_common.js，先于页面脚本加载
const common=fs.readFileSync('py_chain/web/bt_common.js','utf8');
const nodes=new Map();const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',innerHTML:'',style:{},hidden:false,setAttribute(){},getAttribute:()=>null,addEventListener(){},removeEventListener(){},querySelector:()=>({textContent:''}),querySelectorAll:()=>[]});return nodes.get(id)};
const ctx={console,URLSearchParams,setTimeout:()=>1,clearTimeout(){},location:{search:'?mode=bad',origin:'http://localhost'},window:{addEventListener(){}},document:{getElementById:node,querySelector:()=>({firstChild:{textContent:''}})}};
vm.createContext(ctx);vm.runInContext(common,ctx);vm.runInContext(script,ctx);
// 回测策略TAB（多策略并行）：init 被剥离，手动注入策略清单（catalog 同源形状）
vm.runInContext(`BT_STRATEGIES=[{id:'chan_v1',name:'缠论V1'},{id:'fxma_v1',name:'强分型均线V1'}];curBtTab='chan_v1';`,ctx);
vm.runInContext(`
if(selectedMode!=='backtest')throw Error('invalid mode default');
sigRows=[{id:1,mode:'backtest',status:'已平仓',pnl:10},{id:2,mode:'live',status:'已平仓',pnl:999},{id:3,mode:'backtest',status:'信号'}];
selectMode('backtest');
if(filteredRows().length!==2)throw Error('cross-mode rows');
el('filter-status').value='已平仓';
selectMode('live');
if(filteredRows().length!==1||filteredRows()[0].pnl!==999)throw Error('live rows');
selectMode('backtest');
if(el('filter-status').value!=='已平仓'||filteredRows().length!==1)throw Error('filter lost');
if(el('card-bt-chan_v1').hidden||!el('card-bt-fxma_v1').hidden||!el('card-live').hidden)throw Error('visibility');
if(el('pnl-summary').innerHTML.includes('999'))throw Error('cross-mode summary');
applyStatus({active:'live',modes:{live:{state:'running',progress:{pct:25,current:1,total:4}},backtest:{state:'idle'}},
             backtests:{chan_v1:{state:'idle'},fxma_v1:{state:'idle'}}});
selectMode('live');selectMode('backtest');
if(state.modes.live.state!=='running'||el('bar-live').style.width!=='25%')throw Error('hidden task state lost');
if(!el('btn-start-bt-chan_v1').disabled||!el('btn-start-bt-fxma_v1').disabled)throw Error('mutex status lost');
`,ctx);
assert.equal(node('count').textContent,'筛选 1 / 共 2 条');
// 策略/方向多选过滤：按 strategyKey 与 方向列口径（planDirection；旧记录回退策略映射）过滤
vm.runInContext(`
sigRows=[{id:1,mode:'backtest',status:'已平仓',pnl:10,strategyKey:'waitSell',planDirection:'空头空'},
  {id:2,mode:'backtest',status:'已平仓',pnl:20,strategyKey:'waitBuy',planDirection:'多头多'},
  {id:3,mode:'backtest',status:'已平仓',pnl:30,strategyKey:'wait1Sell',planDirection:null}];
sigFilterValues = n => n==='strategy' ? ['waitSell','wait1Sell'] : [];
if(filteredRows().length!==2)throw Error('strategy filter');
sigFilterValues = n => n==='direction' ? ['多头多'] : [];
if(filteredRows().length!==1||filteredRows()[0].id!==2)throw Error('direction filter');
sigFilterValues = n => n==='direction' ? ['空头空'] : [];
if(filteredRows().length!==1||filteredRows()[0].id!==1)throw Error('direction filter excludes other fallbacks');
sigFilterValues = n => n==='direction' ? ['多头空'] : [];
if(filteredRows().length!==1||!filteredRows().some(r=>r.id===3))throw Error('direction fallback (wait1Sell→多头空)');
`,ctx);
// 多策略并行：行按策略TAB隔离（旧行无 strategy 字段回落缠论V1）；切换 TAB 后各自可见
vm.runInContext(`
sigFilterValues = n => [];
sigRows=[{id:11,mode:'backtest',status:'已平仓',pnl:10,strategy:'chan_v1'},
  {id:12,mode:'backtest',status:'已平仓',pnl:20,strategy:'fxma_v1'},
  {id:13,mode:'backtest',status:'已平仓',pnl:30}];
if(filteredRows().length!==2||filteredRows().some(r=>r.id===12))throw Error('chan tab isolation');
selectBtTab('fxma_v1');
if(filteredRows().length!==1||filteredRows()[0].id!==12)throw Error('fxma tab isolation');
if(!el('card-bt-fxma_v1').hidden===false&&el('card-bt-chan_v1').hidden===false)throw Error('tab card switch');
selectBtTab('chan_v1');
if(displayNo(sigRows[0])!==1)throw Error('display no per strategy');
`,ctx);
console.log('Mode selection, visibility, filter retention, count and summary checks passed');
console.log('Strategy/direction multi-select filter checks passed');
console.log('Per-strategy tab isolation checks passed');
