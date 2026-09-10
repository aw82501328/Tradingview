// Loaded only by WEB analysis jobs. Existing CLI entry points remain usable.
'use strict';
const fs = require('fs');
const path = require('path');
const util = require('util');
const modulePath = require.resolve('../server-cdp/node_modules/chrome-remote-interface');
const CDP = require(modulePath);
const target = process.env.CHAN_TARGET;
const symbol = process.env.CHAN_SYMBOL;
const reportPath = process.env.CHAN_REPORT;
const errors = [];
let inputBars = {};
try {
  const key = String(symbol).replace(/[^A-Za-z0-9_.-]/g, '_');
  inputBars = JSON.parse(fs.readFileSync(path.join(process.env.CHAN_CACHE_DIR, `bis_${key}.json`), 'utf8')).bars || {};
} catch (_) { /* The first (bi) stage creates the snapshot. */ }
const originalLog = console.log;
console.log = (...args) => {
  const line = util.format(...args);
  // Legacy scripts sometimes catch errors and exit 0. Do not report them green.
  if (/^(?:Error:|ERROR:|错误:|警告:.*落盘失败)/im.test(line) ||
      /无K线数据或切换失败|全部笔超出|跳过 \d+ 根更早的笔|重试后仍有/.test(line) ||
      /"(?:bi_fail|bi_bad|zs_fail|failed|errors)"\s*:\s*[1-9]/.test(line)) errors.push(line);
  originalLog(...args);
};
async function guardedCDP(options = {}) {
  const client = await CDP({...options, target, port: Number(process.env.CHAN_PORT || 9222)});
  const evaluate = client.Runtime.evaluate.bind(client.Runtime);
  client.Runtime.evaluate = async (args) => {
    const guard = `if (typeof TradingViewApi === 'undefined' || TradingViewApi.activeChart().symbol() !== ${JSON.stringify(symbol)}) throw new Error('ANALYSIS_TARGET_CHANGED');
    (()=>{const ra=TradingViewApi._replayApi;const r=ra&&typeof ra.value==='function'?ra.value():ra;if(r&&typeof r.isReplayStarted==='function'){const s=r.isReplayStarted();const started=s&&typeof s.value==='function'?s.value():s;if(started===true)throw new Error('ANALYSIS_REPLAY_ACTIVE');}})();\n`;
    const result = await evaluate({...args, expression: guard + args.expression});
    const value = result.result?.value;
    // Downstream algorithms consume the exact OHLC snapshot that produced the
    // strokes. Drawing coverage probes still use the live chart's real data.
    if (value && Array.isArray(value.bars) && value.resolution) {
      const res = ({'1D':'D','1W':'W'})[value.resolution] || value.resolution;
      if (inputBars[res]?.length) {
        value.bars = structuredClone(inputBars[res]);
        value.len = value.bars.length;
        value.notCovered = false;
        const gaps = value.bars.slice(-20).slice(1).map((b, i) => b.time - value.bars.slice(-20)[i].time).sort((a,b)=>a-b);
        value.gap = gaps[Math.floor(gaps.length / 2)] || 0;
      }
    }
    if (value && !Array.isArray(value)) {
      for (const key of ['err', 'bi_err', 'zs_err']) {
        if (Array.isArray(value[key]) && value[key].length) errors.push(JSON.stringify(value));
      }
    }
    if (result.exceptionDetails) {
      const description = result.exceptionDetails.exception?.description || result.exceptionDetails.text;
      if (/ANALYSIS_TARGET_CHANGED|ANALYSIS_REPLAY_ACTIVE/.test(String(description))) {
        errors.push('目标图表品种改变或进入历史回放，已停止后续操作');
        throw new Error(String(description));
      }
    }
    return result;
  };
  return client;
}
Object.assign(guardedCDP, CDP);
guardedCDP.List = async () => {
  const pages = await CDP.List({port: Number(process.env.CHAN_PORT || 9222)});
  const selected = pages.filter(p => p.id === target && p.type === 'page' && p.url.includes('tradingview.com'));
  if (!selected.length) throw new Error('目标图表已断开');
  return selected;
};
require.cache[modulePath].exports = guardedCDP;
process.on('exit', code => {
  if (!reportPath) return;
  try { fs.writeFileSync(reportPath, JSON.stringify({ok: code === 0 && !errors.length, errors, code}), 'utf8'); }
  catch (_) { process.exitCode = 1; }
});
