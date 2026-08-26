/**
 * 缠论算法核心（唯一算法源）
 * 纯函数模块，不依赖 CDP、不绘图。供 chan-bi（画笔）与 mark-buy-sell（买卖点）两个 SKILL 复用。
 *
 * 统一约定：
 *   - 笔对象字段：type(up/down)、startIdx/endIdx(合并K线索引)、startTime/endTime(校准后端点时间)、
 *     startPrice/endPrice、rawCount(覆盖原始K线数)、span(幅度)、gapLocked(跳空成笔)、macdCross(MACD变色成笔)
 *   - 配置：CHAN_CFG.gapFilter（跳空独立成笔阈值，默认 1.0）、CHAN_CFG.debug（调试打印）
 *   - 所有时间均为 Unix 秒（UTC），与 TradingView K线时间一致
 *
 * 用法：
 *   const core = require("./chan_core.js");
 *   core.CHAN_CFG.debug = DEBUG;
 *   core.CHAN_CFG.gapFilter = GAP_FILTER;
 *   const merged = core.mergeBars(rawBars);
 *   ...
 */

// ============================================================
// 配置
// ============================================================

const CHAN_CFG = {
  gapFilter: 1.0, // 跳空独立成笔阈值：相邻K线缺口 >= gapFilter*ATR 时强制独立成笔
  debug: false,   // 调试打印（buildBi / 买卖点识别过程）
};

// ============================================================
// 1. 包含关系处理（合并K线）
// ============================================================

/**
 * 包含关系处理（合并K线）
 * 相邻K线有包含关系时合并，方向由前序趋势决定：
 *   向上合并取「高高」，向下合并取「低低」
 * 每根合并K线记录 _rawCount（覆盖的原始K线数），以及 highTime/lowTime（极值原始K线时间）
 */
function mergeBars(rawBars) {
  const merged = [];
  let direction = 0;
  for (const bar of rawBars) {
    if (merged.length === 0) {
      merged.push({ ...bar, _rawCount: 1, highTime: bar.time, lowTime: bar.time, rawHigh: bar.high, rawLow: bar.low });
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
      // 记录覆盖原始K线的真实极值范围（跳空检测用，不受合并方向高低取舍影响）
      if (bar.high > last.rawHigh) last.rawHigh = bar.high;
      if (bar.low < last.rawLow) last.rawLow = bar.low;
      last._rawCount += 1;
      last.time = bar.time;
      direction = dir;
    } else {
      direction = bar.high > last.high ? 1 : -1;
      merged.push({ ...bar, _rawCount: 1, highTime: bar.time, lowTime: bar.time, rawHigh: bar.high, rawLow: bar.low });
    }
  }
  return merged;
}

// ============================================================
// 2. 分型识别
// ============================================================

/**
 * 分型识别（顶分型/底分型）
 * 顶分型：中间K线最高，且整体高于左右
 * 底分型：中间K线最低，且整体低于左右
 * time 取极值所在的原始K线时间（顶分型用最高价时间，底分型用最低价时间）
 */
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

// ============================================================
// 3. 笔构建辅助函数
// ============================================================

/** 统计 (startIdx, endIdx] 覆盖的原始K线数 */
function countRaw(merged, startIdx, endIdx) {
  let t = 0;
  for (let k = startIdx + 1; k <= endIdx; k++) t += merged[k]._rawCount;
  return t;
}

/**
 * 检测两个分型（合并K线索引区间）之间是否存在跳空缺口。
 * 跳空 = 相邻合并K线之间的价格缺口（向上跳空：后K最低价 > 前K最高价；
 * 向下跳空：后K最高价 < 前K最低价），且缺口幅度 >= gapFilter*ATR。
 */
function hasGapBetween(merged, aIdx, bIdx, atr, gapFilter) {
  const th = atr * gapFilter;
  for (let i = aIdx; i < bIdx; i++) {
    const cur = merged[i], next = merged[i + 1];
    // 用覆盖原始K线的真实极值范围判断跳空，避免合并K线（向下合并压低高点/向上合并抬高低点）
    // 造成「假缺口」：真实原始K线之间若无价格跳空，不应被判为跳空。
    const curHigh = cur.rawHigh !== undefined ? cur.rawHigh : cur.high;
    const curLow = cur.rawLow !== undefined ? cur.rawLow : cur.low;
    const nextHigh = next.rawHigh !== undefined ? next.rawHigh : next.high;
    const nextLow = next.rawLow !== undefined ? next.rawLow : next.low;
    const gapUp = nextLow - curHigh;
    const gapDown = curLow - nextHigh;
    if (gapUp >= th || gapDown >= th) return true;
  }
  return false;
}

