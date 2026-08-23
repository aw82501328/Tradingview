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
 */
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");

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
const ATR_FILTER = getArg("atr", 0.5);
const GAP_FILTER = getArg("gap", 1.0);
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

const MIN_WINDOW_BARS = 20;
const ANCHOR_BUFFER = 30;

// ============================================================
// 缠论算法（与 chan-bi SKILL 保持一致）
// ============================================================

function mergeBars(rawBars) {
  const merged = [];
  let direction = 0;
  for (const bar of rawBars) {
    if (merged.length === 0) {
      merged.push({ ...bar, _rawCount: 1, highTime: bar.time, lowTime: bar.time });
      continue;
    }
    const last = merged[merged.length - 1];
    const containUp = bar.high >= last.high && bar.low <= last.low;
    const containDown = bar.high <= last.high && bar.low >= last.low;
    const hasContain = containUp || containDown;
    if (hasContain) {
      let dir = direction;
      if (dir === 0 && merged.length >= 2) {
        dir = last.high >= merged[merged.length - 2].high ? 1 : -1;
      }
      if (dir === 0) dir = 1;
      if (dir === 1) {
        if (bar.high > last.high) { last.high = bar.high; last.highTime = bar.time; }
        if (bar.low > last.low) { last.low = bar.low; last.lowTime = bar.time; }
      } else {
        if (bar.high < last.high) { last.high = bar.high; last.highTime = bar.time; }
        if (bar.low < last.low) { last.low = bar.low; last.lowTime = bar.time; }
      }
      last._rawCount += 1;
      last.time = bar.time;
      direction = dir;
    } else {
      direction = bar.high > last.high ? 1 : -1;
      merged.push({ ...bar, _rawCount: 1, highTime: bar.time, lowTime: bar.time });
    }
  }
  return merged;
}

function findFractals(merged) {
  const fractals = [];
  for (let i = 1; i < merged.length - 1; i++) {
    const prev = merged[i - 1], cur = merged[i], next = merged[i + 1];
    if (cur.high > prev.high && cur.high > next.high && cur.low > prev.low && cur.low > next.low) {
      fractals.push({ mergedIdx: i, type: "top", high: cur.high, low: cur.low, time: cur.highTime });
    }
    if (cur.low < prev.low && cur.low < next.low && cur.high < prev.high && cur.high < next.high) {
      fractals.push({ mergedIdx: i, type: "bottom", high: cur.high, low: cur.low, time: cur.lowTime });
    }
  }
  return fractals;
}

function countRaw(merged, startIdx, endIdx) {
  let t = 0;
  for (let k = startIdx + 1; k <= endIdx; k++) t += merged[k]._rawCount;
  return t;
}

function hasGapBetween(merged, aIdx, bIdx, atr, gapFilter) {
  const th = atr * gapFilter;
  for (let i = aIdx; i < bIdx; i++) {
    const cur = merged[i], next = merged[i + 1];
    const gapUp = next.low - cur.high;
    const gapDown = cur.low - next.high;
    if (gapUp >= th || gapDown >= th) return true;
  }
  return false;
}

