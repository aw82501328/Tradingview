/**
 * mark-entry 区间套下沉判定单元测试（与 py_chain/test_mark_entry_sink.py 对齐）
 * + 出场规则单元测试（与 py_chain/test_exit_rules.py 成对同构，出场阶梯重构 2026-09-09）
 *
 * 依据 SPEC_divergence_chanset.md 规则 1/2/3 与 .cursor/skills/mark-entry/SPEC.md §2.2/§2.4。
 * 数据为手工构造、可独立验算的最小示例（确认制 sinkChainConfirm 路径）。
 *
 * 运行：node --test .cursor/skills/mark-entry/scripts/mark_entry.test.js
 */
const { describe, test } = require("node:test");
const assert = require("node:assert/strict");
const {
  findDivergePoints, lowerDiverge, levelsBelow, sinkChainConfirm,
  stopRefOf, findBiEvent, trendFollowingOf, favSeg5Time, simulatePosition,
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

// ============================================================
// 出场规则（出场阶梯重构 2026-09-09；与 py_chain/test_exit_rules.py 成对同构）
// ============================================================
const bar = (time, open, high, low, close) => ({ time, open, high, low, close });
// 交替高低的无包含K线：每根各自成块 → born = 各 bar 时间（与 py 用例的 T8/T7 同构）
const altBars = (times) => times.map((t, i) => bar(t, 10, i % 2 ? 11 : 10, i % 2 ? 10 : 9, 10));

describe("出场规则：stopRefOf（支阻位±滑点/兜底）", () => {
  test("short 上方支阻位 + 止损滑点；nearSr 错侧重选/正确侧沿用", () => {
    const sig = { direction: "short", price: 4450 };
    assert.equal(stopRefOf(sig, [{ price: 4460 }, { price: 4420 }]), 4463);
    assert.equal(stopRefOf({ ...sig, nearSr: 4420 }, [{ price: 4460 }]), 4463);
    assert.equal(stopRefOf({ ...sig, nearSr: 4465 }, [{ price: 4460 }]), 4468);
  });

  test("无正确侧位 → 兜底 进场价±兜底滑点（永不为 null）", () => {
    const sig = { direction: "short", price: 4450 };
    assert.equal(stopRefOf(sig, [{ price: 4400 }]), 4460);
    assert.equal(stopRefOf(sig, []), 4460);
    assert.equal(stopRefOf({ direction: "long", price: 4450 }, []), 4440);
  });

  test("自定义滑点", () => {
    const sig = { direction: "short", price: 4450 };
    assert.equal(stopRefOf(sig, [{ price: 4460 }], 5, 20), 4465);
    assert.equal(stopRefOf(sig, [], 5, 20), 4470);
  });
});

describe("出场规则：trendFollowingOf / favSeg5Time", () => {
  test("顺势判定：计划方向 + strategyKey 兜底", () => {
    assert.equal(trendFollowingOf("多头多"), true);
    assert.equal(trendFollowingOf("空头空"), true);
    assert.equal(trendFollowingOf("多头空"), false);
    assert.equal(trendFollowingOf("空头多"), false);
    assert.equal(trendFollowingOf(null, "wait2Buy"), true);
    assert.equal(trendFollowingOf(null, "wait1Sell"), false);
  });

  test("favSeg5Time：段内第 5 块合并K诞生时间（成对 py forming_seg_ready 五块就绪）", () => {
    // JS 视角是最终结构：up(50→300) 后的下跌段已成笔 down(300→700)；
    // py 视角为形成态（末笔 up、其后 4 块）——两侧同一触发时间 700
    const pd8 = { bars: altBars([0, 100, 200, 300, 400, 500, 600, 700]),
                  bis: [bi("up", 50, 300, 4440, 4450), bi("down", 300, 700, 4450, 4430)] };
    assert.equal(favSeg5Time(pd8, true, 100), 700);
  });

  test("favSeg5Time：不足 5 块 → null（成对 py 四块未就绪）", () => {
    const pd7 = { bars: altBars([0, 100, 200, 300, 400, 500, 600]),
                  bis: [bi("up", 50, 300, 4440, 4450), bi("down", 300, 600, 4450, 4430)] };
    assert.equal(favSeg5Time(pd7, true, 100), null);
  });

  test("favSeg5Time：无进场后有利方向笔 → null", () => {
    const pd = { bars: altBars([0, 100, 200, 300, 400, 500, 600, 700]),
                 bis: [bi("down", 0, 50, 4460, 4440), bi("up", 50, 300, 4440, 4450)] };
    assert.equal(favSeg5Time(pd, true, 100), null);
  });
});

describe("出场规则：simulatePosition（出场状态机）", () => {
  const SIG = (over) => Object.assign(
    { direction: "short", time: 100, price: 4450, strategyKey: "wait2Sell", planDirection: "空头空" }, over);
  // markRes K线：进场K线 @100（high 4452 → beStop = 4455）
  const MR_BARS = [
    bar(100, 4450, 4452, 4448, 4451),
    bar(200, 4450, 4451, 4440, 4445),
    bar(300, 4445, 4448, 4438, 4442),
    bar(400, 4450, 4456, 4446, 4452),
  ];

  test("TP1 → 止损移至 beStop → 盘中破坏 beStop 记 stopBe（跳空按开盘成交）", () => {
    const mr = { bis: [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)], bars: MR_BARS };
    const px = { bis: [bi("up", 50, 300, 4440, 4450)], bars: altBars([0, 100, 200, 300, 400]) };
    const sim = simulatePosition(SIG(), 4463, mr, px);
    assert.equal(sim.beStop, 4455);
    assert.deepEqual(sim.events.map(e => e.type), ["breakeven", "stopBe"]);
    assert.equal(sim.events[1].time, 400);
    assert.equal(sim.events[1].price, 4455); // max(beStop, open 4450)
    assert.equal(sim.closed, true);
  });

  test("TP2 顺势：形成段≥5块 → 平一半（下一开盘成交），剩余半仓打 beStop 终局", () => {
    const mrBars = [
      bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
      bar(300, 4445, 4449, 4438, 4442), bar(400, 4445, 4449, 4436, 4440),
      bar(500, 4440, 4448, 4435, 4444), bar(600, 4444, 4450, 4438, 4446),
      bar(700, 4446, 4451, 4440, 4448), bar(800, 4448, 4452, 4442, 4450),
      bar(900, 4450, 4456, 4446, 4452),
    ];
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: mrBars }; // 无有利方向笔 → TP1 未触发
    const px = { bars: altBars([0, 100, 200, 300, 400, 500, 600, 700, 800]),
                 bis: [bi("up", 50, 300, 4440, 4450), bi("down", 300, 700, 4450, 4430)] };
    const sim = simulatePosition(SIG(), 4463, mr, px);
    const types = sim.events.map(e => e.type);
    assert.deepEqual(types, ["half", "stopBe"]); // t5=700 → half 成交于下一开盘 800；900 破 beStop
    assert.equal(sim.events[0].time, 800);
    assert.equal(sim.events[0].price, 4448);
    assert.equal(sim.events[1].time, 900);
    assert.equal(sim.events[1].price, 4455);
    assert.equal(sim.closed, true);
  });

  test("逆势（多头空）：无 half，形成段≥5块直接全平", () => {
    const mrBars = [
      bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
      bar(300, 4445, 4449, 4438, 4442), bar(400, 4445, 4449, 4436, 4440),
      bar(500, 4440, 4448, 4435, 4444), bar(600, 4444, 4450, 4438, 4446),
      bar(700, 4446, 4451, 4440, 4448), bar(800, 4448, 4452, 4442, 4450),
    ];
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: mrBars };
    const px = { bars: altBars([0, 100, 200, 300, 400, 500, 600, 700, 800]),
                 bis: [bi("up", 50, 300, 4440, 4450), bi("down", 300, 700, 4450, 4430)] };
    const sim = simulatePosition(SIG({ planDirection: "多头空" }), 4463, mr, px);
    assert.deepEqual(sim.events.map(e => e.type), ["close"]); // t5=700 → 下一开盘 800 全平
    assert.equal(sim.events[0].time, 800);
    assert.equal(sim.events[0].price, 4448);
  });

  test("TP3a 顺势：有利方向笔破前低 → 全平（下一开盘成交）", () => {
    const mrBars = [
      bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
      bar(300, 4445, 4448, 4430, 4436),
    ];
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: mrBars };
    const px = { bars: altBars([0, 100, 200, 300]),
                 bis: [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                       bi("down", 150, 250, 4450, 4435), bi("up", 250, 300, 4435, 4445)] };
    const sim = simulatePosition(SIG(), 4463, mr, px);
    assert.deepEqual(sim.events.map(e => e.type), ["close"]);
    assert.equal(sim.events[0].time, 300); // tp3a=250 → 下一开盘 300
    assert.equal(sim.events[0].price, 4445);
  });

  test("stopSr：未保本时盘中破坏止损位（跳空按开盘成交）", () => {
    const mrBars = [
      bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
      bar(300, 4462, 4466, 4455, 4460),
    ];
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: mrBars };
    const px = { bis: [bi("up", 50, 300, 4440, 4450)], bars: altBars([0, 100, 200, 300]) };
    const sim = simulatePosition(SIG(), 4463, mr, px);
    assert.deepEqual(sim.events.map(e => e.type), ["stopSr"]);
    assert.equal(sim.events[0].price, 4463); // max(4463, open 4462)
  });

  test("同拍 half 优先于 close（与 py 同拍单事件语义一致）", () => {
    const mrBars = [
      bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
      bar(300, 4445, 4448, 4438, 4442), bar(400, 4445, 4449, 4436, 4440),
      bar(500, 4440, 4448, 4435, 4444), bar(600, 4444, 4450, 4438, 4446),
      bar(700, 4446, 4451, 4440, 4448),
    ];
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: mrBars };
    // tp3a：down(300→500) 破前低 4435<4440（time 500）；favSeg5Time：首个 fav 笔 down(150→250)
    // 段起点 150 → 第5块 born[1+4]=500 —— 两者同拍 500，half 先挂、close 顺延
    const px = { bars: altBars([0, 100, 200, 300, 400, 500, 600, 700]),
                 bis: [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                       bi("down", 150, 250, 4450, 4445), bi("up", 250, 300, 4445, 4448),
                       bi("down", 300, 500, 4448, 4435)] };
    const sim = simulatePosition(SIG(), 4463, mr, px);
    const types = sim.events.map(e => e.type);
    assert.deepEqual(types, ["half", "close"]); // bar500 同拍挂 half，close 顺延下一拍
    assert.equal(sim.events[0].time, 600);       // t5=500 → 下一开盘 600
    assert.equal(sim.events[1].time, 700);       // close 成交不早于 half（lastFill 单调）
  });
});

