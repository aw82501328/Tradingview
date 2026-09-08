/**
 * 支阻互换位标记脚本（独立 SKILL：mark-sr-flip）
 * 读取 chan-bi（画笔）SKILL 落盘的笔数据 .cursor/cache/bis_<品种>.json，
 * 识别各周期重要的「支阻位」（支撑/阻力关键价位），
 * 并用水平线（horizontal_line）在 TradingView Desktop 图上标记。
 *
 * 支阻位三类来源（--sr-types 可分别开关，默认 cluster,boll）：
 *   1. 密集区（cluster）——基于价格聚类（原逻辑不变）：
 *      a. 强支阻互换位：某价位被价格反复测试（触及次数 >= minTouch），
 *         之后价格突破该价位，该价位角色互换（R2S 阻力转支撑 / S2R 支撑转阻力）；
 *      b. 近期极值位：最近若干根笔的 swing 端点（高低点），是当前最直接的
 *         支撑/阻力参考（RES 阻力 / SUP 支撑，无触及要求）。
 *   2. 黄金分割（fib，默认关）——对每方向「最新的非一类买卖点」（2买/类2买/3买、
 *      2卖/类2卖/3卖，现算 findBuyPoints/findSellPoints），取其回调笔紧邻前方的
 *      顺势笔为参照笔，画经典回撤分割位；某方向无已形成点时走「预期回退」（pending）：
 *      形成中回调笔 + 其前方顺势笔生成预期位（次高点结构破坏自动失效）。
 *   3. BOLL 布林带（boll，默认开）——每周期最后一根已收盘K线的布林带上/中/下轨
 *      （26 周期 SMA ± 2σ，--boll-length/--boll-mult 可调，总体标准差 ÷N），
 *      上轨=阻力 RES、下轨=支撑 SUP、中轨按现价侧；不区分买卖点、无 pending。
 *
 * 三类同池合并：三类候选全部进 mergeFlipsAcrossPeriods（同一规则），价差 ≤ 0.5×最小周期ATR
 * 并成一条「位置线」；多来源混合线删掉 fib/pending/boll 标记，纯单来源 fib/boll 独立线保留标记。
 * 密集区按评分截断（--max-per-period），fib/boll 豁免（评分语义不适用）。
 *
 * 用法：
 *   node mark_sr_flip.js --from=2026-06-30            计算并绘制（默认 D,240,60,15,3）
 *   node mark_sr_flip.js --dry --from=2026-06-30      只计算打印，不绘图
 *
 * 参数：
 *   --from=YYYY-MM-DD   起始日期（应与画笔 chan-bi 一致）
 *   --periods=...       要标记的周期（逗号分隔，默认 D,240,60,15,3）
 *   --sr-types=...      支阻位类型开关（逗号分隔，默认 cluster,boll；可选 cluster/fib/boll）
 *   --fib-levels=...    黄金分割比率（逗号分隔，默认 0.382,0.5,0.618，须在 (0,1) 内）
 *   --boll-length=N     BOLL SMA 周期（默认 26，已收盘K线口径）
 *   --boll-mult=K       BOLL 标准差倍数（默认 2）
 *   --side-count=N      每周期图每侧条数（默认 2 → 每图最多 4 条）
 *   --cluster=K         价位聚类阈值（×ATR，默认 0.5）
 *   --merge=K           跨周期合并阈值（×最小周期ATR，默认 0.5）
 *   --recent-cluster=K  近期极值位聚类阈值（×ATR，默认 1.0）
 *   --min-touch=N       最少触及次数（可选；不传则按级别：D/240/60=4，15=3，3=8）
 *   --max-dist=K        选取时距离上限（×线自身级别ATR，默认 3.0）
 *   --max-per-period=N  每周期候选数量上限（默认 50，仅密集区，超出按强度评分降序截断）
 *   --dry               只计算不绘图
 *   --debug             打印调试信息
 *
 * 显示规则（全新模型）：
 *   每个周期图最多 2×side-count 条线；候选池 = 该级别及以上级别的合并线（高级别线继承到
 *   低周期图），取「距现价最近的上方 N + 下方 N」；每条线受 ≤ 3×线自身级别ATR 距离上限；
 *   所有线统一灰色 #787B86 实线，来源用 title/text 标注（如 BOLL上轨+4小时 / 密集区+15分钟）；
 *   每条线只在其显示周期可见（高级别线继承到低周期时为各周期生成独立实例）；清线前缀统一 SR_。
 *   数据层：periods = 各周期原始候选（密集区截断后 + fib + boll）；
 *   merged = 三类统一合并结果（下游 mark-entry 仍只读 price）；drawnByPeriod = 各显示周期选中的 ≤2×side-count 条（含来源标注）。
 */
const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");
// 缠论算法核心（复用 calcATR 等工具函数，支阻互换本身不属于缠论算法；
// 黄金分割支阻位需现算买卖点，复用 findBuyPoints/findSellPoints/calcMACD）
const core = require("../../chan-core/scripts/chan_core.js");
const { calcATR, calcMACD, intervalSecOf, findBuyPoints, findSellPoints, fmtT } = core;

// 缓存目录（chan-bi 画笔落盘笔数据、本脚本落盘支阻互换位数据）
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
const CLUSTER_ATR = Math.max(parseFloat(getArg("cluster", 0.5)) || 0.5, 0.01);
// 跨周期合并阈值（×最小周期ATR）：价格相近的互换位合并为一条
const MERGE_ATR = Math.max(parseFloat(getArg("merge", 0.5)) || 0.5, 0.01);
// 最少触及次数：--min-touch 显式指定则全局覆盖；否则按级别取默认值
const MIN_TOUCH_ARG = args.find(x => x.startsWith("--min-touch="));
const MIN_TOUCH_OVERRIDE = MIN_TOUCH_ARG ? parseInt(MIN_TOUCH_ARG.split("=")[1], 10) : null;
const FROM_DATE = getStrArg("from", "");
let FROM_TS = null;
{
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec((FROM_DATE || "").trim());
  if (m) FROM_TS = Math.floor(Date.UTC(+m[1], +m[2] - 1, +m[3]) / 1000);
  else if (FROM_DATE) console.log("警告: --from 日期格式应为 YYYY-MM-DD，忽略该参数");
}
// 要标记的周期列表（默认 日线/4小时/1小时/15分钟/3分钟 全部级别）
const PERIODS = getStrArg("periods", "D,240,60,15,3")
  .split(",").map(s => s.trim()).filter(Boolean);

// 每个周期图绘制规则：当前价「上方最近的 sideCount 个 + 下方最近的 sideCount 个」
// （--side-count 可配，默认 2 → 每图最多 4 条，允许上下不对称）
const SIDE_COUNT = Math.max(parseInt(getStrArg("side-count", "2"), 10) || 2, 1);
// 近期极值位：取最近 N 根笔的 swing 端点作为「当前最直接的支撑/阻力参考」
const RECENT_BI_COUNT = 20;
// 近期极值位的聚类容差（×ATR）：比强支阻位聚类更宽，把同一天密集的高低点
// 聚成一条「近期价位区」（如 8-29 的 4460~4467 高点群聚成 4464 一线的阻力），
// 避免碎点被拆散导致漏标。
const RECENT_CLUSTER_ATR = Math.max(parseFloat(getArg("recent-cluster", 1.0)) || 1.0, 0.01);
// 支阻位强度评分权重：score = TOUCH_WEIGHT × norm(触及次数) + BARS_WEIGHT × norm(经过K线数)
const TOUCH_WEIGHT = 0.6;  // 支撑/压力被触及的次数多 → 权重 60%
const BARS_WEIGHT = 0.4;   // 该价位带经过的 K 线数量多 → 权重 40%
// 选取时的距离范围（×本级别ATR）：同一侧只在「距当前价 ≤ 该值×本级别ATR」的候选中选评分最高者，
// 避免远古强位（如 4051）因评分高把当前价附近的支阻位挤掉。
const MAX_DIST_ATR = Math.max(parseFloat(getArg("max-dist", 3.0)) || 3.0, 0.5);
// 每周期候选数量上限：识别结果每周期最多保留 maxPerPeriod 个（超出按强度评分降序截断）。
// 上限只约束「标记/落盘」的数据层；图上默认仍按每级别上下各 1 个绘制（显示层不受影响）。
const MAX_PER_PERIOD = Math.max(parseInt(getStrArg("max-per-period", "50"), 10) || 50, 1);

