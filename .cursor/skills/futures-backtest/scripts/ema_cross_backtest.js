/**
 * EMA 交叉策略回测
 *
 * 策略规则（交叉触发）:
 *   - 多头: 价格上穿 EMA → 开多; 价格下穿 EMA → 平多
 *   - 空头: 价格下穿 EMA → 开空; 价格上穿 EMA → 平空
 *
 * 等价于: 价格在 EMA 上方持多, 下方持空, 永远持仓（单均线趋势跟踪）。
 *
 * 用法:
 *   node ema_cross_backtest.js [周期] [EMA周期] [回测年数]
 *   例: node ema_cross_backtest.js 1D 20 3
 *   例: node ema_cross_backtest.js 15 20 0   （0 = 用全部可用数据）
 *
 * 周期代码: 15 / 60 / 240 / 1D / 1W
 *
 * 通过 CDP 连接 TradingView Desktop, 获取当前图表品种的K线数据。
 */

const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");

const PORT = 9222;
const INTERVAL = process.argv[2] || "1D";    // 默认日线
const EMA_PERIOD = parseInt(process.argv[3] || "20", 10);
const YEARS = parseFloat(process.argv[4] || "0"); // 回测年数，0=用全部数据

// 回测参数
const INITIAL_CAPITAL = 100000;
const SPREAD_POINTS = 0; // 每笔交易滑点/点差成本（价格单位），0 = 忽略

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// 计算 EMA 序列
function calcEMA(closes, period) {
  const ema = [];
  const k = 2 / (period + 1);
  let prev = null;
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) {
      ema.push(null);
      continue;
    }
    if (prev === null) {
      // 初始值用前 period 根简单平均
      let sum = 0;
      for (let j = 0; j < period; j++) sum += closes[j];
      prev = sum / period;
    } else {
      prev = closes[i] * k + prev * (1 - k);
    }
    ema.push(prev);
  }
  return ema;
}

