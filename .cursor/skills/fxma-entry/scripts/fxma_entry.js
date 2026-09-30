// -*- coding: utf-8 -*-
// 强分型均线V1（fxma_v1）工作台标记脚本 —— 与 py_chain/fx_ma.py 同一套规则的工作台侧实现。
//
// 规则（与引擎一致，详见 SKILL.md / SPEC.md）：
//   信号（每个所选进出场周期 P 独立，P 每根已收K线收盘评估）：
//     ① P 上最新买卖点（chan-core findBuyPoints/findSellPoints）属所选类别
//        （1买/1卖→1类，2买/类2买、2卖/类2卖→2类，3买/类3买、3卖/类3卖→3类；4类不交易）；
//        反向点出现后该点失效
//     ② 点之后出现强分型（实体口径：底=右肩收盘>左肩开盘，顶=右肩收盘<左肩开盘，
//        落差 ≥ strongFxMinPts 点）
//     ③ 均线分离（按类别选均线对：1类=maFast1/maSlow1，2/3类=maFast2/maSlow2）：
//        当拍收盘 快线高于慢线≥crossMinPts（买）/ 低于慢线≥crossMinPts（卖）
//     ④ pointValidBars 根内齐备 → 触发（每点一次）→ 下一根 P 周期K线开盘成交
//   出场：P 已收K线触及 止损(进场∓stopPts)/止盈(进场±tpPts) → 下一根 P 开盘成交；
//        同根双触按 sameBarPriority（默认止损优先）；期末未触发 mark-to-market。
//   互斥：mutexScope=global 同向全局一笔 / perPeriod 每周期独立。
//
// 口径声明（与回测/实盘引擎的既有差异，同 mark-entry「工作台 vs 引擎」）：
//   本脚本用画笔落盘的最终笔快照 + 全窗口K线（事后视角）回放评估；
//   引擎用当下增量状态逐拍评估（无未来函数）。两者信号可能不完全一致，
//   属研究口径差（见 SPEC.md §2.1.5.2），不能混用。

const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");

const core = require("../../chan-core/scripts/chan_core.js");
const { calcMACD, intervalSecOf, markWickBars, mergeBars, findFractals,
        findBuyPoints, findSellPoints, fmtT } = core;

const CACHE_DIR = process.env.CHAN_CACHE_DIR || path.join(__dirname, "..", "..", "..", "..", ".cursor", "cache");
const cacheFile = (prefix, symbol) => path.join(CACHE_DIR, `${prefix}_${String(symbol).replace(/[^A-Za-z0-9_.-]/g, "_")}.json`);

// ============================================================
// 参数
// ============================================================

const args = process.argv.slice(2);
const DRY = args.includes("--dry");      // 只算不画（分析/测试用）
const DEBUG = DRY || args.includes("--debug");
const getStrArg = (name, def) => {
  const hit = args.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : def;
};
const getNumArg = (name, def) => {
  const v = parseFloat(getStrArg(name, ""));
  return Number.isFinite(v) ? v : def;
};

const FROM_DATE = getStrArg("from", "");
const ENTRY_RES = String(getStrArg("entry-res", "3,15,60")).split(",").map(s => s.trim()).filter(Boolean);
const POINT_CLASSES = new Set(String(getStrArg("point-classes", "1,2,3")).split(",").map(s => s.trim()).filter(Boolean));
const MA_TYPE = String(getStrArg("ma-type", "SMA")).toUpperCase() === "EMA" ? "EMA" : "SMA";
const MA_FAST1 = getNumArg("ma-fast-1", 8);
const MA_SLOW1 = getNumArg("ma-slow-1", 20);
const MA_FAST2 = getNumArg("ma-fast-2", 5);
const MA_SLOW2 = getNumArg("ma-slow-2", 8);
const CROSS_MIN_PTS = getNumArg("cross-min-pts", 2.0);
const STRONG_FX_MIN_PTS = getNumArg("strong-fx-min-pts", 0.0);
const POINT_VALID_BARS = Math.max(0, Math.round(getNumArg("point-valid-bars", 0)));
const STOP_PTS = getNumArg("stop-pts", 10.0);
const TP_PTS = getNumArg("tp-pts", 30.0);
const SAME_BAR_PRIORITY = String(getStrArg("same-bar-priority", "stop")) === "tp" ? "tp" : "stop";
const MUTEX_SCOPE = String(getStrArg("mutex-scope", "global")) === "perPeriod" ? "perPeriod" : "global";
const LOTS = getNumArg("lots", 4);
const UPPER_OF = { "30S": "3", "3": "15", "15": "60", "60": "240" };
const POINT_CLASS = {
  "1买": 1, "1卖": 1,
  "2买": 2, "类2买": 2, "2卖": 2, "类2卖": 2,
  "3买": 3, "类3买": 3, "3卖": 3, "类3卖": 3,
};