// 支阻位类型开关（--sr-types，逗号分隔）：cluster=密集区（强支阻互换位+近期极值位）、
// fib=黄金分割（非一类买卖点参照笔的回撤分割位）、boll=布林带（末根已收盘K线的上/中/下轨）。
// 三类可分别开关（默认 cluster,boll），全部关闭则报错。
const SR_TYPES = new Set(
  getStrArg("sr-types", "cluster,boll")
    .split(",").map(s => s.trim().toLowerCase()).filter(Boolean)
    .filter(t => {
      if (t === "cluster" || t === "fib" || t === "boll") return true;
      console.log(`警告: 未知支阻位类型 "${t}"（可选 cluster/fib/boll），已忽略`);
      return false;
    })
);
if (SR_TYPES.size === 0) {
  console.log("错误: --sr-types 解析后为空（可选 cluster/fib/boll）");
  process.exit(1);
}

// 黄金分割比率（--fib-levels 可配，须在 (0,1) 内，去重升序）
const FIB_LEVELS = (() => {
  const raw = getStrArg("fib-levels", "0.382,0.5,0.618")
    .split(",").map(s => parseFloat(s.trim())).filter(r => r > 0 && r < 1);
  const uniq = [...new Set(raw)].sort((a, b) => a - b);
  if (uniq.length === 0) {
    console.log("警告: --fib-levels 全部非法（须为 (0,1) 内数字），回退默认 0.382/0.5/0.618");
    return [0.382, 0.5, 0.618];
  }
  return uniq;
})();

// 黄金分割只取「非一类买卖点」（2买/类2买/3买、2卖/类2卖/3卖）。
// 真1买/真1卖是 mark-buy-sell 层 mergeNearFirstSecond 的合并标注，
// findBuyPoints/findSellPoints 原始输出不存在，白名单过滤即天然排除一类。
const FIB_BUY_TYPES = ["2买", "类2买", "3买"];
const FIB_SELL_TYPES = ["2卖", "类2卖", "3卖"];
// 上级周期映射（现算买卖点的区间套用；D 及未收录周期无上级，走结构底分支）。
// 用固定映射而非 PERIODS 前一元素：--periods 可传子集，而 bis 文件含全部周期。
const UPPER_OF = { "240": "D", "60": "240", "15": "60", "3": "15" };
// BOLL 布林带参数（已收盘K线口径：剔除末根形成中K线后取末 BOLL_LENGTH 根收盘价）
const BOLL_LENGTH = Math.max(parseInt(getStrArg("boll-length", "26"), 10) || 26, 1);
const BOLL_MULT = Math.max(parseFloat(getArg("boll-mult", 2)) || 2, 0.01);
// 所有支阻位线统一灰色实线（来源用 title/text 标注区分）
const SR_GRAY = "#787B86";

// 读取K线时，起始日期前额外取的缓冲根数（保证覆盖最早笔端点）
const BAR_BUFFER = 30;

// ============================================================
// 按级别的颜色 / 可见范围 / 最少触及次数
// ============================================================

/**
 * 支阻位颜色：与该级别笔一致（复用 chan-bi 的 resolutionColor 映射）。
 * 3分钟=青蓝, 15分钟=紫, 1小时=黄, 4小时=蓝, 日线=红, 周线=绿。
 */
