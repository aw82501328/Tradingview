const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),zlib=require('zlib');
const c=require('../.cursor/skills/chan-core/scripts/chan_core.js');
const data=JSON.parse(zlib.gunzipSync(fs.readFileSync('py_chain/fixtures/near_double_xauusd_20260910.json.gz')));
const ts=s=>Date.parse(`2026-${s}+08:00`)/1000, oldTime=ts('08-07T00:00:00'),newTime=ts('08-07T08:00:00');
// 2026-10-02：取消 15m 双动能确认（makeBiLowerContext/lowerEndpointWeaker 已删），
// 容差只保留 nearDoubleFixed（默认 2.0）；比较先影线后实体——影线更极端走更极端替换，
// 影线不满足比实体：后实体不低于前实体直接后移，更低则差 ≤ fixed 才后移。
function build(res,locked=null){const b=data[res],m=c.mergeBars(c.markWickBars(b));return c.fixBiExtremes(c.buildBi(c.findFractals(m),m,c.calcATR(b,14),c.calcMACD(b),locked,c.nearDoubleOn(res)),m);}
test('frozen gold: 8-7 08:00 4229.875 target present',()=>{
 // 00:00 前底影线更极端（4223.505<4230.46）走更极端替换、不带 nearDouble 封顶
 // （先影线闸门），08:00 后底实体 4232.90 低于前底实体 4233.815（diff=-0.915）→ 直接后移。
 for(const res of Object.keys(data)){const out=build(res);
  if(res==='60'){const t=out.find(x=>x.endTime===newTime);assert.equal(t&&t.endPrice,4229.875);
   assert.ok(out.some(x=>x.startTime===newTime));}}
});
test('locked endpoint and nearDouble-marked endpoint are preserved',()=>{
 // 阈值边界由合成K线用例覆盖；本用例保留锁定端点与单跳封顶语义。
 const bars=data['60'],m=c.mergeBars(c.markWickBars(bars)),f=c.findFractals(m);
 const old=f.find(x=>x.time===oldTime),atr=c.calcATR(bars,14),macd=c.calcMACD(bars);
 const b=build('60',[{dir:'bottom',price:old.low}]);assert.ok(b.some(x=>x.endTime===oldTime));
 const marked=structuredClone(f);marked.find(x=>x.time===oldTime).nearDouble=true;
 assert.ok(c.buildBi(marked,m,atr,macd,null,true).some(x=>x.endTime===oldTime));
});
test('fixed tolerance boundary: below diff keeps old, >= diff takes new',()=>{
 // 合成K线（与 py test_fixed_tolerance_boundary 同构）：B1/B2 近等平台双底，中间 T2
 // 反弹不成笔（两侧 gap=1）、回调深度 0.8 » thr；B2 low 5.30 > B1 low 4.90 影线不满足。
 const bar_=(t,h,l)=>{const m=(h+l)/2;return {time:t*3600,open:m,high:h,low:l,close:m};};
 const bars=[bar_(0,5.10,4.80),bar_(1,5.00,4.40),bar_(2,5.50,4.60),bar_(3,6.00,5.00),bar_(4,6.30,5.20),
  bar_(5,6.60,5.50),bar_(6,6.20,5.30),bar_(7,5.80,5.10),bar_(8,5.60,4.95),
  bar_(9,5.35,4.90),bar_(10,5.70,5.35),bar_(11,5.65,5.30),
  bar_(12,5.90,5.40),bar_(13,6.30,5.65),bar_(14,6.80,5.90),bar_(15,7.40,6.20),bar_(16,6.90,6.00)];
 const m=c.mergeBars(c.markWickBars(bars)),f=c.findFractals(m),atr=c.calcATR(bars,14),macd=c.calcMACD(bars);
 const b1=f.find(x=>x.type==='bottom'&&Math.abs(x.low-4.90)<1e-9),b2=f.find(x=>x.type==='bottom'&&Math.abs(x.low-5.30)<1e-9),t3=f.find(x=>x.type==='top'&&Math.abs(x.high-7.40)<1e-9);
 const diff=m[b2.mergedIdx].bodyBottom-m[b1.mergedIdx].bodyBottom;
 assert.ok(Math.abs(diff-0.35)<1e-9);
 const saved=c.CHAN_CFG.nearDoubleFixed;
 try{for(const [fixed,expected] of [[0,b1.time],[diff-1e-9,b1.time],[diff,b2.time],[diff+0.10,b2.time]]){c.CHAN_CFG.nearDoubleFixed=fixed;const b=c.buildBi(structuredClone(f),m,atr,macd,null,true);assert.equal(b.find(x=>x.endTime===t3.time).startTime,expected,`fixed=${fixed}`);}}finally{c.CHAN_CFG.nearDoubleFixed=saved;}
});
test('body not lower shifts directly at fixed=0 (bottom platform + mirrored top)',()=>{
 // 2026-10-02 新语义：影线不满足时比实体，后实体价更极端（底更低/顶更高）在 fixed=0
 // 下也直接后移；nearDouble 关闭则停在原端点。顶=底的价格镜像（-x）。
 const bar_=(t,h,l)=>{const m=(h+l)/2;return {time:t*3600,open:m,high:h,low:l,close:m};};
 const barsBot=[bar_(0,5.10,4.80),bar_(1,5.00,4.40),bar_(2,5.50,4.60),bar_(3,6.00,5.00),bar_(4,6.30,5.20),
  bar_(5,6.60,5.50),bar_(6,6.20,5.30),bar_(7,5.80,5.10),bar_(8,5.60,4.95),
  bar_(9,5.45,4.90),           // B1 底（实体 5.175，影线 4.90）
  bar_(10,5.70,5.35),
  bar_(11,5.25,5.00),          // B2 底（实体 5.125 更低，low 5.00 > 4.90）
  bar_(12,5.90,5.40),bar_(13,6.30,5.65),bar_(14,6.80,5.90),bar_(15,7.40,6.20),bar_(16,6.90,6.00)];
 const barsTop=barsBot.map(b=>({time:b.time,open:-b.open,high:-b.low,low:-b.high,close:-b.close}));
 const endAt=(bars,cfg=null,near=true)=>{const m=c.mergeBars(c.markWickBars(bars));
  const saved=cfg&&Object.fromEntries(Object.keys(cfg).map(k=>[k,c.CHAN_CFG[k]]));
  try{if(cfg)Object.assign(c.CHAN_CFG,cfg);
   return c.buildBi(c.findFractals(m),m,c.calcATR(bars,14),c.calcMACD(bars),null,near).map(x=>x.endTime);
  }finally{if(cfg)Object.assign(c.CHAN_CFG,saved);}};
 assert.ok(endAt(barsBot,{nearDoubleFixed:0}).includes(11*3600));
 assert.ok(endAt(barsBot,null,false).includes(9*3600));
 assert.ok(endAt(barsTop,{nearDoubleFixed:0}).includes(11*3600));
 assert.ok(endAt(barsTop,null,false).includes(9*3600));
});
test('per-period switches flip nearDoubleOn; off reverts to plain build, on restructures 15m',()=>{
 const bars=data['60'],m=c.mergeBars(c.markWickBars(bars)),f=c.findFractals(m),atr=c.calcATR(bars,14),macd=c.calcMACD(bars);
 assert.equal(c.nearDoubleOn('60')&&c.nearDoubleOn('240')&&c.nearDoubleOn('D'),true);
 assert.equal(c.nearDoubleOn('3')&&c.nearDoubleOn('15'),true);  // 2026-09-28 起五周期默认全开
 assert.equal(c.nearDoubleOn(3600)&&c.nearDoubleOn(14400)&&c.nearDoubleOn(86400),true);
 assert.equal(c.nearDoubleOn(180)&&c.nearDoubleOn(900),true);
 assert.equal(c.nearDoubleOn(30)||c.nearDoubleOn(0)||c.nearDoubleOn('30S')||c.nearDoubleOn('W')||c.nearDoubleOn('5')||c.nearDoubleOn('30'),false);
 assert.equal(c.nearDoubleOn('1H')&&c.nearDoubleOn('4H')&&c.nearDoubleOn('1D'),true);
 const saved60=c.CHAN_CFG.nearDouble60,saved15=c.CHAN_CFG.nearDouble15;
 try{
  c.CHAN_CFG.nearDouble60=false;
  assert.equal(c.nearDoubleOn('60'),false);
  const off=build('60');
  // 开关语义由「与无开关直构逐笔相等」的 parity 断言承担。
  const direct=c.fixBiExtremes(c.buildBi(structuredClone(f),m,atr,macd,null,false),m);
  assert.deepEqual(off,direct);
  const m15=c.mergeBars(c.markWickBars(data['15'])),f15=c.findFractals(m15);
  const base15=c.buildBi(structuredClone(f15),m15,c.calcATR(data['15'],14),c.calcMACD(data['15']),null,false);
  c.CHAN_CFG.nearDouble15=true;
  assert.equal(c.nearDoubleOn('15'),true);
  const on15=c.buildBi(structuredClone(f15),m15,c.calcATR(data['15'],14),c.calcMACD(data['15']),null,true);
  assert.notEqual(JSON.stringify(on15),JSON.stringify(base15));
 }finally{c.CHAN_CFG.nearDouble60=saved60;c.CHAN_CFG.nearDouble15=saved15;}
});
// ---- 近等后顶/后底（反弹不成笔）取后（2026-09-26）----
// 案例：60m 2026-09-18 顶 4399.67(15:00+8) → 底 4342.73(22:00+8，与23:00包含合并)
// → 顶 4397.045(9-19 01:00+8)，反弹腿仅 3 根合并K（gap=2<4)；实体差 4393.49-4393.05=0.44 ≤ thr。
// 240 层平台规则已把 4h 顶后移到 9-19 01:00 → 60m 须复现（locked 路径=区间套落地）。
const rdata=JSON.parse(zlib.gunzipSync(fs.readFileSync('py_chain/fixtures/near_double_rebound_xauusd_20260919.json.gz')));
const rStart=ts('09-18T11:00:00'),rOld=ts('09-18T15:00:00'),rNew=ts('09-19T01:00:00');
function rbuild(locked=null,nearDouble=true,cfg=null){
 const bars=rdata['60'],m=c.mergeBars(c.markWickBars(bars));
 const saved=cfg&&Object.fromEntries(Object.keys(cfg).map(k=>[k,c.CHAN_CFG[k]]));
 try{if(cfg)Object.assign(c.CHAN_CFG,cfg);
  return c.fixBiExtremes(c.buildBi(c.findFractals(m),m,c.calcATR(bars,14),c.calcMACD(bars),locked,nearDouble),m);
 }finally{if(cfg)Object.assign(c.CHAN_CFG,saved);}}
