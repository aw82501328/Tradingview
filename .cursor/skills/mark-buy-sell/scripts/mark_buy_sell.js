/**
 * 缠论买卖点标记脚本
 * 基于各周期上画的笔，按照缠论定义标记各周期的 1、2、3 类买卖点（红色文字，只在本周期显示）。
 *
 * 用法：
 *   node mark_buy_sell.js --from=2026-07-02
 *   node mark_buy_sell.js --dry --from=2026-07-02    只计算并打印，不绘图
 *
 * 参数：
 *   --from=YYYY-MM-DD   必填：起始日期，所有周期都从该日期开始计算笔和买卖点
 *   --periods=...       要标记的周期列表（逗号分隔，默认 D,240,60,15,3）
 *   --dry               只计算不绘图
 *   --debug             打印锚定过程、标记列表等调试信息
 *   --nearp=0.3         1买与2买（1卖与2卖）价差阈值（ATR 倍数），价差不超过该值视为很近并合并标注「真1买/真1卖」
 */
const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");
// 缠论算法核心（唯一算法源，与 chan-bi SKILL 共用）
const core = require("../../chan-core/scripts/chan_core.js");
const {
  mergeBars, findFractals, countRaw, hasGapBetween, buildBi,
  calcATR, calcMACD, hasMacdCrossBetween,
  extendLastBi, lowerResOf, calibrateBiTimes, intervalSecOf,
  fmtT, biMacdMetrics, isBiDiverge,
  findBuyPoints, findSellPoints, anchorFirstBuy, anchorFirstSell,
  isSameAsUpperBi, snapToOwnBar, keepRecentEach,
} = core;

// 笔数据缓存目录：由 chan-bi 画笔 SKILL 落盘，本脚本强制读取（画笔 → 标记 数据依赖）
const CACHE_DIR = path.join(__dirname, "..", "..", "..", "..", ".cursor", "cache");
// 品种名中的特殊字符替换为下划线，保证文件名合法（与 chan-bi 落盘规则一致）
const bisCacheFile = (symbol) => path.join(CACHE_DIR, `bis_${String(symbol).replace(/[^A-Za-z0-9_.-]/g, "_")}.json`);

// 解析命令行参数
const args = process.argv.slice(2);
const DRY = args.includes("--dry");
const DEBUG = args.includes("--debug");
core.CHAN_CFG.debug = DEBUG;
const getArg = (name, def) => {
  const a = args.find(x => x.startsWith("--" + name + "="));
  return a ? parseFloat(a.split("=")[1]) : def;
};
const getStrArg = (name, def) => {
  const a = args.find(x => x.startsWith("--" + name + "="));
  return a ? a.split("=")[1] : def;
};
const ATR_FILTER = getArg("atr", 0.5);
const GAP_FILTER = getArg("gap", 1.0);
core.CHAN_CFG.gapFilter = GAP_FILTER;
// 1类 与 2类 邻近判定：1买/1卖 与 2买/2卖 的「K线最低/最高价差」不超过 nearp×ATR（至少 0.05）时
// 视为「很近」（不限制时间差），直接把 1类 合并到 2类 标记上标注为「真1买/真1卖」（见 mergeNearFirstSecond）
const NEAR_ATR_RATIO = Math.max(parseFloat(getArg("nearp", 0.3)) || 0.3, 0);
const FROM_DATE = getStrArg("from", "");
if (!FROM_DATE) {
  console.log("错误: 必须指定起始日期 --from=YYYY-MM-DD");
  process.exit(1);
}
let FROM_TS = null;
const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(FROM_DATE.trim());
if (m) {
  FROM_TS = Math.floor(Date.UTC(+m[1], +m[2] - 1, +m[3]) / 1000);
} else {
  console.log("警告: --from 日期格式应为 YYYY-MM-DD，忽略该参数");
}
const PERIODS = getStrArg("periods", "D,240,60,15,3")
  .split(",").map(s => s.trim()).filter(Boolean);
// 每类买卖点保留的个数（默认 1，即每类只保留时间上最近的一个，减少图上标记数量）
const KEEP = Math.max(1, parseInt(getStrArg("keep", "1"), 10) || 1);

const ANCHOR_BUFFER = 30;