function srColor(res) {
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
 * 支阻位可见范围：某级别找到的位置，只显示在「该级别及以下级别」。
 * 核心周期从大到小：D > 240 > 60 > 15 > 3。
 * 用 intervalsVisibilities 连续范围表达（中间周期如 2h/5m 会一并显示，与笔一致）：
 *   D    → 全可见
 *   240  → 4小时及以下（hours 1~4 + minutes 1~59）
 *   60   → 1小时及以下（hours 1~1 + minutes 1~59）
 *   15   → 15分钟及以下（minutes 1~15）
 *   3    → 仅3分钟（minutes 3~3）
 */
function srVisibilityFor(res) {
  const NONE = {
    ticks: false,
    seconds: false, secondsFrom: 1, secondsTo: 59,
    minutes: false, minutesFrom: 1, minutesTo: 59,
    hours: false, hoursFrom: 1, hoursTo: 24,
    days: false, daysFrom: 1, daysTo: 366,
    weeks: false, weeksFrom: 1, weeksTo: 52,
    months: false, monthsFrom: 1, monthsTo: 12,
  };
  const r = String(res).toUpperCase();
  switch (r) {
    case "1D":
    case "D":
      // 日线及以下：全可见
      return {
        ...NONE,
        minutes: true, minutesFrom: 1, minutesTo: 59,
        hours: true, hoursFrom: 1, hoursTo: 24,
        days: true, daysFrom: 1, daysTo: 366,
        weeks: true, weeksFrom: 1, weeksTo: 52,
        months: true, monthsFrom: 1, monthsTo: 12,
      };
    case "240":
    case "4H":
      return { ...NONE, minutes: true, minutesFrom: 1, minutesTo: 59, hours: true, hoursFrom: 1, hoursTo: 4 };
    case "60":
    case "1H":
      return { ...NONE, minutes: true, minutesFrom: 1, minutesTo: 59, hours: true, hoursFrom: 1, hoursTo: 1 };
    case "15":
      return { ...NONE, minutes: true, minutesFrom: 1, minutesTo: 15 };
    case "3":
      return { ...NONE, minutes: true, minutesFrom: 3, minutesTo: 3 };
    case "1W":
    case "W":
      return { ...NONE, minutes: true, minutesFrom: 1, minutesTo: 59, hours: true, hoursFrom: 1, hoursTo: 24, days: true, daysFrom: 1, daysTo: 366, weeks: true, weeksFrom: 1, weeksTo: 52 };
    default:
      // 未知周期：全可见兜底
      return { ...NONE, minutes: true, minutesFrom: 1, minutesTo: 59, hours: true, hoursFrom: 1, hoursTo: 24, days: true, daysFrom: 1, daysTo: 366 };
  }
}

/**
 * 单周期可见性（新显示模型）：某条线只在指定周期图可见。
 * 每个显示周期各画自己的 ≤4 条线；高级别线继承到低周期图时为各周期生成
 * 各自独立的线实例，可见性 = 仅该周期（互不重叠）。
 */
function srVisibilitySingle(res) {
  const NONE = {
    ticks: false,
    seconds: false, secondsFrom: 1, secondsTo: 59,
    minutes: false, minutesFrom: 1, minutesTo: 59,
    hours: false, hoursFrom: 1, hoursTo: 24,
    days: false, daysFrom: 1, daysTo: 366,
    weeks: false, weeksFrom: 1, weeksTo: 52,
    months: false, monthsFrom: 1, monthsTo: 12,
  };
  const r = String(res).toUpperCase();
  switch (r) {
    case "1D":
    case "D":
      return { ...NONE, days: true, daysFrom: 1, daysTo: 1 };
    case "240":
    case "4H":
      return { ...NONE, hours: true, hoursFrom: 4, hoursTo: 4 };
    case "60":
    case "1H":
      return { ...NONE, hours: true, hoursFrom: 1, hoursTo: 1 };
    case "15":
      return { ...NONE, minutes: true, minutesFrom: 15, minutesTo: 15 };
    case "3":
      return { ...NONE, minutes: true, minutesFrom: 3, minutesTo: 3 };
    case "1W":
    case "W":
      return { ...NONE, weeks: true, weeksFrom: 1, weeksTo: 1 };
    default:
      // 未知周期：全可见兜底
      return { ...NONE, minutes: true, minutesFrom: 1, minutesTo: 59, hours: true, hoursFrom: 1, hoursTo: 24, days: true, daysFrom: 1, daysTo: 366 };
  }
}

/**
 * 最少触及次数（按级别）：
 * 大级别（日线/4小时/1小时）走势缓慢、同一价位易被反复测试 → 阈值适中；
 * 小级别（15/3分钟）波动大、笔极多，若阈值过低会识别出海量噪音位
 * （3分钟 min-touch=2 曾识别出 162 个，间距仅约4美元），故显著提高。
 * --min-touch 显式指定时全局覆盖。
 */
function minTouchFor(res) {
  if (MIN_TOUCH_OVERRIDE) return MIN_TOUCH_OVERRIDE;
  const r = String(res).toUpperCase();
  switch (r) {
    case "1D": case "D":
    case "240": case "4H":
    case "60": case "1H":
      return 4;
    case "15":
      return 3;
    case "3":
      return 8;
    default:
      return 4;
  }
}

/**
 * 支阻位类型的中文标签（打印用）：
 * mixed → 位置线；boll → BOLL上轨/中轨/下轨；fib → 黄金分割支撑/阻力
 * （pending 预期位加「预期」前缀）；其余为密集区四类（R2S/S2R/RES/SUP）。
 */
function typeNameOf(f) {
  if (f.srcType === "mixed") return "位置线";
  if (f.boll) {
    return f.boll === "upper" ? "BOLL上轨" : f.boll === "mid" ? "BOLL中轨" : "BOLL下轨";
  }
  if (f.fib) {
    const name = f.type === "SUP" ? "支撑" : "阻力";
    return f.pending ? `预期黄金分割${name}` : `黄金分割${name}`;
  }
  return f.type === "R2S" ? "阻力转支撑" :
    f.type === "S2R" ? "支撑转阻力" :
    f.type === "RES" ? "阻力(近期极值)" : "支撑(近期极值)";
}

/**
 * 周期中文名（标注与打印用）：240→4小时、60→1小时、15→15分钟、3→3分钟、D→日线。
 */
function periodNameOf(res) {
  const r = String(res).toUpperCase();
  switch (r) {
    case "3": return "3分钟";
    case "15": return "15分钟";
    case "60":
    case "1H": return "1小时";
    case "240":
    case "4H": return "4小时";
    case "1D":
    case "D": return "日线";
    case "1W":
    case "W": return "周线";
    case "30S": return "30秒";
    default: return String(res);
  }
}

/**
 * 支阻位来源类型的中文标注（绘制 title/text 与落盘 label 用）。
 * 混合合并线（srcType=mixed）→ 位置线；boll → BOLL上轨/中轨/下轨；
 * fib → 预期<N>（pending）/ 黄金分割<ratio>（已形成）；cluster → 密集区。
 */
function sourceLabelOf(f) {
  if (f.srcType === "mixed") return "位置线";
  if (f.boll) {
    return f.boll === "upper" ? "BOLL上轨" : f.boll === "mid" ? "BOLL中轨" : "BOLL下轨";
  }
  if (f.fib) {
    return f.pending ? (f.fromPoint ? f.fromPoint.type : "预期") : `黄金分割${f.ratio}`;
  }
  return "密集区";
}

/**
 * 完整标注：`<来源类型>+<周期中文名>`（写入 title/text 与落盘 drawnByPeriod 的 label 字段）。
 */
function labelOf(f) {
  return `${sourceLabelOf(f)}+${periodNameOf(f.level)}`;
}

// ============================================================
// 支阻互换位识别算法（纯函数，便于 --dry 验证）
// ============================================================

/**
 * 从笔列表中提取 swing 高低转折点（去重）。
 * 笔首尾相连，每笔的终点就是一次转折；额外补第一笔的起点。
 * @param {Array} bis 某周期的笔列表
 * @returns {Array} [{ price, time, kind }]，kind = 'high'（阻力高点）| 'low'（支撑低点）
 */
function extractSwingPoints(bis) {
  const points = [];
  for (let i = 0; i < bis.length; i++) {
    const bi = bis[i];
    if (i === 0) {
      points.push({ price: bi.startPrice, time: bi.startTime, kind: bi.type === "up" ? "low" : "high" });
    }
    points.push({ price: bi.endPrice, time: bi.endTime, kind: bi.type === "up" ? "high" : "low" });
  }
  return points;
}

/**
 * 价位聚类（单遍扫描）：按价格排序后，相邻价差 <= tol 的点并入同一「价位簇」。
 * 每个簇记录代表价（触及价均值）与全部触及点。
 * @param {Array} points swing 点列表
 * @param {number} tol 聚类价格容差
 * @returns {Array} [{ price, touches:[{price,time,kind}] }]
 */
function clusterPoints(points, tol) {
  const sorted = [...points].sort((a, b) => a.price - b.price);
  const clusters = [];
  for (const p of sorted) {
    const last = clusters.length > 0 ? clusters[clusters.length - 1] : null;
    if (last && p.price - last.price <= tol) {
      last.touches.push(p);
      const sum = last.touches.reduce((s, x) => s + x.price, 0);
      last.price = sum / last.touches.length;
    } else {
      clusters.push({ price: p.price, touches: [p] });
    }
  }
  return clusters;
}

/**
 * 判断某个价位簇是否构成「支阻互换位」。
 * 规则：
 *   1. 若首尾触及角色相反（先高后低 → R2S，先低后高 → S2R），说明价位已被双向测试、角色已反转；
 *   2. 若角色未反转（全部高点或全部低点），用突破判定：
 *      - 主导为阻力（高点多）：之后收盘价向上突破价位 → R2S
 *      - 主导为支撑（低点多）：之后收盘价向下跌破价位 → S2R
 * @param {Object} cluster 价位簇 { price, touches }
 * @param {Array} bars K线列表 [{time, open, high, low, close, volume}]
 * @param {number} tol 价格容差（用于突破缓冲，避免刚好碰到价位）
 * @returns {Object|null} 互换位 { price, type, breakTime, touchCount, firstTouch, lastTouch }
 */
function detectFlip(cluster, bars, tol) {
  const touches = [...cluster.touches].sort((a, b) => a.time - b.time);
  const first = touches[0];
  const last = touches[touches.length - 1];
  const price = cluster.price;
  const base = { price, touchCount: touches.length, firstTouch: first.time, lastTouch: last.time };

  // 情况1：首尾角色相反（价位已被双向测试，角色已反转）
  if (first.kind === "high" && last.kind === "low") {
    return { ...base, type: "R2S", breakTime: last.time };
  }
  if (first.kind === "low" && last.kind === "high") {
    return { ...base, type: "S2R", breakTime: last.time };
  }

  // 情况2：角色未反转，用突破判定（收盘价有效穿越价位）
  const highCount = touches.filter(t => t.kind === "high").length;
  const lowCount = touches.filter(t => t.kind === "low").length;
  const dominant = highCount >= lowCount ? "resistance" : "support";
  const lastTouch = last.time;
  for (const bar of bars) {
    if (bar.time <= lastTouch) continue;
    if (dominant === "resistance" && bar.close > price + tol) {
      return { ...base, type: "R2S", breakTime: bar.time, dominant };
    }
    if (dominant === "support" && bar.close < price - tol) {
      return { ...base, type: "S2R", breakTime: bar.time, dominant };
    }
  }
  return null;
}

/**
 * 提取最近若干根笔的 swing 端点（近期极值位候选）。
 * 最近完成的笔端点即当前最直接的支撑/阻力参考（最新的高低点），
 * 用于补充强支阻互换位在最新价格行为上的滞后（如 8-29 高开后 4467 一线形成的阻力）。
 * @param {Array} bis 某周期的笔列表
 * @param {number} count 取最后多少根笔
 * @returns {Array} [{ price, time, kind }]，kind = 'high' | 'low'
 */
function extractRecentExtremes(bis, count) {
  const points = [];
  const start = Math.max(0, bis.length - count);
  for (let i = start; i < bis.length; i++) {
    const bi = bis[i];
    if (i === 0) {
      points.push({ price: bi.startPrice, time: bi.startTime, kind: bi.type === "up" ? "low" : "high" });
    }
    points.push({ price: bi.endPrice, time: bi.endTime, kind: bi.type === "up" ? "high" : "low" });
  }
  return points;
}

/**
 * 近期极值位的轻量判定：不要求触及次数（minTouch）。
 * - 簇内同时存在高、低点 → 价位被双向测试，按首尾角色判定互换类型；
 * - 否则按主导角色记为纯阻力 RES / 纯支撑 SUP（当前最直接的阻挡/承接位）。
 * @param {Object} cluster 价位簇 { price, touches }
 * @returns {Object} { price, type, breakTime, touchCount, firstTouch, lastTouch, recent: true }
 */
function detectRecentFlip(cluster) {
  const touches = [...cluster.touches].sort((a, b) => a.time - b.time);
  const first = touches[0];
  const last = touches[touches.length - 1];
  const price = cluster.price;
  const base = { price, touchCount: touches.length, firstTouch: first.time, lastTouch: last.time, recent: true };
  const highCount = touches.filter(t => t.kind === "high").length;
  const lowCount = touches.filter(t => t.kind === "low").length;
  if (highCount > 0 && lowCount > 0) {
    if (first.kind === "high" && last.kind === "low") return { ...base, type: "R2S", breakTime: last.time };
    if (first.kind === "low" && last.kind === "high") return { ...base, type: "S2R", breakTime: last.time };
  }
  return { ...base, type: highCount >= lowCount ? "RES" : "SUP", breakTime: last.time };
}

// ============================================================
// 黄金分割支阻位（fib）
// 对每方向「最新的非一类买卖点」，取其回调笔紧邻前方的顺势笔为参照笔，
// 画经典回撤分割位。fib 走「并行双轨」：不进 capPerPeriod（评分对结构派生位
// 无意义且每周期上界 2×比率数）/ mergeFlipsAcrossPeriods（并簇均价会破坏
// 比例位几何语义）/ pickByLevel（三条比率位是成组结构，上下各1会拆散）。
// ============================================================

/**
 * 从买卖点列表中取「最新的非一类点」。
 * @param {Array} points findBuyPoints/findSellPoints 原始输出 [{type,time,price}]
 * @param {Array} types 白名单（FIB_BUY_TYPES / FIB_SELL_TYPES）
 * @returns {Object|null} 白名单内 time 最大的点（同 time 取数组靠后者）；无 → null
 */
function pickLatestFibPoint(points, types) {
  let best = null;
  for (const p of points) {
    if (!types.includes(p.type)) continue;
    if (!best || p.time >= best.time) best = p;
  }
  return best;
}

/**
 * 定位买卖点的回调笔与参照笔。
 * 回调笔 = 终点即该点的笔（买点是 down 笔、卖点是 up 笔）；
 * 参照笔 = 紧邻回调笔前方的反向顺势笔（买点取前方上涨笔、卖点取前方下跌笔）。
 * @param {Array} bis 本周期笔列表（应传未过滤 FROM_TS 的全量，最新点的回调笔
 *                   可能横跨窗口边界，其前方笔会被过滤掉导致误判「首笔」）
 * @param {number} pointTime 买卖点时间（= 笔 endTime）
 * @param {string} pullbackType 回调笔方向：买点 "down"、卖点 "up"
 * @returns {Object|null} { pullback, refer }；匹配不到 / 首笔无前方笔 /
 *                        前一笔同向（笔应交替，同向为脏数据）→ null
 */
function referBiOfPoint(bis, pointTime, pullbackType) {
  const idx = bis.findIndex(b => b.endTime === pointTime && b.type === pullbackType);
  if (idx < 0 || idx === 0) return null;
  const refer = bis[idx - 1];
  if (!refer || refer.type === pullbackType) return null;
  return { pullback: bis[idx], refer };
}

/**
 * 参照笔的黄金分割回撤位。
 *   buy（参照笔为上涨笔 L→H）：分割位 = H - r×(H-L)，即上涨走势的回撤支撑；
 *   sell（参照笔为下跌笔 H→L）：分割位 = L + r×(H-L)，即下跌走势的反弹阻力。
 * @param {Object} refer 参照笔
 * @param {string} side "buy" | "sell"
 * @param {Array} fibLevels 比率列表
 * @returns {Array} [{ ratio, price }]；span=0（退化笔）→ []
 */
function fibLevelsOf(refer, side, fibLevels) {
  const H = side === "buy" ? refer.endPrice : refer.startPrice;
  const L = side === "buy" ? refer.startPrice : refer.endPrice;
  if (!(H > L)) return [];
  return fibLevels.map(ratio => ({
    ratio,
    price: side === "buy" ? H - ratio * (H - L) : L + ratio * (H - L),
  }));
}

/**
 * 预期回退（pending）：某方向无已形成非一类点时，用「形成中的回调笔 + 其前方顺势笔」
 * 生成预期黄金分割位（等待2卖/2买 状态下的预期形成区——实盘即「预期位置 + 够笔/背驰确认」）。
 * - 卖向：末笔为形成中上涨笔（bis 落盘口径：末笔延伸至最新极值）且其现高点 < 前方
 *   下跌笔起点（次高点结构未破坏）→ 参照笔 = 该下跌笔；
 * - 买向对称：末笔为形成中下跌笔且其现低点 > 前方上涨笔起点；
 * - 形成笔突破前方笔起点（结构被否定）→ 条件失效，该方向无预期位（下次重算自动消失）。
 * @param {Array} bis 本周期全量笔
 * @param {string} side "buy" | "sell"
 * @returns {Object|null} { forming, refer }；末笔是首笔/方向不符/前方笔同向（脏数据）/
 *                        次高点结构破坏 → null
 */
function pendingReferOf(bis, side) {
  if (bis.length < 2) return null;
  const forming = bis[bis.length - 1];
  const pullbackType = side === "buy" ? "down" : "up";  // 形成中的回调笔方向
  if (forming.type !== pullbackType) return null;
  const refer = bis[bis.length - 2];
  if (!refer || refer.type === pullbackType) return null;
  // 次高点/次低点结构未破坏：形成笔现极值未超越前方顺势笔起点
  if (side === "sell" ? !(forming.endPrice < refer.startPrice)
                      : !(forming.endPrice > refer.startPrice)) return null;
  return { forming, refer };
}

/**
 * 组装黄金分割支阻位候选：每方向优先取「最新非一类点」× 全部比率（不回退更早点）；
 * 该方向无已形成点时走**预期回退**（pendingReferOf），生成 pending 预期位补位。
 * @param {Array} fullBis 本周期全量笔（供参照笔定位，见 referBiOfPoint）
 * @param {Array} buyPts findBuyPoints 原始输出
 * @param {Array} sellPts findSellPoints 原始输出
 * @param {Array} fibLevels 比率列表
 * @param {Array} bars 本周期K线（算 barsPassed，与密集区候选数据形状对齐）
 * @param {number} tol 价格容差
 * @returns {Array} fib 候选列表（买点组在前；预期位带 pending: true）
 */
function buildFibCandidates(fullBis, buyPts, sellPts, fibLevels, bars, tol) {
  const out = [];
  const sides = [
    { side: "buy", points: buyPts, types: FIB_BUY_TYPES, pullbackType: "down", type: "SUP" },
    { side: "sell", points: sellPts, types: FIB_SELL_TYPES, pullbackType: "up", type: "RES" },
  ];
  for (const cfg of sides) {
    const pt = pickLatestFibPoint(cfg.points, cfg.types);
    let found = null;
    let srcPoint = null;   // 派生源：已形成点 or 预期（形成笔极值）
    let pending = false;
    if (pt) {
      // 有已形成点：只用它（预期位只补位，不抢占已形成点语义）；定位不到参照笔则跳过
      const f = referBiOfPoint(fullBis, pt.time, cfg.pullbackType);
      if (f) {
        found = f;
        srcPoint = { type: pt.type, time: pt.time, price: pt.price };
      }
    } else {
      // 无已形成点：预期回退（等待2卖/2买 的预期形成区）
      const p = pendingReferOf(fullBis, cfg.side);
      if (p) {
        found = p;
        srcPoint = {
          type: cfg.side === "buy" ? "预期2买" : "预期2卖",
          time: p.forming.endTime, price: p.forming.endPrice,
        };
        pending = true;
      }
    }
    if (!found) continue;
    for (const { ratio, price } of fibLevelsOf(found.refer, cfg.side, fibLevels)) {
      const cand = {
        price,
        type: cfg.type,
        fib: true,
        ratio,
        fromPoint: srcPoint,
        referBi: {
          startTime: found.refer.startTime, endTime: found.refer.endTime,
          startPrice: found.refer.startPrice, endPrice: found.refer.endPrice,
        },
        touchCount: 1,
        firstTouch: found.refer.startTime,
        lastTouch: srcPoint.time,
        breakTime: srcPoint.time,  // 信号点/形成笔极值时间 = fib 位生效/绘线锚点时间
        barsPassed: countBarsPassing(price, bars, tol),
      };
      if (pending) cand.pending = true;
      out.push(cand);
    }
  }
  return out;
}

// ============================================================
// BOLL 布林带支阻位（boll）
// 每周期取「最后一根已收盘K线」的布林带上/中/下轨（BOLL_LENGTH 周期 SMA ± BOLL_MULT×σ，
// 总体标准差 ÷N，与 TradingView 同口径），上轨=阻力 RES、下轨=支撑 SUP、中轨按现价侧。
// boll 同 fib 一样豁免截断与评分（评分语义不适用），但**参与**跨周期合并（三类同池）。
// ============================================================

/**
 * 计算布林带（已收盘口径）：剔除末根形成中K线，取末 len 根收盘价的 SMA 与总体标准差。
 * @param {Array} bars K线列表 [{time,open,high,low,close,...}]
 * @param {number} len SMA 周期
 * @param {number} mult 标准差倍数
 * @returns {Object|null} { upper, mid, lower }；已收盘不足 len 根 → null
 */
function calcBOLL(bars, len, mult) {
  if (!bars || bars.length === 0) return null;
  // 剔除末根（形成中）K线，只取已收盘
  const closed = bars.slice(0, bars.length - 1);
  if (closed.length < len) return null;
  const closes = closed.slice(-len).map(b => b.close);
  const n = closes.length;
  const mid = closes.reduce((s, c) => s + c, 0) / n;
  const variance = closes.reduce((s, c) => s + (c - mid) * (c - mid), 0) / n;  // 总体方差（÷N）
  const sigma = Math.sqrt(variance);
  return { upper: mid + mult * sigma, mid, lower: mid - mult * sigma };
}

/**
 * 组装 BOLL 候选：三轨各一条。上轨 RES、下轨 SUP、中轨按现价侧（现价 ≥ 中轨 → 支撑）。
 * @param {Array} bars K线列表
 * @param {number} len SMA 周期
 * @param {number} mult 标准差倍数
 * @param {number} currentPrice 当前价（中轨角色判定）
 * @returns {Array} 3 个候选（无布林位时为空数组）
 */
function buildBollCandidates(bars, len, mult, currentPrice) {
  const band = calcBOLL(bars, len, mult);
  if (!band) return [];
  const closed = bars.slice(0, bars.length - 1);
  const anchorTime = closed[closed.length - 1].time;
  const midType = (currentPrice !== null && currentPrice !== undefined && currentPrice >= band.mid) ? "SUP" : "RES";
  const make = (price, type, tag) => ({
    price, type, boll: tag,
    touchCount: 1, barsPassed: 0,
    firstTouch: anchorTime, lastTouch: anchorTime, breakTime: anchorTime,
  });
  return [
    make(band.upper, "RES", "upper"),
    make(band.mid, midType, "mid"),
    make(band.lower, "SUP", "lower"),
  ];
}

/**
 * 统计某价位带（price ± tol）被多少根 K 线覆盖/穿越（含影线）。
 * K线高低价覆盖价位带：low <= price + tol 且 high >= price - tol，
 * 衡量价格在该价位停留/穿越的时长，是支阻位强度评分的维度之一。
 * @param {number} price 价位
 * @param {Array} bars K线列表 [{time, open, high, low, close, volume}]
 * @param {number} tol 价格容差
 * @returns {number} 覆盖该价位带的 K 线根数
 */
function countBarsPassing(price, bars, tol) {
  let n = 0;
  for (const b of bars) {
    if (b.low <= price + tol && b.high >= price - tol) n++;
  }
  return n;
}

/**
 * 支阻位强度评分：score = 0.6 × norm(触及次数) + 0.4 × norm(经过K线数量)。
 * 同一级别候选集内 min-max 归一化（消除量纲差异），
 * norm 后两者都在 [0,1]，得分越高说明该价位被市场测试/停留越多、越重要。
 * @param {Object} f 支阻位 { touchCount, barsPassed }
 * @param {Array} group 同级别候选集（用于 min-max）
 * @returns {number} [0,1] 之间的评分
 */
function flipScore(f, group) {
  const ts = group.map(g => g.touchCount);
  const bs = group.map(g => g.barsPassed);
  const tMin = Math.min(...ts), tMax = Math.max(...ts);
  const bMin = Math.min(...bs), bMax = Math.max(...bs);
  const normTouch = tMax > tMin ? (f.touchCount - tMin) / (tMax - tMin) : 1;
  const normBars = bMax > bMin ? (f.barsPassed - bMin) / (bMax - bMin) : 1;
  return TOUCH_WEIGHT * normTouch + BARS_WEIGHT * normBars;
}

/**
 * 跨周期合并：把各周期识别出的互换位按价格合并，价格相近的合成一个。
 * 目的：不同级别可能在同一价位各自识别出互换位，合并避免图上多条近乎重叠的线。
 * 合并后确定「主要来源级别」= 来源中最大的级别（大级别支阻位更重要，保留其归属，
 * 用于决定该支阻位的颜色与可见范围：显示在「该级别及以下」）。
 * @param {Object} allFlips { 周期: [flip,...] }
 * @param {number} tol 合并价格容差
 * @returns {Array} [{ price, type, touchCount, firstTouch, breakTime, sources:[...], level }]
 */
function mergeFlipsAcrossPeriods(allFlips, tol) {
  // 每条候选的来源类型（cluster/fib/boll，由标记反推；混合类型合并后统一按「位置线」口径）
  const kindOf = (f) => (f.boll ? "boll" : f.fib ? "fib" : "cluster");
  const all = [];
  for (const res of Object.keys(allFlips)) {
    for (const f of allFlips[res]) {
      all.push({ ...f, source: res });
    }
  }
  all.sort((a, b) => a.price - b.price);
  const merged = [];
  for (const f of all) {
    const last = merged.length > 0 ? merged[merged.length - 1] : null;
    if (last && f.price - last.price <= tol) {
      const prevTouch = last.touchCount;
      const totalTouch = prevTouch + f.touchCount;
      // 价格按触及次数加权平均
      last.price = (last.price * prevTouch + f.price * f.touchCount) / totalTouch;
      last.touchCount = totalTouch;
      // 经过 K 线数量同样累加（同一价位带的停留时间合并）
      last.barsPassed = (last.barsPassed || 0) + (f.barsPassed || 0);
      if (!last.sources.includes(f.source)) last.sources.push(f.source);
      last.firstTouch = Math.min(last.firstTouch, f.firstTouch);
      last.breakTime = Math.max(last.breakTime, f.breakTime);
      // 类型冲突（罕见）：以触及次数更多者为准
      if (f.touchCount > prevTouch) last.type = f.type;
      if (!last._kinds.includes(kindOf(f))) last._kinds.push(kindOf(f));
    } else {
      merged.push({ ...f, sources: [f.source], _kinds: [kindOf(f)] });
    }
  }
  // 确定每个合并项的主要来源级别 = 来源中最大的级别（大级别优先）
  for (const m of merged) {
    m.level = dominantLevel(m.sources);
    delete m.source;
    // 多来源混合线：删除 fib/pending/boll 等具体来源标记，统一按「位置线」口径
    if (m._kinds.length > 1) {
      m.srcType = "mixed";
      delete m.fib; delete m.pending; delete m.ratio;
      delete m.fromPoint; delete m.referBi; delete m.boll;
    } else if (m._kinds.length === 1) {
      m.srcType = m._kinds[0];
    }
    delete m._kinds;
  }
  return merged;
}

// 级别大小顺序（从大到小），用于取「最大级别」与可见范围判断
const LEVEL_ORDER = ["1W", "W", "1D", "D", "240", "4H", "60", "1H", "15", "3"];

/**
 * 从来源周期列表确定主要来源级别：取最大的级别（LEVEL_ORDER 中更靠前）。
 * 大级别（日线/4小时/1小时）识别的支阻位是更长期的价位，应优先保留其归属与颜色，
 * 不能被触及次数更多的小级别（3分钟）「淹没」。
 */
function dominantLevel(sources) {
  let best = null;
  for (const res of sources) {
    if (best === null || LEVEL_ORDER.indexOf(res) < LEVEL_ORDER.indexOf(best)) {
      best = res;
    }
  }
  return best;
}

/**
 * 每个级别只保留「当前价格上方最近的 1 个 + 下方最近的 1 个」支阻位。
 * 先限定距离范围（距当前价 ≤ maxDistAtr×本级别ATR，避免远古强位挤掉近位），
 * 同一侧仍存在多个候选时，选「强度评分最高」的 1 个
 * （score = 0.6×触及次数 + 0.4×经过K线数，同级别候选集内 min-max 归一化）。
 * 上方 = price >= currentPrice；下方 = price < currentPrice。
 * @param {Array} merged 合并后的支阻位列表（含 level 字段）
 * @param {number} currentPrice 当前价格
 * @param {number} sideCount 每方向保留个数（默认 1）
 * @param {number} maxDistAtr 距离上限（×本级别ATR）
 * @param {Object} periodAtrs 各周期 ATR 表（用于算本级别距离上限）
 * @returns {Array} 过滤后的支阻位列表（含 score）
 */
function pickByLevel(merged, currentPrice, sideCount, maxDistAtr, periodAtrs) {
  const byLevel = {};
  for (const f of merged) {
    (byLevel[f.level] = byLevel[f.level] || []).push(f);
  }
  const result = [];
  for (const level of Object.keys(byLevel)) {
    const group = byLevel[level];
    // 本级别距离上限 = maxDistAtr × 本级别ATR（无ATR时退回与当前价最近）
    const levelAtr = periodAtrs[level];
    const maxDist = levelAtr ? maxDistAtr * levelAtr : Infinity;
    // 距离范围内先给同级别候选集计算强度评分（min-max 归一化）
    for (const f of group) f.score = flipScore(f, group);
    // 上方：>= 当前价 且在距离范围内，评分降序取前 sideCount
    const above = group
      .filter(f => f.price >= currentPrice && f.price - currentPrice <= maxDist)
      .sort((a, b) => b.score - a.score)
      .slice(0, sideCount);
    // 下方：< 当前价 且在距离范围内，评分降序取前 sideCount
    const below = group
      .filter(f => f.price < currentPrice && currentPrice - f.price <= maxDist)
      .sort((a, b) => b.score - a.score)
      .slice(0, sideCount);
    result.push(...above, ...below);
  }
  return result;
}

/**
 * 按显示周期选取（新显示模型）：每个显示周期图最多 2×sideCount 条线。
 * 候选池 = 该级别及以上级别的合并线（高级别线继承到低周期图，如 3m 图候选池含 3/15/60/240/D 全部位置线），
 * 取「距现价最近的上方 sideCount 条 + 下方 sideCount 条」，每条仍受 ≤ maxDistAtr×线自身级别ATR
 * 距离上限（periodAtrs[line.level] 缺失时 Infinity），允许上下不对称。
 * @param {Array} merged 合并后的支阻位列表（含 level 字段）
 * @param {Array} displayPeriods 显示周期列表（从大到小）
 * @param {number} currentPrice 当前价格
 * @param {number} sideCount 每侧条数（默认 2）
 * @param {number} maxDistAtr 距离上限（×线自身级别ATR）
 * @param {Object} periodAtrs 各周期 ATR 表
 * @returns {Object} { 周期: [line,...] }
 */
function pickNearestForDisplay(merged, displayPeriods, currentPrice, sideCount, maxDistAtr, periodAtrs) {
  const out = {};
  for (const L of displayPeriods) {
    const li = LEVEL_ORDER.indexOf(L);
    // 候选池 = 该级别及以上（level 在 LEVEL_ORDER 中更靠前或相同）
    const pool = merged.filter(f => LEVEL_ORDER.indexOf(f.level) <= li);
    const maxDistOf = (f) => {
      const levelAtr = periodAtrs[f.level];
      return levelAtr ? maxDistAtr * levelAtr : Infinity;
    };
    // 上方：>= 当前价 且在距离范围内，按价差升序（就近）取前 sideCount
    const above = pool
      .filter(f => f.price >= currentPrice && f.price - currentPrice <= maxDistOf(f))
      .sort((a, b) => a.price - b.price)
      .slice(0, sideCount);
    // 下方：< 当前价 且在距离范围内，按价差升序（就近）取前 sideCount
    const below = pool
      .filter(f => f.price < currentPrice && currentPrice - f.price <= maxDistOf(f))
      .sort((a, b) => b.price - a.price)
      .slice(0, sideCount);
    out[L] = [...above, ...below];
  }
  return out;
}

// ============================================================
// 每周期候选数量上限（数据层截断）
// ============================================================

/**
 * 每周期候选数量上限截断：每周期最多保留 maxPerPeriod 个候选。
 * 超出时按「强度评分降序」保留 Top N——保留市场测试/停留最多（最重要）的位置，
 * 丢弃最弱的候选（如 3 分钟识别出的海量低分噪音位）。
 * 评分沿用 flipScore，以该周期全部候选为归一化集（与 pickByLevel 内一致）。
 * 只截断数据层（落盘的 periods/merged），不影响默认绘制层（每级别上下各 1 个）。
 * @param {Object} allFlips 各周期候选 { 周期: [flip,...] }
 * @param {number} maxPerPeriod 每周期上限（<=0 表示不截断）
 * @returns {Object} 截断后的 allFlips（每周期最多 maxPerPeriod 个）
 */
function capPerPeriod(allFlips, maxPerPeriod) {
  if (!allFlips) return allFlips;
  if (!maxPerPeriod || maxPerPeriod <= 0) return allFlips;
  const out = {};
  for (const res of Object.keys(allFlips)) {
    const group = allFlips[res];
    if (!group || group.length <= maxPerPeriod) { out[res] = group; continue; }
    // 以该周期全部候选为归一化集计算强度评分（flipScore 会为 group 内每个 f 填充 score）
    const scored = group.map(f => ({ ...f, score: flipScore(f, group) }));
    scored.sort((a, b) => b.score - a.score);
    out[res] = scored.slice(0, maxPerPeriod);
  }
  return out;
}

// ============================================================
// 主流程
// ============================================================

async function main() {
  let client;
  if (!FROM_DATE) {
    console.log("错误: 必须指定起始日期 --from=YYYY-MM-DD");
    process.exit(1);
  }
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
    console.log("将标记周期:", PERIODS.join(", "));

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

    // 读取K线（从起始日期开始，含缓冲；数据未覆盖时自动加载完整历史）
    const fetchBars = async (fromTs, buffer) => {
      const tolerance = 6 * 3600; // 允许K线起点与起始日有小偏差
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
          // 强制加载完整历史（scrollToFirstBar 触发后台逐步加载到第一根K线）
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

    // ============================================================
    // 逐周期计算支阻互换位
    // ============================================================
    const allFlips = {};  // 密集区候选（强互换位+近期极值位，走评分/截断/合并/选取管线）
    const allFibs = {};   // 黄金分割候选（同池合并，不进评分/截断管线）
    const allBolls = {};  // BOLL 布林带候选（同池合并，豁免评分/截断）
    const barsByRes = {}; // 各周期原始K线（BOLL 生成需在 currentPrice 已知后，故先存起）
    const periodAtrs = {};
    const lastCloseByRes = {};
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
      // 读取K线（用于 ATR 与突破判定）
      const d = await fetchBars(FROM_TS, BAR_BUFFER);
      if (!d || !d.bars || d.bars.length === 0) {
        console.log(`\n[周期 ${res}] 读取K线失败，跳过`);
        continue;
      }
      const bars = d.bars;
      barsByRes[res] = bars;
      lastCloseByRes[res] = bars[bars.length - 1].close;
      const atr = calcATR(bars, 14);
      periodAtrs[res] = atr;
      const tol = CLUSTER_ATR * atr;
      const minTouch = minTouchFor(res);
      if (DEBUG) console.log(`\n[周期 ${res}] K线 ${bars.length} 根 ATR=${atr.toFixed(2)} 聚类阈值=${tol.toFixed(2)} 最少触及=${minTouch}`);

      // 提取 swing 点 → 聚类 → 筛选 → 互换判定（强支阻互换位）
      const swingPoints = extractSwingPoints(bis);
      const clusters = clusterPoints(swingPoints, tol);
      const flips = [];
      for (const c of clusters) {
        if (c.touches.length < minTouch) continue;
        const flip = detectFlip(c, bars, tol);
        if (flip) {
          flip.barsPassed = countBarsPassing(flip.price, bars, tol);
          flips.push(flip);
        }
      }
      // 近期极值位：最近若干根笔的 swing 端点（当前最直接的支撑/阻力参考，不要求触及次数）。
      // 用更宽的聚类容差（RECENT_CLUSTER_ATR），把同一天密集高低点聚成一条近期价位区。
      const recentPoints = extractRecentExtremes(bis, RECENT_BI_COUNT);
      const recentClusters = clusterPoints(recentPoints, RECENT_CLUSTER_ATR * atr);
      const recentFlips = [];
      for (const c of recentClusters) {
        const r = detectRecentFlip(c);
        if (r) {
          r.barsPassed = countBarsPassing(r.price, bars, tol);
          recentFlips.push(r);
        }
      }
      // 黄金分割支阻位：现算买卖点（买卖点不落盘），每方向只取最新非一类点，
      // 参照笔=回调前顺势笔。买卖点在 FROM_TS 过滤后的笔上算（与 mark-buy-sell
      // 图上口径一致），参照笔在未过滤的全量笔上定位（回调笔可能横跨窗口边界）。
      let fibFlips = [];
      if (SR_TYPES.has("fib")) {
        const curBisF = bis.filter(b => b.endTime >= FROM_TS);
        const upperRes = UPPER_OF[res];
        const upperBisF = upperRes && (periodBis[upperRes] || []).length > 0
          ? periodBis[upperRes].filter(b => b.endTime >= FROM_TS)
          : null;
        const macdArr = calcMACD(bars);
        fibFlips = buildFibCandidates(
          bis,
          findBuyPoints(curBisF, upperBisF, macdArr, intervalSecOf(res)),
          findSellPoints(curBisF, upperBisF, macdArr, intervalSecOf(res)),
          FIB_LEVELS, bars, tol
        );
      }
      allFibs[res] = fibFlips;
      allFlips[res] = SR_TYPES.has("cluster") ? flips.concat(recentFlips) : [];
      console.log(`\n=== 支阻互换位 [周期 ${res}]（聚类阈值 ${tol.toFixed(2)}，最少触及 ${minTouch} 次，近期极值位=${recentFlips.length} 个，黄金分割位=${fibFlips.length} 个）===`);
      if (allFlips[res].length === 0 && fibFlips.length === 0) {
        console.log("无互换位");
      } else {
        allFlips[res].forEach((f, idx) => {
          const tag = f.recent ? " [近期极值]" : "";
          console.log(
            `互换位${idx + 1}: ${f.price.toFixed(2)} [${typeNameOf(f)}${tag}] 触及${f.touchCount}次 首触=${toT(f.firstTouch)} 末触=${toT(f.lastTouch)} 互换于=${toT(f.breakTime)}`
          );
        });
        fibFlips.forEach((f, idx) => {
          console.log(
            `黄金分割${idx + 1}: ${f.price.toFixed(2)} [${typeNameOf(f)} ${f.fromPoint.type}@${toT(f.fromPoint.time)} 比率${f.ratio} 参照笔${toT(f.referBi.startTime)}~${toT(f.referBi.endTime)}]`
          );
        });
      }
    }

    // 每周期候选数量上限（数据层截断）：每周期最多保留 MAX_PER_PERIOD 个，
    // 超出按强度评分降序截断；只截断密集区（fib/boll 评分语义不适用，豁免）。
    const allFlipsCapped = capPerPeriod(allFlips, MAX_PER_PERIOD);
    const cappedSummary = Object.keys(allFlipsCapped)
      .map(res => `${res}:${allFlips[res].length}→${allFlipsCapped[res].length}`)
      .join("  ");
    if (DEBUG || Object.values(allFlips).some(a => a.length > MAX_PER_PERIOD)) {
      console.log(`\n每周期候选上限 ${MAX_PER_PERIOD} 个（按强度评分截断，仅密集区）: ${cappedSummary}`);
    }

    // 当前价格：用最小有数据周期的最后一根K线收盘价（各周期收盘价接近，取最小周期最精确）
    const currentPrice =
      lastCloseByRes["3"] || lastCloseByRes["15"] || lastCloseByRes["60"] ||
      lastCloseByRes["240"] || lastCloseByRes["D"] || null;
    console.log(`\n当前价格（最小周期收盘价）: ${currentPrice !== null ? currentPrice.toFixed(2) : "未知"}`);

    // BOLL 布林带候选：中轨按现价侧，需 currentPrice 已知后生成（逐周期独立，三轨一组）
    if (SR_TYPES.has("boll")) {
      for (const res of PERIODS) {
        if (!barsByRes[res]) continue;
        allBolls[res] = buildBollCandidates(barsByRes[res], BOLL_LENGTH, BOLL_MULT, currentPrice);
      }
    }

    // 统一合并池：三类候选（密集区截断后 + fib + boll）全部进 mergeFlipsAcrossPeriods（同一池、同一规则）。
    // 合并容差按「最小有数据的周期 ATR」缩放：小级别价位密集，按最小周期ATR只合并真正的「同一价位」。
    const combined = {};
    for (const res of PERIODS) {
      const arr = (allFlipsCapped[res] || []).concat(allFibs[res] || []).concat(allBolls[res] || []);
      if (arr.length > 0) combined[res] = arr;
    }
    const atrValues = Object.keys(combined).filter(r => periodAtrs[r]).map(r => periodAtrs[r]);
    const minAtr = atrValues.length ? Math.min(...atrValues) : 0;
    const mergeTol = MERGE_ATR * minAtr;
    const mergedOut = mergeFlipsAcrossPeriods(combined, mergeTol);
    const totalBefore = Object.values(combined).reduce((s, a) => s + a.length, 0);
    console.log(`\n=== 跨周期合并（统一池，合并阈值 ${mergeTol.toFixed(2)} = ${MERGE_ATR}×最小周期ATR ${minAtr.toFixed(2)}）===`);
    console.log(`合并前 ${totalBefore} 个 → 合并后 ${mergedOut.length} 个（密集区/fib/BOLL 三类同池合并）`);
    mergedOut.forEach((f, idx) => {
      console.log(`支阻位${idx + 1}: ${f.price.toFixed(2)} [${typeNameOf(f)}] 触及${f.touchCount}次 经过${f.barsPassed || 0}根K线 主级别=${f.level} 来源=${f.sources.join("+")} 互换于=${toT(f.breakTime)}`);
    });

    // 按显示周期选取：每周期图 ≤ 2×sideCount 条（就近上下各 N，高级别线继承到低周期图）。
    // 显示周期 = 成功处理（有 ATR/K线）的周期，顺序沿 PERIODS（从大到小）。
    const displayPeriods = PERIODS.filter(r => periodAtrs[r]);
    const drawnByPeriod = currentPrice !== null
      ? pickNearestForDisplay(mergedOut, displayPeriods, currentPrice, SIDE_COUNT, MAX_DIST_ATR, periodAtrs)
      : {};
    // 每条绘制线附来源标注（title/text 与落盘 label 用）
    for (const L of Object.keys(drawnByPeriod)) {
      for (const f of drawnByPeriod[L]) {
        f.label = labelOf(f);
      }
    }
    const totalDrawn = Object.values(drawnByPeriod).reduce((s, a) => s + a.length, 0);
    console.log(`\n=== 按周期显示（每图 ≤ ${2 * SIDE_COUNT} 条：就近上下各 ${SIDE_COUNT}，距离上限 ≤ ${MAX_DIST_ATR}×线自身级别ATR，当前价 ${currentPrice !== null ? currentPrice.toFixed(2) : "?"}）===`);
    if (totalDrawn === 0) {
      console.log("无支阻位需要绘制");
    } else {
      for (const L of Object.keys(drawnByPeriod)) {
        drawnByPeriod[L].forEach((f, idx) => {
          const side = f.price >= currentPrice ? "上方" : "下方";
          const dist = currentPrice !== null ? Math.abs(f.price - currentPrice).toFixed(2) : "?";
          console.log(`[${periodNameOf(L)}] 绘制${idx + 1}: ${f.price.toFixed(2)} [${side} ${dist}] ${f.label} 触及${f.touchCount}次`);
        });
      }
    }

    // 支阻位数据落盘：periods = 各周期原始候选（密集区截断后 + fib + boll），
    // merged = 三类统一合并结果（mark-entry 仍只读 price），drawnByPeriod = 各显示周期选中的 ≤2×sideCount 条（含来源标注）。
    try {
      fs.mkdirSync(CACHE_DIR, { recursive: true });
      const srFile = cacheFile("srflip", SYMBOL);
      const periodsOut = {};
      for (const res of PERIODS) {
        if (!periodAtrs[res]) continue;
        periodsOut[res] = (allFlipsCapped[res] || []).concat(allFibs[res] || []).concat(allBolls[res] || []);
      }
      const payload = {
        symbol: SYMBOL,
        from: FROM_DATE || null,
        fromTs: FROM_TS,
        generatedAt: new Date().toISOString(),
        clusterAtr: CLUSTER_ATR,
        mergeAtr: MERGE_ATR,
        minTouch: MIN_TOUCH_OVERRIDE !== null ? MIN_TOUCH_OVERRIDE : "per-level",
        sideCount: SIDE_COUNT,
        recentBiCount: RECENT_BI_COUNT,
        scoreWeights: { touch: TOUCH_WEIGHT, bars: BARS_WEIGHT },
        maxDistAtr: MAX_DIST_ATR,
        maxPerPeriod: MAX_PER_PERIOD,
        srTypes: [...SR_TYPES],
        fibLevels: FIB_LEVELS,
        bollCfg: { length: BOLL_LENGTH, mult: BOLL_MULT },
        currentPrice: currentPrice,
        periods: periodsOut,
        merged: mergedOut,
        drawnByPeriod: drawnByPeriod,
      };
      fs.writeFileSync(srFile, JSON.stringify(payload, null, 2), "utf8");
      console.log(`\n支阻位数据已落盘: ${srFile}（${Object.keys(periodsOut).length} 个周期原始 ${totalBefore} 个，merged 共 ${mergedOut.length} 个，绘制 ${totalDrawn} 条）`);
    } catch (e) {
      console.log("警告: 支阻位数据落盘失败:", e.message);
    }

    if (DRY) {
      console.log("\n[DRY RUN] 不绘图。");
      await client.close();
      return;
    }

    // ============================================================
    // 绘制阶段：先清除旧横线，再绘制新横线（灰色无限水平线）
    // ============================================================

    /**
     * 清除旧的支阻位横线（title 以 SR_ 开头，兼容历史 SR_FLIP_<周期> / SR_FIB_<...>）。
     * 支阻位全周期可见，无需切换周期即可清除。
     */
    const clearSrFlip = async () => {
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
              const t = readTitle(s.id);
              if (t.indexOf('SR_') === 0) {
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
     * 绘制支阻互换位（无限水平线），颜色/可见范围/标题按「主要来源级别」分别设置。
     * horizontal_line 为无限水平线，只需一个锚点（time=互换发生时间，price=代表价）。
     * @param {Array} items 已附带 color/title/iv 字段的绘制项
     */
    const createSrFlip = async (items) => {
      const r = await client.Runtime.evaluate({
        expression: `(async function() {
          const chart = TradingViewApi.activeChart();
          const ITEMS = ${JSON.stringify(items)};
          const out = { ok: 0, err: [] };
          const created = [];

          const applyIV = (id, IV_CFG) => {
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

          for (const f of ITEMS) {
            try {
              const id = await chart.createMultipointShape(
                [{ time: f.time, price: f.price }],
                { shape: 'horizontal_line', lock: false, overrides: { linecolor: f.color, linewidth: 1, linestyle: f.linestyle !== undefined ? f.linestyle : 0, title: f.title, text: f.text } }
              );
              applyIV(id, f.iv);
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

    // 清除旧线（title 前缀 SR_，兼容历史 SR_FLIP / SR_FIB）
    const cleared = await clearSrFlip();
    console.log(`\n已清除旧支阻位横线: ${cleared.cleared} 个`);

    // 绘制：所有线统一灰色实线，来源用 title/text 标注；可见性 = 仅其显示周期
    console.log("\n=== 绘制结果（统一灰线 + 来源标注，每周期图仅该周期可见）===");
    if (totalDrawn > 0) {
      const drawItems = [];
      for (const L of Object.keys(drawnByPeriod)) {
        for (const f of drawnByPeriod[L]) {
          const label = f.label || labelOf(f);
          drawItems.push({
            time: f.breakTime,
            price: f.price,
            color: SR_GRAY,
            linestyle: 0,  // 实线
            title: "SR_" + label,
            text: label,
            iv: srVisibilitySingle(L),
          });
        }
      }
      const result = await createSrFlip(drawItems);
      console.log(JSON.stringify(result, null, 2));
    } else {
      console.log("无支阻位需要绘制");
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
}

// ============================================================
// 导出（供 TDD 单元测试复用）
// ============================================================

module.exports = {
  // 常量
  CLUSTER_ATR,
  MERGE_ATR,
  RECENT_CLUSTER_ATR,
  TOUCH_WEIGHT,
  BARS_WEIGHT,
  MAX_DIST_ATR,
  MAX_PER_PERIOD,
  SIDE_COUNT,
  RECENT_BI_COUNT,
  SR_TYPES,
  FIB_LEVELS,
  FIB_BUY_TYPES,
  FIB_SELL_TYPES,
  UPPER_OF,
  BOLL_LENGTH,
  BOLL_MULT,
  SR_GRAY,
  // 工具
  srColor,
  srVisibilityFor,
  srVisibilitySingle,
  minTouchFor,
  typeNameOf,
  periodNameOf,
  sourceLabelOf,
  labelOf,
  // 支阻互换位识别（密集区）
  extractSwingPoints,
  clusterPoints,
  detectFlip,
  extractRecentExtremes,
  detectRecentFlip,
  countBarsPassing,
  flipScore,
  // 黄金分割支阻位
  pickLatestFibPoint,
  referBiOfPoint,
  pendingReferOf,
  fibLevelsOf,
  buildFibCandidates,
  // BOLL 布林带支阻位
  calcBOLL,
  buildBollCandidates,
  // 合并 / 截断 / 选取
  mergeFlipsAcrossPeriods,
  dominantLevel,
  capPerPeriod,
  pickByLevel,
  pickNearestForDisplay,
};

// 直接运行时才连接 CDP 执行主流程（被 require 时仅导出纯函数，供单元测试）
if (require.main === module) {
  main();
}