test('near-equal rebound (gap<4) shifts endpoint to later top: 9-18 11:00 → 9-19 01:00',()=>{
 // 隔离守卫（与镜像用例同款）：关端点内部极值恢复，否则端点被上移到 9-18 17:00
 // 被包含合并吞掉的上影真高 4398.2——本用例只验近等后顶取后本身。
 const iso={endInnerRecoverOn:false};
 let up=rbuild(null,true,iso).find(x=>x.startTime===rStart);
 assert.equal(up.endTime,rNew);assert.equal(up.endPrice,4397.045);
 assert.equal(rbuild(null,true,iso).find(x=>x.startTime===rNew).endPrice,4322.81);
 // 关 nearDoubleRebound 且无锁定 → 旧行为：端点停在前顶 4399.67
 up=rbuild(null,true,{nearDoubleRebound:false,...iso}).find(x=>x.startTime===rStart);
 assert.equal(up.endTime,rOld);assert.equal(up.endPrice,4399.67);
 // 关 nearDoubleRebound 但 k 为上级锁定端点（如 240 层已后移的 4397.05）→ 区间套落地不受开关限制
 up=rbuild([{dir:'top',price:4397.045}],true,{nearDoubleRebound:false,...iso}).find(x=>x.startTime===rStart);
 assert.equal(up.endTime,rNew);assert.equal(up.endPrice,4397.045);
 // 无锁定且非 nearDouble 周期（模拟 15m 口径）→ 不后移
 up=rbuild(null,false).find(x=>x.startTime===rStart);
 assert.equal(up.endTime,rOld);assert.equal(up.endPrice,4399.67);
});
test('near-equal rebound mirrors for double bottom',()=>{
 // 10000-x 镜像（顶↔底互换、MACD 取反）。注：上影 _topCand/下影 _origLow 的处理
 // 本身不对称，镜像的中间结构与原窗口不同——只断言本规则本身：后底（较高、较晚，
 // 同类型 <= 替换不可能产生）成为端点 ⇔ 开关开。（前底影线更极端走更极端替换、
 // 不带 nearDouble 封顶，反弹后移才不被单跳封顶拦截——先影线闸门的镜像验证。）
 const bars=rdata['60'].map(b=>({time:b.time,open:10000-b.open,high:10000-b.low,low:10000-b.high,close:10000-b.close}));
 const mirror=()=>{const m=c.mergeBars(c.markWickBars(bars));
  return c.fixBiExtremes(c.buildBi(c.findFractals(m),m,c.calcATR(bars,14),c.calcMACD(bars),null,true),m);};
 // 隔离守卫（与 py 镜像用例同款）：关端点内部极值恢复，本用例只验近等后底取后本身。
 const saved=c.CHAN_CFG.nearDoubleRebound,savedRec=c.CHAN_CFG.endInnerRecoverOn;
 c.CHAN_CFG.endInnerRecoverOn=false;
 try{
  const on=mirror();
  const down=on.find(x=>x.endTime===rNew);
  assert.equal(down.type,'down');
  assert.equal(down.endPrice,10000-4397.045);   // 后底 5602.955 > 前底 5600.330：非 <= 替换可达
  c.CHAN_CFG.nearDoubleRebound=false;
  assert.ok(!mirror().some(x=>x.endTime===rNew)); // 关开关（无锁定）→ 端点停在前底 09-18 15:00
 }finally{c.CHAN_CFG.nearDoubleRebound=saved;c.CHAN_CFG.endInnerRecoverOn=savedRec;}
});
// ---- 死顶/死底近等吸收（2026-10-22）----
// 双顶/双底的前一个顶/底自身与笔起点不成笔（间隔不足）时，与其近等的后顶/后底
// 吸收它作为笔端点。真实案例：60m 2026-09-14 21:00 底 4253.64 → 09-15 00:00 顶
// 4317.83（间隔仅 3 根被丢弃）→ 10:00 后顶 4317.63 差 0.20——吸收后上涨笔成笔。
const ddata=JSON.parse(zlib.gunzipSync(fs.readFileSync('py_chain/fixtures/dead_top_absorb_xauusd_20260915.json.gz')));
const dStart=ts('09-14T21:00:00'),dEnd=ts('09-15T10:00:00');
function dbuild(nearDouble=true,cfg=null){
 const bars=ddata['60'],m=c.mergeBars(c.markWickBars(bars));
 const saved=cfg&&Object.fromEntries(Object.keys(cfg).map(k=>[k,c.CHAN_CFG[k]]));
 try{if(cfg)Object.assign(c.CHAN_CFG,cfg);
  return c.buildBi(c.findFractals(m),m,c.calcATR(bars,14),c.calcMACD(bars),null,nearDouble);
 }finally{if(cfg)Object.assign(c.CHAN_CFG,saved);}}
