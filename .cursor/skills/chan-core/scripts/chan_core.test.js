/**
 * chan-core 缠论算法核心单元测试
 *
 * 依据 SPEC.md（`.cursor/skills/chan-core/SPEC.md`）的行为契约编写，
 * 把 SPEC 中每个导出函数的关键行为固化为可执行用例，形成回归保护基线。
 * 数据均为手工构造、可独立验算的最小示例。
 *
 * 运行：node --test .cursor/skills/chan-core/scripts/chan_core.test.js
 */
const { describe, test } = require("node:test");
const assert = require("node:assert/strict");
const core = require("./chan_core.js");

// 构造一根原始K线
const bar = (t, h, l, c) => ({ time: t, open: c, high: h, low: l, close: c });

// 构造一根合并K线（含跳空检测/端点修正所需字段）
const mk = (t, h, l, opts = {}) => ({
  time: t, high: h, low: l,
  _rawCount: opts.rawCount !== undefined ? opts.rawCount : 1,
  highTime: opts.highTime || t, lowTime: opts.lowTime || t,
  rawHigh: opts.rawHigh !== undefined ? opts.rawHigh : h,
  rawLow: opts.rawLow !== undefined ? opts.rawLow : l,
  rawHighTime: opts.rawHighTime || t, rawLowTime: opts.rawLowTime || t,
});

// ============================================================
// 1. 包含关系处理（SPEC 2.1）
// ============================================================

describe("mergeBars 包含关系处理", () => {
  test("无包含关系时逐根原样输出", () => {
    const bars = [bar(1, 100, 90), bar(2, 110, 92), bar(3, 115, 95)];
    const merged = core.mergeBars(bars);
    assert.equal(merged.length, 3);
    assert.equal(merged[2]._rawCount, 1);
    assert.equal(merged[2].rawLow, 95);
  });

  test("向上趋势中的包含合并取「高高」", () => {
    const bars = [bar(1, 100, 90), bar(2, 110, 92), bar(3, 112, 88)];
    const merged = core.mergeBars(bars);
    assert.equal(merged.length, 2);
    assert.equal(merged[1].high, 112); // 高点取更高
    assert.equal(merged[1].low, 92);   // 低点取更高（88 更低不更新）
    assert.equal(merged[1]._rawCount, 2);
  });

  test("向下趋势中的包含合并取「低低」", () => {
    const bars = [bar(1, 110, 90), bar(2, 100, 80), bar(3, 95, 85)];
    const merged = core.mergeBars(bars);
    assert.equal(merged.length, 2);
    assert.equal(merged[1].high, 95);  // 高点取更低
    assert.equal(merged[1].low, 80);   // 低点取更低（85 更高不更新）
    assert.equal(merged[1]._rawCount, 2);
  });

  test("前序方向未定时包含默认向上合并", () => {
    const bars = [bar(1, 100, 90), bar(2, 108, 85)];
    const merged = core.mergeBars(bars);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].high, 108);
    assert.equal(merged[0].low, 90);
    assert.equal(merged[0]._rawCount, 2);
  });

  test("连续三根包含时 _rawCount 逐根累加且真实极值保留", () => {
    const bars = [bar(1, 100, 90), bar(2, 105, 85), bar(3, 110, 80)];
    const merged = core.mergeBars(bars);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].high, 110);
    assert.equal(merged[0].low, 90);
    assert.equal(merged[0]._rawCount, 3);
    // SPEC：rawHigh/rawLow 保留覆盖原始K线的真实极值（不受合并方向影响）
    assert.equal(merged[0].rawHigh, 110);
    assert.equal(merged[0].rawLow, 80);
    assert.equal(merged[0].rawLowTime, 3);
  });
});

describe("countRaw 覆盖原始K线数（SPEC 2.1）", () => {
  test("统计 (startIdx, endIdx] 的 _rawCount 累加", () => {
    const merged = [
      mk(1, 100, 90, { rawCount: 1 }),
      mk(2, 102, 91, { rawCount: 2 }),
      mk(3, 104, 92, { rawCount: 3 }),
      mk(4, 106, 93, { rawCount: 1 }),
    ];
    assert.equal(core.countRaw(merged, 0, 3), 2 + 3 + 1);
    assert.equal(core.countRaw(merged, 1, 2), 3);
    assert.equal(core.countRaw(merged, 0, 0), 0); // 空区间
  });
});

// ============================================================
// 2. 分型识别（SPEC 2.2）
// ============================================================

describe("findFractals 分型识别", () => {
  test("识别交替的底/顶分型，time 取极值原始K线时间", () => {
    const merged = [
      mk(1, 103, 93, { lowTime: 1 }),
      mk(2, 100, 88, { lowTime: 2, highTime: 2 }),   // 底分型
      mk(3, 105, 90, { highTime: 3 }),
      mk(4, 110, 95, { highTime: 4, lowTime: 4 }),   // 顶分型
      mk(5, 108, 94, { lowTime: 5 }),
      mk(6, 107, 82, { lowTime: 6, highTime: 6 }),   // 底分型
      mk(7, 109, 85, { highTime: 7 }),
    ];
    const fs = core.findFractals(merged);
    assert.equal(fs.length, 3);
    assert.deepEqual(fs[0], { mergedIdx: 1, type: "bottom", high: 100, low: 88, time: 2 });
    assert.deepEqual(fs[1], { mergedIdx: 3, type: "top", high: 110, low: 95, time: 4 });
    assert.deepEqual(fs[2], { mergedIdx: 5, type: "bottom", high: 107, low: 82, time: 6 });
  });

  test("单调序列不产生分型", () => {
    const merged = [
      mk(1, 100, 90), mk(2, 102, 91), mk(3, 104, 92), mk(4, 106, 93),
    ];
    assert.equal(core.findFractals(merged).length, 0);
  });
});

