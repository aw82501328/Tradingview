/**
 * trading-plan 震荡 regime 单元测试（与 py_chain/test_range_gate.py 对齐；
 * 2026-10-16 起交易计划只剩震荡/顺势两项判定——策略行/strategyOf/classifySecond/
 * predictPlan 已删除，映射用例移至 mark_entry.test.js 的 pointStrategyOf）
 *
 * 覆盖：
 *   - rangeGateRow：笔 <2 → insufficient；自身判震荡（A 支）→ range:true；强趋势 → range:false
 *   - computeRegime：关闭 → null；参考周期震荡/强趋势/笔不足三态
 *   - TREND_RES / RANGE_RES 默认口径与 py_chain/trading_plan.py 常量一致
 *
 * 运行：node --test .cursor/skills/trading-plan/scripts/trading_plan.test.js
 */
const { describe, test } = require("node:test");
const assert = require("node:assert/strict");
const { rangeGateRow, computeRegime, RANGE_RES, TREND_RES } = require("./trading_plan.js");

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

const PERIODS = ["D", "240", "60", "15", "3"];
const buildPeriods = ({ bis240, bars240 }) => ({
  periodBis: { D: altBis(0, 86400, 4, 4000, 4100), 240: bis240, 60: [], 15: [], 3: [] },
  barsByPeriod: { D: flatBars(0, 86400, 40, 3999, 4101), 240: bars240, 60: [], 15: [], 3: [] },
});

describe("rangeGateRow（参考周期 regime）", () => {
  test("笔 <2 → insufficient", () => {
    const g = rangeGateRow("240", altBis(0, 14400, 1, 4100, 4110),
                          flatBars(0, 14400, 40, 4099, 4111), 100, null, 4105);
    assert.equal(g.range, true);
    assert.equal(g.insufficient, true);
    assert.match(g.reason, /4小时笔数据不足/);
  });

  test("自身判震荡（A 支）→ range:true", () => {
    const g = rangeGateRow("240", altBis(0, 14400, 6, 4100, 4110),
                          flatBars(0, 14400, 40, 4099, 4111), 100, null, 4105);
    assert.equal(g.range, true);
    assert.ok(!g.insufficient);
    assert.equal(g.resName, "4小时");
  });

  test("强趋势 → range:false", () => {
    const g = rangeGateRow("240", trendBis(0, 14400, 6, 5000, 100),
                          trendBars(0, 14400, 40, 5000, 25), 1, null, null);
    assert.equal(g.range, false);
  });
});

describe("computeRegime（震荡 regime 唯一入口；2026-10-16 从计划行抽出）", () => {
  test("关闭（range_res 空/off）→ null", () => {
    const { periodBis, barsByPeriod } = buildPeriods(
      { bis240: altBis(0, 14400, 6, 4100, 4110), bars240: flatBars(0, 14400, 40, 4099, 4111) });
    assert.equal(computeRegime(periodBis, barsByPeriod, PERIODS, ""), null);
    assert.equal(computeRegime(periodBis, barsByPeriod, PERIODS, "off"), null);
  });

  test("参考周期震荡 → {range:true} 且 reason 注明判定", () => {
    const { periodBis, barsByPeriod } = buildPeriods(
      { bis240: altBis(0, 14400, 6, 4100, 4110), bars240: flatBars(0, 14400, 40, 4099, 4111) });
    const g = computeRegime(periodBis, barsByPeriod, PERIODS, "240");
    assert.equal(g.range, true);
    assert.ok(!g.insufficient);
    assert.equal(g.resName, "4小时");
    assert.match(g.reason, /判定为震荡整理/);
  });

  test("参考周期强趋势 → {range:false}", () => {
    const { periodBis, barsByPeriod } = buildPeriods(
      { bis240: trendBis(0, 14400, 6, 5000, 100), bars240: trendBars(0, 14400, 40, 5000, 25) });
    const g = computeRegime(periodBis, barsByPeriod, PERIODS, "240");
    assert.equal(g.range, false);
  });

  test("参考周期笔不足（<2 或无数据）→ insufficient", () => {
    const thin = buildPeriods(
      { bis240: altBis(0, 14400, 1, 4100, 4110), bars240: flatBars(0, 14400, 40, 4099, 4111) });
    const g = computeRegime(thin.periodBis, thin.barsByPeriod, PERIODS, "240");
    assert.equal(g.range, true);
    assert.equal(g.insufficient, true);
    const none = buildPeriods({ bis240: [], bars240: [] });
    const g2 = computeRegime(none.periodBis, none.barsByPeriod, PERIODS, "240");
    assert.equal(g2.range, true);
    assert.equal(g2.insufficient, true);
  });
});

describe("默认口径（与 py_chain/trading_plan.py 常量一致）", () => {
  test("TREND_RES / RANGE_RES 默认关闭（与 py_chain 常量一致）", () => {
    assert.equal(TREND_RES, "");
    assert.equal(RANGE_RES, "");
  });
});
