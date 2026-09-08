/**
 * mark-entry 区间套下沉判定单元测试（与 py_chain/test_mark_entry_sink.py 对齐）
 *
 * 依据 SPEC_divergence_chanset.md 规则 1/2/3 与 .cursor/skills/mark-entry/SPEC.md §2.2。
 * 数据为手工构造、可独立验算的最小示例（确认制 sinkChainConfirm 路径）。
 *
 * 运行：node --test .cursor/skills/mark-entry/scripts/mark_entry.test.js
 */
const { describe, test } = require("node:test");
const assert = require("node:assert/strict");
const {
  findDivergePoints, lowerDiverge, levelsBelow, sinkChainConfirm,
} = require("./mark_entry.js");

const bi = (type, startTime, endTime, startPrice, endPrice) => ({
  type, startTime, endTime, startPrice, endPrice, span: Math.abs(endPrice - startPrice),
});

const macd = (tv) => tv.map(([t, m, d]) => ({ time: t, macd: m, dif: d, dea: 0 }));

// 时间基准（秒）：与 py 用例同构。8-31 00:00=0 → 10:00=36000、19:00=68400、
// 19:15=68700、21:45=77700、9-1 04:00=100800、07:15=112500、08:15=116100
const B60_DOWN_BEFORE = bi("down", 0, 36000, 4500, 4400);
const B60_UP = bi("up", 36000, 82800, 4400, 4461.7); // 60m 末段（终点即 P）
const B15 = [
  bi("down", 0, 35500, 4500, 4400),
  bi("up", 35500, 50100, 4400, 4440.0),
  bi("down", 50100, 60300, 4440.0, 4420.0),
  bi("up", 60300, 70200, 4420.0, 4455.93), // #3（参照）
  bi("down", 70200, 78300, 4455.93, 4430.0),
  bi("up", 78300, 82800, 4430.0, 4461.7),  // 末段（终点即 P）
];
const B3 = [
  bi("down", 0, 78200, 4500, 4430.0),
  bi("up", 78200, 82800, 4430.0, 4461.7),
];
const B3_RICH = [
  bi("down", 0, 60000, 4500, 4420.0),
  bi("up", 60000, 70000, 4420.0, 4455.0),
  bi("down", 70000, 78200, 4455.0, 4430.0),
  bi("up", 78200, 82800, 4430.0, 4461.7),
];
const pd = (bis, macdArr) => ({ bis, macdArr: macdArr || [] });

describe("levelsBelow 连续低级别链", () => {
  test("缺 15 时链截断于 60（不跳级到 3）", () => {
    const data = { "60": pd(B15), "3": pd(B3_RICH) };
    assert.deepEqual(levelsBelow(data, "60"), []);
  });

  test("全级别在链（60→15→3）", () => {
    const data = { "60": pd(B15), "15": pd(B15), "3": pd(B3_RICH) };
    assert.deepEqual(levelsBelow(data, "60"), ["15", "3"]);
  });
});

describe("sinkChainConfirm 下沉链", () => {
  test("案例A：60 笔内 15 五笔展开 → 下沉 15；15 末段内 3 仅 1 笔 → 停 15", () => {
    const data = { "60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(B15), "3": pd(B3) };
    const [stop, parent] = sinkChainConfirm(data, "60", 82800, "short");
    assert.equal(stop, "15");
    assert.equal(parent.startTime, 36000); // parent = 60m 主笔
  });

  test("案例B：X=15、末段内 3 仅 1 笔 → 停 15（不产 3 级候选）", () => {
    const data = { "15": pd(B15), "3": pd(B3) };
    const [stop] = sinkChainConfirm(data, "15", 82800, "short");
    assert.equal(stop, "15");
  });

  test("15m 笔内 3m 点位映射：3 级端点的下沉停止级是 15", () => {
    const data = { "60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(B15), "3": pd(B3) };
    const [stop] = sinkChainConfirm(data, "60", 82800 - 180, "short");
    assert.equal(stop, "15");
  });

  test("P 不是 X 级端点且早于末段端点 → 无链", () => {
    const data = { "60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(B15), "3": pd(B3) };
    const [stop] = sinkChainConfirm(data, "60", 70000, "short");
    assert.equal(stop, null);
  });

  test("P 晚于 X 末段端点（近等双顶平台）→ 虚拟形成笔下沉，parent=末段端点", () => {
    // 60m 末段 up 止于 8-31 19:00@4464.23；19:00 后 15m 走出 4 段（≥3）到 9-1 08:15 顶
    const b60 = [bi("down", 0, 36000, 4500, 4400), bi("up", 36000, 68400, 4400, 4464.23)];
    const b15 = [
      bi("up", 38700, 68700, 4400, 4460.0),       // 10:45 → 19:15（平台内）
      bi("down", 68700, 77700, 4460.0, 4415.75),  // 19:15 → 21:45
      bi("up", 77700, 100800, 4415.75, 4455.93),  // 21:45 → 04:00（#3 参照）
      bi("down", 100800, 112500, 4455.93, 4441.85),
      bi("up", 112500, 116100, 4441.85, 4461.7),  // 末段（终点即 P，越过 60m 端点）
    ];
    const data = { "60": pd(b60), "15": pd(b15), "3": pd(B3) };
    const [stop, parent] = sinkChainConfirm(data, "60", 116100, "short");
    assert.equal(stop, "15");
    assert.equal(parent.startTime, 68400); // 虚拟形成笔起点 = 60m 末段端点 19:00
  });

  test("15m 展开不足 3 段 → 停在 X", () => {
    const b15_2 = [
      bi("down", 0, 30000, 4500, 4400),
      bi("up", 30000, 34000, 4400, 4440.0),      // 起点在 60 笔外 → 不计
      bi("down", 34000, 70200, 4440.0, 4420.0),
      bi("up", 70200, 82800, 4420.0, 4461.7),    // 内部仅 2 段 <3
    ];
    const data = { "60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(b15_2), "3": pd(B3_RICH) };
    const [stop] = sinkChainConfirm(data, "60", 82800, "short");
    assert.equal(stop, "60");
  });
});

describe("findDivergePoints / lowerDiverge 过滤", () => {
  test("findDivergePoints 候选带 referStart", () => {
    // 3m：末段创新高 + MACD 红柱面积变小 → 顶背驰点
    const m3 = macd([[60300, 5.0, 3.0], [61200, 5.0, 3.0], [78200, 1.0, 1.0], [82800, 1.0, 1.0]]);
    const pts = findDivergePoints(B3_RICH, m3);
    const shorts = pts.filter(p => p.direction === "short");
    assert.ok(shorts.length >= 1);
    assert.equal(shorts[0].referStart, 60000); // 参照 = up(60000→70000)
  });

  test("3m 裸背驰点在下沉停止级=15 时被 lowerDiverge 过滤", () => {
    const m3 = macd([[60300, 5.0, 3.0], [61200, 5.0, 3.0], [78200, 1.0, 1.0], [82800, 1.0, 1.0]]);
    const data = { "60": pd([B60_DOWN_BEFORE, B60_UP]), "15": pd(B15), "3": pd(B3_RICH, m3) };
    // 3m 有裸顶背驰点，但该点下沉停止级是 15 → 过滤（15 又未创新高无候选）→ 空
    assert.ok(findDivergePoints(B3_RICH, m3).some(p => p.direction === "short"));
    assert.deepEqual(lowerDiverge(data, "60", "short"), []);
  });
});
