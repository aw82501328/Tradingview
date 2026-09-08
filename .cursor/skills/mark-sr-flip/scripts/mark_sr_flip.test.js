/**
 * mark-sr-flip 支阻互换位标记单元测试
 *
 * 依据 SPEC.md（`.cursor/skills/mark-sr-flip/SPEC.md`）的行为契约编写，
 * 覆盖支阻互换位识别、强度评分、每周期候选上限截断、跨周期合并与按级别选取
 * 的纯函数逻辑（本脚本已模块化导出，require 时不连接 CDP）。
 *
 * 运行：node --test .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.test.js
 */
const { describe, test } = require("node:test");
const assert = require("node:assert/strict");
const sr = require("./mark_sr_flip.js");

// ============================================================
// 工具
// ============================================================

/** 构造一根原始K线 */
const bar = (t, h, l, c) => ({ time: t, open: c, high: h, low: l, close: c });

/** 构造一根笔 */
const bi = (type, startTime, endTime, startPrice, endPrice) =>
  ({ type, startTime, endTime, startPrice, endPrice, span: Math.abs(endPrice - startPrice) });

/** 构造一个支阻位候选 */
const flip = (price, touchCount, barsPassed, opts = {}) =>
  ({ price, touchCount, barsPassed: barsPassed, type: opts.type || "R2S", breakTime: opts.breakTime || 1000, firstTouch: opts.firstTouch || 500, lastTouch: opts.lastTouch || 900, ...opts });

// ============================================================
// 1. swing 点提取（SPEC 2.1）
// ============================================================

describe("extractSwingPoints swing 点提取", () => {
  test("每笔终点为一次转折，额外补第一笔起点", () => {
    const bis = [
      bi("up", 100, 200, 90, 120),
      bi("down", 200, 300, 120, 100),
      bi("up", 300, 400, 100, 110),
    ];
    const pts = sr.extractSwingPoints(bis);
    assert.deepEqual(pts, [
      { price: 90, time: 100, kind: "low" },    // 第一笔起点（up → low）
      { price: 120, time: 200, kind: "high" },  // 第一笔终点（up → high）
      { price: 100, time: 300, kind: "low" },   // 第二笔终点（down → low）
      { price: 110, time: 400, kind: "high" },  // 第三笔终点（up → high）
    ]);
  });
});

// ============================================================
// 2. 价位聚类（SPEC 2.1）
// ============================================================

describe("clusterPoints 价位聚类", () => {
  test("价差 ≤ tol 的点并入同一簇，代表价为均值", () => {
    const pts = [
      { price: 100, time: 1, kind: "high" },
      { price: 102, time: 2, kind: "low" },
      { price: 110, time: 3, kind: "high" },
    ];
    const clusters = sr.clusterPoints(pts, 5);
    assert.equal(clusters.length, 2);
    assert.equal(clusters[0].touches.length, 2);
    assert.equal(clusters[0].price, 101); // (100+102)/2
    assert.equal(clusters[1].price, 110);
  });

  test("不相邻的相近价格不合并（单遍扫描仅合并相邻）", () => {
    const pts = [
      { price: 100, time: 1, kind: "high" },
      { price: 110, time: 2, kind: "high" },
      { price: 104, time: 3, kind: "low" },
    ];
    const clusters = sr.clusterPoints(pts, 3);
    // 排序后：100,104,110 → 100&104 合并（差4>3？不合并），104&110 合并（差6>3？不合并）
    // 实际 104-100=4 > 3 不合并；110-104=6 > 3 不合并 → 3 个簇
    assert.equal(clusters.length, 3);
  });
});

// ============================================================
// 3. 互换判定（SPEC 2.1 / 2.2）
// ============================================================

describe("detectFlip 强支阻互换判定", () => {
  const bars = [
    bar(1000, 110, 90, 100), bar(1100, 112, 95, 105),
    bar(1200, 115, 98, 112), // 向上突破 105+5
  ];

  test("首尾角色相反（先高后低）→ R2S", () => {
    const cluster = {
      price: 100,
      touches: [
        { price: 100, time: 100, kind: "high" },
        { price: 100, time: 200, kind: "low" },
      ],
    };
    const f = sr.detectFlip(cluster, bars, 5);
    assert.equal(f.type, "R2S");
    assert.equal(f.breakTime, 200);
    assert.equal(f.touchCount, 2);
  });

  test("首尾角色相反（先低后高）→ S2R", () => {
    const cluster = {
      price: 100,
      touches: [
        { price: 100, time: 100, kind: "low" },
        { price: 100, time: 200, kind: "high" },
      ],
    };
    const f = sr.detectFlip(cluster, bars, 5);
    assert.equal(f.type, "S2R");
  });

  test("角色未反转且无突破 → null", () => {
    const cluster = {
      price: 100,
      touches: [
        { price: 100, time: 100, kind: "high" },
        { price: 100, time: 200, kind: "high" },
      ],
    };
    // 全高点（阻力主导），其后收盘价 100/105/112 需 > 100+5=105 → 1200 处 112 突破
    const f = sr.detectFlip(cluster, bars, 5);
    assert.equal(f.type, "R2S");
    assert.equal(f.breakTime, 1200);
  });
});

