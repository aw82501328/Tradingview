/**
 * fxma-entry 纯函数单元测试（与 py_chain/test_fx_ma.py 对齐）
 *
 * 覆盖：maSeries（SMA/EMA）、strongFxAfter（实体口径+minPts+确认时刻）、
 * scanPeriodSignals（点→强分型→均线分离→收盘站线 齐备才触发；点/类别过滤——用注入笔
 * 构造 2买，其余口径由 py 侧同构用例覆盖；黄金分割附近/上级同向两条件经 opts 注入
 * 开启——模块常量固定为默认值（默认关），新条件必须走注入口）、applyMutexAndSimulate
 * （global 互斥 + 止损/止盈盘中触价即成交 + 同根双触优先级）、parseFibLevels。
 *
 * 注意：脚本顶层解析 CLI 参数（默认值即测试口径），require 侧不连 CDP（main
 * 仅 require.main===module 时执行）。
 *
 * 运行：node --test .cursor/skills/fxma-entry/scripts/fxma_entry.test.js
 */
const { describe, test } = require("node:test");
const assert = require("node:assert/strict");
const { maSeries, strongFxAfter, scanPeriodSignals, applyMutexAndSimulate,
        collectBiEnds, parseFibLevels, MODULE_OPTS } =
  require("./fxma_entry.js");

const bar = (time, open, high, low, close) => ({ time, open, high, low, close });
const SEC = 180;

// 与 py 侧同构：方向感知影线（下跌K留下影、上涨K无下影）——V 反处底分型可成立
function barsFromCloses(baseTime, closes, sec = SEC) {
  const out = [];
  let prev = closes[0];
  for (let i = 0; i < closes.length; i++) {
    const o = i ? prev : closes[0] - 1;
    let hi, lo;
    if (closes[i] < o) { hi = o + 0.2; lo = closes[i] - 0.4; }
    else if (closes[i] > o) { hi = closes[i] + 0.2; lo = o; }
    else { hi = o + 0.2; lo = o - 0.2; }
    out.push(bar(baseTime + i * sec, o, hi, lo, closes[i]));
    prev = closes[i];
  }
  return out;
}

describe("maSeries", () => {
  test("SMA 满窗与滑动", () => {
    const s = maSeries([10, 20, 30, 40], 3, "SMA");
    assert.equal(s[1], null);
    assert.equal(s[2], 20);
    assert.equal(s[3], 30);
  });
  test("EMA 首值即就绪", () => {
    const e = maSeries([10, 20], 3, "EMA");
    assert.equal(e[0], 10);
    assert.equal(e[1], 15); // 10 + 0.5×10
  });
});

describe("strongFxAfter", () => {
  const merged = [{ time: 100, open: 100, close: 101 }, { time: 300, open: 50, close: 50 },
                  { time: 500, open: 102, close: 103 }];
  const fx = [{ type: "bottom", time: 300, mergedIdx: 1 }];
  test("实体口径命中 + 确认时刻=右肩块收盘", () => {
    assert.deepEqual(strongFxAfter(merged, fx, 200, "bottom", 0, 180),
                     { time: 300, confirmTime: 680 });
  });
  test("点在分型后/反向/落差不足 → 不命中", () => {
    assert.equal(strongFxAfter(merged, fx, 400, "bottom", 0, 180), null);
    assert.equal(strongFxAfter(merged, fx, 200, "top", 0, 180), null);
    assert.equal(strongFxAfter(merged, fx, 200, "bottom", 5, 180), null);
  });
});

