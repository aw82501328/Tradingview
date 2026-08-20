/**
 * 多品种多周期趋势对比工具
 *
 * 通过 CDP 连接 TradingView Desktop，依次切换品种/周期获取K线数据，
 * 计算 ADX、线性回归 R²、方向一致性等指标，按综合评分排名。
 *
 * 用法:
 *   node scripts/compare.js
 *   node scripts/compare.js --symbols "OANDA:XAUUSD,OANDA:XAGUSD" --timeframes "1D,240"
 *
 * 前置条件:
 *   1. TradingView Desktop 以 --remote-debugging-port=9222 启动
 *   2. 已打开至少一张图表
 *   3. chrome-remote-interface 已安装 (在 server-cdp/node_modules)
 */

const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");

// ========== 参数解析 ==========
function parseArgs() {
  const args = {};
  for (const arg of process.argv.slice(2)) {
    const m = arg.match(/^--(\w+)=(.+)$/);
    if (m) args[m[1]] = m[2];
  }
  // 默认品种
  args.symbols = (args.symbols || "OANDA:XAUUSD,OANDA:XAGUSD,TVC:USOIL,FX:NAS100").split(",").map(s => s.trim());
  // 默认周期
  args.timeframes = (args.timeframes || "1D,240,60,15").split(",").map(s => s.trim());
  // 默认 lookback 根数
  args.lookback = parseInt(args.lookback || "20", 10);
  // 品种标签（可选）
  args.labels = args.labels ? args.labels.split(",").map(s => s.trim()) : null;
  return args;
}

// ========== 指标计算 ==========

function wilder(arr, n) {
  if (arr.length < n) return [];
  const s = [arr.slice(0, n).reduce((a, b) => a + b, 0)];
  for (let i = n; i < arr.length; i++) {
    s.push(s[s.length - 1] + (arr[i] - s[s.length - 1]) / n);
  }
  return s;
}

function linearRegression(prices) {
  const n = prices.length;
  if (n < 2) return { slope: 0, r2: 0 };
  let sumX = 0, sumY = 0, sumXY = 0, sumX2 = 0, sumY2 = 0;
  for (let i = 0; i < n; i++) {
    sumX += i; sumY += prices[i]; sumXY += i * prices[i];
    sumX2 += i * i; sumY2 += prices[i] * prices[i];
  }
  const den = n * sumX2 - sumX * sumX;
  if (den === 0) return { slope: 0, r2: 0 };
  const slope = (n * sumXY - sumX * sumY) / den;
  const intercept = (sumY - slope * sumX) / n;
  let ssRes = 0, ssTot = 0;
  const meanY = sumY / n;
  for (let i = 0; i < n; i++) {
    const pred = slope * i + intercept;
    ssRes += (prices[i] - pred) ** 2;
    ssTot += (prices[i] - meanY) ** 2;
  }
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
    const dx = (pdi + mdi) === 0 ? 0 : (Math.abs(pdi - mdi) / (pdi + mdi)) * 100;
    adxRaw.push(dx);
  }
  if (adxRaw.length < period) return { adx: 0, plusDI: 0, minusDI: 0 };
  const adxS = wilder(adxRaw, period);
  return {
    adx: adxS[adxS.length - 1],
    plusDI: atrS[atrS.length - 1] > 0 ? (pdiS[pdiS.length - 1] / atrS[atrS.length - 1]) * 100 : 0,
    minusDI: atrS[atrS.length - 1] > 0 ? (mdiS[mdiS.length - 1] / atrS[atrS.length - 1]) * 100 : 0,
  };
}

