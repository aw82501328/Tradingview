const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");
const fs = require("fs");
const path = require("path");

// ========== 指标计算 ==========
function wilder(arr, n) {
  if (arr.length < n) return [];
  const s = [arr.slice(0, n).reduce((a, b) => a + b, 0)];
  for (let i = n; i < arr.length; i++) s.push(s[s.length - 1] + (arr[i] - s[s.length - 1]) / n);
  return s;
}
function linearRegression(prices) {
  const n = prices.length;
  if (n < 2) return { slope: 0, r2: 0 };
  let sumX = 0, sumY = 0, sumXY = 0, sumX2 = 0, sumY2 = 0;
  for (let i = 0; i < n; i++) { sumX += i; sumY += prices[i]; sumXY += i * prices[i]; sumX2 += i * i; sumY2 += prices[i] * prices[i]; }
  const den = n * sumX2 - sumX * sumX;
  if (den === 0) return { slope: 0, r2: 0 };
  const slope = (n * sumXY - sumX * sumY) / den;
  const intercept = (sumY - slope * sumX) / n;
  let ssRes = 0, ssTot = 0;
  const meanY = sumY / n;
  for (let i = 0; i < n; i++) { const pred = slope * i + intercept; ssRes += (prices[i] - pred) ** 2; ssTot += (prices[i] - meanY) ** 2; }
  return { slope, r2: ssTot === 0 ? 0 : 1 - ssRes / ssTot };
}
function calcADX(highs, lows, closes, period = 14) {
  const tr = [], plusDM = [], minusDM = [];
  for (let i = 1; i < highs.length; i++) {
    tr.push(Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1])));
    const up = highs[i] - highs[i - 1], down = lows[i - 1] - lows[i];
    plusDM.push(up > down && up > 0 ? up : 0);
    minusDM.push(down > up && down > 0 ? down : 0);
  }
  if (tr.length < period * 2) return { adx: 0, plusDI: 0, minusDI: 0 };
  const atrS = wilder(tr, period), pdiS = wilder(plusDM, period), mdiS = wilder(minusDM, period);
  const adxRaw = [];
  for (let i = 0; i < atrS.length; i++) {
    const pdi = atrS[i] > 0 ? (pdiS[i] / atrS[i]) * 100 : 0;
    const mdi = atrS[i] > 0 ? (mdiS[i] / atrS[i]) * 100 : 0;
    adxRaw.push((pdi + mdi) === 0 ? 0 : (Math.abs(pdi - mdi) / (pdi + mdi)) * 100);
  }
  if (adxRaw.length < period) return { adx: 0, plusDI: 0, minusDI: 0 };
  const adxS = wilder(adxRaw, period);
  return { adx: adxS[adxS.length - 1], plusDI: atrS[atrS.length - 1] > 0 ? (pdiS[pdiS.length - 1] / atrS[atrS.length - 1]) * 100 : 0, minusDI: atrS[atrS.length - 1] > 0 ? (mdiS[mdiS.length - 1] / atrS[atrS.length - 1]) * 100 : 0 };
}
function analyze(bars, lookback) {
  if (!bars || bars.length < lookback + 10) return { error: `数据不足(${bars ? bars.length : 0}根)` };
  const closes = bars.map(b => b.c), highs = bars.map(b => b.h), lows = bars.map(b => b.l);
  const cLb = closes.slice(-lookback);
  const reg = linearRegression(cLb);
  const avgLb = cLb.reduce((a, b) => a + b, 0) / lookback;
  const ns = avgLb > 0 ? (reg.slope / avgLb) * 100 : 0;
  const adx = calcADX(highs, lows, closes);
  const maBias = (closes[closes.length - 1] / avgLb - 1) * 100;
  const dir = ns >= 0 ? 1 : -1;
  let aligned = 0;
  for (let i = closes.length - lookback + 1; i < closes.length; i++) { if ((closes[i] - closes[i - 1]) * dir > 0) aligned++; }
  const consistency = lookback > 1 ? aligned / (lookback - 1) : 0;
  const trArr = [];
  for (let i = 1; i < highs.length; i++) trArr.push(Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1])));
  const atr14 = trArr.slice(-14).reduce((a, b) => a + b, 0) / 14;
  const todayRange = atr14 > 0 ? (highs[highs.length - 1] - lows[lows.length - 1]) / atr14 : 1;
  const rets = [];
  for (let i = 1; i < closes.length; i++) rets.push((closes[i] - closes[i - 1]) / closes[i - 1]);
  const meanR = rets.reduce((a, b) => a + b, 0) / rets.length;
  const vol = Math.sqrt(rets.reduce((a, b) => a + (b - meanR) ** 2, 0) / rets.length) * 100;
  const score = Math.abs(ns) * 20 * reg.r2 * (adx.adx / 100) * consistency * Math.min(todayRange / 2, 2);
  return { price: closes[closes.length - 1], bars: bars.length, slope: ns, r2: reg.r2, adx: adx.adx, plusDI: adx.plusDI, minusDI: adx.minusDI, direction: ns >= 0 ? "涨" : "跌", maBias, consistency, aRange: todayRange, vol, score };
}