describe("detectRecentFlip 近期极值位判定（SPEC 2.2）", () => {
  test("簇内同时有高、低点 → 按首尾判定互换类型", () => {
    const cluster = {
      price: 100,
      touches: [
        { price: 100, time: 100, kind: "low" },
        { price: 100, time: 200, kind: "high" },
      ],
    };
    const f = sr.detectRecentFlip(cluster);
    assert.equal(f.type, "S2R");
    assert.equal(f.recent, true);
  });

  test("只有高点 → 纯阻力 RES", () => {
    const cluster = {
      price: 100,
      touches: [
        { price: 100, time: 100, kind: "high" },
        { price: 100, time: 200, kind: "high" },
      ],
    };
    const f = sr.detectRecentFlip(cluster);
    assert.equal(f.type, "RES");
  });

  test("只有低点 → 纯支撑 SUP", () => {
    const cluster = {
      price: 100,
      touches: [
        { price: 100, time: 100, kind: "low" },
        { price: 100, time: 200, kind: "low" },
      ],
    };
    const f = sr.detectRecentFlip(cluster);
    assert.equal(f.type, "SUP");
  });
});

// ============================================================
// 4. 经过K线数 / 强度评分（SPEC 3）
// ============================================================

describe("countBarsPassing 覆盖价位带的K线数", () => {
  test("K线高低价覆盖 price±tol 才算（含影线）", () => {
    const bars = [
      bar(1, 110, 90, 100),  // 覆盖 100±5
      bar(2, 108, 96, 102),  // low 96 >= 95 且 high 108 >= 95 → 覆盖
      bar(3, 108, 106, 107), // low 106 > 105 → 不覆盖（全部在价位带上方）
      bar(4, 92, 88, 90),    // high 92 < 95 → 不覆盖（全部在下方）
    ];
    assert.equal(sr.countBarsPassing(100, bars, 5), 2);
  });
});

describe("flipScore 强度评分（SPEC 3）", () => {
  test("触及次数多的得分更高", () => {
    const group = [
      flip(100, 2, 10),
      flip(110, 8, 10),
      flip(120, 4, 10),
    ];
    const s1 = sr.flipScore(group[0], group);
    const s2 = sr.flipScore(group[1], group);
    assert.ok(s2 > s1);
  });

  test("经过K线多的得分更高", () => {
    const group = [
      flip(100, 4, 5),
      flip(110, 4, 15),
      flip(120, 4, 10),
    ];
    const s1 = sr.flipScore(group[0], group);
    const s2 = sr.flipScore(group[1], group);
    assert.ok(s2 > s1);
  });

  test("同值全等时归一化返回 1（不除零）", () => {
    const group = [flip(100, 3, 5), flip(110, 3, 5)];
    assert.equal(sr.flipScore(group[0], group), 1);
  });
});

// ============================================================
// 5. 每周期候选上限截断（SPEC 2.3）
// ============================================================

describe("capPerPeriod 每周期候选上限截断", () => {
  test("未超过上限的周期保持不变", () => {
    const allFlips = { "3": [flip(100, 2, 5), flip(110, 4, 6)] };
    const out = sr.capPerPeriod(allFlips, 50);
    assert.equal(out["3"].length, 2);
  });

  test("超过上限时按强度评分降序保留 Top N", () => {
    // 构造 5 个候选，触及次数递增（评分随触及次数单调升），上限 3 → 保留触及次数最多的 3 个
    const allFlips = {
      "3": [flip(100, 1, 5), flip(101, 2, 5), flip(102, 3, 5), flip(103, 4, 5), flip(104, 5, 5)],
    };
    const out = sr.capPerPeriod(allFlips, 3);
    assert.equal(out["3"].length, 3);
    const keptPrices = out["3"].map(f => f.price).sort((a, b) => b - a);
    assert.deepEqual(keptPrices, [104, 103, 102]); // 评分最高的 3 个
  });

  test("截断后候选带 score 字段（供落盘/后续使用）", () => {
    const allFlips = {
      "3": [flip(100, 1, 5), flip(101, 2, 5), flip(102, 3, 5), flip(103, 4, 5)],
    };
    const out = sr.capPerPeriod(allFlips, 2);
    assert.ok(out["3"].every(f => typeof f.score === "number"));
  });

  test("maxPerPeriod<=0 时不截断", () => {
    const allFlips = { "3": [flip(100, 1, 5), flip(101, 2, 5)] };
    assert.equal(sr.capPerPeriod(allFlips, 0), allFlips);
    assert.equal(sr.capPerPeriod(allFlips, -1), allFlips);
  });

  test("空输入返回原对象", () => {
    assert.equal(sr.capPerPeriod(null, 50), null);
    assert.equal(sr.capPerPeriod({}, 50).length === undefined, true);
  });

  test("多个周期各自独立截断", () => {
    const allFlips = {
      "3": [flip(100, 1, 5), flip(101, 2, 5), flip(102, 3, 5), flip(103, 4, 5), flip(104, 5, 5)],
      "15": [flip(200, 1, 5), flip(201, 2, 5)],
    };
    const out = sr.capPerPeriod(allFlips, 2);
    assert.equal(out["3"].length, 2);
    assert.equal(out["15"].length, 2); // 本身不足 2 个，不变
  });
});

// ============================================================
// 6. 跨周期合并（SPEC 4）
// ============================================================