function buildBi(fractals, merged, atr, macdArr) {
  const gapThreshold = atr ? atr * GAP_FILTER : 0;
  const seq = [];
  for (const f of fractals) {
    if (seq.length === 0) { seq.push(f); continue; }
    const last = seq[seq.length - 1];
    if (f.type === last.type) {
      if (f.type === "top") { if (f.high >= last.high) seq[seq.length - 1] = f; }
      else { if (f.low <= last.low) seq[seq.length - 1] = f; }
    } else {
      seq.push(f);
    }
  }
  const isValid = (a, b) => {
    const gap = b.mergedIdx - a.mergedIdx;
    if (gap < 4) return false;
    return countRaw(merged, a.mergedIdx, b.mergedIdx) >= 5;
  };
  const noMoreExtremeInside = (a, b) => {
    for (let i = a.mergedIdx + 1; i < b.mergedIdx; i++) {
      if (b.type === "bottom" && merged[i].low < b.low) return false;
      if (b.type === "top" && merged[i].high > b.high) return false;
    }
    return true;
  };
  // 分型范围脱离检查：一笔的两端分型不能互相"包含"。
  // 顶分型 → 底分型（下跌笔）：底分型的底必须**低于顶分型三根K线的最低点**
  //   （底不能在顶分型的范围内，即必须跌破顶分型形成处的价格范围才算真正转势）；
  // 底分型 → 顶分型（上涨笔）：顶分型的顶必须**高于底分型三根K线的最高点**。
  // 例：1小时 8-3 23:00 顶(84.54) 的三根K线低点为 8-3 22:00 的 83.11，
  //   而 8-4 04:00 底(83.36) 未跌破 83.11，该"下跌笔"不成立。
  const fractalRangeClear = (a, b) => {
    const i = a.mergedIdx;
    const rangeLow = Math.min(merged[i - 1].low, merged[i].low, merged[i + 1].low);
    const rangeHigh = Math.max(merged[i - 1].high, merged[i].high, merged[i + 1].high);
    if (a.type === "top" && b.type === "bottom") return b.low < rangeLow;
    if (a.type === "bottom" && b.type === "top") return b.high > rangeHigh;
    return true;
  };
  const result = [];
  for (const k of seq) {
    if (result.length === 0) { result.push(k); continue; }
    const last = result[result.length - 1];
    if (k.type === last.type) {
      if (!last.gapLocked) {
        if (k.type === "top") { if (k.high >= last.high) result[result.length - 1] = k; }
        else { if (k.low <= last.low) result[result.length - 1] = k; }
      } else {
        if (k.type === "top") { if (k.high > last.high) result[result.length - 1] = k; }
        else { if (k.low < last.low) result[result.length - 1] = k; }
      }
      continue;
    }
    if (result.length >= 2) {
      const prev2 = result[result.length - 2];
      if (prev2.macdCross === true && prev2.type === k.type &&
          ((k.type === "top" && k.high > prev2.high) || (k.type === "bottom" && k.low < prev2.low))) {
        k.macdCross = true;
        result[result.length - 2] = k;
        result.pop();
        continue;
      }
    }
    const hasGap = gapThreshold > 0 && hasGapBetween(merged, last.mergedIdx, k.mergedIdx, atr, GAP_FILTER);
    if (hasGap) {
      k.gapLocked = true;
      result.push(k);
      continue;
    }
    // 前顶/前底作废：prev2（result[-2]）与 k 同类型，且 prev2→last 不构成有效笔
    // （间隔不足，prev2 是脆弱端点），而 k 比 prev2 更极端（创新高/新低）时，
    // prev2 作为笔端点已被市场否定，应让 k 顶替 prev2 并移除中间的 last。
    // 例：3分钟 8-12 11:18 顶(89.84) 被 12:00 顶(90.07) 突破，上涨笔应画到 90.07@12:00。
    // 附加约束（防止误伤真实顶/底）：
    //   1) last 不得比 prev3 更极端（否则 last 是深回调的真实转折）；
    //   2) 回调/反弹必须浅（< 50%，深回调意味着 prev2 是真实顶/底）；
    //   3) last 必须是「弱分型」：MACD 变色成笔且原始K线 < 5 根
    //      （不足 5 根的 MACD 成笔点是微回调停顿，最容易被后续突破否定）。
    if (result.length >= 3) {
      const prev3 = result[result.length - 3];
      const prev2 = result[result.length - 2];
      const lastMoreExtremeThanPrev3 =
        (prev3.type === "top" && last.high > prev3.high) ||
        (prev3.type === "bottom" && last.low < prev3.low);
      let shallow = true;
      if (prev2.type === "top") {
        const rise = prev2.high - prev3.low;
        const pull = prev2.high - last.low;
        shallow = pull < rise * 0.5;
      } else {
        const drop = prev3.high - prev2.low;
        const bounce = last.high - prev2.low;
        shallow = bounce < drop * 0.5;
      }
      if (prev2.type === k.type &&
          !isValid(prev2, last) &&
          !lastMoreExtremeThanPrev3 &&
          shallow &&
          last.macdCross === true && last.macdRaw < 5 &&
          ((k.type === "top" && k.high > prev2.high) || (k.type === "bottom" && k.low < prev2.low))) {
        if (prev2.macdCross === true) k.macdCross = true;
        result[result.length - 2] = k;
        result.pop();
        continue;
      }
    }
    if (isValid(last, k) && (noMoreExtremeInside(last, k) || last.gapLocked) && (fractalRangeClear(last, k) || last.gapLocked)) {
      result.push(k);
    } else if (isValid(last, k)) {
      if (DEBUG) {
        const fr = fractalRangeClear(last, k);
        const ex = noMoreExtremeInside(last, k);
        console.log(`[阶段二] 忽略 k: ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 极值冲突=${!ex} 分型范围未脱离=${!fr}`);
      }
    } else {
      const macdCross = macdArr && hasMacdCrossBetween(macdArr, merged, last.mergedIdx, k.mergedIdx, last.time, k.time);
      const macdRawCount = countRaw(merged, last.mergedIdx, k.mergedIdx);
      if (macdCross && macdRawCount >= 4 && noMoreExtremeInside(last, k)) {
        k.macdCross = true;
        k.macdRaw = macdRawCount;
        result.push(k);
      } else {
        if (result.length >= 2 && result[result.length - 2].type === k.type) {
          const prev = result[result.length - 2];
          const moreExtreme = k.type === "top" ? k.high >= prev.high : k.low <= prev.low;
          const gapPrevLast = last.mergedIdx - prev.mergedIdx;
          const gapPrevK = k.mergedIdx - prev.mergedIdx;
          if (moreExtreme && (gapPrevLast <= 12 || gapPrevK >= 4)) {
            result[result.length - 2] = k;
            result.pop();
          }
        }
      }
    }
  }
  const bis = [];
  for (let i = 0; i + 1 < result.length; i++) {
    const a = result[i], b = result[i + 1];
    const startPrice = a.type === "top" ? a.high : a.low;
    const endPrice = b.type === "top" ? b.high : b.low;
    const isUp = b.type === "top";
    bis.push({
      type: isUp ? "up" : "down",
      startIdx: a.mergedIdx,
      endIdx: b.mergedIdx,
      startTime: a.time,
      endTime: b.time,
      startPrice,
      endPrice,
      rawCount: countRaw(merged, a.mergedIdx, b.mergedIdx),
      span: Math.abs(endPrice - startPrice),
      gapLocked: b.gapLocked === true,
      macdCross: b.macdCross === true,
    });
  }
  return bis;
}

function calcATR(rawBars, period = 14) {
  const trs = [];
  for (let i = 1; i < rawBars.length; i++) {
    const h = rawBars[i].high, l = rawBars[i].low, pc = rawBars[i - 1].close;
    trs.push(Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc)));
  }
  const start = Math.max(0, trs.length - period);
  const slice = trs.slice(start);
  if (slice.length === 0) return 0;
  return slice.reduce((a, b) => a + b, 0) / slice.length;
}