// ========== CDP ==========
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
async function getBars(client, symbol, interval) {
  await client.Runtime.evaluate({ expression: `(function() { TradingViewApi.activeChart().setSymbol("${symbol}"); return 1; })()`, returnByValue: true, awaitPromise: true, timeout: 15000 });
  await sleep(4500);
  await client.Runtime.evaluate({ expression: `(function() { TradingViewApi.activeChart().setResolution("${interval}"); return 1; })()`, returnByValue: true, awaitPromise: true, timeout: 10000 });
  await sleep(3500);
  const r = await client.Runtime.evaluate({ expression: `(function() { const items = TradingViewApi.activeChart().chartModel().mainSeries().data().m_bars._items; return items ? items.map(b => ({ v: b.value })) : []; })()`, returnByValue: true, awaitPromise: true, timeout: 10000 });
  return (r.result.value || []).map(b => ({ t: b.v[0], o: b.v[1], h: b.v[2], l: b.v[3], c: b.v[4], vol: b.v[5] }));
}

// ========== 市场分组 ==========
const TF_LIST = ["1D", "240", "60", "15"];
const TF_LABELS = { "1D": "日线", "240": "4H", "60": "1H", "15": "15M" };
const LOOKBACK = 20;
const ICONS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"];

const GROUPS = [
  {
    id: "commodities",
    name: "商品/股指",
    emoji: "🏦",
    symbols: [
      { sym: "OANDA:XAUUSD", label: "黄金" },
      { sym: "OANDA:XAGUSD", label: "白银" },
      { sym: "TVC:USOIL", label: "原油" },
      { sym: "FX:NAS100", label: "纳指" },
    ],
  },
  {
    id: "forex",
    name: "主要外汇对",
    emoji: "💱",
    symbols: [
      { sym: "OANDA:EURUSD", label: "欧美" },
      { sym: "OANDA:GBPUSD", label: "镑美" },
      { sym: "OANDA:USDJPY", label: "美日" },
      { sym: "OANDA:USDCHF", label: "美瑞" },
      { sym: "OANDA:AUDUSD", label: "澳美" },
      { sym: "OANDA:NZDUSD", label: "纽美" },
      { sym: "OANDA:USDCAD", label: "美加" },
    ],
  },
  {
    id: "crypto",
    name: "数字货币",
    emoji: "🪙",
    symbols: [
      { sym: "BINANCE:BTCUSDT", label: "BTC" },
      { sym: "BINANCE:ETHUSDT", label: "ETH" },
      { sym: "BINANCE:BNBUSDT", label: "BNB" },
      { sym: "BINANCE:SOLUSDT", label: "SOL" },
      { sym: "BINANCE:XRPUSDT", label: "XRP" },
      { sym: "BINANCE:DOGEUSDT", label: "DOGE" },
      { sym: "BINANCE:ADAUSDT", label: "ADA" },
      { sym: "BINANCE:AVAXUSDT", label: "AVAX" },
    ],
  },
];