function backtest(bars, emaPeriod) {
  const closes = bars.map(b => b.close);
  const ema = calcEMA(closes, emaPeriod);

  let position = 0;        // 0=空仓, 1=多头, -1=空头
  let entryPrice = 0;
  let cash = INITIAL_CAPITAL;
  let shares = 0;          // 多头持仓数量（满仓用现金买入）
  let shortShares = 0;     // 空头持仓数量

  const trades = [];
  let equity = INITIAL_CAPITAL;
  let peakEquity = INITIAL_CAPITAL;
  let maxDrawdown = 0;
  const equityCurve = [];

  for (let i = emaPeriod; i < closes.length; i++) {
    const prevClose = closes[i - 1];
    const prevEma = ema[i - 1];
    const curClose = closes[i];
    const curEma = ema[i];

    // 交叉判断
    const crossUp = prevEma !== null && prevClose <= prevEma && curClose > curEma;
    const crossDown = prevEma !== null && prevClose >= prevEma && curClose < curEma;

    if (crossUp && position !== 1) {
      // 平空 + 开多
      if (position === -1) {
        // 平空
        const pnl = (entryPrice - curClose) * shortShares - SPREAD_POINTS * shortShares;
        cash += shortShares * entryPrice + pnl; // 归还保证金 + 盈利
        trades.push({ dir: "空头平仓", entryPrice, exitPrice: curClose, pnl, time: bars[i].time });
        shortShares = 0;
        position = 0;
      }
      // 开多（满仓）
      entryPrice = curClose;
      shares = cash / curClose;
      cash = 0;
      position = 1;
      trades.push({ dir: "多头开仓", entryPrice, exitPrice: null, pnl: 0, time: bars[i].time });
    } else if (crossDown && position !== -1) {
      // 平多 + 开空
      if (position === 1) {
        const pnl = (curClose - entryPrice) * shares - SPREAD_POINTS * shares;
        cash = shares * curClose + pnl;
        trades.push({ dir: "多头平仓", entryPrice, exitPrice: curClose, pnl, time: bars[i].time });
        shares = 0;
        position = 0;
      }
      // 开空（满仓）
      entryPrice = curClose;
      shortShares = cash / curClose;
      cash = 0;
      position = -1;
      trades.push({ dir: "空头开仓", entryPrice, exitPrice: null, pnl: 0, time: bars[i].time });
    }

    // 计算当前权益（用于回撤）
    if (position === 1) {
      equity = shares * curClose;
    } else if (position === -1) {
      equity = shortShares * entryPrice + (entryPrice - curClose) * shortShares;
    } else {
      equity = cash;
    }
    if (equity > peakEquity) peakEquity = equity;
    const dd = (peakEquity - equity) / peakEquity;
    if (dd > maxDrawdown) maxDrawdown = dd;
    equityCurve.push({ time: bars[i].time, equity });
  }

  // 收盘平仓（最后持仓市值）
  if (position === 1) {
    cash = shares * closes[closes.length - 1];
    shares = 0;
  } else if (position === -1) {
    cash = shortShares * entryPrice + (entryPrice - closes[closes.length - 1]) * shortShares;
    shortShares = 0;
  }

  // 统计
  const roundTrips = []; // 一次完整开平算一笔
  let curTrade = null;
  for (const t of trades) {
    if (t.dir.endsWith("开仓")) {
      curTrade = { dir: t.dir, entryPrice: t.entryPrice, entryTime: t.time };
    } else if (t.dir.endsWith("平仓") && curTrade) {
      roundTrips.push({
        dir: curTrade.dir,
        entryPrice: curTrade.entryPrice,
        exitPrice: t.exitPrice,
        pnl: t.pnl,
        entryTime: curTrade.entryTime,
        exitTime: t.time,
      });
      curTrade = null;
    }
  }

  const wins = roundTrips.filter(t => t.pnl > 0);
  const losses = roundTrips.filter(t => t.pnl <= 0);
  const grossWin = wins.reduce((s, t) => s + t.pnl, 0);
  const grossLoss = losses.reduce((s, t) => s + t.pnl, 0);
  const avgWin = wins.length ? grossWin / wins.length : 0;
  const avgLoss = losses.length ? grossLoss / losses.length : 0;

  return {
    finalEquity: cash,
    totalReturn: (cash - INITIAL_CAPITAL) / INITIAL_CAPITAL,
    totalTrades: roundTrips.length,
    wins: wins.length,
    losses: losses.length,
    winRate: roundTrips.length ? wins.length / roundTrips.length : 0,
    avgWin,
    avgLoss,
    profitFactor: grossLoss !== 0 ? grossWin / Math.abs(grossLoss) : (grossWin > 0 ? Infinity : 0),
    maxDrawdown,
    maxWin: wins.length ? Math.max(...wins.map(t => t.pnl)) : 0,
    maxLoss: losses.length ? Math.min(...losses.map(t => t.pnl)) : 0,
    roundTrips,
    equityCurve,
    barCount: closes.length - emaPeriod,
  };
}

async function getBars(client, symbol, interval) {
  await client.Runtime.evaluate({
    expression: `(function() { TradingViewApi.activeChart().setResolution("${interval}"); return 1; })()`,
    returnByValue: true, awaitPromise: true, timeout: 10000,
  });
  await sleep(4000);

  // 滚动到最早时间，触发加载全部历史数据（突破默认 300 根限制）
  await client.Runtime.evaluate({
    expression: `(function() { TradingViewApi.activeChart()._chartWidget.model().timeScale().scrollToFirstBar(); return 1; })()`,
    returnByValue: true, awaitPromise: true, timeout: 5000,
  });

  // 等待历史数据逐步加载完成（轮询数量直到稳定）
  let prevCount = 0;
  for (let i = 0; i < 20; i++) {
    await sleep(1500);
    const cntR = await client.Runtime.evaluate({
      expression: `(function() { const items = TradingViewApi.activeChart().chartModel().mainSeries().data().m_bars._items; return items ? items.length : 0; })()`,
      returnByValue: true, awaitPromise: true, timeout: 5000,
    });
    const count = cntR.result.value;
    if (count === prevCount && count > 0) break;
    prevCount = count;
  }

  const r = await client.Runtime.evaluate({
    expression: `(function() {
      const items = TradingViewApi.activeChart().chartModel().mainSeries().data().m_bars._items;
      return items ? items.map(b => ({ t: b.value[0], o: b.value[1], h: b.value[2], l: b.value[3], c: b.value[4], v: b.value[5] })) : [];
    })()`,
    returnByValue: true, awaitPromise: true, timeout: 10000,
  });
  return (r.result.value || []).map(b => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c, volume: b.v }));
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