const BUY_COLOR = "#F23645";
const SELL_COLOR = "#089981";
const EXIT_COLOR = "#FFEB3B";
const BAR_BUFFER = 30;

// ============================================================
// 纯函数（导出供单元测试）
// ============================================================

/** SMA/EMA 序列：closes[i] → ma[i]（窗口未满为 null）。 */
function maSeries(closes, period, type) {
  const out = new Array(closes.length).fill(null);
  if (String(type).toUpperCase() === "EMA") {
    const k = 2.0 / (period + 1);
    let ema = null;
    for (let i = 0; i < closes.length; i++) {
      ema = ema === null ? closes[i] : closes[i] * k + ema * (1 - k);
      out[i] = ema;
    }
    return out;
  }
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

/**
 * 强分型（实体口径，与 fx_ma.strong_fx_after 同式）：返回 t（含）之后首个命中
 * 分型的 {time, confirmTime} 或 null。confirmTime = 右肩合并块的收盘时刻
 * （该块最后一根原始K线时间 + barSec——引擎口径：右肩块收盘后分型才可用）。
 */
function strongFxAfter(merged, fractals, t, kind, minPts, barSec) {
  for (const f of fractals) {
    if (f.type !== kind || f.time < t) continue;
    const i = f.mergedIdx;
    if (i - 1 < 0 || i + 1 >= merged.length) continue; // 左/右肩不完整
    const left = merged[i - 1], right = merged[i + 1];
    const diff = kind === "bottom" ? right.close - left.open : left.open - right.close;
    if (diff > 0 && diff >= (minPts || 0)) {
      return { time: f.time, confirmTime: right.time + barSec };
    }
  }
  return null;
}

/**
 * 单周期信号扫描（工作台口径：全窗口K线 + 最终笔快照，按时间轴回放）。
 * @param bars P 周期K线（升序）；periodBis 画笔落盘 {res: [bi]}；upperRes 上级周期键
 * @returns signals [{periodX, direction, strategyKey, pointType, pointTime, signalTime,
 *                    signalPrice, entryIdx}]（未含互斥过滤；entryIdx=成交K线下标）
 */
function scanPeriodSignals(P, bars, periodBis, upperRes) {
  const barSec = intervalSecOf(P) || 180;
  const closes = bars.map(b => b.close);
  const f1 = maSeries(closes, MA_FAST1, MA_TYPE), s1 = maSeries(closes, MA_SLOW1, MA_TYPE);
  const f2 = maSeries(closes, MA_FAST2, MA_TYPE), s2 = maSeries(closes, MA_SLOW2, MA_TYPE);
  const merged = mergeBars(markWickBars(bars));
  const fractals = findFractals(merged);
  const macdArr = calcMACD(bars);
  const buys = findBuyPoints(periodBis[P] || [], periodBis[upperRes] || [], macdArr, barSec);
  const sells = findSellPoints(periodBis[P] || [], periodBis[upperRes] || [], macdArr, barSec);
  // 点按时间升序（回放时维护「当前最新点」——引擎语义：只有最新点可触发）
  const pts = [...buys.map(p => ({ ...p, side: "buy" })),
               ...sells.map(p => ({ ...p, side: "sell" }))].sort((a, b) => a.time - b.time);
  const signals = [];
  const fired = new Set();
  let pi = 0, curBuy = null, curSell = null;
  for (let i = 0; i < bars.length; i++) {
    const closeT = bars[i].time + barSec; // 决策拍 = 本根收盘 = 下一根开盘时刻
    while (pi < pts.length && pts[pi].time <= bars[i].time) {
      const p = pts[pi++];
      if (p.side === "buy") curBuy = p; else curSell = p;
    }
    for (const [pt, direction, kind] of [[curBuy, "long", "bottom"], [curSell, "short", "top"]]) {
      if (!pt) continue;
      const cls = POINT_CLASS[pt.type];
      if (cls === undefined || !POINT_CLASSES.has(String(cls))) continue;
      const key = `${P}|${pt.type}|${pt.time}`;
      if (fired.has(key)) continue;
      const opp = direction === "long" ? curSell : curBuy;
      if (opp && opp.time > pt.time) { fired.add(key); continue; } // 反向点已出现
      if (POINT_VALID_BARS > 0) {
        let after = 0;
        for (let j = 0; j <= i; j++) if (bars[j].time > pt.time) after++;
        if (after > POINT_VALID_BARS) { fired.add(key); continue; } // 超时作废
      }
      const fx = strongFxAfter(merged, fractals, pt.time, kind, STRONG_FX_MIN_PTS, barSec);
      if (!fx || fx.confirmTime > closeT) continue;
      const fa = cls === 1 ? f1[i] : f2[i], sl = cls === 1 ? s1[i] : s2[i];
      if (fa === null || sl === null) continue;
      const diff = direction === "short" ? sl - fa : fa - sl;
      if (!(diff > 0 && diff >= CROSS_MIN_PTS)) continue;
      fired.add(key);
      signals.push({
        periodX: P, direction,
        strategyKey: `fx${cls}${direction === "long" ? "Buy" : "Sell"}`,
        pointType: pt.type, pointTime: pt.time, pointPrice: pt.price,
        strongFxTime: fx.time, crossGap: Math.round(diff * 10000) / 10000,
        signalTime: closeT, signalPrice: closes[i], entryIdx: i + 1,
      });
    }
  }
  return signals;
}

/**
 * 全局互斥 + 出场模拟（与引擎同口径：已收K线触及 → 下一开盘成交）。
 * @returns 每个信号补齐 entryTime/entryPrice/stopRef/tpRef/exits/exitType/pnl/suppressed
 */
function applyMutexAndSimulate(signals, barsByP, contractMult = 1.0) {
  const sorted = [...signals].sort((a, b) => a.signalTime - b.signalTime);
  // 开仓占用：global 按方向 / perPeriod 按 (P,方向)；value = 终局时刻
  const busyUntil = (P, d) => MUTEX_SCOPE === "global" ? `g|${d}` : `p|${P}|${d}`;
  const slots = new Map();
  const open = [];
  for (const s of sorted) {
    const k = busyUntil(s.periodX, s.direction);
    const busy = slots.get(k) || 0;
    if (busy > s.signalTime) { s.suppressed = true; continue; }
    const bars = barsByP[s.periodX] || [];
    const ei = s.entryIdx;
    if (ei <= 0 || ei >= bars.length) { s.suppressed = true; continue; } // 无下一根可成交
    const short = s.direction === "short";
    const entryPrice = bars[ei].open, entryTime = bars[ei].time;
    const stopRef = short ? entryPrice + STOP_PTS : entryPrice - STOP_PTS;
    const tpRef = short ? entryPrice - TP_PTS : entryPrice + TP_PTS;
    const d = short ? -1 : 1;
    s.entryTime = entryTime; s.entryPrice = entryPrice;
    s.stopRef = stopRef; s.tpRef = tpRef; s.lots = LOTS;
    s.exits = []; s.exitType = null; s.pnl = null;
    let pending = null;
    for (let j = ei; j < bars.length; j++) {
      const b = bars[j];
      if (pending) { // 上一根已收盘判定挂起 → 本根开盘成交
        const ex = { type: pending, time: b.time, price: b.open };
        s.exits.push(ex); s.exitType = pending; s.exitTime = b.time; s.exitPrice = b.open;
        s.pnl = (b.open - entryPrice) * d * LOTS * contractMult;
        break;
      }
      if (j === ei) continue; // 进场那根不判出场（引擎 _evalCut 口径）
      const stopHit = short ? b.high >= stopRef : b.low <= stopRef;
      const tpHit = short ? b.low <= tpRef : b.high >= tpRef;
      if (stopHit && tpHit) pending = SAME_BAR_PRIORITY === "stop" ? "stop" : "takeProfit";
      else if (stopHit) pending = "stop";
      else if (tpHit) pending = "takeProfit";
      if (pending) s.exitTriggerTime = b.time;
    }
    if (pending && !s.exitType) { s.pendingUnfilled = true; } // 触发K线无下一根：仍持仓
    if (!s.exitType) { // 未终局 → mark-to-market（最新收盘）
      const last = bars[bars.length - 1];
      s.state = "open";
      if (last) s.pnl = (last.close - entryPrice) * d * LOTS * contractMult;
    } else {
      s.state = "closed";
    }
    slots.set(k, s.exitTime || Infinity); // 占用至终局（未终局=无限占用）
    open.push(s);
  }
  return sorted;
}

// ============================================================
// 可见性 / 绘图（与 mark-entry 同款）
// ============================================================

function onlyThisInterval(res) {
  const s = String(res).toUpperCase();
  const NONE = { ticks: false, seconds: false, secondsFrom: 1, secondsTo: 59,
    minutes: false, minutesFrom: 1, minutesTo: 59, hours: false, hoursFrom: 1, hoursTo: 24,
    days: false, daysFrom: 1, daysTo: 366, weeks: false, weeksFrom: 1, weeksTo: 52,
    months: false, monthsFrom: 1, monthsTo: 12 };
  switch (s) {
    case "30S": return { ...NONE, seconds: true, secondsFrom: 30, secondsTo: 30 };
    case "3":   return { ...NONE, minutes: true, minutesFrom: 3, minutesTo: 3 };
    case "15":  return { ...NONE, minutes: true, minutesFrom: 15, minutesTo: 15 };
    case "60":
    case "1H":  return { ...NONE, hours: true, hoursFrom: 1, hoursTo: 1 };
    default:    return { ...NONE, minutes: true, minutesFrom: 1, minutesTo: 59 };
  }
}

// ============================================================
// 主流程
// ============================================================

async function main() {
  if (!FROM_DATE) { console.log("错误: 必须指定起始日期 --from=YYYY-MM-DD"); process.exit(1); }
  if (!ENTRY_RES.length) { console.log("错误: --entry-res 不能为空（可选 30S/3/15/60）"); process.exit(1); }
  let client;
  try {
    const targets = await CDP.List({ port: 9222 });
    const pg = targets.find(t => t.type === "page" && t.url.includes("tradingview.com"));
    if (!pg) { console.log("ERROR: 未找到 TradingView 页面"); process.exit(1); }
    client = await CDP({ target: pg.id, port: 9222 });
    await client.Page.enable();
    await client.Runtime.enable();
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    const curRes = await client.Runtime.evaluate({
      expression: `(function() {
        const chart = TradingViewApi.activeChart();
        return { symbol: chart.symbol(), resolution: String(chart.resolution()) };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    const SYMBOL = curRes.result.value.symbol;
    const originalRes = curRes.result.value.resolution;
    console.log("品种:", SYMBOL, "当前周期:", originalRes);
    console.log(`强分型均线V1：进出场周期 ${ENTRY_RES.join(",")}，类别 ${[...POINT_CLASSES].join(",")}，`
      + `${MA_TYPE} ${MA_FAST1}/${MA_SLOW1}+${MA_FAST2}/${MA_SLOW2}，分离≥${CROSS_MIN_PTS}点，`
      + `止损${STOP_PTS}/止盈${TP_PTS}点，互斥 ${MUTEX_SCOPE}`);

    // 依赖：画笔落盘（chan-bi 产出）
    const bisFile = cacheFile("bis", SYMBOL);
    if (!fs.existsSync(bisFile)) {
      console.log(`ERROR: 未找到笔数据文件 ${bisFile}，请先运行「画笔」（chan-bi）。`);
      process.exit(1);
    }
    const bisData = JSON.parse(fs.readFileSync(bisFile, "utf8"));
    if (bisData && bisData.analysisInvalidReason) throw new Error(bisData.analysisInvalidReason);
    if (bisData.symbol !== SYMBOL) {
      console.log(`ERROR: 笔数据属于 ${bisData.symbol}，与当前品种 ${SYMBOL} 不一致`);
      process.exit(1);
    }
    const periodBis = bisData.periods || {};
    for (const P of ENTRY_RES) {
      if (!(periodBis[P] || []).length) {
        console.log(`ERROR: 笔数据缺少周期 ${P}（30S 需画笔开启 30 秒级别）`);
        process.exit(1);
      }
    }

    const ensureResolution = async (targetRes) => {
      await client.Runtime.evaluate({
        expression: `TradingViewApi.activeChart().setResolution(${JSON.stringify(targetRes)});`,
        returnByValue: true, awaitPromise: true, timeout: 10000,
      });
      let lastLen = 0;
      for (let i = 0; i < 40; i++) {
        await sleep(500);
        const r = await client.Runtime.evaluate({
          expression: `(function() {
            const chart = TradingViewApi.activeChart();
            const items = chart.chartModel().mainSeries().data().m_bars._items;
            return { res: String(chart.resolution()), len: items ? items.length : 0 };
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 10000,
        });
        const v = r.result.value;
        if (v.res === targetRes && v.len > 0) { if (v.len === lastLen) break; lastLen = v.len; }
      }
    };

    const FROM_TS = Math.floor(new Date(`${FROM_DATE}T00:00:00Z`).getTime() / 1000);
    // 取数窗口：30S 只取最近 3 天（秒级历史极浅），分钟级取完整窗口
    const WINDOW_DAYS = { "30S": 3 };
    const fetchBars = async (fromTs, buffer, windowDays) => {
      const tolerance = 6 * 3600;
      for (let attempt = 0; attempt < 60; attempt++) {
        const dataRes = await client.Runtime.evaluate({
          expression: `(function() {
            const chart = TradingViewApi.activeChart();
            const items = chart.chartModel().mainSeries().data().m_bars._items;
            if (!items || items.length === 0) return { error: 'no_items' };
            const bars = items.map(i => ({ time: i.value[0], open: i.value[1], high: i.value[2], low: i.value[3], close: i.value[4] }));
            const fromTs = ${JSON.stringify(fromTs)};
            const windowDays = ${JSON.stringify(windowDays || null)};
            const tolerance = ${JSON.stringify(tolerance)};
            const latestTs = bars.length ? bars[bars.length - 1].time : 0;
            const effFrom = (fromTs && windowDays) ? Math.max(fromTs, latestTs - windowDays * 86400) : fromTs;
            if (bars[0].time > effFrom + tolerance) return { bars: [], notCovered: true, resolution: String(chart.resolution()) };
            const fromIdx = bars.findIndex(k => k.time >= effFrom);
            const start = Math.max(0, fromIdx - ${buffer || 0});
            return { bars: bars.slice(start), resolution: String(chart.resolution()) };
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 15000,
        });
        const d = dataRes.result.value;
        if (!d || d.error) { await sleep(1200); continue; }
        if (d.notCovered) {
          await client.Runtime.evaluate({
            expression: `(function() {
              const chart = TradingViewApi.activeChart();
              const widget = chart._chartWidget || (chart.chartModel && chart.chartModel()._chartWidget);
              const ts = widget && widget.model ? widget.model().timeScale() : chart.chartModel().timeScale();
              ts.scrollToFirstBar(); return 'ok';
            })()`,
            returnByValue: true, awaitPromise: true, timeout: 10000,
          });
          await sleep(2500);
          continue;
        }
        return d;
      }
      return null;
    };

    // 逐周期：取K线 → 扫描信号
    const barsByP = {};
    let allSignals = [];
    let drawRes = originalRes;
    for (const P of ENTRY_RES) {
      await ensureResolution(P);
      drawRes = P;
      const maWarm = Math.max(MA_SLOW1, MA_SLOW2) + 10;
      const d = await fetchBars(FROM_TS, maWarm > BAR_BUFFER ? maWarm : BAR_BUFFER, WINDOW_DAYS[P] || null);
      if (!d || !d.bars || !d.bars.length) {
        console.log(`[周期 ${P}] 未读到K线，跳过`);
        continue;
      }
      barsByP[P] = d.bars;
      const sigs = scanPeriodSignals(P, d.bars, periodBis, UPPER_OF[P]);
      console.log(`[周期 ${P}] ${d.bars.length} 根K线，信号 ${sigs.length} 个`
        + (WINDOW_DAYS[P] ? `（窗口最近 ${WINDOW_DAYS[P]} 天）` : ""));
      allSignals = allSignals.concat(sigs);
    }

    // 全局互斥 + 出场模拟
    allSignals = applyMutexAndSimulate(allSignals, barsByP);
    const filled = allSignals.filter(s => !s.suppressed);
    const suppressed = allSignals.filter(s => s.suppressed);
    console.log(`\n成交 ${filled.length} 笔（互斥过滤 ${suppressed.length}）：`
      + `止损 ${filled.filter(s => s.exitType === "stop").length} / `
      + `止盈 ${filled.filter(s => s.exitType === "takeProfit").length} / `
      + `仍持仓 ${filled.filter(s => s.state === "open").length}`);

    // 落盘（工作台结果缓存；回测/监控引擎结果以 py_chain 为准）
    const outFile = cacheFile("fxma", SYMBOL);
    fs.writeFileSync(outFile, JSON.stringify({
      symbol: SYMBOL, strategy: "fxma_v1", generatedAt: new Date().toISOString(),
      params: { entryRes: ENTRY_RES, pointClasses: [...POINT_CLASSES], maType: MA_TYPE,
                maFast1: MA_FAST1, maSlow1: MA_SLOW1, maFast2: MA_FAST2, maSlow2: MA_SLOW2,
                crossMinPts: CROSS_MIN_PTS, strongFxMinPts: STRONG_FX_MIN_PTS,
                pointValidBars: POINT_VALID_BARS, stopPts: STOP_PTS, tpPts: TP_PTS,
                sameBarPriority: SAME_BAR_PRIORITY, mutexScope: MUTEX_SCOPE, lots: LOTS },
      signals: allSignals,
    }), "utf8");
    console.log("已落盘:", outFile);

    if (DRY) { // --dry：不画图（分析/测试用）
      await ensureResolution(originalRes);
      await client.close();
      return;
    }

    // 绘图：清除旧标记 → 画进场箭头 + 出场标记（只本周期显示）
    const clearTitle = async (TITLE) => {
      const r = await client.Runtime.evaluate({
        expression: `(function() {
          const chart = TradingViewApi.activeChart();
          const out = { cleared: 0 };
          const readTitle = (id) => {
            try {
              const sh = chart.getShapeById(id);
              const props = sh && sh._source && sh._source._properties;
              return props && props.title ? String(props.title._value) : '';
            } catch(e) { return ''; }
          };
          try {
            for (const s of chart.getAllShapes()) {
              if (readTitle(s.id) === "${TITLE}") {
                try { chart.removeEntity(s.id); out.cleared++; } catch(e) {}
              }
            }
          } catch(e) {}
          return out;
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };
    const drawShapes = async (TITLE, IV, items) => {
      const r = await client.Runtime.evaluate({
        expression: `(async function() {
          const chart = TradingViewApi.activeChart();
          const ITEMS = ${JSON.stringify(items)};
          const IV_CFG = ${JSON.stringify(IV)};
          const out = { ok: 0, err: [] };
          const applyIV = (id) => {
            if (!IV_CFG) return;
            try {
              const iv = chart.getShapeById(id)._source._properties.intervalsVisibilities;
              for (const [k, v] of Object.entries(IV_CFG)) { try { iv[k].setValue(v); } catch(e) {} }
            } catch(e) {}
          };
          for (const it of ITEMS) {
            try {
              const id = await chart.createMultipointShape(
                [{ time: it.time, price: it.price }],
                { shape: it.shape, lock: false, text: it.text || "",
                  overrides: { color: it.color, arrowColor: it.color, textColor: it.color, title: "${TITLE}" } });
              applyIV(id);
              out.ok++;
            } catch(err) { out.err.push(err.message); }
          }
          return out;
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };

    const EXIT_LABEL = { stop: "止损", takeProfit: "止盈" };
    for (const P of ENTRY_RES) {
      if (P !== drawRes) { await ensureResolution(P); drawRes = P; }
      await clearTitle(`ENTRY_FX_${P}`);
      await clearTitle(`EXIT_FX_${P}`);
      const entries = filled.filter(s => s.periodX === P);
      if (!entries.length) continue;
      const entryItems = entries.map(s => ({
        time: s.entryTime, price: s.entryPrice,
        shape: s.direction === "long" ? "arrow_up" : "arrow_down",
        color: s.direction === "long" ? BUY_COLOR : SELL_COLOR,
        text: s.strategyKey,
      }));
      const exitItems = [];
      for (const s of entries) {
        if (!s.exitType) continue; // 仍持仓不画出场
        exitItems.push({
          time: s.exitTime, price: s.exitPrice,
          shape: s.direction === "long" ? "arrow_down" : "arrow_up",
          color: EXIT_COLOR, text: EXIT_LABEL[s.exitType] || s.exitType,
        });
      }
      const r1 = await drawShapes(`ENTRY_FX_${P}`, onlyThisInterval(P), entryItems);
      const r2 = exitItems.length ? await drawShapes(`EXIT_FX_${P}`, onlyThisInterval(P), exitItems) : { ok: 0 };
      console.log(`[周期 ${P}] 进场箭头 ${r1.ok} 个，出场标记 ${r2.ok} 个`);
    }
    await ensureResolution(originalRes);
    console.log("\n已切回原周期:", originalRes);
    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
    process.exitCode = 1;
  }
}

module.exports = { maSeries, strongFxAfter, scanPeriodSignals, applyMutexAndSimulate,
                   POINT_CLASS, UPPER_OF };

if (require.main === module) {
  main();
}