// ========== HTML 生成 ==========
function scoreBadge(score) {
  if (score >= 0.5) return '<span class="badge strong">强趋势</span>';
  if (score >= 0.1) return '<span class="badge moderate">中等</span>';
  if (score >= 0.01) return '<span class="badge weak">弱趋势</span>';
  return '<span class="badge none">震荡</span>';
}
function dirSpan(d) {
  if (d === "涨") return '<span class="up">▲ 涨</span>';
  if (d === "跌") return '<span class="down">▼ 跌</span>';
  return d;
}
function adxColor(v) {
  if (v >= 40) return "color:#22c55e;font-weight:bold";
  if (v >= 25) return "color:#facc15";
  return "";
}

function buildTFTable(groupResults, tf, symbols) {
  const entries = symbols.map(s => {
    const r = groupResults[tf]?.[s.label];
    return r || { label: s.label, error: "—" };
  });
  const sorted = entries.filter(r => !r.error).sort((a, b) => b.score - a.score);

  let html = `<table class="tf-table"><thead><tr>
    <th>#</th><th>品种</th><th>方向</th><th>价格</th><th>ADX</th><th>+DI/-DI</th><th>R²</th><th>斜率</th><th>一致性</th><th>MA偏离</th><th>波动</th><th>评分</th>
  </tr></thead><tbody>`;

  for (let i = 0; i < sorted.length; i++) {
    const r = sorted[i];
    html += `<tr>
      <td>${ICONS[i] || (i + 1)}</td>
      <td><b>${r.label}</b></td>
      <td>${dirSpan(r.direction)}</td>
      <td>${r.price?.toFixed(2)}</td>
      <td style="${adxColor(r.adx)}">${r.adx?.toFixed(1)}</td>
      <td>${r.plusDI?.toFixed(1)}/${r.minusDI?.toFixed(1)}</td>
      <td>${r.r2?.toFixed(3)}</td>
      <td>${r.slope?.toFixed(4)}</td>
      <td>${(r.consistency * 100).toFixed(0)}%</td>
      <td>${r.maBias?.toFixed(2)}%</td>
      <td>${r.vol?.toFixed(2)}%</td>
      <td><b>${r.score?.toFixed(3)}</b> ${scoreBadge(r.score)}</td>
    </tr>`;
  }
  html += "</tbody></table>";
  return html;
}

function buildResonanceTable(groupResults, symbols) {
  let html = `<table class="res-table"><thead><tr><th>品种</th>${TF_LIST.map(t => "<th>" + TF_LABELS[t] + "</th>").join("")}<th>共振</th></tr></thead><tbody>`;
  for (const s of symbols) {
    const dirs = TF_LIST.map(t => groupResults[t]?.[s.label]?.direction || "—");
    const up = dirs.filter(d => d === "涨").length;
    const down = dirs.filter(d => d === "跌").length;
    let res;
    if (up === dirs.length) res = '<span class="resonance up4">全涨 🔥</span>';
    else if (down === dirs.length) res = '<span class="resonance down4">全跌 ❄️</span>';
    else if (up >= 3 || down >= 3) res = `<span class="resonance ${up > down ? 'up3' : 'down3'}">${up}涨${down}跌</span>`;
    else res = `<span class="resonance mixed">${up}涨${down}跌 ⚠️</span>`;
    html += `<tr><td><b>${s.label}</b></td>${dirs.map(d => "<td>" + dirSpan(d) + "</td>").join("")}<td>${res}</td></tr>`;
  }
  html += "</tbody></table>";
  return html;
}

