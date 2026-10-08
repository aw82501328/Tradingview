/**
 * trading-plan 参考周期震荡闸门单元测试（与 py_chain/test_range_gate.py 对齐；
 * 2026-10-08 rangeRes 可关闭 + Python/JS 强一致）
 *
 * 覆盖：
 *   - predictPlan(rangeGate=…)：regime 震荡 → 观望（strategy 注明来源周期）；
 *     非震荡 → 跳过自身 A/B 直接走趋势分支（替换语义）；insufficient → 观望·笔数据不足；
 *     无 rangeGate → 本周期自判 A/B（旧行为，rangeRes 关闭时的口径）
 *   - planGateRow：笔 <2 → insufficient；自身判震荡（A 支）→ range:true；强趋势 → range:false
 *   - TREND_RES / RANGE_RES 默认口径与 py_chain/trading_plan.py 常量一致
 *
 * 运行：node --test .cursor/skills/trading-plan/scripts/trading_plan.test.js
 */
const { describe, test } = require("node:test");
const assert = require("node:assert/strict");
const { predictPlan, planGateRow, RANGE_RES, TREND_RES } = require("./trading_plan.js");

const bi = (type, startTime, endTime, startPrice, endPrice) => ({
  type, startTime, endTime, startPrice, endPrice, span: Math.abs(endPrice - startPrice),
});
const bar = (time, open, high, low, close) => ({ time, open, high, low, close });

// ---- 素材（与 py_chain/test_range_gate.py 同构） ----
/** n 笔涨跌交替的小区间笔（端点都在 lo~hi 内）——isRangeBound A 支素材 */
const altBis = (t0, sec, n, lo, hi) => {
  const out = []; let t = t0, up = true;
  for (let i = 0; i < n; i++) {
    const [s, e] = up ? [lo, hi] : [hi, lo];
    out.push(bi(up ? "up" : "down", t, t + sec, s, e));
    t += sec; up = !up;
  }
  return out;
};
/** n 根小区间K线（高低都在 lo~hi 内） */
const flatBars = (t0, sec, n, lo, hi) => {
  const mid = (lo + hi) / 2;
  return Array.from({ length: n }, (_, i) => bar(t0 + i * sec, mid, hi, lo, mid));
};
/** 单边上行K线（每根递增 step，区间远超 rangeKMult×小 ATR） */
const trendBars = (t0, sec, n, p0, step) => {
  const out = []; let p = p0;
  for (let i = 0; i < n; i++) {
    out.push(bar(t0 + i * sec, p, p + step + 1, p - 1, p + step));
    p += step;
  }
  return out;
};
/** 单边上行笔序列（up 大步 / down 小回撤交替，不断创新高） */
const trendBis = (t0, sec, n, p0, step) => {
  const out = []; let t = t0, p = p0, up = true;
  for (let i = 0; i < n; i++) {
    if (up) { out.push(bi("up", t, t + sec, p, p + step)); p += step; }
    else { out.push(bi("down", t, t + sec, p, p - step / 4)); p -= step / 4; }
    t += sec; up = !up;
  }
  return out;
};

// 自身数据会判 A 支震荡的 15m 素材（altBis + flatBars + 大 ATR）
const SELF_RANGE = { bis: altBis(0, 900, 8, 4200, 4210), bars: flatBars(0, 900, 40, 4199, 4211), atr: 100 };

describe("predictPlan rangeGate（参考周期闸门替换语义）", () => {
  test("regime 震荡 → 观望并注明来源周期", () => {
    const row = predictPlan({ res: "15", bis: SELF_RANGE.bis, bars: SELF_RANGE.bars,
      atr: SELF_RANGE.atr, lastPrice: 4205,
      rangeGate: { range: true, resName: "4小时", reason: "4小时：最近40根K线区间…判定为震荡整理" } });
    assert.equal(row.direction, "观望");
    assert.match(row.strategy, /震荡整理（4小时）/);
    assert.equal(row.label, "震荡观望");
  });

  test("regime 非震荡 → 跳过自身 A/B 走趋势分支（自身数据本会判震荡）", () => {
    const row = predictPlan({ res: "15", bis: SELF_RANGE.bis, bars: SELF_RANGE.bars,
      atr: SELF_RANGE.atr, lastPrice: 4205,
      rangeGate: { range: false, resName: "4小时", reason: "" } });
    assert.notEqual(row.label, "震荡观望");
    assert.ok(!row.strategy.includes("震荡整理"));
  });

  test("regime 笔不足 → 观望·笔数据不足", () => {
    const row = predictPlan({ res: "15", bis: SELF_RANGE.bis, bars: SELF_RANGE.bars,
      atr: SELF_RANGE.atr, lastPrice: 4205,
      rangeGate: { range: true, insufficient: true, resName: "4小时",
                   reason: "4小时笔数据不足（少于2笔），无法判定震荡/趋势，观望" } });
    assert.equal(row.direction, "观望");
    assert.match(row.strategy, /4小时笔数据不足，观望/);
    assert.equal(row.label, "数据不足");
  });

  test("无 rangeGate → 本周期自判 A/B（旧行为；rangeRes 关闭时的口径）", () => {
    const row = predictPlan({ res: "15", bis: SELF_RANGE.bis, bars: SELF_RANGE.bars,
      atr: SELF_RANGE.atr, lastPrice: 4205 });
    assert.equal(row.label, "震荡观望");
    assert.match(row.strategy, /震荡整理，观望等待方向选择/);
  });
});

describe("planGateRow（参考周期 regime）", () => {
  test("笔 <2 → insufficient", () => {
    const g = planGateRow("240", altBis(0, 14400, 1, 4100, 4110),
                          flatBars(0, 14400, 40, 4099, 4111), 100, null, [], null);
    assert.equal(g.range, true);
    assert.equal(g.insufficient, true);
    assert.match(g.reason, /4小时笔数据不足/);
  });

  test("自身判震荡（A 支）→ range:true", () => {
    const g = planGateRow("240", altBis(0, 14400, 6, 4100, 4110),
                          flatBars(0, 14400, 40, 4099, 4111), 100, null, [], 4105);
    assert.equal(g.range, true);
    assert.ok(!g.insufficient);
    assert.equal(g.resName, "4小时");
  });

  test("强趋势 → range:false", () => {
    const g = planGateRow("240", trendBis(0, 14400, 6, 5000, 100),
                          trendBars(0, 14400, 40, 5000, 25), 1, null, [], null);
    assert.equal(g.range, false);
  });
});

describe("默认口径（与 py_chain/trading_plan.py 常量一致）", () => {
  test("TREND_RES / RANGE_RES 默认 240（闸门开启，强一致默认）", () => {
    assert.equal(TREND_RES, "240");
    assert.equal(RANGE_RES, "240");
  });
});