// ============================================================
// 3. 跳空检测（SPEC 2.3）
// ============================================================

describe("hasGapBetween 跳空检测", () => {
  test("无跳空返回 false", () => {
    const merged = [mk(1, 100, 90), mk(2, 103, 98)];
    assert.equal(core.hasGapBetween(merged, 0, 1, 10, 1), false);
  });

  test("向上跳空超过阈值返回 true", () => {
    const merged = [mk(1, 100, 90), mk(2, 115, 112)];
    assert.equal(core.hasGapBetween(merged, 0, 1, 10, 1), true);
  });

  test("向下跳空超过阈值返回 true", () => {
    const merged = [mk(1, 115, 110), mk(2, 100, 95)];
    assert.equal(core.hasGapBetween(merged, 0, 1, 10, 1), true);
  });

  test("缺口低于阈值返回 false（gapFilter 缩放）", () => {
    const merged = [mk(1, 100, 90), mk(2, 112, 106)];
    // 向上缺口 106-100=6 < 10（gapFilter=1, atr=10）
    assert.equal(core.hasGapBetween(merged, 0, 1, 10, 1), false);
    // gapFilter=0.5 时阈值 5 → 6 >= 5 成立
    assert.equal(core.hasGapBetween(merged, 0, 1, 10, 0.5), true);
  });

  test("用真实极值（rawHigh/rawLow）判断，合并掩盖不产生假缺口", () => {
    // 合并K线 high/low 因向下合并压低，但真实极值 rawHigh 更高 → 无真实跳空
    const merged = [
      mk(1, 100, 90, { rawHigh: 100, rawLow: 90 }),
      mk(2, 100, 80, { rawHigh: 115, rawLow: 80 }),
    ];
    // 用合并值看：curLow- nextHigh = 90-100 = -10 无跳空；但 rawHigh=115 若与 nextLow 判... 
    // 真实判断：nextLow - curHigh = 80 - 100 < 0；curLow - nextHigh = 90 - 115 < 0 → 无跳空
    assert.equal(core.hasGapBetween(merged, 0, 1, 10, 1), false);
  });
});

// ============================================================
// 4. 笔构建（SPEC 2.4）
// ============================================================

describe("buildBi 笔构建", () => {
  // 13 根合并K线：底(0)→顶(4)→底(8)→顶(12)，相邻分型间隔 4
  const merged = [
    mk(1, 100, 90), mk(2, 102, 92), mk(3, 104, 93), mk(4, 106, 95),
    mk(5, 108, 96), mk(6, 106, 95), mk(7, 104, 94), mk(8, 102, 92),
    mk(9, 100, 90), mk(10, 102, 91), mk(11, 104, 93), mk(12, 106, 95),
    mk(13, 108, 96),
  ];
  const fractals = [
    { mergedIdx: 0, type: "bottom", high: 100, low: 90, time: 1 },
    { mergedIdx: 4, type: "top", high: 108, low: 96, time: 5 },
    { mergedIdx: 8, type: "bottom", high: 100, low: 90, time: 9 },
    { mergedIdx: 12, type: "top", high: 108, low: 96, time: 13 },
  ];

  test("相邻分型间隔≥4 时两两连笔，类型交替、价格/rawCount/span 正确", () => {
    const bis = core.buildBi(fractals, merged, 1, [], null);
    assert.equal(bis.length, 3);
    assert.equal(bis[0].type, "up");
    assert.equal(bis[0].startPrice, 90);
    assert.equal(bis[0].endPrice, 108);
    assert.equal(bis[0].rawCount, 4);
    assert.equal(bis[0].span, 18);
    assert.equal(bis[1].type, "down");
    assert.equal(bis[1].startPrice, 108);
    assert.equal(bis[1].endPrice, 90);
    assert.equal(bis[1].span, 18);
    assert.equal(bis[2].type, "up");
    assert.equal(bis[2].startPrice, 90);
    assert.equal(bis[2].endPrice, 108);
  });

  test("间隔不足（<4）的中间分型被吞并，不成笔", () => {
    const m2 = [
      mk(1, 100, 90), mk(2, 103, 95), mk(3, 101, 92), mk(4, 98, 88),
    ];
    const f2 = [
      { mergedIdx: 0, type: "bottom", high: 100, low: 90, time: 1 },
      { mergedIdx: 1, type: "top", high: 103, low: 95, time: 2 },
      { mergedIdx: 3, type: "bottom", high: 98, low: 88, time: 4 },
    ];
    assert.equal(core.buildBi(f2, m2, 1, [], null).length, 0);
  });

  test("locked 端点（区间套锁定）不可被更高同类型分型替换", () => {
    const m3 = [
      mk(1, 100, 90), mk(2, 102, 92), mk(3, 104, 93), mk(4, 106, 95),
      mk(5, 108, 96), mk(6, 106, 95), mk(7, 104, 94), mk(8, 102, 92),
      mk(9, 100, 90), mk(10, 102, 91), mk(11, 104, 93), mk(12, 110, 97),
    ];
    const f3 = [
      { mergedIdx: 0, type: "bottom", high: 100, low: 90, time: 1 },
      { mergedIdx: 4, type: "top", high: 108, low: 96, time: 5 },
      { mergedIdx: 8, type: "bottom", high: 100, low: 90, time: 9 },
      { mergedIdx: 11, type: "top", high: 110, low: 97, time: 12 },
    ];
    // 上级锁定 top@108；11 处更高顶 110 间隔 3<4，回溯替换会被 locked 保护拦截
    const locked = [{ dir: "top", price: 108 }];
    const bis = core.buildBi(f3, m3, 1, [], locked);
    assert.equal(bis.length, 2);
    // 顶保持锁定值 108：更高的 110 不应出现在任何笔端点
    for (const b of bis) {
      assert.notEqual(b.startPrice, 110);
      assert.notEqual(b.endPrice, 110);
    }
    assert.equal(bis[1].type, "down");
    assert.equal(bis[1].endPrice, 90);
  });

  test("间隔不足但有跳空缺口时强制独立成笔（gapLocked）", () => {
    const m4 = [
      mk(1, 100, 90, { rawHigh: 100, rawLow: 90 }),
      mk(2, 102, 92, { rawHigh: 102, rawLow: 92 }),
      mk(3, 104, 93, { rawHigh: 104, rawLow: 93 }),
      mk(4, 106, 95, { rawHigh: 106, rawLow: 95 }),
      mk(5, 108, 96, { rawHigh: 108, rawLow: 96 }), // 顶（先有效接入）
      mk(6, 85, 78, { rawHigh: 85, rawLow: 78 }),   // 底：向下跳空 96-85=11 >= atr10
    ];
    const f4 = [
      { mergedIdx: 0, type: "bottom", high: 100, low: 90, time: 1 },
      { mergedIdx: 4, type: "top", high: 108, low: 96, time: 5 },
      { mergedIdx: 5, type: "bottom", high: 85, low: 78, time: 6 },
    ];
    const bis = core.buildBi(f4, m4, 10, [], null);
    assert.equal(bis.length, 2);
    assert.equal(bis[1].type, "down");
    assert.equal(bis[1].gapLocked, true);
    assert.equal(bis[1].endPrice, 78);
  });
});