function buildGroupHTML(group, results) {
  const syms = group.symbols;

  // 日线排名卡片
  const dailyRanked = syms.map(s => results["1D"]?.[s.label]).filter(r => r && !r.error).sort((a, b) => b.score - a.score);
  let summary = "";
  if (dailyRanked.length > 0) {
    const best = dailyRanked[0], worst = dailyRanked[dailyRanked.length - 1];
    summary = `<div class="summary">日线最强: <b>${best.label} ${dirSpan(best.direction)}</b> 评分 ${best.score.toFixed(3)} | 最弱: <b>${worst.label}</b> 评分 ${worst.score.toFixed(3)}</div>`;
  }

  let html = `<section class="group" id="${group.id}">
    <h2>${group.emoji} ${group.name} <span class="count">${syms.length}个品种</span></h2>
    ${summary}`;

  // 每个周期一张表
  for (const tf of TF_LIST) {
    html += `<div class="tf-section"><h3>${TF_LABELS[tf]} <small>(Lookback ${LOOKBACK}根)</small></h3>`;
    html += buildTFTable(results, tf, syms);
    html += "</div>";
  }

  // 跨周期共振
  html += `<div class="tf-section"><h3>跨周期方向共振</h3>`;
  html += buildResonanceTable(results, syms);
  html += "</div>";

  // 结论
  html += `<div class="tf-section"><h3>结论</h3>`;
  html += buildConclusion(results, syms, group.name);
  html += "</div></section>";

  return html;
}

function buildConclusion(results, symbols, groupName) {
  const lines = [];

  // 1. 最强/最弱品种（日线冠军）
  const daily = symbols.map(s => results["1D"]?.[s.label]).filter(r => r && !r.error).sort((a, b) => b.score - a.score);
  if (daily.length >= 2) {
    const best = daily[0], worst = daily[daily.length - 1];
    lines.push(`日线趋势最强: <b>${best.label}</b>（${dirSpan(best.direction)}，ADX ${best.adx.toFixed(1)}，R² ${best.r2.toFixed(3)}，评分 ${best.score.toFixed(3)}）`);
    lines.push(`日线趋势最弱: <b>${worst.label}</b>（${dirSpan(worst.direction)}，ADX ${worst.adx.toFixed(1)}，R² ${worst.r2.toFixed(3)}，评分 ${worst.score.toFixed(3)}）`);
  }

  // 2. 共振分析
  const resonance = symbols.map(s => {
    const dirs = TF_LIST.map(t => results[t]?.[s.label]?.direction || null);
    const up = dirs.filter(d => d === "涨").length;
    const down = dirs.filter(d => d === "跌").length;
    return { label: s.label, up, down, dirs };
  });

  const fullUp = resonance.filter(r => r.up === TF_LIST.length);
  const fullDown = resonance.filter(r => r.down === TF_LIST.length);
  const mostlyUp = resonance.filter(r => r.up >= 3 && r.up < TF_LIST.length);
  const mostlyDown = resonance.filter(r => r.down >= 3 && r.down < TF_LIST.length);
  const mixed = resonance.filter(r => r.up < 3 && r.down < 3);

  if (fullUp.length > 0) {
    lines.push(`四周期全涨: <b>${fullUp.map(r => `<span class="up">${r.label}</span>`).join("、")}</b> — 多头最强共识，顺势做多`);
  }
  if (fullDown.length > 0) {
    lines.push(`四周期全跌: <b>${fullDown.map(r => `<span class="down">${r.label}</span>`).join("、")}</b> — 空头完全掌控，顺势做空`);
  }
  if (mostlyUp.length > 0) {
    lines.push(`三涨一跌: <b>${mostlyUp.map(r => `<span class="up">${r.label}</span>`).join("、")}</b> — 整体偏多，但短周期有回调`);
  }
  if (mostlyDown.length > 0) {
    lines.push(`三跌一涨: <b>${mostlyDown.map(r => `<span class="down">${r.label}</span>`).join("、")}</b> — 整体偏空，但短周期有反弹`);
  }
  if (mixed.length > 0) {
    lines.push(`方向混乱: <b>${mixed.map(r => r.label).join("、")}</b> — 无共识方向，建议观望`);
  }

  // 3. 整体市场情绪
  let totalUp = 0, totalDown = 0, totalNeutral = 0;
  TF_LIST.forEach(tf => {
    symbols.forEach(s => {
      const d = results[tf]?.[s.label]?.direction;
      if (d === "涨") totalUp++;
      else if (d === "跌") totalDown++;
      else totalNeutral++;
    });
  });
  const total = totalUp + totalDown + totalNeutral;
  let mood;
  if (totalDown > total * 0.7) mood = '<span class="down">极度偏空 ❄️</span>';
  else if (totalDown > total * 0.55) mood = '<span class="down">偏空 📉</span>';
  else if (totalUp > total * 0.7) mood = '<span class="up">极度偏多 🔥</span>';
  else if (totalUp > total * 0.55) mood = '<span class="up">偏多 📈</span>';
  else mood = '<span style="color:#d29922">中性震荡 ⚖️</span>';
  lines.push(`市场情绪: ${mood}（四周期合计 ${totalUp} 涨 / ${totalDown} 跌）`);

  // 4. ADX 冠军
  const allResults = [];
  TF_LIST.forEach(tf => symbols.forEach(s => { const r = results[tf]?.[s.label]; if (r && !r.error) allResults.push(r); }));
  allResults.sort((a, b) => b.adx - a.adx);
  if (allResults.length > 0) {
    // Find which timeframe the ADX peak came from
    const adxTop = allResults[0];
    let adxTf = "";
    for (const tf of TF_LIST) {
      const r = results[tf]?.[adxTop.label];
      if (r && r.adx === adxTop.adx) { adxTf = TF_LABELS[tf]; break; }
    }
    lines.push(`ADX峰值: <b>${adxTop.label}</b> ${adxTf} ADX ${adxTop.adx.toFixed(1)}，趋势信号最强`);
  }

  // 5. 可操作性总结
  const actionable = [];
  if (fullUp.length > 0) actionable.push(`做多: ${fullUp.map(r => r.label).join("、")}`);
  if (fullDown.length > 0) actionable.push(`做空: ${fullDown.map(r => r.label).join("、")}`);
  if (mostlyUp.length > 0 && fullUp.length === 0) actionable.push(`偏多(谨慎): ${mostlyUp.map(r => r.label).join("、")}`);
  if (mostlyDown.length > 0 && fullDown.length === 0) actionable.push(`偏空(谨慎): ${mostlyDown.map(r => r.label).join("、")}`);
  if (actionable.length === 0) {
    lines.push(`<span style="color:#d29922">无明确操作方向，建议观望</span>`);
  } else {
    lines.push(`操作建议: ${actionable.join(" | ")}`);
  }

  return `<div class="conclusion">${lines.map(l => `<p>• ${l}</p>`).join("")}</div>`;
}