// 小周期只加载最近 N 天的K线：3分钟最近15天、15分钟最近30天，
// 与画笔 chan-bi 的 DRAW_WINDOW_DAYS 保持一致——画笔只绘制窗口内笔，
// 本脚本加载K线只需覆盖窗口内买卖点即可，避免为覆盖起始日期加载数月完整历史而超时。
const DRAW_WINDOW_DAYS = { '3': 15, '15': 30 };

// ============================================================
// 买卖点显示配置
// ============================================================

/**
 * 买卖点只在该周期显示：intervalsVisibilities 精确到「仅本周期」。
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

/**
 * 新增：1类 与 2类 买卖点价格很接近时，直接把 1类 标记合并到最近的 2类 标记上，标注为「真1买/真1卖」。
 *
 * 缠论中 1买（MACD 背驰底，时间在前）与 2买（其后回调不破前低的确认买点，时间在后）若价格很近
 * （2买 对应K线最低价与 1买 的最低价之差不超过阈值，不限制时间差），说明回调极浅、底部确认
 * 非常强，该 1买 是可靠的「真1买」——直接在 2买 标记上新增「真1买」，避免图上两个紧邻标记。
 * 原 1买 标记移除（由「真1买」替代），2买 标记保留；一个 2类 标记最多承接一个 1类 标记。
 * 1卖/2卖 对称处理（合并为「真1卖」）。附近没有价格接近的 2类 标记的 1类 标记保持原名不变。
 *
 * @param {Array} points 买卖点列表 [{type,time,price}, ...]
 * @param {string} firstType  一买或一卖的类型名（"1买"/"1卖"）
 * @param {string} secondType 二买或二卖的类型名（"2买"/"2卖"）
 * @param {string} mergedType 合并后的类型名（"真1买"/"真1卖"）
 * @param {number} nearPrice  邻近判定阈值（价格）：1类与2类价格差不超过该值视为「很近」
 */
