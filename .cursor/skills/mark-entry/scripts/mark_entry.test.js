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
  pointStrategyOf, sideCandidates,
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

describe("出场规则：stopRefOf（支阻位±滑点/最大止损）", () => {
  test("short 上方支阻位 + 止损滑点，再按最大止损夹紧；nearSr 错侧重选/正确侧沿用", () => {
    const sig = { direction: "short", price: 4450 };
    // 4460+3=4463 → 最大止损 4460；nearSr 错侧重选同；正确侧 4465+3=4468 → 4460
    assert.equal(stopRefOf(sig, [{ price: 4460 }, { price: 4420 }]), 4460);
    assert.equal(stopRefOf({ ...sig, nearSr: 4420 }, [{ price: 4460 }]), 4460);
    assert.equal(stopRefOf({ ...sig, nearSr: 4465 }, [{ price: 4460 }]), 4460);
  });

  test("无正确侧位 → 进场价±最大止损（永不为 null）", () => {
    const sig = { direction: "short", price: 4450 };
    assert.equal(stopRefOf(sig, [{ price: 4400 }]), 4460);
    assert.equal(stopRefOf(sig, []), 4460);
    assert.equal(stopRefOf({ direction: "long", price: 4450 }, []), 4440);
  });

  test("自定义滑点；支阻更近时保持", () => {
    const sig = { direction: "short", price: 4450 };
    assert.equal(stopRefOf(sig, [{ price: 4460 }], 5, 20), 4465);
    assert.equal(stopRefOf(sig, [], 5, 20), 4470);
    assert.equal(stopRefOf(sig, [{ price: 4455 }]), 4458);
    assert.equal(stopRefOf({ direction: "long", price: 4450 }, [{ price: 4445 }]), 4442);
  });

  test("ATR 分量：有效滑点 = 固定值 + 系数×ATR；再按有效最大止损夹紧", () => {
    const sig = { direction: "short", price: 4450 };
    // 4460+(3+0.5×2)=4464 → 最大止损 4462；无正确侧 4462；nearSr 4470 → 4460
    assert.equal(stopRefOf(sig, [{ price: 4460 }], 3, 10, 2, 0.5, 1), 4462);
    assert.equal(stopRefOf(sig, [], 3, 10, 2, 0.5, 1), 4462);
    assert.equal(stopRefOf({ ...sig, nearSr: 4465 }, [{ price: 4460 }], 3, 10, 2, 1, 0), 4460);
    // k=0（或 atr=0）→ 仍受默认最大止损 10 约束
    assert.equal(stopRefOf(sig, [{ price: 4460 }], 3, 10, 2, 0, 0), 4460);
    assert.equal(stopRefOf(sig, [{ price: 4460 }], 3, 10, 0, 0.7, 0.9), 4460);
  });
});