describe("fixBiExtremes 端点极值修正（SPEC 2.4）", () => {
  test("终点分型后被合并掩盖的真实低点把端点下移，且下一笔起点联动", () => {
    const merged = [
      mk(1, 110, 100),
      mk(2, 106, 101),
      mk(3, 103, 99),   // 底端点
      mk(4, 102, 97),
      mk(5, 105, 96, { rawLow: 94, rawLowTime: 5 }), // 被掩盖：真实低点 94 < 合并低点 96
      mk(6, 112, 104),  // 下一笔终点（顶）
    ];
    const bis = [
      { type: "down", startIdx: 0, endIdx: 2, startPrice: 110, endPrice: 99, startTime: 1, endTime: 3, span: 11 },
      { type: "up", startIdx: 2, endIdx: 5, startPrice: 99, endPrice: 112, startTime: 3, endTime: 6, span: 13 },
    ];
    core.fixBiExtremes(bis, merged);
    assert.equal(bis[0].endPrice, 94);
    assert.equal(bis[0].endTime, 5);
    assert.equal(bis[0].endIdx, 4);
    assert.equal(bis[1].startPrice, 94); // 下一笔起点联动保持连续
    assert.equal(bis[1].startTime, 5);
    assert.equal(bis[1].startIdx, 4);
  });

  test("gapLocked 跳空成笔端点固定不参与修正", () => {
    const merged = [
      mk(1, 110, 100),
      mk(2, 103, 99),   // 底端点（gapLocked 笔终点）
      mk(3, 102, 97, { rawLow: 95, rawLowTime: 3 }), // 被掩盖：真实低点 95 < 合并低点 97
    ];
    const bis = [
      { type: "down", startIdx: 0, endIdx: 1, startPrice: 110, endPrice: 99, startTime: 1, endTime: 2, span: 11, gapLocked: true },
      { type: "up", startIdx: 1, endIdx: 2, startPrice: 99, endPrice: 102, startTime: 2, endTime: 3, span: 3, gapLocked: false },
    ];
    // 第一笔是跳空成笔（gapLocked），即使终点之后存在被掩盖的更极端低点 95，也不做修正
    core.fixBiExtremes(bis, merged);
    assert.equal(bis[0].endPrice, 99);
    assert.equal(bis[0].endIdx, 1);
    assert.equal(bis[1].startPrice, 99); // 下一笔起点未被联动
  });
});

describe("lockedPivotsOf 提取锁定端点（SPEC 2.4）", () => {
  test("上涨笔起底/终顶，下跌笔起顶/终底", () => {
    const prev = [
      { type: "up", startPrice: 100, endPrice: 120 },
      { type: "down", startPrice: 120, endPrice: 90 },
    ];
    assert.deepEqual(core.lockedPivotsOf(prev), [
      { dir: "bottom", price: 100 },
      { dir: "top", price: 120 },
      { dir: "top", price: 120 },
      { dir: "bottom", price: 90 },
    ]);
  });

  test("空笔数组返回 null", () => {
    assert.equal(core.lockedPivotsOf([]), null);
    assert.equal(core.lockedPivotsOf(null), null);
  });
});