function calcMACD(rawBars) {
  if (!rawBars || rawBars.length < 2) return [];
  const closes = rawBars.map(b => b.close);
  const ema = (period) => {
    const k = 2 / (period + 1);
    const out = [];
    let prev = closes[0];
    out.push(prev);
    for (let i = 1; i < closes.length; i++) {
      prev = closes[i] * k + prev * (1 - k);
      out.push(prev);
    }
    return out;
  };
  const ema12 = ema(12);
  const ema26 = ema(26);
  const dif = closes.map((_, i) => ema12[i] - ema26[i]);
  const dea = [];
  let prevDea = dif[0];
  dea.push(prevDea);
  for (let i = 1; i < dif.length; i++) {
    prevDea = dif[i] * (2 / (9 + 1)) + prevDea * (1 - (2 / (9 + 1)));
    dea.push(prevDea);
  }
  return rawBars.map((b, i) => ({
    time: b.time,
    macd: (dif[i] - dea[i]) * 2,
    dif: dif[i],
    dea: dea[i],
  }));
}

// 模块级时间格式化（供买卖点识别 DEBUG 打印使用）
function fmtT(ts) {
  const dt = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${dt.getMonth()+1}-${dt.getDate()} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
}

// 计算一笔区间内的 MACD 动能指标（用于背驰判定）
// 返回：{
//   redArea:   红柱面积（区间内 macd>0 的柱累加）——上涨段多头动能
//   greenArea: 绿柱面积（区间内 macd<0 的柱绝对值累加）——下跌段空头动能
//   difHigh:   区间内 DIF（黄白线）最大值
//   difLow:    区间内 DIF（黄白线）最小值
// }
function biMacdMetrics(bi, macdArr) {
  const metrics = { redArea: 0, greenArea: 0, difHigh: -Infinity, difLow: Infinity };
  if (!macdArr || macdArr.length === 0) return null;
  const t0 = bi.startTime, t1 = bi.endTime;
  let found = false;
  for (const m of macdArr) {
    if (m.time < t0) continue;
    if (m.time > t1) break;
    found = true;
    if (m.macd > 0) metrics.redArea += m.macd;
    else metrics.greenArea += -m.macd;
    if (m.dif > metrics.difHigh) metrics.difHigh = m.dif;
    if (m.dif < metrics.difLow) metrics.difLow = m.dif;
  }
  if (!found) return null;
  return metrics;
}

// MACD 背驰判定（OR 关系，满足其一即算背驰）：
//   底背驰（对应一买，下跌笔）：绿柱面积变小 或 黄白线低点抬高（下跌动能减弱）
//   顶背驰（对应一卖，上涨笔）：红柱面积变小 或 黄白线高点变低（上涨动能减弱）
function isBiDiverge(bi, refer, macdArr) {
  const cur = biMacdMetrics(bi, macdArr);
  const ref = biMacdMetrics(refer, macdArr);
  if (!cur || !ref) return false;
  if (bi.type === "down") {
    // 底背驰：绿柱面积变小 或 黄白线低点抬高
    return cur.greenArea < ref.greenArea || cur.difLow > ref.difLow;
  }
  // 顶背驰：红柱面积变小 或 黄白线高点变低
  return cur.redArea < ref.redArea || cur.difHigh < ref.difHigh;
}

function hasMacdCrossBetween(macdArr, merged, aIdx, bIdx, aTime, bTime) {
  if (!macdArr || macdArr.length === 0) return false;
  const t0 = aTime !== undefined ? aTime : merged[aIdx].time;
  const t1 = bTime !== undefined ? bTime : merged[bIdx].time;
  let prev = null;
  for (const mm of macdArr) {
    if (mm.time < t0) continue;
    if (mm.time > t1) break;
    if (prev !== null) {
      const crossed = (prev.macd >= 0 && mm.macd < 0) || (prev.macd <= 0 && mm.macd > 0);
      if (crossed) return true;
    }
    prev = mm;
  }
  return false;
}

function extendLastBi(bisArr, bars) {
  if (!bisArr || bisArr.length === 0) return bisArr;
  const last = bisArr[bisArr.length - 1];
  if (last.gapLocked) return bisArr;
  const startIdx = bars.findIndex(k => k.time >= last.startTime);
  if (startIdx === -1) return bisArr;
  const tail = bars.slice(startIdx);
  if (tail.length < 2) return bisArr;
  if (last.type === "up") {
    let maxBar = tail[0];
    for (const k of tail) if (k.high > maxBar.high) maxBar = k;
    if (maxBar.time > last.endTime && maxBar.high > last.endPrice) {
      last.endTime = maxBar.time;
      last.endPrice = maxBar.high;
      last.span = maxBar.high - last.startPrice;
    }
  } else {
    let minBar = tail[0];
    for (const k of tail) if (k.low < minBar.low) minBar = k;
    if (minBar.time > last.endTime && minBar.low < last.endPrice) {
      last.endTime = minBar.time;
      last.endPrice = minBar.low;
      last.span = last.startPrice - minBar.low;
    }
  }
  return bisArr;
}

function lowerResOf(res) {
  const s = String(res).toUpperCase();
  if (s === "D" || s === "1D") return "240";
  if (s === "240" || s === "4H") return "60";
  if (s === "60" || s === "1H") return "15";
  if (s === "15") return "3";
  return null;
}

function calibrateBiTimes(bis, bigBars, refBars, bigIntervalSec) {
  if (!bis || bis.length === 0 || !refBars || refBars.length === 0) return bis;
  const eps = 0.001;
  const calibrateTime = (t, price) => {
    const big = bigBars.find(k => k.time <= t && t < k.time + bigIntervalSec);
    if (!big) return t;
    const rangeEnd = big.time + bigIntervalSec;
    let best = null;
    for (const rb of refBars) {
      if (rb.time < big.time || rb.time >= rangeEnd) continue;
      if (Math.abs(rb.high - price) < eps || Math.abs(rb.low - price) < eps) {
        best = rb;
      }
    }
    return best ? best.time : t;
  };
  for (const b of bis) {
    b.startTime = calibrateTime(b.startTime, b.startPrice);
    b.endTime = calibrateTime(b.endTime, b.endPrice);
  }
  return bis;
}