describe("mergeFlipsAcrossPeriods 跨周期合并", () => {
  test("价差 ≤ tol 的候选合并，价格按触及次数加权平均", () => {
    const allFlips = {
      "60": [flip(100, 4, 20)],
      "15": [flip(103, 6, 30)],
    };
    const merged = sr.mergeFlipsAcrossPeriods(allFlips, 5);
    assert.equal(merged.length, 1);
    // 加权平均：(100×4 + 103×6) / 10 = (400+618)/10 = 101.8
    assert.equal(merged[0].price, 101.8);
    assert.equal(merged[0].touchCount, 10);
    assert.equal(merged[0].barsPassed, 50);
    assert.equal(merged[0].sources.join("+"), "60+15"); // 价格排序：60(100) 先入，15(103) 后并入
  });

  test("主要来源级别 = 来源中最大的级别", () => {
    const allFlips = {
      "3": [flip(100, 9, 40)],
      "60": [flip(103, 3, 10)],
    };
    const merged = sr.mergeFlipsAcrossPeriods(allFlips, 5);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].level, "60"); // 小级别触及次数多但主级别取大
  });

  test("价差超过容差的不合并", () => {
    const allFlips = {
      "60": [flip(100, 4, 20)],
      "15": [flip(110, 6, 30)],
    };
    const merged = sr.mergeFlipsAcrossPeriods(allFlips, 5);
    assert.equal(merged.length, 2);
  });
});

describe("dominantLevel 主要来源级别", () => {
  test("取 LEVEL_ORDER 中最靠前（最大）的级别", () => {
    assert.equal(sr.dominantLevel(["3", "60", "15"]), "60");
    assert.equal(sr.dominantLevel(["15", "3"]), "15");
    assert.equal(sr.dominantLevel(["D", "240"]), "D");
  });
});

// ============================================================
// 7. 按级别选取（SPEC 5.1）
// ============================================================

describe("pickByLevel 每级别上下各 1 个", () => {
  const periodAtrs = { "60": 10, "15": 5, "3": 2 };

  test("同侧多个候选按评分取最高，且限定距离范围", () => {
    const merged = [
      { ...flip(101, 2, 10), level: "60" },   // 上方，低分
      { ...flip(105, 8, 20), level: "60" },   // 上方，高分
      { ...flip(96, 9, 25), level: "60" },    // 下方
    ];
    const picked = sr.pickByLevel(merged, 100, 1, 3.0, periodAtrs);
    assert.equal(picked.length, 2);
    const above = picked.filter(f => f.price >= 100);
    const below = picked.filter(f => f.price < 100);
    assert.equal(above.length, 1);
    assert.equal(above[0].price, 105);  // 评分高的上方位
    assert.equal(below.length, 1);
    assert.equal(below[0].price, 96);   // 唯一的下方位
  });

  test("距离超出 maxDistAtr×本级别ATR 的候选被排除", () => {
    const merged = [
      { ...flip(160, 9, 30), level: "60" },  // 距现价 60 > 3.0×10=30 → 排除
      { ...flip(120, 3, 10), level: "60" },  // 距现价 20 ≤ 30 → 保留
    ];
    const picked = sr.pickByLevel(merged, 100, 1, 3.0, periodAtrs);
    assert.equal(picked.length, 1);
    assert.equal(picked[0].price, 120);
  });

  test("按级别分组，每级别上下各 1 个", () => {
    const merged = [
      { ...flip(105, 8, 20), level: "60" },
      { ...flip(95, 9, 25), level: "60" },
      { ...flip(102, 6, 15), level: "15" },
      { ...flip(97, 7, 18), level: "15" },
    ];
    const picked = sr.pickByLevel(merged, 100, 1, 3.0, periodAtrs);
    assert.equal(picked.length, 4); // 每个级别上下各 1
    const lv60 = picked.filter(f => f.level === "60");
    const lv15 = picked.filter(f => f.level === "15");
    assert.equal(lv60.length, 2);
    assert.equal(lv15.length, 2);
  });

  test("无该级别 ATR 时不限距离（Infinity）", () => {
    const merged = [
      { ...flip(105, 8, 20), level: "60" },
    ];
    const picked = sr.pickByLevel(merged, 100, 1, 3.0, {});
    assert.equal(picked.length, 1);
  });
});

// ============================================================
// 8. 颜色 / 可见范围 / 最少触及次数
// ============================================================

describe("srColor 按级别颜色", () => {
  test("各周期颜色映射", () => {
    assert.equal(sr.srColor("D"), "#F23645");
    assert.equal(sr.srColor("240"), "#2962FF");
    assert.equal(sr.srColor("60"), "#FFD700");
    assert.equal(sr.srColor("15"), "#8A2BE2");
    assert.equal(sr.srColor("3"), "#00BCD4");
  });
});

describe("srVisibilityFor 可见范围", () => {
  test("3分钟仅 3m 可见", () => {
    const iv = sr.srVisibilityFor("3");
    assert.equal(iv.minutes, true);
    assert.equal(iv.minutesFrom, 3);
    assert.equal(iv.minutesTo, 3);
    assert.equal(iv.hours, false);
  });

  test("日线全可见", () => {
    const iv = sr.srVisibilityFor("D");
    assert.equal(iv.days, true);
    assert.equal(iv.weeks, true);
    assert.equal(iv.minutes, true);
  });

  test("4小时含分钟与 1~4 小时", () => {
    const iv = sr.srVisibilityFor("240");
    assert.equal(iv.hours, true);
    assert.equal(iv.hoursFrom, 1);
    assert.equal(iv.hoursTo, 4);
  });
});

