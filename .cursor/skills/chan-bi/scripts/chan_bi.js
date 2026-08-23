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
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");

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
// 缠论算法
// ============================================================

/**
 * 1. 包含关系处理（合并K线）
 * 相邻K线有包含关系时合并，方向由前序趋势决定：
 *   向上合并取「高高」，向下合并取「低低」
 * 每根合并K线记录 _rawCount（覆盖的原始K线数）
 */
function mergeBars(rawBars) {
  const merged = [];
  let direction = 0; // 0=初始, 1=向上, -1=向下

  for (const bar of rawBars) {
    if (merged.length === 0) {
      merged.push({ ...bar, _rawCount: 1, highTime: bar.time, lowTime: bar.time });
      continue;
    }
    const last = merged[merged.length - 1];
    // 判断包含关系
    const containUp = bar.high >= last.high && bar.low <= last.low;   // bar 包含 last
    const containDown = bar.high <= last.high && bar.low >= last.low; // last 包含 bar
    const hasContain = containUp || containDown;

    if (hasContain) {
      // 确定合并方向：由前两根已合并K线决定
      let dir = direction;
      if (dir === 0 && merged.length >= 2) {
        dir = last.high >= merged[merged.length - 2].high ? 1 : -1;
      }
      if (dir === 0) dir = 1; // 初始默认向上

      if (dir === 1) {
        // 向上：取高高
        if (bar.high > last.high) { last.high = bar.high; last.highTime = bar.time; }
        if (bar.low > last.low) { last.low = bar.low; last.lowTime = bar.time; }
      } else {
        // 向下：取低低
        if (bar.high < last.high) { last.high = bar.high; last.highTime = bar.time; }
        if (bar.low < last.low) { last.low = bar.low; last.lowTime = bar.time; }
      }
      last._rawCount += 1;
      last.time = bar.time; // 最新K线时间（仅用于排序展示）
      direction = dir;
    } else {
      // 无包含：更新方向
      direction = bar.high > last.high ? 1 : -1;
      merged.push({ ...bar, _rawCount: 1, highTime: bar.time, lowTime: bar.time });
    }
  }
  return merged;
}

/**
 * 2. 分型识别（顶分型/底分型）
 * 顶分型：中间K线最高，且整体高于左右
 * 底分型：中间K线最低，且整体低于左右
 * time 取极值所在的原始K线时间（顶分型用最高价时间，底分型用最低价时间）
 */
function findFractals(merged) {
  const fractals = [];
  for (let i = 1; i < merged.length - 1; i++) {
    const prev = merged[i - 1], cur = merged[i], next = merged[i + 1];
    // 顶分型
    if (cur.high > prev.high && cur.high > next.high && cur.low > prev.low && cur.low > next.low) {
      fractals.push({ mergedIdx: i, type: "top", high: cur.high, low: cur.low, time: cur.highTime });
    }
    // 底分型
    if (cur.low < prev.low && cur.low < next.low && cur.high < prev.high && cur.high < next.high) {
      fractals.push({ mergedIdx: i, type: "bottom", high: cur.high, low: cur.low, time: cur.lowTime });
    }
  }
  return fractals;
}

/**
 * 3. 笔的构建（交替分型序列 + 回溯替换）
 * 规则：
 *   - 阶段一：构建严格交替的分型序列（连续同类型分型：顶取最高、底取最低）
 *   - 阶段二：遍历序列，若相邻异类型分型间隔不足（合并K线间隔 < 4 或 覆盖原始K线 < 5），
 *             则该中间分型作废，更极端的后续分型回溯顶替前一同类型分型
 *   - 阶段三：两两连笔（此时首尾自然连续）
 */
function countRaw(merged, startIdx, endIdx) {
  let t = 0;
  for (let k = startIdx + 1; k <= endIdx; k++) t += merged[k]._rawCount;
  return t;
}