// ============================================================
// 买卖点识别（与 chan-bi SKILL 一致）
// ============================================================

function findBuyPoints(bis, upperBis, macdArr, barSec) {
  if (bis.length < 3) return [];
  const downIdx = [];
  bis.forEach((b, i) => { if (b.type === "down") downIdx.push(i); });
  // 候选一买：创新低 + MACD 背驰（绿柱面积变小 或 黄白线低点抬高，即下跌动能减弱）
  const firstBuys = [];
  for (let k = 1; k < downIdx.length; k++) {
    const cur = bis[downIdx[k]];
    // 本周期这笔与上一级别某笔完全重合（内部无结构，整笔即上级一笔），
    // 本周期无法判断背驰，1买 由上一级别标记，本周期不标记
    if (isSameAsUpperBi(cur, upperBis, barSec)) {
      if (DEBUG) console.log(`[一买跳过-与上级笔重合] ${fmtT(cur.endTime)}(${cur.endPrice}) 整笔与上一级别完全重合，本周期不标记`);
      continue;
    }
    let refer = null;
    for (let j = k - 1; j >= 0; j--) {
      const cand = bis[downIdx[j]];
      if (cand.span < cur.span * 0.5) continue;
      refer = cand;
      break;
    }
    if (refer && cur.endPrice < refer.endPrice) {
      const diverge = isBiDiverge(cur, refer, macdArr);
      if (DEBUG) {
        const cm = biMacdMetrics(cur, macdArr);
        const rm = biMacdMetrics(refer, macdArr);
        console.log(
          `[一买候选] ${fmtT(cur.endTime)}(${cur.endPrice}) vs 参照 ${fmtT(refer.endTime)}(${refer.endPrice}) ` +
          `| 创新低=${cur.endPrice < refer.endPrice} ` +
          `| 绿柱面积 ${cm ? cm.greenArea.toFixed(2) : "-"} vs ${rm ? rm.greenArea.toFixed(2) : "-"} (变小=${cm && rm ? cm.greenArea < rm.greenArea : false}) ` +
          `| DIF低点 ${cm ? cm.difLow.toFixed(3) : "-"} vs ${rm ? rm.difLow.toFixed(3) : "-"} (抬高=${cm && rm ? cm.difLow > rm.difLow : false}) ` +
          `| 背驰=${diverge}`
        );
      }
      if (diverge) {
        firstBuys.push({ biIdx: downIdx[k], time: cur.endTime, price: cur.endPrice });
      }
    }
  }
  const firstBuy = firstBuys.length > 0 ? firstBuys[firstBuys.length - 1] : null;

  const points = [];

  // 2买 / 类2买（区间套）：
  //   有上级笔时，只在「上一级别上涨笔」段内找抬高低点：
  //     段内第一个回调低点（高于段起点）= 2买；其后第一个更高低点 = 类2买。
  //   无上级笔（日线为最高级别）时，沿用结构底逻辑。
  if (upperBis && upperBis.length > 0) {
    for (const up of upperBis) {
      if (up.type !== "up") continue;
      const lows = [];
      for (let i = 0; i < bis.length; i++) {
        const b = bis[i];
        if (b.type !== "down") continue;
        if (b.endTime >= up.startTime && b.endTime <= up.endTime + 1) {
          lows.push({ biIdx: i, time: b.endTime, price: b.endPrice });
        }
      }
      if (lows.length === 0) continue;
      lows.sort((a, b2) => a.time - b2.time);
      const firstLow = lows.find(l => l.price > up.startPrice);
      if (firstLow) {
        points.push({ type: "2买", time: firstLow.time, price: firstLow.price });
        const laterHigh = lows.find(l => l.time > firstLow.time && l.price > firstLow.price);
        if (laterHigh) points.push({ type: "类2买", time: laterHigh.time, price: laterHigh.price });
      }
    }
  } else {
    // 结构底：最近一买之前（或全窗口）的最低底，作为上涨段的起点
    let structBottomIdx = null;
    if (firstBuy) {
      let minP = Infinity;
      for (const i of downIdx) {
        if (i >= firstBuy.biIdx) break;
        if (bis[i].endPrice < minP) { minP = bis[i].endPrice; structBottomIdx = i; }
      }
    }
    if (structBottomIdx === null) {
      let minP = Infinity;
      for (const i of downIdx) {
        if (bis[i].endPrice < minP) { minP = bis[i].endPrice; structBottomIdx = i; }
      }
    }
    if (structBottomIdx !== null) {
      const bottom = bis[structBottomIdx];
      let secondBuy = null;
      for (let i = structBottomIdx + 1; i < bis.length; i++) {
        if (bis[i].type !== "down") continue;
        if (bis[i].endPrice > bottom.endPrice) {
          secondBuy = { biIdx: i, time: bis[i].endTime, price: bis[i].endPrice };
          break;
        }
      }
      if (secondBuy) {
        points.push({ type: "2买", time: secondBuy.time, price: secondBuy.price });
        let classSecond = null;
        for (let i = secondBuy.biIdx + 1; i < bis.length; i++) {
          if (bis[i].type !== "down") continue;
          if (bis[i].endPrice > secondBuy.price) {
            classSecond = { time: bis[i].endTime, price: bis[i].endPrice };
            break;
          }
        }
        if (classSecond) points.push({ type: "类2买", time: classSecond.time, price: classSecond.price });
      }
    }
  }

  // 1买：所有 MACD 背驰底（全部保留）
  for (const fb of firstBuys) {
    points.push({ type: "1买", time: fb.time, price: fb.price });
  }

  // 3买：2买过后的上涨段未出现背驰（上涨笔创新高、突破前顶），其后的回调就是 3买。
  // 规则：
  //   1) 对每一笔 2买 检查其后的上涨段，每个 2买 段最多标一个 3买，
  //      且扫描范围限制到「下一个 2买」之前（避免跨段覆盖/重复）；
  //   2) 前顶 = 2买 之前最近一个上涨笔的结束点；
  //   3) 段内取「突破前顶的上涨笔 + 其后第一个回调不破前顶」中最后满足的一个；
  //   4) 区间套：回调结束点必须位于上一级别上涨笔段内（无上级笔时跳过此检查）。
  const twoBuys = points.filter(p => p.type === "2买").sort((a, b) => a.time - b.time);
  const thirdBuys = [];
  for (let k = 0; k < twoBuys.length; k++) {
    const tb = twoBuys[k];
    const twoIdx = bis.findIndex(b => b.endTime === tb.time);
    if (twoIdx < 0) continue;
    const endScan = k + 1 < twoBuys.length
      ? bis.findIndex(b => b.endTime === twoBuys[k + 1].time)
      : bis.length;
    // 前顶 = 2买 之前最近一个上涨笔的结束点
    let prevTop = null;
    for (let j = twoIdx - 1; j >= 0; j--) {
      if (bis[j].type === "up") { prevTop = bis[j].endPrice; break; }
    }
    if (prevTop === null) continue;
    let lastValid = null;
    for (let i = twoIdx + 1; i < endScan; i++) {
      if (bis[i].type !== "up") continue;
      if (bis[i].endPrice <= prevTop) continue; // 未突破前顶
      // 其后第一个回调笔：回调低点不破前顶 → 3买候选
      for (let mm = i + 1; mm < endScan; mm++) {
        if (bis[mm].type !== "down") continue;
        const bt = bis[mm].endTime, bp = bis[mm].endPrice;
        if (bp > prevTop) {
          // 区间套：回调结束点须位于上级上涨笔段内，且回调价须高于该上涨笔起点价
          //（排除回调恰好落在上级笔起点/新段启动点的情况，如 7-29 00:30、8-13 22:00）
          let inUp = true;
          if (upperBis && upperBis.length > 0) {
            inUp = false;
            for (const up of upperBis) {
              if (up.type === "up" && bt >= up.startTime && bt <= up.endTime && bp > up.startPrice) { inUp = true; break; }
            }
          }
          if (inUp) lastValid = { time: bt, price: bp };
        }
        break; // 只检查紧跟突破笔的第一个回调
      }
    }
    if (lastValid) thirdBuys.push(lastValid);
  }
  // 按时间去重后加入（同一位置同时满足类2买时，保留 3买）
  for (const t of thirdBuys) {
    if (points.some(p => p.type === "3买" && p.time === t.time)) continue;
    const dup = points.findIndex(p => p.type === "类2买" && p.time === t.time);
    if (dup >= 0) points.splice(dup, 1);
    points.push({ type: "3买", time: t.time, price: t.price });
  }
  return points;
}