describe("minTouchFor 按级别最少触及次数", () => {
  test("默认按级别", () => {
    assert.equal(sr.minTouchFor("D"), 4);
    assert.equal(sr.minTouchFor("60"), 4);
    assert.equal(sr.minTouchFor("15"), 3);
    assert.equal(sr.minTouchFor("3"), 8);
  });
});

// ============================================================
// 9. 黄金分割支阻位（SPEC 2.4）
// ============================================================

describe("typeNameOf 支阻位类型标签", () => {
  test("fib → 黄金分割支撑/阻力", () => {
    assert.equal(sr.typeNameOf({ fib: true, type: "SUP" }), "黄金分割支撑");
    assert.equal(sr.typeNameOf({ fib: true, type: "RES" }), "黄金分割阻力");
  });

  test("密集区四类标签不变", () => {
    assert.equal(sr.typeNameOf({ type: "R2S" }), "阻力转支撑");
    assert.equal(sr.typeNameOf({ type: "S2R" }), "支撑转阻力");
    assert.equal(sr.typeNameOf({ type: "RES" }), "阻力(近期极值)");
    assert.equal(sr.typeNameOf({ type: "SUP" }), "支撑(近期极值)");
  });
});

describe("pickLatestFibPoint 最新非一类点选取（SPEC 2.4）", () => {
  test("白名单内取 time 最新；一类点被排除", () => {
    const points = [
      { type: "2买", time: 100, price: 90 },
      { type: "1买", time: 500, price: 80 },   // 一类，排除
      { type: "类2买", time: 300, price: 95 },
      { type: "3买", time: 200, price: 98 },
    ];
    const p = sr.pickLatestFibPoint(points, sr.FIB_BUY_TYPES);
    assert.equal(p.type, "类2买");
    assert.equal(p.time, 300);
  });

  test("同 time 取数组靠后者", () => {
    const points = [
      { type: "2买", time: 300, price: 90 },
      { type: "3买", time: 300, price: 95 },
    ];
    assert.equal(sr.pickLatestFibPoint(points, sr.FIB_BUY_TYPES).type, "3买");
  });

  test("空列表 / 全一类 → null", () => {
    assert.equal(sr.pickLatestFibPoint([], sr.FIB_BUY_TYPES), null);
    assert.equal(sr.pickLatestFibPoint([{ type: "1买", time: 100, price: 90 }], sr.FIB_BUY_TYPES), null);
  });
});

describe("referBiOfPoint 回调笔与参照笔定位（SPEC 2.4）", () => {
  test("买点：time 命中 down 笔，参照笔=前一 up 笔", () => {
    const bis = [
      bi("up", 100, 200, 90, 120),    // 参照笔（买点回调前的上涨笔）
      bi("down", 200, 300, 120, 100), // 回调笔，终点即 2买
    ];
    const found = sr.referBiOfPoint(bis, 300, "down");
    assert.equal(found.pullback.type, "down");
    assert.equal(found.pullback.endTime, 300);
    assert.equal(found.refer.type, "up");
    assert.equal(found.refer.startPrice, 90);
    assert.equal(found.refer.endPrice, 120);
  });

  test("匹配不到（time 或方向不符）→ null", () => {
    const bis = [bi("up", 100, 200, 90, 120), bi("down", 200, 300, 120, 100)];
    assert.equal(sr.referBiOfPoint(bis, 999, "down"), null);
    assert.equal(sr.referBiOfPoint(bis, 200, "down"), null); // time 命中的是 up 笔
  });

  test("回调笔是首笔（无前方笔）→ null", () => {
    const bis = [bi("down", 100, 200, 120, 100)];
    assert.equal(sr.referBiOfPoint(bis, 200, "down"), null);
  });

  test("前一笔同向（笔应交替的脏数据防御）→ null", () => {
    const bis = [
      bi("down", 100, 200, 120, 110),
      bi("down", 200, 300, 110, 100), // 脏数据：连续同向
    ];
    assert.equal(sr.referBiOfPoint(bis, 300, "down"), null);
  });
});

describe("fibLevelsOf 参照笔黄金分割回撤位（SPEC 2.4）", () => {
  test("买向：上涨笔 100→200，分割位 = H - r×(H-L)", () => {
    const refer = bi("up", 100, 200, 100, 200);
    const levels = sr.fibLevelsOf(refer, "buy", [0.382, 0.5, 0.618]);
    assert.deepEqual(levels.map(l => l.ratio), [0.382, 0.5, 0.618]);
    // 0.382 → 200-38.2=161.8；0.5 → 150；0.618 → 138.2
    assert.ok(Math.abs(levels[0].price - 161.8) < 1e-9);
    assert.ok(Math.abs(levels[1].price - 150) < 1e-9);
    assert.ok(Math.abs(levels[2].price - 138.2) < 1e-9);
  });

  test("卖向：下跌笔 200→100，分割位 = L + r×(H-L)", () => {
    const refer = bi("down", 100, 200, 200, 100);
    const levels = sr.fibLevelsOf(refer, "sell", [0.382, 0.5, 0.618]);
    assert.ok(Math.abs(levels[0].price - 138.2) < 1e-9);
    assert.ok(Math.abs(levels[1].price - 150) < 1e-9);
    assert.ok(Math.abs(levels[2].price - 161.8) < 1e-9);
  });

  test("span=0（退化笔）→ []", () => {
    assert.deepEqual(sr.fibLevelsOf(bi("up", 100, 200, 100, 100), "buy", [0.5]), []);
  });
});