(async () => {
  console.log(`EMA${EMA_PERIOD} 交叉策略回测 | 周期: ${INTERVAL} | 回测年数: ${YEARS > 0 ? YEARS : "全部"} | 初始资金: ${INITIAL_CAPITAL}\n`);

  let client;
  try {
    const targets = await CDP.List({ port: PORT });
    const pg = targets.find(t => t.type === "page" && t.url.includes("tradingview.com"));
    if (!pg) { console.log("❌ 未找到 TradingView 页面"); process.exit(1); }
    client = await CDP({ target: pg.id, port: PORT });
    await client.Page.enable(); await client.Runtime.enable();

    // 获取当前品种
    const symResult = await client.Runtime.evaluate({
      expression: `(function() { return TradingViewApi.activeChart().symbol(); })()`,
      returnByValue: true, awaitPromise: true, timeout: 5000,
    });
    const symbol = symResult.result.value;

    console.log(`品种: ${symbol}`);
    let bars = await getBars(client, symbol, INTERVAL);
    console.log(`加载K线: ${bars.length} 根 (${fmtTime(bars[0].time)} ~ ${fmtTime(bars[bars.length - 1].time)})`);

    // 按年数截取
    if (YEARS > 0) {
      const cutoff = Math.floor(Date.now() / 1000) - Math.floor(YEARS * 365 * 24 * 3600);
      bars = bars.filter(b => b.time >= cutoff);
      console.log(`截取近 ${YEARS} 年: ${bars.length} 根 (${fmtTime(bars[0].time)} ~ ${fmtTime(bars[bars.length - 1].time)})`);
    }
    console.log("");

    if (bars.length < EMA_PERIOD + 10) {
      console.log(`❌ 数据不足（${bars.length}根），无法回测 EMA${EMA_PERIOD}。`);
      process.exit(1);
    }

    const result = backtest(bars, EMA_PERIOD);

    console.log("═".repeat(60));
    console.log("                    回测结果");
    console.log("═".repeat(60));
    console.log(`  初始资金       : ${INITIAL_CAPITAL.toFixed(2)}`);
    console.log(`  最终资金       : ${result.finalEquity.toFixed(2)}`);
    console.log(`  总收益率       : ${(result.totalReturn * 100).toFixed(2)}%`);
    console.log(`  最大回撤       : ${(result.maxDrawdown * 100).toFixed(2)}%`);
    console.log("─".repeat(60));
    console.log(`  总交易次数     : ${result.totalTrades}`);
    console.log(`  盈利次数       : ${result.wins}`);
    console.log(`  亏损次数       : ${result.losses}`);
    console.log(`  胜率           : ${(result.winRate * 100).toFixed(2)}%`);
    console.log(`  平均盈利       : ${result.avgWin.toFixed(2)}`);
    console.log(`  平均亏损       : ${result.avgLoss.toFixed(2)}`);
    console.log(`  盈亏比         : ${result.profitFactor === Infinity ? "∞" : result.profitFactor.toFixed(2)}`);
    console.log(`  最大单笔盈利   : ${result.maxWin.toFixed(2)}`);
    console.log(`  最大单笔亏损   : ${result.maxLoss.toFixed(2)}`);
    console.log("─".repeat(60));

    // 最近10笔交易明细
    console.log("\n最近10笔交易:");
    console.log("| 方向 | 开仓价 | 平仓价 | 盈亏 | 开仓时间 | 平仓时间 |");
    console.log("|------|--------|--------|------|----------|----------|");
    const recent = result.roundTrips.slice(-10);
    for (const t of recent) {
      const dirName = t.dir === "多头开仓" ? "多" : "空";
      console.log(`| ${dirName} | ${t.entryPrice.toFixed(2)} | ${t.exitPrice.toFixed(2)} | ${t.pnl >= 0 ? "+" : ""}${t.pnl.toFixed(2)} | ${fmtTime(t.entryTime)} | ${fmtTime(t.exitTime)} |`);
    }

    console.log(`\n注: 未含手续费/点差/滑点；信号以收盘价成交。`);

  } catch (e) {
    console.log("❌ 错误:", e.message);
  } finally {
    if (client) await client.close();
    process.exit(0);
  }
})();