// ============================================================
// 顺势参考周期方向判定（trading-plan 模块 trendDirection/trendStateOf；
// 与 py_chain/test_trend_filter.py 对齐，规则见 WEB 参数页交易计划页签）
// ============================================================
const planCore = require("../../trading-plan/scripts/trading_plan.js");
const S240 = 14400;
// bar5(time, open, close)：high/low 由开收推导（无影线，避免长影压平干扰）
const bar5 = (t, o, c) => ({ time: t, open: o, close: c, high: Math.max(o, c), low: Math.min(o, c) });
// 结构底路径产出 2买 的 4h 笔序列（与 py 用例同构）：b1 低点 90=结构底、
// b3 低点 95>90 → 2买 @ 4*S240（价格 95）；同窗口 2卖 @ 3*S240（108<顶 110）更早
const BUY_BIS = [
  bi("up", 0, S240, 100, 110),
  bi("down", S240, 2 * S240, 110, 90),
  bi("up", 2 * S240, 3 * S240, 90, 108),
  bi("down", 3 * S240, 4 * S240, 108, 95),
];
// 卖点镜像：结构顶 111（b1），b3 高点 108<111 → 2卖 @ 4*S240（价格 108）
const SELL_BIS = [
  bi("down", 0, S240, 110, 100),
  bi("up", S240, 2 * S240, 100, 111),
  bi("down", 2 * S240, 3 * S240, 111, 105),
  bi("up", 3 * S240, 4 * S240, 105, 108),
];