test('dead top absorbed: up stroke 4253.64 -> 4317.63 forms (09-14 21:00 -> 09-15 10:00)',()=>{
 const up=dbuild().find(x=>x.startTime===dStart&&x.type==='up');
 assert.equal(up.endTime,dEnd);assert.equal(up.endPrice,4317.63);
 // 近等整族关闭（JS 无单规则开关，隔离语义由 py 侧 monkeypatch 用例承担）→ 不成笔
 assert.ok(!dbuild(false).some(x=>x.endTime===dEnd));
});
// 合成场景（与 py DeadTopAbsorbTests.OHLC 同构）：P(125)↓B(100)↗2根T1(111·死顶)↘浅回调
// 底(104)↗T2(109.5·实体差1.0≤thr)↘B3(97)；T1 影线 111 ≤ T2+thr=111.5。
const dOHLC=[[120,122,118,119],[121,125,119,123],[120,124,116,118],[117,121,113,115],
 [105,109,102,104],[104,108.5,101,103],[105,108,100,104.5],[105,109,103,107],[107,111,105,110],
 [108,109.5,104.5,109],[107.5,108,104,107.8],[107.8,109,106,108.5],[108.5,109.5,107,109],
 [107.5,108.5,104,105],[104.5,106,101,102.5],[102,103.5,99,100.5],[100.5,103,97,98],
 [99,104,98,103],[103,106,102,105],[104,107,103,106],[105,108,104,107]];
