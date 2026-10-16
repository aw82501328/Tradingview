/**
 * 交易计划脚本（独立 SKILL：trading-plan）—— 2026-10-16 起仅保留两项判定：
 *   1. 震荡 regime（rangeRes 开启时：参考周期判震荡 → 更低周期观望；
 *      默认关闭 = 无震荡闸，开单链路直接走买卖点）
 *   2. 顺势方向状态（trendRes 开启时：参考周期方向过滤更低周期进场；默认关闭）
 * 原逐周期「方向+策略」计划行与图上蓝色策略文字已取消——进出场（mark-entry）
 * 直接消费 chan-core 买卖点（点类型+强弱 → 进场键），不再读取 plan_<品种>.json。
 *
 * 输出：
 *   1. 文本摘要：震荡 regime / 顺势方向（开启时）；
 *   2. plan_<品种>.json 落盘为 regime-only 结构（regime/trend 两字段）；
 *   3. 清除图上遗留的旧 CHAN_PLAN_* 策略文字（不再绘制新文字）。
 *
 * 用法：
 *   node trading_plan.js                     计算 regime/顺势并落盘 + 清旧标记
 *   node trading_plan.js --range-res=240     开启震荡参考周期（4小时）
 *   node trading_plan.js --trend-res=240     开启顺势参考周期（4小时）
 *   node trading_plan.js --dry               不动图（不清标记）
 *
 * 算法来源：缠论算法复用 chan-core（唯一算法源）；震荡判定 isRangeBound
 * 复制自 chan-status SKILL（.cursor/skills/chan-status/scripts/chan_status.js，
 * 为保持 chan-status 不受改动，不迁移、不修改，仅复制并标注来源）。
 */
const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");
// 缠论算法核心（唯一算法源，与 chan-bi / mark-buy-sell / chan-zs / chan-status 共用）
const core = require("../../chan-core/scripts/chan_core.js");
const {
  calcATR, calcMACD, findBuyPoints, findSellPoints, buildZS, buildZSByUpper, fmtT, intervalSecOf,
  markWickBars, mergeBars, findFractals,
} = core;

// 缓存目录：chan-bi 画笔落盘笔数据（强制依赖）
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
const PERIODS = getStrArg("periods", "D,240,60,15,3")
  .split(",").map(s => s.trim()).filter(Boolean);
// 震荡判定阈值（WEB 参数中心可调；默认值与 py_chain/trading_plan.py RANGE_DEFAULTS 一致）
const numArg = (name, def) => {
  const v = parseFloat(getStrArg(name, ""));
  return Number.isFinite(v) ? v : def;
};
const RANGE_CFG = {
  rangeBoundOn: getStrArg("range-bound-on", "1") !== "0",
  rangeBarN: Math.round(numArg("range-bar-n", 40)),
  rangeBiN: Math.round(numArg("range-bi-n", 4)),
  rangeKMult: numArg("range-k-mult", 5.0),
  rangeBiMult: numArg("range-bi-mult", 7.0),
  rangeBreakMult: numArg("range-break-mult", 1.0),
  rangeZsOn: getStrArg("range-zs-on", "1") !== "0",
};
// 震荡判定参考周期（对齐 py_chain/trading_plan.py RANGE_RES；WEB 参数中心 plan 模块
// rangeRes，analysis_service 透传 --range-res）。开启（"240"/"D"）= 参考周期闸门：
// 更低周期 regime 震荡即观望；""（off，默认）= 关闭 → 无震荡闸，直接走买卖点
const RANGE_RES_ARG = getStrArg("range-res", "");
const RANGE_RES = (RANGE_RES_ARG === "off" || RANGE_RES_ARG === "") ? "" : RANGE_RES_ARG;

// 参数中心（WEB 参数配置页）整体覆盖；未知键在 JS 侧闲置无害
const CHAN_CFG_JSON = getStrArg("chan-cfg", "");
if (CHAN_CFG_JSON) {
  try { Object.assign(core.CHAN_CFG, JSON.parse(CHAN_CFG_JSON)); }
  catch (e) { console.log("警告: --chan-cfg JSON 解析失败，忽略该参数"); }
}