// ============================================================
// 4. 笔构建（交替分型序列 + 回溯替换）
// ============================================================

/**
 * 笔的构建：
 *   - 阶段一：构建严格交替的分型序列（连续同类型分型：顶取最高、底取最低）
 *   - 阶段二：遍历序列，处理跳空成笔 / MACD变色成笔 / 前顶前底作废 / 分型范围脱离 / 极值规则
 *   - 阶段三：两两连笔（此时首尾自然连续）
 */
function buildBi(fractals, merged, atr, macdArr, lockedPivots) {
  const gapThreshold = atr ? atr * CHAN_CFG.gapFilter : 0;
  // 阶段一：严格交替分型序列
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

  // 区间套强制对齐（优先级最高）：上级笔端点（lockedPivots）必须在下级笔中被保留为端点，
  // 不能被阶段二的任何「移除中间分型」逻辑（MACD端点让位/前顶前底作废/回溯替换）吞掉。
  // 在阶段一序列上，把与上级端点「方向一致且价格一致」的分型标记为 locked。
  if (lockedPivots && lockedPivots.length) {
    for (const f of seq) {
      const p = f.type === "top" ? f.high : f.low;
      for (const lp of lockedPivots) {
        if (lp.dir === f.type && Math.abs(lp.price - p) <= 0.001) {
          f.locked = true;
          break;
        }
      }
    }
  }

  // 有效笔判断：合并后K线从起点分型到终点分型（含两端分型）至少 5 根即可成笔。
  // gap = b.mergedIdx - a.mergedIdx，等价于合并K线数 gap+1 >= 5。
  const isValid = (a, b) => {
    const gap = b.mergedIdx - a.mergedIdx;
    return gap >= 4;
  };

  // 笔内极值检查：一笔的顶/底必须是该笔范围内所有K线的最高/最低点。
  const noMoreExtremeInside = (a, b) => {
    for (let i = a.mergedIdx + 1; i < b.mergedIdx; i++) {
      if (b.type === "bottom" && merged[i].low < b.low) return false;
      if (b.type === "top" && merged[i].high > b.high) return false;
    }
    return true;
  };

  // 分型范围脱离检查：一笔的两端分型不能互相"包含"。
  // 顶分型 → 底分型（下跌笔）：底分型的底必须**低于顶分型三根K线的最低点**；
  // 底分型 → 顶分型（上涨笔）：顶分型的顶必须**高于底分型三根K线的最高点**。
  const fractalRangeClear = (a, b) => {
    const i = a.mergedIdx;
    const rangeLow = Math.min(merged[i - 1].low, merged[i].low, merged[i + 1].low);
    const rangeHigh = Math.max(merged[i - 1].high, merged[i].high, merged[i + 1].high);
    if (a.type === "top" && b.type === "bottom") return b.low < rangeLow;
    if (a.type === "bottom" && b.type === "top") return b.high > rangeHigh;
    return true;
  };

  if (CHAN_CFG.debug) {
    const ft = (s) => `${s.type === "top" ? "顶" : "底"}@${s.mergedIdx}(${s.type === "top" ? s.high : s.low})${s.locked ? "(锁定)" : ""}`;
    console.log("[阶段一] 交替分型序列:", seq.map(ft).join(" → "));
  }

  // 阶段二：移除间隔不足的中间分型（回溯替换）
  const result = [];
  for (const k of seq) {
    if (result.length === 0) { result.push(k); continue; }
    const last = result[result.length - 1];
    if (k.type === last.type) {
      if (last.locked) {
        // locked 端点（上级笔端点，区间套强制对齐）不可被同类型分型替换
        continue;
      }
      if (!last.gapLocked) {
        if (k.type === "top") { if (k.high >= last.high) result[result.length - 1] = k; }
        else { if (k.low <= last.low) result[result.length - 1] = k; }
      } else {
        // 跳空锁定的端点：仅当后续同类型分型「突破」锁定价格时才解锁替换
        if (k.type === "top") { if (k.high > last.high) result[result.length - 1] = k; }
        else { if (k.low < last.low) result[result.length - 1] = k; }
      }
      continue;
    }
    // 异类型
    // MACD 变色成笔端点让位：若 result[-2] 是 MACD 变色成笔的端点，且当前分型 k 是
    // 更极端的同类型分型，让位更新该端点并移除中间分型，保证 MACD 变色成笔的终点是
    // 区间内最新的绝对极值（例：92.83 应让位给更低的 92.74）。
    if (result.length >= 2) {
      const prev2 = result[result.length - 2];
      const topOne = result[result.length - 1];
      if (prev2.macdCross === true && prev2.type === k.type &&
          !topOne.locked &&
          ((k.type === "top" && k.high > prev2.high) || (k.type === "bottom" && k.low < prev2.low))) {
        if (CHAN_CFG.debug) console.log(`[阶段二] MACD端点让位: ${prev2.type === "top" ? "顶" : "底"}@${prev2.mergedIdx}(${prev2.type === "top" ? prev2.high : prev2.low}) → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 更新为更极端${k.type === "top" ? "高点" : "低点"}，移除中间分型`);
        k.macdCross = true;
        result[result.length - 2] = k;
        result.pop();
        continue;
      }
    }
    // 跳空优先：若 last→k 之间存在幅度 >= gapFilter*ATR 的跳空缺口，则强制独立成笔
    const hasGap = gapThreshold > 0 && hasGapBetween(merged, last.mergedIdx, k.mergedIdx, atr, CHAN_CFG.gapFilter);
    if (hasGap) {
      if (CHAN_CFG.debug) console.log(`[阶段二] 跳空成笔: ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx} → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx} (缺口≥${gapThreshold.toFixed(2)})`);
      k.gapLocked = true;
      result.push(k);
      continue;
    }
    // 前顶/前底作废：prev2（result[-2]）与 k 同类型，且 prev2→last 不构成有效笔
    // （间隔不足，prev2 是脆弱端点），而 k 比 prev2 更极端（创新高/新低）时，
    // prev2 作为笔端点已被市场否定，应让 k 顶替 prev2 并移除中间的 last。
    // 附加约束（防止误伤真实顶/底）：
    //   1) last 必须不是极端点：last 不得比 prev3 更极端（否则 last 是深回调的真实转折）；
    //   2) 回调/反弹必须浅：pull/bounce < 上涨/下跌幅度的 50%；
    //   3) last 必须是「弱分型」：MACD 变色成笔且原始K线 < 5 根。
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
          !last.locked && !prev2.locked &&
          ((k.type === "top" && k.high > prev2.high) || (k.type === "bottom" && k.low < prev2.low))) {
        if (CHAN_CFG.debug) console.log(`[阶段二] 前顶/前底作废: ${prev2.type === "top" ? "顶" : "底"}@${prev2.mergedIdx}(${prev2.type === "top" ? prev2.high : prev2.low}) 被 ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 突破，k 顶替 prev2，移除中间 ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}`);
        if (prev2.macdCross === true) k.macdCross = true;
        result[result.length - 2] = k;
        result.pop();
        continue;
      }
    }
    if (isValid(last, k) && (noMoreExtremeInside(last, k) || last.gapLocked) && (fractalRangeClear(last, k) || last.gapLocked)) {
      result.push(k);
    } else if (isValid(last, k)) {
      // 间隔足够但区间内存在更极端的点 或 分型范围未脱离：k 不能作为笔端点，
      // 等待后续更极端的分型或并入更大的笔
      if (CHAN_CFG.debug) {
        const fr = fractalRangeClear(last, k);
        const ex = noMoreExtremeInside(last, k);
        console.log(`[阶段二] 忽略 k: ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 极值冲突=${!ex} 分型范围未脱离=${!fr}`);
      }
    } else {
      // 间隔不足：先检查 last→k 是否满足「合并后只有4根K + 方向性 MACD 变色」成笔。
      // 方向性变色：底到顶(上涨) 柱状体由绿变红；顶到底(下跌) 柱状体由红变绿。
      const gap = k.mergedIdx - last.mergedIdx;
      const direction = last.type === "bottom" ? "up" : "down";
      const macdCross = macdArr && hasMacdCrossBetween(macdArr, merged, last.mergedIdx, k.mergedIdx, last.time, k.time, direction);
      const macdRawCount = countRaw(merged, last.mergedIdx, k.mergedIdx);
      if (gap === 3 && macdCross && noMoreExtremeInside(last, k)) {
        if (CHAN_CFG.debug) console.log(`[阶段二] MACD变色成笔: ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx} → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx} (合并4根K, ${direction === "up" ? "绿变红" : "红变绿"})`);
        k.macdCross = true;
        k.macdRaw = macdRawCount;
        result.push(k);
      } else {
        // 间隔不足且无 MACD 变色：中间分型 last 作废，k 回溯与 result[-2]（同类型）比较
        if (result.length >= 2 && result[result.length - 2].type === k.type) {
          const prev = result[result.length - 2];
          const moreExtreme = k.type === "top" ? k.high >= prev.high : k.low <= prev.low;
          const gapPrevLast = last.mergedIdx - prev.mergedIdx;
          const gapPrevK = k.mergedIdx - prev.mergedIdx;
          if (CHAN_CFG.debug) console.log(`[阶段二] 间隔不足: ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx} 与 ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}, 回溯比较同类型 prev, moreExtreme=${moreExtreme}, gapPrevLast=${gapPrevLast}`);
          // 仅当 prev→last 这笔是「脆弱笔」（合并间隔 <= 12）时才允许 k 回溯替换 prev；
          // 或 prev 与 k 之间已满足最小笔间隔（合并K线 >= 4）时，前顶/前底已被市场否定。
          if (moreExtreme && (gapPrevLast <= 12 || gapPrevK >= 4)) {
            // 回溯替换保护（区间套一致性）：当 last 比更早的同类型分型 result[-3] 更极端时，
            // last 是笔内真实转折点（如插针低点/插针高点），不能无条件 pop 掉——吞掉会导致
            // 该笔内部藏着更极值（违反笔内极值原则），且本级别笔端点与上级周期（区间套）不重合。
            // 此时保留 last 取代 result[-3]，prev 被更高顶/更低底突破而作废移除，
            // k 与 last 间隔不足、暂不接入，等待后续满足最小间隔的分型成笔。
            if (result.length >= 3) {
              const prev3 = result[result.length - 3];
              const lastIsDeeper =
                (k.type === "top") ? (last.low < prev3.low) : (last.high > prev3.high);
              if (lastIsDeeper && !prev.locked && !prev3.locked) {
                if (CHAN_CFG.debug) console.log(`[阶段二] 回溯替换保护: ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}(${last.type === "top" ? last.high : last.low}) 比 ${prev3.type === "top" ? "顶" : "底"}@${prev3.mergedIdx}(${prev3.type === "top" ? prev3.high : prev3.low}) 更极端，保留 last 为端点，作废 ${prev.type === "top" ? "顶" : "底"}@${prev.mergedIdx}(${prev.type === "top" ? prev.high : prev.low})，暂不接入 ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}`);
                result[result.length - 3] = last;
                result.pop();
                result.pop();
                continue;
              }
            }
            if (!last.locked && !prev.locked) {
              result[result.length - 2] = k;
              result.pop();
            }
          }
        }
      }
    }
  }

  if (CHAN_CFG.debug) {
    const ft = (s) => `${s.type === "top" ? "顶" : "底"}@${s.mergedIdx}(${s.type === "top" ? s.high : s.low})`;
    console.log("[阶段二] 结果序列:", result.map(ft).join(" → "));
  }

  // 阶段三：两两连笔
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