describe("alignBiToUpper 区间套强制对齐（SPEC 2.4）", () => {
  test("下级漏掉上级极值时，同时对齐时间+价格", () => {
    const upper = [
      { type: "up", startTime: 1000, startPrice: 100, endTime: 2000, endPrice: 120 },
      { type: "down", startTime: 2000, startPrice: 120, endTime: 3000, endPrice: 90 },
    ];
    const lower = [
      { type: "up", startTime: 1000, startPrice: 100, endTime: 2050, endPrice: 118 },
      { type: "down", startTime: 2050, startPrice: 118, endTime: 3000, endPrice: 90 },
    ];
    core.alignBiToUpper(lower, upper, 300);
    // 下级顶 118 比上级顶 120 不极端（漏掉真极值）→ 时间+价格对齐到上级
    assert.equal(lower[0].endTime, 2000);
    assert.equal(lower[0].endPrice, 120);
    assert.equal(lower[1].startTime, 2000);
    assert.equal(lower[1].startPrice, 120);
    assert.equal(lower[1].endTime, 3000);
  });

  test("下级已找到相同极值时只对齐价格，保留更精确时间", () => {
    const upper = [
      { type: "up", startTime: 1000, startPrice: 100, endTime: 2000, endPrice: 120 },
    ];
    const lower = [
      { type: "up", startTime: 1000, startPrice: 100, endTime: 1950, endPrice: 120 },
    ];
    core.alignBiToUpper(lower, upper, 300);
    assert.equal(lower[0].endPrice, 120);
    assert.equal(lower[0].endTime, 1950); // 价格已等，不强制时间
  });

  test("幽灵底端点：上级底低于本级局部K线最低价（数据源聚合差异）→ 跳过对齐", () => {
    // 上级下跌笔终点是底 @2000 价格 88；本级K线在 [2000,2300) 内最低 low=92，
    // 88 在本级数据中不存在 → 幽灵端点，不对齐，保留本级真实极值 95@2200
    const upper = [
      { type: "down", startTime: 1000, startPrice: 120, endTime: 2000, endPrice: 88 },
    ];
    const lower = [
      { type: "up", startTime: 1000, startPrice: 100, endTime: 2050, endPrice: 118 },
      { type: "down", startTime: 2050, startPrice: 118, endTime: 2200, endPrice: 95 },
    ];
    const lowerBars = [
      { time: 2000, high: 118, low: 92, open: 110, close: 100 },
      { time: 2150, high: 120, low: 95, open: 100, close: 95 },
    ];
    core.alignBiToUpper(lower, upper, 300, lowerBars);
    assert.equal(lower[1].endPrice, 95);  // 保留本级真实低点价格
    assert.equal(lower[1].endTime, 2200); // 保留本级真实低点时间
  });

  test("幽灵顶端点：上级顶高于本级局部K线最高价 → 跳过对齐", () => {
    // 上级上涨笔终点是顶 @2000 价格 130；本级K线在 [2000,2300) 内最高 high=126，
    // 130 在本级数据中不存在 → 幽灵端点，不对齐，保留本级真实极值 125@2200
    const upper = [
      { type: "up", startTime: 1000, startPrice: 100, endTime: 2000, endPrice: 130 },
    ];
    const lower = [
      { type: "down", startTime: 1000, startPrice: 105, endTime: 1950, endPrice: 102 },
      { type: "up", startTime: 1950, startPrice: 102, endTime: 2200, endPrice: 125 },
    ];
    const lowerBars = [
      { time: 2000, high: 126, low: 100, open: 105, close: 110 },
      { time: 2150, high: 124, low: 101, open: 110, close: 125 },
    ];
    core.alignBiToUpper(lower, upper, 300, lowerBars);
    assert.equal(lower[1].endPrice, 125);
    assert.equal(lower[1].endTime, 2200);
  });

  test("上级极值在本级局部可达时仍正常对齐（防御不误伤）", () => {
    // 上级下跌笔终点底 @2000 价格 90；本级K线在 [2000,2300) 内最低 low=88，
    // 90 可达（未低于局部最低）→ 正常对齐时间+价格
    const upper = [
      { type: "down", startTime: 1000, startPrice: 120, endTime: 2000, endPrice: 90 },
    ];
    const lower = [
      { type: "up", startTime: 1000, startPrice: 100, endTime: 2050, endPrice: 118 },
      { type: "down", startTime: 2050, startPrice: 118, endTime: 2200, endPrice: 95 },
    ];
    const lowerBars = [
      { time: 2000, high: 118, low: 88, open: 110, close: 100 },
      { time: 2150, high: 120, low: 95, open: 100, close: 95 },
    ];
    core.alignBiToUpper(lower, upper, 300, lowerBars);
    assert.equal(lower[1].endPrice, 90);   // 对齐到上级真实底价
    assert.equal(lower[1].endTime, 2000);  // 对齐到上级底时间
  });
});

// ============================================================
// 5. 中枢（SPEC 2.5）
// ============================================================