// ============================================================
// 纯函数：震荡判定（复制自 chan-status SKILL，保持原逻辑不变）
// ============================================================

/**
 * 震荡（横盘整理）判定：K线重叠度高、价格变化不大、无明确方向。
 * 三条件同时满足才判定为震荡：
 *   1. K线区间小：最近 rangeBarN 根K线的 maxHigh - minLow <= rangeKMult × ATR
 *   2. 笔端点区间小：最近 rangeBiN 笔的端点极差（max-min）<= rangeBiMult × ATR
 *   3. 方向性弱：最近 rangeBiN 笔中涨跌交替（同时存在 up 与 down，且无明显单边）
 * 突破跳过：最后一笔终点相对窗口区间的另一端明显偏移（> rangeBreakMult × ATR）
 *   视为突破盘整，跳过 A 震荡判定（返回 range:false, breakOut:true）。
 * 来源：chan-status/.cursor/skills/chan-status/scripts/chan_status.js（isRangeBound），
 * 本版本在原逻辑基础上增加了「最后一笔明显突破窗口区间则跳过 A」规则。
 * @param {Array} bis     本周期笔列表
 * @param {Array} bars    本周期原始K线（含 high/low，可为空——为空则跳过判定）
 * @param {number} atr    本周期 ATR
 * @param {object} cfg    可选阈值覆盖（用于测试）
 * @returns {{range:boolean, kSpan:number, biSpan:number, kAtr:number, biAtr:number, alt:boolean, winBiCount:number, breakOut?:boolean, breakMult?:number}|null} 判定结果；bars 缺失时返回 null（跳过）
 */
function isRangeBound(bis, bars, atr, cfg) {
  if (!bars || bars.length === 0 || !bis || bis.length < 3 || !atr || atr <= 0) return null;
  const c = cfg || RANGE_CFG;
  const rangeBarN = c.rangeBarN ?? 40, rangeBiN = c.rangeBiN ?? 4;
  const rangeKMult = c.rangeKMult ?? 5.0, rangeBiMult = c.rangeBiMult ?? 7.0;
  const rangeBreakMult = c.rangeBreakMult ?? 1.0;

  // 条件1：最近 rangeBarN 根K线区间
  const win = bars.slice(-rangeBarN);
  const maxH = Math.max(...win.map(b => b.high));
  const minL = Math.min(...win.map(b => b.low));
  const kSpan = maxH - minL;
  const kAtr = kSpan / atr;

  // 最后一笔明显突破窗口区间 → 跳过震荡判定（视为趋势）
  const lastBi = bis[bis.length - 1];
  if (lastBi) {
    let broke = false;
    if (lastBi.type === "up") broke = lastBi.endPrice > minL + rangeBreakMult * atr;
    else if (lastBi.type === "down") broke = lastBi.endPrice < maxH - rangeBreakMult * atr;
    if (broke) {
      return { range: false, kSpan, biSpan: 0, kAtr, biAtr: 0, alt: true,
               breakOut: true, breakMult: rangeBreakMult,
               rangeBarN, rangeBiN, winBiCount: 0 };
    }
  }

  // 条件2/3 的前提：笔须落在「最近 rangeBarN 根K线」的时间范围内
  const winStart = win[0].time, winEnd = win[win.length - 1].time;
  const inWin = bis.filter(b =>
    (b.startTime >= winStart && b.startTime <= winEnd) ||
    (b.endTime >= winStart && b.endTime <= winEnd));
  let biAtr = 0, biSpan = 0, hasBoth = true, alt = true;
  if (inWin.length > 0) {
    const recentBis = inWin.slice(-rangeBiN);
    const endpoints = [];
    for (const b of recentBis) { endpoints.push(b.startPrice); endpoints.push(b.endPrice); }
    biSpan = Math.max(...endpoints) - Math.min(...endpoints);
    biAtr = biSpan / atr;
    const types = recentBis.map(b => b.type);
    hasBoth = types.includes("up") && types.includes("down");
    alt = true;
    for (let i = 2; i < types.length; i++) {
      if (types[i] === types[i - 1] && types[i] === types[i - 2]) { alt = false; break; }
    }
  }
  const biOk = inWin.length === 0 || (biAtr <= rangeBiMult && hasBoth && alt);
  const isRange = kAtr <= rangeKMult && biOk;
  return { range: isRange, kSpan, biSpan, kAtr, biAtr, alt, rangeBarN, rangeBiN, winBiCount: inWin.length };
}

