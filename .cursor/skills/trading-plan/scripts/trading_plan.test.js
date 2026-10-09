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
const { predictPlan, planGateRow, RANGE_RES, TREND_RES, strategyOf, classifySecond } = require("./trading_plan.js");

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

// ---- 弱档原始点分流（2026-10-09，与 py_chain/test_plan_strategy.py 对齐） ----
describe("classifySecond 弱档原始点分流", () => {
  const P = { type: "2买", time: 20, price: 95 };
  // 2买 @20@95；前高 120；after 涨 104 距前高 16、回调 101 距点 6 → 弱档素材
  const weakBis = [
    bi("up", 0, 10, 100, 120), bi("down", 10, 20, 120, 95),
    bi("up", 20, 30, 95, 104), bi("down", 30, 40, 104, 101),
  ];

  test("默认开：反弹高点未过原始点 → 未过原始点", () => {
    assert.equal(classifySecond(weakBis, [], P), "未过原始点");
  });

  test("weakTierByOrigin=false → 旧「其他」", () => {
    assert.equal(classifySecond(weakBis, [], P, { weakTierByOrigin: false }), "其他");
  });

  test("原始点=回溯中第一个支配其后所有反弹高的更早顶（130 而非最近顶 110）", () => {
    const bis = [
      bi("up", 0, 10, 100, 130), bi("down", 10, 20, 130, 100),
      bi("up", 20, 30, 100, 110), bi("down", 30, 40, 110, 95),
      bi("up", 40, 50, 95, 104),
    ];
    assert.equal(classifySecond(bis, [], { type: "2买", time: 40, price: 95 }), "未过原始点");
  });

  test("后续反弹顶越过原始点 → 翻回「其他」", () => {
    const bis = weakBis.concat([bi("up", 40, 50, 101, 125)]);
    assert.equal(classifySecond(bis, [], P), "其他");
  });

  test("3买 不产生哨兵（分流仅 2买/类2买/2卖/类2卖）", () => {
    assert.equal(classifySecond(weakBis, [], { type: "3买", time: 20, price: 95 }), "其他");
  });

  test("卖侧镜像：点后低点未跌破上涨原始点 → 未过原始底（够笔→作废转弱二买）", () => {
    const bis = [
      bi("down", 0, 10, 120, 100), bi("up", 10, 20, 100, 125),
      bi("down", 20, 30, 125, 108), bi("up", 30, 40, 108, 112),
    ];
    const forming = [
      bi("down", 0, 10, 120, 100), bi("up", 10, 20, 100, 125),
      { ...bi("down", 20, 30, 125, 108), _forming: true, mergedCount: 3 },
    ];
    const p = { type: "2卖", time: 20, price: 125 };
    assert.equal(classifySecond(bis, [], p), "未过原始底够笔");   // after 已确认=够笔→作废
    assert.equal(classifySecond(forming, [], p), "未过原始底");   // 形成中<5块=未够笔→等2卖
    assert.equal(classifySecond(bis, [], p, { weakTierByOrigin: false }), "其他");
    const out = strategyOf("60", "类2卖", "", "", "未过原始底够笔");
    assert.equal(out.direction, "空头多");
    assert.equal(out.strategy, "等待低点附近的2买");
  });
});

describe("strategyOf 弱档分流档位（2026-10-09）", () => {
  test("未过原始点 → 空头多/等待低点附近的2买（2买与类2买同）", () => {
    for (const t of ["2买", "类2买"]) {
      const out = strategyOf("60", t, "", `趋势|${t}`, "未过原始点");
      assert.equal(out.direction, "空头多");
      assert.equal(out.strategy, "等待低点附近的2买");
    }
  });

  test("未过原始底 → 多头空/等待高点附近的2卖", () => {
    for (const t of ["2卖", "类2卖"]) {
      const out = strategyOf("60", t, "", `趋势|${t}`, "未过原始底");
      assert.equal(out.direction, "多头空");
      assert.equal(out.strategy, "等待高点附近的2卖");
    }
  });

  test("过了原始点（其他）→ 旧弱档不变", () => {
    assert.equal(strategyOf("60", "2买", "", "", "其他").strategy, "等待高点附近的一卖");
    assert.equal(strategyOf("60", "2卖", "", "", "其他").strategy, "等待低点附近的一买");
  });
});