function findSellPoints(bis, upperBis, macdArr, barSec) {
  if (bis.length < 3) return [];
  const upIdx = [];
  bis.forEach((b, i) => { if (b.type === "up") upIdx.push(i); });
  // 候选一卖：创新高 + MACD 背驰（红柱面积变小 或 黄白线高点变低，即上涨动能减弱）
  const firstSells = [];
  for (let k = 1; k < upIdx.length; k++) {
    const cur = bis[upIdx[k]];
    // 本周期这笔与上一级别某笔完全重合（内部无结构，整笔即上级一笔），
    // 本周期无法判断背驰，1卖 由上一级别标记，本周期不标记
    if (isSameAsUpperBi(cur, upperBis, barSec)) {
      if (DEBUG) console.log(`[一卖跳过-与上级笔重合] ${fmtT(cur.endTime)}(${cur.endPrice}) 整笔与上一级别完全重合，本周期不标记`);
      continue;
    }
    let refer = null;
    for (let j = k - 1; j >= 0; j--) {
      const cand = bis[upIdx[j]];
      if (cand.span < cur.span * 0.5) continue;
      refer = cand;
      break;
    }
    if (refer && cur.endPrice > refer.endPrice) {
      const diverge = isBiDiverge(cur, refer, macdArr);
      if (DEBUG) {
        const cm = biMacdMetrics(cur, macdArr);
        const rm = biMacdMetrics(refer, macdArr);
        console.log(
          `[一卖候选] ${fmtT(cur.endTime)}(${cur.endPrice}) vs 参照 ${fmtT(refer.endTime)}(${refer.endPrice}) ` +
          `| 创新高=${cur.endPrice > refer.endPrice} ` +
          `| 红柱面积 ${cm ? cm.redArea.toFixed(2) : "-"} vs ${rm ? rm.redArea.toFixed(2) : "-"} (变小=${cm && rm ? cm.redArea < rm.redArea : false}) ` +
          `| DIF高点 ${cm ? cm.difHigh.toFixed(3) : "-"} vs ${rm ? rm.difHigh.toFixed(3) : "-"} (变低=${cm && rm ? cm.difHigh < rm.difHigh : false}) ` +
          `| 背驰=${diverge}`
        );
      }
      if (diverge) {
        firstSells.push({ biIdx: upIdx[k], time: cur.endTime, price: cur.endPrice });
      }
    }
  }
  const firstSell = firstSells.length > 0 ? firstSells[firstSells.length - 1] : null;

  // 1卖 锚定（区间套，与 1买 锚定对称）：低级别的 1卖 必须锚定到「上一级别上涨笔的结束点」。
  // 当候选一卖位于某上级上涨笔内部（该上涨笔尚未结束，例如 15分钟 8-17 10:00 位于
  // 1小时上涨笔 8-14 21:00→8-20 20:00 内部），该局部顶不是真正的 1卖，
  // 1卖 应上移到上级上涨笔的结束点（上涨线段的终点）。
  // 对**每一个**候选一卖都做锚定，全部保留；
  // 多个候选若锚定到同一个上级上涨笔结束点（同一位置），只保留一个避免重复标记
  const anchoredSells = [];
  const seenSellPos = new Set();
  for (const fs of firstSells) {
    let anchored = fs;
    if (upperBis && upperBis.length > 0) {
      const a = anchorFirstSell(fs, upperBis);
      if (a) {
        let bestBi = null, bestDist = Infinity;
        for (let i = 0; i < bis.length; i++) {
          const b = bis[i];
          if (b.type !== "up") continue;
          const d = Math.abs(b.endTime - a.time);
          if (d < bestDist) { bestDist = d; bestBi = i; }
        }
        anchored = {
          biIdx: bestBi !== null ? bestBi : fs.biIdx,
          time: a.time,
          price: a.price,
        };
      }
    }
    if (seenSellPos.has(anchored.time)) continue;
    seenSellPos.add(anchored.time);
    anchoredSells.push(anchored);
  }

  const points = [];

  // 2卖 / 类2卖（区间套）：
  //   有上级笔时，只在「上一级别下跌笔」段内找次高点：
  //     段内第一个次高点（低于段起点）= 2卖；其后第一个更低次高点 = 类2卖。
  //   无上级笔（日线为最高级别）时，沿用结构顶逻辑。
  if (upperBis && upperBis.length > 0) {
    for (const dn of upperBis) {
      if (dn.type !== "down") continue;
      const highs = [];
      for (let i = 0; i < bis.length; i++) {
        const b = bis[i];
        if (b.type !== "up") continue;
        if (b.endTime >= dn.startTime && b.endTime <= dn.endTime + 1) {
          highs.push({ biIdx: i, time: b.endTime, price: b.endPrice });
        }
      }
      if (highs.length === 0) continue;
      highs.sort((a, b2) => a.time - b2.time);
      const firstHigh = highs.find(h => h.price < dn.startPrice);
      if (firstHigh) {
        points.push({ type: "2卖", time: firstHigh.time, price: firstHigh.price });
        const laterLow = highs.find(h => h.time > firstHigh.time && h.price < firstHigh.price);
        if (laterLow) points.push({ type: "类2卖", time: laterLow.time, price: laterLow.price });
      }
    }
  } else {
    // 结构顶：最近一卖之前（或全窗口）的最高顶，作为下跌段的起点
    let structTopIdx = null;
    if (firstSell) {
      let maxP = -Infinity;
      for (const i of upIdx) {
        if (i >= firstSell.biIdx) break;
        if (bis[i].endPrice > maxP) { maxP = bis[i].endPrice; structTopIdx = i; }
      }
    }
    if (structTopIdx === null) {
      let maxP = -Infinity;
      for (const i of upIdx) {
        if (bis[i].endPrice > maxP) { maxP = bis[i].endPrice; structTopIdx = i; }
      }
    }
    if (structTopIdx !== null) {
      const top = bis[structTopIdx];
      let secondSell = null;
      for (let i = structTopIdx + 1; i < bis.length; i++) {
        if (bis[i].type !== "up") continue;
        if (bis[i].endPrice < top.endPrice) {
          secondSell = { biIdx: i, time: bis[i].endTime, price: bis[i].endPrice };
          break;
        }
      }
      if (secondSell) {
        points.push({ type: "2卖", time: secondSell.time, price: secondSell.price });
        let classSecond = null;
        for (let i = secondSell.biIdx + 1; i < bis.length; i++) {
          if (bis[i].type !== "up") continue;
          if (bis[i].endPrice < secondSell.price) {
            classSecond = { time: bis[i].endTime, price: bis[i].endPrice };
            break;
          }
        }
        if (classSecond) points.push({ type: "类2卖", time: classSecond.time, price: classSecond.price });
      }
    }
  }

  // 1卖：所有 MACD 背驰顶（锚定到上级上涨笔结束点），15分钟及以上周期保留全部
  for (const as of anchoredSells) {
    points.push({ type: "1卖", time: as.time, price: as.price });
  }

  // 3卖：2卖过后的下跌段未出现背驰（下跌笔创新低、跌破前底），其后的反弹就是 3卖。
  // 规则（与 3买 对称）：
  //   1) 对每一笔 2卖 检查其后的下跌段，每个 2卖 段最多标一个 3卖，
  //      且扫描范围限制到「下一个 2卖」之前（避免跨段覆盖/重复）；
  //   2) 前底 = 2卖 之前最近一个下跌笔的结束点；
  //   3) 段内取「跌破前底的下跌笔 + 其后第一个反弹不破前底」中最后满足的一个；
  //   4) 区间套：反弹结束点必须位于上一级别下跌笔段内，且反弹价低于该下跌笔起点价
  //     （排除反弹恰好落在上级笔起点/新段启动点的情况）。
  const twoSells = points.filter(p => p.type === "2卖").sort((a, b) => a.time - b.time);
  const thirdSells = [];
  for (let k = 0; k < twoSells.length; k++) {
    const ts = twoSells[k];
    const twoIdx = bis.findIndex(b => b.endTime === ts.time);
    if (twoIdx < 0) continue;
    const endScan = k + 1 < twoSells.length
      ? bis.findIndex(b => b.endTime === twoSells[k + 1].time)
      : bis.length;
    // 前底 = 2卖 之前最近一个下跌笔的结束点
    let prevLow = null;
    for (let j = twoIdx - 1; j >= 0; j--) {
      if (bis[j].type === "down") { prevLow = bis[j].endPrice; break; }
    }
    if (prevLow === null) continue;
    let lastValid = null;
    for (let i = twoIdx + 1; i < endScan; i++) {
      if (bis[i].type !== "down") continue;
      if (bis[i].endPrice >= prevLow) continue; // 未跌破前底
      // 其后第一个反弹笔：反弹高点不破前底 → 3卖候选
      for (let mm = i + 1; mm < endScan; mm++) {
        if (bis[mm].type !== "up") continue;
        const st = bis[mm].endTime, sp = bis[mm].endPrice;
        if (sp < prevLow) {
          // 区间套：反弹结束点须位于上级下跌笔段内，且反弹价低于该下跌笔起点价
          let inDown = true;
          if (upperBis && upperBis.length > 0) {
            inDown = false;
            for (const dn of upperBis) {
              if (dn.type === "down" && st >= dn.startTime && st <= dn.endTime && sp < dn.startPrice) { inDown = true; break; }
            }
          }
          if (inDown) lastValid = { time: st, price: sp };
        }
        break; // 只检查紧跟跌破笔的第一个反弹
      }
    }
    if (lastValid) thirdSells.push(lastValid);
  }
  // 按时间去重后加入（同一位置同时满足类2卖时，保留 3卖）
  for (const t of thirdSells) {
    if (points.some(p => p.type === "3卖" && p.time === t.time)) continue;
    const dup = points.findIndex(p => p.type === "类2卖" && p.time === t.time);
    if (dup >= 0) points.splice(dup, 1);
    points.push({ type: "3卖", time: t.time, price: t.price });
  }
  return points;
}

