/**
 * 进出场标记脚本（独立 SKILL：mark-entry）
 * 依赖「交易计划」（trading-plan）落盘的 plan_<品种>.json 判定各周期当前进场状态，
 * 映射到 6 种进场策略，校验该策略的进场条件后，在「背驰级别」（更低周期）标记进场箭头：
 *   - 买点（多头）= 向上红色箭头（arrow_up）
 *   - 卖点（空头）= 向下绿色箭头（arrow_down）
 *
 * 出场规则（同向持仓互斥：同方向持仓未终局不再开新仓，多空互不影响）：
 *   - 止损：盘中破坏进场参考的支阻位（方向感知选位：short 上方最近 / long 下方最近）
 *   - 止盈1：背驰周期够笔（有利方向笔完成）→ 保本（止损位上移至进场价）
 *   - 止盈2：检测周期够笔 → 平一半
 *   - 止盈3：检测周期破前底/过前高（进场后反向笔端点破前一同向笔端点）→ 全平
 *   出场标记统一黄色箭头（EXIT_COLOR #FFEB3B）：方向=平仓方向（多头出场 ↓ / 空头出场 ↑），title = EXIT_<背驰级别>
 *
 * 用法：
 *   node mark_entry.js --from=2026-06-30            计算并绘制（默认 240,60,15,3）
 *   node mark_entry.js --dry --from=2026-06-30      只计算打印，不绘图
 *
 * 参数：
 *   --from=YYYY-MM-DD   起始日期（应与画笔/支阻位/交易计划一致）
 *   --periods=...       检测周期（逗号分隔，默认 240,60,15,3）
 *   --near=K            靠近支阻位阈值（×状态所在周期ATR，默认 1.0）
 *   --dry               只计算不绘图
 *   --debug             打印调试信息
 *
 * 数据依赖（运行顺序）：画笔(chan-bi) → 标记买卖点(mark-buy-sell) → 支阻位(mark-sr-flip) → 交易计划(trading-plan) → 本脚本
 */
const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");
// 缠论算法核心（复用背驰判定、中枢、ATR、MACD 等工具函数）
const core = require("../../chan-core/scripts/chan_core.js");
const { calcATR, calcMACD, isBiDiverge, fmtT, lowerResOf, buildZSByUpper, intervalSecOf } = core;

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
// --with-30s：启用 30 秒级别（ALL_RES 追加 30S，3分钟状态可用 30S 背驰产生进场信号，
// 箭头画在 30S 级别；30S 自身无更低级别，不作为检测周期）
const WITH_30S = args.includes("--with-30s");
const PERIODS = getStrArg("periods", "240,60,15,3")
  .split(",").map(s => s.trim()).filter(Boolean);

// 箭头颜色：买点（多头）红色、卖点（空头）绿色
const BUY_COLOR = "#F23645";
const SELL_COLOR = "#089981";
// 出场标记颜色：黄色（与 Web 控制台出场箭头一致；方向=平仓方向：多头出场 ↓ / 空头出场 ↑）
const EXIT_COLOR = "#FFEB3B";
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
    case "30S":  return { ...NONE, seconds: true, secondsFrom: 30, secondsTo: 30 };
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
// 进场状态 → 策略映射（依赖交易计划 plan 落盘结果）
// ============================================================

/**
 * 交易计划策略 → 进场策略映射（用户规则）。
 * plan.strategy 来自 trading-plan 的 strategyOf 输出；震荡/数据不足/趋势中无匹配
 * （方向=观望）等不产生进场策略，返回 null。
 * @param {string} planStrategy 交易计划 strategy 文本
 * @returns {null|{key:string, direction:string, label:string}} key 策略标识、direction long/short
 */
function entryStrategyOf(planStrategy) {
  switch (planStrategy) {
    case "等待反弹后做2卖": // 状态=1卖
      return { key: "wait2Sell", direction: "short", label: "等待反弹后做2卖" };
    case "等待回调后做2买": // 状态=1买
      return { key: "wait2Buy", direction: "long", label: "等待回调后做2买" };
    case "等待高点附近的一卖": // 状态=2/3买+其他
      return { key: "wait1Sell", direction: "short", label: "等待一卖" };
    case "等待低点附近的一买": // 状态=2/3卖+其他
      return { key: "wait1Buy", direction: "long", label: "等待一买" };
    case "等待回调后的新买点": // 状态=2/3买+过左高不背驰
      return { key: "waitBuy", direction: "long", label: "等待回调后买点" };
    case "等待反弹后的新卖点": // 状态=2/3卖+过左低不背驰
      return { key: "waitSell", direction: "short", label: "等待反弹后卖点" };
    default:
      return null;
  }
}

// ============================================================
// 6 种进场策略的条件判定（纯函数，可单测）
// ============================================================

/**
 * 够笔：最后一笔是否为预期方向（空头→up 反弹、多头→down 回调）。
 * @param {Array} bis 本周期笔列表
 * @param {string} wantType "up" | "down"
 */
