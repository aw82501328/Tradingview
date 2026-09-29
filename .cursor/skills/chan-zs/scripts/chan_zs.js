/**
 * 缠论中枢绘制脚本（独立 SKILL：chan-zs）
 * 读取 chan-bi（画笔）SKILL 落盘的笔数据 .cursor/cache/bis_<品种>.json，
 * 按缠论中枢定义（分解原则：同一上级笔内、至少5笔、取全部笔重叠区间、
 * 水平边缘=[进入笔终点-5根K, 离开笔起点+5根K]）计算中枢，
 * 并在 TradingView Desktop 图上绘制矩形（颜色与该周期笔一致、仅本周期显示）。
 *
 * 用法：
 *   node chan_zs.js --from=YYYY-MM-DD   计算并绘制中枢
 *   node chan_zs.js --dry --from=...    只计算打印，不绘图
 *
 * 参数：
 *   --from=YYYY-MM-DD   起始日期（应与画笔 chan-bi 一致）
 *   --periods=...       周期列表（逗号分隔，默认 D,240,60,15,3）
 *   --zs-periods=...    要画中枢的周期（逗号分隔，默认 60；空=全部不画但仍清旧图）
 *   --zs-keep=60:10,240:5  各周期只保留最近 N 个中枢（省略则该周期全画）
 *   --dry               只计算不绘图
 *   --debug             打印调试信息
 */
const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");
// 缠论算法核心（唯一算法源，与 chan-bi / mark-buy-sell 共用）
const core = require("../../chan-core/scripts/chan_core.js");
const { buildZSByUpper, lowerResOf, intervalSecOf } = core;

// 缓存目录（chan-bi 画笔落盘笔数据、本脚本落盘中枢数据）
const CACHE_DIR = process.env.CHAN_CACHE_DIR || path.join(__dirname, "..", "..", "..", "..", ".cursor", "cache");
const cacheFile = (prefix, symbol) => path.join(CACHE_DIR, `${prefix}_${String(symbol).replace(/[^A-Za-z0-9_.-]/g, "_")}.json`);

// 解析命令行参数
const args = process.argv.slice(2);
const DRY = args.includes("--dry");
const DEBUG = args.includes("--debug");
const getStrArg = (name, def) => {
  const a = args.find(x => x.startsWith("--" + name + "="));
  return a ? a.split("=")[1] : def;
};
const FROM_DATE = getStrArg("from", "");
// 参数中心（WEB 参数配置页）整体覆盖（中枢构建当前不读 CHAN_CFG，透传保持五模块 CLI 一致；
// 未知键在 JS 侧闲置无害）
const CHAN_CFG_JSON = getStrArg("chan-cfg", "");
if (CHAN_CFG_JSON) {
  try { Object.assign(core.CHAN_CFG, JSON.parse(CHAN_CFG_JSON)); }
  catch (e) { console.log("警告: --chan-cfg JSON 解析失败，忽略该参数"); }
}
let FROM_TS = null;
if (FROM_DATE) {
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(FROM_DATE.trim());
  if (m) FROM_TS = Math.floor(Date.UTC(+m[1], +m[2] - 1, +m[3]) / 1000);
  else console.log("警告: --from 日期格式应为 YYYY-MM-DD，忽略该参数");
}
// 周期列表（从大到小），用于上级笔归属关系
const PERIODS = getStrArg("periods", "D,240,60,15,3")
  .split(",").map(s => s.trim()).filter(Boolean);
// 要画中枢的周期（默认仅 1小时 60；空字符串=不画任何周期）
const ZS_PERIODS = (getStrArg("zs-periods", "60") || "")
  .split(",").map(s => s.trim()).filter(Boolean);
// 各周期保留最近个数：形如 60:10,240:5；未列出的周期画全部
const ZS_KEEP = (() => {
  const map = {};
  const raw = getStrArg("zs-keep", "") || "";
  for (const part of raw.split(",")) {
    const s = part.trim();
    if (!s) continue;
    const i = s.indexOf(":");
    if (i <= 0) continue;
    const res = s.slice(0, i).trim();
    const n = parseInt(s.slice(i + 1).trim(), 10);
    if (res && Number.isFinite(n) && n >= 1) map[res] = n;
  }
  return map;
})();
// 中枢水平边缘左右各外扩的K线根数（用户规则：进入笔终点-5根K ~ 离开笔起点+5根K）
const ZS_EDGE_PAD_BARS = 5;

// ============================================================
// 绘制配置（颜色与可见范围，与 chan-bi 保持一致）
// ============================================================

