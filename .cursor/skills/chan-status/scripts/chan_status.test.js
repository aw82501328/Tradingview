/**
 * chan-status SKILL 单元测试（TDD）
 * 覆盖预测规则纯函数：predictPeriod 各状态分支 + 辅助函数。
 * 运行：node .cursor/skills/chan-status/scripts/chan_status.test.js
 */
const assert = require("assert");
const {
  predictPeriod, findRefer, checkThirdSell, checkThirdBuy, isRangeBound,
  dispWidth, padCell, printPeriodTable,
} = require("./chan_status.js");

/** 构造一笔（时间用秒，价格用数字） */
function bi(type, st, sp, et, ep) {
  return { type, startTime: st, startPrice: sp, endTime: et, endPrice: ep, span: Math.abs(ep - sp), rawCount: 0 };
}

/** 构造 MACD 数组：区间 [t0, t1] 内 macd 恒为 val */
function macdRun(t0, t1, macd, dif) {
  const arr = [];
  for (let t = t0; t <= t1; t++) arr.push({ time: t, macd, dif: dif || macd, dea: 0 });
  return arr;
}

function run() {
  let n = 0;
  const t = (name, fn) => { try { fn(); n++; console.log("  ok", name); } catch (e) { console.error("  FAIL", name, "->", e.message); process.exitCode = 1; } };

  console.log("== 数据不足 ==");
  t("少于2笔返回数据不足", () => {
    const r = predictPeriod({ res: "60", upperRes: "240", bis: [bi("up", 100, 10, 200, 20)], upperBis: [], macdArr: [], atr: 1, lastPrice: 15, lastBarTime: 200 });
    assert.strictEqual(r.status, "数据不足");
  });

  console.log("== 日线等待2买（用户例子） ==");
  t("日线 up/down/up 第三笔创新高后回调中 -> 等待2买", () => {
    const bis = [
      bi("up", 100, 3942.1, 200, 4202.705),
      bi("down", 200, 4202.705, 300, 3959.8),
      bi("up", 300, 3959.8, 900, 4697.105),
    ];
    const r = predictPeriod({ res: "D", upperRes: null, bis, upperBis: [], macdArr: [], atr: 10, lastPrice: 4460, lastBarTime: 950 });
    assert.ok(r.status.includes("回调进行中"), r.status);
    assert.ok(r.status.includes("等待2买"), r.status);
    assert.ok(r.reason.includes("强过前期结构高点"), r.reason);
    assert.strictEqual(r.pred.type, "预判2买");
    assert.strictEqual(r.pred.price, 4460);
  });

  console.log("== 4小时预判2卖（用户例子） ==");
  t("上级顶后第一反弹不创新高 -> 预判2卖", () => {
    const upperBis = [bi("up", 100, 3959.8, 1000, 4697.105)];
    const bis = [
      bi("down", 1000, 4697.105, 2000, 4564.27),
      bi("up", 2000, 4564.27, 3000, 4631.98),
    ];
    const r = predictPeriod({ res: "240", upperRes: "D", bis, upperBis, macdArr: [], atr: 10, lastPrice: 4631.98, lastBarTime: 3000 });
    assert.ok(r.status.includes("预判2卖"), r.status);
    assert.strictEqual(r.pred.type, "预判2卖");
    assert.strictEqual(r.pred.price, 4631.98);
    // 附加预判：类2卖
    assert.ok(r.preds.some(p => p.type === "预判类2卖"));
  });

  t("上级顶后反弹已回落 -> 2卖运行中", () => {
    const upperBis = [bi("up", 100, 3959.8, 1000, 4697.105)];
    const bis = [
      bi("down", 1000, 4697.105, 2000, 4564.27),
      bi("up", 2000, 4564.27, 3000, 4631.98),
    ];
    const r = predictPeriod({ res: "240", upperRes: "D", bis, upperBis, macdArr: [], atr: 10, lastPrice: 4454.99, lastBarTime: 3200 });
    assert.ok(r.status.includes("2卖运行中"), r.status);
    assert.strictEqual(r.pred, null);
    assert.ok(r.reason.includes("2卖点"), r.reason);
  });

  console.log("== 预判1卖（创新高+顶背驰） ==");
  t("上涨创新高且MACD顶背驰 -> 预判1卖", () => {
    const bis = [
      bi("up", 1000, 4000, 2000, 4100),
      bi("down", 2000, 4100, 2500, 4050),
      bi("up", 2500, 4050, 3000, 4120),
    ];
    const macd = [...macdRun(1000, 1999, 5), ...macdRun(2000, 2499, 0), ...macdRun(2500, 2999, 1)];
    const r = predictPeriod({ res: "60", upperRes: "240", bis, upperBis: [], macdArr: macd, atr: 2, lastPrice: 4120, lastBarTime: 3000 });
    assert.ok(r.status.includes("预判1卖"), r.status);
    assert.strictEqual(r.pred.type, "预判1卖");
  });

  t("上涨创新高但MACD未背驰 -> 上涨延续，不预判1卖", () => {
    const bis = [
      bi("up", 1000, 4000, 2000, 4100),
      bi("down", 2000, 4100, 2500, 4050),
      bi("up", 2500, 4050, 3000, 4120),
    ];
    // cur 区间红柱面积更大（5 > 1）→ 无顶背驰
    const macd = [...macdRun(1000, 1999, 1), ...macdRun(2000, 2499, 0), ...macdRun(2500, 2999, 5)];
    const r = predictPeriod({ res: "60", upperRes: "240", bis, upperBis: [], macdArr: macd, atr: 2, lastPrice: 4120, lastBarTime: 3000 });
    assert.ok(r.status.includes("上涨延续"), r.status);
    assert.strictEqual(r.pred, null);
  });

  console.log("== 预判1买（创新低+底背驰） ==");
  t("下跌创新低且MACD底背驰 -> 预判1买", () => {
    const bis = [
      bi("down", 1000, 4200, 2000, 4100),
      bi("up", 2000, 4100, 2500, 4150),
      bi("down", 2500, 4150, 3000, 4080),
    ];
    // refer 区间绿柱大，cur 区间绿柱小 → 底背驰
    const macd = [...macdRun(1000, 1999, -5), ...macdRun(2000, 2499, 0), ...macdRun(2500, 2999, -1)];
    const r = predictPeriod({ res: "60", upperRes: "240", bis, upperBis: [], macdArr: macd, atr: 2, lastPrice: 4080, lastBarTime: 3000 });
    assert.ok(r.status.includes("预判1买"), r.status);
    assert.strictEqual(r.pred.type, "预判1买");
  });

  console.log("== 预判3卖 ==");
  t("2卖后创新低跌破前底 + 当前反弹不破前底 -> 预判3卖", () => {
    const upperBis = [bi("down", 100, 5000, 1000, 4300)];
    const bis = [
      bi("down", 150, 4600, 250, 4400),
      bi("up", 250, 4400, 350, 4600),   // 2卖（上级下跌段内次高点）
      bi("down", 350, 4600, 500, 4450),
      bi("up", 500, 4450, 600, 4520),
      bi("down", 600, 4520, 800, 4380), // 创新低跌破前底 4400
      bi("up", 800, 4380, 900, 4395),   // 当前反弹不破前底
    ];
    const r = predictPeriod({ res: "240", upperRes: "D", bis, upperBis, macdArr: [], atr: 5, lastPrice: 4395, lastBarTime: 900 });
    assert.ok(r.status.includes("预判3卖"), r.status);
    assert.strictEqual(r.pred.type, "预判3卖");
  });

  console.log("== 预判3买 ==");
  t("2买后创新高突破前顶 + 当前回调不破前顶 -> 预判3买", () => {
    const upperBis = [bi("up", 100, 4000, 1000, 5000)];
    const bis = [
      bi("up", 150, 4300, 250, 4500),
      bi("down", 250, 4500, 350, 4400), // 2买（上级上涨段内抬高低点）
      bi("up", 350, 4400, 500, 4550),
      bi("down", 500, 4550, 600, 4480),
      bi("up", 600, 4480, 800, 4620),   // 创新高突破前顶 4500
      bi("down", 800, 4620, 900, 4520), // 当前回调不破前顶 4500
    ];
    const r = predictPeriod({ res: "240", upperRes: "D", bis, upperBis, macdArr: [], atr: 5, lastPrice: 4520, lastBarTime: 900 });
    assert.ok(r.status.includes("预判3买"), r.status);
    assert.strictEqual(r.pred.type, "预判3买");
  });

  console.log("== 边界：不满足条件 ==");
  t("上级最后一笔为下跌笔，本周期反弹 -> 不预判2卖（上级底已确认且价格升破，落入区间套描述）", () => {
    const upperBis = [bi("down", 100, 5000, 1000, 4300)];
    const bis = [
      bi("down", 1000, 4300, 1500, 4280),
      bi("up", 1500, 4280, 2000, 4500),
    ];
    const r = predictPeriod({ res: "240", upperRes: "D", bis, upperBis, macdArr: [], atr: 5, lastPrice: 4500, lastBarTime: 2000 });
    assert.ok(r.status.includes("上级上涨段内部"), r.status);
    assert.ok(!r.status.includes("预判2卖"), r.status);
    assert.strictEqual(r.pred, null);
  });

  t("上级顶后反弹已回落 -> 2卖运行中（不等待2买）", () => {
    const upperBis = [bi("up", 100, 4000, 1000, 5000)];
    const bis = [
      bi("down", 1000, 5000, 1100, 4600),
      bi("up", 1100, 4600, 1200, 4950),
    ];
    const r = predictPeriod({ res: "240", upperRes: "D", bis, upperBis, macdArr: [], atr: 5, lastPrice: 4800, lastBarTime: 1300 });
    assert.ok(r.status.includes("2卖运行中"), r.status);
    assert.ok(!r.status.includes("等待2买"), r.status);
    assert.strictEqual(r.pred, null);
  });

  t("上级底后回调已反弹 -> 2买运行中", () => {
    const upperBis = [bi("down", 100, 5000, 1000, 4000)];
    const bis = [
      bi("up", 1000, 4000, 1500, 4400),
      bi("down", 1500, 4400, 1600, 4050),
    ];
    const r = predictPeriod({ res: "240", upperRes: "D", bis, upperBis, macdArr: [], atr: 5, lastPrice: 4200, lastBarTime: 1700 });
    assert.ok(r.status.includes("2买运行中"), r.status);
    assert.strictEqual(r.pred, null);
    assert.ok(r.reason.includes("2买点"), r.reason);
  });

  // 区间套（走势完美）：上级顶/底已确认且价格已跌破/升破 → 本周期处于上级下跌/上涨段内部
  t("上级顶已确认且价格跌破上级顶，本级别最后上涨笔终点即上级顶 -> 等待反弹做2卖", () => {
    const upperBis = [bi("up", 100, 4400, 1000, 4600)]; // 4小时顶 4600
    const bis = [
      bi("down", 700, 4550, 900, 4500),
      bi("up", 900, 4500, 1000, 4600), // 1小时最后上涨笔终点 4600 = 上级顶
    ];
    const r = predictPeriod({ res: "60", upperRes: "240", bis, upperBis, macdArr: [], atr: 5, lastPrice: 4480, lastBarTime: 1100 });
    assert.ok(r.status.includes("等待反弹做2卖"), r.status);
    assert.ok(r.status.includes("下跌第一笔"), r.status);
    assert.ok(!r.status.includes("等待2买"), r.status);
    assert.strictEqual(r.pred, null);
  });

  t("上级底已确认且价格升破上级底，本级别最后下跌笔终点即上级底 -> 等待回调做2买", () => {
    const upperBis = [bi("down", 100, 4600, 1000, 4400)]; // 4小时底 4400
    const bis = [
      bi("up", 700, 4450, 900, 4500),
      bi("down", 900, 4500, 1000, 4400), // 1小时最后下跌笔终点 4400 = 上级底
    ];
    const r = predictPeriod({ res: "60", upperRes: "240", bis, upperBis, macdArr: [], atr: 5, lastPrice: 4520, lastBarTime: 1100 });
    assert.ok(r.status.includes("等待回调做2买"), r.status);
    assert.ok(r.status.includes("上涨第一笔"), r.status);
    assert.ok(!r.status.includes("等待2卖"), r.status);
    assert.strictEqual(r.pred, null);
  });

  t("上级顶已确认且价格跌破上级顶，本级别已走出独立下跌笔 -> 上级下跌段内部下跌运行中等待反弹做2卖", () => {
    const upperBis = [bi("up", 100, 4400, 1000, 4600)];
    const bis = [
      bi("down", 700, 4550, 900, 4500),
      bi("up", 900, 4500, 1000, 4600),
      bi("down", 1000, 4600, 1100, 4520), // 已走出上级顶后的独立下跌笔
    ];
    const r = predictPeriod({ res: "60", upperRes: "240", bis, upperBis, macdArr: [], atr: 5, lastPrice: 4500, lastBarTime: 1150 });
    assert.ok(r.status.includes("上级下跌段内部"), r.status);
    assert.ok(r.status.includes("等待反弹做2卖"), r.status);
    assert.ok(!r.status.includes("下跌延续"), r.status);
    assert.strictEqual(r.pred, null);
  });

  // 用户场景：1小时下跌第一笔成笔（上级最后一笔已是下跌笔）后，1小时应判「上级下跌段内部下跌运行中」
  t("上级(240)顶后 1小时完成下跌第一笔(上级最后笔为down) -> 上级下跌段内部下跌运行中等待反弹做2卖", () => {
    const upperBis = [
      bi("down", 100, 4697, 700, 4564),
      bi("up", 700, 4564, 1000, 4631.98), // 4小时顶 4631.98
    ];
    const bis = [
      bi("up", 800, 4571, 900, 4618),
      bi("down", 900, 4618, 1000, 4631.98),
      bi("down", 1000, 4631.98, 1100, 4445.455), // 1小时下跌第一笔已成笔
    ];
    const r = predictPeriod({ res: "60", upperRes: "240", bis, upperBis, macdArr: [], atr: 5, lastPrice: 4454.99, lastBarTime: 1150 });
    assert.ok(r.status.includes("上级下跌段内部"), r.status);
    assert.ok(r.status.includes("等待反弹做2卖"), r.status);
    assert.ok(!r.status.includes("预判1买"), r.status);
    assert.strictEqual(r.pred, null);
  });

  // 用户场景：15分钟在1小时下跌第一笔内部只完成2笔（下跌+反弹），反弹后回落 → 2卖运行中（而非预判3卖）
  t("上级(60)顶后 15分钟完成2笔，反弹后回落 -> 2卖运行中", () => {
    const upperBis = [bi("up", 800, 4589, 1000, 4631.98)]; // 1小时顶 4631.98
    const bis = [
      bi("down", 700, 4614, 800, 4589),
      bi("up", 800, 4589, 1000, 4631.98),
      bi("down", 1000, 4631.98, 1100, 4445.455), // 15分钟下跌第一笔
      bi("up", 1100, 4445.455, 1200, 4467.185),  // 15分钟反弹笔（第2笔）
    ];
    const r = predictPeriod({ res: "15", upperRes: "60", bis, upperBis, macdArr: [], atr: 3, lastPrice: 4454.99, lastBarTime: 1250 });
    assert.ok(r.status.includes("2卖运行中"), r.status);
    assert.ok(!r.status.includes("预判3卖"), r.status);
    assert.ok(!r.status.includes("预判2卖"), r.status);
    assert.strictEqual(r.pred, null);
  });

  // 同场景但价格仍在反弹高点上方 → 预判2卖形成中
  t("上级(60)顶后 15分钟完成2笔，反弹未回落 -> 预判2卖形成中", () => {
    const upperBis = [bi("up", 800, 4589, 1000, 4631.98)];
    const bis = [
      bi("down", 700, 4614, 800, 4589),
      bi("up", 800, 4589, 1000, 4631.98),
      bi("down", 1000, 4631.98, 1100, 4445.455),
      bi("up", 1100, 4445.455, 1200, 4467.185),
    ];
    const r = predictPeriod({ res: "15", upperRes: "60", bis, upperBis, macdArr: [], atr: 3, lastPrice: 4468, lastBarTime: 1250 });
    assert.ok(r.status.includes("预判2卖形成中"), r.status);
    assert.strictEqual(r.pred.type, "预判2卖");
  });

  t("上级底已确认且价格升破上级底，本级别已走出独立上涨笔 -> 上级上涨段内部上涨运行中等待回调做2买", () => {
    const upperBis = [bi("down", 100, 4600, 1000, 4400)];
    const bis = [
      bi("up", 700, 4450, 900, 4500),
      bi("down", 900, 4500, 1000, 4400),
      bi("up", 1000, 4400, 1100, 4480), // 已走出上级底后的独立上涨笔
    ];
    const r = predictPeriod({ res: "60", upperRes: "240", bis, upperBis, macdArr: [], atr: 5, lastPrice: 4500, lastBarTime: 1150 });
    assert.ok(r.status.includes("上级上涨段内部"), r.status);
    assert.ok(r.status.includes("等待回调做2买"), r.status);
    assert.ok(!r.status.includes("上涨延续"), r.status);
    assert.strictEqual(r.pred, null);
  });

  console.log("== 震荡判定 ==");
  // 构造窄幅横盘K线：40 根，high/low 都在 [4453, 4467] 内，ATR=2.875
  const rangeBars = [];
  for (let i = 0; i < 40; i++) {
    const c = 4460 + (i % 3);
    rangeBars.push({ time: i, open: c, high: c + 2, low: c - 2, close: c });
  }
  // 窄幅横盘笔：涨跌交替，端点都在窄区间内
  const rangeBis = [
    bi("down", 100, 4467.185, 200, 4453.035),
    bi("up", 200, 4453.035, 300, 4462.755),
    bi("down", 300, 4462.755, 400, 4453.88),
    bi("up", 400, 4453.88, 500, 4460.12),
  ];
  t("窄幅横盘（K线区间小+笔端点区间小+涨跌交替）-> 判定为震荡", () => {
    const r = predictPeriod({ res: "3", upperRes: "60", bis: rangeBis, upperBis: [], macdArr: [], atr: 2.875, lastPrice: 4455, lastBarTime: 500, bars: rangeBars });
    assert.ok(r.status.includes("震荡"), r.status);
    assert.strictEqual(r.pred, null);
  });
  t("isRangeBound 直接判定窄幅横盘为真", () => {
    const rb = isRangeBound(rangeBis, rangeBars, 2.875);
    assert.ok(rb && rb.range, "应判定为震荡");
    assert.ok(rb.kAtr < 5.0, "K线区间应 < 5×ATR");
  });
  // 窗口内无笔：rangeBars 时间 0..39，rangeBis 时间 100..500（端点全在窗口外）
  t("40根K线内无笔 -> 跳过笔端点区间/涨跌交替，仅按K线区间判震荡", () => {
    const rb = isRangeBound(rangeBis, rangeBars, 2.875);
    assert.ok(rb && rb.range, "窗口内无笔时只要K线区间小就判震荡");
    assert.strictEqual(rb.winBiCount, 0, "窗口内应有 0 笔");
    assert.ok(rb.biAtr === 0, "窗口内无笔时 biAtr 应为 0（条件2跳过）");
    assert.ok(rb.alt === true, "窗口内无笔时 alt 应为 true（条件3跳过）");
  });
  // 窗口内有笔：构造端点落在窗口内但涨跌区间大（超过 7×ATR）的笔，应不判震荡
  t("窗口内有笔且笔端点区间大 -> 不判震荡", () => {
    const wideBars = [];
    for (let i = 0; i < 40; i++) wideBars.push({ time: i, open: 4460, high: 4462, low: 4458, close: 4460 });
    // ATR=2.875，7×ATR=20.125；笔端点 4453~4475 跨度 22 > 20.125
    const wideBis = [
      bi("down", 5, 4475, 15, 4453),
      bi("up", 15, 4453, 25, 4472),
      bi("down", 25, 4472, 35, 4455),
      bi("up", 35, 4455, 39, 4470),
    ];
    const rb = isRangeBound(wideBis, wideBars, 2.875);
    assert.ok(rb && !rb.range, "窗口内有笔但笔端点区间大不应判震荡");
    assert.ok(rb.winBiCount > 0, "窗口内应有笔");
  });
  t("趋势明确（K线区间大）不判震荡", () => {
    const trendBars = [];
    for (let i = 0; i < 40; i++) trendBars.push({ time: i, open: 4000 + i * 5, high: 4005 + i * 5, low: 3995 + i * 5, close: 4000 + i * 5 });
    const trendBis = [
      bi("down", 100, 4200, 200, 4100),
      bi("up", 200, 4100, 300, 4250),
      bi("down", 300, 4250, 400, 4050),
      bi("up", 400, 4050, 500, 4300),
    ];
    const rb = isRangeBound(trendBis, trendBars, 2.875);
    assert.ok(rb && !rb.range, "区间大不应判震荡");
  });
  t("bars 缺失时跳过震荡判定（不影响原逻辑）", () => {
    const r = predictPeriod({ res: "3", upperRes: "60", bis: rangeBis, upperBis: [], macdArr: [], atr: 2.875, lastPrice: 4455, lastBarTime: 500 });
    assert.ok(!r.status.includes("震荡"), "无 bars 不应判震荡");
  });

  console.log("== 辅助函数 ==");
  t("findRefer 跳过幅度不足的次级别回调", () => {
    const bis = [
      bi("up", 100, 4000, 200, 4100, 100),
      bi("down", 200, 4100, 300, 4070),
      bi("up", 300, 4070, 400, 4120, 50),
    ];
    const ref = findRefer(bis, 2);
    assert.ok(ref, "应找到参照上涨笔");
    assert.strictEqual(ref.endPrice, 4100);
  });

  t("checkThirdSell 反弹破前底则不预判3卖", () => {
    const bis = [
      bi("down", 100, 4600, 200, 4400),
      bi("up", 200, 4400, 300, 4600),
      bi("down", 300, 4600, 400, 4380),
      bi("up", 400, 4380, 500, 4420), // 反弹已破前底 4400
    ];
    const last2Sell = { time: 300, price: 4600 };
    assert.strictEqual(checkThirdSell(bis, last2Sell), null);
  });

  console.log("== 表格输出 ==");
  t("dispWidth 中文字符按全角宽 2 计算", () => {
    assert.strictEqual(dispWidth("D"), 1);
    assert.strictEqual(dispWidth("周期"), 4);
    assert.strictEqual(dispWidth("上涨"), 4);
  });

  t("padCell 按显示宽度补空格（中文对齐）", () => {
    assert.strictEqual(padCell("D", 4), "D   ");
    assert.strictEqual(padCell("周期", 4), "周期");
    assert.strictEqual(padCell("240", 4), "240 ");
  });

  t("printPeriodTable 输出含表头与边框的表格", () => {
    const logs = [];
    const orig = console.log;
    console.log = (s) => logs.push(s);
    try {
      printPeriodTable([{ res: "D", status: "等待2买", reason: "测试原因", pred: "预判2买" }]);
    } finally {
      console.log = orig;
    }
    const all = logs.join("\n");
    assert.ok(all.includes("┌"), "应有上边框");
    assert.ok(all.includes("└"), "应有下边框");
    assert.ok(all.includes("周期"), "应有表头");
    assert.ok(all.includes("状态"), "应有表头");
    assert.ok(all.includes("原因"), "应有表头");
    assert.ok(all.includes("预判"), "应有表头");
    assert.ok(all.includes("等待2买"), "应含状态内容");
    assert.ok(all.includes("预判2买"), "应含预判内容");
  });

  console.log(`\n完成 ${n} 个用例${process.exitCode ? "（有失败）" : "，全部通过"}`);
}

run();