describe("scanPeriodSignals + applyMutexAndSimulate", () => {
  // 上涨→下跌→V反大阳→反弹（与 py signal_bars 同构；触发拍在尾部）
  const closes = [];
  for (let i = 0; i < 12; i++) closes.push(100 + 2.0 * i);
  for (let i = 0; i < 10; i++) closes.push(closes[closes.length - 1] - 2.8);
  closes.push(closes[closes.length - 1] + 9.0);
  for (let i = 0; i < 10; i++) closes.push(closes[closes.length - 1] + 2.0);
  const bars = barsFromCloses(0, closes);
  const ptTime = bars[closes.length - 12].time;
  const T = (i) => bars[i].time;
  // 笔结构（无上级笔 → 2买=结构底抬高）：先跌到 90（结构底），V 反后回调只到 94
  // （更高底）→ 2买 @ ptTime（笔价与K线不必逐点一致，仅供点分类）
  const periodBis = {
    "3": [
      { type: "up", startTime: T(0), endTime: T(1), startPrice: closes[0], endPrice: closes[1] },
      { type: "down", startTime: T(1), endTime: T(2), startPrice: closes[1], endPrice: 90 },
      { type: "up", startTime: T(2), endTime: T(3), startPrice: 90, endPrice: closes[3] },
      { type: "down", startTime: T(3), endTime: ptTime, startPrice: closes[3], endPrice: closes[closes.length - 12] },
      { type: "up", startTime: ptTime, endTime: T(closes.length - 1), startPrice: closes[closes.length - 12], endPrice: closes[closes.length - 1] },
    ],
    "15": [],
  };

  test("点+强分型+均线分离 → 触发一次，开盘进场，止损价位精确", () => {
    const sigs = scanPeriodSignals("3", bars, periodBis, "15");
    assert.equal(sigs.length, 1);
    const s = sigs[0];
    assert.equal(s.strategyKey, "fx2Buy");
    assert.equal(s.direction, "long");
    assert.equal(s.pointTime, ptTime);
    assert.ok(s.entryIdx > closes.length - 12);  // 点后首个齐备拍（首个满足条件的收盘K线）
    assert.ok(s.standGap > 0);                    // 默认开：收盘站上 SMA5
    const done = applyMutexAndSimulate(sigs, { "3": bars });
    const t = done[0];
    assert.equal(t.entryTime, bars[s.entryIdx].time);
    assert.equal(t.entryPrice, bars[s.entryIdx].open);
    assert.equal(Math.abs(t.stopRef - (t.entryPrice - 10)), 0);
    assert.equal(Math.abs(t.tpRef - (t.entryPrice + 30)), 0);
  });

  test("点有效期(值)：盘中价距点极值超距 → 全程不出信号（等待不作废）", () => {
    // 点价 94；点后各评估根 high（≥97）距点价 ≥3 > 2 → 每拍都在价距闸门等待，
    // 点后无一根回到范围内 → 无信号（与根数版不同：无作废闩锁，纯逐拍等待）
    const sigs = scanPeriodSignals("3", bars, periodBis, "15",
                                   { ...MODULE_OPTS, pointValidPts: 2 });
    assert.equal(sigs.length, 0);
  });

  test("点有效期(值)：超距等待后价格回到范围内 → 同一点触发（不闩锁）", () => {
    // 主 fixture 尾部加深回撤：末根（收 93，high=95.2）距点价 94 仅 1.2 ≤ 2；
    // 关掉强分型/均线/站线（隔离价距闸门）：点根(漂移3>2)与反弹段全被拦，
    // 回撤到末根才回范围内 → 恰在末根收盘触发、点仍是原 2买
    const c3 = closes.concat([119, 115, 111, 107, 103, 99, 95, 93]);
    const bars3 = barsFromCloses(0, c3);
    const bis3 = {
      "3": [
        { type: "up", startTime: T(0), endTime: T(1), startPrice: c3[0], endPrice: c3[1] },
        { type: "down", startTime: T(1), endTime: T(2), startPrice: c3[1], endPrice: 90 },
        { type: "up", startTime: T(2), endTime: T(3), startPrice: 90, endPrice: c3[3] },
        { type: "down", startTime: T(3), endTime: ptTime, startPrice: c3[3], endPrice: c3[closes.length - 12] },
        { type: "up", startTime: ptTime, endTime: T(32), startPrice: c3[closes.length - 12], endPrice: c3[32] },
      ],
      "15": [],
    };
    const sigs = scanPeriodSignals("3", bars3, bis3, "15",
                                   { ...MODULE_OPTS, strongFxOn: false, maOn: false,
                                     maStandOn: false, pointValidPts: 2 });
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].pointTime, ptTime);
    assert.equal(sigs[0].direction, "long");
    assert.equal(sigs[0].entryIdx, c3.length);  // 末根(i=40)收盘触发 → 成交K线=下一根(41)
  });

  test("收盘站线：齐备拍急跌收盘跌回 SMA5 下方不出信号，再站上后触发（standGap>0）", () => {
    // 上涨→下跌→V反→短反弹3根（分离未到2）→急跌一根（分离≥2 成立但收盘<SMA5 → 站线拦下）
    // →再拉开：首个「站上且分离≥2」拍=急跌后第 2 根触发（模块默认 maStandOn 开、周期 5）
    const c2 = [];
    for (let i = 0; i < 12; i++) c2.push(100 + 2.0 * i);
    for (let i = 0; i < 10; i++) c2.push(c2[c2.length - 1] - 2.8);
    c2.push(c2[c2.length - 1] + 9.0);
    for (let i = 0; i < 3; i++) c2.push(c2[c2.length - 1] + 2.0);
    const dipIdx = c2.length;
    c2.push(c2[c2.length - 1] - 5.0);
    for (let i = 0; i < 8; i++) c2.push(c2[c2.length - 1] + 2.0);
    const bars2 = barsFromCloses(0, c2);
    const ptTime2 = bars2[21].time;
    const T2 = (i) => bars2[i].time;
    const periodBis2 = {
      "3": [
        { type: "up", startTime: T2(0), endTime: T2(1), startPrice: c2[0], endPrice: c2[1] },
        { type: "down", startTime: T2(1), endTime: T2(2), startPrice: c2[1], endPrice: 90 },
        { type: "up", startTime: T2(2), endTime: T2(3), startPrice: 90, endPrice: c2[3] },
        { type: "down", startTime: T2(3), endTime: ptTime2, startPrice: c2[3], endPrice: c2[21] },
        { type: "up", startTime: ptTime2, endTime: T2(c2.length - 1), startPrice: c2[21], endPrice: c2[c2.length - 1] },
      ],
      "15": [],
    };
    const sigs = scanPeriodSignals("3", bars2, periodBis2, "15");
    assert.equal(sigs.length, 1);
    const s = sigs[0];
    assert.equal(s.pointTime, ptTime2);
    assert.ok(s.signalTime > bars2[dipIdx].time + SEC);  // 急跌拍被站线闸住，信号在其后
    assert.equal(s.signalTime, bars2[dipIdx + 2].time + SEC);
    assert.ok(s.standGap > 0);
  });

  test("同根双触默认止损优先，盘中按触发价成交（sameBarPriority=stop 为模块默认）", () => {
    const sigs = scanPeriodSignals("3", bars, periodBis, "15");
    // 触发信号改为成交于原末根，随后一根大振幅K盘中同时触及止损与止盈 → 即时按止损位成交
    const last = bars[bars.length - 1];
    const wild = bar(last.time + SEC, last.close, last.close + 60, last.close - 60, last.close - 30);
    const bars2 = bars.concat([wild]);
    sigs[0].entryIdx = bars.length - 1;
    const done = applyMutexAndSimulate(sigs, { "3": bars2 });
    const t = done[0];
    assert.equal(t.exitType, "stop");
    assert.equal(t.exitTime, wild.time);
    assert.equal(Math.abs(t.exitPrice - (t.entryPrice - 10)), 0);   // 成交价=止损位（触发价）
  });

  test("global 互斥：同向第二信号被压制", () => {
    const s1 = scanPeriodSignals("3", bars, periodBis, "15");
    const s2 = { ...s1[0], signalTime: s1[0].signalTime + SEC, entryIdx: s1[0].entryIdx + 1 };
    const done = applyMutexAndSimulate([s1[0], s2], { "3": bars });
    assert.equal(done.filter(x => x.suppressed).length, 1);
  });
});