// ============================================================
// 5. ATR / MACD
// ============================================================

/** 计算 ATR（14周期平均真实波幅） */
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

/**
 * 计算 MACD（基于原始K线收盘价 EMA12/EMA26/DIF/DEA）
 * 约定：macd > 0 为红柱（多头动能），macd < 0 为绿柱（空头动能）。
 * 返回：[{ time, macd, dif, dea }, ...]，time 与原始K线一一对应。
 */
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

// ============================================================
// 6. MACD 背驰判定
// ============================================================

/** 模块级时间格式化（供 DEBUG 打印使用） */
function fmtT(ts) {
  const dt = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${dt.getMonth()+1}-${dt.getDate()} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
}

/**
 * 计算一笔区间内的 MACD 动能指标（用于背驰判定）
 * 返回：{ redArea, greenArea, difHigh, difLow }
 */
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

/**
 * MACD 背驰判定（OR 关系，满足其一即算背驰）：
 *   底背驰（对应一买，下跌笔）：绿柱面积变小 或 黄白线低点抬高（下跌动能减弱）
 *   顶背驰（对应一卖，上涨笔）：红柱面积变小 或 黄白线高点变低（上涨动能减弱）
 */
function isBiDiverge(bi, refer, macdArr) {
  const cur = biMacdMetrics(bi, macdArr);
  const ref = biMacdMetrics(refer, macdArr);
  if (!cur || !ref) return false;
  if (bi.type === "down") {
    return cur.greenArea < ref.greenArea || cur.difLow > ref.difLow;
  }
  return cur.redArea < ref.redArea || cur.difHigh < ref.difHigh;
}