// ========== 主流程 ==========
(async () => {
  console.log("连接 TradingView CDP...");
  const targets = await CDP.List({ port: 9222 });
  const pg = targets.find(t => t.type === "page" && t.url.includes("tradingview.com"));
  if (!pg) { console.log("❌ 未找到 TradingView 页面"); process.exit(1); }
  const client = await CDP({ target: pg.id, port: 9222 });
  await client.Page.enable(); await client.Runtime.enable();

  const allGroupResults = {};
  let totalSymbols = 0;
  GROUPS.forEach(g => totalSymbols += g.symbols.length);
  const totalFetches = totalSymbols * TF_LIST.length;
  let done = 0;

  for (const group of GROUPS) {
    console.log(`\n========== ${group.name} ==========`);
    const groupResults = {};
    for (const tf of TF_LIST) groupResults[tf] = {};

    for (const s of group.symbols) {
      for (const tf of TF_LIST) {
        done++;
        console.log(`  [${done}/${totalFetches}] ${group.name} ${s.label} ${TF_LABELS[tf]}`);
        const bars = await getBars(client, s.sym, tf);
        const a = analyze(bars, LOOKBACK);
        a.label = s.label; a.sym = s.sym;
        groupResults[tf][s.label] = a;
      }
    }
    allGroupResults[group.id] = groupResults;
  }

  await client.close();

  // 生成 HTML
  const sectionsHTML = GROUPS.map(g => buildGroupHTML(g, allGroupResults[g.id])).join("\n");

  const now = new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });

  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>多市场趋势对比报告</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0e17; color: #c9d1d9; padding: 20px; }