function analyze(bars, lookback) {
  if (!bars || bars.length < lookback + 10) return { error: `数据不足 (${bars ? bars.length : 0}根, 需≥${lookback + 10})` };
  const closes = bars.map(b => b.c), highs = bars.map(b => b.h), lows = bars.map(b => b.l);

  const cLookback = closes.slice(-lookback);
  const reg = linearRegression(cLookback);
  const avgLook = cLookback.reduce((a, b) => a + b, 0) / lookback;
  const ns = avgLook > 0 ? (reg.slope / avgLook) * 100 : 0;

  const adx = calcADX(highs, lows, closes);
  const maBias = (closes[closes.length - 1] / avgLook - 1) * 100;

  const dir = ns >= 0 ? 1 : -1;
  let aligned = 0;
  for (let i = closes.length - lookback + 1; i < closes.length; i++) {
    if ((closes[i] - closes[i - 1]) * dir > 0) aligned++;
  }
  const consistency = lookback > 1 ? aligned / (lookback - 1) : 0;

  const trArr = [];
  for (let i = 1; i < highs.length; i++) {
    trArr.push(Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1])));
  }
  const atr14 = trArr.slice(-14).reduce((a, b) => a + b, 0) / 14;
  const todayRange = atr14 > 0 ? (highs[highs.length - 1] - lows[lows.length - 1]) / atr14 : 1;

  const rets = [];
  for (let i = 1; i < closes.length; i++) rets.push((closes[i] - closes[i - 1]) / closes[i - 1]);
  const meanR = rets.reduce((a, b) => a + b, 0) / rets.length;
  const vol = Math.sqrt(rets.reduce((a, b) => a + (b - meanR) ** 2, 0) / rets.length) * 100;

  // 综合评分: 斜率 × R² × ADX × 一致性 × 振幅系数
  const score = Math.abs(ns) * 20 * reg.r2 * (adx.adx / 100) * consistency * Math.min(todayRange / 2, 2);

  return {
    price: closes[closes.length - 1],
    bars: bars.length,
    slope: ns.toFixed(4), r2: reg.r2.toFixed(3),
    adx: adx.adx.toFixed(1), plusDI: adx.plusDI.toFixed(1), minusDI: adx.minusDI.toFixed(1),
    direction: ns >= 0 ? "涨" : "跌",
    maBias: maBias.toFixed(2) + "%",
    consistency: (consistency * 100).toFixed(1) + "%",
    aRange: todayRange.toFixed(1) + "x",
    vol: vol.toFixed(2) + "%",
    score,
  };
}

// ========== CDP 操作 ==========

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function getBars(client, symbol, interval) {
  await client.Runtime.evaluate({
    expression: `(function() { TradingViewApi.activeChart().setSymbol("${symbol}"); return 1; })()`,
    returnByValue: true, awaitPromise: true, timeout: 15000,
  });
  await sleep(4500);
  await client.Runtime.evaluate({
    expression: `(function() { TradingViewApi.activeChart().setResolution("${interval}"); return 1; })()`,
    returnByValue: true, awaitPromise: true, timeout: 10000,
  });
  await sleep(3500);
  const r = await client.Runtime.evaluate({
    expression: `(function() {
      const items = TradingViewApi.activeChart().chartModel().mainSeries().data().m_bars._items;
      return items ? items.map(b => ({ v: b.value })) : [];
    })()`,
    returnByValue: true, awaitPromise: true, timeout: 10000,
  });
  return (r.result.value || []).map(b => ({ t: b.v[0], o: b.v[1], h: b.v[2], l: b.v[3], c: b.v[4], vol: b.v[5] }));
}

// ========== 输出格式化 ==========

const ICONS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"];
const TF_LABELS = { "1D": "日线", "240": "4小时", "60": "1小时", "15": "15分钟", "1W": "周线", "1M": "月线" };

function renderTable(rows, title) {
  const header = "| 排名 | 品种 | 方向 | 价格 | ADX | +DI/-DI | R² | 斜率%/bar | 一致性 | MA偏离 | 振幅 | 波动 | 评分 |";
  const sep    = "|------|------|------|------|-----|---------|----|-----------|--------|--------|------|------|------|";
  const lines = [title, "", header, sep];
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (r.error) {
      lines.push(`| ${ICONS[i] || (i+1)} | ${r.label} | ❌ ${r.error} | - | - | - | - | - | - | - | - | - | - |`);
    } else {
      lines.push(`| ${ICONS[i] || (i+1)} | ${r.label} | ${r.direction} | ${r.price?.toFixed(2)} | ${r.adx} | ${r.plusDI}/${r.minusDI} | ${r.r2} | ${r.slope} | ${r.consistency} | ${r.maBias} | ${r.aRange} | ${r.vol} | ${r.score.toFixed(3)} |`);
    }
  }
  return lines.join("\n");
}