/**
 * A/B 两支震荡判定（对齐 py_chain _rangeVerdict）：A 横盘 / B 未离开中枢且现价在箱内。
 * @returns {{reason:string}|null} 命中即返回震荡理由；非震荡返回 null
 */
function rangeVerdict(bis, bars, atr, upperBis, lastPrice, barSec) {
  if (RANGE_CFG.rangeBoundOn !== false) {
    const rb = isRangeBound(bis, bars, atr, RANGE_CFG);
    if (rb && rb.range) {
      const reason = `最近 ${rb.rangeBarN} 根K线区间 ${rb.kSpan.toFixed(2)}（${rb.kAtr.toFixed(1)}×ATR）${rb.winBiCount > 0
        ? `，笔端点区间 ${rb.biSpan.toFixed(2)}（${rb.biAtr.toFixed(1)}×ATR），涨跌交替无明确方向`
        : `，窗口内无笔`}，判定为震荡整理`;
      return { reason };
    }
  }
  if (RANGE_CFG.rangeZsOn !== false) {
    let zss = [];
    try {
      if (upperBis && upperBis.length > 0) {
        zss = buildZSByUpper(bis, upperBis, barSec);
      } else {
        zss = buildZS(bis, barSec);
      }
    } catch (e) {
      if (DEBUG) console.log(`[regime] buildZS 失败: ${e.message}`);
    }
    const upperLast = upperBis && upperBis.length > 0 ? upperBis[upperBis.length - 1] : null;
    const zsList = upperLast
      ? zss.filter(z => z.upperStart != null && z.upperStart >= upperLast.startTime - barSec)
      : zss;
    const lastZS = zsList[zsList.length - 1];
    if (lastZS && lastZS.exitTime == null && lastPrice != null && lastPrice >= lastZS.zd && lastPrice <= lastZS.zg) {
      const reason = `存在未离开中枢 [${lastZS.zd.toFixed(2)}, ${lastZS.zg.toFixed(2)}]（归属上一级别同一笔内），当前价 ${lastPrice.toFixed(2)} 位于中枢内，判定为震荡整理`;
      return { reason };
    }
  }
  return null;
}

/**
 * 参考周期震荡 regime（对齐 py_chain _range_gate_row）：笔 <2 → insufficient
 * （更低周期观望·笔数据不足）；否则 rangeVerdict 做 A/B——命中即 regime 震荡。
 * @param {string} res       参考周期名（如 "240"）
 * @param {Array}  bis       参考周期笔列表
 * @param {Array}  bars      参考周期K线
 * @param {number} atr       参考周期 ATR
 * @param {Array}  upperBis  参考周期的上一级笔（B 支中枢归属用，可为空）
 * @param {number} lastPrice 最新价
 * @returns {{range:boolean, resName:string, reason:string, insufficient?:boolean}}
 */
function rangeGateRow(res, bis, bars, atr, upperBis, lastPrice) {
  const name = trendResName(res);
  if (!bis || bis.length < 2) {
    return { range: true, insufficient: true, resName: name,
             reason: `${name}笔数据不足（少于2笔），无法判定震荡/趋势，观望` };
  }
  const v = rangeVerdict(bis, bars, atr, upperBis, lastPrice, intervalSecOf(res) || 60);
  if (v) return { range: true, resName: name, reason: v.reason || "" };
  return { range: false, resName: name, reason: "" };
}