function lastBiOk(bis, wantType) {
  if (!bis || bis.length === 0) return false;
  return bis[bis.length - 1].type === wantType;
}

/**
 * 下跌段破前底：最近完成的一笔 down 笔终点，跌破更早最近同向 down 笔终点（创新低）。
 * 语义即「最近下跌段创新低」，不参照 findDivergePoints 的 50% span 过滤（大幅下跌后
 * 参照笔会被全部跳过导致误判未破前底）。
 * @param {Array} bis 本周期笔列表
 */
function brokePrevLow(bis) {
  if (!bis || bis.length < 2) return false;
  let lastDownIdx = -1;
  for (let i = bis.length - 1; i >= 0; i--) {
    if (bis[i].type === "down") { lastDownIdx = i; break; }
  }
  if (lastDownIdx <= 0) return false;
  let prevLow = Infinity;
  for (let i = lastDownIdx - 1; i >= 0; i--) {
    if (bis[i].type !== "down") continue;
    prevLow = bis[i].endPrice;
    break;
  }
  if (prevLow === Infinity) return false;
  return bis[lastDownIdx].endPrice < prevLow;
}

/**
 * 上涨段过前高：最近完成的一笔 up 笔终点，突破更早最近同向 up 笔终点（创新高）。
 * @param {Array} bis 本周期笔列表
 */
function brokePrevHigh(bis) {
  if (!bis || bis.length < 2) return false;
  let lastUpIdx = -1;
  for (let i = bis.length - 1; i >= 0; i--) {
    if (bis[i].type === "up") { lastUpIdx = i; break; }
  }
  if (lastUpIdx <= 0) return false;
  let prevHigh = -Infinity;
  for (let i = lastUpIdx - 1; i >= 0; i--) {
    if (bis[i].type !== "up") continue;
    prevHigh = bis[i].endPrice;
    break;
  }
  if (prevHigh === -Infinity) return false;
  return bis[lastUpIdx].endPrice > prevHigh;
}

/**
 * MACD 当前在 0 轴之下（dif < 0）：下0轴后反弹不过0轴。
 * @param {Array} macdArr 本周期 MACD 数组（含 dif）
 */
function macdBelowZero(macdArr) {
  if (!macdArr || macdArr.length === 0) return false;
  const last = macdArr[macdArr.length - 1];
  return last.dif < 0;
}

/**
 * MACD 当前在 0 轴之上（dif > 0）：上0轴后回调不破0轴。
 * @param {Array} macdArr 本周期 MACD 数组（含 dif）
 */
function macdAboveZero(macdArr) {
  if (!macdArr || macdArr.length === 0) return false;
  return macdArr[macdArr.length - 1].dif > 0;
}

/**
 * 出中枢的力度变弱（buildZSByUpper 取最后一个中枢）：
 * 离开中枢的笔相对进入中枢的笔 isBiDiverge 为 true，或离开笔 span < 进入笔 span × ratio。
 * @param {Array} bis       本周期笔列表
 * @param {Array} upperBis  上一级别笔列表（可为空数组）
 * @param {Array} macdArr   本周期 MACD 数组
 * @param {number} barSec   本周期单根K线秒数（供 buildZSByUpper）
 * @param {number} ratio    span 缩小比例阈值（默认 1.0）
 * @param {string} wantDir  期望的离开方向（"short"→离开笔应为 up，"long"→离开笔应为 down）
 */
function zsExitWeak(bis, upperBis, macdArr, barSec, ratio, wantDir) {
  let zss = [];
  try {
    zss = buildZSByUpper(bis, upperBis || [], barSec);
  } catch (e) { return false; }
  const last = zss[zss.length - 1];
  if (!last) return false;
  const enter = bis.find(b => b.endTime === last.enterEndTime);
  if (!enter) return false;
  // 离开笔 = exitTime 对应的笔（exitStartTime 为离开笔起点时间，原笔对象时间戳精确匹配）
  const exitBi = last.exitTime != null ? bis.find(b => b.startTime === last.exitStartTime) : null;
  if (!exitBi) return false; // 中枢未离开（仍在延伸），无「出中枢」力度可言
  // 期望的离开方向过滤：一卖应向上离开中枢、一买应向下离开中枢
  if (wantDir === "short" && exitBi.type !== "up") return false;
  if (wantDir === "long" && exitBi.type !== "down") return false;
  // 力度变弱：离开笔相对进入笔 MACD 背驰 或 离开笔幅度小于进入笔幅度 × ratio
  if (isBiDiverge(exitBi, enter, macdArr)) return true;
  if (exitBi.span < enter.span * (ratio || 1.0)) return true;
  return false;
}