/**
 * 周期颜色（与笔颜色一致）：3分钟=青蓝, 15分钟=紫, 1小时=黄, 4小时=蓝, 日线=红, 周线=绿
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
 * 只在该周期显示（不跨周期）：intervalsVisibilities 精确到「仅本周期」。
 * 中枢只在该周期显示，切到其他周期时自动隐藏（不能跨周期）。
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

    // 读取 chan-bi 画笔落盘的笔数据（强制依赖）
    const bisFile = cacheFile("bis", SYMBOL);
    if (!fs.existsSync(bisFile)) {
      console.log(`ERROR: 未找到笔数据文件 ${bisFile}`);
      console.log("请先对当前品种运行「画笔」SKILL（chan-bi）生成笔数据，再运行本脚本画中枢。");
      process.exit(1);
    }
    const bisData = JSON.parse(fs.readFileSync(bisFile, "utf8"));
    if (bisData && bisData.analysisInvalidReason) throw new Error(bisData.analysisInvalidReason);
    if (bisData.symbol !== SYMBOL) {
      console.log(`ERROR: 笔数据文件属于 ${bisData.symbol}，与当前品种 ${SYMBOL} 不一致`);
      console.log("请先对当前品种运行「画笔」SKILL（chan-bi）生成笔数据，再运行本脚本画中枢。");
      process.exit(1);
    }
    const periodBis = core.structurePeriods(bisData.periods || {}, bisData.bars || {}, Math.floor(Date.now()/1000));
    console.log(`已读取笔数据: ${bisFile}（${Object.keys(periodBis).length} 个周期）`);

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

    const toT = (ts) => {
      const dt = new Date(ts * 1000);
      const p = (n) => String(n).padStart(2, '0');
      return `${dt.getMonth()+1}-${dt.getDate()} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
    };

    /**
     * 绘制前确保图表数据覆盖到最早中枢的时间：
     * 切换到低一级周期后图表可能只加载最近 N 根 K 线。较早中枢的左右缘都超出数据范围时，
     * TradingView 会把两个角点吸到同一根已加载 K 线上，矩形宽度变成 0，图上就是一条竖线。
     * 因此绘制前检查第一根 K 线是否早于最早中枢，未覆盖则 scrollToFirstBar 加载完整历史。
     * 数据分批推进可能在批次之间停顿数秒，单轮「len+first 连续不变」会提前退出，
     * 所以最多 3 轮「scrollToFirstBar + 等待」，每轮稳定后重触发滚动继续加载。
     * 返回 { covered, first }：covered=false 时调用方必须跳过超范围中枢，不能创建 shape。
     */
    const ensureBarsCover = async (res, minTs) => {
      const readFirst = async () => {
        const r = await client.Runtime.evaluate({
          expression: `(function() {
            const chart = TradingViewApi.activeChart();
            const items = chart.chartModel().mainSeries().data().m_bars._items;
            if (!items || items.length === 0) return { first: null, len: 0 };
            const t = items[0].value[0];
            return { first: t, len: items.length };
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 10000,
        });
        return r.result.value || { first: null, len: 0 };
      };
      const scrollToFirstBar = () => client.Runtime.evaluate({
        expression: `(function() {
          const chart = TradingViewApi.activeChart();
          const widget = chart._chartWidget || (chart.chartModel && chart.chartModel()._chartWidget);
          const ts = widget && widget.model ? widget.model().timeScale() : chart.chartModel().timeScale();
          ts.scrollToFirstBar();
          return 'ok';
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 10000,
      });
      let cur = await readFirst();
      if (cur.first !== null && cur.first <= minTs) return { covered: true, first: cur.first };
      if (DEBUG) console.log(`[数据覆盖] ${res} 首根K线 ${toT(cur.first)} 晚于最早中枢 ${toT(minTs)}，加载完整历史...`);
      let covered = false;
      for (let round = 0; round < 3 && !covered; round++) {
        if (round > 0 && DEBUG) console.log(`[数据覆盖] ${res} 第 ${round + 1} 轮重试加载（当前首根 ${toT(cur.first)}）...`);
        await scrollToFirstBar();
        let prevLen = cur.len;
        let prevFirst = cur.first;
        let stableCnt = 0;
        // 每轮最多 60×1.2s：覆盖即停；「len+first 连续 3 次不变」只结束本轮
        for (let i = 0; i < 60; i++) {
          await sleep(1200);
          cur = await readFirst();
          if (cur.first !== null && cur.first <= minTs) { covered = true; break; }
          if (cur.len === prevLen && cur.len > 0 && i >= 3 && cur.first === prevFirst) {
            stableCnt++;
            if (stableCnt >= 3) break;
          } else {
            stableCnt = 0;
          }
          prevLen = cur.len;
          prevFirst = cur.first;
        }
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
      return { covered, first: cur.first };
    };

    // 按 id 读回已创建中枢矩形的两个角点（创建成功 ≠ 左右缘正确：超出数据范围会被吸到同一根 K 线）
    const readRectsByIds = async (ids) => {
      const r = await client.Runtime.evaluate({
        expression: `(function() {
          const chart = TradingViewApi.activeChart();
          const IDS = ${JSON.stringify(ids)};
          return IDS.map(id => {
            try {
              const sh = chart.getShapeById(id);
              const pts = sh && sh._source && sh._source._points;
              if (!pts || pts.length < 2) return null;
              return [{ time: pts[0].time, price: pts[0].price }, { time: pts[1].time, price: pts[1].price }];
            } catch (e) { return null; }
          });
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 20000,
      });
      return (r.result && r.result.value) || [];
    };

    const removeShapesByIds = async (ids) => {
      if (!ids || ids.length === 0) return;
      await client.Runtime.evaluate({
        expression: `(function() {
          const chart = TradingViewApi.activeChart();
          for (const id of ${JSON.stringify(ids)}) { try { chart.removeEntity(id); } catch (e) {} }
          return 'ok';
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 20000,
      });
    };

    // ============================================================
    // 清除某周期在本图上的中枢（只清除本周期自己的中枢）
    // 必须在「源周期」上调用（该周期中枢在源周期一定可见）
    // ============================================================
    const clearZS = async (res) => {
      const ZS_TITLE = "CHAN_ZS_" + res;
      const r = await client.Runtime.evaluate({
        expression: `(function() {
          const chart = TradingViewApi.activeChart();
          const ZS_TITLE = "${ZS_TITLE}";
          const out = { cleared: 0 };
          const readTitle = (id) => {
            try {
              const sh = chart.getShapeById(id);
              const props = sh && sh._source && sh._source._properties;
              return props && props.title ? String(props.title._value) : '';
            } catch(e) { return ''; }
          };
          try {
            const shapes = chart.getAllShapes();
            for (const s of shapes) {
              if (s.name !== 'rectangle') continue;
              const t = readTitle(s.id);
              if (t === ZS_TITLE) {
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
    // 在某周期绘制中枢（矩形，仅边框不填充）
    // 与 chan-bi 的笔一致：必须在「绘制周期」（校准基准周期）上调用，
    // 使校准后的端点时间正好落在基准周期的 bar 边界上。
    // 矩形范围：水平 [startTime, endTime]，垂直 [zd, zg]（中枢区间）。
    // title 打上源周期标签 + 可见范围仅本周期（不跨周期，见 onlyThisInterval）。
    // ============================================================
    const createZS = async (res, zss) => {
      const ZS_COLOR = resolutionColor(res);   // 颜色与笔一致
      const ZS_TITLE = "CHAN_ZS_" + res;
      const IV_CFG = onlyThisInterval(res);
      const r = await client.Runtime.evaluate({
        expression: `(async function() {
          const chart = TradingViewApi.activeChart();
          const ZSS = ${JSON.stringify(zss)};
          const ZS_COLOR = "${ZS_COLOR}";
          const ZS_TITLE = "${ZS_TITLE}";
          const IV_CFG = ${JSON.stringify(IV_CFG)};
          const out = { zs_ok: 0, zs_err: [] };
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

          // 创建矩形后设置边框颜色、填充透明、标题
          // 注意：TradingView rectangle 的边框色属性是 color（非 rectcolor），
          // 填充开关是 fillBackground + backgroundColor（非 rectbackground）。
          const styleRect = (id) => {
            try {
              const props = chart.getShapeById(id)._source._properties;
              if (props.color) props.color.setValue(ZS_COLOR);                  // 边框颜色与该周期笔一致
              if (props.fillBackground) props.fillBackground.setValue(false);    // 不填充，仅边框
              if (props.backgroundColor) props.backgroundColor.setValue('rgba(0,0,0,0)');
              if (props.title) props.title.setValue(ZS_TITLE);
              if (props.linestyle) props.linestyle.setValue(0);
              if (props.linewidth) props.linewidth.setValue(1);
            } catch(e) {}
          };

          for (const z of ZSS) {
            try {
              // rectangle 需要 2 个点（左上=起始时间+中枢上沿，右下=结束时间+中枢下沿）
              const id = await chart.createMultipointShape(
                [{ time: z.startTime, price: z.zg }, { time: z.endTime, price: z.zd }],
                { shape: 'rectangle', lock: false }
              );
              styleRect(id);
              applyIV(id);
              created.push(id);
              out.zs_ok++;
            } catch(e) { out.zs_err.push(e.message); }
          }
          return { ...out, created_ids: created };
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };

    // ============================================================
    // 主流程：按周期计算中枢并绘制
    // 中枢在所有周期笔数据（chan-bi 落盘）基础上统一计算，计算过程不修改任何笔。
    // 只对 --zs-periods 指定的周期绘制；先清 PERIODS 内旧中枢，避免关闭周期残留。
    const clearList = PERIODS.filter(res => periodBis[res]);
    const drawZSPeriods = PERIODS.filter(res => ZS_PERIODS.includes(res) && periodBis[res] && periodBis[res].length >= 3);
    console.log("将绘制中枢周期:", drawZSPeriods.length ? drawZSPeriods.join(", ") : "(无)");

    // 逐周期计算中枢
    const allZS = {};
    for (const res of drawZSPeriods) {
      const piU = PERIODS.indexOf(String(res));
      // 上级 = PERIODS 中 res 的上一项（PERIODS 从大到小排列）
      const upperRes = piU > 0 ? PERIODS[piU - 1] : null;
      const upperBis = upperRes ? (periodBis[upperRes] || []) : [];
      const lowerBis = periodBis[res];
      // 计算中枢（分解原则：本级别中枢只能构建在「同一个上级笔」内；
      // barSec = 本周期单根K线时长，中枢水平边缘左右各外扩 5 根K线）
      const barSec = intervalSecOf(res);
      let zss = buildZSByUpper(lowerBis, upperBis, barSec);
      const keepN = ZS_KEEP[res];
      if (keepN != null && zss.length > keepN) {
        // 按进入笔终点时间保留最近 N 个
        zss = zss.slice().sort((a, b) => (a.enterEndTime || a.startTime) - (b.enterEndTime || b.startTime));
        zss = zss.slice(-keepN);
      }
      allZS[res] = zss;
      if (zss.length > 0) {
        console.log(`\n=== 缠论中枢 [周期 ${res}]（分解约束：同一上级笔 ${upperRes} 内，至少3笔；水平边缘=[进入笔终点-5根K, 离开笔起点+5根K]${keepN != null ? `；保留最近${keepN}个` : ""}）===`);
        zss.forEach((z, idx) => {
          const exitTag = z.exitTime ? ` 离开笔起点[${toT(z.exitTime)}]` : "";
          const edgeTag = ` 左缘=${toT(z.startTime)} 右缘=${toT(z.endTime)}`;
          console.log(
            `中枢${idx + 1}: 区间[${z.zd.toFixed(2)}, ${z.zg.toFixed(2)}] GG=${z.gg.toFixed(2)} DD=${z.dd.toFixed(2)} 笔数=${z.biCount}${z.extended ? " (延伸)" : ""} 归属上级笔[${toT(z.upperStart)}~${toT(z.upperEnd)}]${exitTag}${edgeTag}`
          );
        });
      } else {
        console.log(`\n[周期 ${res}] 无满足条件的中枢`);
      }
    }

    // 中枢数据落盘（分解原则：每个中枢带 upperStart/upperEnd 所属上级笔范围）
    try {
      fs.mkdirSync(CACHE_DIR, { recursive: true });
      const zsCacheFile = cacheFile("zs", SYMBOL);
      const payload = {
        symbol: SYMBOL,
        from: FROM_DATE || null,
        fromTs: FROM_TS,
        generatedAt: new Date().toISOString(),
        zsEdgePadBars: ZS_EDGE_PAD_BARS,
        periods: allZS, // key=周期(如 240/60/15/3), value=该周期中枢数组
      };
      fs.writeFileSync(zsCacheFile, JSON.stringify(payload, null, 2), "utf8");
      const total = Object.keys(allZS).reduce((s, r) => s + allZS[r].length, 0);
      console.log(`\n中枢数据已落盘: ${zsCacheFile}（${Object.keys(allZS).length} 个周期，共 ${total} 个中枢）`);
    } catch (e) {
      console.log("警告: 中枢数据落盘失败:", e.message);
    }

    if (DRY) {
      console.log("\n[DRY RUN] 不绘图。");
      await client.close();
      return;
    }

    // 绘制阶段：先清除 PERIODS 内旧中枢（含已关闭周期），再统一创建
    let currentRes = originalRes;
    for (const res of clearList) {
      if (res !== currentRes) {
        await ensureResolution(res);
        currentRes = res;
      }
      const clearedZS = await clearZS(res);
      console.log(`\n[周期 ${res}] 已清除旧中枢: ${clearedZS.cleared} 个`);
    }
    console.log("\n=== 中枢阶段（统一绘制）===");
    for (const res of drawZSPeriods) {
      const zss = allZS[res];
      if (zss.length === 0) continue;
      // 与笔一致：在「低一级校准基准周期」上创建矩形，使端点落在基准周期 bar 边界
      const lowerRes = lowerResOf(res);
      const drawRes = lowerRes || res;
      if (drawRes !== currentRes) {
        await ensureResolution(drawRes);
        currentRes = drawRes;
      }
      // 绘制前确保图表数据覆盖最早中枢时间。未覆盖则只画左右缘都在已加载范围内的中枢：
      // 更早的中枢两个角都会被吸到数据边缘的同一根 K 线，矩形退化成竖线。落盘数据仍保留完整列表。
      let drawZss = zss;
      const minTs = zss.reduce((m, z) => Math.min(m, z.startTime, z.endTime), Infinity);
      if (minTs !== Infinity) {
        const cover = await ensureBarsCover(drawRes, minTs);
        if (!cover.covered && cover.first !== null) {
          const before = drawZss.length;
          drawZss = drawZss.filter(z => z.startTime >= cover.first && z.endTime >= cover.first);
          console.log(`[周期 ${res}] 警告: ${drawRes} 周期数据仅加载到 ${toT(cover.first)}，跳过 ${before - drawZss.length} 个更早的中枢（避免吸附成竖线；落盘数据完整）`);
          if (drawZss.length === 0) {
            console.log(`[周期 ${res}] 全部中枢超出已加载数据范围，本轮跳过绘制`);
            continue;
          }
        }
      }
      const createZSResult = await createZS(res, drawZss);

      // 回读校验：左右缘时间被吸到同一根 K 线（读回宽度不超过一根基准 K，而请求宽度更长）时删除重试。
      // 只认「宽度塌掉」，不比较绝对时间——读回时间若整体平移，宽度仍在的矩形保持不动。
      let finalResult = { ...createZSResult };
      const createdIds = createZSResult.created_ids || [];
      if (createdIds.length > 0 && drawZss.length > 0) {
        const barSec = intervalSecOf(drawRes) || 1;
        const verifyOnce = async (ids, list) => {
          const ptsArr = await readRectsByIds(ids);
          const bad = [];
          for (let i = 0; i < ids.length; i++) {
            const p = ptsArr[i];
            if (!p || !p[0] || !p[1]) continue; // 角点读不到不误删
            const z = list[i];
            const want = Math.abs(z.endTime - z.startTime);
            const got = Math.abs(p[1].time - p[0].time);
            const collapsed = want > barSec && got <= barSec;
            const hi = Math.max(p[0].price, p[1].price);
            const lo = Math.min(p[0].price, p[1].price);
            const priceBad = Math.abs(hi - z.zg) > 0.05 || Math.abs(lo - z.zd) > 0.05;
            if (collapsed || priceBad) bad.push({ id: ids[i], z });
          }
          return bad;
        };
        const bad = await verifyOnce(createdIds, drawZss);
        if (bad.length > 0) {
          console.log(`[周期 ${res}] 回读校验: ${bad.length}/${createdIds.length} 个中枢被吸附成竖线，删除后重试...`);
          await removeShapesByIds(bad.map(x => x.id));
          const retryMin = bad.reduce((m, x) => Math.min(m, x.z.startTime, x.z.endTime), Infinity);
          if (retryMin !== Infinity) await ensureBarsCover(drawRes, retryMin);
          const retryZss = bad.map(x => x.z);
          const retry = await createZS(res, retryZss);
          const bad2 = await verifyOnce(retry.created_ids || [], retryZss);
          if (bad2.length > 0) {
            await removeShapesByIds(bad2.map(x => x.id));
            console.log(`[周期 ${res}] 警告: 重试后仍有 ${bad2.length} 个中枢无法正确创建（数据未覆盖），已移除，可稍后重跑`);
          }
          finalResult = {
            ...finalResult,
            zs_ok: (createZSResult.zs_ok || 0) - bad.length + (retry.zs_ok || 0) - bad2.length,
            zs_bad: bad2.length,
          };
        }
      }
      console.log(`\n=== 中枢绘制结果 [周期 ${res}] ===`);
      console.log(JSON.stringify(finalResult, null, 2));
    }

    // 最后切回原周期
    if (originalRes !== currentRes) {
      await ensureResolution(originalRes);
      console.log("\n已切回原周期:", originalRes);
    }

    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
  }
})();
