/**
 * 交易计划脚本（独立 SKILL：trading-plan）
 * 区分各周期（日线/4小时/1小时/15分钟/3分钟）当前是「震荡」还是「趋势」，
 * 趋势时依据「最近笔端点的买卖点类型」生成对应交易策略，
 * 输出各周期交易计划表（品种、周期、方向、策略），并把策略以蓝色文字标记显示在各周期图上。
 *
 * 输出：
 *   1. 文本报告：逐周期输出「方向 + 策略」计划表；
 *   2. 图上用蓝色文字在对应周期的空白处（最新bar右侧上方）标记该周期策略。
 *
 * 用法：
 *   node trading_plan.js           计算并输出计划表 + 图上蓝色策略标记
 *   node trading_plan.js --dry     只输出文本计划表，不绘图
 *
 * 参数：
 *   --periods=...       周期列表（逗号分隔，默认 D,240,60,15,3）
 *   --dry               只输出文本不绘图
 *   --debug             打印调试信息
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
  calcATR, calcMACD, findBuyPoints, findSellPoints, buildZS, buildZSByUpper, isBiDiverge, fmtT, intervalSecOf,
} = core;

// 缓存目录：chan-bi 画笔落盘笔数据（强制依赖）
const CACHE_DIR = path.join(__dirname, "..", "..", "..", "..", ".cursor", "cache");
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

// 图上策略标记的颜色（蓝色）
const PLAN_COLOR = "#2962FF";

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
  const rangeBarN = (cfg && cfg.rangeBarN) || 40;
  const rangeBiN = (cfg && cfg.rangeBiN) || 4;
  const rangeKMult = (cfg && cfg.rangeKMult) || 5.0;
  const rangeBiMult = (cfg && cfg.rangeBiMult) || 7.0;
  const rangeBreakMult = (cfg && cfg.rangeBreakMult) || 1.0;

  // 条件1：最近 rangeBarN 根K线区间
  const win = bars.slice(-rangeBarN);
  const maxH = Math.max(...win.map(b => b.high));
  const minL = Math.min(...win.map(b => b.low));
  const kSpan = maxH - minL;
  const kAtr = kSpan / atr;

  // 新增：最后一笔明显突破窗口区间 → 跳过 A 震荡判定（视为趋势）。
  // 窗口区间 = 最近 rangeBarN 根K线 [minL, maxH]；
  // 最后一笔终点相对窗口区间的另一端明显偏移（超过 rangeBreakMult×ATR）即视为突破盘整：
  //   up 笔  终点高于窗口低点 1×ATR 以上（从窗口底部单边拉升）；
  //   down 笔 终点低于窗口高点 1×ATR 以上（从窗口顶部单边下杀）。
  const lastBi = bis[bis.length - 1];
  if (lastBi) {
    const broke = lastBi.type === "up"
      ? lastBi.endPrice > minL + rangeBreakMult * atr
      : lastBi.type === "down"
        ? lastBi.endPrice < maxH - rangeBreakMult * atr
        : false;
    if (broke) {
      return {
        range: false, kSpan, biSpan: 0, kAtr, biAtr: 0, alt: true,
        breakOut: true, breakMult: rangeBreakMult, rangeBarN, rangeBiN, winBiCount: 0,
      };
    }
  }

  // 条件2/条件3 的前提：笔须落在「最近 rangeBarN 根K线」的时间范围内。
  const winStart = win[0].time;
  const winEnd = win[win.length - 1].time;
  const inWin = bis.filter(b =>
    (b.startTime >= winStart && b.startTime <= winEnd) ||
    (b.endTime >= winStart && b.endTime <= winEnd)
  );
  let biAtr = 0, biSpan = 0, hasBoth = true, alt = true;
  if (inWin.length > 0) {
    const recentBis = inWin.slice(-rangeBiN);
    const endpoints = [];
    for (const b of recentBis) endpoints.push(b.startPrice, b.endPrice);
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
  const range = kAtr <= rangeKMult && biOk;
  return { range, kSpan, biSpan, kAtr, biAtr, alt, rangeBarN, rangeBiN, winBiCount: inWin.length };
}

// ============================================================
// 纯函数：交易计划生成（可单测）
// ============================================================

/**
 * 依据买卖点类型生成交易策略（用户规则）。
 *   1卖 → 空头，等待反弹后做2卖；
 *   1买 → 多头，等待回调后做2买；
 *   2买/类2买/3买 → 买点后过左高不背驰：多头，等待回调后的新买点；
 *                    其他分类：多头（逆势），等待高点附近的一卖；
 *   2卖/类2卖/3卖 → 卖点后过左低不背驰：空头，等待反弹后的新卖点；
 *                    其他分类：空头（逆势），等待低点附近的一买。
 * @param {string} res       周期名
 * @param {string} type      买卖点类型（如 "1卖"、"2买"）
 * @param {string} reason    原因描述
 * @param {string} label     图上标记文案
 * @param {string} cls       2/3类买卖点的后续分类：买点 "过左高不背驰"/"其他"，卖点 "过左低不背驰"/"其他"
 */