/**
 * 以下级别出现背驰：收集所有更低周期（intervalSecOf 更小）中方向匹配的背驰点，
 * 按时间**降序**返回候选列表（最新的在前）。
 * 调用方（evaluateEntry）依次尝试候选并做支阻位校验，失败回退次新——
 * 避免「--with-30s 后高频 30S 背驰点抢占原级别点、而 30S 微观点又远离支阻位
 * 导致信号彻底消失」的问题（如 2026-09-04 13:39 的 3分钟背驰级别信号）。
 * @param {object} periodData {res: {bis, macdArr, ...}} 全部周期的预取数据
 * @param {string} X         当前检测周期
 * @param {string} wantDir   期望背驰方向 "long"（底背驰）| "short"（顶背驰）
 * @returns {{res:string, point:{time,price,direction}}[]} 候选列表，按 point.time 降序
 */
function lowerDiverge(periodData, X, wantDir) {
  const xSec = intervalSecOf(X) || Infinity;
  const cands = [];
  for (const res of Object.keys(periodData)) {
    const sec = intervalSecOf(res) || 0;
    if (sec >= xSec) continue; // 只取更低级别
    const pd = periodData[res];
    if (!pd || !pd.bis || pd.bis.length < 3) continue;
    let pts = [];
    try { pts = findDivergePoints(pd.bis, pd.macdArr); } catch (e) { continue; }
    for (const p of pts) {
      if (p.direction !== wantDir) continue;
      cands.push({ res, point: p });
    }
  }
  cands.sort((a, b) => b.point.time - a.point.time); // 最新在前
  return cands;
}

/**
 * 在支阻位附近：背驰点价与任一 srLevels 支阻位价差 ≤ nearTol。
 * @param {number} price     背驰点价格
 * @param {Array} srLevels   支阻位列表（srflip.merged，每项含 price）
 * @param {number} nearTol   靠近阈值
 * @returns {null|{sr:object, dist:number}} 最近命中的支阻位
 */
function nearSr(price, srLevels, nearTol) {
  if (!srLevels || srLevels.length === 0) return null;
  let best = null;
  for (const sr of srLevels) {
    const d = Math.abs(sr.price - price);
    if (d <= nearTol && (!best || d < best.dist)) best = { sr, dist: d };
  }
  return best;
}

/**
 * 校验某个进场策略的全部条件（在检测周期 X 上）。
 * 公共条件：够笔 + 以下级别背驰 + 在支阻位附近；按策略附加专属条件。
 * @param {object} ctx {res, bis, upperBis, macdArr, atr, barSec, nearAtr, srLevels, periodData}
 * @param {object} strategy entryStrategyOf 返回值
 * @returns {{ok:boolean, reason?:string, markRes?:string, point?:object, nearSr?:number}}
 *   ok=true 时 markRes=背驰所在更低周期、point=背驰点、nearSr=命中支阻位价格
 */
function evaluateEntry(ctx, strategy) {
  const { res, bis, upperBis, macdArr, atr, barSec, nearAtr, srLevels, periodData } = ctx;
  const { key, direction } = strategy;
  const wantType = direction === "short" ? "up" : "down"; // 空头等反弹(up)，多头等回调(down)
  const divergeDir = direction;                            // 空头→顶背驰(short)，多头→底背驰(long)

  // 1. 够笔
  if (!lastBiOk(bis, wantType)) {
    return { ok: false, reason: `最后一笔为 ${bis[bis.length - 1] ? bis[bis.length - 1].type : "?"}，需 ${wantType}（反弹/回调不够笔）` };
  }

  // 2. 各策略专属条件
  switch (key) {
    case "wait2Sell":
      if (!brokePrevLow(bis)) return { ok: false, reason: "下跌段未破前底" };
      if (!macdBelowZero(macdArr)) return { ok: false, reason: "MACD 未下0轴或反弹过0轴" };
      break;
    case "wait2Buy":
      if (!brokePrevHigh(bis)) return { ok: false, reason: "上涨段未过前高" };
      if (!macdAboveZero(macdArr)) return { ok: false, reason: "MACD 未上0轴或回调破0轴" };
      break;
    case "wait1Sell":
      if (!brokePrevHigh(bis)) return { ok: false, reason: "未够笔且过高点" };
      if (!zsExitWeak(bis, upperBis, macdArr, barSec, 1.0, "short")) return { ok: false, reason: "出中枢力度未变弱" };
      break;
    case "wait1Buy":
      if (!brokePrevLow(bis)) return { ok: false, reason: "未够笔且过低点" };
      if (!zsExitWeak(bis, upperBis, macdArr, barSec, 1.0, "long")) return { ok: false, reason: "出中枢力度未变弱" };
      break;
    case "waitBuy":
    case "waitSell":
      break; // 仅需够笔 + 以下级别背驰 + 支阻位附近
  }

  // 3+4. 以下级别背驰候选（按时间降序）依次做支阻位校验，失败回退次新点：
  //      策略专属条件不依赖背驰点（第2步已过），只需重试「支阻位附近」。
  //      （--with-30s 后 30S 高频背驰点常抢占原级别点，且其微观极值价常远离支阻位——
  //       无回退时信号彻底消失：旧点被抢占丢弃、新点校验被拒，两边都不出箭头。）
  const cands = lowerDiverge(periodData, res, divergeDir);
  if (cands.length === 0) return { ok: false, reason: "以下级别无匹配方向背驰" };

  const nearTol = nearAtr * atr; // 用背驰点价 vs 检测周期 ATR
  for (const c of cands) {
    const near = nearSr(c.point.price, srLevels, nearTol);
    if (near) return { ok: true, markRes: c.res, point: c.point, nearSr: near.sr.price };
  }
  return { ok: false, reason: "以下级别背驰点均远离支阻位" };
}

