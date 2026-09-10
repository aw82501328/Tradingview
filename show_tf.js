const fs = require('fs');
const data = JSON.parse(fs.readFileSync('bars_all_tf.json', 'utf8'));

const mergeBars = (rawBars) => {
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
};
const findFractals = (merged) => {
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
};
const countRaw = (merged, startIdx, endIdx) => {
  let t = 0;
  for (let k = startIdx + 1; k <= endIdx; k++) t += merged[k]._rawCount;
  return t;
};
const calcMACD = (rawBars) => {
  const n = rawBars.length;
  if (n < 26) return [];
  const ema = (arr, p) => {
    const out = [arr[0]];
    const k = 2 / (p + 1);
    for (let i = 1; i < arr.length; i++) out.push(arr[i] * k + out[i - 1] * (1 - k));
    return out;
  };
  const closes = rawBars.map(b => b.close);
  const e12 = ema(closes, 12), e26 = ema(closes, 26);
  const dif = e12.map((v, i) => v - e26[i]);
  const dea = ema(dif, 9);
  return rawBars.map((b, i) => ({ time: b.time, macd: (dif[i] - dea[i]) * 2 }));
};
const hasMacdCrossBetween = (macdArr, merged, aIdx, bIdx, aTime, bTime) => {
  let sign = null;
  for (const m of macdArr) {
    if (m.time < aTime) continue;
    if (m.time > bTime) break;
    const s = m.macd > 0 ? 1 : m.macd < 0 ? -1 : 0;
    if (s === 0) continue;
    if (sign === null) { sign = s; continue; }
    if (s !== sign) return true;
  }
  return false;
};
const hasGapBetween = (merged, aIdx, bIdx, atr, gapFilter) => {
  const th = atr * gapFilter;
  for (let i = aIdx; i < bIdx; i++) {
    const cur = merged[i], next = merged[i + 1];
    const gapUp = next.low - cur.high;
    const gapDown = cur.low - next.high;
    if (gapUp >= th || gapDown >= th) return true;
  }
  return false;
};
const calcATR = (rawBars, period = 14) => {
  const trs = [];
  for (let i = 1; i < rawBars.length; i++) {
    const h = rawBars[i].high, l = rawBars[i].low, pc = rawBars[i - 1].close;
    trs.push(Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc)));
  }
  const start = Math.max(0, trs.length - period);
  const slice = trs.slice(start);
  if (slice.length === 0) return 0;
  return slice.reduce((a, b) => a + b, 0) / slice.length;
};
const GAP_FILTER = 1.0;
const fmt = (ts) => {
  const dt = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${dt.getMonth()+1}-${dt.getDate()} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
};

function compute(bars, newRule) {
  const merged = mergeBars(bars);
  const fractals = findFractals(merged);
  const atr = calcATR(bars, 14);
  const macdArr = calcMACD(bars);
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
    if (newRule && result.length >= 2) {
      const prev2 = result[result.length - 2];
      if (prev2.type === k.type &&
          !isValid(prev2, last) &&
          ((k.type === "top" && k.high > prev2.high) || (k.type === "bottom" && k.low < prev2.low))) {
        if (prev2.macdCross === true) k.macdCross = true;
        result[result.length - 2] = k;
        result.pop();
        continue;
      }
    }
    if (isValid(last, k) && (noMoreExtremeInside(last, k) || last.gapLocked)) {
      result.push(k);
    } else if (isValid(last, k)) {
    } else {
      const macdCross = macdArr && hasMacdCrossBetween(macdArr, merged, last.mergedIdx, k.mergedIdx, last.time, k.time);
      const macdRawCount = countRaw(merged, last.mergedIdx, k.mergedIdx);
      if (macdCross && macdRawCount >= 4 && noMoreExtremeInside(last, k)) {
        k.macdCross = true;
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
    bis.push({ type: isUp ? "up" : "down", s: `${fmt(a.time)}:${startPrice}`, e: `${fmt(b.time)}:${endPrice}` });
  }
  return bis;
}

const res = process.argv[2] || "60";
const bars = data[res];
const oldBis = compute(bars, false);
const newBis = compute(bars, true);
console.log(`[${res}] OLD=${oldBis.length} NEW=${newBis.length}`);
console.log("=== OLD ===");
oldBis.forEach((b, i) => console.log(`  ${i}: ${b.type === "up" ? "UP  " : "DOWN"} ${b.s} -> ${b.e}`));
console.log("=== NEW ===");
newBis.forEach((b, i) => console.log(`  ${i}: ${b.type === "up" ? "UP  " : "DOWN"} ${b.s} -> ${b.e}`));