/**
 * 震荡 regime 汇总入口（对齐 py_chain compute_regime；mark_entry 震荡闸消费）。
 * @param {object} periodBis     各周期笔 { res: [bis] }
 * @param {object} barsByPeriod  各周期原始K线 { res: [bars] }
 * @param {Array}  periods       周期列表（含参考周期）
 * @param {string} rangeRes      震荡参考周期（""/off = 关闭 → null）
 * @returns {{range:boolean, insufficient?:boolean, resName:string, reason:string}|null}
 */
function computeRegime(periodBis, barsByPeriod, periods, rangeRes) {
  if (!rangeRes || rangeRes === "off") return null;
  const ref = periods.find(r => String(r).toUpperCase() === String(rangeRes).toUpperCase());
  const curBis = ref ? (periodBis[ref] || []) : [];
  const name = trendResName(rangeRes);
  if (!curBis.length) {
    return { range: true, insufficient: true, resName: name,
             reason: `${name}笔数据不足（少于2笔），无法判定震荡/趋势，观望` };
  }
  // 上级周期取法与 trendStateOf 同口径：比参考周期大一级的最小周期
  const sec = intervalSecOf(ref) || 0;
  let upper = null, best = null;
  for (const r of Object.keys(periodBis)) {
    const s = intervalSecOf(r) || 0;
    if (s > sec && (best === null || s < (intervalSecOf(best) || 0))) best = r;
  }
  if (best !== null) upper = periodBis[best];
  const rawBars = (barsByPeriod || {})[ref] || [];
  const closed = rawBars.filter(b => b.time + sec <= Math.floor(Date.now() / 1000));
  const atr = calcATR(closed, 14) || 0;
  const lastBar = closed[closed.length - 1];
  const lastPrice = lastBar ? lastBar.close : null;
  const rangeN = RANGE_CFG.rangeBarN ?? 40;
  const rangeBars = closed.length > rangeN ? closed.slice(-rangeN) : closed;
  return rangeGateRow(ref, curBis, rangeBars, atr, upper, lastPrice);
}

// ============================================================
// 参考周期方向判定（顺势过滤，2026-09-15 口径与用户逐条确认；
// 与 py_chain/trading_plan.py 同步维护，规则文本另见 WEB 参数页交易计划页签）
// ============================================================

// 顺势参考周期默认值："" = 关闭（2026-10-10 与 py TREND_RES 对齐）；"240" = 4小时；"D" = 日线
// （WEB 参数中心 plan 模块 trendRes；mark-entry 进场方向过滤消费）
const TREND_RES = "";

// 周期 → 中文名（方向成因展示用，如「4小时2买」「日线1卖」）
const RES_NAME_CN = { "D": "日线", "240": "4小时", "60": "1小时", "30": "30分钟", "15": "15分钟", "3": "3分钟", "30S": "30秒" };
const trendResName = (res) => RES_NAME_CN[String(res).toUpperCase()] || String(res);

// t（含）之后是否出现强分型（kind="bottom"|"top"）。
// 强分型只比实体、不含影线：右肩收盘与左肩开盘比较。
// 底分型：右肩收盘 > 左肩开盘。顶分型：右肩收盘 < 左肩开盘。合并K/分型与 buildBi 同源
// （markWickBars → mergeBars → findFractals）。
function strongFractalAfter(merged, fractals, t, kind) {
  for (const f of fractals) {
    if (f.type !== kind || f.time < t) continue;
    const i = f.mergedIdx;
    if (i - 1 < 0 || i + 1 >= merged.length) continue; // 左/右肩不完整（尾部形成中）
    const left = merged[i - 1], right = merged[i + 1];
    if (kind === "bottom" && right.close > left.open) return true;
    if (kind === "top" && right.close < left.open) return true;
  }
  return false;
}