describe("pointStrategyOf：买卖点 type+strong → 进场 key 直映射（2026-10-16 直连）", () => {
  test("1类直接档统一（形成中与已确认同档）+ 二买族强弱二分 + 3/类3类4族分流", () => {
    const cases = [
      [{ type: "1买", time: 1, price: 1 }, "wait1Buy", "long"],
      [{ type: "1卖", time: 1, price: 1 }, "wait1Sell", "short"],
      [{ type: "2买", time: 1, price: 1, strong: true }, "wait2Buy", "long"],
      [{ type: "类2买", time: 1, price: 1, strong: true }, "wait2Buy", "long"],
      [{ type: "2买", time: 1, price: 1, strong: false }, "wait2BuyBear", "long"],
      [{ type: "类2买", time: 1, price: 1, strong: null }, "wait2BuyBear", "long"],   // 未定型弱先行
      [{ type: "2卖", time: 1, price: 1, strong: true }, "wait2Sell", "short"],
      [{ type: "类2卖", time: 1, price: 1, strong: false }, "wait2SellBear", "short"],
      [{ type: "3买", time: 1, price: 1 }, "wait3Buy", "long"],
      [{ type: "3卖", time: 1, price: 1 }, "wait3Sell", "short"],
      [{ type: "类3买", time: 1, price: 1 }, "waitBuy", "long"],
      [{ type: "4买", time: 1, price: 1 }, "waitBuy", "long"],
      [{ type: "类4买", time: 1, price: 1 }, "waitBuy", "long"],
      [{ type: "类3卖", time: 1, price: 1 }, "waitSell", "short"],
      [{ type: "4卖", time: 1, price: 1 }, "waitSell", "short"],
      [{ type: "类4卖", time: 1, price: 1 }, "waitSell", "short"],
    ];
    for (const [point, key, direction] of cases) {
      const s = pointStrategyOf(point);
      assert.ok(s, point.type);
      assert.equal(s.key, key, point.type);
      assert.equal(s.direction, direction, point.type);
    }
  });

  test("pointCls：2族由 strong 派生（强二买/弱类2买…），非2族为 null", () => {
    assert.equal(pointStrategyOf({ type: "2买", time: 1, price: 1, strong: true }).pointCls, "强二买");
    assert.equal(pointStrategyOf({ type: "类2买", time: 1, price: 1, strong: false }).pointCls, "弱类2买");
    assert.equal(pointStrategyOf({ type: "1买", time: 1, price: 1 }).pointCls, null);
  });

  test("未知类型 → null（waitLike2Buy/waitLike2Sell 无映射入口）", () => {
    assert.equal(pointStrategyOf({ type: "观望", time: 1, price: 1 }), null);
    assert.equal(pointStrategyOf({ type: "", time: 1, price: 1 }), null);
  });

  test("sideCandidates：买卖双侧各自取最近笔端点锚", () => {
    const bis = [
      { type: "up", startTime: 0, endTime: 60, startPrice: 100, endPrice: 110 },
      { type: "down", startTime: 60, endTime: 120, startPrice: 110, endPrice: 95 },
    ];
    const points = { buy: [{ type: "1买", time: 120, price: 95 }],
                     sell: [{ type: "1卖", time: 60, price: 110 }] };
    const cands = sideCandidates(bis, points, 0, 180);
    assert.deepEqual(cands.map(c => c.type).sort(), ["1买", "1卖"]);
    assert.deepEqual(sideCandidates(bis, { buy: [], sell: [] }, 0, 180), []);
  });
});