describe("buildZS 笔中枢", () => {
  // 5 笔：down/up/down/up/down，第 5 笔向下突破下沿离开
  const bis = [
    { type: "down", startTime: 1000, endTime: 1100, startPrice: 110, endPrice: 100 },
    { type: "up", startTime: 1100, endTime: 1200, startPrice: 100, endPrice: 108 },
    { type: "down", startTime: 1200, endTime: 1300, startPrice: 108, endPrice: 102 },
    { type: "up", startTime: 1300, endTime: 1400, startPrice: 102, endPrice: 106 },
    { type: "down", startTime: 1400, endTime: 1500, startPrice: 106, endPrice: 98 },
  ];

  test("三笔重叠形成中枢，延伸后离开，biCount>=5 才输出", () => {
    const zss = core.buildZS(bis, 0);
    assert.equal(zss.length, 1);
    const z = zss[0];
    assert.equal(z.biCount, 5);
    assert.equal(z.extended, true);
    // 全部笔重叠：ZG=min(110,108,108,106,106)=106，ZD=max(100,100,102,102,98)=102
    assert.equal(z.zg, 106);
    assert.equal(z.zd, 102);
    // GG/DD：构成中枢 + 延伸笔的范围（离开笔不扩展 GG/DD，只用于识别离开）
    assert.equal(z.gg, 110);
    assert.equal(z.dd, 100);
    assert.equal(z.exitTime, 1400);
    assert.equal(z.enterEndTime, 1100); // 进入笔 = 三笔重叠第一笔 bis[0]，其终点
    assert.equal(z.startTime, 1100);    // 左边缘 = 进入笔终点 - 5×barSec（barSec=0 不外扩）
  });

  test("仅 3 笔（无延伸）不输出中枢", () => {
    const bis3 = bis.slice(0, 3);
    assert.deepEqual(core.buildZS(bis3, 0), []);
  });

  test("笔数 < 3 返回空", () => {
    assert.deepEqual(core.buildZS(bis.slice(0, 2), 0), []);
  });
});

describe("buildZSByUpper 按上级笔分解中枢（SPEC 2.5）", () => {
  const bis = [
    { type: "down", startTime: 1000, endTime: 1100, startPrice: 110, endPrice: 100 },
    { type: "up", startTime: 1100, endTime: 1200, startPrice: 100, endPrice: 108 },
    { type: "down", startTime: 1200, endTime: 1300, startPrice: 108, endPrice: 102 },
    { type: "up", startTime: 1300, endTime: 1400, startPrice: 102, endPrice: 106 },
    { type: "down", startTime: 1400, endTime: 1500, startPrice: 106, endPrice: 98 },
  ];

  test("无上级约束时直接用全部笔构建", () => {
    const zss = core.buildZSByUpper(bis, [], 900);
    assert.equal(zss.length, 1);
    assert.equal(zss[0].upperStart, 1000);
    assert.equal(zss[0].upperEnd, 1500);
    assert.equal(zss[0].biCount, 5);
  });

  test("全部笔完整落在同一上级笔内时构建成功", () => {
    const upper = [{ type: "up", startTime: 900, endTime: 1600, startPrice: 90, endPrice: 120 }];
    const zss = core.buildZSByUpper(bis, upper, 900);
    assert.equal(zss.length, 1);
    assert.equal(zss[0].upperStart, 900);
    assert.equal(zss[0].upperEnd, 1600);
  });
});

// ============================================================
// 6. ATR / MACD（SPEC 2.6）
// ============================================================

describe("calcATR 平均真实波幅", () => {
  test("TR 取 (H-L, |H-pc|, |L-pc|) 最大值，多根取均值", () => {
    const bars = [
      bar(1, 110, 90, 105),
      bar(2, 115, 95, 110),
      bar(3, 120, 100, 115),
    ];
    // TR1 = max(20, |115-105|=10, |95-105|=10) = 20
    // TR2 = max(20, |120-110|=10, |100-110|=10) = 20 → ATR = 20
    assert.equal(core.calcATR(bars), 20);
  });

  test("无数据返回 0", () => {
    assert.equal(core.calcATR([]), 0);
    assert.equal(core.calcATR([bar(1, 110, 90, 100)]), 0); // 单根无 TR
  });
});

describe("calcMACD 指标计算", () => {
  test("返回长度与K线一一对应，time 一致", () => {
    const bars = [];
    for (let i = 1; i <= 30; i++) bars.push(bar(i, i + 1, i - 1, i));
    const macd = core.calcMACD(bars);
    assert.equal(macd.length, 30);
    assert.equal(macd[29].time, 30);
  });

  test("持续上涨 → 末端 macd>0（红柱）", () => {
    const bars = [];
    for (let i = 1; i <= 40; i++) bars.push(bar(i, i + 1, i - 1, i));
    const macd = core.calcMACD(bars);
    assert.ok(macd[macd.length - 1].macd > 0);
  });

  test("持续下跌 → 末端 macd<0（绿柱）", () => {
    const bars = [];
    for (let i = 1; i <= 40; i++) bars.push(bar(i, 100 - i, 98 - i, 99 - i));
    const macd = core.calcMACD(bars);
    assert.ok(macd[macd.length - 1].macd < 0);
  });

  test("不足 2 根返回空数组", () => {
    assert.deepEqual(core.calcMACD([bar(1, 100, 90, 95)]), []);
  });
});

// ============================================================
// 7. MACD 红绿转换（SPEC 2.6）
// ============================================================