// ========== 主流程 ==========

(async () => {
  const args = parseArgs();
  const symbols = args.symbols;
  const timeframes = args.timeframes;
  const labels = args.labels || symbols.map(s => s.split(":")[1] || s);
  const lookback = args.lookback;

  if (symbols.length === 0 || timeframes.length === 0) {
    console.log("用法: node scripts/compare.js [--symbols=...] [--timeframes=...] [--lookback=20]");
    process.exit(1);
  }

  const symbolDefs = symbols.map((sym, i) => ({ label: labels[i] || sym, sym }));

  console.log(`品种: ${symbolDefs.map(s => s.label).join(", ")}`);
  console.log(`周期: ${timeframes.map(t => t).join(", ")} | lookback: ${lookback}根\n`);

  try {
    const targets = await CDP.List({ port: 9222 });
    const pg = targets.find(t => t.type === "page" && t.url.includes("tradingview.com"));
    if (!pg) {
      console.log("❌ 未找到 TradingView 页面。请确保:\n  1. TradingView Desktop 已启动\n  2. 以 --remote-debugging-port=9222 启动\n  3. 已打开至少一张图表");
      process.exit(1);
    }

    const client = await CDP({ target: pg.id, port: 9222 });
    await client.Page.enable(); await client.Runtime.enable();

    const allResults = {};

    for (const intv of timeframes) {
      allResults[intv] = {};
      for (const s of symbolDefs) {
        const bars = await getBars(client, s.sym, intv);
        const a = analyze(bars, lookback);
        a.label = s.label; a.sym = s.sym;
        allResults[intv][s.label] = a;
      }
    }

    // 输出每个周期的排名表
    console.log("═".repeat(80));
    console.log("                    多周期趋势排名总览");
    console.log("═".repeat(80));

    for (const intv of timeframes) {
      const results = allResults[intv];
      const sorted = symbolDefs
        .map(s => results[s.label])
        .filter(r => r && !r.error)
        .sort((a, b) => b.score - a.score);

      const tfName = TF_LABELS[intv] || intv;
      const title = `## ${tfName} (${intv}) — lookback ${lookback}根`;
      console.log("\n" + renderTable(sorted, title));

      if (sorted.length > 0) {
        const best = sorted[0], worst = sorted[sorted.length - 1];
        console.log(`\n  → 最强: ${best.label}(${best.direction}) 评分${best.score.toFixed(3)}  |  最弱: ${worst.label}(${worst.direction}) 评分${worst.score.toFixed(3)}`);
      }
    }

    // 跨周期方向一致性
    console.log("\n\n" + "═".repeat(80));
    console.log("                    跨周期方向一致性");
    console.log("═".repeat(80));
    const dirLines = ["| 品种 | " + timeframes.map(t => TF_LABELS[t] || t).join(" | ") + " | 共振 |"];
    dirLines.push("|------|" + timeframes.map(() => "------").join("|") + "|------|");
    for (const s of symbolDefs) {
      const dirs = timeframes.map(t => allResults[t][s.label]?.direction || "—");
      const upCount = dirs.filter(d => d === "涨").length;
      const downCount = dirs.filter(d => d === "跌").length;
      const resonance = upCount === dirs.length ? "全涨" : downCount === dirs.length ? "全跌" : `${upCount}涨${downCount}跌`;
      dirLines.push(`| ${s.label} | ${dirs.join(" | ")} | ${resonance} |`);
    }
    console.log(dirLines.join("\n"));

    await client.close();
    process.exit(0);
  } catch (e) {
    console.log(`\n❌ 错误: ${e.message}`);
    console.log("请检查:\n  1. TradingView Desktop 是否以调试模式运行\n  2. 端口 9222 是否被占用\n  3. 图表页面是否已打开");
    process.exit(1);
  }
})();
