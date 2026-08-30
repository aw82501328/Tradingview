/**
 * 进出场标记脚本（独立 SKILL：mark-entry）
 * 读取 chan-bi（画笔）落盘的笔数据 与 mark-sr-flip（支阻互换位）落盘的支阻位数据，
 * 当价格靠近支阻位、且任一周期（3分钟/15分钟/1小时/4小时）出现缠论背驰时，
 * 在该周期标记进场黄色箭头：
 *   - 底背驰（下跌笔创新低 + MACD 背驰）→ 做多 → 向上箭头（arrow_up）
 *   - 顶背驰（上涨笔创新高 + MACD 背驰）→ 做空 → 向下箭头（arrow_down）
 *
 * 用法：
 *   node mark_entry.js --from=2026-06-30            计算并绘制（默认 240,60,15,3）
 *   node mark_entry.js --dry --from=2026-06-30      只计算打印，不绘图
 *
 * 参数：
 *   --from=YYYY-MM-DD   起始日期（应与画笔/支阻位一致）
 *   --periods=...       检测周期（逗号分隔，默认 240,60,15,3）
 *   --near=K            靠近支阻位阈值（×当前周期ATR，默认 1.0）
 *   --dry               只计算不绘图
 *   --debug             打印调试信息
 */
const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");
// 缠论算法核心（复用背驰判定、ATR、MACD 等工具函数）
const core = require("../../chan-core/scripts/chan_core.js");
const { calcATR, calcMACD, isBiDiverge, fmtT, lowerResOf } = core;

// 缓存目录（chan-bi 笔数据、mark-sr-flip 支阻位数据、本脚本落盘进出场数据）
const CACHE_DIR = path.join(__dirname, "..", "..", "..", "..", ".cursor", "cache");
const cacheFile = (prefix, symbol) => path.join(CACHE_DIR, `${prefix}_${String(symbol).replace(/[^A-Za-z0-9_.-]/g, "_")}.json`);

// 解析命令行参数
const args = process.argv.slice(2);
const DRY = args.includes("--dry");
const DEBUG = args.includes("--debug");
const getArg = (name, def) => {
  const a = args.find(x => x.startsWith("--" + name + "="));
  return a ? parseFloat(a.split("=")[1]) : def;
};
const getStrArg = (name, def) => {
  const a = args.find(x => x.startsWith("--" + name + "="));
  return a ? a.split("=")[1] : def;
};
// 靠近支阻位阈值（×当前周期ATR）
const NEAR_ATR = Math.max(parseFloat(getArg("near", 1.0)) || 1.0, 0.01);
const FROM_DATE = getStrArg("from", "");
if (!FROM_DATE) {
  console.log("错误: 必须指定起始日期 --from=YYYY-MM-DD");
  process.exit(1);
}
let FROM_TS = null;
{
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(FROM_DATE.trim());
  if (m) FROM_TS = Math.floor(Date.UTC(+m[1], +m[2] - 1, +m[3]) / 1000);
  else console.log("警告: --from 日期格式应为 YYYY-MM-DD，忽略该参数");
}
// 检测周期列表（默认 4小时/1小时/15分钟/3分钟，从大到小）
const PERIODS = getStrArg("periods", "240,60,15,3")
  .split(",").map(s => s.trim()).filter(Boolean);

// 黄色箭头颜色
const ARROW_COLOR = "#FFD700";
// 读取K线时，起始日期前额外取的缓冲根数
const BAR_BUFFER = 30;

// ============================================================
// 可见性配置（只在本周期显示）
// ============================================================

/**
 * 箭头只在该周期显示（与买卖点一致，不跨周期）。
 */
function onlyThisInterval(res) {
  const s = String(res).toUpperCase();
  const NONE = {
    ticks: false,
    seconds: false, secondsFrom: 1, secondsTo: 59,
    minutes: false, minutesFrom: 1, minutesTo: 59,
    hours: false, hoursFrom: 1, hoursTo: 24,
    days: false, daysFrom: 1, daysTo: 366,
    weeks: false, weeksFrom: 1, weeksTo: 52,
    months: false, monthsFrom: 1, monthsTo: 12,
  };
  switch (s) {
    case "3":    return { ...NONE, minutes: true, minutesFrom: 3, minutesTo: 3 };
    case "15":   return { ...NONE, minutes: true, minutesFrom: 15, minutesTo: 15 };
    case "60":
    case "1H":   return { ...NONE, hours: true, hoursFrom: 1, hoursTo: 1 };
    case "240":
    case "4H":   return { ...NONE, hours: true, hoursFrom: 4, hoursTo: 4 };
    case "1D":
    case "D":    return { ...NONE, days: true, daysFrom: 1, daysTo: 1 };
    default:     return { ...NONE, minutes: true, minutesFrom: 1, minutesTo: 59 };
  }
}

