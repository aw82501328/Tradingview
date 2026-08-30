/**
 * 当前周期状态描述 + 当下预判脚本（独立 SKILL：chan-status）
 * 描述各周期（日线/4小时/1小时/15分钟/3分钟）当前处于什么结构位置，
 * 并对「当下正在进行的K线」做买卖点预判（1/2/3 类买卖点只要符合特征都预判）。
 *
 * 输出：
 *   1. 文本报告：逐周期输出「状态 + 原因」（+ 预判点位）；
 *   2. 图上用蓝色文字在对应周期的空白处（最新bar右侧上方）标记该周期的状态。
 *
 * 用法：
 *   node chan_status.js           计算并输出报告 + 图上蓝色状态标记
 *   node chan_status.js --dry     只输出文本报告，不绘图
 *
 * 参数：
 *   --periods=...       周期列表（逗号分隔，默认 D,240,60,15,3）
 *   --dry               只输出文本不绘图
 *   --debug             打印调试信息
 */
const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");
// 缠论算法核心（唯一算法源，与 chan-bi / mark-buy-sell / chan-zs 共用）
const core = require("../../chan-core/scripts/chan_core.js");
const {
  calcATR, calcMACD, isBiDiverge,
  findBuyPoints, findSellPoints, fmtT, intervalSecOf,
} = core;

// 缓存目录：chan-bi 画笔落盘笔数据（强制依赖）、mark-sr-flip 落盘支阻位（可选）
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

// 图上状态标记的颜色（蓝色）
const STATUS_COLOR = "#2962FF";

// ============================================================
// 纯函数：状态分类 + 预判规则（可单测）
// ============================================================

/**
 * 向前找最近一笔「有效同向笔」（幅度 >= 当前笔 50%），用作背驰参照 / 创新高(低)对比。
 */
function findRefer(bis, curIdx) {
  const cur = bis[curIdx];
  for (let j = curIdx - 1; j >= 0; j--) {
    const c = bis[j];
    if (c.type !== cur.type) continue;
    if (c.span < cur.span * 0.5) continue;
    return c;
  }
  return null;
}

/** 找指定上涨笔之前最近的一笔下跌笔（1买/前低） */
function findBottomBefore(bis, upIdx) {
  for (let j = upIdx - 1; j >= 0; j--) {
    if (bis[j].type === "down") return bis[j];
  }
  return null;
}

/** 找指定下跌笔之前最近的一笔上涨笔（1卖/前顶） */
function findTopBefore(bis, downIdx) {
  for (let j = downIdx - 1; j >= 0; j--) {
    if (bis[j].type === "up") return bis[j];
  }
  return null;
}

/** 找上涨笔之前最近的上涨笔顶（前期结构高点） */
function findPrevUpTop(bis, upIdx) {
  for (let j = upIdx - 1; j >= 0; j--) {
    if (bis[j].type === "up") return bis[j];
  }
  return null;
}

/** 找下跌笔之前最近的下跌笔底（前期结构低点） */
function findPrevDnLow(bis, downIdx) {
  for (let j = downIdx - 1; j >= 0; j--) {
    if (bis[j].type === "down") return bis[j];
  }
  return null;
}

/**
 * 1买/前低 标签：该下跌笔是「1买」（MACD 底背驰 或 无更早参照视为结构底）
 * 否则标「前低」。
 */
function labelBottom(bottom, bis, macdArr) {
  if (!bottom) return "前低";
  const idx = bis.findIndex(b => b === bottom);
  const ref = idx >= 0 ? findRefer(bis, idx) : null;
  if (ref && !isBiDiverge(bottom, ref, macdArr)) return "前低";
  return "1买";
}

/** 1卖/前顶 标签：对称 */
function labelTop(top, bis, macdArr) {
  if (!top) return "前顶";
  const idx = bis.findIndex(b => b === top);
  const ref = idx >= 0 ? findRefer(bis, idx) : null;
  if (ref && !isBiDiverge(top, ref, macdArr)) return "前顶";
  return "1卖";
}

/**
 * 预判3卖：最近存在 2卖，且 2卖 之后出现「创新低跌破前底」的下跌笔，
 * 当前正处于「反弹笔」（最后一笔为上涨）且反弹价不破前底 → 3卖形成中。
 * 前底 = 2卖 之前最近一笔下跌笔的结束点。
 * 区间套：反弹须位于上一级别下跌笔段内（简化：不强制检查上级段，仅检查上级方向）。
 */