function mergeNearFirstSecond(points, firstType, secondType, mergedType, nearPrice) {
  const firsts = points.filter(p => p.type === firstType);
  if (firsts.length === 0) return points;
  const seconds = points.filter(p => p.type === secondType);
  if (seconds.length === 0) return points;
  const usedSecond = new Set();
  const out = [];
  for (const p of points) {
    if (p.type !== firstType) { out.push(p); continue; }
    // 找时间上在 1类 之后（确认点）、价格最接近、且尚未承接过的 2类 标记
    let best = null, bestDiff = Infinity;
    for (const s of seconds) {
      if (s.time < p.time) continue;
      if (usedSecond.has(s.time)) continue;
      const d = Math.abs(s.price - p.price);
      if (d < bestDiff) { bestDiff = d; best = s; }
    }
    if (best && bestDiff <= nearPrice) {
      usedSecond.add(best.time);
      out.push({ type: mergedType, time: best.time, price: best.price });
      if (DEBUG) console.log(`[邻近合并] ${firstType}@${fmtT(p.time)}(${p.price}) 与 ${secondType}@${fmtT(best.time)}(${best.price}) 价差 ${bestDiff.toFixed(3)}，合并为 ${mergedType}@${fmtT(best.time)}`);
    } else {
      out.push(p); // 附近无价格接近的 2类 标记，保留原 1类 标记
    }
  }
  return out;
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
    console.log("将标记周期:", PERIODS.join(", "), "起始日期:", FROM_DATE);

    // ============================================================
    // 强制依赖画笔数据：读取 chan-bi 画笔 SKILL 落盘的笔数据文件
    // 找不到文件 / 品种不匹配 → 报错退出（必须先运行画笔 SKILL）
    // ============================================================
    const bisCachePath = bisCacheFile(SYMBOL);
    let bisCache = null;
    try {
      if (fs.existsSync(bisCachePath)) {
        bisCache = JSON.parse(fs.readFileSync(bisCachePath, "utf8"));
      }
    } catch (e) {
      console.log("错误: 笔数据文件解析失败:", e.message);
    }
    if (!bisCache || !bisCache.periods || Object.keys(bisCache.periods).length === 0) {
      console.log(`错误: 未找到 ${SYMBOL} 的笔数据文件（${bisCachePath}）。`);
      console.log("本 SKILL 强制依赖 chan-bi 画笔 SKILL：请先运行「画笔」生成笔数据（会落盘到 .cursor/cache/bis_<symbol>.json），再运行「标记买卖点」。");
      await client.close();
      process.exit(1);
    }
    if (bisCache.symbol !== SYMBOL) {
      console.log(`错误: 笔数据文件属于 ${bisCache.symbol}，与当前品种 ${SYMBOL} 不一致（文件：${bisCachePath}）。`);
      console.log("请先对当前品种运行「画笔」SKILL，再运行「标记买卖点」。");
      await client.close();
      process.exit(1);
    }
    console.log(`已读取画笔笔数据: ${bisCachePath}（生成于 ${bisCache.generatedAt || "未知"}，周期: ${Object.keys(bisCache.periods).join(", ")}）`);

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

    const fetchBars = async (expectedIntervalSec, fromTs, buffer, windowDays) => {
      const tolerance = Math.max(expectedIntervalSec * 24, 6 * 3600);
      let lastD = null;
      for (let attempt = 0; attempt < 90; attempt++) {
        const dataRes = await client.Runtime.evaluate({
          expression: `(function() {
            const chart = TradingViewApi.activeChart();
            const ms = chart.chartModel().mainSeries();
            const items = ms.data().m_bars._items;
            if (!items || items.length === 0) return { error: 'no_items' };
            const bars = items.map(i => {
              const v = i.value;
              return { time: v[0], open: v[1], high: v[2], low: v[3], close: v[4], volume: v[5] };
            });
            const gaps = [];
            for (let i = bars.length - 1; i >= Math.max(1, bars.length - 20); i--) {
              gaps.push(bars[i].time - bars[i - 1].time);
            }
            gaps.sort((a, b) => a - b);
            const gap = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0;
            const fromTs = ${JSON.stringify(fromTs)};
            const windowDays = ${JSON.stringify(windowDays || null)};
            const tolerance = ${JSON.stringify(tolerance)};
            // 小周期窗口：覆盖目标从 fromTs 改为 max(fromTs, 最新K线时间 - windowDays*86400)，
            // 只需加载最近 N 天数据即可通过覆盖检查，避免为覆盖起始日期加载数月完整历史而超时
            const latestTs = bars.length ? bars[bars.length - 1].time : 0;
            const effFrom = (fromTs && windowDays) ? Math.max(fromTs, latestTs - windowDays * 86400) : fromTs;
            if (effFrom) {
              if (bars[0].time > effFrom + tolerance) {
                return { bars: [], resolution: String(chart.resolution()), gap, len: bars.length, notCovered: true };
              }
              const fromIdx = bars.findIndex(k => k.time >= effFrom);
              const buf = ${buffer || 0};
              const start = Math.max(0, fromIdx - buf);
              return { bars: bars.slice(start), resolution: String(chart.resolution()), gap, len: bars.length };
            }
            return { bars, resolution: String(chart.resolution()), gap, len: bars.length };
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 15000,
        });
        const d = dataRes.result.value;
        lastD = d;
        if (!d || d.error) return null;
        if (d.notCovered) {
          if (!d.scrolled) {
            await client.Runtime.evaluate({
              expression: `(function() {
                const chart = TradingViewApi.activeChart();
                const ts = chart.chartModel().timeScale();
                ts.scrollToFirstBar();
                return 'ok';
              })()`,
              returnByValue: true, awaitPromise: true, timeout: 10000,
            });
          }
          d.scrolled = true;
          await sleep(1200);
          continue;
        }
        if (d.gap && d.gap !== expectedIntervalSec && Math.abs(d.gap - expectedIntervalSec) > expectedIntervalSec * 0.3) {
          // 周期未切换成功，等待
          await sleep(800);
          continue;
        }
        return d;
      }
      console.log(`[fetchBars] ${expectedIntervalSec}s 取数超时，最后一次状态: ` + JSON.stringify(lastD || { error: "no_data" }));
      return null;
    };

    const ensureBarsCover = async (res, minTs) => {
      const readFirst = async () => {
        const r = await client.Runtime.evaluate({
          expression: `(function() {
            const chart = TradingViewApi.activeChart();
            const items = chart.chartModel().mainSeries().data().m_bars._items;
            return items && items.length > 0
              ? { first: items[0].value[0], len: items.length }
              : { first: null, len: 0 };
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 10000,
        });
        return r.result.value;
      };
      let cur = await readFirst();
      if (cur.first !== null && cur.first <= minTs) return;
      await client.Runtime.evaluate({
        expression: `(function() {
          const chart = TradingViewApi.activeChart();
          const ts = chart.chartModel().timeScale();
          ts.scrollToFirstBar();
          return 'ok';
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 10000,
      });
      let prevLen = cur.len;
      let prevFirst = cur.first;
      let stableCnt = 0;
      for (let i = 0; i < 200; i++) {
        await sleep(1200);
        cur = await readFirst();
        if (cur.first !== null && cur.first <= minTs) break; // 已覆盖
        // 数据长度稳定（不再增长）说明已加载到最早，接受现状；
        // 但必须「首根K线时间也连续多次不变」才算真正加载完——
        // scrollToFirstBar 触发加载后数据会分批推进，若只在 len 短暂稳定时
        // 就退出，加载可能只进行到中途，早期标记时间超出已加载数据范围，
        // 会被 TradingView 吸附到数据边缘导致标记堆叠（曾出现 8-4 20:00 处
        // 多个买卖点重叠）
        if (cur.len === prevLen && cur.len > 0 && i >= 3 && cur.first === prevFirst) {
          stableCnt++;
          if (stableCnt >= 3) break;
        } else {
          stableCnt = 0;
        }
        prevLen = cur.len;
        prevFirst = cur.first;
      }
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

    const toT = (ts) => {
      const dt = new Date(ts * 1000);
      const p = (n) => String(n).padStart(2, '0');
      return `${dt.getMonth()+1}-${dt.getDate()} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
    };

    // 清除某周期的旧买卖点标记（title = CHAN_BUYSELL_<周期>）
    const clearBuySell = async (res) => {
      const TITLE = "CHAN_BUYSELL_" + res;
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
          const shapes = chart.getAllShapes();
          for (const s of shapes) {
            if (readTitle(s.id) === TITLE) {
              try { chart.removeEntity(s.id); out.cleared++; } catch(e) {}
            }
          }
          return out;
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };

    // 在某周期绘制买卖点标记（text 红色文字，只在本周期显示）
    const drawMarks = async (res, marks) => {
      const TITLE = "CHAN_BUYSELL_" + res;
      const IV_CFG = onlyThisInterval(res);
      const r = await client.Runtime.evaluate({
        expression: `(async function() {
          const chart = TradingViewApi.activeChart();
          const MARKS = ${JSON.stringify(marks)};
          const TITLE = "${TITLE}";
          const IV_CFG = ${JSON.stringify(IV_CFG)};
          const out = { ok: 0, err: [] };
          const created = [];

          const applyIV = (id) => {
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

          for (const mk of MARKS) {
            try {
              const id = await chart.createMultipointShape(
                [{ time: mk.time, price: mk.price }],
                { shape: 'text', lock: false, overrides: {
                    text: mk.label, color: mk.color, bold: true,
                    title: TITLE
                  } }
              );
              applyIV(id);
              created.push(id);
              out.ok++;
            } catch(e) { out.err.push(e.message); }
          }
          return { ...out, created_ids: created };
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };

    // 标记前先清除所有周期的旧买卖点标记
    let clearedAll = 0;
    if (!DRY) {
      let currentRes = originalRes;
      for (const res of PERIODS) {
        if (res !== currentRes) {
          await ensureResolution(res);
          currentRes = res;
        }
        const cl = await clearBuySell(res);
        clearedAll += cl.cleared;
      }
      console.log("\n标记前清除旧买卖点:", clearedAll, "个");
    }

    // 主循环：从大到小逐周期计算并标记
    let currentRes = originalRes;
    let upperBis = null; // 上一级周期的笔（用于一买锚定）
    const periodMarks = {}; // 各周期已生成的标记（用于跨周期共振判定：上级买卖点 ↔ 本级别1类）
    for (let pi = 0; pi < PERIODS.length; pi++) {
      const res = PERIODS[pi];
      if (res !== currentRes) {
        await ensureResolution(res);
        currentRes = res;
      }

      let d = await fetchBars(intervalSecOf(res), FROM_TS, ANCHOR_BUFFER, DRAW_WINDOW_DAYS[res]);
      if (!d || d.error || !d.bars || d.bars.length === 0) {
        // 数据未加载好（切换周期时序问题），等待后重试一次
        console.log(`\n[周期 ${res}] 首次取数失败，等待重试...`);
        await sleep(3000);
        await ensureResolution(res);
        currentRes = res;
        d = await fetchBars(intervalSecOf(res), FROM_TS, ANCHOR_BUFFER, DRAW_WINDOW_DAYS[res]);
      }
      if (!d || d.error || !d.bars || d.bars.length === 0) {
        console.log(`\n[周期 ${res}] 无K线数据或切换失败，跳过`);
        continue;
      }

      // 本周期笔：强制从画笔 SKILL 落盘的笔数据读取（与图上所画笔完全一致）。
      // 不再本地计算（mergeBars/findFractals/buildBi/ATR过滤/extendLastBi/校准 均由 chan-bi 完成并落盘）
      let curBis = (bisCache.periods[res] || []);
      if (curBis.length === 0) {
        console.log(`\n[周期 ${res}] 笔数据为空（画笔未覆盖该周期），跳过`);
        continue;
      }
      // 过滤到起始日期之后的笔（与画笔一致；画笔只绘制结束时间 >= 起始日期的笔）
      curBis = curBis.filter(b => b.endTime >= FROM_TS);

      // 仍需要本周期K线：用于 ATR 偏移量、买卖点吸附到本周期bar边界、MACD 背驰判定
      const rawBars = d.bars;
      const atr = calcATR(rawBars, 14);
      const macdArr = calcMACD(rawBars);

      // 计算买卖点（区间套：2买/类2买 只在上级上涨笔段内，2卖/类2卖 只在上级下跌笔段内）
      // 1买/1卖 必须满足 MACD 背驰（柱状体面积变小 或 黄白线动能减弱）才标记
      let buyPts = findBuyPoints(curBis, upperBis, macdArr, intervalSecOf(res));
      let sellPts = findSellPoints(curBis, upperBis, macdArr, intervalSecOf(res));

      // 保留区间套下识别出的全部买卖点，再做邻近合并后，按 --keep 每类只保留最近 N 个（默认 1，减少图上标记数量）

      // 一买锚定：除日线外，每个一买都锚定到上一级某笔的底部端点
      // 注意：2买/类2买 基于结构底，不依赖一买锚定，予以保留
      let anchoredBuyPts = buyPts;
      const firstBuyPts = buyPts.filter(p => p.type === "1买");
      if (firstBuyPts.length > 0 && pi > 0) {
        const anchoredMarks = [];
        const seenMarkPos = new Set();
        for (const cand of firstBuyPts) {
          const anchored = anchorFirstBuy(cand, upperBis);
          if (anchored) {
            const snapTime = snapToOwnBar(anchored.price, anchored.time, rawBars);
            // 多个候选锚定到同一上级底（同一位置）时只保留一个，避免重复标记
            if (seenMarkPos.has(snapTime)) continue;
            seenMarkPos.add(snapTime);
            if (DEBUG) console.log(`[锚定] ${res} 候选一买 ${toT(cand.time)}@${cand.price} → 锚定上级底 ${toT(anchored.time)}@${anchored.price} → 映射到本周期 ${toT(snapTime)}`);
            anchoredMarks.push({ type: "1买", time: snapTime, price: anchored.price });
          } else {
            if (DEBUG) console.log(`[锚定] ${res} 候选一买 ${toT(cand.time)}@${cand.price} 前没有上级笔底部端点，不标记一买`);
          }
        }
        anchoredBuyPts = [
          ...buyPts.filter(p => p.type !== "1买"),
          ...anchoredMarks,
        ];
      }

      // 新增：1类 与 2类 买卖点价格很接近时，直接把 1类 合并到 2类 标记上，标注为「真1买/真1卖」。
      // 判定基于价格（不限制时间差）：1买/1卖 与 2买/2卖 的价差不超过 nearp×ATR（至少 0.05）视为很近
      const nearPrice = Math.max(atr * NEAR_ATR_RATIO, 0.05);
      anchoredBuyPts = mergeNearFirstSecond(anchoredBuyPts, "1买", "2买", "真1买", nearPrice);
      sellPts = mergeNearFirstSecond(sellPts, "1卖", "2卖", "真1卖", nearPrice);

      // 每类买卖点只保留时间上最近 KEEP 个（--keep=N，默认 1），避免图上标记过多
      anchoredBuyPts = keepRecentEach(anchoredBuyPts, KEEP);
      sellPts = keepRecentEach(sellPts, KEEP);

      // 汇总标记：把时间吸附到本周期bar边界
      // rawTime/rawPrice 保存未吸附的原始点位，用于跨周期共振判定
      const marks = [];
      const offset = Math.max(atr * 0.5, 0.05);
      for (const p of anchoredBuyPts) {
        const t = snapToOwnBar(p.price, p.time, rawBars);
        // 真1买 与 2买 同点位，文字偏移双倍（更靠下）避免重叠
        const mul = p.type === "真1买" ? 2 : 1;
        marks.push({ label: p.type, time: t, price: p.price - offset * mul, rawTime: p.time, rawPrice: p.price });
      }
      for (const p of sellPts) {
        const t = snapToOwnBar(p.price, p.time, rawBars);
        // 真1卖 与 2卖 同点位，文字偏移双倍（更靠上）避免重叠
        const mul = p.type === "真1卖" ? 2 : 1;
        marks.push({ label: p.type, time: t, price: p.price + offset * mul, rawTime: p.time, rawPrice: p.price });
      }

      // 跨周期共振：本级别的 1买/1卖 若与紧邻上一级别的 1/2/3 类买卖点落在同一点位
      // （原始时间/价格一致），说明该1类点同时是上级买卖点（如 4小时2买 同时是 1小时1买），
      // 本级别标记颜色改为绿色，其余保持红色。
      const RED = '#F23645', GREEN = '#089981';
      const upperRes = pi > 0 ? PERIODS[pi - 1] : null;
      const upperMarks = upperRes ? periodMarks[upperRes] : null;
      const upperClassRe = /^([123]买|[123]卖|类2买|类2卖)$/;
      for (const mk of marks) {
        mk.color = RED;
        if ((mk.label === '1买' || mk.label === '1卖') && upperMarks) {
          for (const um of upperMarks) {
            if (!upperClassRe.test(um.label)) continue;
            if (Math.abs(mk.rawTime - um.rawTime) <= 60 && Math.abs(mk.rawPrice - um.rawPrice) <= Math.max(atr * 0.2, 0.05)) {
              mk.color = GREEN;
              if (DEBUG) console.log(`[共振] ${res} ${mk.label}@${toT(mk.rawTime)}(${mk.rawPrice}) 与上级 ${upperRes} ${um.label}@${toT(um.rawTime)}(${um.rawPrice}) 同点位 → 绿色`);
              break;
            }
          }
        }
      }

      // 打印结果
      console.log("\n=== 买卖点结果 [周期 " + res + "] ===");
      console.log("品种:", SYMBOL, "周期:", res, "(从 " + FROM_DATE + " 开始)");
      console.log("笔数量:", curBis.length, "原始K线:", rawBars.length);
      console.log("买点:", anchoredBuyPts.length ? anchoredBuyPts.map(p => `${p.type}@${toT(p.time)}(${p.price})`).join(", ") : "无");
      console.log("卖点:", sellPts.length ? sellPts.map(p => `${p.type}@${toT(p.time)}(${p.price})`).join(", ") : "无");
      if (marks.length) {
        console.log("标记:", marks.map(mk => `${mk.label}@${toT(mk.time)}(${mk.price})${mk.color === GREEN ? "(绿)" : ""}`).join(", "));
      }

      // 记录本周期标记，供下一级周期跨周期共振判定使用
      periodMarks[res] = marks;

      // 记录本周期笔，供下一级周期锚定一买使用
      upperBis = curBis;

      if (DRY) continue;

      // 绘制前确保图表数据覆盖最早标记时间
      if (marks.length > 0) {
        const minT = marks.reduce((m2, mk) => Math.min(m2, mk.time), Infinity);
        await ensureBarsCover(res, minT);
      }

      const drawResult = await drawMarks(res, marks);
      console.log("=== 绘制结果 [周期 " + res + "] ===");
      console.log(JSON.stringify(drawResult, null, 2));
    }

    // 切回原周期
    if (originalRes !== currentRes) {
      await ensureResolution(originalRes);
      console.log("\n已切回原周期:", originalRes);
    }

    if (DRY) {
      console.log("\n[DRY RUN] 不绘图。");
    }

    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
  }
})();