// ============================================================
// 买卖点锚定与显示配置
// ============================================================

/**
 * 一买锚定：低级别的一买必须锚定到「上一级别某笔的起点」。
 * 在上级笔列表中取「候选一买时间之前、时间上最近的一个底部端点」
 * （上级上涨笔的起点 或 上级下跌笔的终点）。
 * 找不到则返回 null（该周期不标记一买）。
 */
function anchorFirstBuy(cand, upperBis) {
  if (!upperBis || upperBis.length === 0) return null;
  let best = null;
  for (const b of upperBis) {
    const t = b.type === "up" ? b.startTime : b.endTime;
    const p = b.type === "up" ? b.startPrice : b.endPrice;
    if (t > cand.time) continue; // 只取候选一买之前的上级底
    if (!best || cand.time - t < cand.time - best.time) best = { time: t, price: p };
  }
  return best;
}

/**
 * 一卖锚定：低级别的一卖必须锚定到「上一级别上涨笔的结束点」（上涨线段的终点）。
 * 1) 若候选一卖位于某上级上涨笔内部（该上涨笔尚未结束，endTime 晚于候选时间），
 *    说明上涨线段仍在延续，该局部顶不是真正的 1卖，上移到上级上涨笔的结束点；
 * 2) 否则取「候选一卖之前、时间上最近的一个顶部端点」（上级上涨笔的终点 或 下跌笔的起点）。
 * 找不到则返回 null。
 */
