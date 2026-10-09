/**
 * 缠论画笔脚本
 * 在 TradingView Desktop 图表上按缠论理论画笔（笔）
 * 中枢（缠论中枢矩形）由独立的 chan-zs SKILL 绘制（读取本脚本落盘的笔数据）。
 *
 * 用法：
 *   node chan_bi.js --dry   只计算并打印笔，不绘图
 *   node chan_bi.js         计算并绘制到图表
 *
 * 参数：
 *   --bars=200      取最近 N 根K线（默认 200，仅在未指定日线起点时使用）
 *   --atr=0.5       ATR 过滤系数（幅度 < atr*ATR 的笔剔除，默认 0.5）
 *   --gap=0.5       跳空独立成笔阈值（跳空缺口 >= gap*ATR 时强制独立成笔）
 *   --periods=...   要绘制的周期列表（逗号分隔，默认 D,240,60,15,3）
 *   --from=YYYY-MM-DD  指定日线起点日期（从该日期的日K开始画，嵌套到各级别）
 *   --with-30s      启用 30 秒级别（追加 30S 周期：只计算并落盘笔数据，不在图上绘制；
 *                   供 mark-entry --with-30s 的「以下级别背驰」使用。关闭后重跑本脚本即移除 30S 数据）
 *   --no-tongbi     关闭「同笔」后处理（默认开启）：大小周期笔完全重叠时大周期线重建置顶，
 *                   并在笔中点标记「同笔60=15」文本（详见主流程同笔后处理注释）
 */
const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");
// 缠论算法核心（唯一算法源，与 mark-buy-sell SKILL 共用）
const core = require("../../chan-core/scripts/chan_core.js");
const {
  markWickBars, mergeBars, findFractals, countRaw, hasGapBetween, buildBi, fixBiExtremes, lockedPivotsOf, alignBiToUpper,
  calcATR, calcMACD, hasMacdCrossBetween, isSameAsUpperBi,
  extendLastBi, lowerResOf, calibrateBiTimes, intervalSecOf, nearDoubleOn,
} = core;

// 笔数据落盘目录（mark-buy-sell SKILL 强制从此读取，实现「画笔 → 标记」数据依赖）
const CACHE_DIR = process.env.CHAN_CACHE_DIR || path.join(__dirname, "..", "..", "..", "..", ".cursor", "cache");
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
// 长影标记参数（见 chan-core markWickBars）：影线占比阈值 + 绝对长度下限 wickMinLen
// （2026-10-02 起为具体数值，原 --wick-atr 系数口径废除）+ 前提约束 wickMinRange
// （整根K线价差须大于该值才判插针压平）
core.CHAN_CFG.wickRatio = getArg("wick-ratio", core.CHAN_CFG.wickRatio);
core.CHAN_CFG.wickMinLen = getArg("wick-len", core.CHAN_CFG.wickMinLen);
core.CHAN_CFG.wickMinRange = getArg("wick-range", core.CHAN_CFG.wickMinRange);
// 参数中心（WEB 参数配置页）整体覆盖：在上述单键 CLI 之后应用、优先级更高；
// 未知键在 JS 侧闲置无害（Python 回测引擎的扩展键如 sinkFallback 不在本脚本读取）
const CHAN_CFG_JSON = getStrArg("chan-cfg", "");
if (CHAN_CFG_JSON) {
  try { Object.assign(core.CHAN_CFG, JSON.parse(CHAN_CFG_JSON)); }
  catch (e) { console.log("警告: --chan-cfg JSON 解析失败，忽略该参数"); }
}
// 参数中心自动跟随（2026-10-02）：未显式传 --chan-cfg 时，读取参数中心保存时导出的
// 当前品种有效配置（py_chain/param_center 写 .cursor/cache/chan_cfg_<品种>.json）。
// 候选文件依次：品种后缀（参数中心桶 id，如 OANDA:XAUUSD → XAUUSD）→ 完整品种名
// sanitized；都不存在则用代码默认（与 chan_cfg_effective「未列品种走默认」语义一致）。
// --debug 是运行诊断开关，不受参数中心覆盖；单键 CLI（--gap 等）先于此应用、
// 参数中心优先级更高（与 --chan-cfg 同序）。
function loadParamCenterCfg(symbol) {
  if (CHAN_CFG_JSON) return; // 显式 --chan-cfg 优先（Web 工作台 analysis_service 路径）
  const sanitize = (s) => String(s).replace(/[^A-Za-z0-9_.-]/g, "_");
  const cands = [];
  const colon = String(symbol).lastIndexOf(":");
  if (colon >= 0) cands.push(String(symbol).slice(colon + 1));
  cands.push(sanitize(symbol));
  for (const c of cands) {
    if (!c) continue;
    const f = path.join(CACHE_DIR, `chan_cfg_${sanitize(c)}.json`);
    if (!fs.existsSync(f)) continue;
    try {
      const payload = JSON.parse(fs.readFileSync(f, "utf8"));
      Object.assign(core.CHAN_CFG, payload.cfg || payload);
      core.CHAN_CFG.debug = DEBUG;
      console.log(`已应用参数中心配置: ${path.basename(f)}`);
      return;
    } catch (e) {
      console.log(`警告: 参数中心配置 ${path.basename(f)} 解析失败，忽略（${e.message}）`);
    }
  }
}
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
// --with-30s：追加 30 秒级别（30S 只计算落盘、不绘制，见 COMPUTE_ONLY）
const WITH_30S = args.includes("--with-30s");
// --closed：只用已收盘K线（bar.time + 周期间隔 <= 当前时刻），过滤未收盘当根。
// 默认含当根（实盘看图反应更快）；--closed 用于与回测引擎精确对照（回测严格只用收盘数据）。
const CLOSED_ONLY = args.includes("--closed");
const PERIODS = getStrArg("periods", "D,240,60,15,3")
  .split(",").map(s => s.trim()).filter(Boolean);
if (WITH_30S && !PERIODS.includes("30S")) PERIODS.push("30S");

// --no-tongbi：关闭「同笔」后处理（默认开启）。大小周期笔完全重叠（同笔，判定与
// 买卖点「同笔例外」同口径，见 chan-core isSameAsUpperBi）时，把大周期的线
// 「先建新、验通过、再删旧」重建置顶（TradingView 后创建的 shape 渲染在上层），
// 并在笔中点画「同笔60=15」文本标记。
const NO_TONGBI = args.includes("--no-tongbi");
// 同笔标记的 shape 标签（清理与识别用；与笔的 CHAN_BI_<res> 标签互不干扰）
const TB_TITLE = "CHAN_BI_TB";

// 内层窗口最小K线数：锚点范围内K线不足时向前扩展
const MIN_WINDOW_BARS = 20;
// 内层计算缓冲：锚点前额外取的K线数，保证窗口起点处能形成完整分型
const ANCHOR_BUFFER = 30;

// 小周期只加载并绘制最近 N 天的笔：默认 3分钟15天、15分钟30天、30秒3天（30秒密度是
// 3 分钟的 6 倍，窗口 3 天 ≈5.5k 根，与 3分钟×15天 同量级）。天数来自参数中心
// CHAN_CFG.windowDays*（--chan-cfg 已在上方合并进 core.CHAN_CFG，须在此之后构造）；
// 值 0 = 该周期不限窗口（与 60m/240m/D 同口径，从 --from 全量）。
// 动机：避免从起始日期到最新的全部笔堆叠导致图上过密，同时缩小加载量、
// 避免 3 分钟为覆盖起始日期加载数月完整历史而超时。
const DRAW_WINDOW_DAYS = {};
// 窗口天数须在「参数合并完成后」取值：--chan-cfg 在文件头部已并入 core.CHAN_CFG，
// 而 CLI 自动跟随（loadParamCenterCfg）要等连上 CDP 拿到品种后才应用——若只在
// 此处构建一次，CLI 路径会用代码默认 30/15/3 而非参数中心的 windowDays*（WEB
// 路径无此问题）。故封装重建函数，247 行参数中心应用后重调一次。
const rebuildDrawWindows = () => {
  for (const k of Object.keys(DRAW_WINDOW_DAYS)) delete DRAW_WINDOW_DAYS[k];
  if (core.CHAN_CFG.windowDays3 > 0) DRAW_WINDOW_DAYS['3'] = core.CHAN_CFG.windowDays3;
  if (core.CHAN_CFG.windowDays15 > 0) DRAW_WINDOW_DAYS['15'] = core.CHAN_CFG.windowDays15;
  if (core.CHAN_CFG.windowDays30S > 0) DRAW_WINDOW_DAYS['30S'] = core.CHAN_CFG.windowDays30S;
};
rebuildDrawWindows();

