const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),zlib=require('zlib');
const c=require('../.cursor/skills/chan-core/scripts/chan_core.js');
const data=JSON.parse(zlib.gunzipSync(fs.readFileSync('py_chain/fixtures/near_double_xauusd_20260910.json.gz')));
const ts=s=>Date.parse(`2026-${s}+08:00`)/1000, oldTime=ts('08-07T00:00:00'),newTime=ts('08-07T08:00:00');
function setup(){const bars=data['60'],m=c.mergeBars(c.markWickBars(bars)),f=c.findFractals(m);return {bars,m,f,old:f.find(x=>x.time===oldTime),end:f.find(x=>x.time===newTime),ctx:c.makeBiLowerContext('60',data['15'])};}
function build(res,context=null,locked=null){const b=data[res],m=c.mergeBars(c.markWickBars(b));return c.fixBiExtremes(c.buildBi(c.findFractals(m),m,c.calcATR(b,14),c.calcMACD(b),locked,c.intervalSecOf(res)>=3600,context),m);}
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
 const {bars,m,f,old,end,ctx}=setup(),atr=c.calcATR(bars,14),macd=c.calcMACD(bars),diff=end.low-old.low,thr=Math.max(atr*.3,old.low*.001),saved=c.CHAN_CFG.nearDoubleLowerRelax;
 try{for(const [ratio,expected] of [[1,oldTime],[diff/thr-1e-6,oldTime],[diff/thr+1e-6,newTime]]){c.CHAN_CFG.nearDoubleLowerRelax=ratio;const b=c.buildBi(structuredClone(f),m,atr,macd,null,true,ctx);assert.equal(b.find(x=>x.startTime===ts('08-06T10:00:00')).endTime,expected);}}finally{c.CHAN_CFG.nearDoubleLowerRelax=saved;}
 const b=build('60',ctx,[{dir:'bottom',price:old.low}]);assert.ok(b.some(x=>x.endTime===oldTime));
 const marked=structuredClone(f);marked.find(x=>x.time===oldTime).nearDouble=true;
 assert.ok(c.buildBi(marked,m,atr,macd,null,true,ctx).some(x=>x.endTime===oldTime));
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