/**
 * 检测两个分型（合并K线索引区间）之间是否存在跳空缺口。
 * 跳空 = 相邻合并K线之间的价格缺口（向上跳空：后K最低价 > 前K最高价；
 * 向下跳空：后K最高价 < 前K最低价），且缺口幅度 >= gap*ATR。
 * 存在跳空缺口时，该处走势是「直接跳空」的，可以独立成笔，
 * 不受笔的最小K线数/间隔限制。
 */
function hasGapBetween(merged, aIdx, bIdx, atr, gapFilter) {
  const th = atr * gapFilter;
  for (let i = aIdx; i < bIdx; i++) {
    const cur = merged[i], next = merged[i + 1];
    const gapUp = next.low - cur.high;   // 向上跳空缺口大小
    const gapDown = cur.low - next.high; // 向下跳空缺口大小
    if (gapUp >= th || gapDown >= th) return true;
  }
  return false;
}

function buildBi(fractals, merged, atr, macdArr) {
  const gapThreshold = atr ? atr * GAP_FILTER : 0;
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

  // 有效笔判断
  const isValid = (a, b) => {
    const gap = b.mergedIdx - a.mergedIdx;
    if (gap < 4) return false;
    return countRaw(merged, a.mergedIdx, b.mergedIdx) >= 5;
  };

  // 笔内极值检查：一笔的顶/底必须是该笔范围内所有K线的最高/最低点。
  // 若 (a, b) 区间内存在比端点 b 更极端的合并K线（下跌笔的低点被跌破、
  // 上涨笔的高点被突破），则 b 不能作为笔端点，防止画出"笔内藏更极值"的非法笔。
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

  if (DEBUG) {
    const ft = (s) => `${s.type === "top" ? "顶" : "底"}@${s.mergedIdx}(${s.type === "top" ? s.high : s.low})`;
    console.log("[阶段一] 交替分型序列:", seq.map(ft).join(" → "));
  }

  // 阶段二：移除间隔不足的中间分型（回溯替换）
  const result = [];
  for (const k of seq) {
    if (result.length === 0) { result.push(k); continue; }
    const last = result[result.length - 1];
    if (k.type === last.type) {
      // 同类型取极值（理论上阶段一已交替，防御性处理）
      if (!last.gapLocked) {
        if (k.type === "top") { if (k.high >= last.high) result[result.length - 1] = k; }
        else { if (k.low <= last.low) result[result.length - 1] = k; }
      } else {
        // 跳空锁定的端点：仅当后续同类型分型「突破」锁定价格时才解锁替换
        // （跳空后走势继续创新高/新低，说明跳空笔应并入更大的趋势，符合"笔必须含极值"）；
        // 未突破则忽略（跳空笔独立保留）
        if (k.type === "top") { if (k.high > last.high) result[result.length - 1] = k; }
        else { if (k.low < last.low) result[result.length - 1] = k; }
      }
      continue;
    }
    // 异类型
    // MACD 变色成笔端点让位：若 result[-2] 是 MACD 变色成笔的端点，且当前分型 k 是
    // 更极端的同类型分型（如后续再创新低/新高），让位更新该端点并移除中间分型，
    // 保证 MACD 变色成笔的终点是区间内最新的绝对极值（例：92.83 应让位给更低的 92.74，
    // 对应 1小时 8-20 23:00 → 8-21 17:00 的时间修正）
    if (result.length >= 2) {
      const prev2 = result[result.length - 2];
      if (prev2.macdCross === true && prev2.type === k.type &&
          ((k.type === "top" && k.high > prev2.high) || (k.type === "bottom" && k.low < prev2.low))) {
        if (DEBUG) console.log(`[阶段二] MACD端点让位: ${prev2.type === "top" ? "顶" : "底"}@${prev2.mergedIdx}(${prev2.type === "top" ? prev2.high : prev2.low}) → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 更新为更极端${k.type === "top" ? "高点" : "低点"}，移除中间分型`);
        k.macdCross = true; // 继承标记，后续更极端的同类型分型仍可继续让位
        result[result.length - 2] = k;
        result.pop(); // 移除中间的异类型分型（如 92.83→94→92.74 中的小顶 94）
        continue;
      }
    }
    // 跳空优先：若 last→k 之间存在幅度 >= gap*ATR 的跳空缺口，则强制独立成笔，
    // 不受「最小间隔」「极值冲突」限制（跳空段无K线，缺口本身即一段独立走势）
    const hasGap = gapThreshold > 0 && hasGapBetween(merged, last.mergedIdx, k.mergedIdx, atr, GAP_FILTER);
    if (hasGap) {
      if (DEBUG) console.log(`[阶段二] 跳空成笔: ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx} → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx} (缺口≥${gapThreshold.toFixed(2)})`);
      k.gapLocked = true; // 锁定该端点，后续同类型分型不得替换，保证跳空独立成笔
      result.push(k);
      continue;
    }
    // 前顶/前底作废：prev2（result[-2]）与 k 同类型，且 prev2→last 不构成有效笔
    // （间隔不足，prev2 是脆弱端点），而 k 比 prev2 更极端（创新高/新低）时，
    // prev2 作为笔端点已被市场否定（缠论原则：顶被后续更高顶突破则前顶作废，
    // 底被后续更低底跌破则前底作废），应让 k 顶替 prev2 并移除中间的 last，
    // 使上涨/下跌笔延续到 k 所在位置。
    // 例：3分钟 8-12 11:18 顶(89.84) 被 12:00 顶(90.07) 突破，上涨笔应画到 90.07@12:00。
    // 附加约束（防止误伤真实顶/底）：
    //   1) last 必须不是极端点：last 不得比 prev3 更极端（否则 last 是深回调的真实转折）；
    //   2) 回调/反弹必须浅：pull/bounce < 上涨/下跌幅度的 50%（深回调意味着 prev2 是真实顶/底）；
    //   3) last 必须是「弱分型」：MACD 变色成笔且原始K线 < 5 根（不足 5 根的正常分型，
    //      是靠 MACD 变色凑数的微回调停顿，最容易被后续突破否定）。
    //      例：3分钟 8-12 11:30 底(MACD成笔 raw=4) 是弱分型，被 12:00 顶突破后应让 11:18 顶作废；
    //      而 1小时 7-22 23:00 底(MACD成笔 raw=7) 结构充足，7-22 17:00 顶是真实顶，不应作废。
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
        if (DEBUG) console.log(`[阶段二] 前顶/前底作废: ${prev2.type === "top" ? "顶" : "底"}@${prev2.mergedIdx}(${prev2.type === "top" ? prev2.high : prev2.low}) 被 ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 突破，k 顶替 prev2，移除中间 ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}`);
        if (prev2.macdCross === true) k.macdCross = true; // 继承 MACD 标记，后续更极端分型可继续让位
        result[result.length - 2] = k;
        result.pop();
        continue;
      }
    }
    if (isValid(last, k) && (noMoreExtremeInside(last, k) || last.gapLocked) && (fractalRangeClear(last, k) || last.gapLocked)) {
      result.push(k);
    } else if (isValid(last, k)) {
      // 间隔足够但区间内存在更极端的点 或 分型范围未脱离：
      // k 不能作为笔端点（笔内必须包含该笔的极值；底分型的底必须跌破顶分型范围），
      // 这种情况说明中间那段无法单独成笔，等待后续更极端的分型或并入更大的笔
      if (DEBUG) {
        const fr = fractalRangeClear(last, k);
        const ex = noMoreExtremeInside(last, k);
        console.log(`[阶段二] 忽略 k: ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 极值冲突=${!ex} 分型范围未脱离=${!fr}`);
      }
    } else {
      // 间隔不足：先检查 last→k 区间内是否发生 MACD 红绿转换。
      // 若发生（且 k 是该区间极值），则 MACD 变色本身代表一段动能切换，
      // 视为独立走势，允许间隔不足也成笔（保留极值规则，避免画出"笔内藏更极值"的非法笔）
      // 检测区间用分型的极值时间（last.time/k.time），而非合并K线最新时间，
      // 避免把顶底极值之后（合并K线包含区间内）的 MACD 变化误算进来
      const macdCross = macdArr && hasMacdCrossBetween(macdArr, merged, last.mergedIdx, k.mergedIdx, last.time, k.time);
      // MACD 变色成笔仍需满足最低K线数：覆盖原始K线至少 4 根（"4根线也可以成笔"），
      // 避免单根/两三根K线内部的小波动被 MACD 变色误触发成笔
      const macdRawCount = countRaw(merged, last.mergedIdx, k.mergedIdx);
      if (macdCross && macdRawCount >= 4 && noMoreExtremeInside(last, k)) {
        if (DEBUG) console.log(`[阶段二] MACD变色成笔: ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx} → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx} (区间内MACD红绿转换, 原始K线${macdRawCount}≥4)`);
        k.macdCross = true; // 标记该笔由 MACD 变色触发，仅用于结果展示
        k.macdRaw = macdRawCount; // 记录原始K线数，供「前顶/前底作废」判断弱分型（raw<5）
        result.push(k);
      } else {
        // 间隔不足且无 MACD 变色：中间分型 last 作废，k 回溯与 result[-2]（同类型）比较
        if (result.length >= 2 && result[result.length - 2].type === k.type) {
          const prev = result[result.length - 2];
          const moreExtreme = k.type === "top" ? k.high >= prev.high : k.low <= prev.low;
          // 只有当 prev→last 这笔是「脆弱笔」（合并间隔 <= 6）时，
          // 才允许 k 回溯替换 prev；否则 last 是坚实的分型，k 属于离 last 太近的噪音，忽略
          const gapPrevLast = last.mergedIdx - prev.mergedIdx;
          if (DEBUG) console.log(`[阶段二] 间隔不足: ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx} 与 ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}, 回溯比较同类型 prev, moreExtreme=${moreExtreme}, gapPrevLast=${gapPrevLast}`);
          // 仅当 prev→last 这笔是「脆弱笔」（合并间隔 <= 12）时，才允许 k 回溯替换 prev；
          // 否则 last 是坚实的分型，k 属于离 last 太近的噪音，忽略。
          // 例：1小时 7-3 20:00(71.3) → 7-6 15:00(71.02)，后续更低的 71.02 需能顶替 71.3，
          //     但 prev→last 间隔 11（横盘后破位）曾因 <=6 过严被拒，导致笔起点停在 7-3。
          // 缠论原则「顶被后续更高顶突破则前顶作废」：当 k 比 prev 更极端（创新高/新低）时，
          // 只要 prev 与 k 之间满足最小笔间隔（合并K线 >= 4），前顶/前底已被市场否定，
          // 应允许 k 顶替 prev，不受「脆弱笔」限制。
          // 例：15分钟 8-19 09:45(92.05) 顶在 17:00(92.38) 创新高，92.05 前顶作废，
          //     上涨笔应画到 92.38（否则 92.05→90.4 下跌笔内藏更高的 92.38，违反极值规则）。
          const gapPrevK = k.mergedIdx - prev.mergedIdx;
          if (moreExtreme && (gapPrevLast <= 12 || gapPrevK >= 4)) {
            result[result.length - 2] = k;
            result.pop(); // 删除作废的中间分型
          }
          // 若 k 不比 prev 更极端，或 prev→last 笔坚实，则忽略 k，保留 prev 与 last
        }
        // result.length < 2 时忽略 k（无法回溯）
      }
    }
  }

  if (DEBUG) {
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
      gapLocked: b.gapLocked === true, // 该笔终点由跳空成笔锁定，不参与未完成笔延伸
      macdCross: b.macdCross === true, // 该笔由 MACD 红绿转换触发成笔
    });
  }
  return bis;
}

