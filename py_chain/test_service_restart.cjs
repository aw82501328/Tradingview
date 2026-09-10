const assert = require('node:assert/strict');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('py_chain/web/service-restart.js', 'utf8');

function setup({confirm = true, postFails = false, rejectPost = false, recover = true} = {}) {
  const button = {}, status = {}, intervals = [];
  let now = 0, posts = 0, reads = 0, reloads = 0, confirmed = 0, restarting = false;
  const old = {instanceId:'old', restarting:false, startCommand:'python -m py_chain.webapp --port 8000'};
  const context = {
    document: {getElementById:()=>button, createElement:()=>status, querySelector:()=>({after(){}})},
    window: {confirm:()=>{confirmed++;return confirm}},
    location: {reload:()=>reloads++},
    Date: {now:()=>now},
    AbortSignal: {timeout:()=>undefined},
    setTimeout: (fn,ms)=>{now+=ms;queueMicrotask(fn)},
    setInterval: fn=>intervals.push(fn),
    fetch: async (path, options) => {
      if (options?.method==='POST') {
        posts++;
        if(rejectPost)return {ok:false,json:async()=>({error:'helper failed'})};
        restarting=true;
        if(postFails)throw Error('connection lost');
        return {ok:true,json:async()=>({ok:true})};
      }
      reads++;
      return {ok:true,json:async()=>({service:restarting&&recover?{...old,instanceId:'new'}:old})};
    }
  };
  status.setAttribute=()=>{};
  vm.runInNewContext(source, context);
  return {button,status,intervals,stats:()=>({posts,reads,reloads,confirmed}), externalRestart:()=>{restarting=true}};
}

(async()=>{
  let s=setup({confirm:false});await s.button.onclick();assert.equal(s.stats().posts,0);assert.equal(s.stats().reads,0);
  s=setup();await Promise.all([s.button.onclick(),s.button.onclick()]);assert.equal(s.stats().posts,1);assert.equal(s.stats().confirmed,1);assert.equal(s.stats().reloads,1);
  s=setup({postFails:true});await s.button.onclick();assert.equal(s.stats().posts,1);assert.equal(s.stats().reloads,1);
  s=setup({recover:false});await s.button.onclick();assert.equal(s.stats().reloads,0);assert.equal(s.stats().posts,1);assert.match(s.status.textContent,/60 秒/);assert.match(s.status.textContent,/python -m/);assert.equal(s.button.disabled,true);
  s=setup({rejectPost:true});await s.button.onclick();assert.match(s.status.textContent,/helper failed/);assert.equal(s.button.disabled,false);
  s=setup();await s.intervals[0]();s.externalRestart();await s.intervals[0]();assert.equal(s.stats().posts,0);assert.equal(s.stats().reloads,1);
  console.log('Restart UI: cancel, duplicate click, lost response, timeout, rejection, other-page recovery passed');
})().catch(e=>{console.error(e);process.exitCode=1});