function strategyOf(res, type, reason, label, cls) {
  const base = { res, reason, label };
  if (type === "1卖") return { ...base, direction: "空头", strategy: "等待反弹后做2卖" };
  if (type === "1买") return { ...base, direction: "多头", strategy: "等待回调后做2买" };
  if (type === "2买" || type === "类2买" || type === "3买") {
    if (cls === "过左高不背驰") return { ...base, direction: "多头", strategy: "等待回调后的新买点" };
    return { ...base, direction: "多头（逆势）", strategy: "等待高点附近的一卖" };
  }
  if (type === "2卖" || type === "类2卖" || type === "3卖") {
    if (cls === "过左低不背驰") return { ...base, direction: "空头", strategy: "等待反弹后的新卖点" };
    return { ...base, direction: "空头（逆势）", strategy: "等待低点附近的一买" };
  }
  return { ...base, direction: "观望", strategy: "趋势中" };
}

/**
 * 2/3 类买卖点的后续分类判定（用户规则）：
 *   买点（2买/类2买/3买）：买点后第一笔上涨是否「过左高」且「不背驰」。
 *     左高 = 买点之前最近顶端点（上涨笔终点 / 下跌笔起点，取最高）；
 *     过左高 = after（买点后第一笔上涨）终点价 > 左高价；
 *     不背驰 = after 相对前一同向上涨参照笔 isBiDiverge=false（MACD 动能未减弱）。
 *   卖点（2卖/类2卖/3卖）：对称判定。
 *     左低 = 卖点之前最近底端点（下跌笔终点 / 上涨笔起点，取最低）；
 *     过左低 = after（卖点后第一笔下跌）终点价 < 左低价；
 *     不背驰 = after 相对前一同向下跌参照笔 isBiDiverge=false。
 * @param {Array} bis      本周期笔列表
 * @param {Array} macdArr  MACD 数组（可为空，为空时 isBiDiverge 视为不背驰）
 * @param {object} p       买卖点 { type, time, price }
 * @returns {string} "过左高不背驰" | "过左低不背驰" | "其他"
 */
function classifySecond(bis, macdArr, p) {
  const wantUp = /买$/.test(p.type);
  // 买卖点之前最近顶/底端点
  const extreme = { time: -1, price: wantUp ? -Infinity : Infinity };
  for (const b of bis) {
    const cands = [];
    if (wantUp) {
      if (b.type === "up") cands.push({ time: b.endTime, price: b.endPrice });       // 顶：上涨笔终点
      if (b.type === "down") cands.push({ time: b.startTime, price: b.startPrice }); // 顶：下跌笔起点
    } else {
      if (b.type === "down") cands.push({ time: b.endTime, price: b.endPrice });     // 底：下跌笔终点
      if (b.type === "up") cands.push({ time: b.startTime, price: b.startPrice });   // 底：上涨笔起点
    }
    for (const c of cands) {
      if (c.time >= p.time) continue;
      if (wantUp ? c.price > extreme.price : c.price < extreme.price) {
        extreme.time = c.time;
        extreme.price = c.price;
      }
    }
  }
  if (extreme.time === -1) return "其他";
  // 买卖点后第一笔同向笔（买点后上涨 / 卖点后下跌），起点在买卖点之后
  const after = bis.find(b => b.startTime >= p.time && b.type === (wantUp ? "up" : "down"));
  if (!after) return "其他";
  // 过左高 / 过左低
  const passed = wantUp ? after.endPrice > extreme.price : after.endPrice < extreme.price;
  if (!passed) return "其他";
  // 不背驰：after 相对前一同向参照笔（span >= after.span*0.5）isBiDiverge=false
  let refer = null;
  for (let i = bis.indexOf(after) - 1; i >= 0; i--) {
    if (bis[i].type !== after.type) continue;
    if (bis[i].span < after.span * 0.5) continue;
    refer = bis[i];
    break;
  }
  const diverge = refer ? isBiDiverge(after, refer, macdArr) : false;
  if (diverge) return "其他";
  return wantUp ? "过左高不背驰" : "过左低不背驰";
}