describe("applyMutexAndSimulate · structure 组合模式（opts 注入）", () => {
  const SEC = 180;
  const mkBars = (closes) => {
    const out = [];
    let prev = closes[0];
    for (let i = 0; i < closes.length; i++) {
      const o = i ? prev : closes[0] - 1;
      const c = closes[i];
      const hi = c > o ? c + 0.2 : o + 0.2;
      const lo = c < o ? c - 0.4 : (c > o ? o : o - 0.2);
      out.push({ time: i * SEC, open: o, high: hi, low: lo, close: c });
      prev = c;
    }
    return out;
  };
  const sig = (over = {}) => ({ periodX: "3", direction: "long", strategyKey: "fx2Buy",
    pointType: "2买", pointTime: SEC, pointPrice: 100,
    signalTime: 180, signalPrice: 100, entryIdx: 1, ...over });

  test("1/2类：主动止盈半份部分平仓 + 剩余走 3买提损跟踪（trailStop）", () => {
    // 进场@100（bars[1].open=100）：前高笔端点 130 → tpRef=130 平 1 手（半仓2的半份）；
    // 3买@125（bars[4] 触及后 bars[5] 起生效）→ 止损上移 124；bars[6] low ≤124 → trailStop
    const closes = [100, 101, 102, 130, 131, 128, 115, 104];
    const bars = mkBars(closes);
    const ends = [{ time: 0, price: 130, side: "high" }];
    const pts = [
      { type: "3买", time: bars[4].time, price: 125, side: "buy" },
    ];
    const done = applyMutexAndSimulate([sig()], { "3": bars }, 1.0,
      { tpMode: "structure", lots: 4, tpNearPts: 0, tpTrailSlipPts: 1,
        ptsByP: { "3": pts }, biEndsByP: { "3": ends } });
    const t = done[0];
    assert.equal(t.lots, 2);                       // 半仓
    assert.equal(t.tpLots, 1);                     // 1/4 仓主动止盈
    assert.equal(t.tpTarget.type, "前高");         // 目标=笔端点（非买卖点）
    assert.equal(t.exits[0].type, "activeTp");     // bars[3] high 130.2 ≥ 130 → @130 平1手
    assert.equal(Math.abs(t.exits[0].price - 130), 0);  // （tpRef 一次性已清空，验事件价）
    assert.equal(t.exits[0].lots, 1);
    assert.equal(t.lotsLeft, 1);
    assert.ok(t.trailRaised);                      // 3买提损 → 止损 90→124
    assert.equal(Math.abs(t.stopRef - 124), 0);
    assert.equal(t.exitType, "trailStop");
    assert.equal(Math.abs(t.exitPrice - 124), 0);
    assert.equal(Math.abs(t.pnl - ((130 - 100) * 1 + (124 - 100) * 1)) < 1e-9, true);
  });

  test("3类：触及目标全平（activeTp 终局），不提损", () => {
    const closes = [100, 101, 102, 130, 131, 128];
    const bars = mkBars(closes);
    const ends = [{ time: 0, price: 130, side: "high" }];
    const pts = [
      { type: "4买", time: bars[4].time, price: 125, side: "buy" },
    ];
    const done = applyMutexAndSimulate([sig({ pointType: "3买", strategyKey: "fx3Buy" })],
      { "3": bars }, 1.0,
      { tpMode: "structure", lots: 4, tpNearPts: 0, tpTrailSlipPts: 1,
        ptsByP: { "3": pts }, biEndsByP: { "3": ends } });
    const t = done[0];
    assert.equal(t.tpLots, 2);                     // 3类=整笔全平
    assert.equal(t.exitType, "activeTp");
    assert.equal(Math.abs(t.pnl - (130 - 100) * 2), 0);
    assert.equal(t.trailRaised, false);            // 3类不提损
  });

  test("奇数手整手口径：lots=5 → 半仓⌊5/2⌋=2、半份止盈 max(1,⌊2/2⌋)=1（与 py 同口径）", () => {
    const closes = [100, 101, 102, 130, 131, 128];
    const bars = mkBars(closes);
    const ends = [{ time: 0, price: 130, side: "high" }];
    const done = applyMutexAndSimulate([sig()], { "3": bars }, 1.0,
      { tpMode: "structure", lots: 5, tpNearPts: 0,
        ptsByP: { "3": [] }, biEndsByP: { "3": ends } });
    const t = done[0];
    assert.equal(t.lots, 2);                       // ⌊5/2⌋ 半仓整手
    assert.equal(t.tpLots, 1);                     // max(1, ⌊2/2⌋)
    assert.ok(Number.isInteger(t.lots) && Number.isInteger(t.tpLots));
    assert.equal(t.exits[0].lots, 1);
    assert.ok(Number.isInteger(t.lotsLeft));
  });

  test("structure lots<2 拒绝（半仓整手 ≥1 手）", () => {
    const closes = [100, 101, 102];
    const bars = mkBars(closes);
    assert.throws(() => applyMutexAndSimulate([sig()], { "3": bars }, 1.0,
      { tpMode: "structure", lots: 1, ptsByP: { "3": [] } }), /须 ≥2/);
  });

  test("目标取笔端点而非反向买卖点：前卖点 125 在场仍取前高 130", () => {
    // trade#11 案例口径：卖点识别滞后，前高笔端点先可用 → 目标必须是笔端点价
    const closes = [100, 101, 102, 130, 131, 128];
    const bars = mkBars(closes);
    const pts = [{ type: "1卖", time: 0, price: 125, side: "sell" }];
    const ends = [{ time: 0, price: 130, side: "high" }];
    const done = applyMutexAndSimulate([sig()], { "3": bars }, 1.0,
      { tpMode: "structure", lots: 4, tpNearPts: 0,
        ptsByP: { "3": pts }, biEndsByP: { "3": ends } });
    const t = done[0];
    // tpRef 触发后一次性清空 → 验 tpTarget 与事件价（非卖点 125）
    assert.deepEqual(t.tpTarget, { type: "前高", time: 0, price: 130 });
    assert.equal(t.exits[0].type, "activeTp");
    assert.equal(Math.abs(t.exits[0].price - 130), 0);
  });

  test("容量制：同向两笔半仓可叠（不同粗类）、第三笔容量压制（lots=4 容量）", () => {
    const closes = [100, 101, 102, 103, 104, 105];
    const bars = mkBars(closes);
    const s2 = sig({ pointType: "3买", strategyKey: "fx3Buy",
                     signalTime: 360, entryIdx: 2 });
    const s3 = sig({ pointType: "1买", strategyKey: "fx1Buy",
                     signalTime: 540, entryIdx: 3 });
    const done = applyMutexAndSimulate([sig(), s2, s3], { "3": bars }, 1.0,
      { tpMode: "structure", lots: 4, ptsByP: { "3": [] } });
    const filled = done.filter(x => !x.suppressed);
    assert.equal(filled.length, 2);                // 2类+3类 两笔半仓=容量满
    assert.equal(done.filter(x => x.suppressed).length, 1);  // 1类被容量压制
    assert.ok(filled.every(x => x.lots === 2));
    assert.ok(filled.every(x => x.state === "open"));  // 未触发 → 仍持仓（各占容量）
  });

  test("同粗类闸门：同类持仓中压制、不同粗类可叠", () => {
    const closes = [100, 101, 102, 103, 104, 105];
    const bars = mkBars(closes);
    // 1类 @180 成交；2类 @360 不同粗类成交；1类 @540 同类压制；
    // 类2买 @720 = 粗类2（第二笔是 2买）同类压制
    const s1 = sig({ pointType: "1买", strategyKey: "fx1Buy" });
    const s2 = sig({ pointType: "2买", signalTime: 360, entryIdx: 2 });
    const s3 = sig({ pointType: "1买", strategyKey: "fx1Buy",
                     signalTime: 540, entryIdx: 3 });
    const s4 = sig({ pointType: "类2买", strategyKey: "fx2xBuy",
                     signalTime: 720, entryIdx: 4 });
    const done = applyMutexAndSimulate([s1, s2, s3, s4], { "3": bars }, 1.0,
      { tpMode: "structure", lots: 4, ptsByP: { "3": [] } });
    assert.equal(done[0].suppressed, undefined);   // 1类成交
    assert.equal(done[1].suppressed, undefined);   // 2类不同粗类成交
    assert.equal(done[2].suppressed, true);        // 1类同类压制
    assert.equal(done[3].suppressed, true);        // 类2买=粗类2 同类压制
  });
});