// 参考周期方向判定（顺势过滤的唯一口径；mark_entry 进场方向过滤消费）。
// 规则（与 py_chain/trading_plan.py trend_direction 一致）：
//   1. 取参考周期最近一个买卖点（最近 60 笔窗口）；
//   2. 1买/1卖：端点后出现强分型才确立方向，未出现回退末笔方向；
//   3. 非1类点（2买/类2买/3买、2卖/类2卖/3卖）：出现即确立方向；
//   4. 破坏闩锁：确立后任一参考周期收盘价 破 买点端点价（跌）/ 涨破卖点端点价 →
//      反向延续，闩锁到下一个买卖点事件（价格收回不翻回，直到更新的买卖点出现）；
//   5. 无买卖点 / 1类点强分型未出现 → 回退末笔方向（up→多、down→空）；
//   6. 笔数据不足（<2 笔）→ [null, ""]，消费方不过滤。
// @returns [dir, reason]：reason 如 "4小时2买"（点确立）、"4小时下跌延续"（破坏）、
//          "4小时末笔向上"（回退）
function phaseDirection(name, p, bis, bars, upperBis, barSec, rb, tCut = null) {
  const seg = bis[bis.length - 1], t = p.type, isBuy = t.endsWith("买");
  const rawCount = (bars || []).filter(b => b.time > seg.startTime && (tCut == null || b.time <= tCut)).length;
  const merged = mergeBars(markWickBars(bars || []));
  const enough = core.mergedSegmentCount(merged, seg.startTime, barSec) >= (rb.min_bars || 5);
  const strong = seg.span / Math.max(1, rawCount) > (rb.angle_ref ?? 5);
  const near = rb.near_pts ?? 5, extreme = seg.endPrice;
  const nearZs = () => {
    const zs = upperBis && upperBis.length ? buildZSByUpper(bis, upperBis, barSec) : buildZS(bis, barSec);
    const before = zs.filter(z => z.startTime != null && z.startTime <= p.time);
    if (!before.length) return false;
    const z = before[before.length-1];
    return Math.min(Math.abs(extreme-z.zg), Math.abs(extreme-z.zd)) <= near;
  };
  if (t === "1买" || t === "1卖") {
    const same = seg.type === (isBuy ? "up" : "down"), dir = isBuy ? "long" : "short", other = isBuy ? "short" : "long";
    if (same) {
      if (!enough) return [dir, `${name}${t}进行中`];
      if (nearZs()) return strong ? [null, `${name}${t}近中枢观望`] : [other, `${name}${t}转${isBuy ? "空" : "多"}预期`];
      return strong ? [dir, `${name}${t}进行中`] : [null, `${name}${t}后方向不明`];
    }
    if (seg.startTime <= p.time) return [dir, `${name}${t}进行中`];
    if (!enough) return [other, `${name}${t}${isBuy ? "回调" : "反弹"}`];
    return strong ? [other, `${name}${t}${isBuy ? "强回" : "强反"}`] : [dir, `${name}${isBuy ? "2买" : "2卖"}预期`];
  }
  if (seg.type === (isBuy ? "down" : "up")) return [null, `${name}${t}${isBuy ? "回调中" : "反弹中"}`];
  const prevs = bis.slice(0,-1).filter(b => b.type === (isBuy ? "up" : "down") && b.endTime <= p.time);
  if (prevs.length && Math.abs(extreme-prevs[prevs.length-1].endPrice) <= near) return [null, `${name}${t}${isBuy ? "前高" : "前低"}附近`];
  if (!enough || strong) return [isBuy ? "long" : "short", `${name}${t}后${isBuy ? "上涨" : "下跌"}`];
  return [null, `${name}${t}后方向不明`];
}