// ============================================================
// 出场条件（止损 + 三档止盈）与同向持仓互斥（纯函数，可单测）
// ============================================================

/**
 * 方向感知的止损参考位：short 取进场价上方最近的支阻位（阻力）、long 取下方最近（支撑）。
 * 信号自带的 nearSr（进场校验时按绝对价差最近命中，不分上下方）若已在正确侧直接沿用；
 * 否则从 srLevels 重选正确侧最近位（进场判定逻辑不变，此处仅供出场止损参考）。
 * 无正确侧支阻位 → null（该仓不设止损，仅三档止盈出场）。
 * @param {object} sig 进场信号（用 direction/price/nearSr）
 * @param {Array} srLevels 支阻位列表（每项含 price）
 * @returns {null|number} 止损参考位价格
 */
function stopRefOf(sig, srLevels) {
  const isShort = sig.direction === "short";
  const entryP = sig.price;
  if (sig.nearSr != null && (isShort ? sig.nearSr > entryP : sig.nearSr < entryP)) {
    return sig.nearSr;
  }
  let best = null;
  for (const sr of (srLevels || [])) {
    const p = sr.price;
    if (isShort ? p > entryP : p < entryP) {
      const d = Math.abs(p - entryP);
      if (!best || d < best.d) best = { p, d };
    }
  }
  return best ? best.p : null;
}

/**
 * 找 fromT 之后首个完成的指定 type 笔（够笔/破高低点事件源）。
 * @param {Array} bis 笔列表
 * @param {number} fromT 起始时间（秒，不含等于）
 * @param {string} type "up"|"down"
 * @param {boolean} requirePostStart true 时要求 startTime >= fromT（TP3 用：
 *   必须是进场后开始的新笔，排除进场前已存在的同向笔——进场背驰点本身常是「创新高/新低」笔）
 * @param {boolean} breakPrev true 时再要求端点破前一同向笔端点
 *   （up 笔 endPrice > 前一 up 笔 endPrice = 过前高；down 笔 = 破前底）
 * @returns {null|{time:number, price:number}} 笔完成时间（endTime）与端点价
 */
function findBiEvent(bis, fromT, type, requirePostStart, breakPrev) {
  if (!bis) return null;
  for (let i = 0; i < bis.length; i++) {
    const b = bis[i];
    if (b.type !== type) continue;
    if (!(b.endTime > fromT)) continue; // 完成于 fromT 之后
    if (requirePostStart && b.startTime < fromT) continue;
    if (breakPrev) {
      let j = i - 1;
      while (j >= 0 && bis[j].type !== type) j--;
      if (j < 0) continue;
      const broke = type === "up" ? b.endPrice > bis[j].endPrice : b.endPrice < bis[j].endPrice;
      if (!broke) continue;
    }
    return { time: b.endTime, price: b.endPrice };
  }
  return null;
}

/**
 * 出场状态机：对单个进场信号从进场时刻起按时间顺序模拟出场事件。
 *   止损：markRes K线逐根盘中检查（short high>止损位 / long low<止损位），跳空按开盘价成交；
 *         止损位初始 = stopRef，TP1 后 = 进场价（保本）
 *   TP1 保本：markRes 首个进场后完成的有利方向笔（short→down / long→up）
 *   TP2 平一半：periodX 首个进场后完成的有利方向笔（需 TP1 已触发，渐进式）
 *   TP3 全平：periodX 首个进场后开始的不利方向笔、端点破前一同向笔端点（破前底/过前高）
 * 已知口径：bis 最后一笔为延伸中的形成笔（chan-bi 落盘口径），其完成事件按延伸端点时间计。
 * @param {object} sig 进场信号
 * @param {null|number} stopRef stopRefOf 的输出
 * @param {object} markResData 背驰级别周期数据 {bis, bars}
 * @param {object} periodXData 检测周期数据 {bis, bars}
 * @returns {{events:Array<{type:string,time:number,price:number|null}>, closed:boolean}}
 *   type: breakeven | half | close | stopSr（支阻位止损）| stopBe（保本止损）| stillOpen
 */