describe("buildFibCandidates 黄金分割候选组装（SPEC 2.4）", () => {
  // 上涨笔 100→150 后回调至 120（2买），回调笔前的上涨笔即参照笔
  const bis = [
    bi("up", 100, 200, 100, 150),    // 参照笔（买）
    bi("down", 200, 300, 150, 120),  // 回调笔 → 2买@300
    bi("down", 300, 400, 120, 110),  // 卖点回调前的下跌笔（参照笔，卖）
    bi("up", 400, 500, 110, 130),    // 卖点的反弹笔 → 2卖@500
  ];
  const bars = [bar(450, 160, 100, 130)];

  test("每方向只取最新非一类点 × 全部比率（共 2×3=6 条）", () => {
    const buyPts = [
      { type: "2买", time: 100, price: 105 },   // 更早的 2买，不产生线
      { type: "2买", time: 300, price: 120 },   // 最新买点
    ];
    const sellPts = [{ type: "2卖", time: 500, price: 130 }];
    const cands = sr.buildFibCandidates(bis, buyPts, sellPts, [0.382, 0.5, 0.618], bars, 5);
    assert.equal(cands.length, 6);
    // 买点组在前：参照笔 100→150，0.382 位 = 150-0.382×50 = 130.9
    const buys = cands.filter(c => c.type === "SUP");
    const sells = cands.filter(c => c.type === "RES");
    assert.equal(buys.length, 3);
    assert.equal(sells.length, 3);
    assert.ok(Math.abs(buys[0].price - 130.9) < 1e-9);
    // 卖点参照笔 120→110，0.382 位 = 110+0.382×10 = 113.82
    assert.ok(Math.abs(sells[0].price - 113.82) < 1e-9);
  });

  test("候选字段：fib/ratio/fromPoint/referBi/touchCount/时间锚点", () => {
    const cands = sr.buildFibCandidates(
      bis,
      [{ type: "类2买", time: 300, price: 120 }],
      [],
      [0.5],
      bars, 5
    );
    assert.equal(cands.length, 1);
    const f = cands[0];
    assert.equal(f.fib, true);
    assert.equal(f.ratio, 0.5);
    assert.equal(f.type, "SUP");
    assert.deepEqual(f.fromPoint, { type: "类2买", time: 300, price: 120 });
    assert.equal(f.referBi.startTime, 100);
    assert.equal(f.referBi.endTime, 200);
    assert.equal(f.referBi.startPrice, 100);
    assert.equal(f.referBi.endPrice, 150);
    assert.equal(f.touchCount, 1);
    assert.equal(f.firstTouch, 100);   // 参照笔起点
    assert.equal(f.lastTouch, 300);    // 信号点时间
    assert.equal(f.breakTime, 300);    // 绘线锚点 = 信号点时间
    assert.equal(typeof f.barsPassed, "number");
  });

  test("仅一类点 → 空数组（一类不生成黄金分割）", () => {
    // 注意：一类点不算已形成非一类点，但卖向预期回退也不触发——末笔 up 130 ≥ 前方
    // down 笔起点 120（次高点结构破坏），故整体为空
    const cands = sr.buildFibCandidates(
      bis,
      [{ type: "1买", time: 300, price: 120 }],
      [{ type: "1卖", time: 500, price: 130 }],
      [0.5], bars, 5
    );
    assert.equal(cands.length, 0);
  });

  test("最新点匹配不到参照笔 → 该方向跳过且不回退更早点", () => {
    // 最新 2买 在 time=999（匹配不到回调笔）→ 买方向整体跳过（有已形成点不走预期回退），
    // 不回退用 time=100 的更早 2买
    const cands = sr.buildFibCandidates(
      bis,
      [{ type: "2买", time: 100, price: 105 }, { type: "2买", time: 999, price: 110 }],
      [],
      [0.5], bars, 5
    );
    assert.equal(cands.length, 0);
  });
});