const dT0=1767225600;
function dsyn(ohlc,cfg=null,nearDouble=true){
 const list=ohlc||dOHLC;
 const bars=list.map((x,i)=>({time:dT0+i*3600,open:x[0],high:x[1],low:x[2],close:x[3]}));
 const m=c.mergeBars(c.markWickBars(bars));
 const saved=cfg&&Object.fromEntries(Object.keys(cfg).map(k=>[k,c.CHAN_CFG[k]]));
 try{if(cfg)Object.assign(c.CHAN_CFG,cfg);
  return c.buildBi(c.findFractals(m),m,c.calcATR(bars,14),c.calcMACD(bars),null,nearDouble);
 }finally{if(cfg)Object.assign(c.CHAN_CFG,saved);}}
test('synthetic dead-top absorb, tolerance boundary, unattributable blocker',()=>{
 const shape=b=>b.map(x=>[x.type,x.startPrice,x.endPrice]);
 assert.deepEqual(shape(dsyn()),[['down',125,100],['up',100,109.5],['down',109.5,97]]);
 // 近等关闭 → T1 死于间隔不足、T2 死于笔内极值守卫，单笔 125→97
 assert.deepEqual(shape(dsyn(null,null,false)),[['down',125,97]]);
 // 实体差 1.0：fixed=1.5 吸收；fixed=0.9 不吸收
 assert.ok(dsyn(null,{nearDoubleFixed:1.5}).some(x=>x.type==='up'&&x.endPrice===109.5));
 assert.ok(!dsyn(null,{nearDoubleFixed:0.9}).some(x=>x.endPrice===109.5));
 // bar9 高点抬到 110：超越点无同类分型归属 → 不吸收
 const ohlc=dOHLC.slice();ohlc[9]=[108,110,104.5,109];
 assert.deepEqual(shape(dsyn(ohlc)),[['down',125,97]]);
});