/**
 * 核心：对单个周期生成「方向 + 策略」。
 *
 * 判定顺序：
 *   1. 震荡优先：isRangeBound（A 震荡）或 buildZS 最后一个中枢未离开且当前价在中枢内（B 震荡）
 *      → 方向「观望」，策略「震荡整理，观望等待方向选择」；
 *   2. 趋势：获取最近笔端点上的买卖点（findBuyPoints/findSellPoints）：
 *      - 先匹配最后一笔终点上的买卖点 → 按类型映射策略；
 *      - 若最后一笔终点无买卖点（空）→ 再向前获取一笔（逐笔向前扫描最近笔端点），
 *        命中的买卖点同样按类型精确映射（1卖→等待反弹后做2卖 等）；
 *      - 仍无 → 「趋势中无匹配买卖点」。
 *
 * @param {object} opts
 *   res        本周期名（如 "240"）
 *   bis        本周期笔列表（已延伸的最终笔，与图上所画笔一致）
 *   upperBis   上一级别笔列表（可为空）
 *   macdArr    MACD 数组（供买卖点背驰判定，可为空）
 *   atr        本周期 ATR
 *   lastPrice  当前最新价（判断是否在中枢区间内）
 *   bars       本周期实时K线（供震荡判定，可为空）
 * @returns {object} { res, direction, strategy, reason, label, pointDesc }
 *   pointDesc  找到的最近买卖点描述（如 "1卖@8-17 18:00(30262.95)"，供图上标注；震荡/无匹配时为空）
 */