// 只计算不绘制的周期：30秒笔太密不画在图上，仅计算并落盘到 bis_<品种>.json，
// 供 mark-entry --with-30s 的「以下级别背驰」检测使用（笔计算/锚定/窗口过滤全部照常）。
const COMPUTE_ONLY = new Set(["30S"]);

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
    case "30S":  return "#FF6D00";  // 橙色（30秒只计算落盘不绘制，此颜色仅用于日志展示）
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

/**
 * 同笔分组：检测相邻周期间「完全重叠」的笔，并链式合并成组。
 * periodsMap：key=周期，value=笔数组（本次运行 allBis 与旧缓存合并后的视图，
 *   见主流程「同笔后处理」）；ladder：周期阶梯（大到小，如 D,240,60,15,3）。
 * 只在阶梯相邻两级间检测：非相邻周期的笔受可见范围限制永不同屏，无重叠置顶需求；
 * COMPUTE_ONLY 周期（30S）不绘制，同样跳过。
 * 判定复用 chan-core isSameAsUpperBi（±1 根低级别 bar 时间 / ±0.01 价格），
 * 与买卖点「同笔例外」同口径。链式合并：3=15 且 15=60 → 一组 {60,15,3}。
 * 返回 [{ members: [{res, bi}] }]，members 大到小（members[0] = 组内最大周期笔），
 * 组间按「组内最大周期」升序排列（小周期组先处理，后处理重建后大周期线最后创建）。
 */
function buildTongBiGroups(periodsMap, ladder) {
  const groupOfBi = new Map(); // 笔对象 → 所属组（链式关联：下级命中上级笔时并入上级所在组）
  const groups = [];
  for (let i = 0; i + 1 < ladder.length; i++) {
    const upper = String(ladder[i]), lower = String(ladder[i + 1]);
    if (COMPUTE_ONLY.has(upper) || COMPUTE_ONLY.has(lower)) continue;
    const upperBis = periodsMap[upper], lowerBis = periodsMap[lower];
    if (!upperBis || !lowerBis) continue;
    const lowerSec = intervalSecOf(lower) || 900;
    for (const bi of lowerBis) {
      const hit = isSameAsUpperBi(bi, upperBis, lowerSec);
      if (!hit) continue;
      let g = groupOfBi.get(hit);
      if (!g) {
        g = { members: [{ res: upper, bi: hit }] };
        groups.push(g);
        groupOfBi.set(hit, g);
      }
      // 同一下级周期只入组一次（防同一上级笔被相邻两根下级笔同时命中的极端情况）
      if (!g.members.some(m => m.res === lower)) {
        g.members.push({ res: lower, bi });
        groupOfBi.set(bi, g);
      }
    }
  }
  // 组间按「组内最大周期」升序（小周期组先处理，后处理重建时大周期组的线最后创建，
  // 组间叠放同样保持大周期在上）。注意 ladder 本身从大到小排列，不能直接用其下标排序。
  groups.sort((a, b) => (intervalSecOf(a.members[0].res) || 0) - (intervalSecOf(b.members[0].res) || 0));
  return groups;
}

/**
 * 多周期可见范围的并集（同笔标记用）：组内任一条线可见的图表周期上，标记也可见。
 * 各周期 intervalVisibility 是「本周期+低一级」的连续阶梯区间，同组相邻成员的并集仍连续。
 * 某大类（seconds/minutes/...）只有在至少一个成员开启时才参与 from/to 合并——
 * 关闭成员的模板默认值（如 minutes 1..59）不得污染范围。
 */
function unionIntervalVisibility(resList) {
  const cfgs = resList.map(intervalVisibility).filter(Boolean);
  if (cfgs.length === 0) return null;
  if (cfgs.length === 1) return cfgs[0];
  const CLASSES = ["seconds", "minutes", "hours", "days", "weeks", "months"];
  const out = { ticks: cfgs.some(c => c.ticks) };
  for (const cls of CLASSES) {
    const on = cfgs.filter(c => c[cls]);
    out[cls] = on.length > 0;
    out[cls + "From"] = on.length ? Math.min(...on.map(c => c[cls + "From"])) : 1;
    out[cls + "To"] = on.length ? Math.max(...on.map(c => c[cls + "To"])) : 1;
  }
  return out;
}

// ============================================================
// 主流程
// ============================================================