describe("collectBiEnds · 已确认笔端点流", () => {
  test("上笔终点=前高/下笔终点=前低，升序，_forming 跳过", () => {
    const bis = [
      { type: "up", startTime: 100, startPrice: 90, endTime: 200, endPrice: 130 },
      { type: "down", startTime: 200, startPrice: 130, endTime: 300, endPrice: 110 },
      { type: "up", startTime: 300, startPrice: 110, endTime: 400,
        endPrice: 140, _forming: true },
    ];
    assert.deepEqual(collectBiEnds("3", { "3": bis }), [
      { time: 200, price: 130, side: "high" },
      { time: 300, price: 110, side: "low" },
    ]);
    assert.deepEqual(collectBiEnds("3", {}), []);   // 无笔 → 空
  });
});

describe("parseFibLevels", () => {
  test("解析/去重/非法档位", () => {
    assert.deepEqual(parseFibLevels("0.382,0.5,0.618"), [0.382, 0.5, 0.618]);
    assert.deepEqual(parseFibLevels("0.5,0.5"), [0.5]);
    for (const bad of ["", "0", "1", "1.5", "abc"]) {
      assert.throws(() => parseFibLevels(bad));
    }
  });
});

describe("scanPeriodSignals · 条件组合（三选N：必选硬门槛 + 票数≥生效N，与 py 同构）", () => {
  // 与上组同构行情（上涨→下跌→V反→反弹；2买 @ bars[21]，尾部三条件齐备）
  const closes = [];
  for (let i = 0; i < 12; i++) closes.push(100 + 2.0 * i);
  for (let i = 0; i < 10; i++) closes.push(closes[closes.length - 1] - 2.8);
  closes.push(closes[closes.length - 1] + 9.0);
  for (let i = 0; i < 10; i++) closes.push(closes[closes.length - 1] + 2.0);
  const bars = barsFromCloses(0, closes);
  const ptTime = bars[21].time;
  const T = (i) => bars[i].time;
  const periodBis = {
    "3": [
      { type: "up", startTime: T(0), endTime: T(1), startPrice: closes[0], endPrice: closes[1] },
      { type: "down", startTime: T(1), endTime: T(2), startPrice: closes[1], endPrice: 90 },
      { type: "up", startTime: T(2), endTime: T(3), startPrice: 90, endPrice: closes[3] },
      { type: "down", startTime: T(3), endTime: ptTime, startPrice: closes[3], endPrice: closes[21] },
      { type: "up", startTime: ptTime, endTime: T(closes.length - 1), startPrice: closes[21], endPrice: closes[closes.length - 1] },
    ],
    "15": [],
  };
  const pick = (n) => ({ ...MODULE_OPTS.entryPick, "2": n });

  test("默认（全启用+全必选+N=3）= 旧 AND 行为：单条件破坏不触发", () => {
    const sigs = scanPeriodSignals("3", bars, periodBis, "15",
                                   { ...MODULE_OPTS, crossMinPts: 1e6 });
    assert.equal(sigs.length, 0);
  });

  test("可选+三选二：破坏均线后 强分型+站线 2 票仍触发", () => {
    const sigs = scanPeriodSignals("3", bars, periodBis, "15",
                                   { ...MODULE_OPTS, crossMinPts: 1e6,
                                     maReq: false, entryPick: pick(2) });
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].strategyKey, "fx2Buy");
  });

  test("必选硬门槛：强分型必选未过 → 即使 N=1 也不触发", () => {
    const sigs = scanPeriodSignals("3", bars, periodBis, "15",
                                   { ...MODULE_OPTS, strongFxMinPts: 1e6,
                                     maReq: false, maStandReq: false,
                                     entryPick: pick(1) });
    assert.equal(sigs.length, 0);
  });

  test("停用收缩生效N：N=3 停用均线 → min(3,2)=2，强分型+站线触发", () => {
    const sigs = scanPeriodSignals("3", bars, periodBis, "15",
                                   { ...MODULE_OPTS, maOn: false });
    assert.equal(sigs.length, 1);
  });

  test("三选一 + 单条件：只留强分型 → 1/1 触发", () => {
    const sigs = scanPeriodSignals("3", bars, periodBis, "15",
                                   { ...MODULE_OPTS, maOn: false, maStandOn: false,
                                     entryPick: pick(1) });
    assert.equal(sigs.length, 1);
  });
});