function checkThirdSell(bis, last2Sell) {
  const twoIdx = bis.findIndex(b => b.endTime === last2Sell.time);
  if (twoIdx < 0) return null;
  let prevLow = null;
  for (let j = twoIdx - 1; j >= 0; j--) {
    if (bis[j].type === "down") { prevLow = bis[j].endPrice; break; }
  }
  if (prevLow === null) return null;
  let brokeLow = false;
  for (let i = twoIdx + 1; i < bis.length; i++) {
    if (bis[i].type === "down" && bis[i].endPrice < prevLow) { brokeLow = true; break; }
  }
  if (!brokeLow) return null;
  const last = bis[bis.length - 1];
  if (!last || last.type !== "up") return null;
  if (last.endPrice >= prevLow) return null;
  return {
    type: "预判3卖",
    time: last.endTime,
    price: last.endPrice,
    note: `跌破前底 ${fmtT(last2Sell.time)} 前的 ${prevLow.toFixed(2)} 后反弹不破前底，3卖形成中`,
  };
}

/** 预判3买：对称（用 2买 与 前顶） */
function checkThirdBuy(bis, last2Buy) {
  const twoIdx = bis.findIndex(b => b.endTime === last2Buy.time);
  if (twoIdx < 0) return null;
  let prevTop = null;
  for (let j = twoIdx - 1; j >= 0; j--) {
    if (bis[j].type === "up") { prevTop = bis[j].endPrice; break; }
  }
  if (prevTop === null) return null;
  let brokeHigh = false;
  for (let i = twoIdx + 1; i < bis.length; i++) {
    if (bis[i].type === "up" && bis[i].endPrice > prevTop) { brokeHigh = true; break; }
  }
  if (!brokeHigh) return null;
  const last = bis[bis.length - 1];
  if (!last || last.type !== "down") return null;
  if (last.endPrice <= prevTop) return null;
  return {
    type: "预判3买",
    time: last.endTime,
    price: last.endPrice,
    note: `突破前顶 ${fmtT(last2Buy.time)} 前的 ${prevTop.toFixed(2)} 后回调不破前顶，3买形成中`,
  };
}

/**
 * 上级最近结构顶：从上级最后一笔起，向前找最近一笔上涨笔的终点。
 * 无论上级最后一笔是涨是跌，都返回「上级下跌段起点」的顶（时间 + 价格）。
 * @param {Array|null} upperBis 上级笔列表（最高级别为 null）
 * @returns {{time:number, price:number}|null}
 */
function findUpperPeak(upperBis) {
  if (!upperBis || upperBis.length === 0) return null;
  for (let i = upperBis.length - 1; i >= 0; i--) {
    if (upperBis[i].type === "up") return { time: upperBis[i].endTime, price: upperBis[i].endPrice };
  }
  return null;
}

/** 上级最近结构底：对称于 findUpperPeak */
function findUpperTrough(upperBis) {
  if (!upperBis || upperBis.length === 0) return null;
  for (let i = upperBis.length - 1; i >= 0; i--) {
    if (upperBis[i].type === "down") return { time: upperBis[i].endTime, price: upperBis[i].endPrice };
  }
  return null;
}

/**
 * 震荡（横盘整理）判定：K线重叠度高、价格变化不大、无明确方向。
 * 三条件同时满足才判定为震荡：
 *   1. K线区间小：最近 rangeBarN 根K线的 maxHigh - minLow <= rangeKMult × ATR
 *   2. 笔端点区间小：最近 rangeBiN 笔的端点极差（max-min）<= rangeBiMult × ATR
 *   3. 方向性弱：最近 rangeBiN 笔中涨跌交替（同时存在 up 与 down，且无明显单边）
 * @param {Array} bis     本周期笔列表
 * @param {Array} bars    本周期原始K线（含 high/low，可为空——为空则跳过判定）
 * @param {number} atr    本周期 ATR
 * @param {object} cfg    可选阈值覆盖（用于测试）
 * @returns {{range:boolean, kSpan:number, biSpan:number, kAtr:number, biAtr:number, alt:boolean, winBiCount:number}|null} 判定结果；bars 缺失时返回 null（跳过）
 */