/**
 * 4. 计算 ATR（14周期简单实现：平均真实波幅）
 */
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
 * 4.1 计算 MACD 柱（红绿柱）
 * 基于原始K线收盘价计算 EMA12/EMA26/DIF/DEA，返回每根原始K线对应的 MACD 柱值。
 * 约定：macd > 0 为红柱（多头动能），macd < 0 为绿柱（空头动能）。
 * 返回数组：[{ time, macd }, ...]，time 与原始K线一一对应。
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
  // DEA = EMA9(DIF)
  const dea = [];
  let prevDea = dif[0];
  dea.push(prevDea);
  for (let i = 1; i < dif.length; i++) {
    prevDea = dif[i] * (2 / (9 + 1)) + prevDea * (1 - (2 / (9 + 1)));
    dea.push(prevDea);
  }
  return rawBars.map((b, i) => ({ time: b.time, macd: (dif[i] - dea[i]) * 2 }));
}

/**
 * 4.2 检测两个分型（合并K线索引区间）之间是否发生 MACD 红绿转换。
 * 红变绿：MACD 柱由正转负（DIF 下穿 DEA）；绿变红：由负转正（DIF 上穿 DEA）。
 * 返回 true 表示区间内存在至少一次红绿转换。
 */
function hasMacdCrossBetween(macdArr, merged, aIdx, bIdx, aTime, bTime) {
  if (!macdArr || macdArr.length === 0) return false;
  // 检测区间用「分型的极值时间」作为边界（顶分型=最高价K线时间，底分型=最低价K线时间），
  // 而不是合并K线的最新时间——合并K线可能覆盖后续包含关系的K线，
  // 若用合并K线的 time（最新K线），会把「顶底极值之后」的 MACD 变化也算入区间，
  // 导致误触发变色成笔（例：15分钟 8-17 17:45 顶 → 18:30 底，底合并K线含 18:30~20:00 七根，
  // 19:30 的红变绿被误算入区间，实际 17:45~18:30 下跌段 MACD 始终为红，不应成笔）。
  // 未传 aTime/bTime 时回退到合并K线时间（保留原逻辑）。
  const t0 = aTime !== undefined ? aTime : merged[aIdx].time;
  const t1 = bTime !== undefined ? bTime : merged[bIdx].time;
  let prev = null;
  for (const m of macdArr) {
    if (m.time < t0) continue;
    if (m.time > t1) break;
    if (prev !== null) {
      // 由正转负：红变绿；由负转正：绿变红
      const crossed = (prev.macd >= 0 && m.macd < 0) || (prev.macd <= 0 && m.macd > 0);
      if (crossed) return true;
    }
    prev = m;
  }
  return false;
}

