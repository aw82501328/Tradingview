const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),zlib=require('zlib');
const c=require('../.cursor/skills/chan-core/scripts/chan_core.js');
const data=JSON.parse(zlib.gunzipSync(fs.readFileSync('py_chain/fixtures/near_double_xauusd_20260910.json.gz')));
const ts=s=>Date.parse(`2026-${s}+08:00`)/1000, oldTime=ts('08-07T00:00:00'),newTime=ts('08-07T08:00:00');
function setup(){const bars=data['60'],m=c.mergeBars(c.markWickBars(bars)),f=c.findFractals(m);return {bars,m,f,old:f.find(x=>x.time===oldTime),end:f.find(x=>x.time===newTime),ctx:c.makeBiLowerContext('60',data['15'])};}
function build(res,context=null,locked=null){const b=data[res],m=c.mergeBars(c.markWickBars(b));return c.fixBiExtremes(c.buildBi(c.findFractals(m),m,c.calcATR(b,14),c.calcMACD(b),locked,c.nearDoubleOn(res),context),m);}
test('frozen gold: only the two adjacent 60m strokes change; other structures retained',()=>{
 for(const res of Object.keys(data)){const a=build(res),b=build(res,c.makeBiLowerContext(res,data['15']));assert.equal(a.length,b.length);const changed=b.filter((x,i)=>JSON.stringify(x)!==JSON.stringify(a[i]));if(res==='60'){assert.equal(changed.length,2);assert.equal(changed[0].endTime,newTime);assert.equal(changed[0].endPrice,4229.875);assert.equal(changed[1].startTime,newTime);}else assert.deepEqual(b,a);}
});
test('lower confirmation: missing data, zero hist, one weak metric, cutoff and symmetric top',()=>{
 const {f,old,end,ctx}=setup();assert.equal(c.lowerEndpointWeaker(old,end,f,ctx),true);
 for(const context of [null,c.makeBiLowerContext('240',data['15']),c.makeBiLowerContext('60',data['15'].filter(b=>b.time>oldTime)),{...ctx,cutoff:newTime+2700}])assert.equal(c.lowerEndpointWeaker(old,end,f,context),false);
 assert.equal(c.lowerEndpointWeaker(old,end,f,{...ctx,cutoff:newTime+3600}),true);
 const weaken=(field,value)=>({...ctx,macd:ctx.macd.map(m=>m.time>=newTime-900&&m.time<=newTime+2700?{...m,[field]:value}:m)});
 assert.equal(c.lowerEndpointWeaker(old,end,f,weaken('macd',0)),false);
 assert.equal(c.lowerEndpointWeaker(old,end,f,weaken('dif',-10)),false);
 assert.equal(c.lowerEndpointWeaker(old,end,f,weaken('macd',-10)),false);
 const flip=f=>({...f,type:f.type==='top'?'bottom':'top',high:10000-f.low,low:10000-f.high});
 const mirrored={...ctx,bars:ctx.bars.map(b=>({...b,high:10000-b.low,low:10000-b.high})),macd:ctx.macd.map(m=>({...m,macd:-m.macd,dif:-m.dif,dea:-m.dea}))};
 assert.equal(c.lowerEndpointWeaker(flip(old),flip(end),f.map(flip),mirrored),true);
});
test('original threshold, extended boundary and locked endpoint are preserved',()=>{
 const {bars,m,f,old,end,ctx}=setup(),atr=c.calcATR(bars,14),macd=c.calcMACD(bars),diff=end.low-old.low,thr=Math.max(atr*c.CHAN_CFG.nearDoubleAtrK,old.low*c.CHAN_CFG.nearDoublePct,c.CHAN_CFG.nearDoubleFixed),saved=c.CHAN_CFG.nearDoubleLowerRelax;
 try{for(const [ratio,expected] of [[1,oldTime],[diff/thr-1e-6,oldTime],[diff/thr+1e-6,newTime]]){c.CHAN_CFG.nearDoubleLowerRelax=ratio;const b=c.buildBi(structuredClone(f),m,atr,macd,null,true,ctx);assert.equal(b.find(x=>x.startTime===ts('08-06T10:00:00')).endTime,expected);}}finally{c.CHAN_CFG.nearDoubleLowerRelax=saved;}
 const b=build('60',ctx,[{dir:'bottom',price:old.low}]);assert.ok(b.some(x=>x.endTime===oldTime));
 const marked=structuredClone(f);marked.find(x=>x.time===oldTime).nearDouble=true;
 assert.ok(c.buildBi(marked,m,atr,macd,null,true,ctx).some(x=>x.endTime===oldTime));
});
test('fixed tolerance joins the union via max: below diff keeps old, >= diff takes new',()=>{
 const {bars,m,f,old,end,ctx}=setup(),atr=c.calcATR(bars,14),macd=c.calcMACD(bars),diff=end.low-old.low;
 // 破坏 15m 动量确认（NEW±窗口柱高置 0 → lower_confirmed=false，纯阈值路径）：
 // thr0≈4.65 < diff≈6.37，fixed 项主导边界翻转；平台回调深度 27.88 » thr，pull 恒成立
 const weak={...ctx,macd:ctx.macd.map(x=>x.time>=newTime-900&&x.time<=newTime+2700?{...x,macd:0}:x)},saved=c.CHAN_CFG.nearDoubleFixed;
 try{for(const [fixed,expected] of [[0,oldTime],[diff-1e-6,oldTime],[diff,newTime],[diff+10,newTime]]){c.CHAN_CFG.nearDoubleFixed=fixed;const b=c.buildBi(structuredClone(f),m,atr,macd,null,true,weak);assert.equal(b.find(x=>x.startTime===ts('08-06T10:00:00')).endTime,expected);}}finally{c.CHAN_CFG.nearDoubleFixed=saved;}
});
test('per-period switches flip nearDoubleOn; off reverts to plain build, on restructures 15m',()=>{
 const {bars,m,f,ctx}=setup(),atr=c.calcATR(bars,14),macd=c.calcMACD(bars);
 assert.equal(c.nearDoubleOn('60')&&c.nearDoubleOn('240')&&c.nearDoubleOn('D'),true);
 assert.equal(c.nearDoubleOn('3')||c.nearDoubleOn('15'),false);
 assert.equal(c.nearDoubleOn(3600)&&c.nearDoubleOn(14400)&&c.nearDoubleOn(86400),true);
 assert.equal(c.nearDoubleOn(180)||c.nearDoubleOn(900)||c.nearDoubleOn(30)||c.nearDoubleOn(0),false);
 assert.equal(c.nearDoubleOn('30S')||c.nearDoubleOn('W')||c.nearDoubleOn('5')||c.nearDoubleOn('30'),false);
 assert.equal(c.nearDoubleOn('1H')&&c.nearDoubleOn('4H')&&c.nearDoubleOn('1D'),true);
 const saved60=c.CHAN_CFG.nearDouble60,saved15=c.CHAN_CFG.nearDouble15;
 try{
  c.CHAN_CFG.nearDouble60=false;
  assert.equal(c.nearDoubleOn('60'),false);
  const off=build('60',ctx);
  assert.ok(!off.some(x=>x.endTime===newTime&&x.endPrice===4229.875));
  const direct=c.fixBiExtremes(c.buildBi(structuredClone(f),m,atr,macd,null,false,null),m);
  assert.deepEqual(off,direct);
  const m15=c.mergeBars(c.markWickBars(data['15'])),f15=c.findFractals(m15);
  const base15=c.buildBi(structuredClone(f15),m15,c.calcATR(data['15'],14),c.calcMACD(data['15']),null,false,null);
  c.CHAN_CFG.nearDouble15=true;
  assert.equal(c.nearDoubleOn('15'),true);
  const on15=c.buildBi(structuredClone(f15),m15,c.calcATR(data['15'],14),c.calcMACD(data['15']),null,true,null);
  assert.notEqual(JSON.stringify(on15),JSON.stringify(base15));
 }finally{c.CHAN_CFG.nearDouble60=saved60;c.CHAN_CFG.nearDouble15=saved15;}
});
test('both momentum ratios accept exactly 50%, reject over 50%; incomplete MACD falls back',()=>{
 const {f,old,end,ctx}=setup();
 const firstStart=ts('08-06T22:00:00'),firstEnd=oldTime+900,lastStart=newTime-900,lastEnd=newTime+2700;
 const altered=(hist,dif)=>({...ctx,macd:ctx.macd.map(m=>m.time>=firstStart&&m.time<=firstEnd?{...m,macd:-2,dif:-4}:m.time>=lastStart&&m.time<=lastEnd?{...m,macd:hist,dif}:m)});
 assert.equal(c.lowerEndpointWeaker(old,end,f,altered(-1,-2)),true);
 assert.equal(c.lowerEndpointWeaker(old,end,f,altered(-1.00001,-2)),false);
 assert.equal(c.lowerEndpointWeaker(old,end,f,altered(-1,-2.00001)),false);
 assert.equal(c.makeBiLowerContext('60',data['15'],Infinity,ctx.macd.map(m=>({time:m.time}))),null);
});
// ---- 近等后顶/后底（反弹不成笔）取后（2026-09-26）----
// 案例：60m 2026-09-18 顶 4399.67(15:00+8) → 底 4342.73(22:00+8，与23:00包含合并)
// → 顶 4397.045(9-19 01:00+8)，反弹腿仅 3 根合并K（gap=2<4）；近等差 2.625 ≤ thr。
// 240 层平台规则已把 4h 顶后移到 9-19 01:00 → 60m 须复现（locked 路径=区间套落地）。
const rdata=JSON.parse(zlib.gunzipSync(fs.readFileSync('py_chain/fixtures/near_double_rebound_xauusd_20260919.json.gz')));
const rStart=ts('09-18T11:00:00'),rOld=ts('09-18T15:00:00'),rNew=ts('09-19T01:00:00');
function rbuild(locked=null,nearDouble=true,cfg=null){
 const bars=rdata['60'],m=c.mergeBars(c.markWickBars(bars));
 const saved=cfg&&Object.fromEntries(Object.keys(cfg).map(k=>[k,c.CHAN_CFG[k]]));
 try{if(cfg)Object.assign(c.CHAN_CFG,cfg);
  return c.fixBiExtremes(c.buildBi(c.findFractals(m),m,c.calcATR(bars,14),c.calcMACD(bars),locked,nearDouble,null),m);
 }finally{if(cfg)Object.assign(c.CHAN_CFG,saved);}}