function trendDirection(res, bis, bars, upperBis, macdArr, tCut = null, rebound = null, lowerStroke = null,
                        upperMacd = null) {
  const name = trendResName(res);
  if (bis && bis.length && !bis[bis.length-1]._contextReady) {
    bis = core.buildStructureContext(bis, bars, intervalSecOf(res), tCut, null, null, null, lowerStroke).bis;
    if (tCut != null) bars = (bars || []).filter(b => b.time + intervalSecOf(res) <= tCut);
  }
  if (!bis || bis.length < 2) return [null, ""];
  const win = bis.slice(-60); // 只看最新结构
  const barSec = intervalSecOf(res);
  let pts = [];
  try {
    pts = findBuyPoints(win, upperBis, macdArr, barSec, undefined, undefined, upperMacd)
      .concat(findSellPoints(win, upperBis, macdArr, barSec, undefined, undefined, upperMacd));
  } catch (e) {
    pts = [];
  }
  if (tCut != null) pts = pts.filter(p => p.time <= tCut);
  pts.sort((a, b) => a.time - b.time);
  let fallback = bis[bis.length - 1].type === "up"
    ? ["long", `${name}末笔向上`] : ["short", `${name}末笔向下`];
  const current = bis[bis.length-1];
  if (current._forming) fallback = [current.type === "up" ? "long" : "short", `${name}${current.phase === "expected" ? "预期" : "够笔"}${current.type === "up" ? "上涨" : "下跌"}`];
  if (!pts.length) return fallback;
  const p = pts[pts.length - 1];
  const t = p.type;
  const isBuy = ["1买", "2买", "类2买", "3买", "类3买", "4买", "类4买"].includes(t);
  if (t === "1买" || t === "1卖") {
    // 1类点须先出现强分型（合并K链与 buildBi 同源；懒计算——仅 1类点需要）
    const merged = mergeBars(markWickBars(bars || []));
    if (!strongFractalAfter(merged, findFractals(merged), p.time, t === "1买" ? "bottom" : "top")) {
      return fallback;
    }
  }
  // 破坏闩锁：点确立后任一参考周期收盘破端点价 → 反向延续（价格收回不翻回）
  for (const b of bars || []) {
    if (b.time < p.time) continue;
    if (isBuy) {
      if (b.close < p.price) return ["short", `${name}下跌延续`];
    } else if (b.close > p.price) {
      return ["long", `${name}上涨延续`];
    }
  }
  if (rebound && rebound.enabled) {
    const result = phaseDirection(name,p,bis,bars,upperBis,barSec,rebound,tCut);
    if (current.phase === "expected") result[1] += "（预期段）";
    return result;
  }
  return [isBuy ? "long" : "short", `${name}${t}`];
}

// 按配置取参考周期方向状态（mark_entry 顺势过滤统一入口）。
// 上级周期取法与 mark_entry.upperResOf 同口径：periodBis 中比参考周期大一级的最小周期
// （240→D），供 findBuyPoints 区间套使用。
// @returns { dir: "long"|"short"|null, reason: string, res } | null（trendRes 关闭）
function trendStateOf(periodBis, barsByPeriod, trendRes, periodMacd = null, cfg = null, tCut = null) {
  if (!trendRes) return null;
  periodBis = core.structurePeriods(periodBis, barsByPeriod, tCut);
  const tr = String(trendRes).toUpperCase();
  const bis = (periodBis || {})[tr] || [];
  const bars = (barsByPeriod || {})[tr] || [];
  if (!bis.length) return { dir: null, reason: "", res: tr };
  const sec = intervalSecOf(tr) || 0;
  let upper = null, best = null;
  for (const r of Object.keys(periodBis)) {
    const s = intervalSecOf(r) || 0;
    if (s > sec && (best === null || s < (intervalSecOf(best) || 0))) best = r;
  }
  if (best !== null) upper = periodBis[best];
  let macdArr = (periodMacd || {})[tr];
  if (macdArr == null) macdArr = calcMACD(bars);
  let upperMacd = best !== null ? (periodMacd || {})[best] : null;
  if (upperMacd == null && best !== null) {
    const ub = (barsByPeriod || {})[best] || [];
    if (ub.length) upperMacd = calcMACD(ub);
  }
  const rebound = cfg && cfg.trendRebound === false ? null : {enabled:true, near_pts:cfg?.reboundNearPts ?? 5, angle_ref:cfg?.reboundAngleRef ?? 5, min_bars:core.CHAN_CFG.expectBiMinBars || 5};
  const [dir, reason] = trendDirection(tr, bis, bars, upper, macdArr, tCut, rebound, core.lowerStrokePack(tr, periodBis, barsByPeriod), upperMacd);
  return { dir, reason, res: tr };
}