describe("scanPeriodSignals · 黄金分割附近/上级同向（opts 注入，默认关）", () => {
  // 与上组同构行情；pt = bars[21]（V 反前低点）@94，摆动窗 bars[4..21] 极值 122.2
  const closes = [];
  for (let i = 0; i < 12; i++) closes.push(100 + 2.0 * i);
  for (let i = 0; i < 10; i++) closes.push(closes[closes.length - 1] - 2.8);
  closes.push(closes[closes.length - 1] + 9.0);
  for (let i = 0; i < 10; i++) closes.push(closes[closes.length - 1] + 2.0);
  const bars = barsFromCloses(0, closes);
  const ptTime = bars[21].time;
  const T = (i) => bars[i].time;
  const ext = Math.max(...bars.slice(4, 22).map(b => b.high));
  // 前置虚构历史造「前一同侧点」：底 70 → 底 prevEnd（更高）→ 2买@T(3)；
  // 其后再回落到 94（更高底）→ 类2买@ptTime（探查实证两点的类型与位置）
  const mkBis = (prevEnd) => ({
    "3": [
      { type: "down", startTime: T(0), endTime: T(1), startPrice: 130, endPrice: 70 },
      { type: "up", startTime: T(1), endTime: T(2), startPrice: 70, endPrice: 105 },
      { type: "down", startTime: T(2), endTime: T(3), startPrice: 105, endPrice: prevEnd },
      { type: "up", startTime: T(3), endTime: T(4), startPrice: prevEnd, endPrice: 108 },
      { type: "down", startTime: T(4), endTime: ptTime, startPrice: 108, endPrice: 94 },
      { type: "up", startTime: ptTime, endTime: T(32), startPrice: 94, endPrice: closes[32] },
    ],
    "15": [],
  });

  test("fibNear：前点摆动的 0.618 位=点价 → 触发（带 fibLevel 证据；前点自身无锚点被拦）", () => {
    const prevEnd = ext - (ext - 94) / 0.618;   // 0.618 回撤位恰为 94
    const sigs = scanPeriodSignals("3", bars, mkBis(prevEnd), "15",
      { ...MODULE_OPTS, fibNearOn: true });
    assert.equal(sigs.length, 1);               // 只有最新点触发；前点 2买 无前一同侧点被拦
    const s = sigs[0];
    assert.equal(s.pointTime, ptTime);
    assert.ok(Math.abs(s.fibLevel - 0.618) < 1e-9);
    assert.ok(s.fibGap <= 5);
  });

  test("fibNear：点价远离全部档位 → 不触发；1 类点豁免（py 侧同构覆盖，此处验 2/3 类拦下）", () => {
    const sigs = scanPeriodSignals("3", bars, mkBis(85), "15",
      { ...MODULE_OPTS, fibNearOn: true });     // 摆动 85→122.2，最近档位(0.618)≈99.2 距 94 达 5.2>5
    assert.equal(sigs.length, 0);
  });

  test("fibNear：无前一同侧点（原始单点 fixture）→ 不触发", () => {
    const sigs = scanPeriodSignals("3", bars, periodBisSingle(), "15",
      { ...MODULE_OPTS, fibNearOn: true });
    assert.equal(sigs.length, 0);
  });

  test("upperDir：上级当前笔同向触发/反向或无笔拦下；默认关跳过", () => {
    const mkUpper = (bi) => ({ ...mkBis(76.6), "15": bi ? [bi] : [] });
    const upBis = mkUpper({ type: "up", startTime: T(0), endTime: T(32), startPrice: 70, endPrice: 130 });
    const downBis = mkUpper({ type: "down", startTime: T(0), endTime: T(32), startPrice: 130, endPrice: 70 });
    const noneBis = mkUpper(null);
    const on = { ...MODULE_OPTS, upperDirOn: true };
    const s1 = scanPeriodSignals("3", bars, upBis, "15", on);
    assert.equal(s1.length, 1);
    assert.equal(s1[0].upperDir, "up");
    // 上级 down 笔：买侧全拦；卖侧对称放行（down 笔令 fixture 生成 2卖，短信号合法触发）
    const s2 = scanPeriodSignals("3", bars, downBis, "15", on);
    assert.equal(s2.filter(s => s.direction === "long").length, 0);
    assert.equal(s2.length, 1);
    assert.equal(s2[0].direction, "short");
    assert.equal(s2[0].upperDir, "down");
    assert.equal(scanPeriodSignals("3", bars, noneBis, "15", on).length, 0);  // 上级无笔全拦
    assert.equal(scanPeriodSignals("3", bars, noneBis, "15").length, 1);      // 默认关：放行
  });

  test("类别拆分：类2买→2x（fx2xBuy）与严格 2 分开选（与 py 侧同构）", () => {
    // mkBis(85) 最新买点=类2买@ptTime（探查实证）；单点 fixture=2买@ptTime
    const only2 = { ...MODULE_OPTS, pointClasses: new Set(["2"]) };
    const only2x = { ...MODULE_OPTS, pointClasses: new Set(["2x"]) };
    assert.equal(scanPeriodSignals("3", bars, mkBis(85), "15", only2).length, 0);      // 类2买被拦
    const s = scanPeriodSignals("3", bars, mkBis(85), "15", only2x);
    assert.equal(s.length, 1);
    assert.equal(s[0].strategyKey, "fx2xBuy");
    assert.equal(s[0].pointType, "类2买");
    assert.equal(scanPeriodSignals("3", bars, periodBisSingle(), "15", only2x).length, 0);  // 2买被拦
    const d = scanPeriodSignals("3", bars, mkBis(85), "15");                          // 默认全选放行
    assert.equal(d.length, 1);
    assert.equal(d[0].strategyKey, "fx2xBuy");
  });

  // 原始单点 fixture（与首组相同的 5 笔结构：单 2买@ptTime，无更早同侧点）
  function periodBisSingle() {
    return {
      "3": [
        { type: "up", startTime: T(0), endTime: T(1), startPrice: closes[0], endPrice: closes[1] },
        { type: "down", startTime: T(1), endTime: T(2), startPrice: closes[1], endPrice: 90 },
        { type: "up", startTime: T(2), endTime: T(3), startPrice: 90, endPrice: closes[3] },
        { type: "down", startTime: T(3), endTime: ptTime, startPrice: closes[3], endPrice: 94 },
        { type: "up", startTime: ptTime, endTime: T(32), startPrice: 94, endPrice: closes[32] },
      ],
      "15": [],
    };
  }
});