// ============================================================
// 7. MACD 红绿转换检测
// ============================================================

/**
 * 检测两个分型（合并K线索引区间）之间是否发生方向性 MACD 红绿转换。
 *   direction === "up"  ：底到顶（上涨），柱状体由绿变红（<=0 转 >0）
 *   direction === "down"：顶到底（下跌），柱状体由红变绿（>0 转 <=0）
 *   其余（undefined）：任意红绿转换（历史兼容）
 * 检测区间用「分型的极值时间」作为边界（而不是合并K线的最新时间），
 * 避免把顶底极值之后（合并K线包含区间内）的 MACD 变化误算进来。
 */
function hasMacdCrossBetween(macdArr, merged, aIdx, bIdx, aTime, bTime, direction) {
  if (!macdArr || macdArr.length === 0) return false;
  const t0 = aTime !== undefined ? aTime : merged[aIdx].time;
  const t1 = bTime !== undefined ? bTime : merged[bIdx].time;
  let prev = null;
  for (const mm of macdArr) {
    if (mm.time < t0) continue;
    if (mm.time > t1) break;
    if (prev !== null) {
      let crossed;
      if (direction === "up") {
        crossed = prev.macd <= 0 && mm.macd > 0;
      } else if (direction === "down") {
        crossed = prev.macd > 0 && mm.macd <= 0;
      } else {
        crossed = (prev.macd >= 0 && mm.macd < 0) || (prev.macd <= 0 && mm.macd > 0);
      }
      if (crossed) return true;
    }
    prev = mm;
  }
  return false;
}

