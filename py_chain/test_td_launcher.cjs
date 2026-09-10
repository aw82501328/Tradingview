const {test}=require('node:test');
const assert=require('node:assert/strict');
const {launch}=require('../.cursor/skills/open-tradingview/scripts/open_tradingview.js');
function fake(overrides={}) {
 let running=false, ready=false, kills=0;
 return {deps:{probe:async()=>({state:ready?'ready':'offline'}),hasTradingViewProcess:async()=>running,
 find:async()=>'TD.exe',killTradingView:async()=>{kills++;running=false},waitProcessExit:async()=>true,
 launchWithPowerShell:async()=>{running=true;ready=true},launchInPackageContext:async()=>{ready=true},sleep:async()=>{},...overrides},kills:()=>kills};
}
test('already connected never launches',async()=>{const f=fake({probe:async()=>({state:'ready'}),find:async()=>assert.fail()});assert.equal((await launch(false,f.deps)).state,'ready')});
test('offline launches without restart',async()=>{const f=fake();assert.equal((await launch(false,f.deps)).state,'ready');assert.equal(f.kills(),0)});
test('running requires confirmation and never kills',async()=>{const f=fake({hasTradingViewProcess:async()=>true});assert.equal((await launch(false,f.deps)).state,'needs_confirmation');assert.equal(f.kills(),0)});
test('confirmed restart kills then connects',async()=>{let running=true;const f=fake({hasTradingViewProcess:async()=>running,killTradingView:async()=>{running=false}});assert.equal((await launch(true,f.deps)).state,'ready')});
test('occupied port never launches',async()=>{const f=fake({probe:async()=>({state:'error'}),find:async()=>assert.fail()});assert.equal((await launch(false,f.deps)).state,'error')});
test('missing installation',async()=>{const f=fake({find:async()=>null});assert.match((await launch(false,f.deps)).message,/未找到/)});
test('timeout reported',async()=>{const f=fake({probe:async()=>({state:'offline'}),launchWithPowerShell:async()=>false,launchInPackageContext:async()=>false});assert.match((await launch(false,f.deps)).message,/超时/)});
test('fallback cannot kill an unconfirmed instance',async()=>{let running=false;const f=fake({probe:async()=>({state:'offline'}),hasTradingViewProcess:async()=>running,launchWithPowerShell:async()=>{running=true}});assert.equal((await launch(false,f.deps)).state,'needs_confirmation');assert.equal(f.kills(),0)});