function simulatePosition(sig, stopRef, markResData, periodXData) {
  const isShort = sig.direction === "short";
  const entryT = sig.time;
  const entryP = sig.price;
  const fav = isShort ? "down" : "up"; // 有利方向笔（short 持仓盼下跌笔）
  const adv = isShort ? "up" : "down"; // 不利方向笔（TP3 破高低点用）
  const tp1 = findBiEvent(markResData && markResData.bis, entryT, fav);
  const tp2 = findBiEvent(periodXData && periodXData.bis, entryT, fav);
  const tp3 = findBiEvent(periodXData && periodXData.bis, entryT, adv, true, true);

  const events = [];
  let stop = stopRef;
  let be = false, half = false;
  // 应用 upto 时刻之前（含）的止盈事件；返回 true 表示已终局（TP3 全平）
  const applyTps = (upto) => {
    for (const [k, e] of [["breakeven", tp1], ["half", tp2], ["close", tp3]]) {
      if (!e || e.time > upto) continue;
      if (k === "breakeven" && !be) {
        be = true;
        stop = entryP; // 保本：止损位上移至进场价
        events.push({ type: "breakeven", time: e.time, price: e.price });
      }
      if (k === "half" && !half && be) {
        half = true;
        events.push({ type: "half", time: e.time, price: e.price });
      }
      if (k === "close") {
        events.push({ type: "close", time: e.time, price: e.price });
        return true;
      }
    }
    return false;
  };

  const bars = (markResData && markResData.bars) || [];
  let closed = false;
  for (const bar of bars) {
    if (bar.time <= entryT) continue; // 进场当根不计（进场K线自身的高低点）
    if (applyTps(bar.time)) { closed = true; break; }
    if (stop != null) {
      const hit = isShort ? bar.high > stop : bar.low < stop;
      if (hit) {
        // 跳空穿越止损位时按开盘价成交（更差价格）
        const fill = isShort ? Math.max(stop, bar.open) : Math.min(stop, bar.open);
        events.push({ type: be ? "stopBe" : "stopSr", time: bar.time, price: fill });
        closed = true;
        break;
      }
    }
  }
  if (!closed) {
    applyTps(Infinity); // 数据末尾仍持仓：补记已触发的止盈事件
    if (!events.some(e => e.type === "close" || e.type === "stopSr" || e.type === "stopBe")) {
      events.push({ type: "stillOpen", time: bars.length ? bars[bars.length - 1].time : entryT, price: null });
    }
  }
  events.sort((a, b) => a.time - b.time);
  return { events, closed };
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

    // 读取 trading-plan 落盘的交易计划数据（强制依赖：判定各周期当前进场状态）
    const planFile = cacheFile("plan", SYMBOL);
    if (!fs.existsSync(planFile)) {
      console.log(`ERROR: 未找到交易计划数据文件 ${planFile}`);
      console.log("请先对当前品种运行「交易计划」SKILL（trading-plan）生成计划数据，再运行本脚本。");
      process.exit(1);
    }
    const planData = JSON.parse(fs.readFileSync(planFile, "utf8"));
    if (planData.symbol !== SYMBOL) {
      console.log(`ERROR: 交易计划数据文件属于 ${planData.symbol}，与当前品种 ${SYMBOL} 不一致`);
      console.log("请先对当前品种运行「交易计划」SKILL（trading-plan）生成计划数据，再运行本脚本。");
      process.exit(1);
    }
    const planPeriods = planData.periods || {};
    if (Object.keys(planPeriods).length === 0) {
      console.log("ERROR: 交易计划数据为空（periods 无数据），请先运行「交易计划」SKILL。");
      process.exit(1);
    }
    console.log(`已读取交易计划: ${planFile}（${Object.keys(planPeriods).length} 个周期）`);

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
    // windowDays：小周期窗口（如 30S 只取最近 N 天）——秒级K线为覆盖数月前的起始日期
    // 而加载完整历史必然超时，仿 chan-bi 的 effFrom 写法，覆盖目标改为
    // max(fromTs, 最新K线时间 - windowDays*86400)
    const fetchBars = async (fromTs, buffer, windowDays) => {
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
            const windowDays = ${JSON.stringify(windowDays || null)};
            const tolerance = ${JSON.stringify(tolerance)};
            const latestTs = bars.length ? bars[bars.length - 1].time : 0;
            const effFrom = (fromTs && windowDays) ? Math.max(fromTs, latestTs - windowDays * 86400) : fromTs;
            if (bars[0].time > effFrom + tolerance) {
              return { bars: [], notCovered: true, len: bars.length, resolution: String(chart.resolution()) };
            }
            const fromIdx = bars.findIndex(k => k.time >= effFrom);
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
    // 预取各周期数据（K线/ATR/MACD），供策略条件判定与「以下级别背驰」检测
    // ============================================================
    // 全部参与判定的周期：检测周期（PERIODS）+ 日线（仅作为 240 的上一级别笔）。
    // --with-30s：追加 30S（需 bis 数据中存在 30S 笔，即先用 chan_bi --with-30s 画过笔），
    // 使 3 分钟状态的「以下级别背驰」可以落到 30S 上。
    const ALL_RES = ["D", "240", "60", "15", "3", ...(WITH_30S ? ["30S"] : [])]
      .filter(r => periodBis[r] && periodBis[r].length > 0);
    // 小周期数据窗口（与 chan-bi/mark-buy-sell 的 DRAW_WINDOW_DAYS 一致）：
    // 3分钟只取最近 15 天、15分钟最近 30 天、30秒（--with-30s）最近 3 天。
    // 笔数据（bis）本身就只含这些窗口（chan-bi 窗口过滤后落盘），K线/MACD 取同窗口即对齐；
    // 不加窗口会把 3m 历史往回加载到 --from（数月、数万根），TV 加载不到该深度导致周期被跳过。
    const DRAW_WINDOW_DAYS = { "3": 15, "15": 30, "30S": 3 };
    const periodData = {};
    let loadRes = originalRes;
    for (const res of ALL_RES) {
      if (res !== loadRes) {
        await ensureResolution(res);
        loadRes = res;
      }
      const d = await fetchBars(FROM_TS, BAR_BUFFER, DRAW_WINDOW_DAYS[res]);
      if (!d || !d.bars || d.bars.length === 0) {
        console.log(`\n[周期 ${res}] 读取K线失败，跳过该周期数据`);
        continue;
      }
      periodData[res] = {
        bis: periodBis[res],
        bars: d.bars,
        atr: calcATR(d.bars, 14),
        macdArr: calcMACD(d.bars),
      };
      if (DEBUG) console.log(`[数据] ${res}: K线 ${d.bars.length} 根 ATR=${periodData[res].atr.toFixed(2)} 笔 ${periodBis[res].length}`);
    }
    // 上一级别周期映射：240→D、60→240、15→60、3→15
    const upperResOf = (res) => {
      const sec = intervalSecOf(res) || 0;
      let best = null;
      for (const r of ALL_RES) {
        const s = intervalSecOf(r) || 0;
        if (s > sec && (!best || s < intervalSecOf(best))) best = r;
      }
      return best;
    };

    // ============================================================
    // 逐周期判定进场状态（依赖交易计划 plan 结果）→ 生成进场信号
    // ============================================================
    // allEntries 按「背驰级别（标记级别）」聚合：periods[markRes] = [信号...]
    let allEntries = {};
    for (const res of PERIODS) {
      const pd = periodData[res];
      if (!pd) {
        console.log(`\n[周期 ${res}] 无周期数据，跳过`);
        continue;
      }
      // 从交易计划结果取该周期状态
      const plan = planPeriods[res];
      const planStrategy = plan ? plan.strategy : null;
      if (!planStrategy || plan.direction === "观望") {
        console.log(`\n[周期 ${res}] 交易计划无进场状态（${planStrategy || "无策略"}），跳过`);
        continue;
      }
      const strategy = entryStrategyOf(planStrategy);
      if (!strategy) {
        console.log(`\n[周期 ${res}] 交易计划策略「${planStrategy}」无对应进场策略，跳过`);
        continue;
      }
      const ctx = {
        res,
        bis: pd.bis,
        upperBis: (upperResOf(res) && periodData[upperResOf(res)]) ? periodData[upperResOf(res)].bis : null,
        macdArr: pd.macdArr,
        atr: pd.atr,
        barSec: intervalSecOf(res),
        nearAtr: NEAR_ATR,
        srLevels,
        periodData,
      };
      const evalRes = evaluateEntry(ctx, strategy);
      if (!evalRes.ok) {
        console.log(`\n[周期 ${res}] 策略「${strategy.label}」条件未满足：${evalRes.reason}`);
        continue;
      }
      // 命中：在背驰级别标记箭头
      const sig = {
        periodX: res,
        time: evalRes.point.time,
        price: evalRes.point.price,
        direction: strategy.direction,
        strategyKey: strategy.key,
        nearSr: evalRes.nearSr,
        color: strategy.direction === "long" ? BUY_COLOR : SELL_COLOR,
      };
      (allEntries[evalRes.markRes] = allEntries[evalRes.markRes] || []).push(sig);
      const dirName = sig.direction === "long" ? "买点(向上)" : "卖点(向下)";
      const colorName = sig.direction === "long" ? "红" : "绿";
      console.log(`\n[周期 ${res}] 策略「${strategy.label}」命中：背驰级别 ${evalRes.markRes}，${toT(sig.time)} @ ${sig.price.toFixed(2)} [${dirName} ${colorName}] 靠近支阻位 ${sig.nearSr.toFixed(2)}`);
    }
    // ============================================================
    // 同向持仓互斥 + 出场模拟（多空各自独立状态机，互不影响）
    // ============================================================
    // 规则：同方向持仓未终局（未止损/未全平）不再开新仓；同时刻同向共振信号
    // 按检测周期从大到小取一条（D>240>60>15>3>30S），其余标记 suppressed（落盘保留、不画箭头）。
    const flatSigs = [];
    for (const mr of Object.keys(allEntries)) {
      for (const s of allEntries[mr]) flatSigs.push(Object.assign({ markRes: mr }, s));
    }
    const prioOf = (res) => intervalSecOf(res) || 0;
    flatSigs.sort((a, b) => (a.time - b.time) || (prioOf(b.periodX) - prioOf(a.periodX)));
    // openPos[dir] = { sig, endTime }：endTime = 该仓终局时间（仍持仓 = Infinity）。
    // 信号时间早于终局时间的同向新信号被过滤；晚于终局时间（平仓后）可再进场。
    const openPos = { long: null, short: null };
    const EXIT_NAMES = {
      breakeven: "保本", half: "平一半", close: "全平",
      stopSr: "支阻位止损", stopBe: "保本止损", stillOpen: "仍持仓",
    };
    for (const s of flatSigs) {
      const held = openPos[s.direction];
      if (held && held.endTime > s.time) {
        s.suppressed = true;
        s.suppressedBy = held.sig.time;
        console.log(`[互斥] ${toT(s.time)} ${s.direction === "long" ? "做多" : "做空"}（检测周期 ${s.periodX}）被过滤：同向仓 ${toT(held.sig.time)}（${held.sig.periodX}）持仓中`);
        continue;
      }
      s.stopRef = stopRefOf(s, srLevels);
      const sim = simulatePosition(
        s, s.stopRef,
        periodData[s.markRes] || null,
        periodData[s.periodX] || null,
      );
      s.exits = sim.events;
      s.state = sim.closed ? "closed" : "open";
      const terminal = [...sim.events].reverse().find(e => e.type === "close" || e.type === "stopSr" || e.type === "stopBe");
      openPos[s.direction] = { sig: s, endTime: terminal ? terminal.time : Infinity };
      const evDesc = sim.events.map(e => `${EXIT_NAMES[e.type] || e.type} ${toT(e.time)}${e.price != null ? " @" + e.price.toFixed(2) : ""}`).join(" → ");
      console.log(`[持仓] ${toT(s.time)} ${s.direction === "long" ? "做多" : "做空"}（检测周期 ${s.periodX}，止损参考 ${s.stopRef != null ? s.stopRef.toFixed(2) : "无"}）: ${evDesc || "无出场事件"}`);
    }
    // 重新按背驰级别聚合（flatSigs 为带互斥/出场信息的信号副本，落盘与绘制都用它）
    allEntries = {};
    for (const s of flatSigs) {
      (allEntries[s.markRes] = allEntries[s.markRes] || []).push(s);
    }

    console.log("\n=== 进出场信号汇总 ===");
    let totalSignals = 0, suppressedCnt = 0, openCnt = 0;
    for (const mr of Object.keys(allEntries)) {
      const arr = allEntries[mr];
      for (const s of arr) {
        if (s.suppressed) suppressedCnt++;
        else if (s.state === "open") openCnt++;
      }
      totalSignals += arr.length;
      console.log(`  标记级别 ${mr}: ${arr.length} 个信号（互斥过滤 ${arr.filter(s => s.suppressed).length}）`);
    }
    console.log(`  共 ${totalSignals} 个信号，互斥过滤 ${suppressedCnt} 个，仍持仓 ${openCnt} 个`);

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
     * 清除某周期的旧进出场标记（title = <prefix><周期>，ENTRY_ 箭头 / EXIT_ 出场）。
     */
    const clearEntry = async (res, prefix) => {
      const TITLE = (prefix || "ENTRY_") + res;
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
     * 在某周期绘制进出场箭头（买点=arrow_up 红、卖点=arrow_down 绿，只本周期显示）。
     * 每个信号带 color 字段（生成时按方向赋值 BUY_COLOR/SELL_COLOR）。
     */
    const createEntry = async (res, entries) => {
      const TITLE = "ENTRY_" + res;
      const IV_CFG = onlyThisInterval(res);
      const r = await client.Runtime.evaluate({
        expression: `(async function() {
          const chart = TradingViewApi.activeChart();
          const ENTRIES = ${JSON.stringify(entries)};
          const TITLE = "${TITLE}";
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
              const color = e.color || (e.direction === 'long' ? '${BUY_COLOR}' : '${SELL_COLOR}');
              const id = await chart.createMultipointShape(
                [{ time: e.time, price: e.price }],
                { shape: shape, lock: false, overrides: { color: color, arrowColor: color, title: TITLE } }
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

    /**
     * 在某周期绘制出场标记（统一黄色箭头 #FFEB3B，title = EXIT_<标记级别>）：
     *   方向=平仓方向（多头出场 ↓ / 空头出场 ↑）；保本/仍持仓不画图（仅落盘）。
     */
    const createExitMarks = async (res, marks) => {
      const TITLE = "EXIT_" + res;
      const IV_CFG = onlyThisInterval(res);
      const r = await client.Runtime.evaluate({
        expression: `(async function() {
          const chart = TradingViewApi.activeChart();
          const MARKS = ${JSON.stringify(marks)};
          const TITLE = "${TITLE}";
          const IV_CFG = ${JSON.stringify(IV_CFG)};
          const COLOR = "${EXIT_COLOR}";
          const out = { ok: 0, err: [] };

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

          for (const m of MARKS) {
            try {
              // 出场 = 单点箭头（arrow_down 平多 / arrow_up 平空），颜色黄色：
              // 箭头图标颜色为独立字段 arrowColor（不走顶层 color/textColor）
              const PTS = [{ time: m.time, price: m.price }];
              const OPTS = { shape: m.shape, lock: false,
                             text: m.text || "",
                             color: COLOR, textColor: COLOR,
                             overrides: { arrowColor: COLOR, color: COLOR, textColor: COLOR, title: TITLE } };
              const id = await chart.createMultipointShape(PTS, OPTS);
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

    // 逐周期清除旧箭头并绘制新箭头
    let drawCurrentRes = originalRes;
    // 清除阶段：遍历所有检测周期与标记级别，无论本次是否识别出信号，都先清除该周期的旧箭头，
    // 避免「某周期本次无信号 / 笔数据不足」时旧箭头残留（旧箭头不会因本次无信号而自动消失）。
    const clearResList = Array.from(new Set([...PERIODS, ...Object.keys(allEntries)]));
    for (const res of clearResList) {
      if (res !== drawCurrentRes) {
        await ensureResolution(res);
        drawCurrentRes = res;
      }
      const clearedEntry = await clearEntry(res, "ENTRY_");
      const clearedExit = await clearEntry(res, "EXIT_");
      console.log(`\n[周期 ${res}] 已清除旧进场箭头: ${clearedEntry.cleared} 个，旧出场标记: ${clearedExit.cleared} 个`);
    }
    console.log("\n=== 绘制阶段（买点红箭头 / 卖点绿箭头 / 出场灰色标记）===");
    // 信号已按「背驰级别（标记级别）」聚合，按标记级别绘制。
    for (const res of Object.keys(allEntries)) {
      // 互斥过滤的信号不画箭头（仅落盘）
      const entries = (allEntries[res] || []).filter(e => !e.suppressed);
      if (!entries || entries.length === 0) continue;
      // 出场标记：与进场同款箭头（统一黄 #FFEB3B），方向 = 平仓方向（多头出场 ↓ /
      // 空头出场 ↑，与 Web 控制台 ML· 出场一致）；文本 = 事件名；保本/仍持仓仅落盘。
      const EXIT_NM = { stopSr: "止损", stopBe: "保损", close: "全平", half: "半平" };
      const exitMarks = [];
      for (const e of entries) {
        const shape = e.direction === "long" ? "arrow_down" : "arrow_up";
        for (const ev of (e.exits || [])) {
          if (ev.type === "stopSr" || ev.type === "stopBe" || ev.type === "close"
              || ev.type === "half") {
            exitMarks.push({ shape, text: `${EXIT_NM[ev.type] || ev.type} ${ev.price != null ? ev.price.toFixed(2) : ""}`,
                             time: ev.time, price: ev.price });
          }
        }
      }
      // 箭头创建在「低一级」周期：低一级周期 bar 边界更细，锚点时间（笔端点已校准到
      // 低一级边界）在其上精确定位；最小周期（默认 3 分钟、--with-30s 时为 30 秒）
      // 无更低级别、锚点在其自身读取始终返回原始时间（即使数据未覆盖），是天然稳定锚定周期。
      const drawRes = lowerResOf(res) || res;
      if (drawRes !== drawCurrentRes) {
        await ensureResolution(drawRes);
        drawCurrentRes = drawRes;
      }
      // 绘制前确保图表数据覆盖最早信号/出场时间（避免标记被吸附到数据边缘）
      const minTs = exitMarks.reduce(
        (m, k) => Math.min(m, k.time),
        entries.reduce((m, e) => Math.min(m, e.time), Infinity),
      );
      if (minTs !== Infinity) await ensureBarsCover(drawRes, minTs);
      const result = await createEntry(res, entries);
      console.log(`\n=== 绘制结果 [标记级别 ${res}]（创建于 ${drawRes}）===`);
      console.log(JSON.stringify(result, null, 2));
      if (exitMarks.length > 0) {
        const exitResult = await createExitMarks(res, exitMarks);
        console.log(`=== 出场标记 [标记级别 ${res}]: ${exitMarks.length} 个（黄箭头：平多 ↓ / 平空 ↑）===`);
        if (DEBUG) console.log(JSON.stringify(exitResult, null, 2));
      }
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
      const minTs = origEntries.reduce(
        (m, e) => (e.exits || []).reduce((mm, ev) => Math.min(mm, ev.time), Math.min(m, e.time)),
        Infinity,
      );
      if (minTs !== Infinity) await ensureBarsCover(originalRes, minTs);
    }
    console.log("\n已切回原周期:", originalRes);

    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
  }
})();