function predictPlan(opts) {
  const { res, bis, upperBis, macdArr, lastPrice, bars } = opts;
  const atr = opts.atr || 0;
  const barSec = opts.barSec || intervalSecOf(res) || 60;
  const empty = { res, direction: "观望", strategy: "数据不足", reason: "笔数量不足，无法判断", label: "数据不足" };
  if (!bis || bis.length < 2) return empty;

  // 1. 震荡优先（A：isRangeBound 横盘判定）
  const rb = isRangeBound(bis, bars, atr);
  if (rb && rb.range) {
    const reason = `最近 ${rb.rangeBarN} 根K线区间 ${rb.kSpan.toFixed(2)}（${rb.kAtr.toFixed(1)}×ATR）${rb.winBiCount > 0
      ? `，笔端点区间 ${rb.biSpan.toFixed(2)}（${rb.biAtr.toFixed(1)}×ATR），涨跌交替无明确方向`
      : `，窗口内无笔`}，判定为震荡整理`;
    return { res, direction: "观望", strategy: "震荡整理，观望等待方向选择", reason, label: "震荡观望" };
  }

  // 1b. 震荡判定（B：存在未离开的中枢且当前价在中枢区间内）
  // 中枢必须构建在「上一级别同一笔」内（buildZSByUpper 分解约束，与 chan-zs 中枢 SKILL 一致）：
  // 用上级笔时间区间把本级别笔切段，每段内独立构建中枢，保证中枢不跨上级笔端点。
  // 只取「上一级别最后一笔」对应段的中枢（无上级笔时退化为本级别全量 buildZS）。
  let zss = [];
  try {
    if (upperBis && upperBis.length > 0) {
      zss = buildZSByUpper(bis, upperBis, barSec);
    } else {
      zss = buildZS(bis, barSec);
    }
  } catch (e) {
    if (DEBUG) console.log(`[计划] ${res} buildZS 失败: ${e.message}`);
  }
  const upperLast = upperBis && upperBis.length > 0 ? upperBis[upperBis.length - 1] : null;
  const zsList = upperLast
    ? zss.filter(z => z.upperStart != null && z.upperStart >= upperLast.startTime - barSec)
    : zss;
  const lastZS = zsList[zsList.length - 1];
  if (lastZS && lastZS.exitTime == null && lastPrice != null && lastPrice >= lastZS.zd && lastPrice <= lastZS.zg) {
    const reason = `存在未离开中枢 [${lastZS.zd.toFixed(2)}, ${lastZS.zg.toFixed(2)}]（归属上一级别同一笔内），当前价 ${lastPrice.toFixed(2)} 位于中枢内，判定为震荡整理`;
    return { res, direction: "观望", strategy: "震荡整理（中枢内），观望等待方向选择", reason, label: "震荡观望" };
  }

  // 2. 趋势 → 获取本周期买卖点
  let buyPts = [], sellPts = [];
  try {
    if (bis.length >= 3) {
      buyPts = findBuyPoints(bis, upperBis || [], macdArr || [], barSec);
      sellPts = findSellPoints(bis, upperBis || [], macdArr || [], barSec);
    }
  } catch (e) {
    if (DEBUG) console.log(`[计划] ${res} findBuy/SellPoints 失败: ${e.message}`);
  }
  if (DEBUG) {
    const allDbg = [...buyPts, ...sellPts];
    console.log(`[计划] ${res} 买卖点(${allDbg.length}): ` + allDbg.map(p => `${p.type}@${fmtT(p.time)}(${p.price.toFixed(2)})`).join(", "));
  }

  // 匹配某笔终点上的买卖点（时间容差 = 本周期 1 个 bar，价格容差 = max(ATR×0.2, 0.05)）
  const tolSec = barSec;
  const tolPrice = Math.max(atr * 0.2, 0.05);
  const matchAt = (biIdx) => {
    const bi = bis[biIdx];
    if (!bi) return null;
    const all = [...buyPts, ...sellPts];
    const atEnd = all.find(p =>
      Math.abs(p.time - bi.endTime) <= tolSec && Math.abs(p.price - bi.endPrice) <= tolPrice
    );
    return atEnd ? { point: atEnd, bi } : null;
  };

  // 3. 先取最后一笔终点的买卖点
  const lastMatch = matchAt(bis.length - 1);
  if (DEBUG) console.log(`[计划] ${res} 最后一笔终点 ${fmtT(bis[bis.length - 1].endTime)}(${bis[bis.length - 1].endPrice.toFixed(2)}) -> ${lastMatch ? lastMatch.point.type : "无"}`);
  if (lastMatch) {
    const p = lastMatch.point;
    const reason = `找到最近买卖点 ${p.type} @ ${fmtT(p.time)} ${p.price.toFixed(2)}（最后一笔端点）`;
    const cls = /^(2买|类2买|3买|2卖|类2卖|3卖)$/.test(p.type) ? classifySecond(bis, macdArr, p) : "其他";
    const out = strategyOf(res, p.type, reason, `趋势|${p.type}`, cls);
    out.strategyLabel = out.strategy;
    out.pointDesc = `${p.type}@${fmtT(p.time)}(${p.price.toFixed(2)})`;
    return out;
  }

  // 4. 最后一笔终点无买卖点（空）→ 再向前获取一笔（逐笔向前扫描最近的笔端点买卖点）
  //    最近买卖点同样按类型精确映射（1卖→等待反弹后做2卖 等），不降级为只判断买卖方向。
  let prevMatch = null;
  for (let j = bis.length - 2; j >= 0; j--) {
    prevMatch = matchAt(j);
    if (prevMatch) break;
  }
  if (DEBUG) console.log(`[计划] ${res} 向前扫描最近买卖点 -> ${prevMatch ? prevMatch.point.type + "@" + fmtT(prevMatch.point.time) : "无"}`);
  if (prevMatch) {
    const p = prevMatch.point;
    const reason = `找到最近买卖点 ${p.type} @ ${fmtT(p.time)} ${p.price.toFixed(2)}（向前扫描最近笔端点）`;
    const cls = /^(2买|类2买|3买|2卖|类2卖|3卖)$/.test(p.type) ? classifySecond(bis, macdArr, p) : "其他";
    const out = strategyOf(res, p.type, reason, `趋势|${p.type}`, cls);
    out.strategyLabel = out.strategy;
    out.pointDesc = `${p.type}@${fmtT(p.time)}(${p.price.toFixed(2)})`;
    return out;
  }

  // 5. 趋势但未匹配到买卖点
  const reason = `趋势（非震荡），但最近笔端点均无已确认买卖点`;
  return { res, direction: "观望", strategy: "趋势中无匹配买卖点", reason, label: "观察" };
}

// ============================================================
// 纯函数：周期计划表格输出
// ============================================================

/** 终端显示宽度：中文/全角字符按 2 计算（对齐用） */
function dispWidth(s) {
  let w = 0;
  for (const ch of String(s)) {
    const code = ch.codePointAt(0);
    const wide = (code >= 0x1100 && code <= 0x115f)
      || code === 0x2329 || code === 0x232a
      || (code >= 0x2e80 && code <= 0xa4cf)
      || (code >= 0xac00 && code <= 0xd7a3)
      || (code >= 0xf900 && code <= 0xfaff)
      || (code >= 0xfe10 && code <= 0xfe19)
      || (code >= 0xfe30 && code <= 0xfe6f)
      || (code >= 0xff00 && code <= 0xff60)
      || (code >= 0xffe0 && code <= 0xffe6);
    w += wide ? 2 : 1;
  }
  return w;
}