describe("hasMacdCrossBetween 方向性红绿转换", () => {
  const macdArr = [
    { time: 1, macd: -2 }, { time: 2, macd: -1 },
    { time: 3, macd: 1 }, { time: 4, macd: 2 },
  ];

  test("up：绿变红（<=0 → >0）返回 true", () => {
    assert.equal(core.hasMacdCrossBetween(macdArr, [], 0, 3, 1, 4, "up"), true);
  });

  test("down：区间内无红变绿返回 false", () => {
    assert.equal(core.hasMacdCrossBetween(macdArr, [], 0, 3, 1, 4, "down"), false);
  });

  test("未指定方向：任意红绿转换", () => {
    assert.equal(core.hasMacdCrossBetween(macdArr, [], 0, 3, 1, 4), true);
  });

  test("macdArr 为空返回 false", () => {
    assert.equal(core.hasMacdCrossBetween([], [], 0, 1, 1, 2, "up"), false);
  });
});

// ============================================================
// 8. 未完成笔延伸 / 周期映射 / 端点校准（SPEC 2.7）
// ============================================================

describe("extendLastBi 未完成笔延伸", () => {
  test("最后一笔终点后出现更极端价 → 延伸终点", () => {
    const bis = [
      { type: "up", startTime: 1000, startPrice: 100, endTime: 2000, endPrice: 110, span: 10 },
    ];
    const bars = [
      { time: 1500, high: 108, low: 100 },
      { time: 2500, high: 115, low: 105 },
      { time: 2600, high: 113, low: 104 },
    ];
    core.extendLastBi(bis, bars);
    assert.equal(bis[0].endTime, 2500);
    assert.equal(bis[0].endPrice, 115);
    assert.equal(bis[0].span, 15);
  });

  test("gapLocked 笔不参与延伸", () => {
    const bis = [
      { type: "up", startTime: 1000, startPrice: 100, endTime: 2000, endPrice: 110, span: 10, gapLocked: true },
    ];
    const bars = [{ time: 2500, high: 115, low: 105 }];
    core.extendLastBi(bis, bars);
    assert.equal(bis[0].endTime, 2000);
    assert.equal(bis[0].endPrice, 110);
  });
});

describe("lowerResOf 逐级校准映射", () => {
  test("D→240→60→15→3→null", () => {
    assert.equal(core.lowerResOf("D"), "240");
    assert.equal(core.lowerResOf("240"), "60");
    assert.equal(core.lowerResOf("60"), "15");
    assert.equal(core.lowerResOf("15"), "3");
    assert.equal(core.lowerResOf("3"), null);
    assert.equal(core.lowerResOf("30S"), null); // 30秒为最小级别，不校准（与 3 分钟同为未校准叶子）
    assert.equal(core.lowerResOf("1D"), "240");
    assert.equal(core.lowerResOf("4H"), "60");
    assert.equal(core.lowerResOf("1H"), "15");
  });
});

describe("intervalSecOf 周期时长", () => {
  test("各周期时长映射", () => {
    assert.equal(core.intervalSecOf("3"), 180);
    assert.equal(core.intervalSecOf("5"), 300);
    assert.equal(core.intervalSecOf("15"), 900);
    assert.equal(core.intervalSecOf("30"), 1800);
    assert.equal(core.intervalSecOf("30S"), 30); // 30秒（秒级 S 后缀）
    assert.equal(core.intervalSecOf("60"), 3600);
    assert.equal(core.intervalSecOf("1H"), 3600);
    assert.equal(core.intervalSecOf("240"), 14400);
    assert.equal(core.intervalSecOf("4H"), 14400);
    assert.equal(core.intervalSecOf("D"), 86400);
    assert.equal(core.intervalSecOf("1D"), 86400);
    assert.equal(core.intervalSecOf("W"), 604800);
  });
});

describe("calibrateBiTimes 跨周期端点时间校准", () => {
  test("端点价格匹配低一级K线高低价时，时间校准到该K线", () => {
    const bis = [{ startTime: 10000, startPrice: 100, endTime: 20000, endPrice: 120 }];
    const bigBars = [{ time: 10000, high: 100, low: 90 }];
    const refBars = [
      { time: 9000, high: 105, low: 95 },
      { time: 10100, high: 100, low: 92 },   // high 匹配 startPrice 100
      { time: 10200, high: 98, low: 96 },
    ];
    core.calibrateBiTimes(bis, bigBars, refBars, 3600);
    assert.equal(bis[0].startTime, 10100);
  });

  test("无匹配时保持原时间", () => {
    const bis = [{ startTime: 10000, startPrice: 100, endTime: 20000, endPrice: 120 }];
    const bigBars = [{ time: 10000, high: 100, low: 90 }];
    const refBars = [{ time: 10100, high: 105, low: 95 }];
    core.calibrateBiTimes(bis, bigBars, refBars, 3600);
    assert.equal(bis[0].startTime, 10000);
  });
});

// ============================================================
// 9. MACD 背驰（SPEC 2.8）
// ============================================================