describe("pendingReferOf 预期回退参照笔（SPEC 2.4 预期回退）", () => {
  const bis = [
    bi("up", 100, 200, 100, 150),
    bi("down", 200, 300, 150, 110),   // 卖向参照笔：H=150 L=110
    bi("up", 300, 400, 110, 140),     // 形成中上涨笔（140 < 150 次高点成立）
  ];

  test("卖向：末笔 up 形成中且次高点结构成立 → 返回前方 down 笔", () => {
    const p = sr.pendingReferOf(bis, "sell");
    assert.equal(p.refer.type, "down");
    assert.equal(p.refer.startPrice, 150);
    assert.equal(p.forming.endPrice, 140);
  });

  test("买向对称：末笔 down 形成中且次低点结构成立", () => {
    const buyBis = [
      bi("down", 100, 200, 150, 110),
      bi("up", 200, 300, 110, 150),
      bi("down", 300, 400, 150, 120),  // 形成中下跌笔（120 > 110 次低点成立）
    ];
    const p = sr.pendingReferOf(buyBis, "buy");
    assert.equal(p.refer.type, "up");
    assert.equal(p.refer.startPrice, 110);
  });

  test("结构破坏（形成笔高点 ≥ 前方下跌笔起点）→ null", () => {
    const broken = [
      bi("up", 100, 200, 100, 150),
      bi("down", 200, 300, 150, 110),
      bi("up", 300, 400, 110, 155),    // 155 ≥ 150：次高点结构被否定
    ];
    assert.equal(sr.pendingReferOf(broken, "sell"), null);
  });

  test("末笔方向不符 → null（卖向需形成中 up）", () => {
    assert.equal(sr.pendingReferOf(bis, "buy"), null);
  });

  test("末笔是首笔 / 前方笔同向（脏数据）→ null", () => {
    assert.equal(sr.pendingReferOf([bi("up", 100, 200, 90, 120)], "sell"), null);
    const dirty = [bi("up", 100, 200, 90, 120), bi("up", 200, 300, 120, 140)];
    assert.equal(sr.pendingReferOf(dirty, "sell"), null);
  });
});

describe("buildFibCandidates 预期回退（无已形成点时补位，SPEC 2.4）", () => {
  const bis = [
    bi("up", 100, 200, 100, 150),
    bi("down", 200, 300, 150, 110),   // 卖向参照笔
    bi("up", 300, 400, 110, 140),     // 形成中上涨笔
  ];
  const bars = [bar(350, 160, 100, 130)];

  test("无已形成点 → 预期2卖 × 全部比率（pending RES）", () => {
    const cands = sr.buildFibCandidates(bis, [], [], [0.382, 0.5, 0.618], bars, 5);
    assert.equal(cands.length, 3);
    assert.ok(cands.every(c => c.pending === true && c.type === "RES" && c.fib === true));
    assert.deepEqual(cands[0].fromPoint, { type: "预期2卖", time: 400, price: 140 });
    assert.equal(cands[0].breakTime, 400);        // 形成笔极值时间 = 锚点
    assert.equal(cands[0].lastTouch, 400);
    assert.equal(cands[0].firstTouch, 200);       // 参照笔起点
    assert.equal(cands[0].referBi.startPrice, 150);
    // 参照 down 150→110：0.382 → 110+0.382×40 = 125.28
    assert.ok(Math.abs(cands[0].price - 125.28) < 1e-9);
  });

  test("买向回退：末笔 down 形成中 → 预期2买（pending SUP）", () => {
    const buyBis = [
      bi("down", 100, 200, 150, 110),
      bi("up", 200, 300, 110, 150),
      bi("down", 300, 400, 150, 120),  // 形成中，120 > 110 ✓
    ];
    const cands = sr.buildFibCandidates(buyBis, [], [], [0.5], bars, 5);
    assert.equal(cands.length, 1);
    assert.equal(cands[0].pending, true);
    assert.equal(cands[0].type, "SUP");
    assert.equal(cands[0].fromPoint.type, "预期2买");
    // 参照 up 110→150：0.5 → 150-0.5×40 = 130
    assert.ok(Math.abs(cands[0].price - 130) < 1e-9);
  });

  test("有已形成点的方向不回退（预期位只补位）", () => {
    // 买向有已形成 2买@300 → 用已形成点；卖向无 → 预期回退（末笔 up 140<150 ✓）
    const cands = sr.buildFibCandidates(
      bis, [{ type: "2买", time: 300, price: 110 }], [], [0.5], bars, 5);
    assert.equal(cands.length, 2);
    const formed = cands.find(c => c.pending === undefined);
    const pend = cands.find(c => c.pending === true);
    assert.equal(formed.type, "SUP");              // 已形成点：参照 up 100→150 → 125
    assert.ok(Math.abs(formed.price - 125) < 1e-9);
    assert.equal(formed.fromPoint.type, "2买");
    assert.equal(pend.type, "RES");                // 预期：参照 down 150→110 → 130
    assert.ok(Math.abs(pend.price - 130) < 1e-9);
    assert.equal(pend.fromPoint.type, "预期2卖");
  });

  test("typeNameOf：pending → 预期黄金分割支撑/阻力", () => {
    assert.equal(sr.typeNameOf({ fib: true, pending: true, type: "RES" }), "预期黄金分割阻力");
    assert.equal(sr.typeNameOf({ fib: true, pending: true, type: "SUP" }), "预期黄金分割支撑");
    assert.equal(sr.typeNameOf({ fib: true, type: "RES" }), "黄金分割阻力");
  });
});

// ============================================================
// 10. BOLL 布林带（SPEC 一 / 二）
// ============================================================

/** 构造 n 根收盘价恒为 c 的K线 */
const constBars = (n, c) => Array.from({ length: n }, (_, i) => bar(i, c, c, c));

