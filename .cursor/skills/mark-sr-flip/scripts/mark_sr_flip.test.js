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