test('near-equal rebound (gap<4) shifts endpoint to later top: 9-18 11:00 → 9-19 01:00',()=>{
 let up=rbuild().find(x=>x.startTime===rStart);
 assert.equal(up.endTime,rNew);assert.equal(up.endPrice,4397.045);
 assert.equal(rbuild().find(x=>x.startTime===rNew).endPrice,4322.81);
 // 关 nearDoubleRebound 且无锁定 → 旧行为：端点停在前顶 4399.67
 up=rbuild(null,true,{nearDoubleRebound:false}).find(x=>x.startTime===rStart);
 assert.equal(up.endTime,rOld);assert.equal(up.endPrice,4399.67);
 // 关 nearDoubleRebound 但 k 为上级锁定端点（如 240 层已后移的 4397.05）→ 区间套落地不受开关限制
 up=rbuild([{dir:'top',price:4397.045}],true,{nearDoubleRebound:false}).find(x=>x.startTime===rStart);
 assert.equal(up.endTime,rNew);assert.equal(up.endPrice,4397.045);
 // 无锁定且非 nearDouble 周期（模拟 15m 口径）→ 不后移
 up=rbuild(null,false).find(x=>x.startTime===rStart);
 assert.equal(up.endTime,rOld);assert.equal(up.endPrice,4399.67);
});
test('near-equal rebound mirrors for double bottom',()=>{
 // 10000-x 镜像（顶↔底互换、MACD 取反）。注：上影 _topCand/下影 _origLow 的处理
 // 本身不对称，镜像的中间结构与原窗口不同——只断言本规则本身：后底（较高、较晚，
 // 同类型 <= 替换不可能产生）成为端点 ⇔ 开关开。
 const bars=rdata['60'].map(b=>({time:b.time,open:10000-b.open,high:10000-b.low,low:10000-b.high,close:10000-b.close}));
 const mirror=()=>{const m=c.mergeBars(c.markWickBars(bars));
  return c.fixBiExtremes(c.buildBi(c.findFractals(m),m,c.calcATR(bars,14),c.calcMACD(bars),null,true,null),m);};
 const on=mirror();
 const down=on.find(x=>x.endTime===rNew);
 assert.equal(down.type,'down');
 assert.equal(down.endPrice,10000-4397.045);   // 后底 5602.955 > 前底 5600.330：非 <= 替换可达
 const saved=c.CHAN_CFG.nearDoubleRebound;
 try{c.CHAN_CFG.nearDoubleRebound=false;
  assert.ok(!mirror().some(x=>x.endTime===rNew)); // 关开关（无锁定）→ 端点停在前底 09-18 15:00
 }finally{c.CHAN_CFG.nearDoubleRebound=saved;}
});