// ============================================================
// 背驰识别算法（纯函数，复用 chan-core.isBiDiverge）
// ============================================================

/**
 * 识别某周期的背驰点（做多=底背驰，做空=顶背驰）。
 * 参考 chan-core.findBuyPoints/findSellPoints 的候选逻辑，但不做区间套/锚定：
 *   - 底背驰：下跌笔创新低 + MACD 背驰（绿柱面积变小 或 DIF低点抬高）
 *   - 顶背驰：上涨笔创新高 + MACD 背驰（红柱面积变小 或 DIF高点变低）
 * 参照笔 = 向前最近同向笔（跳过幅度 < 当前 50% 的次级别回调）。
 * @param {Array} bis 某周期笔列表
 * @param {Array} macdArr 该周期 MACD 数组
 * @returns {Array} [{ time, price, direction }] direction='long'（做多）|'short'（做空）
 */
function findDivergePoints(bis, macdArr) {
  if (!bis || bis.length < 3) return [];
  const points = [];

  // 做多（底背驰）：下跌笔创新低 + 背驰
  const downIdx = [];
  bis.forEach((b, i) => { if (b.type === "down") downIdx.push(i); });
  for (let k = 1; k < downIdx.length; k++) {
    const cur = bis[downIdx[k]];
    let refer = null;
    for (let j = k - 1; j >= 0; j--) {
      const cand = bis[downIdx[j]];
      if (cand.span < cur.span * 0.5) continue; // 跳过幅度不足的次级别回调
      refer = cand;
      break;
    }
    if (refer && cur.endPrice < refer.endPrice && isBiDiverge(cur, refer, macdArr)) {
      points.push({ time: cur.endTime, price: cur.endPrice, direction: "long" });
    }
  }

  // 做空（顶背驰）：上涨笔创新高 + 背驰
  const upIdx = [];
  bis.forEach((b, i) => { if (b.type === "up") upIdx.push(i); });
  for (let k = 1; k < upIdx.length; k++) {
    const cur = bis[upIdx[k]];
    let refer = null;
    for (let j = k - 1; j >= 0; j--) {
      const cand = bis[upIdx[j]];
      if (cand.span < cur.span * 0.5) continue;
      refer = cand;
      break;
    }
    if (refer && cur.endPrice > refer.endPrice && isBiDiverge(cur, refer, macdArr)) {
      points.push({ time: cur.endTime, price: cur.endPrice, direction: "short" });
    }
  }

  return points;
}

// ============================================================
// 主流程
// ============================================================