function anchorFirstSell(cand, upperBis) {
  if (!upperBis || upperBis.length === 0) return null;
  // 1) 上涨延续中：候选一卖被上级上涨笔包含
  for (const b of upperBis) {
    if (b.type !== "up") continue;
    if (b.startTime <= cand.time && b.endTime >= cand.time) {
      return { time: b.endTime, price: b.endPrice };
    }
  }
  // 2) 候选一卖之前最近的上级顶
  let best = null;
  for (const b of upperBis) {
    const t = b.type === "up" ? b.endTime : b.startTime;
    const p = b.type === "up" ? b.endPrice : b.startPrice;
    if (t > cand.time) continue; // 只取候选一卖之前的上级顶
    if (!best || cand.time - t < cand.time - best.time) best = { time: t, price: p };
  }
  return best;
}

/**
 * 判断本周期某笔是否与上一级别某笔完全重合（起点、终点时间与价格一致）。
 * 完全重合说明本周期该笔内部无更细结构（整笔就是上一级别的一笔），
 * 本周期无法在本级别判断背驰，1类买卖点应由上一级别标记，本周期不标记。
 * 例：15分钟 8-11 21:15(86.60) → 8-12 12:00(90.07) 与 1小时 同区间笔完全重合，
 *     该笔在15分钟内部没有次级别结构，15分钟不应标记 1卖，由 1小时 标记。
 */
function isSameAsUpperBi(bi, upperBis, barSec) {
  if (!upperBis || upperBis.length === 0) return false;
  // 时间容差 = 本周期 1 个 bar：低级别笔用更低一级校准（如 15分钟用3分钟校准，
  // 端点在3分钟bar边界如 21:39），上级笔用本周期校准（如 1小时用15分钟校准，
  // 端点在15分钟bar边界如 21:30），同笔的两端点时间可能相差最多一个本周期bar。
  // 若容差过小（60秒），会把实际同笔误判为不同笔，导致本周期仍标记1类买卖点。
  const tEps = barSec || 900;
  const pEps = 0.01; // 价格容差
  for (const ub of upperBis) {
    if (ub.type !== bi.type) continue;
    if (Math.abs(ub.startTime - bi.startTime) <= tEps &&
        Math.abs(ub.endTime - bi.endTime) <= tEps &&
        Math.abs(ub.startPrice - bi.startPrice) <= pEps &&
        Math.abs(ub.endPrice - bi.endPrice) <= pEps) {
      return true;
    }
  }
  return false;
}