describe("trendDirection 顺势方向状态机", () => {
  test("笔数据不足 → [null, '']", () => {
    assert.deepEqual(planCore.trendDirection("240", [], [], null, []), [null, ""]);
    assert.deepEqual(planCore.trendDirection("240", [bi("up", 0, S240, 1, 2)], [], null, []), [null, ""]);
  });

  test("2买出现即判多（结构底路径，最近点=2买）", () => {
    const bars = [0, 1, 2, 3, 4].map((i) => bar5(i * S240, 100, 96));
    assert.deepEqual(planCore.trendDirection("240", BUY_BIS, bars, null, []),
                     ["long", "4小时2买"]);
  });

  test("破坏闩锁：点后收盘跌破买点端点价 → 下跌延续（价格收回不翻回）", () => {
    const bars = [bar5(3 * S240, 100, 96), bar5(4 * S240, 95, 94), bar5(5 * S240, 94, 99)];
    assert.deepEqual(planCore.trendDirection("240", BUY_BIS, bars, null, []),
                     ["short", "4小时下跌延续"]);
  });

  test("卖点镜像：2卖即判空；涨破端点价 → 上涨延续", () => {
    const ok = [0, 1, 2, 3, 4].map((i) => bar5(i * S240, 107, 106));
    assert.deepEqual(planCore.trendDirection("240", SELL_BIS, ok, null, []),
                     ["short", "4小时2卖"]);
    const brk = [bar5(4 * S240, 108, 108.5), bar5(5 * S240, 108.5, 110)];
    assert.deepEqual(planCore.trendDirection("240", SELL_BIS, brk, null, []),
                     ["long", "4小时上涨延续"]);
  });

  test("无买卖点 → 回退末笔方向", () => {
    const bars = [0, 1, 2].map((i) => bar5(i * S240, 100, 103));
    const downEnd = [bi("up", 0, S240, 100, 110), bi("down", S240, 2 * S240, 110, 105)];
    assert.deepEqual(planCore.trendDirection("240", downEnd, bars, null, []),
                     ["short", "4小时末笔向下"]);
    const upEnd = [bi("down", 0, S240, 110, 100), bi("up", S240, 2 * S240, 100, 108)];
    assert.deepEqual(planCore.trendDirection("240", upEnd, bars, null, []),
                     ["long", "4小时末笔向上"]);
  });

  test("强分型：右肩收盘穿左肩极值（底/顶镜像）", () => {
    const mBottom = [
      { high: 106, low: 96, close: 100, time: 0, highTime: 0, lowTime: 0 },
      { high: 104, low: 90, close: 98, time: 1, highTime: 1, lowTime: 1 },
      { high: 108, low: 92, close: 107, time: 2, highTime: 2, lowTime: 2 },
    ];
    const frs = findFractalsOf(mBottom);
    assert.equal(frs.length, 1);
    assert.equal(frs[0].type, "bottom");
    assert.equal(planCore.strongFractalAfter(mBottom, frs, 1, "bottom"), true);
    const mWeak = mBottom.map((b, i) => (i === 2 ? { ...b, close: 103 } : b));
    assert.equal(planCore.strongFractalAfter(mWeak, findFractalsOf(mWeak), 1, "bottom"), false);
  });

  test("trendStateOf：关闭 null / 无笔 dir=null / 默认参考周期 240", () => {
    assert.equal(planCore.trendStateOf({}, {}, ""), null);
    assert.deepEqual(planCore.trendStateOf({ "240": [] }, { "240": [] }, "240"),
                     { dir: null, reason: "", res: "240" });
    assert.equal(planCore.TREND_RES, "240");
  });
});

// findFractals 从 chan-core 取（保持测试自包含的薄封装）
function findFractalsOf(merged) {
  return require("../../chan-core/scripts/chan_core.js").findFractals(merged);
}