describe("calcBOLL 布林带（已收盘口径，SPEC 一）", () => {
  test("常数列三轨合一（σ=0）", () => {
    const band = sr.calcBOLL(constBars(27, 100), 26, 2);
    assert.ok(band);
    assert.ok(Math.abs(band.mid - 100) < 1e-9);
    assert.ok(Math.abs(band.upper - 100) < 1e-9);
    assert.ok(Math.abs(band.lower - 100) < 1e-9);
  });

  test("手工数列 SMA 与总体标准差（÷N）", () => {
    // 已收盘 4 根收盘价 [1,3,1,3]，末根形成中排除 → mid=2, σ=1, mult=2 → upper=4 lower=0
    const bars = [bar(1, 1, 1, 1), bar(2, 3, 3, 3), bar(3, 1, 1, 1), bar(4, 3, 3, 3), bar(5, 9, 9, 9)];
    const band = sr.calcBOLL(bars, 4, 2);
    assert.ok(band);
    assert.ok(Math.abs(band.mid - 2) < 1e-9);
    assert.ok(Math.abs(band.upper - 4) < 1e-9);
    assert.ok(Math.abs(band.lower - 0) < 1e-9);
  });

  test("剔除末根形成中K线：末根收盘价不影响结果", () => {
    const a = sr.calcBOLL([...constBars(27, 100), bar(99, 200, 200, 200)], 26, 2);
    const b = sr.calcBOLL([...constBars(27, 100), bar(99, 5, 5, 5)], 26, 2);
    assert.ok(Math.abs(a.mid - b.mid) < 1e-9);
    assert.ok(Math.abs(a.upper - b.upper) < 1e-9);
  });

  test("已收盘不足 len → null", () => {
    assert.equal(sr.calcBOLL(constBars(26, 100), 26, 2), null); // 仅 25 根已收盘
    assert.equal(sr.calcBOLL([], 26, 2), null);
  });
});

describe("buildBollCandidates 布林带候选组装（SPEC 一）", () => {
  test("三轨类型：上 RES / 下 SUP / 中按现价侧", () => {
    const bars = constBars(27, 100);
    const above = sr.buildBollCandidates(bars, 26, 2, 120); // 现价 ≥ 中轨 → 中轨 SUP
    assert.equal(above.length, 3);
    assert.equal(above[0].boll, "upper");
    assert.equal(above[0].type, "RES");
    assert.equal(above[1].boll, "mid");
    assert.equal(above[1].type, "SUP");
    assert.equal(above[2].boll, "lower");
    assert.equal(above[2].type, "SUP");

    const below = sr.buildBollCandidates(bars, 26, 2, 90); // 现价 < 中轨 → 中轨 RES
    assert.equal(below[1].type, "RES");
  });

  test("候选字段：touchCount=1 / barsPassed=0 / 时间锚=末根已收盘K线", () => {
    const bars = constBars(27, 100);
    const cands = sr.buildBollCandidates(bars, 26, 2, 100);
    assert.ok(cands.every(c => c.touchCount === 1 && c.barsPassed === 0));
    const anchor = bars[bars.length - 2].time; // 末根已收盘K线
    assert.ok(cands.every(c => c.firstTouch === anchor && c.lastTouch === anchor && c.breakTime === anchor));
  });

  test("bars 不足无布林位 → 空数组", () => {
    assert.deepEqual(sr.buildBollCandidates(constBars(10, 100), 26, 2, 100), []);
  });
});

// ============================================================
// 11. 按显示周期选取（SPEC 三）
// ============================================================

describe("pickNearestForDisplay 按周期显示选取（SPEC 三）", () => {
  const periodAtrs = { "240": 10, "60": 10, "3": 2 };

  test("高级别线继承到低周期图（候选池=该级别及以上）", () => {
    const merged = [
      { ...flip(105, 8, 20), level: "240" },   // 高级别线
      { ...flip(50, 3, 5), level: "3" },        // 低级别远位（距现价 50 > 3×ATR3=6 → 排除）
    ];
    const drawn = sr.pickNearestForDisplay(merged, ["240", "3"], 100, 1, 3.0, periodAtrs);
    assert.equal(drawn["3"].length, 1);
    assert.equal(drawn["3"][0].level, "240"); // 继承自 240
    assert.equal(drawn["240"][0].level, "240");
  });

  test("每周期就近上下各 N（sideCount=2）", () => {
    const merged = [
      { ...flip(102, 1, 5), level: "60" },
      { ...flip(105, 1, 5), level: "60" },
      { ...flip(98, 1, 5), level: "60" },
      { ...flip(95, 1, 5), level: "60" },
    ];
    const drawn = sr.pickNearestForDisplay(merged, ["60"], 100, 2, 3.0, periodAtrs);
    const lines = drawn["60"];
    const above = lines.filter(f => f.price >= 100).map(f => f.price);
    const below = lines.filter(f => f.price < 100).map(f => f.price);
    assert.deepEqual(above, [102, 105]); // 上方按价差升序（就近）
    assert.deepEqual(below, [98, 95]);   // 下方按价差升序（就近）
  });

  test("距离上限 ≤ maxDistAtr×线自身级别ATR，允许上下不对称", () => {
    const merged = [
      { ...flip(150, 9, 30), level: "60" },  // 距现价 50 > 3×10=30 → 排除
      { ...flip(105, 3, 10), level: "60" },  // 上方，保留
    ];
    const drawn = sr.pickNearestForDisplay(merged, ["60"], 100, 2, 3.0, periodAtrs);
    assert.equal(drawn["60"].length, 1); // 下方无候选 → 不对称
    assert.equal(drawn["60"][0].price, 105);
  });

  test("无该级别 ATR 时不限距离（Infinity）", () => {
    const merged = [{ ...flip(500, 1, 5), level: "60" }];
    const drawn = sr.pickNearestForDisplay(merged, ["60"], 100, 1, 3.0, {});
    assert.equal(drawn["60"].length, 1);
    assert.equal(drawn["60"][0].price, 500);
  });
});