/**
 * 4.5 笔的颜色（按图表周期）
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
 * 4.6 周期的可见范围（intervalsVisibilities 精确范围）
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
 * 5. 买卖点识别（基于笔结构，无中枢简化版）
 * 一买：下跌笔创新低 + 力度背驰（相对前一下跌笔）
 * 二买：一买之后，回调下跌笔不破一买低点（底抬高）
 * 三买：上涨笔创新高（突破前顶）后，回调下跌笔不破该前顶
 * 为避免重复标记，每类买点取「最近」的一个
 */
function findBuyPoints(bis) {
  if (bis.length < 3) return [];

  // 记录所有下跌笔及其索引
  const downIdx = [];
  bis.forEach((b, i) => { if (b.type === "down") downIdx.push(i); });

  // 一买候选：下跌笔创新低 + 背驰
  // 力度参照笔：向前找「最近的有效下跌笔」——跳过幅度不足当前笔 50% 的次级别
  // 回调噪音笔（避免被中间小回调挡住力度比较），当前笔创新低且力度衰减即为一买。
  const firstBuys = [];
  for (let k = 1; k < downIdx.length; k++) {
    const cur = bis[downIdx[k]];
    let refer = null;
    for (let j = k - 1; j >= 0; j--) {
      const cand = bis[downIdx[j]];
      if (cand.span < cur.span * 0.5) continue; // 次级别回调笔，跳过
      refer = cand;
      break;
    }
    // 当前底比参照底更低（创新低），且力度衰减（幅度更小）
    if (refer && cur.endPrice < refer.endPrice && cur.span < refer.span) {
      firstBuys.push({ biIdx: downIdx[k], time: cur.endTime, price: cur.endPrice });
    }
  }

  // 只保留最近的一个一买
  const firstBuy = firstBuys.length > 0 ? firstBuys[firstBuys.length - 1] : null;
  const points = [];
  if (firstBuy) {
    points.push({ type: "1买", time: firstBuy.time, price: firstBuy.price });

    // 二买：一买之后，第一个回调下跌笔，其低点 > 一买低点
    let secondBuy = null;
    for (let i = firstBuy.biIdx + 1; i < bis.length; i++) {
      if (bis[i].type !== "down") continue;
      if (bis[i].endPrice > firstBuy.price) {
        secondBuy = { biIdx: i, time: bis[i].endTime, price: bis[i].endPrice };
        break;
      }
    }
    if (secondBuy) {
      points.push({ type: "2买", time: secondBuy.time, price: secondBuy.price });

      // 三买：二买之后，先有一段上涨笔创新高（突破二买前的顶），
      //       随后回调下跌笔低点仍高于该突破前顶
      let thirdBuy = null;
      for (let i = secondBuy.biIdx + 1; i < bis.length; i++) {
        if (bis[i].type !== "up") continue;
        const breakoutHigh = bis[i].endPrice;
        // 该上涨笔创新高（终点高于之前所有顶）
        let isBreakout = true;
        for (let j = 0; j < i; j++) {
          if (bis[j].type === "up" && bis[j].endPrice >= breakoutHigh) { isBreakout = false; break; }
        }
        if (!isBreakout) continue;
        // 找随后的回调下跌笔
        for (let m = i + 1; m < bis.length; m++) {
          if (bis[m].type !== "down") continue;
          if (bis[m].endPrice > breakoutHigh) {
            thirdBuy = { time: bis[m].endTime, price: bis[m].endPrice };
            break;
          }
          break; // 第一个下跌笔若不满足则不再往后找
        }
        if (thirdBuy) break;
      }
      if (thirdBuy) points.push({ type: "3买", time: thirdBuy.time, price: thirdBuy.price });
    }
  }

  return points;
}

