/**
 * 导出对齐基准（离线）：用 chan_core 复现 chan-bi + mark-buy-sell 的完整计算，
 * 把「共同输入(原始K线/校准基准K线) + JS 计算结果(笔/买卖点)」导出为 baseline.json，
 * 供 Python 端 chan_core.py 独立计算后对比。
 *
 * 数据来源：bars_all_tf.json（各周期 FROM 之后的K线，无缓冲）。
 * 说明：这里不复现 30 根缓冲窗口（bars_all_tf.json 已是 FROM 之后），
 *       对齐测试验证的是「同一份输入下 JS vs Python 逐函数等价」。
 */
const fs = require("fs");
const core = require("./.cursor/skills/chan-core/scripts/chan_core.js");
const {
  mergeBars, findFractals, buildBi, lockedPivotsOf, alignBiToUpper, calcATR, calcMACD,
  extendLastBi, lowerResOf, calibrateBiTimes, intervalSecOf,
  findBuyPoints, findSellPoints, anchorFirstBuy, snapToOwnBar,
} = core;

const FROM_TS = Math.floor(Date.UTC(2026, 6, 2) / 1000); // 2026-07-02 UTC
const PERIODS = ["D", "240", "60", "15", "3"];
const ATR_FILTER = 0.5;
core.CHAN_CFG.gapFilter = 1.0;
core.CHAN_CFG.debug = false;

const barsAll = JSON.parse(fs.readFileSync("./bars_all_tf.json", "utf8"));

// ---------- 复现 chan_bi：算笔 ----------
const allBis = {};
const rawBarsByRes = {};
const refBarsByRes = {};
let prevBis = null;
for (let i = 0; i < PERIODS.length; i++) {
  const res = PERIODS[i];
  const rawBars = barsAll[res]; // 无缓冲，FROM 之后
  rawBarsByRes[res] = rawBars;
  const merged = mergeBars(rawBars);
  const fractals = findFractals(merged);
  const atr = calcATR(rawBars, 14);
  const macdArr = calcMACD(rawBars);
  // 区间套强制对齐：把上一层笔端点作为锁定端点传入 buildBi
  const lockedPivots = lockedPivotsOf(prevBis);
  let bis = buildBi(fractals, merged, atr, macdArr, lockedPivots);
  const threshold = atr * ATR_FILTER;
  bis = bis.filter(b => b.span >= threshold);
  let drawBis = bis; // 无 anchor 过滤（bars_all_tf.json 已 FROM 之后）
  drawBis = extendLastBi(drawBis, rawBars);
  const lowerRes = lowerResOf(res);
  if (lowerRes && barsAll[lowerRes]) {
    const refBars = barsAll[lowerRes];
    refBarsByRes[res] = refBars;
    drawBis = calibrateBiTimes(drawBis, rawBars, refBars, intervalSecOf(res));
  }
  // 区间套强制对齐：把本级别笔拐点对齐到紧邻上级笔拐点
  if (prevBis && prevBis.length > 0) {
    drawBis = alignBiToUpper(drawBis, prevBis, intervalSecOf(PERIODS[i - 1]));
  }
  allBis[res] = drawBis;
  prevBis = drawBis;
}

// ---------- 复现 mark_buy_sell：算买卖点 ----------
let upperBis = null;
const buySellByRes = {};
const marksByRes = {};
const periodMarks = {};
const RED = '#F23645', GREEN = '#089981';
const upperClassRe = /^([123]买|[123]卖|类2买|类2卖)$/;
for (let pi = 0; pi < PERIODS.length; pi++) {
  const res = PERIODS[pi];
  const curBis = allBis[res].filter(b => b.endTime >= FROM_TS);
  const rawBars = rawBarsByRes[res];
  const atr = calcATR(rawBars, 14);
  const macdArr = calcMACD(rawBars);
  let buyPts = findBuyPoints(curBis, upperBis, macdArr, intervalSecOf(res));
  let sellPts = findSellPoints(curBis, upperBis, macdArr, intervalSecOf(res));

  // 一买锚定（除日线外）
  let anchoredBuyPts = buyPts;
  const firstBuyPts = buyPts.filter(p => p.type === "1买");
  if (firstBuyPts.length > 0 && PERIODS.indexOf(res) > 0) {
    const anchoredMarks = [];
    const seenMarkPos = new Set();
    for (const cand of firstBuyPts) {
      const anchored = anchorFirstBuy(cand, upperBis);
      if (anchored) {
        const snapTime = snapToOwnBar(anchored.price, anchored.time, rawBars);
        if (seenMarkPos.has(snapTime)) continue;
        seenMarkPos.add(snapTime);
        anchoredMarks.push({ type: "1买", time: snapTime, price: anchored.price });
      }
    }
    anchoredBuyPts = [...buyPts.filter(p => p.type !== "1买"), ...anchoredMarks];
  }

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

  // 跨周期共振：本级别 1买/1卖 若与紧邻上级买卖点同点位 → 绿色
  const upperRes = pi > 0 ? PERIODS[pi - 1] : null;
  const upperMarks = upperRes ? periodMarks[upperRes] : null;
  for (const mk of marks) {
    mk.color = RED;
    if ((mk.label === '1买' || mk.label === '1卖') && upperMarks) {
      for (const um of upperMarks) {
        if (!upperClassRe.test(um.label)) continue;
        if (Math.abs(mk.rawTime - um.rawTime) <= 60 && Math.abs(mk.rawPrice - um.rawPrice) <= Math.max(atr * 0.2, 0.05)) {
          mk.color = GREEN;
          break;
        }
      }
    }
  }

  buySellByRes[res] = { buyPts: anchoredBuyPts, sellPts };
  marksByRes[res] = marks;
  periodMarks[res] = marks;
  upperBis = curBis;
}

const out = {
  fromTs: FROM_TS,
  rawBars: rawBarsByRes,
  refBars: refBarsByRes,
  bis: allBis,
  buySell: buySellByRes,
  marks: marksByRes,
};
fs.writeFileSync("./baseline.json", JSON.stringify(out, null, 2));

let totalBis = 0, totalBuy = 0, totalSell = 0;
for (const res of PERIODS) {
  totalBis += allBis[res].length;
  totalBuy += buySellByRes[res].buyPts.length;
  totalSell += buySellByRes[res].sellPts.length;
  console.log(res, "bis=" + allBis[res].length, "buy=" + buySellByRes[res].buyPts.length, "sell=" + buySellByRes[res].sellPts.length);
}
console.log("TOTAL bis=" + totalBis, "buy=" + totalBuy, "sell=" + totalSell);
console.log("saved baseline.json");