/**
 * 把极值价格/时间映射到本周期K线的 bar 边界：
 * 在本周期K线中找「high 或 low 达到该价格」的K线，取时间最接近参考时间的。
 */
function snapToOwnBar(price, refTime, bars) {
  const eps = 0.001;
  let best = null, bestDist = Infinity;
  for (const k of bars) {
    if (Math.abs(k.high - price) < eps || Math.abs(k.low - price) < eps) {
      const d = Math.abs(k.time - refTime);
      if (d < bestDist) { bestDist = d; best = k.time; }
    }
  }
  if (best !== null) return best;
  // 兜底：吸附到时间上最近的K线
  let nearest = bars.length > 0 ? bars[0].time : refTime;
  let nd = Infinity;
  for (const k of bars) { const d = Math.abs(k.time - refTime); if (d < nd) { nd = d; nearest = k.time; } }
  return nearest;
}

/**
 * 低级别每类买卖点只保留时间上最近的一个（避免区间套在低级别产生大量标记）。
 */
function keepRecentEach(points) {
  const byType = {};
  for (const p of points) {
    if (!byType[p.type] || p.time > byType[p.type].time) byType[p.type] = p;
  }
  return Object.values(byType).sort((a, b) => a.time - b.time);
}

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

function intervalSecOf(res) {
  const r = String(res).toUpperCase();
  if (r === "3") return 180;
  if (r === "5") return 300;
  if (r === "15") return 900;
  if (r === "30") return 1800;
  if (r === "60" || r === "1H") return 3600;
  if (r === "240" || r === "4H") return 14400;
  if (r === "D" || r === "1D") return 86400;
  if (r === "W" || r === "1W") return 604800;
  return 0;
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

    const fetchBars = async (expectedIntervalSec, fromTs, buffer) => {
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
            const tolerance = ${JSON.stringify(tolerance)};
            if (fromTs) {
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
    const refCache = {};
    let upperBis = null; // 上一级周期的笔（用于一买锚定）
    const periodMarks = {}; // 各周期已生成的标记（用于跨周期共振判定：上级买卖点 ↔ 本级别1类）
    for (let pi = 0; pi < PERIODS.length; pi++) {
      const res = PERIODS[pi];
      if (res !== currentRes) {
        await ensureResolution(res);
        currentRes = res;
      }

      let d = await fetchBars(intervalSecOf(res), FROM_TS, ANCHOR_BUFFER);
      if (!d || d.error || !d.bars || d.bars.length === 0) {
        // 数据未加载好（切换周期时序问题），等待后重试一次
        console.log(`\n[周期 ${res}] 首次取数失败，等待重试...`);
        await sleep(3000);
        await ensureResolution(res);
        currentRes = res;
        d = await fetchBars(intervalSecOf(res), FROM_TS, ANCHOR_BUFFER);
      }
      if (!d || d.error || !d.bars || d.bars.length === 0) {
        console.log(`\n[周期 ${res}] 无K线数据或切换失败，跳过`);
        continue;
      }

      // 计算本周期笔（从起始日期开始 + 前30根缓冲）
      const rawBars = d.bars;
      const merged = mergeBars(rawBars);
      const fractals = findFractals(merged);
      const atr = calcATR(rawBars, 14);
      const macdArr = calcMACD(rawBars);
      let bis = buildBi(fractals, merged, atr, macdArr);

      // ATR 过滤
      const threshold = atr * ATR_FILTER;
      bis = bis.filter(b => b.span >= threshold);

      // 只画起始日期之后的笔
      let curBis = bis.filter(b => b.endTime >= FROM_TS);

      // 未完成笔延伸
      curBis = extendLastBi(curBis, rawBars);

      // 跨周期端点校准（低一级校准，与画笔一致）
      const lowerRes = lowerResOf(res);
      if (lowerRes) {
        let refBars = refCache[lowerRes];
        if (!refBars) {
          await ensureResolution(lowerRes);
          const dref = await fetchBars(intervalSecOf(lowerRes), FROM_TS, ANCHOR_BUFFER);
          if (dref && !dref.error && dref.bars && dref.bars.length > 0) {
            refBars = dref.bars;
            refCache[lowerRes] = refBars;
          }
          await ensureResolution(res);
          currentRes = res;
        }
        if (refBars) {
          curBis = calibrateBiTimes(curBis, rawBars, refBars, intervalSecOf(res));
        }
      }

      // 计算买卖点（区间套：2买/类2买 只在上级上涨笔段内，2卖/类2卖 只在上级下跌笔段内）
      // 1买/1卖 必须满足 MACD 背驰（柱状体面积变小 或 黄白线动能减弱）才标记
      let buyPts = findBuyPoints(curBis, upperBis, macdArr, intervalSecOf(res));
      let sellPts = findSellPoints(curBis, upperBis, macdArr, intervalSecOf(res));

      // 所有周期（含 3分钟）都保留区间套下识别出的全部买卖点，不做每类只留最近一个的过滤

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

      // 汇总标记：把时间吸附到本周期bar边界
      // rawTime/rawPrice 保存未吸附的原始点位，用于跨周期共振判定
      const marks = [];
      const offset = Math.max(atr * 0.5, 0.05);
      for (const p of anchoredBuyPts) {
        const t = snapToOwnBar(p.price, p.time, rawBars);
        marks.push({ label: p.type, time: t, price: p.price - offset, rawTime: p.time, rawPrice: p.price });
      }
      for (const p of sellPts) {
        const t = snapToOwnBar(p.price, p.time, rawBars);
        marks.push({ label: p.type, time: t, price: p.price + offset, rawTime: p.time, rawPrice: p.price });
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