/**
 * 5.1 卖点识别（基于笔结构，无中枢简化版，与买点对称）
 * 一卖：上涨笔创新高 + 力度背驰（相对前上涨笔，幅度衰减）
 * 二卖：一卖之后，回调上涨笔不破一卖高点（顶降低）
 * 三卖：下跌笔创新低（跌破前底）后，回调上涨笔不破该前底
 * 为避免重复标记，每类卖点取「最近」的一个
 */
function findSellPoints(bis) {
  if (bis.length < 3) return [];

  // 记录所有上涨笔及其索引
  const upIdx = [];
  bis.forEach((b, i) => { if (b.type === "up") upIdx.push(i); });

  // 一卖候选：上涨笔创新高 + 背驰
  // 力度参照笔：向前找「最近的有效上涨笔」——跳过幅度不足当前笔 50% 的次级别
  // 回调噪音笔（避免被中间小回调挡住力度比较），当前笔创新高且力度衰减即为一卖。
  const firstSells = [];
  for (let k = 1; k < upIdx.length; k++) {
    const cur = bis[upIdx[k]];
    let refer = null;
    for (let j = k - 1; j >= 0; j--) {
      const cand = bis[upIdx[j]];
      if (cand.span < cur.span * 0.5) continue; // 次级别回调笔，跳过
      refer = cand;
      break;
    }
    // 当前顶比参照顶更高（创新高），且力度衰减（幅度更小）
    if (refer && cur.endPrice > refer.endPrice && cur.span < refer.span) {
      firstSells.push({ biIdx: upIdx[k], time: cur.endTime, price: cur.endPrice });
    }
  }

  // 只保留最近的一个一卖
  const firstSell = firstSells.length > 0 ? firstSells[firstSells.length - 1] : null;
  const points = [];
  if (firstSell) {
    points.push({ type: "1卖", time: firstSell.time, price: firstSell.price });

    // 二卖：一卖之后，第一个回调上涨笔，其高点 < 一卖高点
    let secondSell = null;
    for (let i = firstSell.biIdx + 1; i < bis.length; i++) {
      if (bis[i].type !== "up") continue;
      if (bis[i].endPrice < firstSell.price) {
        secondSell = { biIdx: i, time: bis[i].endTime, price: bis[i].endPrice };
        break;
      }
    }
    if (secondSell) {
      points.push({ type: "2卖", time: secondSell.time, price: secondSell.price });

      // 三卖：二卖之后，先有一段下跌笔创新低（跌破二卖前的底），
      //       随后回调上涨笔高点仍低于该创新低前的底
      let thirdSell = null;
      for (let i = secondSell.biIdx + 1; i < bis.length; i++) {
        if (bis[i].type !== "down") continue;
        const breakoutLow = bis[i].endPrice;
        // 该下跌笔创新低（终点低于之前所有底）
        let isBreakout = true;
        for (let j = 0; j < i; j++) {
          if (bis[j].type === "down" && bis[j].endPrice <= breakoutLow) { isBreakout = false; break; }
        }
        if (!isBreakout) continue;
        // 找随后的回调上涨笔
        for (let m = i + 1; m < bis.length; m++) {
          if (bis[m].type !== "up") continue;
          if (bis[m].endPrice < breakoutLow) {
            thirdSell = { time: bis[m].endTime, price: bis[m].endPrice };
            break;
          }
          break; // 第一个上涨笔若不满足则不再往后找
        }
        if (thirdSell) break;
      }
      if (thirdSell) points.push({ type: "3卖", time: thirdSell.time, price: thirdSell.price });
    }
  }

  return points;
}