// ============================================================
// 8. 未完成笔延伸 / 周期映射 / 端点校准
// ============================================================

/**
 * 未完成笔延伸：缠论要求最新一笔延伸到当前K线。
 * 当最后一笔方向上的极端价出现在窗口末尾（当前笔终点之后）时，
 * 把最后一笔的终点推进到该极端价所在K线。只处理最后一笔。
 */
function extendLastBi(bisArr, bars) {
  if (!bisArr || bisArr.length === 0) return bisArr;
  const last = bisArr[bisArr.length - 1];
  // 跳空独立成笔锁定的笔：终点固定在跳空缺口处，不参与延伸（避免吞并跳空笔）
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

/** 逐级校准映射：每个周期用其「低一级」周期校准端点时间（15分钟←3分钟，1小时←15分钟，4小时←1小时，日线←4小时） */
function lowerResOf(res) {
  const s = String(res).toUpperCase();
  if (s === "D" || s === "1D") return "240";
  if (s === "240" || s === "4H") return "60";
  if (s === "60" || s === "1H") return "15";
  if (s === "15") return "3";
  return null;
}

/**
 * 跨周期端点时间校准（逐级校准）：
 * 大周期K线的时间戳是 bar 起点，其内部最高/最低点可能发生在更晚的低一级K线上。
 * 每个周期用「低一级」周期K线校准：把本周期笔的端点时间校准到
 * 「低一级K线极值所在位置」，使不同周期对同一极值的标记位置在图上重合。
 */
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

/** 周期 → 单根K线时长（秒） */
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
// 9. 买卖点识别
// ============================================================

/**
 * 判断本周期某笔是否与上一级别某笔完全重合（起点、终点时间与价格一致）。
 * 完全重合说明本周期该笔内部无更细结构（整笔就是上一级别的一笔），
 * 本周期无法在本级别判断背驰，1类买卖点应由上一级别标记，本周期不标记。
 * 时间容差 = 本周期 1 个 bar（低级别笔用更低一级校准，与上级笔端点可能有最多一个bar的偏差）。
 */
function isSameAsUpperBi(bi, upperBis, barSec) {
  if (!upperBis || upperBis.length === 0) return false;
  const tEps = barSec || 900;
  const pEps = 0.01;
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
 * 一买锚定：低级别的一买必须锚定到「上一级别某笔的起点」。
 * 在上级笔列表中取「候选一买时间之前、时间上最近的一个底部端点」。
 * 找不到则返回 null（该周期不标记一买）。
 */
function anchorFirstBuy(cand, upperBis) {
  if (!upperBis || upperBis.length === 0) return null;
  let best = null;
  for (const b of upperBis) {
    const t = b.type === "up" ? b.startTime : b.endTime;
    const p = b.type === "up" ? b.startPrice : b.endPrice;
    if (t > cand.time) continue;
    if (!best || cand.time - t < cand.time - best.time) best = { time: t, price: p };
  }
  return best;
}

/**
 * 一卖锚定：低级别的一卖必须锚定到「上一级别上涨笔的结束点」（上涨线段的终点）。
 * 1) 若候选一卖位于某上级上涨笔内部（该上涨笔尚未结束），上移到上级上涨笔的结束点；
 * 2) 否则取「候选一卖之前、时间上最近的一个顶部端点」。
 * 找不到则返回 null。
 */
function anchorFirstSell(cand, upperBis) {
  if (!upperBis || upperBis.length === 0) return null;
  for (const b of upperBis) {
    if (b.type !== "up") continue;
    if (b.startTime <= cand.time && b.endTime >= cand.time) {
      return { time: b.endTime, price: b.endPrice };
    }
  }
  let best = null;
  for (const b of upperBis) {
    const t = b.type === "up" ? b.endTime : b.startTime;
    const p = b.type === "up" ? b.endPrice : b.startPrice;
    if (t > cand.time) continue;
    if (!best || cand.time - t < cand.time - best.time) best = { time: t, price: p };
  }
  return best;
}

/** 把极值价格/时间映射到本周期K线的 bar 边界 */
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
  let nearest = bars.length > 0 ? bars[0].time : refTime;
  let nd = Infinity;
  for (const k of bars) { const d = Math.abs(k.time - refTime); if (d < nd) { nd = d; nearest = k.time; } }
  return nearest;
}

/**
 * 买点识别（含区间套与 MACD 背驰）：
 *   1买：下跌笔创新低 + MACD 背驰（绿柱面积变小 或 黄白线低点抬高）
 *   2买/类2买：上一级别上涨笔段内的抬高低点
 *   3买：2买过后的上涨段未出现背驰（创新高突破前顶），其后回调不破前顶
 */
function findBuyPoints(bis, upperBis, macdArr, barSec) {
  if (bis.length < 3) return [];
  const downIdx = [];
  bis.forEach((b, i) => { if (b.type === "down") downIdx.push(i); });
  // 候选一买：创新低 + MACD 背驰
  const firstBuys = [];
  for (let k = 1; k < downIdx.length; k++) {
    const cur = bis[downIdx[k]];
    // 本周期这笔与上一级别某笔完全重合（内部无结构），本周期不标记 1买
    if (isSameAsUpperBi(cur, upperBis, barSec)) {
      if (CHAN_CFG.debug) console.log(`[一买跳过-与上级笔重合] ${fmtT(cur.endTime)}(${cur.endPrice}) 整笔与上一级别完全重合，本周期不标记`);
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
      if (CHAN_CFG.debug) {
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

  // 2买 / 类2买（区间套）：只在「上一级别上涨笔」段内找抬高低点
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
  // 对每一笔 2买 检查其后的上涨段（扫描范围限制到下一个 2买 之前，每段最多标一个），
  // 前顶 = 2买 之前最近一个上涨笔的结束点；区间套要求回调位于上级上涨笔段内且高于该笔起点价。
  const twoBuys = points.filter(p => p.type === "2买").sort((a, b) => a.time - b.time);
  const thirdBuys = [];
  for (let k = 0; k < twoBuys.length; k++) {
    const tb = twoBuys[k];
    const twoIdx = bis.findIndex(b => b.endTime === tb.time);
    if (twoIdx < 0) continue;
    const endScan = k + 1 < twoBuys.length
      ? bis.findIndex(b => b.endTime === twoBuys[k + 1].time)
      : bis.length;
    let prevTop = null;
    for (let j = twoIdx - 1; j >= 0; j--) {
      if (bis[j].type === "up") { prevTop = bis[j].endPrice; break; }
    }
    if (prevTop === null) continue;
    let lastValid = null;
    for (let i = twoIdx + 1; i < endScan; i++) {
      if (bis[i].type !== "up") continue;
      if (bis[i].endPrice <= prevTop) continue;
      for (let mm = i + 1; mm < endScan; mm++) {
        if (bis[mm].type !== "down") continue;
        const bt = bis[mm].endTime, bp = bis[mm].endPrice;
        if (bp > prevTop) {
          let inUp = true;
          if (upperBis && upperBis.length > 0) {
            inUp = false;
            for (const up of upperBis) {
              if (up.type === "up" && bt >= up.startTime && bt <= up.endTime && bp > up.startPrice) { inUp = true; break; }
            }
          }
          if (inUp) lastValid = { time: bt, price: bp };
        }
        break;
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

/**
 * 卖点识别（含区间套与 MACD 背驰，与买点对称）：
 *   1卖：上涨笔创新高 + MACD 背驰（红柱面积变小 或 黄白线高点变低），锚定到上级上涨笔结束点
 *   2卖/类2卖：上一级别下跌笔段内的次高点
 *   3卖：2卖过后的下跌段未出现背驰（创新低跌破前底），其后反弹不破前底
 */
function findSellPoints(bis, upperBis, macdArr, barSec) {
  if (bis.length < 3) return [];
  const upIdx = [];
  bis.forEach((b, i) => { if (b.type === "up") upIdx.push(i); });
  // 候选一卖：创新高 + MACD 背驰
  const firstSells = [];
  for (let k = 1; k < upIdx.length; k++) {
    const cur = bis[upIdx[k]];
    // 本周期这笔与上一级别某笔完全重合（内部无结构），本周期不标记 1卖
    if (isSameAsUpperBi(cur, upperBis, barSec)) {
      if (CHAN_CFG.debug) console.log(`[一卖跳过-与上级笔重合] ${fmtT(cur.endTime)}(${cur.endPrice}) 整笔与上一级别完全重合，本周期不标记`);
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
      if (CHAN_CFG.debug) {
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

  // 1卖 锚定：对**每一个**候选一卖都做锚定，全部保留；多个候选锚定到同一位置时去重
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

  // 2卖 / 类2卖（区间套）：只在「上一级别下跌笔」段内找次高点
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

  // 1卖：所有 MACD 背驰顶（锚定到上级上涨笔结束点），全部保留
  for (const as of anchoredSells) {
    points.push({ type: "1卖", time: as.time, price: as.price });
  }

  // 3卖：2卖过后的下跌段未出现背驰（下跌笔创新低、跌破前底），其后的反弹就是 3卖。
  // 与 3买 对称：对每一笔 2卖 检查其后的下跌段（扫描范围限制到下一个 2卖 之前），
  // 前底 = 2卖 之前最近一个下跌笔的结束点；区间套要求反弹位于上级下跌笔段内且低于该笔起点价。
  const twoSells = points.filter(p => p.type === "2卖").sort((a, b) => a.time - b.time);
  const thirdSells = [];
  for (let k = 0; k < twoSells.length; k++) {
    const ts = twoSells[k];
    const twoIdx = bis.findIndex(b => b.endTime === ts.time);
    if (twoIdx < 0) continue;
    const endScan = k + 1 < twoSells.length
      ? bis.findIndex(b => b.endTime === twoSells[k + 1].time)
      : bis.length;
    let prevLow = null;
    for (let j = twoIdx - 1; j >= 0; j--) {
      if (bis[j].type === "down") { prevLow = bis[j].endPrice; break; }
    }
    if (prevLow === null) continue;
    let lastValid = null;
    for (let i = twoIdx + 1; i < endScan; i++) {
      if (bis[i].type !== "down") continue;
      if (bis[i].endPrice >= prevLow) continue;
      for (let mm = i + 1; mm < endScan; mm++) {
        if (bis[mm].type !== "up") continue;
        const st = bis[mm].endTime, sp = bis[mm].endPrice;
        if (sp < prevLow) {
          let inDown = true;
          if (upperBis && upperBis.length > 0) {
            inDown = false;
            for (const dn of upperBis) {
              if (dn.type === "down" && st >= dn.startTime && st <= dn.endTime && sp < dn.startPrice) { inDown = true; break; }
            }
          }
          if (inDown) lastValid = { time: st, price: sp };
        }
        break;
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

/** 低级别每类买卖点只保留时间上最近的一个（历史策略保留，现主流程已不调用） */
function keepRecentEach(points) {
  const byType = {};
  for (const p of points) {
    if (!byType[p.type] || p.time > byType[p.type].time) byType[p.type] = p;
  }
  return Object.values(byType).sort((a, b) => a.time - b.time);
}

// ============================================================
// 导出
// ============================================================

/**
 * 由上级笔提取「锁定端点」列表（区间套强制对齐用）：
 * 上级笔的每个起点/终点都是一个明确的极值端点（上涨笔起点是底、终点是顶；下跌笔反之）。
 * 下级周期画笔时，把这些端点作为 lockedPivots 传入 buildBi，保证下级笔端点与上级对齐。
 */
function lockedPivotsOf(prevBis) {
  if (!prevBis || prevBis.length === 0) return null;
  const arr = [];
  for (const b of prevBis) {
    if (b.type === "up") {
      arr.push({ dir: "bottom", price: b.startPrice });
      arr.push({ dir: "top", price: b.endPrice });
    } else {
      arr.push({ dir: "top", price: b.startPrice });
      arr.push({ dir: "bottom", price: b.endPrice });
    }
  }
  return arr;
}

/**
 * 区间套强制对齐（优先级最高）：把下级周期笔的拐点对齐到上级周期笔的拐点。
 * 上级笔的每个起点/终点都是明确极值（顶/底），下级周期必须复现相同极值。
 * 当下级周期因包含关系把上级极值吞掉（如插针低点/高点）时，下级拐点会漂移到
 * 次极值上（例：上级底 4311.04 被下级画成 4311.27）；本函数把「同方向且时间最近」
 * 的下级拐点快照到上级拐点的（时间+价格），实现「上级笔与下级笔同笔」。
 *
 * @param {Array} lowerBis 下级周期笔（原地修改并返回）
 * @param {Array} upperBis 上级周期笔
 * @param {number} upperIntervalSec 上级周期K线间隔（秒），作为时间容差
 */
function alignBiToUpper(lowerBis, upperBis, upperIntervalSec) {
  if (!lowerBis || !upperBis || lowerBis.length === 0 || upperBis.length === 0) return lowerBis;
  // 上级拐点：每笔的起点+终点
  const upperPts = [];
  for (const b of upperBis) {
    if (b.type === "up") {
      upperPts.push({ time: b.startTime, price: b.startPrice, dir: "bottom" });
      upperPts.push({ time: b.endTime, price: b.endPrice, dir: "top" });
    } else {
      upperPts.push({ time: b.startTime, price: b.startPrice, dir: "top" });
      upperPts.push({ time: b.endTime, price: b.endPrice, dir: "bottom" });
    }
  }
  const tol = upperIntervalSec || 0;

  // 下级拐点：n 笔 → n+1 个拐点（相邻两笔共享同一拐点）
  const n = lowerBis.length;
  const pts = new Array(n + 1);
  for (let i = 0; i <= n; i++) {
    if (i === 0) {
      const b = lowerBis[0];
      pts[i] = { time: b.startTime, price: b.startPrice, dir: b.type === "up" ? "bottom" : "top" };
    } else if (i === n) {
      const b = lowerBis[n - 1];
      pts[i] = { time: b.endTime, price: b.endPrice, dir: b.type === "up" ? "top" : "bottom" };
    } else {
      const b = lowerBis[i];
      pts[i] = { time: b.startTime, price: b.startPrice, dir: b.type === "up" ? "bottom" : "top" };
    }
  }

  // 每个上级拐点：找同方向、时间最近且未使用的下级拐点，快照对齐。
  const used = new Array(n + 1).fill(false);
  for (const up of upperPts) {
    let best = -1, bestDiff = Infinity;
    for (let i = 0; i <= n; i++) {
      if (used[i]) continue;
      if (pts[i].dir !== up.dir) continue;
      const diff = Math.abs(pts[i].time - up.time);
      if (diff <= tol && diff < bestDiff) { bestDiff = diff; best = i; }
    }
    if (best >= 0) {
      const p = pts[best];
      // 仅当下级拐点「更不极端」（漏掉上级真极值）时才对齐时间+价格；
      // 否则（下级已找到相同极值）只对齐价格保持严格相等，保留下级更精确的时间。
      const lessExtreme = up.dir === "bottom" ? p.price > up.price : p.price < up.price;
      used[best] = true;
      p.price = up.price;
      if (lessExtreme) p.time = up.time;
    }
  }

  // 由快照后的拐点重建笔端点
  for (let i = 0; i < n; i++) {
    lowerBis[i].startTime = pts[i].time;
    lowerBis[i].startPrice = pts[i].price;
    lowerBis[i].endTime = pts[i + 1].time;
    lowerBis[i].endPrice = pts[i + 1].price;
    lowerBis[i].span = Math.abs(lowerBis[i].endPrice - lowerBis[i].startPrice);
  }
  return lowerBis;
}

module.exports = {
  CHAN_CFG,
  // K线/分型/笔
  mergeBars,
  findFractals,
  countRaw,
  hasGapBetween,
  buildBi,
  lockedPivotsOf,
  alignBiToUpper,
  calcATR,
  calcMACD,
  hasMacdCrossBetween,
  extendLastBi,
  lowerResOf,
  calibrateBiTimes,
  intervalSecOf,
  // MACD 背驰
  fmtT,
  biMacdMetrics,
  isBiDiverge,
  // 买卖点
  findBuyPoints,
  findSellPoints,
  anchorFirstBuy,
  anchorFirstSell,
  isSameAsUpperBi,
  snapToOwnBar,
  keepRecentEach,
};