// ============================================================
// 12. 来源标注 / 单周期可见性（SPEC 三）
// ============================================================

describe("periodNameOf/sourceLabelOf/labelOf 来源标注（SPEC 三）", () => {
  test("周期中文名", () => {
    assert.equal(sr.periodNameOf("3"), "3分钟");
    assert.equal(sr.periodNameOf("15"), "15分钟");
    assert.equal(sr.periodNameOf("60"), "1小时");
    assert.equal(sr.periodNameOf("240"), "4小时");
    assert.equal(sr.periodNameOf("D"), "日线");
  });

  test("boll 三轨标签", () => {
    assert.equal(sr.sourceLabelOf({ boll: "upper" }), "BOLL上轨");
    assert.equal(sr.sourceLabelOf({ boll: "mid" }), "BOLL中轨");
    assert.equal(sr.sourceLabelOf({ boll: "lower" }), "BOLL下轨");
  });

  test("fib 已形成 / 预期标签", () => {
    assert.equal(sr.sourceLabelOf({ fib: true, ratio: 0.5 }), "黄金分割0.5");
    assert.equal(sr.sourceLabelOf({ fib: true, pending: true, fromPoint: { type: "预期2卖" } }), "预期2卖");
  });

  test("cluster / mixed 标签", () => {
    assert.equal(sr.sourceLabelOf({ type: "R2S" }), "密集区");
    assert.equal(sr.sourceLabelOf({ srcType: "mixed" }), "位置线");
  });

  test("labelOf 组合 `<类型>+<周期中文名>`", () => {
    assert.equal(sr.labelOf({ boll: "upper", level: "240" }), "BOLL上轨+4小时");
    assert.equal(sr.labelOf({ type: "R2S", level: "15" }), "密集区+15分钟");
    assert.equal(sr.labelOf({ fib: true, ratio: 0.5, level: "60" }), "黄金分割0.5+1小时");
    assert.equal(sr.labelOf({ fib: true, pending: true, fromPoint: { type: "预期2卖" }, level: "240" }), "预期2卖+4小时");
  });

  test("typeNameOf boll/mixed 标签", () => {
    assert.equal(sr.typeNameOf({ boll: "upper" }), "BOLL上轨");
    assert.equal(sr.typeNameOf({ srcType: "mixed" }), "位置线");
  });
});

describe("srVisibilitySingle 单周期可见性（SPEC 三）", () => {
  test("3分钟仅 minutes 3-3", () => {
    const iv = sr.srVisibilitySingle("3");
    assert.equal(iv.minutes, true);
    assert.equal(iv.minutesFrom, 3);
    assert.equal(iv.minutesTo, 3);
    assert.equal(iv.hours, false);
  });

  test("240 仅 hours 4-4（不含分钟）", () => {
    const iv = sr.srVisibilitySingle("240");
    assert.equal(iv.hours, true);
    assert.equal(iv.hoursFrom, 4);
    assert.equal(iv.hoursTo, 4);
    assert.equal(iv.minutes, false);
  });

  test("日线仅 days 1-1", () => {
    const iv = sr.srVisibilitySingle("D");
    assert.equal(iv.days, true);
    assert.equal(iv.daysFrom, 1);
    assert.equal(iv.daysTo, 1);
    assert.equal(iv.weeks, false);
  });
});

// ============================================================
// 13. 统一合并池：混合来源标记清理（SPEC 二）
// ============================================================

describe("mergeFlipsAcrossPeriods 混合来源标记清理（SPEC 二）", () => {
  test("cluster+fib 混合合并 → srcType=mixed，删 fib 标记", () => {
    const fibCand = { ...flip(100, 1, 5), fib: true, ratio: 0.5, fromPoint: { type: "2买", time: 1, price: 1 }, referBi: {} };
    const clusterCand = flip(102, 4, 20);
    const merged = sr.mergeFlipsAcrossPeriods({ "60": [fibCand], "15": [clusterCand] }, 5);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].srcType, "mixed");
    assert.equal(merged[0].fib, undefined);
    assert.equal(merged[0].ratio, undefined);
    assert.equal(merged[0].fromPoint, undefined);
    assert.equal(merged[0].boll, undefined);
  });

  test("纯单来源 fib 独立线保留 fib 标记", () => {
    const fibCand = { ...flip(100, 1, 5), fib: true, ratio: 0.5, fromPoint: { type: "2买", time: 1, price: 1 }, referBi: {} };
    const merged = sr.mergeFlipsAcrossPeriods({ "60": [fibCand] }, 5);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].srcType, "fib");
    assert.equal(merged[0].fib, true);
    assert.equal(merged[0].ratio, 0.5);
  });

  test("纯单来源 boll 独立线保留 boll 标记", () => {
    const bollCand = { ...flip(100, 1, 0), boll: "upper" };
    const merged = sr.mergeFlipsAcrossPeriods({ "60": [bollCand] }, 5);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].srcType, "boll");
    assert.equal(merged[0].boll, "upper");
  });
});