/** 按显示宽度补齐空格（中文对齐） */
function padCell(s, width) {
  const str = String(s);
  const gap = width - dispWidth(str);
  return str + (gap > 0 ? " ".repeat(gap) : "");
}

/**
 * 打印各周期交易计划表。
 * 列：品种 | 周期 | 方向 | 策略 | 理由。列宽按内容自适应（中文按全角对齐）。
 * @param {Array} rows [{symbol, res, direction, strategy, reason}, ...]
 */
function printPlanTable(rows) {
  const headers = ["品种", "周期", "方向", "策略", "理由"];
  const cols = ["symbol", "res", "direction", "strategy", "reason"];
  const widths = headers.map((h, i) =>
    Math.max(dispWidth(h), ...rows.map(r => dispWidth(r[cols[i]] || "")))
  );
  const line = (l, m, r) => l + widths.map(w => "─".repeat(w + 2)).join(m) + r;
  const fmtRow = (cells) => "│ " + cells.map((c, i) => padCell(c, widths[i])).join(" │ ") + " │";
  console.log(line("┌", "┬", "┐"));
  console.log(fmtRow(headers));
  console.log(line("├", "┼", "┤"));
  for (const row of rows) {
    console.log(fmtRow(cols.map(k => row[k] || "")));
  }
  console.log(line("└", "┴", "┘"));
}