describe("scanPeriodSignals · 次级别/次次级别背驰（opts+lowerCtx 注入，默认关；真实 lowerDiverge）", () => {
  // 与首组同构行情：2买@ptTime=3780（bars[21]）；pt 前跌到 90、V 反后回调只到 94
  const closes = [];
  for (let i = 0; i < 12; i++) closes.push(100 + 2.0 * i);
  for (let i = 0; i < 10; i++) closes.push(closes[closes.length - 1] - 2.8);
  closes.push(closes[closes.length - 1] + 9.0);
  for (let i = 0; i < 10; i++) closes.push(closes[closes.length - 1] + 2.0);
  const bars = barsFromCloses(0, closes);
  const ptTime = bars[21].time;
  const T = (i) => bars[i].time;
  const periodBis = {
    "3": [
      { type: "up", startTime: T(0), endTime: T(1), startPrice: closes[0], endPrice: closes[1] },
      { type: "down", startTime: T(1), endTime: T(2), startPrice: closes[1], endPrice: 90 },
      { type: "up", startTime: T(2), endTime: T(3), startPrice: 90, endPrice: closes[3] },
      { type: "down", startTime: T(3), endTime: ptTime, startPrice: closes[3], endPrice: 94 },
      { type: "up", startTime: ptTime, endTime: T(32), startPrice: 94, endPrice: closes[32] },
    ],
    "15": [],
  };

  // lowerCtx（真实走 mark_entry.lowerDiverge 区间套下沉链）：P=3，容器 3m down 笔
  // [t0, t0+780]；30S 容器内 4 段展开（≥3），末段 d2 创新低（104<107）+ DIF 低点抬高
  // （-0.5 > -3.0）+ 绿柱面积缩小（2.3 < 9.0，时长可比）→ 30S 底背驰点 @t0+780，
  // 下沉停止级=30S（次级别）。t0=3000 → 背驰点 3780 = 点同刻（同极值口径）。
  const bi = (type, startTime, endTime, startPrice, endPrice) => ({
    type, startTime, endTime, startPrice, endPrice, span: Math.abs(endPrice - startPrice),
  });
  const macd = (tv) => tv.map(([t, m, d]) => ({ time: t, macd: m, dif: d, dea: 0 }));
  const mkLowerCtx = (t0) => {
    const t = (off) => t0 + off;
    return {
      "3": { bis: [bi("up", t(-1000), t(0), 100.0, 110.0), bi("down", t(0), t(780), 110.0, 100.0)] },
      "30S": {
        // B 开关（divergeReferByZs 默认开）：u1/d2/u2 价格重叠成中枢，d1=入中枢段
        // （refer，绿柱面积 9.0、difLow -3.0）；d3=离开段创新低 104<107 + 动能衰竭
        // （面积 2.3、difLow -1.2）→ 底背驰点 @t+780，下沉停止级=30S
        bis: [
          bi("down", t(-1000), t(0), 116.0, 114.0),   // 容器外（不计展开）
          bi("up", t(0), t(200), 114.0, 117.0),
          bi("down", t(200), t(400), 117.0, 107.0),   // d1 入中枢段（参照）
          bi("up", t(400), t(460), 107.0, 111.0),     // ┐
          bi("down", t(460), t(560), 111.0, 107.5),   // │ 中枢（重叠区 107.5~110.5）
          bi("up", t(560), t(640), 107.5, 110.5),     // ┘
          bi("down", t(640), t(780), 110.5, 104.0),   // d3 离开段（背驰候选）
        ],
        macdArr: macd([
          [t(250), -5.0, -3.0], [t(350), -4.0, -2.5],          // d1 窗
          [t(660), -1.0, -1.2], [t(700), -0.8, -0.8], [t(770), -0.5, -0.5],  // d3 窗
        ]),
      },
    };
  };
  const divOn = { ...MODULE_OPTS, divLowerOn: true };

  test("唯一触发+命中（背驰点=点同刻，同极值口径）→ 触发，证据字段齐全", () => {
    const sigs = scanPeriodSignals("3", bars, periodBis, "15", divOn, mkLowerCtx(3000));
    assert.equal(sigs.length, 1);
    const s = sigs[0];
    assert.equal(s.pointTime, ptTime);
    assert.equal(s.divLowerRes, "30S");
    assert.equal(s.divLowerTime, ptTime);   // t0+780 = 3780
  });

  test("默认关：lowerCtx 在场也不影响（与旧基线一致）", () => {
    assert.equal(scanPeriodSignals("3", bars, periodBis, "15", MODULE_OPTS, mkLowerCtx(3000)).length, 1);
    assert.equal(scanPeriodSignals("3", bars, periodBis, "15").length, 1);   // 无 lowerCtx 同样放行
  });

  test("互斥：窗口外候选（背驰点早于点前1根）→ 三条件全过也拦下（不回退条件组合）", () => {
    // t0=2000 → 背驰点 2780 < ptTime−180=3600：点之前的旧背驰不算确认
    assert.equal(scanPeriodSignals("3", bars, periodBis, "15", divOn, mkLowerCtx(2000)).length, 0);
  });

  test("互斥：无更低级别数据（lowerCtx 缺 30S）→ 拦下（同引擎 P=3 无 30S 语义）", () => {
    const ctxOnly3 = { "3": mkLowerCtx(3000)["3"] };
    assert.equal(scanPeriodSignals("3", bars, periodBis, "15", divOn, ctxOnly3).length, 0);
  });

  test("防未来：快照中的未来背驰点（晚于全部评估拍）不计入 → 拦下", () => {
    // t0=13000 → 背驰点 13780 > 末拍 closeT（T(32)+180=5940）：逐拍评估时还不可知
    assert.equal(scanPeriodSignals("3", bars, periodBis, "15", divOn, mkLowerCtx(13000)).length, 0);
  });
});