module.exports = {
  isRangeBound,
  rangeVerdict,
  rangeGateRow,
  computeRegime,
  TREND_RES,
  RANGE_RES,
  RES_NAME_CN,
  trendResName,
  strongFractalAfter,
  phaseDirection,
  trendDirection,
  trendStateOf,
};

// ============================================================
// 主流程（被 require 时仅导出纯函数，供单元测试）
// ============================================================

if (require.main === module) {
  main();
}

async function main() {
  let client;
  try {
    const targets = await CDP.List({ port: 9222 });
    const pg = targets.find(t => t.type === "page" && t.url.includes("tradingview.com"));
    if (!pg) { console.log("ERROR: 未找到 TradingView 页面"); process.exit(1); }
    client = await CDP({ target: pg.id, port: 9222 });
    await client.Page.enable();
    await client.Runtime.enable();

    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    const curVal = await client.Runtime.evaluate({
      expression: `(function() {
        const chart = TradingViewApi.activeChart();
        return { symbol: chart.symbol(), resolution: String(chart.resolution()) };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    const SYMBOL = curVal.result.value.symbol;
    const originalRes = curVal.result.value.resolution;
    console.log("品种:", SYMBOL, "当前周期:", originalRes);
    console.log("交易计划（2026-10-16 起 regime-only）：策略行已取消，进出场直接消费买卖点。");

    // 强制依赖画笔数据（与其它 SKILL 一致）
    const bisCachePath = cacheFile("bis", SYMBOL);
    let bisCache = null;
    try {
      if (fs.existsSync(bisCachePath)) bisCache = JSON.parse(fs.readFileSync(bisCachePath, "utf8"));
    if (bisCache && bisCache.analysisInvalidReason) throw new Error(bisCache.analysisInvalidReason);
    } catch (e) {
      console.log("错误: 笔数据文件解析失败:", e.message);
    }
    if (bisCache && bisCache.analysisInvalidReason) throw new Error(bisCache.analysisInvalidReason);
    if (!bisCache || !bisCache.periods || Object.keys(bisCache.periods).length === 0) {
      console.log(`错误: 未找到 ${SYMBOL} 的笔数据文件（${bisCachePath}）。`);
      console.log("本 SKILL 强制依赖 chan-bi 画笔 SKILL：请先运行「画笔」生成笔数据，再运行本脚本。");
      await client.close();
      process.exit(1);
    }
    if (bisCache.symbol !== SYMBOL) {
      console.log(`错误: 笔数据文件属于 ${bisCache.symbol}，与当前品种 ${SYMBOL} 不一致（文件：${bisCachePath}）。`);
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

    const fetchBars = async (expectedIntervalSec) => {
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
            return { bars, resolution: String(chart.resolution()), gap, len: bars.length };
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 15000,
        });
        const d = dataRes.result.value;
        lastD = d;
        if (!d || d.error) return null;
        if (d.gap && d.gap !== expectedIntervalSec && Math.abs(d.gap - expectedIntervalSec) > expectedIntervalSec * 0.3) {
          await sleep(800);
          continue;
        }
        return d;
      }
      console.log(`[fetchBars] ${expectedIntervalSec}s 取数超时，最后一次状态: ` + JSON.stringify(lastD || { error: "no_data" }));
      return null;
    };

    // 清除某周期的旧策略标记（title = CHAN_PLAN_<周期>；策略文字已停画，仅清理遗留）
    const clearPlan = async (res) => {
      const TITLE = "CHAN_PLAN_" + res;
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

    // 清除所有周期遗留的旧策略文字（不再绘制新文字）
    if (!DRY) {
      let clearedAll = 0;
      let currentRes = originalRes;
      for (const res of PERIODS) {
        if (res !== currentRes) { await ensureResolution(res); currentRes = res; }
        const cl = await clearPlan(res);
        clearedAll += cl.cleared;
      }
      console.log("\n清除遗留旧策略标记:", clearedAll, "个（策略文字已停画）");
    }

    // 收笔要下一级笔：先按周期从小到大取K线并建结构
    const barsHeld = {};
    const structuredBis = {};
    const nowCut = Math.floor(Date.now() / 1000);
    let prepRes = originalRes;
    const prepOrder = PERIODS.slice().sort((a, b) => (intervalSecOf(a) || 0) - (intervalSecOf(b) || 0));
    for (const res of prepOrder) {
      if (res !== prepRes) { await ensureResolution(res); prepRes = res; }
      let d = await fetchBars(intervalSecOf(res));
      if (!d || d.error || !d.bars || d.bars.length === 0) {
        await sleep(3000);
        await ensureResolution(res);
        prepRes = res;
        d = await fetchBars(intervalSecOf(res));
      }
      if (!d || d.error || !d.bars || d.bars.length === 0) continue;
      barsHeld[res] = d.bars;
      const lower = core.lowerResOf(res);
      const lowerStroke = (lower && structuredBis[lower] && barsHeld[lower] && barsHeld[lower].length)
        ? { bis: structuredBis[lower], bars: barsHeld[lower], barSec: intervalSecOf(lower) }
        : null;
      structuredBis[res] = core.buildStructureContext(
        bisCache.periods[res] || [], d.bars, intervalSecOf(res), nowCut, null, null,
        lowerStroke).bis;
    }

    // 1. 震荡 regime（rangeRes 开启时；关闭 → null）
    const regime = computeRegime(structuredBis, barsHeld, PERIODS, RANGE_RES);
    if (RANGE_RES) {
      const state = regime && regime.range ? (regime.insufficient ? "笔不足·观望" : "震荡·观望") : "非震荡";
      console.log(`\n[震荡regime] 参考周期 ${trendResName(RANGE_RES)}：${state}${regime && regime.reason ? `（${regime.reason}）` : ""}`);
    } else {
      console.log("\n[震荡regime] rangeRes 已关闭：无震荡闸，直接走买卖点");
    }

    // 2. 顺势方向状态（trendRes 开启时；关闭 → null）
    const TREND_RES_ARG = getStrArg("trend-res", TREND_RES);
    const TREND_RES_CLI = (TREND_RES_ARG === "off" || TREND_RES_ARG === "") ? "" : TREND_RES_ARG;
    const trendState = TREND_RES_CLI
      ? trendStateOf(structuredBis, barsHeld, TREND_RES_CLI, null,
                     { trendRebound: getStrArg("trend-rebound", "1") !== "0",
                       reboundNearPts: numArg("rebound-near-pts", 5),
                       reboundAngleRef: numArg("rebound-angle-ref", 5) })
      : null;
    if (TREND_RES_CLI) {
      const dirName = trendState.dir === "long" ? "多" : trendState.dir === "short" ? "空" : "无方向（不过滤）";
      console.log(`[顺势] 参考周期 ${trendResName(TREND_RES_CLI)}：${dirName}${trendState.reason ? `（${trendState.reason}）` : ""}`);
    } else {
      console.log("[顺势] trendRes 已关闭：不过滤");
    }

    // regime-only 落盘（结构对齐 py_chain compute_regime 输出；mark-entry 不再读取本文件，
    // 仅供工作台查询/审计）
    try {
      fs.mkdirSync(CACHE_DIR, { recursive: true });
      const planFile = cacheFile("plan", SYMBOL);
      const planPayload = {
        symbol: SYMBOL,
        from: null,
        generatedAt: new Date().toISOString(),
        rangeRes: RANGE_RES,
        regime,
        trendRes: TREND_RES_CLI,
        trend: trendState,
      };
      fs.writeFileSync(planFile, JSON.stringify(planPayload, null, 2), "utf8");
      console.log(`\nregime 已落盘: ${planFile}`);
    } catch (e) {
      console.log("警告: regime 落盘失败:", e.message);
    }

    // 切回原周期
    if (originalRes !== prepRes) {
      await ensureResolution(originalRes);
      console.log("\n已切回原周期:", originalRes);
    }

    if (DRY) console.log("\n[DRY RUN] 不动图。");
    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
  }
}