h1 { text-align: center; font-size: 28px; margin-bottom: 4px; color: #f0f6fc; }
h1 small { font-size: 14px; color: #8b949e; display: block; margin-top: 4px; }
h2 { font-size: 22px; color: #f0f6fc; margin-bottom: 8px; padding: 12px 16px; background: #161b22; border-radius: 8px 8px 0 0; border-bottom: 2px solid #30363d; }
h2 .count { font-size: 13px; color: #8b949e; font-weight: normal; margin-left: 8px; }
h3 { font-size: 16px; color: #e6edf3; margin: 16px 0 8px 0; padding-left: 14px; border-left: 3px solid #58a6ff; }
h3 small { font-size: 12px; color: #8b949e; font-weight: normal; }
.group { margin-bottom: 32px; background: #161b22; border-radius: 8px; border: 1px solid #30363d; overflow: hidden; }
.tf-section { padding: 0 16px 12px 16px; }
.summary { padding: 8px 16px; font-size: 14px; color: #8b949e; background: #0d1117; border-bottom: 1px solid #21262d; }
.tf-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 8px; }
.tf-table th { background: #21262d; color: #8b949e; padding: 6px 8px; text-align: left; font-weight: 600; font-size: 12px; white-space: nowrap; }
.tf-table td { padding: 5px 8px; border-bottom: 1px solid #21262d; white-space: nowrap; }
.tf-table tbody tr:hover { background: #1c2128; }
.res-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 12px; }
.res-table th { background: #21262d; color: #8b949e; padding: 6px 8px; text-align: center; font-size: 12px; }
.res-table td { padding: 6px 8px; border-bottom: 1px solid #21262d; text-align: center; }
.res-table tbody tr:hover { background: #1c2128; }
.up { color: #3fb950; }
.down { color: #f85149; }
.badge { font-size: 10px; padding: 1px 6px; border-radius: 4px; margin-left: 4px; font-weight: 600; }
.badge.strong { background: #1a4d2e; color: #3fb950; }
.badge.moderate { background: #3d3a00; color: #d29922; }
.badge.weak { background: #2a1a1a; color: #f85149; }
.badge.none { background: #1c2128; color: #8b949e; }
.resonance { font-size: 11px; padding: 1px 6px; border-radius: 4px; font-weight: 600; }
.resonance.up4 { background: #0a3d1a; color: #3fb950; }
.resonance.down4 { background: #3d0a0a; color: #f85149; }
.resonance.up3 { background: #1a3d0a; color: #d29922; }
.resonance.down3 { background: #3d1a0a; color: #d29922; }
.resonance.mixed { background: #1c2128; color: #8b949e; }
.conclusion { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 12px 16px; margin: 8px 0 12px 0; }
.conclusion p { margin: 4px 0; font-size: 13px; line-height: 1.6; color: #c9d1d9; }
.footer { text-align: center; font-size: 12px; color: #484f58; margin-top: 30px; padding: 20px; }
@media (max-width: 900px) { .tf-table { font-size: 11px; } .tf-table td, .tf-table th { padding: 4px 4px; } }
</style>
</head>
<body>
<h1>📊 多市场趋势对比报告<small>生成时间: ${now} | 周期: 日线 / 4H / 1H / 15M | Lookback: ${LOOKBACK}根</small></h1>
${sectionsHTML}
<div class="footer">评分 = 斜率 × R² × ADX% × 一致性 × 振幅系数 | ADX > 25 = 有趋势 | R² 越接近1 = 趋势越规则</div>
</body>
</html>`;

  const outPath = path.join(__dirname, "report.html");
  fs.writeFileSync(outPath, html, "utf-8");
  console.log(`\n✅ 报告已生成: ${outPath}`);
  process.exit(0);
})().catch(e => { console.log("❌", e.message); process.exit(1); });
