/**
 * 缠论画笔脚本
 * 在 TradingView Desktop 图表上按缠论理论画笔（笔 + 一买/二买/三买）
 *
 * 用法：
 *   node chan_bi.js --dry   只计算并打印笔和买点，不绘图
 *   node chan_bi.js         计算并绘制到图表
 *
 * 参数：
 *   --bars=200      取最近 N 根K线（默认 200，仅在未指定日线起点时使用）
 *   --atr=0.5       ATR 过滤系数（幅度 < atr*ATR 的笔剔除，默认 0.5）
 *   --gap=0.5       跳空独立成笔阈值（跳空缺口 >= gap*ATR 时强制独立成笔）
 *   --periods=...   要绘制的周期列表（逗号分隔，默认 D,240,60,15,3）
 *   --from=YYYY-MM-DD  指定日线起点日期（从该日期的日K开始画，嵌套到各级别）
 */
const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");
// 缠论算法核心（唯一算法源，与 mark-buy-sell SKILL 共用）
const core = require("../../chan-core/scripts/chan_core.js");
const {
  mergeBars, findFractals, countRaw, hasGapBetween, buildBi, lockedPivotsOf, alignBiToUpper,
  calcATR, calcMACD, hasMacdCrossBetween,
  extendLastBi, lowerResOf, calibrateBiTimes, intervalSecOf,
} = core;

// 笔数据落盘目录（mark-buy-sell SKILL 强制从此读取，实现「画笔 → 标记」数据依赖）
const CACHE_DIR = path.join(__dirname, "..", "..", "..", "..", ".cursor", "cache");
// 品种名中的特殊字符替换为下划线，保证文件名合法（如 TVC:UKOIL → TVC_UKOIL）
const bisCacheFile = (symbol) => path.join(CACHE_DIR, `bis_${String(symbol).replace(/[^A-Za-z0-9_.-]/g, "_")}.json`);

// 解析命令行参数
const args = process.argv.slice(2);
const DRY = args.includes("--dry");
const getArg = (name, def) => {
  const a = args.find(x => x.startsWith("--" + name + "="));
  return a ? parseFloat(a.split("=")[1]) : def;
};
const getStrArg = (name, def) => {
  const a = args.find(x => x.startsWith("--" + name + "="));
  return a ? a.split("=")[1] : def;
};
const N_BARS = getArg("bars", 200);
const ATR_FILTER = getArg("atr", 0.5);
// 跳空独立成笔阈值：相邻K线缺口 >= gap*ATR 时，强制在该缺口处分笔（不受笔的最小间隔/极值规则限制）
const GAP_FILTER = getArg("gap", 1.0);
const DEBUG = args.includes("--debug");
core.CHAN_CFG.debug = DEBUG;
core.CHAN_CFG.gapFilter = GAP_FILTER;
// 指定的日线起点日期（如 2026-07-02），解析为 UTC 当天 0 点的时间戳
const FROM_DATE = getStrArg("from", "");
let FROM_TS = null;
if (FROM_DATE) {
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(FROM_DATE.trim());
  if (m) {
    FROM_TS = Math.floor(Date.UTC(+m[1], +m[2] - 1, +m[3]) / 1000);
  } else {
    console.log("警告: --from 日期格式应为 YYYY-MM-DD，忽略该参数");
  }
}
// 要绘制的周期列表，按从大到小排列（日线 → 4小时 → 1小时 → 15分钟 → 3分钟）
// 外层先画，内层以外层一笔的起点为锚，嵌套迭代画内部笔
const PERIODS = getStrArg("periods", "D,240,60,15,3")
  .split(",").map(s => s.trim()).filter(Boolean);

// 内层窗口最小K线数：锚点范围内K线不足时向前扩展
const MIN_WINDOW_BARS = 20;
// 内层计算缓冲：锚点前额外取的K线数，保证窗口起点处能形成完整分型
const ANCHOR_BUFFER = 30;

// ============================================================
// 绘制配置（笔的颜色与周期可见范围）
// ============================================================

/**
 * 笔的颜色（按图表周期）
 * 3分钟=青蓝色, 15分钟=紫色, 1小时=黄色, 4小时=蓝色, 日线=红色, 周线=绿色
 * 其他周期默认紫色
 */