(async () => {
  let client;
  try {
    const targets = await CDP.List({ port: 9222 });
    const pg = targets.find(t => t.type === "page" && t.url.includes("tradingview.com"));
    if (!pg) { console.log("ERROR: 未找到 TradingView 页面"); process.exit(1); }
    client = await CDP({ target: pg.id, port: 9222 });
    await client.Page.enable();
    await client.Runtime.enable();

    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    // 读取当前品种与周期
    const curRes = await client.Runtime.evaluate({
      expression: `(function() {
        const chart = TradingViewApi.activeChart();
        return { symbol: chart.symbol(), resolution: String(chart.resolution()) };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    const curVal = curRes.result.value;
    const SYMBOL = curVal.symbol;
    const originalRes = curVal.resolution;
    console.log("品种:", SYMBOL, "当前周期:", originalRes);
    console.log("将检测周期:", PERIODS.join(", "));

    // 读取 chan-bi 画笔落盘的笔数据（强制依赖）
    const bisFile = cacheFile("bis", SYMBOL);
    if (!fs.existsSync(bisFile)) {
      console.log(`ERROR: 未找到笔数据文件 ${bisFile}`);
      console.log("请先对当前品种运行「画笔」SKILL（chan-bi）生成笔数据，再运行本脚本。");
      process.exit(1);
    }
    const bisData = JSON.parse(fs.readFileSync(bisFile, "utf8"));
    if (bisData.symbol !== SYMBOL) {
      console.log(`ERROR: 笔数据文件属于 ${bisData.symbol}，与当前品种 ${SYMBOL} 不一致`);
      console.log("请先对当前品种运行「画笔」SKILL（chan-bi）生成笔数据，再运行本脚本。");
      process.exit(1);
    }
    const periodBis = bisData.periods || {};

    // 读取 mark-sr-flip 落盘的支阻位数据（强制依赖）
    const srFile = cacheFile("srflip", SYMBOL);
    if (!fs.existsSync(srFile)) {
      console.log(`ERROR: 未找到支阻位数据文件 ${srFile}`);
      console.log("请先对当前品种运行「支阻互换位」SKILL（mark-sr-flip）生成支阻位数据，再运行本脚本。");
      process.exit(1);
    }
    const srData = JSON.parse(fs.readFileSync(srFile, "utf8"));
    const srLevels = (srData.merged && srData.merged.length > 0) ? srData.merged : [];
    if (srLevels.length === 0) {
      console.log("ERROR: 支阻位数据为空（merged 列表无数据），请先运行「支阻互换位」SKILL 生成支阻位。");
      process.exit(1);
    }
    console.log(`已读取笔数据: ${bisFile}（${Object.keys(periodBis).length} 个周期）`);
    console.log(`已读取支阻位: ${srFile}（${srLevels.length} 个支阻位）`);

    // 切换到指定周期并等待K线加载完成
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
        if (v.res === targetRes && v.len > 0) {
          if (v.len === lastLen) break;
          lastLen = v.len;
        }
      }
    };

    // 读取K线（从起始日期开始，含缓冲；数据未覆盖时自动加载完整历史）
    const fetchBars = async (fromTs, buffer) => {
      const tolerance = 6 * 3600;
      for (let attempt = 0; attempt < 60; attempt++) {
        const dataRes = await client.Runtime.evaluate({
          expression: `(function() {
            const chart = TradingViewApi.activeChart();
            const ms = chart.chartModel().mainSeries();
            const items = ms.data().m_bars._items;
            if (!items || items.length === 0) return { error: 'no_items' };
            const bars = items.map(i => ({
              time: i.value[0], open: i.value[1], high: i.value[2], low: i.value[3], close: i.value[4], volume: i.value[5]
            }));
            const fromTs = ${JSON.stringify(fromTs)};
            const tolerance = ${JSON.stringify(tolerance)};
            if (bars[0].time > fromTs + tolerance) {
              return { bars: [], notCovered: true, len: bars.length, resolution: String(chart.resolution()) };
            }
            const fromIdx = bars.findIndex(k => k.time >= fromTs);
            const buf = ${buffer || 0};
            const start = Math.max(0, fromIdx - buf);
            return { bars: bars.slice(start), len: bars.length, resolution: String(chart.resolution()) };
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
              ts.scrollToFirstBar();
              return 'ok';
            })()`,
            returnByValue: true, awaitPromise: true, timeout: 10000,
          });
          if (DEBUG) console.log(`[fetchBars ${d.resolution}] 数据未覆盖起始日期，正在加载完整历史...`);
          await sleep(2500);
          continue;
        }
        return d;
      }
      return null;
    };

    const toT = (ts) => {
      const dt = new Date(ts * 1000);
      const p = (n) => String(n).padStart(2, '0');
      return `${dt.getMonth() + 1}-${dt.getDate()} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
    };

    /**
     * 绘制前确保图表数据覆盖到最早信号的时间：
     * 切换周期后图表可能只加载最近N根K线，较早的信号时间会被 TradingView 吸附到数据边缘
     * （表现为所有箭头 time 堆在最新 bar 上、位置错乱）。绘制前检查第一根K线是否覆盖，
     * 未覆盖则 scrollToFirstBar 加载完整历史，直到首根K线时间真正早于/等于最早信号时间。
     *
     * 历史教训：不能靠「len/first 连续多次不变」提前判定加载完成——TradingView 的历史数据是
     * 分批异步加载的，加载中途会短暂暂停（len/first 看似稳定），此时提前退出会导致早期信号
     * 全部被吸附到「已加载数据边缘」，15/60 分钟箭头叠成一团。必须等 first 真正 <= minTs，
     * 仅在长时间完全无进展时兜底放弃。
     */
    const ensureBarsCover = async (res, minTs) => {
      const readFirst = async () => {
        const r = await client.Runtime.evaluate({
          expression: `(function() {
            const chart = TradingViewApi.activeChart();
            const items = chart.chartModel().mainSeries().data().m_bars._items;
            if (!items || items.length === 0) return { first: null, len: 0 };
            return { first: items[0].value[0], len: items.length };
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 10000,
        });
        return r.result.value || { first: null, len: 0 };
      };
      const scrollToFirst = async () => {
        await client.Runtime.evaluate({
          expression: `(function() {
            const chart = TradingViewApi.activeChart();
            const widget = chart._chartWidget || (chart.chartModel && chart.chartModel()._chartWidget);
            const ts = widget && widget.model ? widget.model().timeScale() : chart.chartModel().timeScale();
            ts.scrollToFirstBar();
            return 'ok';
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 10000,
        });
      };
      const cur = await readFirst();
      if (cur.first !== null && cur.first <= minTs) return;
      if (DEBUG) console.log(`[数据覆盖] ${res} 首根K线 ${toT(cur.first)} 晚于最早信号 ${toT(minTs)}，加载完整历史...`);
      await scrollToFirst();
      let prevLen = cur.len;
      let prevFirst = cur.first;
      let noProgress = 0;
      for (let i = 0; i < 300; i++) {
        await sleep(1200);
        const c = await readFirst();
        if (DEBUG) console.log(`  [加载 ${res}] i=${i} len=${c.len} first=${c.first ? toT(c.first) : null}${c.first !== null && c.first <= minTs ? " [已覆盖]" : ""}`);
        if (c.first !== null && c.first <= minTs) break;
        // 周期性重新触发滚动到最左：加载过程中可视范围可能被回弹到实时，导致数据停止前进
        if (i > 0 && i % 15 === 0) {
          if (DEBUG) console.log("  -> 重新 scrollToFirstBar 继续加载");
          await scrollToFirst();
        }
        if (c.len === prevLen && c.len > 0 && c.first === prevFirst) {
          noProgress++;
          // 连续 30 次（约 36 秒）无任何进展，才认定已加载到该周期数据的最早期限（兜底，避免死等）
          if (noProgress >= 30) break;
        } else {
          noProgress = 0;
        }
        prevLen = c.len;
        prevFirst = c.first;
      }
      const finalFirst = await readFirst();
      if (DEBUG || finalFirst.first === null || finalFirst.first > minTs) {
        console.log(`[数据覆盖] ${res} 加载结束：首根K线 ${finalFirst.first ? toT(finalFirst.first) : 'null'}，最早信号 ${toT(minTs)}${finalFirst.first !== null && finalFirst.first <= minTs ? "（已覆盖）" : "（未覆盖，早期信号可能被吸附）"}`);
      }
      // 恢复可视范围到实时
      await client.Runtime.evaluate({
        expression: `(function() {
          const chart = TradingViewApi.activeChart();
          const ts = chart.chartModel().timeScale();
          if (ts.scrollToRealtime) ts.scrollToRealtime();
          else ts.scrollToBar(chart.chartModel().mainSeries().data().m_bars._items.length - 1);
          return 'ok';
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 10000,
      });
    };

    // ============================================================
    // 逐周期识别背驰点 + 靠近支阻位判定
    // ============================================================
    const allEntries = {};
    let currentRes = originalRes;
    for (const res of PERIODS) {
      const bis = periodBis[res];
      if (!bis || bis.length < 3) {
        console.log(`\n[周期 ${res}] 笔数据不足（${bis ? bis.length : 0} 笔），跳过`);
        continue;
      }
      if (res !== currentRes) {
        await ensureResolution(res);
        currentRes = res;
      }
      const d = await fetchBars(FROM_TS, BAR_BUFFER);
      if (!d || !d.bars || d.bars.length === 0) {
        console.log(`\n[周期 ${res}] 读取K线失败，跳过`);
        continue;
      }
      const bars = d.bars;
      const atr = calcATR(bars, 14);
      const nearTol = NEAR_ATR * atr;
      const macdArr = calcMACD(bars);
      if (DEBUG) console.log(`\n[周期 ${res}] K线 ${bars.length} 根 ATR=${atr.toFixed(2)} 靠近阈值=${nearTol.toFixed(2)}`);

      // 识别背驰点
      const divergePoints = findDivergePoints(bis, macdArr);
      // 靠近支阻位判定
      const entries = [];
      for (const p of divergePoints) {
        const near = srLevels.find(sr => Math.abs(sr.price - p.price) <= nearTol);
        if (near) {
          entries.push({ time: p.time, price: p.price, direction: p.direction, nearSr: near.price });
        }
      }
      allEntries[res] = entries;
      console.log(`\n=== 进出场标记 [周期 ${res}]（背驰 ${divergePoints.length} 个，靠近支阻位 ${entries.length} 个，阈值 ${nearTol.toFixed(2)}）===`);
      if (entries.length === 0) {
        console.log("无进出场信号");
      } else {
        entries.forEach((e, idx) => {
          const dirName = e.direction === "long" ? "做多(向上)" : "做空(向下)";
          console.log(`信号${idx + 1}: ${toT(e.time)} @ ${e.price.toFixed(2)} [${dirName}] 靠近支阻位 ${e.nearSr.toFixed(2)}`);
        });
      }
    }

    // 进出场数据落盘
    try {
      fs.mkdirSync(CACHE_DIR, { recursive: true });
      const entryFile = cacheFile("entry", SYMBOL);
      const payload = {
        symbol: SYMBOL,
        from: FROM_DATE || null,
        fromTs: FROM_TS,
        generatedAt: new Date().toISOString(),
        nearAtr: NEAR_ATR,
        periods: allEntries,
      };
      fs.writeFileSync(entryFile, JSON.stringify(payload, null, 2), "utf8");
      const total = Object.values(allEntries).reduce((s, a) => s + a.length, 0);
      console.log(`\n进出场数据已落盘: ${entryFile}（${Object.keys(allEntries).length} 个周期，共 ${total} 个信号）`);
    } catch (e) {
      console.log("警告: 进出场数据落盘失败:", e.message);
    }

    if (DRY) {
      console.log("\n[DRY RUN] 不绘图。");
      await client.close();
      return;
    }

    // ============================================================
    // 绘制阶段：先清除旧箭头，再绘制新箭头（黄色，只本周期显示）
    // ============================================================

    /**
     * 清除某周期的旧进出场箭头（title = ENTRY_<周期>）。
     */
    const clearEntry = async (res) => {
      const TITLE = "ENTRY_" + res;
      const r = await client.Runtime.evaluate({
        expression: `(function() {
          const chart = TradingViewApi.activeChart();
          const TITLE = "${TITLE}";
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
              const t = readTitle(s.id);
              if (t === TITLE) {
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

    /**
     * 在某周期绘制进出场箭头（黄色，做多=arrow_up，做空=arrow_down，只本周期显示）。
     */
    const createEntry = async (res, entries) => {
      const TITLE = "ENTRY_" + res;
      const IV_CFG = onlyThisInterval(res);
      const r = await client.Runtime.evaluate({
        expression: `(async function() {
          const chart = TradingViewApi.activeChart();
          const ENTRIES = ${JSON.stringify(entries)};
          const TITLE = "${TITLE}";
          const ARROW_COLOR = "${ARROW_COLOR}";
          const IV_CFG = ${JSON.stringify(IV_CFG)};
          const out = { ok: 0, err: [] };
          const created = [];

          const applyIV = (id) => {
            if (!IV_CFG) return;
            try {
              const iv = chart.getShapeById(id)._source._properties.intervalsVisibilities;
              iv.ticks.setValue(IV_CFG.ticks);
              iv.seconds.setValue(IV_CFG.seconds);
              iv.secondsFrom.setValue(IV_CFG.secondsFrom);
              iv.secondsTo.setValue(IV_CFG.secondsTo);
              iv.minutes.setValue(IV_CFG.minutes);
              iv.minutesFrom.setValue(IV_CFG.minutesFrom);
              iv.minutesTo.setValue(IV_CFG.minutesTo);
              iv.hours.setValue(IV_CFG.hours);
              iv.hoursFrom.setValue(IV_CFG.hoursFrom);
              iv.hoursTo.setValue(IV_CFG.hoursTo);
              iv.days.setValue(IV_CFG.days);
              iv.daysFrom.setValue(IV_CFG.daysFrom);
              iv.daysTo.setValue(IV_CFG.daysTo);
              iv.weeks.setValue(IV_CFG.weeks);
              iv.weeksFrom.setValue(IV_CFG.weeksFrom);
              iv.weeksTo.setValue(IV_CFG.weeksTo);
              iv.months.setValue(IV_CFG.months);
              iv.monthsFrom.setValue(IV_CFG.monthsFrom);
              iv.monthsTo.setValue(IV_CFG.monthsTo);
              iv.ranges.setValue(false);
            } catch(e) {}
          };

          for (const e of ENTRIES) {
            try {
              const shape = e.direction === 'long' ? 'arrow_up' : 'arrow_down';
              const id = await chart.createMultipointShape(
                [{ time: e.time, price: e.price }],
                { shape: shape, lock: false, overrides: { color: ARROW_COLOR, arrowColor: ARROW_COLOR, title: TITLE } }
              );
              applyIV(id);
              created.push(id);
              out.ok++;
            } catch(err) { out.err.push(err.message); }
          }
          // 创建后立即读回（调试用，确认创建时刻的锚点位置）
          const verify = [];
          for (const id of created) {
            try {
              const sh = chart.getShapeById(id);
              verify.push(sh.getPoints().map(p => ({ time: p.time, price: p.price })));
            } catch(e) { verify.push([{ err: e.message }]); }
          }
          return { ...out, created_ids: created, verify };
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };

    // 逐周期清除旧箭头并绘制新箭头
    let drawCurrentRes = originalRes;
    // 清除阶段：遍历所有检测周期，无论本次是否识别出信号，都先清除该周期的旧箭头，
    // 避免「某周期本次无信号 / 笔数据不足」时旧箭头残留（旧箭头不会因本次无信号而自动消失）。
    for (const res of PERIODS) {
      if (res !== drawCurrentRes) {
        await ensureResolution(res);
        drawCurrentRes = res;
      }
      const cleared = await clearEntry(res);
      console.log(`\n[周期 ${res}] 已清除旧进出场箭头: ${cleared.cleared} 个`);
    }
    console.log("\n=== 绘制阶段（黄色箭头）===");
    for (const res of PERIODS) {
      const entries = allEntries[res];
      if (!entries || entries.length === 0) continue;
      // 箭头创建在「低一级」周期：低一级周期 bar 边界更细，锚点时间（笔端点已校准到
      // 低一级边界）在其上精确定位；其中 3 分钟是最小周期，锚点在 3 分钟读取始终
      // 返回原始时间（即使数据未覆盖），是天然稳定锚定周期。
      const drawRes = lowerResOf(res) || res;
      if (drawRes !== drawCurrentRes) {
        await ensureResolution(drawRes);
        drawCurrentRes = drawRes;
      }
      // 绘制前确保图表数据覆盖最早信号时间（避免箭头被吸附到数据边缘）
      const minTs = entries.reduce((m, e) => Math.min(m, e.time), Infinity);
      if (minTs !== Infinity) await ensureBarsCover(drawRes, minTs);
      const result = await createEntry(res, entries);
      console.log(`\n=== 绘制结果 [周期 ${res}]（创建于 ${drawRes}）===`);
      console.log(JSON.stringify(result, null, 2));
    }

    // 最后切回原周期并恢复完整历史：切周期会让当前周期数据重置为「默认加载」
    // （最近若干根K线），早期箭头会被吸附到数据边缘。切回原周期后重新加载完整
    // 历史，确保原周期的早期箭头锚点正确。
    if (originalRes !== drawCurrentRes) {
      await ensureResolution(originalRes);
      drawCurrentRes = originalRes;
    }
    const origEntries = allEntries[originalRes];
    if (origEntries && origEntries.length > 0) {
      const minTs = origEntries.reduce((m, e) => Math.min(m, e.time), Infinity);
      if (minTs !== Infinity) await ensureBarsCover(originalRes, minTs);
    }
    console.log("\n已切回原周期:", originalRes);

    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
  }
})();