describe("biMacdMetrics 笔区间 MACD 指标", () => {
  const macdArr = [
    { time: 1000, macd: 1, dif: 5 },
    { time: 1500, macd: -2, dif: 4 },
    { time: 2000, macd: -3, dif: 3 },
  ];

  test("红柱面积/绿柱面积/DIF高低点计算正确", () => {
    const m = core.biMacdMetrics({ startTime: 1000, endTime: 2000 }, macdArr);
    assert.equal(m.redArea, 1);
    assert.equal(m.greenArea, 5);
    assert.equal(m.difHigh, 5);
    assert.equal(m.difLow, 3);
  });

  test("笔区间无数据返回 null", () => {
    assert.equal(core.biMacdMetrics({ startTime: 3000, endTime: 4000 }, macdArr), null);
    assert.equal(core.biMacdMetrics({ startTime: 1000, endTime: 2000 }, []), null);
  });
});

describe("isBiDiverge MACD 背驰判定（OR 关系）", () => {
  test("底背驰：绿柱面积变小 → true", () => {
    const bi = { type: "down", startTime: 2000, endTime: 3000 };
    const refer = { type: "down", startTime: 1000, endTime: 1500 };
    const macdArr = [
      { time: 1000, macd: -5, dif: 2 }, { time: 1500, macd: -5, dif: 2 },
      { time: 2000, macd: -2, dif: 3 }, { time: 3000, macd: -2, dif: 3 },
    ];
    assert.equal(core.isBiDiverge(bi, refer, macdArr), true);
  });

  test("底背驰：黄白线低点抬高 → true（面积未变小）", () => {
    const bi = { type: "down", startTime: 2000, endTime: 3000 };
    const refer = { type: "down", startTime: 1000, endTime: 1500 };
    const macdArr = [
      { time: 1000, macd: -2, dif: 1 }, { time: 1500, macd: -2, dif: 1 },
      { time: 2000, macd: -2, dif: 3 }, { time: 3000, macd: -2, dif: 3 },
    ];
    // 面积相等但 difLow 抬高（1→3）
    assert.equal(core.isBiDiverge(bi, refer, macdArr), true);
  });

  test("顶背驰：红柱面积变小 → true", () => {
    const bi = { type: "up", startTime: 2000, endTime: 3000 };
    const refer = { type: "up", startTime: 1000, endTime: 1500 };
    const macdArr = [
      { time: 1000, macd: 5, dif: 2 }, { time: 1500, macd: 5, dif: 2 },
      { time: 2000, macd: 2, dif: 1 }, { time: 3000, macd: 2, dif: 1 },
    ];
    assert.equal(core.isBiDiverge(bi, refer, macdArr), true);
  });

  test("动能未减弱（面积未变小且 DIF 未背离）→ false", () => {
    const bi = { type: "down", startTime: 2000, endTime: 3000 };
    const refer = { type: "down", startTime: 1000, endTime: 1500 };
    const macdArr = [
      { time: 1000, macd: -2, dif: 3 }, { time: 1500, macd: -2, dif: 3 },
      { time: 2000, macd: -5, dif: 1 }, { time: 3000, macd: -5, dif: 1 },
    ];
    // 面积变大且 difLow 更低 → 无背驰
    assert.equal(core.isBiDiverge(bi, refer, macdArr), false);
  });
});

// ============================================================
// 10. 买卖点（SPEC 2.9）
// ============================================================

describe("findBuyPoints 买点识别", () => {
  // 创新低(1买候选) + 背驰；之后抬高低点(2买)
  const bis = [
    { type: "down", startTime: 1000, endTime: 1100, startPrice: 120, endPrice: 95, span: 25 },
    { type: "up", startTime: 1100, endTime: 1200, startPrice: 95, endPrice: 108, span: 13 },
    { type: "down", startTime: 1200, endTime: 1300, startPrice: 108, endPrice: 90, span: 18 },
    { type: "up", startTime: 1300, endTime: 1400, startPrice: 90, endPrice: 100, span: 10 },
    { type: "down", startTime: 1400, endTime: 1500, startPrice: 100, endPrice: 96, span: 4 },
  ];
  const macdArr = [
    { time: 1000, macd: -4, dif: 2 }, { time: 1050, macd: -4, dif: 2 }, { time: 1100, macd: -4, dif: 2 },
    { time: 1200, macd: -2, dif: 3 }, { time: 1250, macd: -2, dif: 3 }, { time: 1300, macd: -2, dif: 3 },
  ];

  test("创新低+背驰标记 1买；结构底之后抬高低点标记 2买", () => {
    const pts = core.findBuyPoints(bis, [], macdArr, 900);
    const b1 = pts.find(p => p.type === "1买");
    const b2 = pts.find(p => p.type === "2买");
    assert.ok(b1, "应存在 1买");
    assert.deepEqual({ time: b1.time, price: b1.price }, { time: 1300, price: 90 });
    assert.ok(b2, "应存在 2买");
    assert.deepEqual({ time: b2.time, price: b2.price }, { time: 1500, price: 96 });
  });

  test("笔数 < 3 返回空", () => {
    assert.deepEqual(core.findBuyPoints(bis.slice(0, 2), [], macdArr, 900), []);
  });
});