function isRangeBound(bis, bars, atr, cfg) {
  if (!bars || bars.length === 0 || !bis || bis.length < 3 || !atr || atr <= 0) return null;
  const rangeBarN = (cfg && cfg.rangeBarN) || 40;
  const rangeBiN = (cfg && cfg.rangeBiN) || 4;
  const rangeKMult = (cfg && cfg.rangeKMult) || 5.0;
  const rangeBiMult = (cfg && cfg.rangeBiMult) || 7.0;

  // 条件1：最近 rangeBarN 根K线区间
  const win = bars.slice(-rangeBarN);
  const maxH = Math.max(...win.map(b => b.high));
  const minL = Math.min(...win.map(b => b.low));
  const kSpan = maxH - minL;
  const kAtr = kSpan / atr;

  // 条件2/条件3 的前提：笔须落在「最近 rangeBarN 根K线」的时间范围内。
  // 只取端点时间在窗口内的笔，避免用窗口外（很久以前）的笔判断当前是否震荡。
  const winStart = win[0].time;
  const winEnd = win[win.length - 1].time;
  const inWin = bis.filter(b =>
    (b.startTime >= winStart && b.startTime <= winEnd) ||
    (b.endTime >= winStart && b.endTime <= winEnd)
  );
  // 窗口内没有笔 → 跳过条件2/条件3（笔端点区间、涨跌交替无从判断，不因笔相关条件否定震荡）
  let biAtr = 0, biSpan = 0, hasBoth = true, alt = true;
  if (inWin.length > 0) {
    const recentBis = inWin.slice(-rangeBiN);
    const endpoints = [];
    for (const b of recentBis) endpoints.push(b.startPrice, b.endPrice);
    biSpan = Math.max(...endpoints) - Math.min(...endpoints);
    biAtr = biSpan / atr;

    // 条件3：涨跌交替（同时有 up 和 down，且不出现连续 3 笔同向）
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

/**
 * 核心：对单个周期生成「状态 + 原因 + 预判」。
 *
 * @param {object} opts
 *   res        本周期名（如 "240"）
 *   upperRes   上一级别周期名（如 "D"），日线为 null
 *   bis        本周期笔列表（已延伸的最终笔，与图上所画笔一致）
 *   upperBis   上一级别笔列表（可为空）
 *   macdArr    MACD 数组（供背驰判定，可为空）
 *   atr        本周期 ATR（用于原因中的幅度描述，可为空）
 *   lastPrice  当前最新价（判断「回调/反弹中」）
 *   lastBarTime 最新K线时间
 * @returns {object} { res, upperRes, status, reason, pred, preds, label }
 */
function predictPeriod(opts) {
  const { res, upperRes, bis, upperBis, macdArr, lastPrice, lastBarTime, bars } = opts;
  const atr = opts.atr || 0;
  const empty = { res, upperRes, status: "数据不足", reason: "笔数量不足，无法判断状态", pred: null, preds: [], label: "数据不足" };
  if (!bis || bis.length < 2) return empty;

  const last = bis[bis.length - 1];
  const lastIdx = bis.length - 1;
  const upperLast = upperBis && upperBis.length > 0 ? upperBis[upperBis.length - 1] : null;

  const mk = (status, reason, pred, label) => {
    const out = { res, upperRes, status, reason, pred: pred || null, preds: [], label: label || status };
    // 附加预判：2类附近可能形成 类2买/类2卖（若后续再创新次低/次高）
    if (pred && pred.type === "预判2买") {
      out.preds.push({ type: "预判类2买", note: "若后续回调再抬高（不破本次低点）则为类2买" });
    }
    if (pred && pred.type === "预判2卖") {
      out.preds.push({ type: "预判类2卖", note: "若后续反弹再走低（不破本次高点）则为类2卖" });
    }
    return out;
  };

  // 震荡优先：K线重叠度高、价格变化不大、无明确方向 → 覆盖一切买卖点预判
  const rb = isRangeBound(bis, bars, atr);
  if (rb && rb.range) {
    // 窗口内有笔 → 展示笔端点区间；窗口内无笔 → 跳过该条件（笔相关条件无法判断）
    const biPart = rb.winBiCount > 0
      ? `最近 ${rb.rangeBiN} 笔端点区间 ${rb.biSpan.toFixed(2)}（${rb.biAtr.toFixed(1)}×ATR），涨跌交替无明确方向`
      : `最近 ${rb.rangeBarN} 根K线内无笔（跳过笔端点区间/涨跌交替判断）`;
    const reason = `最近 ${rb.rangeBarN} 根K线区间 ${rb.kSpan.toFixed(2)}（${rb.kAtr.toFixed(1)}×ATR），${biPart}，判定为震荡整理`;
    return mk(`震荡整理`, reason, null, "震荡");
  }

  // 历史已确认买卖点（供 3买/3卖 预判 + 原因描述）
  let buyPts = [], sellPts = [];
  try {
    if (bis.length >= 3) {
      buyPts = findBuyPoints(bis, upperBis, macdArr || [], intervalSecOf(res) || 60);
      sellPts = findSellPoints(bis, upperBis, macdArr || [], intervalSecOf(res) || 60);
    }
  } catch (e) {
    if (DEBUG) console.log(`[预测] ${res} findBuy/SellPoints 失败: ${e.message}`);
  }
  const last2Buy = buyPts.filter(p => p.type === "2买").pop();
  const last2Sell = sellPts.filter(p => p.type === "2卖").pop();

  if (last.type === "up") {
    const upRefer = findRefer(bis, lastIdx);
    const newHigh = upRefer ? last.endPrice > upRefer.endPrice : false;
    const bottom = findBottomBefore(bis, lastIdx);
    const prevUp = findPrevUpTop(bis, lastIdx);

    // 【区间套优先】上级下跌段内部：上级最近结构顶已确认，本级别在其后只完成≤2笔
    // （下跌第一笔 + 首次反弹），当前反弹不创新高 → 2卖位。无论上级最后一笔是涨是跌，
    // 只要本级别已进入上级下跌段内部且仅完成2笔，就以2卖（而非3卖/1卖）解读。
    const upperPeak = findUpperPeak(upperBis);
    if (upperPeak && last.startTime >= upperPeak.time && last.endPrice < upperPeak.price && lastPrice < upperPeak.price) {
      const afterPeakCount = bis.filter(b => b.startTime >= upperPeak.time).length;
      if (afterPeakCount <= 2) {
        const prevDn = findBottomBefore(bis, lastIdx);
        const ctx = `上级(${upperRes})顶 ${fmtT(upperPeak.time)} ${upperPeak.price} 已确认${prevDn ? `，本级别先下跌至 ${fmtT(prevDn.endTime)} ${prevDn.endPrice}` : ""}，反弹至 ${fmtT(last.endTime)} ${last.endPrice} 不创新高（2卖点）`;
        if (lastPrice >= last.endPrice) {
          const reason = `${ctx}，当前价 ${lastPrice} 尚未回落，若反弹结束回落则2卖成立`;
          return mk(`上级下跌段内反弹不创新高，预判2卖形成中`, reason, { type: "预判2卖", time: last.endTime, price: last.endPrice, note: `反弹结束回落则2卖成立` }, "预判2卖");
        }
        const reason = `${ctx}，当前价 ${lastPrice} 已回落跌破反弹高点，2卖成立，下跌运行中`;
        return mk(`2卖运行中`, reason, null, "2卖运行中");
      }
    }

    // 【区间套优先】上级上涨段内部：上级底已确认且当前价已升破上级底，本级别上涨笔运行中。
    // 优先于 3卖/1卖：上级上涨段内部第一笔上涨中的反弹不构成卖点，等待回调做2买。
    // 限制：上级底之后本级别仅完成≤2笔（上涨第一笔+首次回调），已走出独立下跌结构则不适用。
    if (upperLast && upperLast.type === "down" && last.startTime >= upperLast.endTime && lastPrice > upperLast.endPrice) {
      const afterTroughCount = bis.filter(b => b.startTime >= upperLast.endTime).length;
      if (afterTroughCount <= 2) {
        const ctx = `上级(${upperRes})底 ${fmtT(upperLast.endTime)} ${upperLast.endPrice} 已确认`;
        const reason = `${ctx}，当前价 ${lastPrice} 已升破上级底，本级别处于上级上涨段内部（当前上涨笔 ${last.span.toFixed(1)} 点），等待回调做2买`;
        return mk(`上级上涨段内部，上涨运行中，等待回调做2买`, reason, null, "等待回调做2买");
      }
    }

    // ① 预判3卖：2卖 后创新低 + 当前反弹不破前底
    if (last2Sell) {
      const t3 = checkThirdSell(bis, last2Sell);
      if (t3) {
        const reason = `最近2卖(${fmtT(last2Sell.time)} ${last2Sell.price}) 之后出现创新低跌破前底的下跌笔，当前反弹至 ${fmtT(last.endTime)} ${last.endPrice} 未破前底，若反弹结束回落则3卖成立`;
        return mk(`预判3卖形成中`, reason, t3, "预判3卖");
      }
    }

    // ② 预判1卖：创新高 + 顶背驰
    if (newHigh && upRefer && isBiDiverge(last, upRefer, macdArr || [])) {
      const cur = core.biMacdMetrics(last, macdArr || []);
      const ref = core.biMacdMetrics(upRefer, macdArr || []);
      const reason = `${fmtT(last.endTime)} 上涨 ${last.span.toFixed(1)} 点创新高(${last.endPrice})，MACD 顶背驰（红柱面积 ${cur && ref ? cur.redArea.toFixed(2) + " < " + ref.redArea.toFixed(2) : "减弱"}），若回落确认则为1卖`;
      return mk(`上涨创新高出现顶背驰，预判1卖`, reason, { type: "预判1卖", time: last.endTime, price: last.endPrice, note: "若回落确认则为1卖" }, "预判1卖");
    }

    // ④ 回调进行中：当前价已跌破该笔终点，但尚未走出新的下跌笔。
    // 分两类（走势完美 + 区间套）：
    //   A. 上级顶未出现（或无上级）→ 处于本级别上涨段内部，回调结束企稳等待2买；
    //   B. 上级顶已确认 且 当前价已跌破上级顶 → 上级下跌段已展开，本级别处于上级
    //      下跌段内部（区间套）：当前处于下跌第一笔中，等待反弹做2卖（而非等待2买）。
    if (lastPrice < last.endPrice) {
      const upperPeak2 = findUpperPeak(upperBis);
      const afterUpperTop = upperPeak2 && last.startTime >= upperPeak2.time;
      const inUpperDown = upperPeak2 && lastPrice < upperPeak2.price;
      if (!afterUpperTop && !inUpperDown) {
        const bottomLabel = labelBottom(bottom, bis, macdArr || []);
        const strong = prevUp && last.endPrice > prevUp.endPrice;
        const reason = `${bottomLabel}(${bottom ? fmtT(bottom.endTime) + " " + bottom.endPrice : "前低"})后上涨 ${last.span.toFixed(1)} 点${strong ? `，强过前期结构高点(${fmtT(prevUp.endTime)} ${prevUp.endPrice})` : ""}，${fmtT(last.endTime)} 见顶 ${last.endPrice} 后当前价 ${lastPrice} 回调未成${res}下跌笔`;
        const pred = { type: "预判2买", time: lastBarTime || last.endTime, price: lastPrice, note: `回调不破 ${last.startPrice.toFixed(2)}（该笔起点）则2买成立` };
        return mk(`上涨见顶回调进行中，等待2买`, reason, pred, "等待2买");
      }
      // 上级顶已确认且价格已跌破 → 上级下跌段内部（区间套）
      const ctx = `上级(${upperRes})顶 ${fmtT(upperPeak2.time)} ${upperPeak2.price} 已确认`;
      if (!afterUpperTop) {
        // 本级别最后上涨笔终点即上级顶，正处于上级下跌段的第一笔下跌中（如 1小时 8-28 22:00 之后）
        const reason = `${ctx}，本级别最后上涨笔终点(${last.endPrice})即为上级顶，当前价 ${lastPrice} 已跌破，正处于上级下跌段的第一笔下跌中，等待反弹做2卖`;
        return mk(`上级下跌段内下跌第一笔进行中，等待反弹做2卖`, reason, null, "等待反弹做2卖");
      }
      const reason = `${ctx}，本级别已走出上级顶后的下跌，处于上级下跌段内部，等待反弹做2卖`;
      return mk(`上级下跌段内部，等待反弹做2卖`, reason, null, "等待反弹做2卖");
    }

    // ⑤ 上涨延续（创新高未背驰 / 一般延续）
    const reason = newHigh
      ? `${fmtT(last.endTime)} 上涨 ${last.span.toFixed(1)} 点创新高(${last.endPrice})，MACD 未现顶背驰，上涨动能未减弱，关注后续背驰（1卖）或回调（2买）`
      : `${fmtT(last.endTime)} 上涨至 ${last.endPrice}，未创新高${upperLast ? `（上级(${upperRes})${upperLast.type === "up" ? "顶" : "底"} ${fmtT(upperLast.endTime)} ${upperLast.endPrice}）` : ""}，上涨延续中`;
    return mk(`上涨延续中（${newHigh ? "创新高未背驰" : "未创新高"}）`, reason, null, "上涨延续");
  }

  // ===== last.type === "down" 对称 =====
  const dnRefer = findRefer(bis, lastIdx);
  const newLow = dnRefer ? last.endPrice < dnRefer.endPrice : false;
  const top = findTopBefore(bis, lastIdx);
  const prevDn = findPrevDnLow(bis, lastIdx);

  // 【区间套优先】上级上涨段内部：上级最近结构底已确认，本级别在其后只完成≤2笔
  // （上涨第一笔 + 首次回调），当前回调不创新低 → 2买位。
  const upperTrough = findUpperTrough(upperBis);
  if (upperTrough && last.startTime >= upperTrough.time && last.endPrice > upperTrough.price && lastPrice > upperTrough.price) {
    const afterTroughCount = bis.filter(b => b.startTime >= upperTrough.time).length;
    if (afterTroughCount <= 2) {
      const prevUp = findTopBefore(bis, lastIdx);
      const ctx = `上级(${upperRes})底 ${fmtT(upperTrough.time)} ${upperTrough.price} 已确认${prevUp ? `，本级别先反弹至 ${fmtT(prevUp.endTime)} ${prevUp.endPrice}` : ""}，回调至 ${fmtT(last.endTime)} ${last.endPrice} 不创新低（2买点）`;
      if (lastPrice <= last.endPrice) {
        const reason = `${ctx}，当前价 ${lastPrice} 尚未反弹，若回调结束企稳则2买成立`;
        return mk(`上级上涨段内回调不创新低，预判2买形成中`, reason, { type: "预判2买", time: last.endTime, price: last.endPrice, note: `回调结束企稳则2买成立` }, "预判2买");
      }
      const reason = `${ctx}，当前价 ${lastPrice} 已反弹升破回调低点，2买成立，上涨运行中`;
      return mk(`2买运行中`, reason, null, "2买运行中");
    }
  }

  // 【区间套优先】上级下跌段内部：上级顶已确认且当前价已跌破上级顶，本级别下跌笔运行中。
  // 优先于 3买/1买：上级下跌段内部第一笔下跌中的低点不构成买点，等待反弹做2卖。
  // 限制：上级顶之后本级别仅完成≤2笔（下跌第一笔+首次反弹），已走出独立上涨结构则不适用。
  const upperPeakD = findUpperPeak(upperBis);
  const afterPeakDCount = upperPeakD ? bis.filter(b => b.startTime >= upperPeakD.time).length : 0;
  if (upperPeakD && last.startTime >= upperPeakD.time && lastPrice < upperPeakD.price && afterPeakDCount <= 2) {
    const ctx = `上级(${upperRes})顶 ${fmtT(upperPeakD.time)} ${upperPeakD.price} 已确认`;
    const reason = `${ctx}，当前价 ${lastPrice} 已跌破上级顶，本级别处于上级下跌段内部（当前下跌笔 ${last.span.toFixed(1)} 点），等待反弹做2卖`;
    return mk(`上级下跌段内部，下跌运行中，等待反弹做2卖`, reason, null, "等待反弹做2卖");
  }

  // ① 预判3买：2买 后创新高 + 当前回调不破前顶
  if (last2Buy) {
    const t3 = checkThirdBuy(bis, last2Buy);
    if (t3) {
      const reason = `最近2买(${fmtT(last2Buy.time)} ${last2Buy.price}) 之后出现创新高突破前顶的上涨笔，当前回调至 ${fmtT(last.endTime)} ${last.endPrice} 未破前顶，若回调结束企稳则3买成立`;
      return mk(`预判3买形成中`, reason, t3, "预判3买");
    }
  }

  // ② 预判1买：创新低 + 底背驰
  if (newLow && dnRefer && isBiDiverge(last, dnRefer, macdArr || [])) {
    const cur = core.biMacdMetrics(last, macdArr || []);
    const ref = core.biMacdMetrics(dnRefer, macdArr || []);
    const reason = `${fmtT(last.endTime)} 下跌 ${last.span.toFixed(1)} 点创新低(${last.endPrice})，MACD 底背驰（绿柱面积 ${cur && ref ? cur.greenArea.toFixed(2) + " < " + ref.greenArea.toFixed(2) : "减弱"}），若止跌企稳则为1买`;
    return mk(`下跌创新低出现底背驰，预判1买`, reason, { type: "预判1买", time: last.endTime, price: last.endPrice, note: "若止跌企稳则为1买" }, "预判1买");
  }

  // ④ 反弹进行中：当前价已升过该笔终点，但尚未走出新的上涨笔。
  // 分两类（走势完美 + 区间套）：
  //   A. 上级底未出现（或无上级）→ 处于本级别下跌段内部，反弹结束滞涨等待2卖；
  //   B. 上级底已确认 且 当前价已升破上级底 → 上级上涨段已展开，本级别处于上级
  //      上涨段内部（区间套）：当前处于上涨第一笔中，等待回调做2买（而非等待2卖）。
  if (lastPrice > last.endPrice) {
    const upperTrough2 = findUpperTrough(upperBis);
    const afterUpperLow = upperTrough2 && last.startTime >= upperTrough2.time;
    const inUpperUp = upperTrough2 && lastPrice > upperTrough2.price;
    if (!afterUpperLow && !inUpperUp) {
      const topLabel = labelTop(top, bis, macdArr || []);
      const weak = prevDn && last.endPrice < prevDn.endPrice;
      const reason = `${topLabel}(${top ? fmtT(top.endTime) + " " + top.endPrice : "前顶"})后下跌 ${last.span.toFixed(1)} 点${weak ? `，跌破前期结构低点(${fmtT(prevDn.endTime)} ${prevDn.endPrice})` : ""}，${fmtT(last.endTime)} 见底 ${last.endPrice} 后当前价 ${lastPrice} 反弹未成${res}上涨笔`;
      const pred = { type: "预判2卖", time: lastBarTime || last.endTime, price: lastPrice, note: `反弹不破 ${last.startPrice.toFixed(2)}（该笔起点）则2卖成立` };
      return mk(`下跌见底反弹进行中，等待2卖`, reason, pred, "等待2卖");
    }
    // 上级底已确认且价格已升破 → 上级上涨段内部（区间套）
    const ctx = `上级(${upperRes})底 ${fmtT(upperTrough2.time)} ${upperTrough2.price} 已确认`;
    if (!afterUpperLow) {
      // 本级别最后下跌笔终点即上级底，正处于上级上涨段的第一笔上涨中
      const reason = `${ctx}，本级别最后下跌笔终点(${last.endPrice})即为上级底，当前价 ${lastPrice} 已升破，正处于上级上涨段的第一笔上涨中，等待回调做2买`;
      return mk(`上级上涨段内上涨第一笔进行中，等待回调做2买`, reason, null, "等待回调做2买");
    }
    const reason = `${ctx}，本级别已走出上级底后的上涨，处于上级上涨段内部，等待回调做2买`;
    return mk(`上级上涨段内部，等待回调做2买`, reason, null, "等待回调做2买");
  }

  // ⑤ 下跌延续
  const reason = newLow
    ? `${fmtT(last.endTime)} 下跌 ${last.span.toFixed(1)} 点创新低(${last.endPrice})，MACD 未现底背驰，下跌动能未减弱，关注后续背驰（1买）或反弹（2卖）`
    : `${fmtT(last.endTime)} 下跌至 ${last.endPrice}，未创新低${upperLast ? `（上级(${upperRes})${upperLast.type === "down" ? "底" : "顶"} ${fmtT(upperLast.endTime)} ${upperLast.endPrice}）` : ""}，下跌延续中`;
  return mk(`下跌延续中（${newLow ? "创新低未背驰" : "未创新低"}）`, reason, null, "下跌延续");
}

// ============================================================
// 纯函数：周期状态汇总表格输出
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
 * 打印各周期状态汇总表。
 * 列：周期 | 状态 | 原因 | 预判。列宽按内容自适应（中文按全角对齐）。
 * @param {Array} rows [{res, status, reason, pred}, ...]
 */
function printPeriodTable(rows) {
  const headers = ["周期", "状态", "原因", "预判"];
  const cols = ["res", "status", "reason", "pred"];
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
  STATUS_COLOR,
  findRefer,
  findBottomBefore,
  findTopBefore,
  findPrevUpTop,
  findPrevDnLow,
  findUpperPeak,
  findUpperTrough,
  isRangeBound,
  labelBottom,
  labelTop,
  checkThirdSell,
  checkThirdBuy,
  predictPeriod,
  dispWidth,
  padCell,
  printPeriodTable,
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
    console.log("将描述周期:", PERIODS.join(", "));

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

    // 可选依赖：支阻位缓存（用于原因中的上下最近支阻）
    const srCachePath = cacheFile("srflip", SYMBOL);
    let srCache = null;
    try {
      if (fs.existsSync(srCachePath)) srCache = JSON.parse(fs.readFileSync(srCachePath, "utf8"));
    } catch (e) { /* 忽略，支阻位为可选依赖 */ }

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

    // 清除某周期的旧状态标记（title = CHAN_STATUS_<周期>）
    const clearStatus = async (res) => {
      const TITLE = "CHAN_STATUS_" + res;
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

    // 在某周期空白处绘制蓝色状态文字。
    // 位置：取「当前图表可见范围」内K线的最高点 + 2×ATR（顶部空白），时间 = 最新bar右侧 2 根bar。
    const drawStatus = async (res, text, atr, lastBarTime, intervalSec) => {
      const TITLE = "CHAN_STATUS_" + res;
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
          // 最近 40 根K线的最高点（定位到近期顶部空白，避免全历史高点）
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
                text: TEXT, color: '${STATUS_COLOR}', bold: true,
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

    // 标记前先清除所有周期的旧状态标记
    let clearedAll = 0;
    if (!DRY) {
      let currentRes = originalRes;
      for (const res of PERIODS) {
        if (res !== currentRes) { await ensureResolution(res); currentRes = res; }
        const cl = await clearStatus(res);
        clearedAll += cl.cleared;
      }
      console.log("\n标记前清除旧状态标记:", clearedAll, "个");
    }

    // 逐周期：从大到小计算状态并收集报告行（表格形式统一输出）
    let currentRes = originalRes;
    let upperBis = null;
    let upperRes = null;
    const reportRows = []; // [{res, status, reason, pred}]
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
      // 取最近 60 笔即可（状态只看最新结构）
      if (curBis.length > 60) curBis = curBis.slice(-60);

      // 计算预测
      const p = predictPeriod({
        res, upperRes, bis: curBis, upperBis, macdArr, atr, lastPrice, lastBarTime, bars: rawBars,
      });

      // 汇总报告行：预判（主预判 + 附加预判）合并为一个单元格
      let predText = "";
      if (p.pred) {
        predText = `${p.pred.type} @${fmtT(p.pred.time)}(${p.pred.price.toFixed(2)})`;
        if (p.pred.note) predText += "；" + p.pred.note;
      }
      for (const extra of p.preds) {
        predText += (predText ? "；" : "") + `${extra.type}；${extra.note}`;
      }
      reportRows.push({ res, status: p.status, reason: p.reason, pred: predText });

      // 绘图：蓝色文字标记该周期状态（放可见范围顶部空白，最新bar右侧）
      if (!DRY) {
        await drawStatus(res, p.label, atr, lastBarTime, intervalSecOf(res));
      }

      // 记录本周期笔，供下一级周期做上级判断
      upperBis = curBis;
      upperRes = res;
    }

    // 表格形式输出全部周期状态报告
    console.log("\n=== 当前周期状态汇总 ===");
    printPeriodTable(reportRows);

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