(async () => {
  let client;
  try {
    // 多标签场景：可用 --page=<url片段> 指定操作的图表页面（如 --page=urA9iDWS 按 /chart/urA9iDWS 匹配），
    // 未指定时取列表第一个 tradingview.com 页面（与历史行为一致）
    const PAGE_MATCH = getStrArg("page", "");
    const targets = await CDP.List({ port: 9222 });
    const tvs = targets.filter(t => t.type === "page" && t.url.includes("tradingview.com"));
    let pg = tvs.find(t => PAGE_MATCH && t.url.includes(PAGE_MATCH)) || null;
    if (!pg && !PAGE_MATCH) pg = tvs[0] || null;
    if (!pg) { console.log("ERROR: 未找到 TradingView 页面" + (PAGE_MATCH ? `（匹配 ${PAGE_MATCH}）` : "")); process.exit(1); }
    if (PAGE_MATCH) console.log("目标页面:", pg.url.split("/chart/")[1] || pg.url);
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
    // 参数中心配置在品种确定后、周期构建前应用（fractalSideRealWick / nearDouble* 等）；
    // 应用后须重建小周期绘制窗口（windowDays* 可能被参数中心覆盖，见 rebuildDrawWindows 注释）
    loadParamCenterCfg(SYMBOL);
    rebuildDrawWindows();

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

    const fetchBars = async (expectedIntervalSec, fromTs, buffer, windowDays) => {
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
            const windowDays = ${JSON.stringify(windowDays || null)};
            const tolerance = ${JSON.stringify(tolerance)};
            // 小周期窗口：覆盖目标从 fromTs 改为 max(fromTs, 最新K线时间 - windowDays*86400)，
            // 只要求加载最近 N 天数据即可通过覆盖检查，避免为覆盖起始日期加载数月完整历史而超时
            const latestTs = bars.length ? bars[bars.length - 1].time : 0;
            const effFrom = (fromTs && windowDays) ? Math.max(fromTs, latestTs - windowDays * 86400) : fromTs;
            if (effFrom) {
              // 数据起点仍晚于目标日期较多（尚未覆盖）→ 返回 notCovered，触发历史加载
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

    // 从 bars.db（回放深拉历史库，py_chain/data_store 维护）读取 [fromTs, toTs] 段K线，
    // 供校准基准补齐图表加载不到的更早小周期历史——TV 图表 3m 深度仅约 2 个月，
    // 回放深拉库可到多年前（WEB 基础数据页 / python -m py_chain.data_store 拉取）。
    // python 不可用、库缺段或读库异常时返回空数组，调用方退化为纯图表基准。
    const fetchStoreBars = (res, fromTs, toTs) => {
      if (fromTs === null || fromTs === undefined || !toTs || toTs <= fromTs) return [];
      const root = path.join(__dirname, "..", "..", "..", "..");
      const script = [
        "import sys, json",
        `sys.path.insert(0, ${JSON.stringify(root)})`,
        "from py_chain import data_store",
        `r = data_store.query_bars(${JSON.stringify(String(SYMBOL))}, ${JSON.stringify(String(res))},`,
        `    int(${Math.floor(fromTs)}), to_ts=int(${Math.floor(toTs)}), limit=200000, order='asc')`,
        "print(json.dumps(r.get('rows') or []))",
      ].join("\n");
      try {
        const { spawnSync } = require("child_process");
        const p = spawnSync("python", ["-c", script],
          { cwd: root, encoding: "utf8", timeout: 90000, windowsHide: true,
            maxBuffer: 64 * 1024 * 1024 }); // 全窗口 3m 可达数万根（~数 MB JSON），默认 1MB 会截断
        if (p.status !== 0 || !p.stdout) {
          if (DEBUG) console.log(`[校准基准] bars.db 读取失败（status=${p.status} ${String(p.stderr || "").slice(0, 200)}），退化为图表数据`);
          return [];
        }
        const rows = JSON.parse(p.stdout);
        return Array.isArray(rows)
          ? rows.filter(b => b && typeof b.time === "number" && typeof b.high === "number" && b.high >= b.low)
          : [];
      } catch (e) {
        if (DEBUG) console.log(`[校准基准] bars.db 读取异常（${e.message}），退化为图表数据`);
        return [];
      }
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
     * 因此绘制前检查图表第一根K线是否 <= 最早笔时间，若未覆盖则 scrollToFirstBar 加载完整历史。
     * 数据分批推进可能在批次之间停顿数秒，单轮「len+first 连续不变」的稳定判据会提前退出
     * （曾致 60分钟笔在 15分钟图上创建时大面积吸附成无效笔），因此最多进行 3 轮
     * 「scrollToFirstBar + 等待」，每轮稳定后重触发滚动继续加载。
     * 返回 { covered, first }：covered=false 时调用方必须裁剪超范围笔（drawClipped），
     * 绝不能在未覆盖的图上创建 shape。
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
      if (cur.first !== null && cur.first <= minTs) return { covered: true, first: cur.first }; // 已覆盖
      if (DEBUG) console.log(`[数据覆盖] ${res} 首根K线 ${toT(cur.first)} 晚于最早笔 ${toT(minTs)}，加载完整历史...`);
      let covered = false;
      for (let round = 0; round < 3 && !covered; round++) {
        if (round > 0 && DEBUG) console.log(`[数据覆盖] ${res} 第 ${round + 1} 轮重试加载（当前首根 ${toT(cur.first)}）...`);
        await scrollToFirstBar();
        let prevLen = cur.len;
        let prevFirst = cur.first;
        let stableCnt = 0;
        // 每轮最多 60×1.2s：覆盖即停；「len+first 连续 3 次不变」只结束本轮（数据停顿
        // 不代表加载完成），由外层再触发滚动重试，总计约 3.5 分钟上限
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
            } catch(e) { created.push(null); out.bi_err.push(e.message); } // null 占位保持与 BIS 索引对齐
          }

          return { ...out, created_ids: created };
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };

    // ============================================================
    // 创建后回读校验辅助：按 id 读回已创建笔的端点 / 按 id 批量删除
    // （创建成功 ≠ 端点正确：TradingView 会把超出数据范围的时间静默吸附到数据边缘）
    // ============================================================
    // replayOk=true 时表达式带回放守卫白名单前缀（回放补绘流程内使用）
    const readStrokesByIds = async (ids, replayOk) => {
      const r = await client.Runtime.evaluate({
        expression: `${replayOk ? RP : ""}(function() {
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

    const removeShapesByIds = async (ids, replayOk) => {
      if (!ids || ids.length === 0) return;
      await client.Runtime.evaluate({
        expression: `${replayOk ? RP : ""}(function() {
          const chart = TradingViewApi.activeChart();
          for (const id of ${JSON.stringify(ids)}) { try { chart.removeEntity(id); } catch (e) {} }
          return 'ok';
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 20000,
      });
    };

    // ============================================================
    // 回放定位补绘：把「超出图表深度」的笔分段画上（与基础数据页回放深拉同路径）
    // 小周期图表深度（3m 约 2 个月）盖不住 15m 150 天窗口的早期笔——进入回放模式、
    // 定位到该批最晚端点（回放数据 = 定位点之前的历史）、scrollToFirstBar 翻页扩出
    // 更早段，在数据范围内创建笔；全部批次完成后退出回放。shape 锚点持久且两种模式
    // 下一致（已实测：退出回放后读回端点与落盘分毫不差），用户回放回看早期行情时
    // 笔可见且位置正确。回放不可用/定位失败等任何异常都只降级为「图上不绘制」，
    // 不影响落盘与 job 结果。
    // ============================================================
    // 回放定位补绘专用 evaluate 前缀：analysis_bridge 的回放态守卫见此前缀放行
    // （否则进回放后第一个 evaluate 就被 ANALYSIS_REPLAY_ACTIVE fail-fast 杀进程）
    const RP = "/*CHAN_REPLAY_OK*/";

    const replayCall = (call, argsJs, awaitIt) => {
      const expr = `${RP}(function() {
        const ra = window.TradingViewApi._replayApi;
        if (!ra) return { error: 'no_replay_api' };
        const t = (ra && typeof ra === 'object' && typeof ra.value === 'function') ? ra.value() : ra;
        if (!t || typeof t.${call} !== 'function') return { error: 'no_method_${call}' };
        try {
          const v = t.${call}(${argsJs || ''});
          ${awaitIt ? `return (v && typeof v.then === 'function')
            ? v.then(function(u){ return { value: u }; }).catch(function(e){ return { error: String(e) }; })
            : { value: v };` : `return { value: v };`}
        } catch (e) { return { error: String(e) }; }
      })()`;
      return client.Runtime.evaluate({
        expression: expr, returnByValue: true, awaitPromise: true, timeout: 30000,
      }).then(r => (r.result && r.result.value) || { error: 'no_result' });
    };

    // 当前图表已加载K线范围（首根/末根/数量/末根收盘）——回放态数据沉降与推进判定
    const readBarsRange = async () => {
      const r = await client.Runtime.evaluate({
        expression: `${RP}(function() {
          const c = TradingViewApi.activeChart();
          const items = c.chartModel().mainSeries().data().m_bars._items;
          if (!items || !items.length) return null;
          const f = items[0].value, l = items[items.length - 1].value;
          return { first: f[0], last: l[0], len: items.length, lastClose: l[4] };
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 15000,
      });
      return (r.result && r.result.value) || null;
    };

    const scrollFirstBar = async () => {
      await client.Runtime.evaluate({
        expression: `${RP}(function(){ const c=TradingViewApi.activeChart();
          const w=c._chartWidget||(c.chartModel&&c.chartModel()._chartWidget);
          const ts=(w&&w.model)?w.model().timeScale():c.chartModel().timeScale();
          ts.scrollToFirstBar(); return 'ok'; })()`,
        returnByValue: true, awaitPromise: true, timeout: 15000,
      });
    };

    const readShapePoints = async (id) => {
      const r = await client.Runtime.evaluate({
        expression: `${RP}(function(){
          const sh = TradingViewApi.activeChart().getShapeById(${JSON.stringify(id)});
          const pts = sh && sh._source && sh._source._points;
          return (pts && pts.length >= 2)
            ? [{ time: pts[0].time, price: pts[0].price }, { time: pts[1].time, price: pts[1].price }] : null;
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 15000,
      });
      return (r.result && r.result.value) || null;
    };

    // 只创建（不清除旧笔），time 传「落盘时间 + comp」——回放态 createMultipointShape
    // 对传入时间做一次「墙钟」解释（实测偏移=本地时区），comp 由探针动态探测，
    // 不依赖时区假设
    const createShapesOnly = async (res, bis, comp) => {
      const BI_COLOR = resolutionColor(res);
      const BI_TITLE = "CHAN_BI_" + res;
      const IV_CFG = intervalVisibility(res);
      const r = await client.Runtime.evaluate({
        expression: `${RP}(async function() {
          const chart = TradingViewApi.activeChart();
          const BIS = ${JSON.stringify(bis)};
          const COMP = ${JSON.stringify(comp || 0)};
          const BI_COLOR = "${BI_COLOR}";
          const BI_TITLE = "${BI_TITLE}";
          const IV_CFG = ${JSON.stringify(IV_CFG)};
          const out = { bi_ok: 0, bi_err: [] };
          const created = [];
          const applyIV = (id) => {
            if (!IV_CFG) return;
            try {
              const iv = chart.getShapeById(id)._source._properties.intervalsVisibilities;
              iv.ticks.setValue(IV_CFG.ticks);
              iv.seconds.setValue(IV_CFG.seconds);
              iv.minutesFrom.setValue(IV_CFG.minutesFrom);
              iv.minutesTo.setValue(IV_CFG.minutesTo);
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
          for (const b of BIS) {
            try {
              const id = await chart.createMultipointShape(
                [{ time: b.startTime + COMP, price: b.startPrice }, { time: b.endTime + COMP, price: b.endPrice }],
                { shape: 'polyline', lock: false, overrides: { linecolor: BI_COLOR, linewidth: 1, title: BI_TITLE } });
              applyIV(id);
              created.push(id);
              out.bi_ok++;
            } catch(e) { created.push(null); out.bi_err.push(e.message); } // null 占位保持与 BIS 索引对齐
          }
          return { ...out, created_ids: created };
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 120000,
      });
      return (r.result && r.result.value) || { bi_ok: 0, bi_err: ['no_result'], created_ids: [] };
    };

    // 锚点校验：TradingView 存储的 shape 锚点是「墙钟时间」（bar UTC ts + 图表时区偏移
    // tzOff，实测约 +8h），渲染时再减回——故比对前须先减 tzOff。tzOff 由探针动态测出
    // （回放补绘流程）；实时段主流程沿用原「紧跟创建读请求值」的竞态口径，不传 tzOff
    const verifyShapes = async (ids, bis, tol, replayOk, tzOff) => {
      const off = tzOff || 0;
      const ptsArr = await readStrokesByIds(ids, replayOk);
      const bad = [];
      for (let i = 0; i < ids.length; i++) {
        const p = ptsArr[i];
        if (!p || !p[0] || !p[1]) continue; // 端点读不到 → 未校验，不误判
        const b = bis[i];
        const tBad = Math.abs(p[0].time - off - b.startTime) > tol || Math.abs(p[1].time - off - b.endTime) > tol;
        const pBad = Math.abs(p[0].price - b.startPrice) > 0.01 || Math.abs(p[1].price - b.endPrice) > 0.01;
        if (tBad || pBad) bad.push({ id: ids[i], bi: b });
      }
      return bad;
    };

    const drawClippedViaReplay = async (res, drawRes, clippedBis) => {
      const out = { created: 0, failed: 0, skipped: 0, rounds: 0, reason: '', fallbackBis: [], tzOff: 0 };
      const lowerSec = intervalSecOf(drawRes) || 60;
      const tol = intervalSecOf(drawRes) || 1;
      let inReplay = false;
      try {
        const avail = await replayCall('isReplayAvailable');
        if (!avail || avail.error || !avail.value) { out.reason = 'replay API 不可用'; return out; }
        const shown = await replayCall('showReplayToolbar', null, true);
        if (shown && shown.error) { out.reason = '打开回放工具栏失败: ' + shown.error; return out; }

        let pending = [...clippedBis].sort((a, b) => a.endTime - b.endTime);
        let lastCoverFrom = Infinity;
        for (let round = 0; round < 12 && pending.length > 0; round++) {
          out.rounds = round + 1;
          const targetTs = pending[pending.length - 1].endTime;   // 剩余笔最晚端点
          const anchor = targetTs + lowerSec;                     // 定位到其后一根 bar：回放数据=定位点之前历史
          const sel = await replayCall('selectDate', String(anchor * 1000), true);
          if (sel && sel.error) { out.reason = '回放定位失败: ' + sel.error; break; }
          inReplay = true;
          // 数据沉降：末根须盖住最晚端点（否则创建会被吸附到数据边缘）
          let range = null;
          for (let i = 0; i < 14; i++) {
            await sleep(3000);
            range = await readBarsRange();
            if (range && range.last >= targetTs) break;
          }
          if (!range || range.last < targetTs) { out.reason = '回放数据未沉降'; break; }
          // 翻页扩出更早历史：滚到「首根连续两轮不前移」或覆盖剩余最早笔
          const needMin = pending.reduce((m, b) => Math.min(m, b.startTime), Infinity);
          let stable = 0, first = range.first;
          for (let i = 0; i < 30 && first > needMin; i++) {
            await scrollFirstBar();
            await sleep(4000);
            const r2 = await readBarsRange();
            if (!r2) break;
            if (r2.first >= first) { stable++; if (stable >= 2) break; }
            else { stable = 0; first = r2.first; }
          }
          range = (await readBarsRange()) || range;
          const coverFrom = range.first;
          const batch = pending.filter(b => b.startTime >= coverFrom && b.endTime >= coverFrom);
          if (batch.length === 0) {
            if (coverFrom >= lastCoverFrom) {
              out.reason = `回放数据最早到 ${toT(coverFrom)}`;
              out.skipped = pending.length;
              break;
            }
            lastCoverFrom = coverFrom;
            continue;
          }
          lastCoverFrom = coverFrom;
          // 墙钟偏移探针：在末根 bar（必在数据内、且是 bar 边界）创建临时线段，
          // 存储-请求 = 图表时区偏移 tzOff（shape 锚点按墙钟存储、渲染时减回，
          // 校验与创建后比对都用它换算）。创建一律直传落盘 ts——字面命中存在的
          // bar，不补偿（曾按 -tzOff 补偿导致存储值缺偏移、渲染整体偏 8 小时，
          // 且补偿值常落进休市/周末断档引发吸附，两坑均已实测）
          const probe = await client.Runtime.evaluate({
            expression: `${RP}(async function(){
              const chart = TradingViewApi.activeChart();
              const id = await chart.createMultipointShape(
                [{ time: ${range.last - lowerSec}, price: ${range.lastClose} }, { time: ${range.last}, price: ${range.lastClose} }],
                { shape: 'polyline', lock: false, overrides: { linecolor: '#000000', linewidth: 1, title: 'REPLAY_PROBE' } });
              return id;
            })()`,
            returnByValue: true, awaitPromise: true, timeout: 20000,
          }).then(r => r.result && r.result.value).catch(() => null);
          let tzOff = null;
          if (probe) {
            await sleep(2500);
            const pts = await readShapePoints(probe);
            if (pts && pts[1] && typeof pts[1].time === 'number') tzOff = pts[1].time - range.last;
            await removeShapesByIds([probe], true);
          }
          if (tzOff === null) {
            if (typeof out.tzOff === 'number' && out.tzOff !== 0) tzOff = out.tzOff; // 复用上一轮
            else { out.reason = '墙钟偏移探针失败'; break; }
          }
          out.tzOff = tzOff;

          const created = await createShapesOnly(res, batch, 0);
          const ids = created.created_ids || [];
          out.created += created.bi_ok || 0;
          // 吸附是创建后异步生效的：紧跟创建立即读回会得到「请求值」（竞态放行），
          // 必须等吸附沉降后再校验（实测 ~2s 后 _points 已更新为存储值）
          await sleep(2500);
          let bad = await verifyShapes(ids, batch, tol, true, tzOff);
          if (bad.length > 0) {
            await removeShapesByIds(bad.map(x => x.id), true);
            out.created -= bad.length;
            const retryBis = bad.map(x => x.bi);
            const retry = await createShapesOnly(res, retryBis, 0);
            out.created += retry.bi_ok || 0;
            await sleep(2500);
            const bad2 = await verifyShapes(retry.created_ids || [], retryBis, tol, true, tzOff);
            if (bad2.length > 0) {
              await removeShapesByIds(bad2.map(x => x.id), true);
              out.created -= bad2.length;
              // 目标 bar 在回放数据中不存在（数据地板/加载边缘）时无法精确锚定——
              // 收集起来退出回放后由调用方在源周期实时图上重建（吸附到源周期 bar
              // 边界，误差 ≤1 根源周期 K 线）
              out.fallbackBis.push(...bad2.map(x => x.bi));
            }
          }
          const batchSet = new Set(batch);
          pending = pending.filter(b => !batchSet.has(b));
        }
        out.skipped += pending.length;
        if (pending.length > 0 && !out.reason) out.reason = '达轮次上限';
      } catch (e) {
        // 任何异常（含守卫拦截）都降级为「图上不绘制」：落盘数据完整，不炸进程
        const msg = String(e && e.message ? e.message : e).replace(/\s+/g, ' ').slice(0, 120);
        out.reason = out.reason || ('回放补绘异常: ' + msg);
      } finally {
        if (inReplay) {
          try {
            await replayCall('stopReplay', null, true);
            for (let i = 0; i < 10; i++) {
              await sleep(2000);
              const s = await replayCall('isReplayStarted');
              if (!s || !s.value) break;
            }
          } catch (e) { /* 退出尽力而为 */ }
        }
      }
      return out;
    };

    // 周期字符串归一化（chart.resolution() 对日线可能返回 "1D"，与我们的 "D" 等价）
    const normRes = (r) => {
      const s = String(r).toUpperCase();
      return s === "1D" ? "D" : s === "1W" ? "W" : s;
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
    // 各基准周期实际覆盖到的最早时间（key=周期）：用于绘制裁剪时判断「被裁笔的端点
    // 是否已在校准基准覆盖内」（bars.db 回放库拼接成功 → 只是图画不上，数据完整）
    const refCoverFrom = {};
    const allRawBars = {};
    const allBis = {}; // 收集各周期最终绘制的笔（含校准后的端点时间），循环结束后落盘供 mark-buy-sell 读取

    let prevBis = null;           // 上一周期的笔（用于确定下一层的锚点）
    for (let pi = 0; pi < PERIODS.length; pi++) {
      const res = PERIODS[pi];
      if (res !== currentRes) {
        await ensureResolution(res);
        currentRes = res;
      }

      const d = await fetchBars(intervalSecOf(res), FROM_TS, ANCHOR_BUFFER, res === '15' ? undefined : DRAW_WINDOW_DAYS[res]);
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

      // --closed 对照口径：剔除未收盘当根（与回测引擎一致），15m 逐级校准基准数据同理
      const nowSec = Math.floor(Date.now() / 1000);
      const rawBars = CLOSED_ONLY
        ? windowBars.filter(k => k.time + intervalSecOf(res) <= nowSec)
        : windowBars;
      allRawBars[res] = rawBars;
      // ATR/MACD 基于未剔除的原始K线计算（ATR 是波动率度量，插针也属波动；
      // MACD 用收盘价序列，不受长影剔除影响）
      const atr = calcATR(rawBars, 14);
      const macdArr = calcMACD(rawBars);
      // 长影标记（chan-core markWickBars）：影线占比 >= 70% 且 >= 0.5*稳定ATR（全窗口TR均值）
      // 的冲高/探底插针打 _wickTop/_wickBottom 标记——bar 原值保留（影线可成端点，
      // 如 60m 7-16 02:00 的 4081.52 成为反弹笔顶），仅影线不参与区间内竞争
      //（避免插针污染笔区间阻止合法分型成笔；稳定ATR避免结果随局部行情抖动）
      const trimmedBars = markWickBars(rawBars);
      const merged = mergeBars(trimmedBars);
      const fractals = findFractals(merged);
      // 区间套强制对齐：把上一层（更高级别）笔的端点作为锁定端点传入 buildBi，
      // 保证本级别笔端点与上级笔的极值端点严格重合（优先级最高）。
      const lockedPivots = lockedPivotsOf(prevBis);
      // 60m 端点时间校准需要完整 15m 基准（refCache['15'] 预热；显示窗口不限制计算输入）。
      if (res === '60') {
        await ensureResolution('15');
        const lower = await fetchBars(900, FROM_TS || rawBars[0].time, ANCHOR_BUFFER);
        if (lower && !lower.error && lower.bars?.length) {
          const lowerBars = CLOSED_ONLY
            ? lower.bars.filter(k => k.time + 900 <= nowSec)
            : lower.bars;
          refCache['15'] = markWickBars(lowerBars);
          refCoverFrom['15'] = refCache['15'][0].time;
        }
        await ensureResolution(res);
        currentRes = res;
      }

      // 近等双顶/双底平台取后顶/后底：按 nearDoubleOn(res) 每周期开关（2026-10-02 起
      // 容差=nearDoubleFixed，先影线后实体；15m双动能补充确认已取消）
      let bis = buildBi(fractals, merged, atr, macdArr, lockedPivots, nearDoubleOn(res), res);

      // 端点极值修正：包含合并可能吞掉更极端的插针低点/高点（如 60分钟 7-29 09:00 的 4010.41 被
      // 08:00/09:00 的向上合并吞掉），把笔终点平移到区间内被掩盖的真实极值，使笔终点落在真实极值K线上
      bis = fixBiExtremes(bis, merged);

      // ATR 过滤（用全窗口稳定 ATR，不用 calcATR 的尾部 14 根——9-4 行情急涨使 15m
      // 尾部 ATR 从 ~17.4 涨到 21+，把 9-3 06:06→07:36 的结构性下跌笔（幅度 10.59）
      // 误判为噪音剔除，06:06 顶 4391.835 随之消失。结构与行情无关，阈值不应随行情漂移；
      // 与 chan-core markWickBars 的稳定基准同理（SPEC §2.0））
      let stableAtr = 0;
      if (rawBars.length > 1) {
        let trSum = 0;
        for (let i = 1; i < rawBars.length; i++) {
          const h = rawBars[i].high, l = rawBars[i].low, pc = rawBars[i - 1].close;
          trSum += Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
        }
        stableAtr = trSum / (rawBars.length - 1);
      }
      const threshold = stableAtr * ATR_FILTER;
      const beforeFilter = bis.length;
      bis = bis.filter(b => b.span >= threshold);
      const filteredOut = beforeFilter - bis.length;

      // 内层只画「结束时间在锚点之后」的笔（锚点前的缓冲仅用于保证分型完整）
      let drawBis = bis;
      if (anchorStart !== null) {
        drawBis = bis.filter(b => b.endTime >= anchorStart);
      }

      // 未完成笔延伸：最后一笔若未推进到当前K线（如末端单调上涨/下跌无新分型），
      // 延伸到窗口内该方向上的最新极端价所在K线（trimmedBars：延伸不能指向已剔除的插针价）
      drawBis = extendLastBi(drawBis, trimmedBars);

      // 逐级端点时间校准：用「低一级」周期K线校准本周期笔的端点时间，
      // 使不同周期对同一极值的标记位置在图上重合
      //   （15分钟用3分钟校准，1小时用15分钟校准，4小时用1小时校准，日线用4小时校准）
      const lowerRes = lowerResOf(res);
      // 校准基准需求起点（= 绘制窗口起点；跨窗口起笔的笔头可早于此）。
      // 提升到 if (lowerRes) 外声明：下方绘制裁剪的分级判定（refOk）也要用
      let refNeedFrom = 0;
      if (lowerRes) {
        let refBars = refCache[lowerRes];
        // 校准基准需求起点：跟随「被校准周期」的绘制窗口（如 15m 窗 150 天 → 3m 基准
        // 同样覆盖 150 天），而非基准周期自身的画笔窗口（windowDays3 只管 3m 自己画几天的笔）；
        // 无窗口周期（60/240/D）维持旧语义：覆盖 --from 全量
        const lowerSec = intervalSecOf(lowerRes);
        const winDays = DRAW_WINDOW_DAYS[res];
        const latestTs = rawBars.length ? rawBars[rawBars.length - 1].time : 0;
        refNeedFrom = winDays
          ? Math.max(FROM_TS || 0, latestTs - winDays * 86400)
          : (FROM_TS || (rawBars.length ? rawBars[0].time : 0));
        // 用 bars.db 回放库补更早段：库末与图表首根间隔 ≤5 天（周末/假期口径，与
        // data_store.missing_segments 一致）视为无缝拼接；库无数据或有断层返回 null
        const mergeStorePrefix = (bars) => {
          if (!bars.length || bars[0].time <= refNeedFrom + lowerSec) return bars;
          const chartFirst = bars[0].time;
          const storeBars = fetchStoreBars(lowerRes, refNeedFrom, chartFirst + lowerSec);
          if (!storeBars.length) return null;
          if (storeBars[storeBars.length - 1].time < chartFirst - 5 * 86400) return null;
          const byTime = new Map(storeBars.map(b => [b.time, b]));
          for (const b of bars) byTime.set(b.time, b); // 同刻图表值优先（实时源更新）
          const merged = [...byTime.values()].sort((a, b) => a.time - b.time);
          console.log(`[校准基准] ${res} 用 ${lowerRes} 校准：图表自 ${toT(chartFirst)}，bars.db 回放库补前段 ${storeBars.length} 根（起点 ${toT(merged[0].time)}）`);
          return merged;
        };
        if (!refBars) {
          await ensureResolution(lowerRes);
          // 先取当前已加载段（不做覆盖深拉），更早历史从 bars.db 回放库拼接——TV 小周期
          // 图表深度仅约 2 个月（如 3m），回放深拉库可到多年前，常规路径秒级完成；
          // 库无数据/断层时退回旧的覆盖深拉路径（scrollToFirstBar 拉到图表极限）
          let dref = await fetchBars(lowerSec, null, 0);
          let chartBars = (dref && !dref.error && dref.bars) ? dref.bars : [];
          let merged = chartBars.length ? mergeStorePrefix(chartBars) : null;
          if (merged === null && (!chartBars.length || chartBars[0].time > refNeedFrom + 86400)) {
            dref = await fetchBars(lowerSec, refNeedFrom, ANCHOR_BUFFER);
            const deepBars = (dref && !dref.error && dref.bars) ? dref.bars : [];
            if (deepBars.length && (!chartBars.length || deepBars[0].time < chartBars[0].time)) {
              chartBars = deepBars;
              merged = mergeStorePrefix(chartBars);
              if (merged === null) merged = chartBars; // 深拉结果直接用（等价旧路径）
            }
          }
          const finalBars = merged || chartBars;
          if (finalBars && finalBars.length > 0) {
            if (finalBars[0].time > refNeedFrom + 86400) {
              console.log(`[校准基准] 提示: ${lowerRes} 基准仅到 ${toT(finalBars[0].time)}（图表深度限制且 bars.db 无更早数据；可在 WEB 基础数据页回放深拉补齐），更早的 ${res} 笔端点将不做低级别校准`);
            }
            // 校准基准同样做长影标记（markWickBars 内部用全窗口稳定 ATR），
            // 避免把已剔除的插针端点校准回插针时间
            refBars = markWickBars(finalBars);
            refCache[lowerRes] = refBars;
            refCoverFrom[lowerRes] = refBars[0].time;
            if (DEBUG) console.log(`[校准基准] ${res} 用 ${lowerRes} 校准，已加载 ${refBars.length} 根 ${lowerRes} 分钟K线`);
          }
          await ensureResolution(res);
          currentRes = res;
        }
        if (refBars) {
          drawBis = calibrateBiTimes(drawBis, trimmedBars, refBars, intervalSecOf(res));
        }
      }

      // 区间套强制对齐（优先级最高）：把本级别笔拐点对齐到紧邻上级笔拐点，
      // 使上级笔的极值端点在本级别中严格复现（上级底/顶=本级底/顶，同级同笔）。
      // 第4参 trimmedBars：幽灵端点防御——上级极值在本级K线中不存在（跨周期数据源聚合
      // 差异）时跳过对齐，保留本级别真实极值（须用长影剔除后的K线，否则 4443.715 这类插针
      // 会让上级极值被误判为"本级存在"而错误对齐）
      if (pi > 0 && prevBis && prevBis.length > 0) {
        drawBis = alignBiToUpper(drawBis, prevBis, intervalSecOf(PERIODS[pi - 1]), trimmedBars);
        // 对齐重建后的断口治理：alignBiToUpper 由「bis[i].start 覆盖 bis[i-1].end」重建端点，
        // 假设列表连续；若 ATR 过滤已删掉中间小笔，重建会把断口两端的笔「缝合」——
        // 前一笔终点被改写到后一笔起点（越过区间内真实极值，如 15m 9-3 连续两根上涨笔
        // 4364.52→4381.25→4440.355，4381.25 并非区间最高点）或产生低于阈值的
        // 「桥接小笔」（3m 8-21 4516.835→4517.98 幅度仅 1.14）。
        // ① 断口合并：相邻同向笔只可能来自被过滤中间笔的断口，合并为一笔
        //   （start=第一笔起点、end=第二笔终点），循环直到无连续同向；
        // ② 补幅度过滤：清除仍低于阈值的桥接残余。
        for (let i = drawBis.length - 2; i >= 0; i--) {
          if (drawBis[i].type === drawBis[i + 1].type) {
            const a = drawBis[i], b = drawBis[i + 1];
            drawBis[i] = {
              ...a,
              endTime: b.endTime,
              endPrice: b.endPrice,
              endIdx: b.endIdx,
              span: Math.abs(b.endPrice - a.startPrice),
              rawCount: a.rawCount + b.rawCount,
              gapLocked: a.gapLocked || b.gapLocked,
              macdCross: a.macdCross && b.macdCross,
            };
            drawBis.splice(i + 1, 1);
            if (DEBUG) console.log(`[断口合并] ${res} 合并连续同向笔 @${toT(a.startTime)}（对齐缝合断口）`);
            i++; // 合并后同位置再查一次（可能连续多段需合并）
          }
        }
        const reBefore = drawBis.length;
        drawBis = drawBis.filter(b => b.span >= threshold);
        if (DEBUG && reBefore !== drawBis.length) {
          console.log(`[对齐后补过滤] ${res} 清除 ${reBefore - drawBis.length} 根对齐缝合产生的桥接小笔`);
        }
      }

      // 小周期绘制窗口：只保留最近 N 天内结束的笔（窗口起点 = 最新K线时间往前推 N 天），
      // 用于 3分钟/15分钟 等小周期避免从起始日期到最新的全部笔堆叠导致图上过密。
      const winDays = DRAW_WINDOW_DAYS[res];
      if (winDays) {
        const latestTs = rawBars[rawBars.length - 1].time;
        const windowStart = latestTs - winDays * 86400;
        const beforeWin = drawBis.length;
        drawBis = drawBis.filter(b => b.endTime >= windowStart);
        if (DEBUG) console.log(`[绘制窗口] ${res} 只绘制最近 ${winDays} 天（${toT(windowStart)} 之后）的笔，过滤 ${beforeWin - drawBis.length} 根`);
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

      // 30秒等 COMPUTE_ONLY 周期：只计算落盘、不绘制（笔太密画图上不可读），
      // 也不做清除（从未画过就无残留）
      if (DRY || COMPUTE_ONLY.has(res)) continue;

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
      // 防抢占断言：运行期间用户可能手动切换图表周期，创建前确认图表仍在绘制周期，
      // 不一致则重新切换（否则 shape 会创建在错误周期的K线集合/数据范围上）
      const resNow = await client.Runtime.evaluate({
        expression: `String(TradingViewApi.activeChart().resolution())`,
        returnByValue: true, awaitPromise: true, timeout: 10000,
      });
      if (resNow.result && normRes(resNow.result.value) !== normRes(drawRes)) {
        console.log(`[周期 ${res}] 检测到图表被切到 ${resNow.result.value}，切回 ${drawRes} 再绘制`);
        await ensureResolution(drawRes);
        currentRes = drawRes;
      }
      // 绘制前确保图表数据覆盖最早笔的时间（切换周期后图表可能只加载最近K线，
      // 会导致较早笔的端点超出数据范围而被 TradingView 吸附到数据边缘，形成无效笔）
      let replayPendingBis = null; // 超出图表深度、待回放定位补绘的笔（见 drawClippedViaReplay）
      let replayStats = null;      // 回放补绘统计（并入绘制结果）
      if (drawBis.length > 0) {
        const minBiTime = drawBis.reduce(
          (m, b) => Math.min(m, b.startTime, b.endTime),
          Infinity
        );
        if (minBiTime !== Infinity) {
          const cover = await ensureBarsCover(drawRes, minBiTime);
          // 未覆盖则裁剪：只绘制两端点都落在已加载数据范围内的笔——超范围端点必然
          // 被吸附成无效笔，宁可跳过并警告。落盘数据不受影响（allBis 已保存完整列表）
          if (!cover.covered && cover.first !== null) {
            const before = drawBis.length;
            const clippedBis = drawBis.filter(b => b.startTime < cover.first || b.endTime < cover.first);
            drawBis = drawBis.filter(b => b.startTime >= cover.first && b.endTime >= cover.first);
            const skippedCount = before - drawBis.length;
            // 基准已覆盖被裁笔的端点（bars.db 回放库拼接成功）→ 裁剪只是「图上画不上」
            // （TradingView 小周期图表深度限制），落盘数据完整且端点已校准——降级为提示，
            // 不计入 report errors、不阻断「更新全部」后续步骤；基准也没覆盖到 → 维持
            // 原警告口径如实失败（先在 WEB 基础数据页回放深拉补齐基准数据）
            const refFrom = lowerRes ? refCoverFrom[lowerRes] : -1;
            // 容差 5 天（周末/假期休市口径，与 data_store.FILL_GAP_SEC 及上方
            // mergeStorePrefix 的无缝拼接判定一致）；比较基准取窗口需求起点
            // refNeedFrom 而非最早笔端点 minBiTime——窗口起点落在周末时第一根笔
            // 从上周五起笔，minBiTime 会被拉早 2~3 天，而基准首根已是窗口内可得的
            // 最早行情（周一开盘，回放也补不出周末数据），不算基准缺失；早于窗口
            // 起点的笔头不校准只损失精细度（保留本级 bar 时间）。基准首根晚于窗口
            // 起点 5 天以上才是真缺段（先在 WEB 基础数据页回放深拉补库）
            const refOk = typeof refFrom === "number" && refFrom >= 0
              && refNeedFrom > 0
              && refFrom <= refNeedFrom + 5 * 86400;
            if (refOk) {
              console.log(`[周期 ${res}] 提示: ${drawRes} 图表数据仅到 ${toT(cover.first)}，更早 ${skippedCount} 根笔已完整落盘（端点经 ${lowerRes} 回放库基准校准，窗口外笔头保留本级时间），将经回放定位补绘（超出 ${drawRes} 图表深度）`);
              if (clippedBis.length > 0 && lowerRes) replayPendingBis = clippedBis;
            } else {
              console.log(`[周期 ${res}] 警告: ${drawRes} 周期数据仅加载到 ${toT(cover.first)}，跳过 ${skippedCount} 根更早的笔（避免吸附成无效笔；落盘数据完整；可在 WEB 基础数据页回放深拉补齐 ${lowerRes || drawRes} 基准）`);
            }
            if (drawBis.length === 0) {
              // 全部笔都超范围：直接回放补绘全部笔后收尾（含断档兜底重建）
              if (replayPendingBis) {
                const rp = await drawClippedViaReplay(res, drawRes, replayPendingBis);
                console.log(`[周期 ${res}] 回放补绘: ${rp.rounds} 轮定位，画上 ${rp.created} 根${rp.failed ? `，失败 ${rp.failed} 根` : ''}${rp.skipped ? `，${rp.skipped} 根超出回放深度未画` : ''}${rp.reason ? `（${rp.reason}）` : ''}`);
                replayStats = rp;
                replayPendingBis = null;
              } else {
                console.log(`[周期 ${res}] 全部笔超出已加载数据范围，本轮跳过绘制`);
              }
              continue;
            }
          }
        }
      }

      const createResult = await createPeriod(res, drawBis);

      // 创建后回读校验：创建成功（bi_ok）≠ 端点正确——TradingView 会把超出数据范围的
      // 时间静默吸附到数据边缘。读回每根笔的端点与请求值比对，不符者删除并重走
      // 「覆盖加载 → 重建」一轮，仍失败则删除并计入 bi_bad 如实报告（避免全绿假象）
      let finalResult = { ...clearedResult, ...createResult };
      const createdIds = createResult.created_ids || [];
      if (createdIds.length > 0 && drawBis.length > 0) {
        const verifyOnce = async (ids, bis) => {
          const ptsArr = await readStrokesByIds(ids);
          const tol = intervalSecOf(drawRes) || 1; // 容差 1 根K线（端点在校准基准周期 bar 边界上）
          const bad = [];
          for (let i = 0; i < ids.length; i++) {
            const p = ptsArr[i];
            if (!p || !p[0] || !p[1]) continue; // 端点读不到（shape 隐藏等）→ 未校验，不误判
            const b = bis[i];
            const timeBad = Math.abs(p[0].time - b.startTime) > tol || Math.abs(p[1].time - b.endTime) > tol;
            const priceBad = Math.abs(p[0].price - b.startPrice) > 0.01 || Math.abs(p[1].price - b.endPrice) > 0.01;
            if (timeBad || priceBad) bad.push({ id: ids[i], bi: b });
          }
          return bad;
        };
        const bad = await verifyOnce(createdIds, drawBis);
        if (bad.length > 0) {
          console.log(`[周期 ${res}] 回读校验: ${bad.length}/${createdIds.length} 根端点被吸附，删除后重试...`);
          await removeShapesByIds(bad.map(x => x.id));
          const minT = bad.reduce((m, x) => Math.min(m, x.bi.startTime, x.bi.endTime), Infinity);
          if (minT !== Infinity) await ensureBarsCover(drawRes, minT);
          const retryBis = bad.map(x => x.bi);
          const retry = await createPeriod(res, retryBis);
          const bad2 = await verifyOnce(retry.created_ids || [], retryBis);
          if (bad2.length > 0) {
            await removeShapesByIds(bad2.map(x => x.id)); // 重建仍坏 → 删除，宁缺毋滥
            console.log(`[周期 ${res}] 警告: 重试后仍有 ${bad2.length} 根无法正确创建（数据未覆盖），已移除，可稍后重跑`);
          }
          finalResult = {
            ...finalResult,
            bi_ok: (createResult.bi_ok || 0) - bad.length + (retry.bi_ok || 0) - bad2.length,
            bi_bad: bad2.length,
          };
        }
      }
      // 实时段画完后回放补绘：超出图表深度的笔经「回放定位 + 翻页加载」分段画上
      // （放在实时段之后——进/出回放会重置图表数据，避免影响上面实时段的覆盖加载）；
      // 回放态无法精确锚定的笔（补偿请求落在休市/周末断档）再回源周期图重建兜底
      const runReplayBackfill = async () => {
        const rp = await drawClippedViaReplay(res, drawRes, replayPendingBis);
        replayPendingBis = null;
        let fbNote = '';
        if (rp.fallbackBis && rp.fallbackBis.length > 0) {
          const fbBis = rp.fallbackBis;
          const minT = fbBis.reduce((m, b) => Math.min(m, b.startTime, b.endTime), Infinity);
          await ensureResolution(res);
          currentRes = res;
          if (minT !== Infinity) await ensureBarsCover(res, minT);
          const fbCreate = await createShapesOnly(res, fbBis, 0);
          await sleep(2500);
          const fbTol = intervalSecOf(res) || 900;
          const fbBad = await verifyShapes(fbCreate.created_ids || [], fbBis, fbTol, false, rp.tzOff);
          const fbOk = Math.max(0, (fbCreate.bi_ok || 0) - fbBad.length);
          if (fbBad.length > 0) await removeShapesByIds(fbBad.map(x => x.id));
          rp.created += fbOk;
          fbNote = `，${fbBis.length} 根回放断档改源周期图重建（成功 ${fbOk}）`;
          if (drawRes !== res) { await ensureResolution(drawRes); currentRes = drawRes; }
        }
        console.log(`[周期 ${res}] 回放补绘: ${rp.rounds} 轮定位，画上 ${rp.created} 根${rp.failed ? `，失败 ${rp.failed} 根` : ''}${rp.skipped ? `，${rp.skipped} 根超出回放深度未画` : ''}${fbNote}${rp.reason ? `（${rp.reason}）` : ''}`);
        replayStats = rp;
      };
      if (replayPendingBis) await runReplayBackfill();
      if (replayStats) {
        finalResult = {
          ...finalResult,
          bi_ok: (finalResult.bi_ok || 0) + (replayStats.created || 0),
          bi_bad: (finalResult.bi_bad || 0) + (replayStats.failed || 0),
        };
      }
      console.log("\n=== 绘制结果 [周期 " + res + "] ===");
      console.log(JSON.stringify(finalResult, null, 2));
    }

    // ============================================================
    // 同笔后处理：大小周期笔完全重叠时，大周期线置顶 + 标记「同笔」
    // TradingView 按「后创建者在上」叠放 shape；主循环从大到小创建，完全重叠处
    // 小周期线盖住了大周期线。此处在全部创建完成后：检测相邻周期同笔（链式成组），
    // 把大周期的线「先建新、验通过、再删旧」重建置顶（失败删新留旧，不丢线），
    // 最后统一画同笔文本标记。--no-tongbi 关闭；--dry 只检测打印不绘图。
    // ============================================================
    if (!NO_TONGBI) {
      // 数据源：本次 allBis 优先；本次未运行的周期用旧缓存补——局部重跑（如
      // --periods=60）时上级 240 的线仍在图上，其笔数据来自上次运行的落盘
      const merged = { ...allBis };
      try {
        const old = JSON.parse(fs.readFileSync(bisCacheFile(SYMBOL), "utf8"));
        if (old && old.periods) {
          for (const [r, list] of Object.entries(old.periods)) {
            if (!merged[r] && Array.isArray(list) && list.length) merged[r] = list;
          }
        }
      } catch (e) { /* 无旧缓存：只检测本次运行的周期 */ }

      const groups = buildTongBiGroups(merged, PERIODS);
      console.log(`\n=== 同笔检测：${groups.length} 组大小周期完全重叠 ===`);
      for (const g of groups) {
        const t = g.members[0].bi;
        console.log(`[同笔] ${g.members.map(m => m.res).join("=")} ${t.startPrice}(${toT(t.startTime)}) -> ${t.endPrice}(${toT(t.endTime)})`);
      }

      if (!DRY) {
        // 整段降级保护：后处理任何异常只放弃置顶/标记，不影响后续笔数据落盘
        // （mark-buy-sell 强制依赖 bis 缓存，不能因标记失败而断链）
        try {
        // 清除旧同笔标记：标记的可见范围横跨多个周期，须逐周期切换后按 title 清除
        // （getAllShapes 只返回当前图表周期可见的 shape，与 clearPeriod 同理）
        const clearTB = async () => {
          const r = await client.Runtime.evaluate({
            expression: `(function() {
              const chart = TradingViewApi.activeChart();
              const TITLE = "${TB_TITLE}";
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
                  if (readTitle(s.id) === TITLE) {
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
        let tbCleared = 0;
        for (const res of PERIODS) {
          if (COMPUTE_ONLY.has(res)) continue;
          if (res !== currentRes) {
            await ensureResolution(res);
            currentRes = res;
          }
          tbCleared += (await clearTB()).cleared;
        }
        if (tbCleared > 0) console.log(`[同笔] 清除旧标记 ${tbCleared} 个`);

        // 墙钟偏移探针（同 drawClippedViaReplay 的探针）：TradingView 存储的 shape 锚点是
        // 「墙钟时间」（bar UTC ts + 图表时区偏移 tzOff，回放图实测 +8h），渲染时再减回。
        // 回放图上事后读回的 _points 已是偏移后的存储值——用落盘 UTC ts 直接比对会全部
        // 失配（曾致同笔置顶全部误判「线不存在」），故比对前先测偏移再减回。
        // 非回放图探针测得 ~0（请求时间即 bar 边界，存储值原样）。
        const probeTzOff = async () => {
          const range = await readBarsRange();
          if (!range || !range.last) return null;
          const lowerSec = 60;
          const id = await client.Runtime.evaluate({
            expression: `(async function(){
              const chart = TradingViewApi.activeChart();
              return await chart.createMultipointShape(
                [{ time: ${range.last - lowerSec}, price: ${range.lastClose} }, { time: ${range.last}, price: ${range.lastClose} }],
                { shape: 'polyline', lock: false, overrides: { linecolor: '#000000', linewidth: 1, title: 'TB_TZ_PROBE' } });
            })()`,
            returnByValue: true, awaitPromise: true, timeout: 20000,
          }).then(r => r.result && r.result.value).catch(() => null);
          if (!id) return null;
          await sleep(2500); // 吸附/换算异步生效，紧跟创建读回是请求值（竞态）
          let off = null;
          const pts = await readShapePoints(id);
          if (pts && pts[1] && typeof pts[1].time === 'number') off = pts[1].time - range.last;
          await removeShapesByIds([id]);
          return typeof off === 'number' ? off : null;
        };

        // 按端点在图上找现有笔 shape：title 匹配 + 两端点容差匹配（口径同回读校验；
        // 存储值须先减墙钟偏移 tzOff 再与落盘 UTC ts 比对）
        const findShapesByBi = async (res, bi, tolSec, tzOff) => {
          const r = await client.Runtime.evaluate({
            expression: `(function() {
              const chart = TradingViewApi.activeChart();
              const TITLE = "CHAN_BI_${res}";
              const BI = ${JSON.stringify({ t0: bi.startTime, t1: bi.endTime, p0: bi.startPrice, p1: bi.endPrice })};
              const TOL = ${tolSec};
              const OFF = ${tzOff || 0};
              const out = [];
              try {
                const shapes = chart.getAllShapes();
                for (const s of shapes) {
                  if (s.name !== 'polyline') continue;
                  try {
                    const sh = chart.getShapeById(s.id);
                    const props = sh && sh._source && sh._source._properties;
                    if (!props || !props.title || String(props.title._value) !== TITLE) continue;
                    const pts = sh._source._points;
                    if (!pts || pts.length < 2) continue;
                    if (Math.abs(pts[0].time - OFF - BI.t0) <= TOL && Math.abs(pts[1].time - OFF - BI.t1) <= TOL &&
                        Math.abs(pts[0].price - BI.p0) <= 0.01 && Math.abs(pts[1].price - BI.p1) <= 0.01) {
                      out.push(s.id);
                    }
                  } catch(e) { continue; }
                }
              } catch(e) {}
              return out;
            })()`,
            returnByValue: true, awaitPromise: true, timeout: 20000,
          });
          return (r.result && r.result.value) || [];
        };

        // 置顶：组按「组内最大周期」升序处理（小周期组先重建、大周期组最后创建，
        // 组间叠放同样保持大周期在上）。在低一级基准周期上重建（与 createPeriod
        // 同规则，避免端点被吸附到源周期 bar 边界）；基准周期恰是两条线同屏可见处，
        // 须同时找到大周期线与直接下级线才处理（其一被裁剪/删除则无重叠可言）。
        const tzOff = await probeTzOff();
        if (tzOff === null) console.log("[同笔] 提示: 墙钟偏移探针失败，按 0 偏移比对（非回放图通常为 0）");
        else if (tzOff !== 0) console.log(`[同笔] 检测到回放图墙钟偏移 ${tzOff}s，端点比对已换算`);
        let lifted = 0, skipped = 0, failed = 0;
        const markedGroups = [];
        for (const g of groups) {
          const chain = g.members.map(m => m.res).join("=");
          const top = g.members[0];
          const partner = g.members[1];
          const bi = top.bi;
          const drawRes = lowerResOf(top.res) || top.res;
          const tol = intervalSecOf(drawRes) || 900;
          if (drawRes !== currentRes) {
            await ensureResolution(drawRes);
            currentRes = drawRes;
          }
          const topIds = await findShapesByBi(top.res, bi, tol, tzOff);
          const partnerIds = partner ? await findShapesByBi(partner.res, partner.bi, tol, tzOff) : [];
          if (!topIds.length || !partnerIds.length) {
            skipped++;
            console.log(`[同笔] 跳过 ${chain}：图上${!topIds.length ? `${top.res} 线不存在` : `${partner.res} 线不存在`}（可能被窗口裁剪/手动删除）`);
            continue;
          }
          // 数据覆盖检查：切换周期后图表可能只加载最近K线，未覆盖时重建会被吸附成无效笔
          const cover = await ensureBarsCover(drawRes, Math.min(bi.startTime, bi.endTime));
          if (!cover.covered) {
            skipped++;
            console.log(`[同笔] 跳过 ${chain}：${drawRes} 图表数据未覆盖到笔起点（保留原叠放）`);
            continue;
          }
          // 防抢占：创建前确认图表仍在绘制周期（同主循环）
          const resNow = await client.Runtime.evaluate({
            expression: `String(TradingViewApi.activeChart().resolution())`,
            returnByValue: true, awaitPromise: true, timeout: 10000,
          });
          if (resNow.result && normRes(resNow.result.value) !== normRes(drawRes)) {
            await ensureResolution(drawRes);
            currentRes = drawRes;
          }
          // 先建新（后创建 → 渲染在上层），回读校验通过后再删旧；失败则删新留旧。
          // 创建直传落盘 ts 不补偿（墙钟换算由 TV 渲染层处理）；校验须等 ~2.5s
          // 吸附/换算沉降后读存储值并减 tzOff（紧跟创建读回是请求值，竞态放行）
          const create = await createPeriod(top.res, [bi]);
          const newId = create.created_ids && create.created_ids[0];
          let ok = false;
          if (newId) {
            await sleep(2500);
            const pts = (await readStrokesByIds([newId]))[0];
            if (pts && pts[0] && pts[1] &&
                Math.abs(pts[0].time - (tzOff || 0) - bi.startTime) <= tol && Math.abs(pts[1].time - (tzOff || 0) - bi.endTime) <= tol &&
                Math.abs(pts[0].price - bi.startPrice) <= 0.01 && Math.abs(pts[1].price - bi.endPrice) <= 0.01) {
              ok = true;
            }
          }
          if (ok) {
            await removeShapesByIds(topIds); // 命中的旧线全删（含历史残留重复）
            lifted++;
            markedGroups.push(g);
          } else {
            if (newId) await removeShapesByIds([newId]);
            failed++;
            console.log(`[同笔] 警告: ${chain} 重建未通过回读校验，保留原线（小周期暂在上）`);
          }
        }

        // 同笔标记：每组一个 text shape，锚=笔中点，文本=同笔60=15（大到小），
        // 颜色=最大周期颜色，可见范围=组内各周期并集（凡组内任一线可见处均可见）
        const drawTBMark = async (g) => {
          const top = g.members[0];
          const bi = top.bi;
          const label = "同笔" + g.members.map(m => m.res).join("=");
          const r = await client.Runtime.evaluate({
            expression: `(async function() {
              const chart = TradingViewApi.activeChart();
              const MARK = ${JSON.stringify({ time: Math.floor((bi.startTime + bi.endTime) / 2), price: (bi.startPrice + bi.endPrice) / 2 })};
              const LABEL = ${JSON.stringify(label)};
              const COLOR = "${resolutionColor(top.res)}";
              const TITLE = "${TB_TITLE}";
              const IV_CFG = ${JSON.stringify(unionIntervalVisibility(g.members.map(m => m.res)))};
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
              try {
                const id = await chart.createMultipointShape(
                  [{ time: MARK.time, price: MARK.price }],
                  { shape: 'text', lock: false, overrides: { text: LABEL, color: COLOR, bold: true, title: TITLE } }
                );
                applyIV(id);
                return { ok: 1 };
              } catch (e) { return { ok: 0, err: e.message }; }
            })()`,
            returnByValue: true, awaitPromise: true, timeout: 30000,
          });
          return r.result.value;
        };
        let marked = 0;
        for (const g of markedGroups) {
          const m = await drawTBMark(g);
          if (m && m.ok) marked++;
          else console.log(`[同笔] 标记绘制失败:`, m && m.err);
        }
        console.log(`[同笔] 置顶 ${lifted} 组，标记 ${marked} 组，跳过 ${skipped} 组${failed ? `，失败 ${failed} 组` : ""}`);
        } catch (e) {
          console.log("[同笔] 后处理异常（放弃剩余置顶/标记，不影响笔数据落盘）:", e.message);
        }
      }
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
        bars: process.env.CHAN_CACHE_DIR ? allRawBars : undefined,
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