describe("findSellPoints 卖点识别", () => {
  // 创新高(1卖候选) + 背驰；之后次高点(2卖)
  const bis = [
    { type: "up", startTime: 1000, endTime: 1100, startPrice: 90, endPrice: 120, span: 30 },
    { type: "down", startTime: 1100, endTime: 1200, startPrice: 120, endPrice: 105, span: 15 },
    { type: "up", startTime: 1200, endTime: 1300, startPrice: 105, endPrice: 125, span: 20 },
    { type: "down", startTime: 1300, endTime: 1400, startPrice: 125, endPrice: 108, span: 17 },
    { type: "up", startTime: 1400, endTime: 1500, startPrice: 108, endPrice: 118, span: 10 },
  ];
  const macdArr = [
    { time: 1000, macd: 4, dif: 2 }, { time: 1050, macd: 4, dif: 2 }, { time: 1100, macd: 4, dif: 2 },
    { time: 1200, macd: 2, dif: 1 }, { time: 1250, macd: 2, dif: 1 }, { time: 1300, macd: 2, dif: 1 },
  ];

  test("创新高+背驰标记 1卖；结构顶之后次高点标记 2卖", () => {
    const pts = core.findSellPoints(bis, [], macdArr, 900);
    const s1 = pts.find(p => p.type === "1卖");
    const s2 = pts.find(p => p.type === "2卖");
    assert.ok(s1, "应存在 1卖");
    assert.deepEqual({ time: s1.time, price: s1.price }, { time: 1300, price: 125 });
    assert.ok(s2, "应存在 2卖");
    assert.deepEqual({ time: s2.time, price: s2.price }, { time: 1500, price: 118 });
  });

  test("笔数 < 3 返回空", () => {
    assert.deepEqual(core.findSellPoints(bis.slice(0, 2), [], macdArr, 900), []);
  });
});

describe("anchorFirstBuy 一买锚定（SPEC 2.9）", () => {
  test("锚定到候选之前最近的底部端点", () => {
    const cand = { time: 1500, price: 90 };
    const upper = [
      { type: "up", startTime: 1000, startPrice: 100, endTime: 1200, endPrice: 110 },
      { type: "down", startTime: 1200, startPrice: 110, endTime: 1600, endPrice: 90 },
    ];
    // 底部端点：1000@100（up起点）、1600@90（down终点，但 1600 > 1500 跳过）
    assert.deepEqual(core.anchorFirstBuy(cand, upper), { time: 1000, price: 100 });
  });

  test("上级笔为空返回 null", () => {
    assert.equal(core.anchorFirstBuy({ time: 1500, price: 90 }, []), null);
  });
});

describe("anchorFirstSell 一卖锚定（SPEC 2.9）", () => {
  test("候选位于上级上涨笔内部时上移到该笔结束点", () => {
    const cand = { time: 1300, price: 125 };
    const upper = [
      { type: "up", startTime: 1000, startPrice: 90, endTime: 1400, endPrice: 120 },
      { type: "down", startTime: 1400, startPrice: 120, endTime: 1700, endPrice: 95 },
    ];
    assert.deepEqual(core.anchorFirstSell(cand, upper), { time: 1400, price: 120 });
  });

  test("不在任何上涨笔内部时取时间最近的顶部端点", () => {
    const cand = { time: 1800, price: 100 };
    const upper = [
      { type: "up", startTime: 1000, startPrice: 90, endTime: 1400, endPrice: 120 },
      { type: "down", startTime: 1400, startPrice: 120, endTime: 1700, endPrice: 95 },
    ];
    assert.deepEqual(core.anchorFirstSell(cand, upper), { time: 1400, price: 120 });
  });
});

describe("isSameAsUpperBi 笔与上级笔完全重合（SPEC 2.9）", () => {
  const bi = { type: "up", startTime: 1000, endTime: 2000, startPrice: 100, endPrice: 120 };

  test("起终点时间与价格一致 → true", () => {
    const upper = [{ type: "up", startTime: 1000, endTime: 2000, startPrice: 100, endPrice: 120 }];
    assert.equal(core.isSameAsUpperBi(bi, upper, 900), true);
  });

  test("价格不同 → false", () => {
    const upper = [{ type: "up", startTime: 1000, endTime: 2000, startPrice: 100, endPrice: 121 }];
    assert.equal(core.isSameAsUpperBi(bi, upper, 900), false);
  });

  test("上级笔为空 → false", () => {
    assert.equal(core.isSameAsUpperBi(bi, [], 900), false);
  });
});

describe("snapToOwnBar 极值映射到本周期K线（SPEC 2.9）", () => {
  const bars = [
    { time: 1000, high: 110, low: 90 },
    { time: 1100, high: 108, low: 92 },
    { time: 1200, high: 106, low: 88 },
  ];

  test("高低价匹配且时间最近 → 该K线时间", () => {
    assert.equal(core.snapToOwnBar(92, 1100, bars), 1100);
    assert.equal(core.snapToOwnBar(92, 1200, bars), 1100);
  });

  test("无价格匹配 → 时间最近的K线", () => {
    assert.equal(core.snapToOwnBar(105, 1200, bars), 1200);
  });
});

describe("keepRecentEach 每类保留最近一个（SPEC 2.9）", () => {
  test("每类取时间最新，按时间排序", () => {
    const points = [
      { type: "1买", time: 1000 },
      { type: "2买", time: 1500 },
      { type: "1买", time: 2000 },
      { type: "3买", time: 1300 },
    ];
    const kept = core.keepRecentEach(points);
    assert.deepEqual(kept, [
      { type: "3买", time: 1300 },
      { type: "2买", time: 1500 },
      { type: "1买", time: 2000 },
    ]);
  });
});