// ---- 平台回退起点重估（2026-10-26，与 py PlatShiftRestoreTests 同步）----
// 9-1 22:15 顶取后 00:00 → 00:30 回退锚 22:15 → 更深底替换后起点复位 00:00。
const prData=JSON.parse(zlib.gunzipSync(fs.readFileSync('py_chain/fixtures/plat_restore_xauusd_20260901.json.gz')));
const prTs=s=>Date.parse(`2026-${s}+08:00`)/1000;
function prBuild(bars,locked=null){const t=c.markWickBars(bars),m=c.mergeBars(t);
 return c.extendLastBi(c.fixBiExtremes(c.buildBi(c.findFractals(m),m,c.calcATR(bars,14),c.calcMACD(bars),locked,true,'15'),m)||[],t);}
test('plat shift restore: start moves to taken-later top 00:00 after replace',()=>{
 const t0=prTs('09-02T00:00:00'),out=prBuild(prData['15']);
 const up=out.find(x=>x.endTime===t0);
 assert.equal(up&&up.type,'up');assert.equal(up.endPrice,4374.79);
 const down=out.find(x=>x.startTime===t0);
 assert.equal(down.type,'down');assert.equal(down.endTime,prTs('09-02T11:30:00'));
 assert.equal(down.endPrice,4282.625);
 // 锚（22:15）被上级锁定 → 重估排除，起点保持回退锚
 const lk=prBuild(prData['15'],[{dir:'top',price:4375.695}]);
 assert.equal(lk.find(x=>x.endPrice===4282.625).startTime,prTs('09-01T22:15:00'));
});
test('plat shift restore: no replace keeps rollback anchor (truncated to 00:45)',()=>{
 const bars=prData['15'].filter(x=>x.time<=prTs('09-02T00:45:00')),out=prBuild(bars);
 const down=out.find(x=>x.endPrice===4347.09);
 assert.equal(down.startTime,prTs('09-01T22:15:00'));
 assert.ok(!out.some(x=>x.startTime===prTs('09-02T00:00:00')));
});
