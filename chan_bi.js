/**
 * 缠论画笔脚本
 * 在 TradingView Desktop 图表上按缠论理论画笔（笔 + 一买/二买/三买）
 *
 * 用法：
 *   node chan_bi.js --dry   只计算并打印笔和买点，不绘图
 *   node chan_bi.js         计算并绘制到图表
 *
 * 参数：
 *   --bars=200   取最近 N 根K线（默认 200）
 *   --atr=0.5    ATR 过滤系数（幅度 < atr*ATR 的笔剔除，默认 0.5）
 */
const CDP = require("E:/AI_Projects/TRADINGVIEW/server-cdp/node_modules/chrome-remote-interface");

// 解析命令行参数
const args = process.argv.slice(2);
const DRY = args.includes("--dry");
const getArg = (name, def) => {
  const a = args.find(x => x.startsWith("--" + name + "="));
  return a ? parseFloat(a.split("=")[1]) : def;
};
const N_BARS = getArg("bars", 200);
const ATR_FILTER = getArg("atr", 0.5);
const DEBUG = args.includes("--debug");

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

function buildBi(fractals, merged) {
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
      if (k.type === "top") { if (k.high >= last.high) result[result.length - 1] = k; }
      else { if (k.low <= last.low) result[result.length - 1] = k; }
      continue;
    }
    // 异类型
    if (isValid(last, k)) {
      result.push(k);
    } else {
      // 间隔不足：中间分型 last 作废，k 回溯与 result[-2]（同类型）比较
      if (result.length >= 2 && result[result.length - 2].type === k.type) {
        const prev = result[result.length - 2];
        const moreExtreme = k.type === "top" ? k.high >= prev.high : k.low <= prev.low;
        // 只有当 prev→last 这笔是「脆弱笔」（间隔刚好为最小间隔 4）时，
        // 才允许 k 回溯替换 prev；否则 last 是坚实的分型，k 属于离 last 太近的噪音，忽略
        const gapPrevLast = last.mergedIdx - prev.mergedIdx;
        if (DEBUG) console.log(`[阶段二] 间隔不足: ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx} 与 ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}, 回溯比较同类型 prev, moreExtreme=${moreExtreme}, gapPrevLast=${gapPrevLast}`);
        if (moreExtreme && gapPrevLast <= 4) {
          result[result.length - 2] = k;
          result.pop(); // 删除作废的中间分型
        }
        // 若 k 不比 prev 更极端，或 prev→last 笔坚实，则忽略 k，保留 prev 与 last
      }
      // result.length < 2 时忽略 k（无法回溯）
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
  const firstBuys = [];
  for (let k = 1; k < downIdx.length; k++) {
    const cur = bis[downIdx[k]];
    const prev = bis[downIdx[k - 1]];
    // 当前底比前一底更低（创新低），且力度衰减（幅度更小）
    if (cur.endPrice < prev.endPrice && cur.span < prev.span) {
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

    // 获取K线数据
    const dataRes = await client.Runtime.evaluate({
      expression: `(function() {
        const chart = TradingViewApi.activeChart();
        if (!chart) return { error: 'no_chart' };
        const ms = chart.chartModel().mainSeries();
        const items = ms.data().m_bars._items;
        if (!items || items.length === 0) return { error: 'no_items' };
        const bars = items.map(i => {
          const v = i.value;
          return { time: v[0], open: v[1], high: v[2], low: v[3], close: v[4], volume: v[5] };
        });
        return { bars, symbol: chart.symbol(), resolution: String(chart.resolution()) };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    const d = dataRes.result.value;
    if (d.error) { console.log("ERROR:", d.error); process.exit(1); }

    const allBars = d.bars;
    const rawBars = allBars.slice(-N_BARS);

    // 缠论计算
    const merged = mergeBars(rawBars);
    const fractals = findFractals(merged);
    global.__debug = DRY;
    let bis = buildBi(fractals, merged);

    // ATR 过滤
    const atr = calcATR(rawBars, 14);
    const threshold = atr * ATR_FILTER;
    const beforeFilter = bis.length;
    bis = bis.filter(b => b.span >= threshold);
    const filteredOut = beforeFilter - bis.length;

    const buyPoints = findBuyPoints(bis);

    console.log("=== 缠论计算结果 ===");
    console.log("品种:", d.symbol, "周期:", d.resolution);
    console.log("原始K线:", rawBars.length, "合并后:", merged.length, "分型:", fractals.length);
    console.log("ATR:", atr.toFixed(4), "过滤阈值(0.5*ATR):", threshold.toFixed(4));
    console.log("笔数量:", bis.length, "(过滤掉", filteredOut, "根噪音小笔)");
    console.log("\n--- 笔列表 ---");
    const toT = (ts) => {
      const dt = new Date(ts * 1000);
      const p = (n) => String(n).padStart(2, '0');
      return `${dt.getMonth()+1}-${dt.getDate()} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
    };
    bis.forEach((b, i) => {
      const dir = b.type === "up" ? "上涨" : "下跌";
      console.log(
        `笔${i + 1} [${dir}] ${b.startPrice}(${toT(b.startTime)}) -> ${b.endPrice}(${toT(b.endTime)}) | 幅度 ${b.span.toFixed(2)} | 原始K线 ${b.rawCount}`
      );
    });
    console.log("\n--- 买点列表 ---");
    buyPoints.forEach(p => console.log(`  ${p.type} @ ${p.price} (time ${p.time})`));

    if (DRY) {
      console.log("\n[DRY RUN] 不绘图。");
      await client.close();
      return;
    }

    // ============================================================
    // 绘制
    // ============================================================
    const drawRes = await client.Runtime.evaluate({
      expression: `(async function() {
        const chart = TradingViewApi.activeChart();
        const BIS = ${JSON.stringify(bis)};
        const POINTS = ${JSON.stringify(buyPoints)};
        const out = { bi_ok: 0, bi_err: [], point_ok: 0, point_err: [], cleared: 0 };
        const created = [];

        // 清除本脚本之前画的线（polyline）和买点箭头（arrow_up），避免重叠
        try {
          const shapes = chart.getAllShapes();
          for (const s of shapes) {
            if (s.name === 'polyline' || s.name === 'arrow_up') {
              try { chart.removeEntity(s.id); out.cleared++; } catch(e) {}
            }
          }
        } catch(e) {}

        // 画笔（线段）：统一紫色
        for (const b of BIS) {
          try {
            const id = await chart.createMultipointShape(
              [{ time: b.startTime, price: b.startPrice }, { time: b.endTime, price: b.endPrice }],
              { shape: 'polyline', lock: true, overrides: { linecolor: '#8A2BE2', linewidth: 1 } }
            );
            created.push(id);
            out.bi_ok++;
          } catch(e) { out.bi_err.push(e.message); }
        }

        // 画买点（箭头 + 文字）
        for (const p of POINTS) {
          try {
            const id = await chart.createShape(
              { time: p.time, price: p.price },
              { shape: 'arrow_up', text: p.type, lock: true }
            );
            created.push(id);
            out.point_ok++;
          } catch(e) { out.point_err.push(e.message); }
        }

        return { ...out, created_ids: created };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 30000,
    });

    console.log("\n=== 绘制结果 ===");
    console.log(JSON.stringify(drawRes.result.value, null, 2));

    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
  }
})();