module.exports = {
  PLAN_COLOR,
  isRangeBound,
  strategyOf,
  classifySecond,
  predictPlan,
  dispWidth,
  padCell,
  printPlanTable,
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
    console.log("将计算周期:", PERIODS.join(", "));

    // 强制依赖画笔数据（与其它 SKILL 一致）
    const bisCachePath = cacheFile("bis", SYMBOL);
    let bisCache = null;
    try {
      if (fs.existsSync(bisCachePath)) bisCache = JSON.parse(fs.readFileSync(bisCachePath, "utf8"));
    } catch (e) {
      console.log("错误: 笔数据文件解析失败:", e.message);
    }
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

    // 只在本周期显示（与其它 SKILL 一致）
    const onlyThisInterval = (res) => {
      const s = String(res).toUpperCase();
      const NONE = {
        ticks: false, seconds: false, secondsFrom: 1, secondsTo: 59,
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
    };

    // 清除某周期的旧策略标记（title = CHAN_PLAN_<周期>）
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

    // 在某周期空白处绘制蓝色策略文字。
    // 位置：最近 40 根K线的最高点 + max(1.5×ATR, 0.5)（顶部空白），时间 = 最新bar右侧 2 根bar。
    const drawPlan = async (res, text, atr, lastBarTime, intervalSec) => {
      const TITLE = "CHAN_PLAN_" + res;
      const IV_CFG = onlyThisInterval(res);
      const r = await client.Runtime.evaluate({
        expression: `(async function() {
          const chart = TradingViewApi.activeChart();
          const TEXT = ${JSON.stringify(text)};
          const TITLE = "${TITLE}";
          const IV_CFG = ${JSON.stringify(IV_CFG)};
          const ATR = ${JSON.stringify(Math.max(atr || 0, 0))};
          const LAST_T = ${JSON.stringify(lastBarTime || 0)};
          const IV_SEC = ${JSON.stringify(intervalSec || 0)};
          const out = { ok: 0, err: [] };
          const ms = chart.chartModel().mainSeries();
          const items = ms.data().m_bars._items;
          const from = Math.max(0, items.length - 40);
          let maxH = -Infinity;
          for (let i = from; i < items.length; i++) {
            const v = items[i].value;
            if (v[2] > maxH) maxH = v[2];
          }
          if (maxH === -Infinity) maxH = 0;
          const PRICE = maxH + Math.max(ATR * 1.5, 0.5);
          const TIME = LAST_T + IV_SEC * 2;
          const id = await chart.createMultipointShape(
            [{ time: TIME, price: PRICE }],
            { shape: 'text', lock: false, overrides: {
                text: TEXT, color: '${PLAN_COLOR}', bold: true,
                title: TITLE
              } }
          );
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
          } catch(e) { out.err.push('IV:' + e.message); }
          out.ok = 1;
          out.price = PRICE;
          out.time = TIME;
          return out;
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };

    // 标记前先清除所有周期的旧策略标记
    let clearedAll = 0;
    if (!DRY) {
      let currentRes = originalRes;
      for (const res of PERIODS) {
        if (res !== currentRes) { await ensureResolution(res); currentRes = res; }
        const cl = await clearPlan(res);
        clearedAll += cl.cleared;
      }
      console.log("\n标记前清除旧策略标记:", clearedAll, "个");
    }

    // 逐周期：从大到小计算交易计划并收集报告行（表格形式统一输出）
    let currentRes = originalRes;
    let upperBis = null;
    const reportRows = []; // [{symbol, res, direction, strategy}]
    const planRows = {}; // {res: {direction, strategy, reason, pointDesc}}，落盘供 mark-entry 读取
    for (let pi = 0; pi < PERIODS.length; pi++) {
      const res = PERIODS[pi];
      if (res !== currentRes) { await ensureResolution(res); currentRes = res; }

      let d = await fetchBars(intervalSecOf(res));
      if (!d || d.error || !d.bars || d.bars.length === 0) {
        await sleep(3000);
        await ensureResolution(res);
        currentRes = res;
        d = await fetchBars(intervalSecOf(res));
      }
      if (!d || d.error || !d.bars || d.bars.length === 0) {
        console.log(`\n[周期 ${res}] 无K线数据，跳过`);
        continue;
      }

      const rawBars = d.bars;
      const atr = calcATR(rawBars, 14);
      const macdArr = calcMACD(rawBars);
      const lastBar = rawBars[rawBars.length - 1];
      const lastPrice = lastBar ? lastBar.close : null;
      const lastBarTime = lastBar ? lastBar.time : null;

      let curBis = bisCache.periods[res] || [];
      if (curBis.length === 0) {
        console.log(`\n[周期 ${res}] 笔数据为空（画笔未覆盖该周期），跳过`);
        continue;
      }
      // 取最近 60 笔即可（计划只看最新结构）
      if (curBis.length > 60) curBis = curBis.slice(-60);

      // 计算交易计划
      const p = predictPlan({
        res, bis: curBis, upperBis, macdArr, atr, lastPrice, bars: rawBars,
      });

      // 汇总报告行
      reportRows.push({ symbol: SYMBOL, res, direction: p.direction, strategy: p.strategy, reason: p.reason || "" });
      // 计划结果落盘收集（供 mark-entry 进出场读取：方向/策略/最近买卖点描述）
      planRows[res] = {
        direction: p.direction,
        strategy: p.strategy,
        reason: p.reason || "",
        pointDesc: p.pointDesc || "",
      };

      // 绘图：蓝色文字标记该周期策略（放可见范围顶部空白，最新bar右侧）
      // 内容：方向 + 策略，若找到最近买卖点则追加一行「最近买卖点:类型@时间(价格)」
      if (!DRY) {
        const markText = `方向:${p.direction} | 策略:${p.strategy}${p.pointDesc ? `\n最近买卖点:${p.pointDesc}` : ""}`;
        await drawPlan(res, markText, atr, lastBarTime, intervalSecOf(res));
      }

      // 记录本周期笔，供下一级周期做上级判断
      upperBis = curBis;
    }

    // 表格形式输出全部周期交易计划
    console.log("\n=== 各周期交易计划 ===");
    printPlanTable(reportRows);

    // 计划结果落盘（供 mark-entry 进出场 SKILL 读取，判定各周期进场状态）
    // 含 --dry 也落盘（与 chan-bi/mark-sr-flip 落盘行为一致）
    try {
      fs.mkdirSync(CACHE_DIR, { recursive: true });
      const planFile = cacheFile("plan", SYMBOL);
      const planPayload = {
        symbol: SYMBOL,
        from: null,
        generatedAt: new Date().toISOString(),
        periods: planRows,
      };
      fs.writeFileSync(planFile, JSON.stringify(planPayload, null, 2), "utf8");
      console.log(`\n交易计划已落盘: ${planFile}（${Object.keys(planRows).length} 个周期）`);
    } catch (e) {
      console.log("警告: 交易计划落盘失败:", e.message);
    }

    // 切回原周期
    if (originalRes !== currentRes) {
      await ensureResolution(originalRes);
      console.log("\n已切回原周期:", originalRes);
    }

    if (DRY) console.log("\n[DRY RUN] 不绘图。");
    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
  }
}
