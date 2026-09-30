/**
 * fxma-entry 纯函数单元测试（与 py_chain/test_fx_ma.py 对齐）
 *
 * 覆盖：maSeries（SMA/EMA）、strongFxAfter（实体口径+minPts+确认时刻）、
 * scanPeriodSignals（点→强分型→均线分离 齐备才触发；点/类别过滤——用注入笔
 * 构造 2买，其余口径由 py 侧同构用例覆盖）、applyMutexAndSimulate
 * （global 互斥 + 止损/止盈下一开盘成交 + 同根双触优先级）。
 *
 * 注意：脚本顶层解析 CLI 参数（默认值即测试口径），require 侧不连 CDP（main
 * 仅 require.main===module 时执行）。
 *
 * 运行：node --test .cursor/skills/fxma-entry/scripts/fxma_entry.test.js
 */
const { describe, test } = require("node:test");
const assert = require("node:assert/strict");
const { maSeries, strongFxAfter, scanPeriodSignals, applyMutexAndSimulate } =
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

  test("点+强分型+均线分离 → 触发一次，下一开盘成交，止损价位精确", () => {
    const sigs = scanPeriodSignals("3", bars, periodBis, "15");
    assert.equal(sigs.length, 1);
    const s = sigs[0];
    assert.equal(s.strategyKey, "fx2Buy");
    assert.equal(s.direction, "long");
    assert.equal(s.pointTime, ptTime);
    assert.ok(s.entryIdx > closes.length - 12);  // 点后首个齐备拍（首个满足条件的收盘K线）
    const done = applyMutexAndSimulate(sigs, { "3": bars });
    const t = done[0];
    assert.equal(t.entryTime, bars[s.entryIdx].time);
    assert.equal(t.entryPrice, bars[s.entryIdx].open);
    assert.equal(Math.abs(t.stopRef - (t.entryPrice - 10)), 0);
    assert.equal(Math.abs(t.tpRef - (t.entryPrice + 30)), 0);
  });

  test("同根双触默认止损优先（sameBarPriority=stop 为模块默认）", () => {
    const sigs = scanPeriodSignals("3", bars, periodBis, "15");
    // 触发信号改为成交于原末根，随后一根大振幅K同时触及止损与止盈，再一根承接开盘成交
    const last = bars[bars.length - 1];
    const wild = bar(last.time + SEC, last.close, last.close + 60, last.close - 60, last.close - 30);
    const fillBar = bar(wild.time + SEC, wild.close - 2, wild.close - 1, wild.close - 3, wild.close - 2);
    const bars2 = bars.concat([wild, fillBar]);
    sigs[0].entryIdx = bars.length - 1;
    const done = applyMutexAndSimulate(sigs, { "3": bars2 });
    assert.equal(done[0].exitType, "stop");
    assert.equal(done[0].exitPrice, fillBar.open);
  });

  test("global 互斥：同向第二信号被压制", () => {
    const s1 = scanPeriodSignals("3", bars, periodBis, "15");
    const s2 = { ...s1[0], signalTime: s1[0].signalTime + SEC, entryIdx: s1[0].entryIdx + 1 };
    const done = applyMutexAndSimulate([s1[0], s2], { "3": bars });
    assert.equal(done.filter(x => x.suppressed).length, 1);
  });
});