describe("出场规则：trendFollowingOf / favSeg5Time", () => {
  test("顺势判定：按 strategyKey（2026-10-16 起唯一口径）；planDirection 仅 strategyKey 缺省时兼容", () => {
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
    // beMode 钉 extreme（py ecfg() 同惯例）：新默认 entry=进场价 4450 会在 bar200
    // （high 4451）提前打保本，本用例验证的是极值口径的 4455 迁移与跳空成交
    const sim = simulatePosition(SIG(), 4463, mr, px, { exitCfg: { beMode: "extreme" } });
    assert.equal(sim.beStop, 4455);
    assert.deepEqual(sim.events.map(e => e.type), ["breakeven", "stopBe"]);
    assert.equal(sim.events[1].time, 400);
    assert.equal(sim.events[1].price, 4455); // max(beStop, open 4450)
    assert.equal(sim.closed, true);
  });

  test("ATR 分量：有效保本滑点 = slipBe + kBe×markRes ATR（2026-09-19，成对 py 开仓点 slip_be_eff）", () => {
    const mr = { bis: [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)],
                 bars: MR_BARS, atr: 2 };
    // 进场K线 high 4452：beStop = 4452 + (3 + 1×2) = 4457
    const sim = simulatePosition(SIG(), 4463, mr, { bars: altBars([0, 100, 200, 300, 400]) },
                                 { slipBe: 3, kBe: 1, exitCfg: { beMode: "extreme" } });
    assert.equal(sim.beStop, 4457);
    // kBe=0 → 纯固定滑点（回归）
    const sim0 = simulatePosition(SIG(), 4463, mr, { bars: altBars([0, 100, 200, 300, 400]) },
                                  { slipBe: 3, kBe: 0, exitCfg: { beMode: "extreme" } });
    assert.equal(sim0.beStop, 4455);
  });

  test("beMode=entry（默认）：beStop=进场价 4450，保本出场=0亏损口径（成对 py TestStopBeMode）", () => {
    const mr = { bis: [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)], bars: MR_BARS };
    const px = { bis: [bi("up", 50, 300, 4440, 4450)], bars: altBars([0, 100, 200, 300, 400]) };
    const sim = simulatePosition(SIG(), 4463, mr, px); // 默认 EXIT_CFG.beMode=entry
    assert.equal(sim.beStop, 4450);
    // bar200：TP1 迁移当拍 high 4451 > 4450 即打保本；跳空按开盘 max(4450,4450)=4450
    assert.deepEqual(sim.events.map(e => e.type), ["breakeven", "stopBe"]);
    assert.equal(sim.events[1].time, 200);
    assert.equal(sim.events[1].price, 4450);
    assert.equal(sim.closed, true);
  });

  test("beMode=bi：beStop=背驰周期首笔有利方向笔起点±滑点（short: down 起点 4450+3）", () => {
    const mr = { bis: [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)], bars: MR_BARS };
    const px = { bis: [bi("up", 50, 300, 4440, 4450)], bars: altBars([0, 100, 200, 300, 400]) };
    const sim = simulatePosition(SIG(), 4463, mr, px, { exitCfg: { beMode: "bi" } });
    assert.equal(sim.beStop, 4453); // tp1=down(100→200) 起点 4450 + slipBe 3
    // bar200 TP1 迁移至 4453（high 4451 未破）；bar400 high 4456 破 → stopBe @4453
    assert.deepEqual(sim.events.map(e => e.type), ["breakeven", "stopBe"]);
    assert.equal(sim.events[1].time, 400);
    assert.equal(sim.events[1].price, 4453);
    assert.equal(sim.closed, true);
  });

  test("beMode=bi：无有利方向笔 → beStop 待定（null，成笔前不迁移）", () => {
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: MR_BARS }; // short 盼 down 笔，不存在
    const px = { bis: [bi("up", 50, 300, 4440, 4450)], bars: altBars([0, 100, 200, 300, 400]) };
    const sim = simulatePosition(SIG(), 4463, mr, px, { exitCfg: { beMode: "bi" } });
    assert.equal(sim.beStop, null);
    assert.ok(!sim.events.some((e) => e.type === "breakeven" || e.type === "stopBe"));
  });

  test("beMode=bi：half 抢先（t5=350 早于成笔 500）→ 止损暂留支阻位，成笔那拍迁至笔端点（成对 py be_active 回落）", () => {
    const mr = {
      bis: [bi("up", 0, 100, 4440, 4450), bi("down", 100, 500, 4450, 4430)],
      bars: [
        bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
        bar(300, 4455, 4458, 4442, 4446), bar(400, 4456, 4459, 4444, 4448),
        bar(500, 4450, 4456, 4446, 4452), bar(600, 4452, 4455, 4448, 4450),
      ],
    };
    // px 首个 fav(down) 形成段起点 150，5 块合并K → t5=350（< tp1.time 500）；
    // 末根 down 终点 4446 ≥ 前一 down 终点 4445 → 不破前低（tp3a=null，隔离出纯 half 路径）。
    // mr bar300/400 high 4458/4459 破 4453 但未破支阻 4463——若 bi 成笔前误迁保本位，
    // 本用例会在 bar300 提前 stopBe 出局（回归锚点）
    const px = { bars: altBars([0, 50, 100, 150, 200, 250, 300, 350]),
                 bis: [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                       bi("down", 150, 250, 4450, 4445), bi("up", 250, 300, 4445, 4448),
                       bi("down", 300, 350, 4448, 4446)] };
    const sim = simulatePosition(SIG(), 4463, mr, px, { exitCfg: { beMode: "bi" } });
    assert.equal(sim.beStop, 4453);
    // half 成交于下一开盘 400（彼时 beStop 未落位，止损仍 4463 → 400 前 high 4459 不触发
    // stopSr）；bar500 成笔落位 4453，当拍 high 4456 破位 → stopBe 全平 @4453
    assert.deepEqual(sim.events.map((e) => e.type), ["half", "stopBe"]);
    assert.equal(sim.events[0].time, 400);
    assert.equal(sim.events[1].time, 500);
    assert.equal(sim.events[1].price, 4453);
    assert.equal(sim.closed, true);
  });

  test("findBiEvent：返回增量键 startPrice（bi 口径取「有利方向笔起点」，成对 py）", () => {
    const bis = [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430),
                 bi("up", 200, 300, 4430, 4448)];
    assert.deepEqual(findBiEvent(bis, 100, "down"),
                     { time: 200, price: 4430, startTime: 100, startPrice: 4450 });
    const ev = findBiEvent(bis, 100, "up", false, true); // breakPrev：up(200→300) 未过前高 4450 → null
    assert.equal(ev, null);
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
    const sim = simulatePosition(SIG(), 4463, mr, px, { exitCfg: { beMode: "extreme" } });
    const types = sim.events.map(e => e.type);
    assert.deepEqual(types, ["half", "stopBe"]); // t5=700 → half 成交于下一开盘 800；900 破 beStop
    assert.equal(sim.events[0].time, 800);
    assert.equal(sim.events[0].price, 4448);
    assert.equal(sim.events[1].time, 900);
    assert.equal(sim.events[1].price, 4455);
    assert.equal(sim.closed, true);
  });

  test("逆势（多头空）：无 half，close 同走破前低（2026-10-10 与 py 引擎统一）", () => {
    const mrBars = [
      bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
      bar(300, 4445, 4449, 4438, 4442), bar(400, 4445, 4449, 4436, 4440),
      bar(500, 4440, 4448, 4435, 4444), bar(600, 4444, 4450, 4438, 4446),
      bar(700, 4446, 4451, 4440, 4448), bar(800, 4448, 4452, 4442, 4450),
    ];
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: mrBars };
    // down(150→700) 终点 4430 破前一同向 down(0→50) 终点 4440 → tp3a=700
    // （旧口径「形成段≥5块直接全平」废除；favSeg5Time 不再是逆势 close 事件源）
    const px = { bars: altBars([0, 100, 200, 300, 400, 500, 600, 700, 800]),
                 bis: [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                       bi("down", 150, 700, 4450, 4430)] };
    // 2026-10-16 起按 strategyKey 判定（wait1Sell=逆势；旧 planDirection 口径已取消）
    const sim = simulatePosition(SIG({ planDirection: "多头空", strategyKey: "wait1Sell" }), 4463, mr, px,
                                 { exitCfg: { closePct: 100 } });
    assert.deepEqual(sim.events.map(e => e.type), ["close"]); // tp3a=700 → 下一开盘 800 全平
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
    // 过高低点默认已改 25% 部分平仓（2026-10-10 统一口径）；本用例钉全平（与 py 同口径）
    const sim = simulatePosition(SIG(), 4463, mr, px, { exitCfg: { closePct: 100 } });
    assert.deepEqual(sim.events.map(e => e.type), ["close"]);
    assert.equal(sim.events[0].time, 300); // tp3a=250 → 下一开盘 300
    assert.equal(sim.events[0].price, 4445);
  });

  test("closePct<100：部分平仓只触发一次（closeDone latch，2026-10-08 与 py 引擎同修）", () => {
    const mrBars = [
      bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
      bar(300, 4445, 4448, 4430, 4436), bar(400, 4445, 4449, 4434, 4440),
      bar(500, 4440, 4448, 4432, 4444), bar(600, 4444, 4450, 4438, 4446),
      bar(700, 4446, 4451, 4440, 4448),
    ];
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: mrBars };
    const px = { bars: altBars([0, 100, 200, 300]),
                 bis: [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                       bi("down", 150, 250, 4450, 4435), bi("up", 250, 300, 4435, 4445)] };
    // tp3a（250 破前低）之后每拍都满足——修复前 close 每 markRes bar 重复平 25%
    // （px 维持 4 根 bar：favSeg5Time 不达标，隔离出纯 close 路径）
    const sim = simulatePosition(SIG(), 4463, mr, px, { exitCfg: { closePct: 25 } });
    const closes = sim.events.filter((e) => e.type === "close");
    assert.equal(closes.length, 1);
    assert.equal(closes[0].time, 300);
    assert.equal(closes[0].lots, 1);          // 25% × 4
    assert.equal(sim.lotsLeft, 3);
    assert.deepEqual(sim.events.map((e) => e.type), ["close", "stillOpen"]);
  });

  test("closePct<100 剩余过小舍入为 0 → 取全部剩余（不产 0 手事件/挂死尾仓）", () => {
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: MR_BARS };
    const px = { bars: altBars([0, 100, 200, 300]),
                 bis: [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                       bi("down", 150, 250, 4450, 4435), bi("up", 250, 300, 4435, 4445)] };
    const sim = simulatePosition(SIG(), 4463, mr, px, { lots: 0.01, exitCfg: { closePct: 25 } });
    assert.deepEqual(sim.events.map((e) => e.type), ["close"]);
    assert.equal(sim.events[0].lots, 0.01);   // 换算不足 1 手 → 封顶剩余=全平剩余
    assert.equal(sim.closed, true);
  });

  test("百分比基数=总仓位：half 50% 平2后 close 25% 仍平1（整数手，非剩余0.5）", () => {
    // 同「同拍 half 优先于 close」场景 + 比例配置：half ⌊4×50%⌋=2、close ⌊4×25%⌋=1
    // （旧口径按剩余仓位 2×25%=0.5 小数手）
    const mrBars = [
      bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
      bar(300, 4445, 4448, 4438, 4442), bar(400, 4445, 4449, 4436, 4440),
      bar(500, 4440, 4448, 4435, 4444), bar(600, 4444, 4450, 4438, 4446),
      bar(700, 4446, 4451, 4440, 4448),
    ];
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: mrBars };
    const px = { bars: altBars([0, 100, 200, 300, 400, 500, 600, 700]),
                 bis: [bi("down", 0, 50, 4460, 4440), bi("up", 50, 150, 4440, 4450),
                       bi("down", 150, 250, 4450, 4445), bi("up", 250, 300, 4445, 4448),
                       bi("down", 300, 500, 4448, 4435)] };
    const sim = simulatePosition(SIG(), 4463, mr, px,
                                 { exitCfg: { beMode: "extreme", halfPct: 50, closePct: 25 } });
    assert.deepEqual(sim.events.map((e) => e.type), ["half", "close", "stillOpen"]);
    assert.equal(sim.events[0].lots, 2);           // ⌊4×50%⌋：总仓位基数、整数手
    assert.equal(sim.events[1].lots, 1);           // ⌊4×25%⌋=1（非剩余 2×25%=0.5）
    assert.ok(Number.isInteger(sim.events[0].lots) && Number.isInteger(sim.events[1].lots));
    assert.equal(sim.lotsLeft, 1);
  });

  test("halfMr（背驰周期够笔）：tp1 同事件源部分平仓一次；默认关不触发（成对 py）", () => {
    const mrBars = [
      bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
      bar(300, 4445, 4448, 4438, 4442), bar(400, 4444, 4449, 4436, 4440),
    ];
    const mr = { bis: [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)], bars: mrBars };
    const px = { bis: [bi("up", 50, 300, 4440, 4450)], bars: altBars([0, 100, 200, 300, 400]) };
    // 默认关：只有 TP1 保本迁移（旧基线可比）
    const sim0 = simulatePosition(SIG(), 4463, mr, px, { exitCfg: { beMode: "extreme" } });
    assert.ok(!sim0.events.some((e) => e.type === "halfMr"));
    assert.deepEqual(sim0.events.map((e) => e.type), ["breakeven", "stillOpen"]);
    // 开启 25%：tp1=200 → halfMr 成交于下一开盘 300，halfMr latch 后不再重复
    const sim = simulatePosition(SIG(), 4463, mr, px,
                                 { exitCfg: { beMode: "extreme", halfMrOn: true, halfMrPct: 25 } });
    const mrs = sim.events.filter((e) => e.type === "halfMr");
    assert.equal(mrs.length, 1);
    assert.equal(mrs[0].time, 300);
    assert.equal(mrs[0].lots, 1);              // 25% × 4
    assert.equal(sim.lotsLeft, 3);
    assert.deepEqual(sim.events.map((e) => e.type), ["breakeven", "halfMr", "stillOpen"]);
  });

  test("stopSr：未保本时盘中破坏止损位（跳空按开盘成交）", () => {
    const mrBars = [
      bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4455),
      bar(300, 4462, 4466, 4455, 4460),
    ];
    const mr = { bis: [bi("up", 0, 700, 4440, 4455)], bars: mrBars };
    const px = { bis: [bi("up", 50, 300, 4440, 4450)], bars: altBars([0, 100, 200, 300]) };
    const sim = simulatePosition(SIG(), 4463, mr, px);
    assert.deepEqual(sim.events.map(e => e.type), ["stopSr"]);
    assert.equal(sim.events[0].price, 4463); // max(4463, open 4462)
  });

  test("跟踪止盈门控保本：beDone 前参照点不提损/不接管，保本后参照点才消费（成对 py）", () => {
    // 门控开启拍=TP1 首次可见的 bar200 → trailMark=200：@150 的更紧参照点（4441）
    // 不追溯；@250 参照点 4442+1=4443 提损；bar400 破位记 trailStop
    const mr = { bis: [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)],
                 bars: [
                   bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
                   bar(300, 4445, 4450, 4442, 4448), bar(400, 4446, 4447, 4440, 4444),
                 ] };
    const px = { bis: [bi("up", 50, 300, 4440, 4450)], bars: altBars([0, 100, 200, 300, 400]) };
    const sigs = [
      { time: 150, price: 4440, direction: "short", strategyKey: "wait3Sell" }, // 门控前：不追溯
      { time: 250, price: 4442, direction: "short", strategyKey: "wait3Sell" }, // 4443 < beStop 4455 上移
    ];
    const sim = simulatePosition(SIG(), 4463, mr, px,
                                 { exitCfg: { beMode: "extreme", trailOn: true, trailSlip: 1 },
                                   trailSigs: sigs });
    const types = sim.events.map((e) => e.type);
    // trailPct 默认 25% 部分平仓 → 终态仍持仓（stillOpen）
    assert.deepEqual(types, ["breakeven", "trailRaise", "trailStop", "stillOpen"]);
    assert.equal(sim.events[1].stopTo, 4443);   // 4442+1；@150 未消费（否则 4441）
    assert.equal(sim.events[2].price, 4446);    // max(4443, 开盘 4446)
  });

  test("最大止损硬上限兜底：trailStop 25% latch 后，剩余手数在 maxLoss 全平（成对 py）", () => {
    // 保本→提损 4445→trailStop 1 手（trailFired latch）→价格越过 maxLoss 4460
    // → 兜底 stopSr 全平剩余 3 手，单手亏损封顶 slip_fallback=10
    const mr = { bis: [bi("up", 0, 100, 4440, 4450), bi("down", 100, 200, 4450, 4430)],
                 bars: [
                   bar(100, 4450, 4452, 4448, 4451), bar(200, 4450, 4451, 4440, 4445),
                   bar(300, 4450, 4451, 4444, 4450), bar(400, 4444, 4446, 4440, 4445),
                   bar(500, 4455, 4461, 4450, 4459),
                 ] };
    const px = { bis: [bi("up", 50, 300, 4440, 4450)], bars: altBars([0, 100, 200, 300, 400, 500]) };
    const sigs = [{ time: 250, price: 4444, direction: "short", strategyKey: "wait3Sell" }];
    const sim = simulatePosition(SIG(), 4463, mr, px,
                                 { exitCfg: { beMode: "extreme", trailOn: true, trailSlip: 1, trailPct: 25 },
                                   trailSigs: sigs });
    const types = sim.events.map((e) => e.type);
    assert.deepEqual(types, ["breakeven", "trailRaise", "trailStop", "stopSr"]);
    assert.equal(sim.events[2].lots, 1);        // 25% × 4
    assert.equal(sim.events[3].lots, 3);        // 兜底全平剩余
    assert.equal(sim.events[3].price, 4460);    // max(maxLoss 4460, open 4455)
    assert.equal(sim.lotsLeft, 0);
    assert.equal(sim.closed, true);
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
    const sim = simulatePosition(SIG(), 4463, mr, px, { exitCfg: { beMode: "extreme" } });
    const types = sim.events.map(e => e.type);
    // half/close 默认均部分平仓 → 终态仍持仓（stillOpen）
    assert.deepEqual(types, ["half", "close", "stillOpen"]); // bar500 同拍挂 half，close 顺延下一拍
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

  test("强分型：右肩收盘穿左肩开盘（底/顶，不含影线）", () => {
    const mBottom = [
      { open: 105, high: 106, low: 96, close: 100, time: 0, highTime: 0, lowTime: 0 },
      { open: 100, high: 104, low: 90, close: 98, time: 1, highTime: 1, lowTime: 1 },
      { open: 98, high: 108, low: 92, close: 107, time: 2, highTime: 2, lowTime: 2 },
    ];
    const frs = findFractalsOf(mBottom);
    assert.equal(frs.length, 1);
    assert.equal(frs[0].type, "bottom");
    assert.equal(planCore.strongFractalAfter(mBottom, frs, 1, "bottom"), true);
    const mWeak = mBottom.map((b, i) => (i === 2 ? { ...b, close: 103 } : b));
    assert.equal(planCore.strongFractalAfter(mWeak, findFractalsOf(mWeak), 1, "bottom"), false);
  });

  test("trendStateOf：关闭 null / 无笔 dir=null / 默认参考周期空（2026-10-10 起关闭）", () => {
    assert.equal(planCore.trendStateOf({}, {}, ""), null);
    assert.deepEqual(planCore.trendStateOf({ "240": [] }, { "240": [] }, "240"),
                     { dir: null, reason: "", res: "240" });
    assert.equal(planCore.TREND_RES, "");
  });
});

// findFractals 从 chan-core 取（保持测试自包含的薄封装）
function findFractalsOf(merged) {
  return require("../../chan-core/scripts/chan_core.js").findFractals(merged);
}