/**
 * 未完成笔延伸：缠论要求最新一笔延伸到当前K线。
 * 当最后一笔方向上的极端价出现在窗口末尾（当前笔终点之后，例如一波单调上涨/下跌
 * 无法形成新的顶/底分型）时，把最后一笔的终点推进到该极端价所在K线。
 * 只处理最后一笔，不影响已经定型的旧笔。
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
    // 最高点出现在当前笔终点之后才延伸（否则该笔已包含最高点）
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

// 逐级校准映射：每个周期用其「低一级」周期校准端点时间
//   15分钟 ← 3分钟；1小时 ← 15分钟；4小时 ← 1小时；日线 ← 4小时
// 3分钟及以下无更低级别，不校准
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
 * 大周期K线的时间戳是 bar 起点，其内部最高/最低点可能发生在更晚的低一级K线上
 * （例如 1小时 20:00 这根 bar 的最高点 94.71 实际出现在 15分钟 20:15）。
 * 每个周期用「低一级」周期K线校准：15分钟用3分钟校准，1小时用15分钟校准，
 * 4小时用1小时校准，日线用4小时校准。
 * 把本周期笔的端点时间校准到「低一级K线极值所在位置」，使不同周期对同一极值的
 * 标记位置在图上重合。
 */
function calibrateBiTimes(bis, bigBars, refBars, bigIntervalSec) {
  if (!bis || bis.length === 0 || !refBars || refBars.length === 0) return bis;
  const eps = 0.001;
  // 把单个端点时间校准到低一级周期基准：找到该端点所属大周期bar，
  // 在其覆盖的低一级K线中，取「价格达到该端点极值且时间最晚」的一根
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

    // 获取当前周期K线（校验K线时间间隔，确保数据已切换到目标周期）
    const intervalSecOf = (res) => {
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
      let bis = buildBi(fractals, merged, atr, macdArr);

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

    if (DRY) {
      console.log("\n[DRY RUN] 不绘图。");
    }

    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
  }
})();