function resolutionColor(res) {
  const r = String(res).toUpperCase();
  switch (r) {
    case "3":    return "#00BCD4";  // 青蓝色
    case "15":   return "#8A2BE2";  // 紫色
    case "60":
    case "1H":   return "#FFD700";  // 黄色
    case "240":
    case "4H":   return "#2962FF";  // 蓝色
    case "1D":
    case "D":    return "#F23645";  // 红色
    case "1W":
    case "W":    return "#089981";  // 绿色
    default:     return "#8A2BE2";  // 紫色（默认）
  }
}

/**
 * 周期的可见范围（intervalsVisibilities 精确范围）
 * 规则：某周期的笔只显示在「该周期 + 低一级周期」，其余周期隐藏。
 *   3分钟 → 30秒、3分钟
 *   15分钟 → 3分钟、15分钟
 *   1小时 → 15分钟、1小时
 *   4小时 → 1小时、4小时
 *   日线  → 4小时、日线
 *   周线  → 日线、周线
 * 通过设置 shape 的 intervalsVisibilities（大类开关 + from/to 范围）实现，
 * 切到不在范围内的周期时，TradingView 会自动隐藏该笔。
 * 返回 null 表示不限制（全部周期可见）。
 */
function intervalVisibility(res) {
  const r = String(res).toUpperCase();
  // 全部关闭的模板（大类为 false 时 from/to 不参与判断）
  const NONE = {
    ticks: false,
    seconds: false, secondsFrom: 1, secondsTo: 59,
    minutes: false, minutesFrom: 1, minutesTo: 59,
    hours: false, hoursFrom: 1, hoursTo: 24,
    days: false, daysFrom: 1, daysTo: 366,
    weeks: false, weeksFrom: 1, weeksTo: 52,
    months: false, monthsFrom: 1, monthsTo: 12,
  };
  switch (r) {
    case "3":
      // 3分钟笔：默认显示在 30秒、3分钟 两个周期
      return { ...NONE, minutes: true, minutesFrom: 3, minutesTo: 3, seconds: true, secondsFrom: 30, secondsTo: 30 };
    case "15":
      return { ...NONE, minutes: true, minutesFrom: 3, minutesTo: 15 };
    case "60":
    case "1H":
      return { ...NONE, minutes: true, minutesFrom: 15, minutesTo: 15, hours: true, hoursFrom: 1, hoursTo: 1 };
    case "240":
    case "4H":
      return { ...NONE, hours: true, hoursFrom: 1, hoursTo: 4 };
    case "1D":
    case "D":
      return { ...NONE, hours: true, hoursFrom: 4, hoursTo: 24, days: true, daysFrom: 1, daysTo: 1 };
    case "1W":
    case "W":
      return { ...NONE, days: true, daysFrom: 1, daysTo: 7, weeks: true, weeksFrom: 1, weeksTo: 1 };
    default:
      return null; // 未列出的周期不限制可见范围
  }
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
    console.log("将绘制周期:", PERIODS.join(", "));

    // 切换到指定周期并等待K线加载完成（长度连续两次一致视为稳定）
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
          if (v.len === lastLen) break; // 数据稳定
          lastLen = v.len;
        }
      }
    };

    const fetchBars = async (expectedIntervalSec, fromTs, buffer) => {
      const needCover = fromTs !== null && fromTs !== undefined;
      // 允许K线起点与起始日期有小偏差：
      // 实际第一根K线通常晚于起始日 00:00（如 15分钟 7-2 03:15 > 7-2 00:00），
      // 若按严格 bars[0] <= fromTs 判断，会永远触发 notCovered 导致加载超时。
      // 容忍度取 max(24根K线间隔, 6小时)。
      const tolerance = Math.max(expectedIntervalSec * 24, 6 * 3600);
      let scrolled = false; // 是否已触发过历史数据加载
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
            // 用最后 20 个相邻间隔的中位数判断真实周期，
            // 避免首尾K线间停牌缺口（如 1D 首两根 gap=3天）导致误判
            const gaps = [];
            for (let i = bars.length - 1; i >= Math.max(1, bars.length - 20); i--) {
              gaps.push(bars[i].time - bars[i - 1].time);
            }
            gaps.sort((a, b) => a - b);
            const gap = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0;
            const fromTs = ${JSON.stringify(fromTs)};
            const tolerance = ${JSON.stringify(tolerance)};
            if (fromTs) {
              // 数据起点仍晚于起始日期较多（尚未覆盖）→ 返回 notCovered，触发历史加载
              if (bars[0].time > fromTs + tolerance) {
                return { bars: [], resolution: String(chart.resolution()), gap, len: bars.length, notCovered: true };
              }
              const fromIdx = bars.findIndex(k => k.time >= fromTs);
              const buf = ${buffer || 0};
              const start = Math.max(0, fromIdx - buf);
              return { bars: bars.slice(start), resolution: String(chart.resolution()), gap, len: bars.length };
            }
            return { bars, resolution: String(chart.resolution()), gap, len: bars.length };
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 15000,
        });
        const d = dataRes.result.value;
        if (!d || d.error || !d.bars) {
          await sleep(1200);
          continue;
        }
        if (DEBUG) console.log(`[fetchBars ${d.resolution}] len=${d.len} slice=${d.bars.length} gap=${d.gap} expect=${expectedIntervalSec}`);
        if (expectedIntervalSec && d.gap !== expectedIntervalSec) {
          await sleep(1500);
          continue;
        }
        if (needCover && d.notCovered) {
          if (!scrolled) {
            scrolled = true;
            // 强制加载完整历史数据（scrollToFirstBar 会触发数据流持续加载到第一根K线）
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
            console.log(`[周期 ${d.resolution}] 数据未覆盖起始日期，正在加载完整历史...`);
            await sleep(800);
          } else {
            await sleep(2500); // 已触发加载，等待数据加载推进
          }
          continue;
        }
        if (needCover && scrolled) {
          // 历史已加载完，把可视范围滚回最新K线，避免图表停在最老的数据上
          try {
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
          } catch(e) {}
        }
        return d;
      }
      // 始终没等到目标周期数据：返回 null，由调用方跳过该周期，避免用错误周期的K线画图
      return null;
    };

    const toT = (ts) => {
      const dt = new Date(ts * 1000);
      const p = (n) => String(n).padStart(2, '0');
      return `${dt.getMonth()+1}-${dt.getDate()} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
    };

    /**
     * 绘制前确保图表数据覆盖到最早笔的时间：
     * 校准步骤（切到低一级周期加载基准K线后切回本周期）会让图表只加载最近N根K线，
     * 此时若直接绘制较早的笔，时间戳超出数据范围会被 TradingView 吸附到数据边缘
     * （端点 index:0 / 错误时间），产生「无效的笔」。
     * 因此绘制前检查图表第一根K线是否 <= 最早笔时间，若未覆盖则 scrollToFirstBar 加载完整历史，
     * 等待覆盖（或数据不再增长即已到最早），再滚回实时。
     */
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
      if (cur.first !== null && cur.first <= minTs) return; // 已覆盖
      if (DEBUG) console.log(`[数据覆盖] ${res} 首根K线 ${toT(cur.first)} 晚于最早笔 ${toT(minTs)}，加载完整历史...`);
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
        // 就退出，加载可能只进行到中途，早期笔端点会被吸附到已加载数据边缘
        if (cur.len === prevLen && cur.len > 0 && i >= 3 && cur.first === prevFirst) {
          stableCnt++;
          if (stableCnt >= 3) break;
        } else {
          stableCnt = 0;
        }
        prevLen = cur.len;
        prevFirst = cur.first;
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
    // 清除某周期在本图上的笔（只清除该周期自己的笔，其他周期的笔保留）
    // 必须在「源周期」上调用（该周期的笔在源周期一定可见，能被 getAllShapes 拿到）
    // ============================================================
    const clearPeriod = async (res) => {
      const BI_TITLE = "CHAN_BI_" + res;            // 笔的周期标签
      const r = await client.Runtime.evaluate({
        expression: `(function() {
          const chart = TradingViewApi.activeChart();
          const BI_TITLE = "${BI_TITLE}";
          const out = { cleared: 0 };

          // 读取 shape 的周期标签（_properties.title 是我们绘制时打上的源周期标记）
          const readTitle = (id) => {
            try {
              const sh = chart.getShapeById(id);
              const props = sh && sh._source && sh._source._properties;
              return props && props.title ? String(props.title._value) : '';
            } catch(e) { return ''; }
          };

          // 只清除「本周期」本脚本之前画的笔（按 title 标签匹配）。
          // 其他周期画的笔会保留，不做清除。
          try {
            const shapes = chart.getAllShapes();
            for (const s of shapes) {
              if (s.name !== 'polyline') continue;
              const t = readTitle(s.id);
              if (t === BI_TITLE) {
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

    // ============================================================
    // 在某周期上创建笔（绘制）
    // 必须在「绘制周期」上调用：
    //   有校准基准的周期（15分钟用3分钟校准、1小时用15分钟校准、
    //   4小时用1小时校准、日线用4小时校准）应在基准周期创建 shape——
    //   TradingView 的 polyline 只支持把点精确放在「当前图表周期」的 bar 边界上，
    //   校准后的端点时间（基准周期的 bar 边界，如 4小时笔的 22:00）若在源周期
    //   （4小时）创建，会被吸附到源周期 bar 边界（20:00），导致跨周期校准失效。
    // ============================================================
    const createPeriod = async (res, bis) => {
      const BI_COLOR = resolutionColor(res);        // 按周期选择笔颜色
      const BI_TITLE = "CHAN_BI_" + res;            // 笔的周期标签
      const IV_CFG = intervalVisibility(res);       // 可见范围配置
      const r = await client.Runtime.evaluate({
        expression: `(async function() {
          const chart = TradingViewApi.activeChart();
          const BIS = ${JSON.stringify(bis)};
          const BI_COLOR = "${BI_COLOR}";
          const BI_TITLE = "${BI_TITLE}";
          const IV_CFG = ${JSON.stringify(IV_CFG)};
          const out = { bi_ok: 0, bi_err: [] };
          const created = [];

          // 给 shape 应用「周期可见范围」：设置 intervalsVisibilities 的各大类开关 + from/to 范围，
          // 使得该笔只显示在源周期及低一级周期，切到其他周期时自动隐藏。
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

          // 画笔（线段）：颜色按周期自动选择（3分青蓝/15分紫/1时黄/4时蓝/日线红/周线绿）
          // title 打上源周期标签；同时应用可见范围，使笔只显示在「该周期 + 低一级周期」
          // 注意：不锁定（lock:false），否则用户在图上右键无法打开设置修改可见周期
          for (const b of BIS) {
            try {
              const id = await chart.createMultipointShape(
                [{ time: b.startTime, price: b.startPrice }, { time: b.endTime, price: b.endPrice }],
                { shape: 'polyline', lock: false, overrides: { linecolor: BI_COLOR, linewidth: 1, title: BI_TITLE } }
              );
              applyIV(id);
              created.push(id);
              out.bi_ok++;
            } catch(e) { out.bi_err.push(e.message); }
          }

          return { ...out, created_ids: created };
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };

    // ============================================================
    // 多周期嵌套画笔：从大到小依次进行
    //   第 1 层（日线）：默认取最近 N 根K线；
    //                   若指定了日线起点日期 --from，则从该日期开始画日线笔
    //   第 2 层起（4小时/1小时/15分钟/3分钟）：以上一层「最后一笔」的起点为锚，
    //   在该锚点往后的K线上画内部笔，锚点范围内K线不足时向前扩展外层笔
    // ============================================================
    let currentRes = originalRes; // 记录当前图表实际所在周期
    // 逐级校准基准数据缓存：key=周期, value=该周期K线
    // 画某个周期时，按需加载其「低一级」周期K线作为端点时间校准基准
    //   （15分钟用3分钟校准，1小时用15分钟校准，4小时用1小时校准，日线用4小时校准）
    const refCache = {};
    const allBis = {}; // 收集各周期最终绘制的笔（含校准后的端点时间），循环结束后落盘供 mark-buy-sell 读取

    let prevBis = null;           // 上一周期的笔（用于确定下一层的锚点）
    for (let pi = 0; pi < PERIODS.length; pi++) {
      const res = PERIODS[pi];
      if (res !== currentRes) {
        await ensureResolution(res);
        currentRes = res;
      }

      const d = await fetchBars(intervalSecOf(res), FROM_TS, ANCHOR_BUFFER);
      if (!d || d.error || !d.bars || d.bars.length === 0) {
        console.log(`\n[周期 ${res}] 无K线数据或切换失败，跳过`);
        continue;
      }

      // 确定本层K线窗口：
      //   指定了起始日期 --from 时，**所有周期**都从该日期开始画笔
      //   （起点前补 ANCHOR_BUFFER 根缓冲保证分型完整），绘制时只画结束时间 ≥ 该日期的笔；
      //   未指定日期时：
      //     最外层（日线）取最近 N 根K线；
      //     内层以外层最后一笔的起点为锚，取该锚点之后的K线，
      //     并在锚点前额外补 ANCHOR_BUFFER 根K线作为分型缓冲，
      //     锚点范围内K线不足 MIN_WINDOW_BARS 根时从外层笔列表从后往前扩展。
      let windowBars, anchorInfo = "";
      let anchorStart = null; // 本层锚点（指定日期 / 外层最后一笔起点），绘制时只画锚点之后的笔
      if (FROM_TS !== null) {
        const idx = d.bars.findIndex(k => k.time >= FROM_TS);
        if (idx === -1) {
          console.log(`\n[周期 ${res}] 指定日期 ${FROM_DATE} 之后没有K线数据，跳过`);
          continue;
        }
        const bufStart = Math.max(0, idx - ANCHOR_BUFFER);
        windowBars = d.bars.slice(bufStart);
        anchorStart = FROM_TS;
        anchorInfo = `(从 ${FROM_DATE} 开始)`;
      } else if (pi === 0) {
        windowBars = d.bars.slice(-N_BARS);
        anchorInfo = `(取最近 ${N_BARS} 根)`;
      } else if (prevBis && prevBis.length > 0) {
        for (let j = prevBis.length - 1; j >= 0; j--) {
          const b = prevBis[j];
          const cnt = d.bars.filter(k => k.time >= b.startTime).length;
          anchorStart = b.startTime;
          if (cnt >= MIN_WINDOW_BARS) break;
        }
        if (anchorStart === null) anchorStart = d.bars[0].time;
        const idx = d.bars.findIndex(k => k.time >= anchorStart);
        const bufStart = Math.max(0, idx - ANCHOR_BUFFER);
        windowBars = d.bars.slice(bufStart);
        anchorInfo = `(锚定上层笔起点 ${toT(anchorStart)})`;
      } else {
        windowBars = d.bars;
        anchorInfo = "(上层无笔，取全部K线)";
      }

      const rawBars = windowBars;
      const merged = mergeBars(rawBars);
      const fractals = findFractals(merged);
      // ATR 需要提前计算，供 buildBi 的跳空独立成笔使用
      const atr = calcATR(rawBars, 14);
      // MACD 红绿柱，供 buildBi 的「MACD变色成笔」使用（区间内红变绿/绿变红时，间隔不足也可成笔）
      const macdArr = calcMACD(rawBars);
      // 区间套强制对齐：把上一层（更高级别）笔的端点作为锁定端点传入 buildBi，
      // 保证本级别笔端点与上级笔的极值端点严格重合（优先级最高）。
      const lockedPivots = lockedPivotsOf(prevBis);
      let bis = buildBi(fractals, merged, atr, macdArr, lockedPivots);

      // ATR 过滤
      const threshold = atr * ATR_FILTER;
      const beforeFilter = bis.length;
      bis = bis.filter(b => b.span >= threshold);
      const filteredOut = beforeFilter - bis.length;

      // 内层只画「结束时间在锚点之后」的笔（锚点前的缓冲仅用于保证分型完整）
      let drawBis = bis;
      if (anchorStart !== null) {
        drawBis = bis.filter(b => b.endTime >= anchorStart);
      }

      // 未完成笔延伸：最后一笔若未推进到当前K线（如末端单调上涨/下跌无新分型），
      // 延伸到窗口内该方向上的最新极端价所在K线
      drawBis = extendLastBi(drawBis, rawBars);

      // 逐级端点时间校准：用「低一级」周期K线校准本周期笔的端点时间，
      // 使不同周期对同一极值的标记位置在图上重合
      //   （15分钟用3分钟校准，1小时用15分钟校准，4小时用1小时校准，日线用4小时校准）
      const lowerRes = lowerResOf(res);
      if (lowerRes) {
        let refBars = refCache[lowerRes];
        if (!refBars) {
          await ensureResolution(lowerRes);
          const dref = await fetchBars(intervalSecOf(lowerRes), FROM_TS, ANCHOR_BUFFER);
          if (dref && !dref.error && dref.bars && dref.bars.length > 0) {
            refBars = dref.bars;
            refCache[lowerRes] = refBars;
            if (DEBUG) console.log(`[校准基准] ${res} 用 ${lowerRes} 校准，已加载 ${refBars.length} 根 ${lowerRes} 分钟K线`);
          }
          await ensureResolution(res);
          currentRes = res;
        }
        if (refBars) {
          drawBis = calibrateBiTimes(drawBis, rawBars, refBars, intervalSecOf(res));
        }
      }

      // 区间套强制对齐（优先级最高）：把本级别笔拐点对齐到紧邻上级笔拐点，
      // 使上级笔的极值端点在本级别中严格复现（上级底/顶=本级底/顶，同级同笔）。
      if (pi > 0 && prevBis && prevBis.length > 0) {
        drawBis = alignBiToUpper(drawBis, prevBis, intervalSecOf(PERIODS[pi - 1]));
      }

      console.log("\n=== 缠论计算结果 [周期 " + res + "] ===");
      console.log("品种:", SYMBOL, "周期:", res, anchorInfo);
      console.log("原始K线:", rawBars.length, "合并后:", merged.length, "分型:", fractals.length);
      console.log("ATR:", atr.toFixed(4), "过滤阈值(0.5*ATR):", threshold.toFixed(4));
      console.log("笔数量:", drawBis.length, "(计算", bis.length, "根，过滤掉", filteredOut, "根噪音小笔)");
      console.log("笔颜色:", resolutionColor(res), "(按周期", res + ")");
      console.log("--- 笔列表 ---");
      drawBis.forEach((b, i) => {
        const dir = b.type === "up" ? "上涨" : "下跌";
        const tag = b.gapLocked ? " | 跳空成笔" : (b.macdCross ? " | MACD变色成笔" : "");
        console.log(
          `笔${i + 1} [${dir}] ${b.startPrice}(${toT(b.startTime)}) -> ${b.endPrice}(${toT(b.endTime)}) | 幅度 ${b.span.toFixed(2)} | 原始K线 ${b.rawCount}${tag}`
        );
      });
      // 记录本层实际绘制的笔，作为下一层（更小周期）的锚定依据
      prevBis = drawBis;
      // 收集本周期最终笔数据（绘制阶段完成后再追加，确保与图上一致）
      allBis[res] = drawBis;

      if (DRY) continue;

      // 清除阶段：切回源周期，只清除本周期旧笔（源周期下本周期笔一定可见）
      if (res !== currentRes) {
        await ensureResolution(res);
        currentRes = res;
      }
      const clearedResult = await clearPeriod(res);

      // 创建阶段：
      // 有校准基准的周期（15分钟用3分钟校准、1小时用15分钟校准、4小时用1小时校准、
      // 日线用4小时校准）在基准周期创建 shape——TradingView 的 polyline 只支持把点
      // 精确放在「当前图表周期」的 bar 边界上，校准后的端点时间（基准周期 bar 边界，
      // 如 4小时笔端点校准到 1小时 22:00）若在源周期（4小时）创建，会被 TradingView
      // 吸附到源周期 bar 边界（20:00），导致跨周期端点校准失效。
      const drawRes = lowerRes || res;
      if (drawRes !== currentRes) {
        await ensureResolution(drawRes);
        currentRes = drawRes;
      }
      // 绘制前确保图表数据覆盖最早笔的时间（切换周期后图表可能只加载最近K线，
      // 会导致较早笔的端点超出数据范围而被 TradingView 吸附到数据边缘，形成无效笔）
      if (drawBis.length > 0) {
        const minBiTime = drawBis.reduce(
          (m, b) => Math.min(m, b.startTime, b.endTime),
          Infinity
        );
        if (minBiTime !== Infinity) {
          await ensureBarsCover(drawRes, minBiTime);
        }
      }

      const createResult = await createPeriod(res, drawBis);
      console.log("\n=== 绘制结果 [周期 " + res + "] ===");
      console.log(JSON.stringify({ ...clearedResult, ...createResult }, null, 2));
    }

    // 最后切回原周期
    if (originalRes !== currentRes) {
      await ensureResolution(originalRes);
      console.log("\n已切回原周期:", originalRes);
    }

    // 笔数据落盘：供 mark-buy-sell SKILL 强制读取（画笔 → 标记 数据依赖）。
    // 包含各周期的最终笔数据（已 ATR 过滤、未完成笔延伸、跨周期端点校准），
    // 与图上实际绘制的笔完全一致。
    try {
      fs.mkdirSync(CACHE_DIR, { recursive: true });
      const cacheFile = bisCacheFile(SYMBOL);
      const payload = {
        symbol: SYMBOL,
        from: FROM_DATE || null,
        fromTs: FROM_TS,
        generatedAt: new Date().toISOString(),
        periods: allBis, // key=周期(如 D/240/60/15/3), value=该周期笔数组
      };
      fs.writeFileSync(cacheFile, JSON.stringify(payload, null, 2), "utf8");
      const total = Object.keys(allBis).reduce((s, r) => s + allBis[r].length, 0);
      console.log(`\n笔数据已落盘: ${cacheFile}（${Object.keys(allBis).length} 个周期，共 ${total} 笔）`);
    } catch (e) {
      console.log("警告: 笔数据落盘失败:", e.message);
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
