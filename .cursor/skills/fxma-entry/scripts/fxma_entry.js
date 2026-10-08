// -*- coding: utf-8 -*-
// 强分型均线V1（fxma_v1）工作台标记脚本 —— 与 py_chain/fx_ma.py 同一套规则的工作台侧实现。
//
// 规则（与引擎一致，详见 SKILL.md / SPEC.md）：
//   信号（每个所选进出场周期 P 独立，P 每根已收K线收盘评估）：
//     ① P 上最新买卖点（chan-core findBuyPoints/findSellPoints）属所选类别
//        （1买/1卖→1类，2买/类2买、2卖/类2卖→2类，3买/类3买、3卖/类3卖→3类；4类不交易）；
//        反向点出现后该点失效
//     ② 点之后出现强分型（实体口径：底=右肩收盘>左肩开盘，顶=右肩收盘<左肩开盘，
//        落差 ≥ strongFxMinPts 点；--strong-fx-on=0 跳过本条）
//     ③ 均线分离（按类别选均线对：1类=maFast1/maSlow1，2/3类=maFast2/maSlow2）：
//        当拍收盘 快线高于慢线≥crossMinPts（买）/ 低于慢线≥crossMinPts（卖）；
//        --ma-on=0 跳过本条
//     ④ 收盘站线（按类别选站线均线：1类=maStand1、2/3类=maStand2）：
//        当拍收盘价 买点须严格站上/卖点须严格站下该均线；--ma-stand-on=0 跳过本条
//     ⑤ 黄金分割附近（--fib-near-on=1 开，默认关；仅 2/3 类点，1 类点豁免）：
//        摆动段 = 前一同侧买卖点价格 → 其后至本点前（时间窗 (前点, 本点]）的
//        P 周期真实K线极值（买取最高/卖取最低），按 fibLevels 算回撤位，
//        本点价格须落在任一档位 ±fibNearPts（绝对点数）内；无前点/摆动退化不触发
//     ⑥ 次级别/次次级别背驰（--div-lower-on=1 开，默认关；缠论V1 lowerDiverge 同源：
//        双判据背驰+区间套下沉链校验，落在次级别还是次次级别由结构自动决定）：
//        窗口=点锚定·粘性——更低级别出现同向背驰点且其时间 ≥ 点时间−1根本周期K线
//        即通过，之后持续有效直到点作废/反向点；无更低级别数据时永不通过
//     ⑦ 上级周期同向（--upper-dir-on=1 开，默认关；全部类别）：上级周期
//        （30S→3→15→60→240）当前笔方向须与信号同向（买=up/卖=down）
//     ⑧ pointValidBars 根内齐备 + pointValidPts 盘中价距点极值上限（买=评估根最高价
//        −买点最低价、卖=卖点最高价−评估根最低价；超距只等待不作废）→ 触发（每点一次）
//        → 下一根 P 周期K线开盘成交（各条件开关全关=点属所选类别且未失效当拍即触发）
//   出场：K线盘中价触及 止损(进场∓stopPts)/止盈 → 即时按该触发价成交
//        （与实盘 MT5 SL/TP 同口径，不等收盘确认、不等下一根开盘；进场那根收盘后即判）；
//        同根双触按 sameBarPriority（默认止损优先）；期末未触发 mark-to-market。
//        止盈方式 tpMode 两选一（--tp-mode=）：
//        - points（默认）：止盈=进场±tpPts，触价全平（旧行为）；
//        - structure：每笔开半仓（可开仓手数÷2），同向容量制叠加（最多两笔）——
//          1/2类入场一半到前高/前低主动止盈（activeTp，目标=入场前最近已确认
//          笔端点（多头前高/空头前低，不要求已识别为买卖点）∓tpNearPts 容差；
//          亏损侧不设）+ 另一半走跟踪止损（新 3/类3/4/类4 同向点
//          出现止损只上移到点价∓tpTrailSlipPts，触发记 trailStop）；
//          3类入场只主动止盈（触及全平）。
//   互斥/容量：points 满仓即容量（同向一笔）；structure 半仓容量（同向最多两笔）；
//        mutexScope=global 全局同向 / perPeriod 每周期独立。
//
// 口径声明（与回测/实盘引擎的既有差异，同 mark-entry「工作台 vs 引擎」）：
//   本脚本用画笔落盘的最终笔快照 + 全窗口K线（事后视角）回放评估；
//   引擎用当下增量状态逐拍评估（无未来函数）。两者信号可能不完全一致，
//   属研究口径差（见 SPEC.md §2.1.5.2），不能混用。

const fs = require("fs");
const path = require("path");
const CDP = require("../../../../server-cdp/node_modules/chrome-remote-interface");

const core = require("../../chan-core/scripts/chan_core.js");
const { calcMACD, intervalSecOf, markWickBars, mergeBars, findFractals,
        findBuyPoints, findSellPoints, fmtT, lowerResOf } = core;
// 进出场模块（次级别背驰条件复用缠论V1 lowerDiverge 区间套下沉判定；require-safe，
// module.exports 见 mark_entry.js 尾部）
const { lowerDiverge } = require("../../mark-entry/scripts/mark_entry.js");

const CACHE_DIR = process.env.CHAN_CACHE_DIR || path.join(__dirname, "..", "..", "..", "..", ".cursor", "cache");
const cacheFile = (prefix, symbol) => path.join(CACHE_DIR, `${prefix}_${String(symbol).replace(/[^A-Za-z0-9_.-]/g, "_")}.json`);

// ============================================================
// 参数
// ============================================================

const args = process.argv.slice(2);
const DRY = args.includes("--dry");      // 只算不画（分析/测试用）
const DEBUG = DRY || args.includes("--debug");
const getStrArg = (name, def) => {
  const hit = args.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : def;
};
const getNumArg = (name, def) => {
  const v = parseFloat(getStrArg(name, ""));
  return Number.isFinite(v) ? v : def;
};

const FROM_DATE = getStrArg("from", "");
const ENTRY_RES = String(getStrArg("entry-res", "3,15,60")).split(",").map(s => s.trim()).filter(Boolean);
const POINT_CLASSES = new Set(String(getStrArg("point-classes", "1,2,2x,3,3x")).split(",").map(s => s.trim()).filter(Boolean));
const MA_ON = getStrArg("ma-on", "1") !== "0";       // 均线分离条件开关（"0"=关，缺省开）
const MA_TYPE = String(getStrArg("ma-type", "SMA")).toUpperCase() === "EMA" ? "EMA" : "SMA";
const MA_FAST1 = getNumArg("ma-fast-1", 8);
const MA_SLOW1 = getNumArg("ma-slow-1", 20);
const MA_FAST2 = getNumArg("ma-fast-2", 5);
const MA_SLOW2 = getNumArg("ma-slow-2", 8);
const CROSS_MIN_PTS = getNumArg("cross-min-pts", 2.0);
const MA_STAND_ON = getStrArg("ma-stand-on", "1") !== "0"; // 收盘站线条件开关（"0"=关，缺省开）
const MA_STAND_1 = getNumArg("ma-stand-1", 5);              // 一类点站线均线周期
const MA_STAND_2 = getNumArg("ma-stand-2", 5);              // 二三类点站线均线周期
const FIB_NEAR_ON = getStrArg("fib-near-on", "0") === "1";  // 黄金分割附近条件开关（"1"=开，默认关）
const FIB_LEVELS = parseFibLevels(getStrArg("fib-levels", "0.382,0.5,0.618"));
const FIB_NEAR_PTS = getNumArg("fib-near-pts", 5.0);        // 档位容差（绝对点数）
const UPPER_DIR_ON = getStrArg("upper-dir-on", "0") === "1"; // 上级周期同向条件开关（"1"=开，默认关）
const DIV_LOWER_ON = getStrArg("div-lower-on", "0") === "1"; // 次级别背驰条件开关（"1"=开，默认关）
const DIV_LOWER_REQ = getStrArg("div-lower-req", "1") !== "0"; // 条件性质（必选=1/可选=0）
const STRONG_FX_ON = getStrArg("strong-fx-on", "1") !== "0";  // 强分型条件开关（"0"=关，缺省开）
const STRONG_FX_MIN_PTS = getNumArg("strong-fx-min-pts", 0.0);
// 条件性质（必选=1/可选=0；必选不通过该类点不触发，可选仅参与满足数计票）
const MA_REQ = getStrArg("ma-req", "1") !== "0";
const MA_STAND_REQ = getStrArg("ma-stand-req", "1") !== "0";
const STRONG_FX_REQ = getStrArg("strong-fx-req", "1") !== "0";
const FIB_REQ = getStrArg("fib-req", "1") !== "0";
// 各类买卖点条件满足数（五选N；生效N=min(N,启用条件数)，缺省3=引擎旧 AND 行为）
const pickClamp = v => Math.min(5, Math.max(1, Math.round(v)));
const ENTRY_PICK = {
  "1": pickClamp(getNumArg("entry-pick-1", 3)),
  "2": pickClamp(getNumArg("entry-pick-2", 3)),
  "2x": pickClamp(getNumArg("entry-pick-2x", 3)),
  "3": pickClamp(getNumArg("entry-pick-3", 3)),
  "3x": pickClamp(getNumArg("entry-pick-3x", 3)),
};
const POINT_VALID_BARS = Math.max(0, Math.round(getNumArg("point-valid-bars", 0)));
const POINT_VALID_PTS = Math.max(0, getNumArg("point-valid-pts", 0)); // 点有效期（值；0=不限）
const STOP_PTS = getNumArg("stop-pts", 10.0);
const TP_PTS = getNumArg("tp-pts", 30.0);
const TP_MODE = String(getStrArg("tp-mode", "points")) === "structure" ? "structure" : "points";
const TP_NEAR_PTS = Math.max(0, getNumArg("tp-near-pts", 0.0));       // 到点容差（0=精确触价）
const TP_TRAIL_SLIP_PTS = Math.max(0, getNumArg("tp-trail-slip-pts", 1.0)); // 提损滑点
const SAME_BAR_PRIORITY = String(getStrArg("same-bar-priority", "stop")) === "tp" ? "tp" : "stop";
const MUTEX_SCOPE = String(getStrArg("mutex-scope", "global")) === "perPeriod" ? "perPeriod" : "global";
const LOTS = Math.max(1, Math.round(getNumArg("lots", 4))); // 手数整数（开/平仓整手口径）
const UPPER_OF = { "30S": "3", "3": "15", "15": "60", "60": "240" };
const POINT_CLASS = {
  "1买": 1, "1卖": 1,
  "2买": 2, "类2买": 2, "2卖": 2, "类2卖": 2,
  "3买": 3, "类3买": 3, "3卖": 3, "类3卖": 3,
};
// 买卖点标签 → 选择键（pointClasses 过滤与策略键粒度：类2/类3 与严格 2/3 分开选，
// 键 2x/3x → 策略键 fx2x*/fx3x*）；键集与 POINT_CLASS 保持一致
const POINT_SEL = {
  "1买": "1", "1卖": "1",
  "2买": "2", "类2买": "2x", "2卖": "2", "类2卖": "2x",
  "3买": "3", "类3买": "3x", "3卖": "3", "类3卖": "3x",
};

// 模块级参数快照（由 CLI 常量组装）：scanPeriodSignals 缺省用它；测试可注入覆盖
// （脚本在模块顶层解析 argv，require 时常量固定为默认值，故提供注入口）。
const MODULE_OPTS = {
  pointClasses: POINT_CLASSES, strongFxOn: STRONG_FX_ON, strongFxMinPts: STRONG_FX_MIN_PTS,
  strongFxReq: STRONG_FX_REQ,
  maOn: MA_ON, maType: MA_TYPE, crossMinPts: CROSS_MIN_PTS, maReq: MA_REQ,
  maFast1: MA_FAST1, maSlow1: MA_SLOW1, maFast2: MA_FAST2, maSlow2: MA_SLOW2,
  maStandOn: MA_STAND_ON, maStand1: MA_STAND_1, maStand2: MA_STAND_2, maStandReq: MA_STAND_REQ,
  fibNearOn: FIB_NEAR_ON, fibLevels: FIB_LEVELS, fibNearPts: FIB_NEAR_PTS, fibReq: FIB_REQ,
  entryPick: ENTRY_PICK,
  upperDirOn: UPPER_DIR_ON, divLowerOn: DIV_LOWER_ON, divLowerReq: DIV_LOWER_REQ,
  pointValidBars: POINT_VALID_BARS, pointValidPts: POINT_VALID_PTS,
  tpMode: TP_MODE, tpNearPts: TP_NEAR_PTS, tpTrailSlipPts: TP_TRAIL_SLIP_PTS,
};

const BUY_COLOR = "#F23645";
const SELL_COLOR = "#089981";
const EXIT_COLOR = "#FFEB3B";
const BAR_BUFFER = 30;

// ============================================================
// 纯函数（导出供单元测试）
// ============================================================

/** 黄金分割档位（逗号串）→ 去重保序浮点数组；逐元素校验 0<r<1（与 fx_ma.parse_fib_levels 同式）。 */
function parseFibLevels(s) {
  const out = [];
  for (const v of String(s).split(",").map(x => x.trim()).filter(Boolean)) {
    const r = parseFloat(v);
    if (!(r > 0 && r < 1)) throw new Error(`fibLevels 档位须满足 0<r<1（收到 ${v}）`);
    if (!out.includes(r)) out.push(r);
  }
  if (!out.length) throw new Error("fibLevels 至少一个档位（0<r<1）");
  return out;
}

/** SMA/EMA 序列：closes[i] → ma[i]（窗口未满为 null）。 */
function maSeries(closes, period, type) {
  const out = new Array(closes.length).fill(null);
  if (String(type).toUpperCase() === "EMA") {
    const k = 2.0 / (period + 1);
    let ema = null;
    for (let i = 0; i < closes.length; i++) {
      ema = ema === null ? closes[i] : closes[i] * k + ema * (1 - k);
      out[i] = ema;
    }
    return out;
  }
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

/**
 * 强分型（实体口径，与 fx_ma.strong_fx_after 同式）：返回 t（含）之后首个命中
 * 分型的 {time, confirmTime} 或 null。confirmTime = 右肩合并块的收盘时刻
 * （该块最后一根原始K线时间 + barSec——引擎口径：右肩块收盘后分型才可用）。
 */
function strongFxAfter(merged, fractals, t, kind, minPts, barSec) {
  for (const f of fractals) {
    if (f.type !== kind || f.time < t) continue;
    const i = f.mergedIdx;
    if (i - 1 < 0 || i + 1 >= merged.length) continue; // 左/右肩不完整
    const left = merged[i - 1], right = merged[i + 1];
    const diff = kind === "bottom" ? right.close - left.open : left.open - right.close;
    if (diff > 0 && diff >= (minPts || 0)) {
      return { time: f.time, confirmTime: right.time + barSec };
    }
  }
  return null;
}

/**
 * 单周期信号扫描（工作台口径：全窗口K线 + 最终笔快照，按时间轴回放）。
 * @param bars P 周期K线（升序）；periodBis 画笔落盘 {res: [bi]}；upperRes 上级周期键
 * @param opts 条件参数（缺省=模块 CLI 常量快照 MODULE_OPTS；测试可注入覆盖）
 * @returns signals [{periodX, direction, strategyKey, pointType, pointTime, signalTime,
 *                    signalPrice, entryIdx, fibLevel, fibGap, upperDir}]（未含互斥过滤；
 *                    entryIdx=成交K线下标）
 */
function scanPeriodSignals(P, bars, periodBis, upperRes, opts = MODULE_OPTS, lowerCtx = null) {
  const barSec = intervalSecOf(P) || 180;
  const closes = bars.map(b => b.close);
  const f1 = maSeries(closes, opts.maFast1, opts.maType), s1 = maSeries(closes, opts.maSlow1, opts.maType);
  const f2 = maSeries(closes, opts.maFast2, opts.maType), s2 = maSeries(closes, opts.maSlow2, opts.maType);
  const st1 = maSeries(closes, opts.maStand1, opts.maType), st2 = maSeries(closes, opts.maStand2, opts.maType);
  const merged = mergeBars(markWickBars(bars));
  const fractals = findFractals(merged);
  const macdArr = calcMACD(bars);
  // 次级别背驰候选（缠论V1 lowerDiverge 同源；最终快照一次算好，时间窗过滤逐拍做。
  // lowerCtx 缺失（无更低级别数据）→ 候选为空 → 条件永不通过，与引擎 P=3 无 30S 同语义）
  const divCandsByDir = (opts.divLowerOn && lowerCtx)
    ? { long: lowerDiverge(lowerCtx, P, "long"), short: lowerDiverge(lowerCtx, P, "short") }
    : null;
  const buys = findBuyPoints(periodBis[P] || [], periodBis[upperRes] || [], macdArr, barSec);
  const sells = findSellPoints(periodBis[P] || [], periodBis[upperRes] || [], macdArr, barSec);
  // 点按时间升序（回放时维护「当前最新点」——引擎语义：只有最新点可触发）
  const pts = [...buys.map(p => ({ ...p, side: "buy" })),
               ...sells.map(p => ({ ...p, side: "sell" }))].sort((a, b) => a.time - b.time);
  // 各点的同侧前一买卖点（黄金分割摆动段锚点；引擎 _fx_prev_point 对应物）
  const prevOf = new Map();
  { let lb = null, ls = null;
    for (const p of pts) { prevOf.set(p, p.side === "buy" ? lb : ls); if (p.side === "buy") lb = p; else ls = p; } }
  const signals = [];
  const fired = new Set();
  let pi = 0, curBuy = null, curSell = null;
  for (let i = 0; i < bars.length; i++) {
    const closeT = bars[i].time + barSec; // 决策拍 = 本根收盘 = 下一根开盘时刻
    while (pi < pts.length && pts[pi].time <= bars[i].time) {
      const p = pts[pi++];
      if (p.side === "buy") curBuy = p; else curSell = p;
    }
    for (const [pt, direction, kind] of [[curBuy, "long", "bottom"], [curSell, "short", "top"]]) {
      if (!pt) continue;
      const cls = POINT_CLASS[pt.type];
      const sel = POINT_SEL[pt.type];
      if (cls === undefined || !opts.pointClasses.has(sel)) continue;
      const key = `${P}|${pt.type}|${pt.time}`;
      if (fired.has(key)) continue;
      const opp = direction === "long" ? curSell : curBuy;
      if (opp && opp.time > pt.time) { fired.add(key); continue; } // 反向点已出现
      if (opts.pointValidBars > 0) {
        let after = 0;
        for (let j = 0; j <= i; j++) if (bars[j].time > pt.time) after++;
        if (after > opts.pointValidBars) { fired.add(key); continue; } // 超时作废
      }
      // 点有效期（值）：评估根盘中价距点极值（买=high−点价、卖=点价−low）；超距等待（点保持存活）
      if (opts.pointValidPts > 0) {
        const far = direction === "long" ? bars[i].high - pt.price : pt.price - bars[i].low;
        if (far > opts.pointValidPts) continue;
      }
      // ---- 条件计票（与引擎 fx_ma._fx_collect 同构，2026-10-08）：均线分离/收盘站线/
      //      强分型/黄金分割/次级别背驰 各带 启用开关+必选标志。必选不通过 → 跳过本拍；
      //      启用条件中通过数 ≥ 生效N（=min(entryPickN, 启用数)）才触发；全停用=点出现即触发。
      //      可选条件未通过只体现在票数中。每条件 ok=null 停用 / true 通过 / false 未过。
      let fxTime = null, crossGap = null;
      let fxOk = null, maOk = null;
      if (opts.strongFxOn) {
        const fx = strongFxAfter(merged, fractals, pt.time, kind, opts.strongFxMinPts, barSec);
        fxOk = !!(fx && fx.confirmTime <= closeT);
        if (fxOk) fxTime = fx.time;
      }
      if (opts.maOn) {
        const fa = cls === 1 ? f1[i] : f2[i], sl = cls === 1 ? s1[i] : s2[i];
        if (fa === null || sl === null) maOk = false;   // 均线未满周期 = 未过
        else {
          const diff = direction === "short" ? sl - fa : fa - sl;
          maOk = diff > 0 && diff >= opts.crossMinPts;
          if (maOk) crossGap = Math.round(diff * 10000) / 10000;
        }
      }
      // ④ 收盘站线（1类=maStand1、2/3类=maStand2；买=收盘严格站上、卖=严格站下）
      let standGap = null;
      let standOk = null;
      if (opts.maStandOn) {
        const sv = cls === 1 ? st1[i] : st2[i];
        if (sv === null) standOk = false;   // 站线均线未满周期 = 未过
        else {
          const gap = closes[i] - sv;
          standOk = direction === "long" ? gap > 0 : gap < 0;
          if (standOk) standGap = Math.round(gap * 10000) / 10000;
        }
      }
      // ⑤ 黄金分割附近（默认关；仅 2/3 类点参与计票，1 类点豁免）：摆动段 = 前一同侧
      //    买卖点价格 → 其后至本点前的 P 周期真实K线极值（买取最高/卖取最低）；
      //    本点价格距任一档位回撤 ≤ fibNearPts（绝对点数）
      let fibLevel = null, fibGap = null;
      let fibOk = null;
      if (opts.fibNearOn && cls !== 1) {
        const prev = prevOf.get(pt);
        if (!prev) fibOk = false;   // 无前一同侧买卖点（摆动段无锚点）
        else {
          let lo = 0;
          while (lo < bars.length && bars[lo].time <= prev.time) lo++;
          let hi = lo, ext = null;
          while (hi < bars.length && bars[hi].time <= pt.time) {
            const b = bars[hi];
            if (direction === "long") ext = ext === null ? b.high : Math.max(ext, b.high);
            else ext = ext === null ? b.low : Math.min(ext, b.low);
            hi++;
          }
          const swing = ext === null ? null
            : (direction === "long" ? ext - prev.price : prev.price - ext);
          if (swing === null || swing <= 0) fibOk = false;   // 摆动段退化
          else {
            let best = null; // {gap, level, ratio}
            for (const r of opts.fibLevels) {
              const level = direction === "long" ? ext - r * swing : ext + r * swing;
              const gap = Math.abs(pt.price - level);
              if (!best || gap < best.gap) best = { gap, level, ratio: r };
            }
            fibOk = best.gap <= opts.fibNearPts;
            if (fibOk) { fibLevel = best.ratio; fibGap = Math.round(best.gap * 10000) / 10000; }
          }
        }
      }
      // ⑥ 次级别/次次级别背驰（--div-lower-on=1 开，默认关；缠论V1 lowerDiverge 同源，
      //    级别归属由下沉链结构自动决定）。窗口=点锚定·粘性：候选点时间 ≥ 点时间−1根
      //    P 周期K线 且 ≤ 本拍收盘（快照口径防未来）即通过，之后持续有效直到点作废
      let divOk = null;
      let divRes = null, divTime = null;
      if (opts.divLowerOn) {
        const cands = (divCandsByDir && divCandsByDir[direction]) || [];
        const winStart = pt.time - barSec;
        const hit = cands.find(c => c.point.time >= winStart && c.point.time <= closeT);
        if (hit) { divOk = true; divRes = hit.res; divTime = hit.point.time; }
        else divOk = false;
      }
      // 计票：必选硬门槛 → 通过票数 ≥ 生效N（不满足点存活，等待后续拍补票）
      {
        const active = [["强分型", fxOk, opts.strongFxReq],
                        ["均线分离", maOk, opts.maReq],
                        ["收盘站线", standOk, opts.maStandReq],
                        ["黄金分割", fibOk, opts.fibReq],
                        ["次级别背驰", divOk, opts.divLowerReq]].filter(c => c[1] !== null);
        if (active.length) {
          if (active.some(c => c[2] && !c[1])) continue;   // 必选未过
          const pick = (opts.entryPick && opts.entryPick[sel]) || 3;
          const need = Math.min(pick, active.length);
          const got = active.filter(c => c[1]).length;
          if (got < need) continue;   // 票数不足
        }
      }
      // ⑦ 上级周期同向（默认关；全部类别）：上级当前笔（startTime ≤ 本根时间的最后一
      //    根笔，最终快照口径）方向须与信号同向（买=up/卖=down）
      let upperDir = null;
      if (opts.upperDirOn) {
        const ubis = periodBis[upperRes] || [];
        let cur = null;
        for (const b of ubis) { if (b.startTime <= bars[i].time) cur = b; else break; }
        if (!cur) continue; // 上级无笔
        upperDir = cur.type;
        if (upperDir !== (direction === "long" ? "up" : "down")) continue;
      }
      fired.add(key);
      signals.push({
        periodX: P, direction,
        strategyKey: `fx${sel}${direction === "long" ? "Buy" : "Sell"}`,
        pointType: pt.type, pointTime: pt.time, pointPrice: pt.price,
        strongFxTime: fxTime, crossGap, standGap,
        fibLevel, fibGap, divLowerRes: divRes, divLowerTime: divTime, upperDir,
        signalTime: closeT, signalPrice: closes[i], entryIdx: i + 1,
      });
    }
  }
  return signals;
}

/** P 周期全部买卖点（含 3/4类；升序，附 side）。提损扫描/信号扫描用。 */
function collectPeriodPoints(P, bars, periodBis, upperRes) {
  const barSec = intervalSecOf(P) || 180;
  const macdArr = calcMACD(bars);
  const buys = findBuyPoints(periodBis[P] || [], periodBis[upperRes] || [], macdArr, barSec);
  const sells = findSellPoints(periodBis[P] || [], periodBis[upperRes] || [], macdArr, barSec);
  return [...buys.map(p => ({ ...p, side: "buy" })),
           ...sells.map(p => ({ ...p, side: "sell" }))].sort((a, b) => a.time - b.time);
}

/**
 * P 周期已确认笔端点流（升序 {time, price, side: "high"|"low"}）。主动止盈目标位用
 * （与引擎 _fx_prev_bi_end 对应物：上笔终点=前高、下笔终点=前低；_forming 跳过）。
 */
function collectBiEnds(P, periodBis) {
  const out = [];
  for (const b of (periodBis[P] || [])) {
    if (b._forming) continue;
    if (b.type === "up") out.push({ time: b.endTime, price: b.endPrice, side: "high" });
    else if (b.type === "down") out.push({ time: b.endTime, price: b.endPrice, side: "low" });
  }
  return out;  // 笔升序且相邻笔共享端点 → 每端点恰一次、时间升序
}

/**
 * 同向容量 + 出场模拟（与引擎同口径：盘中触及触发价 → 即时按触发价成交）。
 * points：满仓=容量（同向一笔，同旧互斥）；structure：每笔半仓、同向最多两笔
 * （容量=可开仓手数）——1/2类主动止盈+跟踪止损对半分工，3类只主动止盈。
 * opts 可注入 tpMode/tpNearPts/tpTrailSlipPts/lots/ptsByP/biEndsByP（测试用；缺省取 CLI 常量）。
 * @returns 每个信号补齐 entryTime/entryPrice/stopRef/tpRef/exits/exitType/pnl/suppressed
 */
function applyMutexAndSimulate(signals, barsByP, contractMult = 1.0, opts = {}) {
  const o = {
    mutexScope: opts.mutexScope ?? MUTEX_SCOPE,
    sameBarPriority: opts.sameBarPriority ?? SAME_BAR_PRIORITY,
    stopPts: opts.stopPts ?? STOP_PTS,
    tpPts: opts.tpPts ?? TP_PTS,
    tpMode: opts.tpMode ?? TP_MODE,
    tpNearPts: opts.tpNearPts ?? TP_NEAR_PTS,
    tpTrailSlipPts: opts.tpTrailSlipPts ?? TP_TRAIL_SLIP_PTS,
    lots: opts.lots ?? LOTS,
    ptsByP: opts.ptsByP ?? null,
    biEndsByP: opts.biEndsByP ?? null,
  };
  const structure = o.tpMode === "structure";
  if (structure && o.lots < 2)
    throw new Error(`structure 模式可开仓手数须 ≥2 才能整手半仓（收到 ${o.lots}）`);
  // 开/平仓手数恒整数：半仓 = ⌊lots/2⌋ 向下取整（引擎 fx_ma entryLots 同口径）
  const entryLots = structure ? Math.floor(o.lots / 2) : Math.floor(o.lots);
  const trailTypes = new Set(["3买", "类3买", "4买", "类4买", "3卖", "类3卖", "4卖", "类4卖"]);
  const sorted = [...signals].sort((a, b) => a.signalTime - b.signalTime);
  // 容量作用域：global 按方向 / perPeriod 按 (P,方向)；占用 = 终局时刻晚于本信号时刻
  const scopeKey = (P, d) => o.mutexScope === "global" ? `g|${d}` : `p|${P}|${d}`;
  const holdingAt = (t, at) => !t.suppressed && (t.exitTime ?? Infinity) > at;
  const done = [];
  for (const s of sorted) {
    const at = s.signalTime;
    const k = scopeKey(s.periodX, s.direction);
    // 同粗类闸门（structure，与容量同作用域）：同粗类（1/2/3；类2/类3 归粗类）
    // 持仓期间不开第二笔；该类前一笔终局后即可再开
    if (structure) {
      const cls = POINT_CLASS[s.pointType];
      if (cls !== undefined) {
        const sameCls = done.find(t => t._scope === k && holdingAt(t, at)
                                       && POINT_CLASS[t.pointType] === cls);
        if (sameCls) { s.suppressed = true; continue; }
      }
    }
    const heldLots = done.filter(t => t._scope === k && holdingAt(t, at))
                         .reduce((sum, t) => sum + (t.lots || 0), 0);
    if (heldLots + entryLots > o.lots + 1e-9) { s.suppressed = true; continue; }
    const bars = barsByP[s.periodX] || [];
    const ei = s.entryIdx;
    if (ei <= 0 || ei >= bars.length) { s.suppressed = true; continue; } // 无下一根可成交
    const short = s.direction === "short";
    const entryPrice = bars[ei].open, entryTime = bars[ei].time;
    const d = short ? -1 : 1;
    s.entryTime = entryTime; s.entryPrice = entryPrice;
    s.lots = entryLots; s.lotsLeft = entryLots;
    s.exits = []; s.exitType = null; s.pnl = null; s._scope = k;
    s.stopRef = short ? entryPrice + o.stopPts : entryPrice - o.stopPts;
    s.tpRef = null; s.tpLots = null; s.tpTarget = null;
    if (!structure) {
      s.tpRef = short ? entryPrice - o.tpPts : entryPrice + o.tpPts;
    } else {
      // 主动止盈目标 = 入场点前最近的已确认笔端点（多头前高/空头前低，不要求已
      // 识别为买卖点；亏损侧/不存在 → 不设）
      const ends = (o.biEndsByP && o.biEndsByP[s.periodX]) || [];
      const wantSide = short ? "low" : "high";
      let tgt = null;
      for (const e of ends) { if (e.time >= s.pointTime) break; if (e.side === wantSide) tgt = e; }
      if (tgt && (short ? tgt.price < entryPrice : tgt.price > entryPrice)) {
        s.tpRef = short ? tgt.price + o.tpNearPts : tgt.price - o.tpNearPts;
        s.tpTarget = { type: short ? "前低" : "前高", time: tgt.time, price: tgt.price };
        // 1/2类主动止盈半份：⌊entryLots/2⌋ 至少 1 手（小手数退化为触及全平）；3类全平
        s.tpLots = POINT_CLASS[s.pointType] === 3 ? entryLots : Math.max(1, Math.floor(entryLots / 2));
      }
      s.trailRaised = false; s.trailMoves = [];
    }
    // 提损点流（structure 1/2类）：入场点之后同向 3/4类点，识别滞后一拍（本根先按旧止损判）
    const trailPts = structure && POINT_CLASS[s.pointType] !== 3 && o.ptsByP
      ? (o.ptsByP[s.periodX] || []).filter(p => trailTypes.has(p.type)
          && p.side === (short ? "sell" : "buy") && p.time > s.pointTime)
      : [];
    let ti = 0;
    const partPnl = () => s.exits.filter(e => e.lots)
      .reduce((sum, e) => sum + (e.price - entryPrice) * d * e.lots * contractMult, 0);
    for (let j = ei; j < bars.length; j++) { // 进场那根（j===ei）收盘后即参与判定
      const b = bars[j];
      const stopHit = short ? b.high >= s.stopRef : b.low <= s.stopRef;
      const tpHit = s.tpRef !== null && (short ? b.low <= s.tpRef : b.high >= s.tpRef);
      const pickTp = tpHit && (!stopHit || o.sameBarPriority === "tp");
      if (pickTp) {
        if (!structure) {
          s.exits.push({ type: "takeProfit", time: b.time, price: s.tpRef });
          s.exitType = "takeProfit"; s.exitTriggerTime = b.time;
          s.exitTime = b.time; s.exitPrice = s.tpRef;
          s.pnl = (s.tpRef - entryPrice) * d * s.lots * contractMult;
          break;
        }
        // structure 主动止盈：按 tpLots 部分平仓（一次性），剩余走止损
        const lots = Math.min(s.tpLots, s.lotsLeft);
        s.lotsLeft = s.lotsLeft - lots;
        s.exits.push({ type: "activeTp", time: b.time, price: s.tpRef, lots });
        s.tpRef = null;
        if (s.lotsLeft <= 1e-9) {  // 3类全平 / 剩余恰好平完 → 终局
          s.exitType = "activeTp"; s.exitTriggerTime = b.time;
          s.exitTime = b.time; s.exitPrice = s.exits[s.exits.length - 1].price;
          s.pnl = partPnl();
          break;
        }
        continue;
      }
      if (stopHit) {
        const et = structure && s.trailRaised ? "trailStop" : "stop";
        s.exits.push({ type: et, time: b.time, price: s.stopRef });
        s.exitType = et; s.exitTriggerTime = b.time;
        s.exitTime = b.time; s.exitPrice = s.stopRef;
        s.pnl = partPnl() + (s.stopRef - entryPrice) * d * s.lotsLeft * contractMult;
        break;
      }
      // 本根未触发 → 提损（点极值触及后下一根起按新止损判，近似引擎识别滞后）
      while (ti < trailPts.length && trailPts[ti].time <= b.time) {
        const q = trailPts[ti++];
        const newStop = short ? q.price + o.tpTrailSlipPts : q.price - o.tpTrailSlipPts;
        if (short ? newStop < s.stopRef : newStop > s.stopRef) {
          s.stopRef = newStop; s.trailRaised = true;
          s.trailMoves.push({ time: b.time, pointType: q.type, pointTime: q.time,
                             pointPrice: q.price, stopTo: newStop });
        }
      }
    }
    if (!s.exitType) { // 未终局 → mark-to-market（最新收盘；部分已平按已实现+剩余浮盈）
      const last = bars[bars.length - 1];
      s.state = "open";
      if (last) s.pnl = partPnl() + (last.close - entryPrice) * d * s.lotsLeft * contractMult;
    } else {
      s.state = "closed";
    }
    done.push(s);
  }
  return sorted;
}

// ============================================================
// 可见性 / 绘图（与 mark-entry 同款）
// ============================================================

function onlyThisInterval(res) {
  const s = String(res).toUpperCase();
  const NONE = { ticks: false, seconds: false, secondsFrom: 1, secondsTo: 59,
    minutes: false, minutesFrom: 1, minutesTo: 59, hours: false, hoursFrom: 1, hoursTo: 24,
    days: false, daysFrom: 1, daysTo: 366, weeks: false, weeksFrom: 1, weeksTo: 52,
    months: false, monthsFrom: 1, monthsTo: 12 };
  switch (s) {
    case "30S": return { ...NONE, seconds: true, secondsFrom: 30, secondsTo: 30 };
    case "3":   return { ...NONE, minutes: true, minutesFrom: 3, minutesTo: 3 };
    case "15":  return { ...NONE, minutes: true, minutesFrom: 15, minutesTo: 15 };
    case "60":
    case "1H":  return { ...NONE, hours: true, hoursFrom: 1, hoursTo: 1 };
    default:    return { ...NONE, minutes: true, minutesFrom: 1, minutesTo: 59 };
  }
}

// ============================================================
// 主流程
// ============================================================

async function main() {
  if (!FROM_DATE) { console.log("错误: 必须指定起始日期 --from=YYYY-MM-DD"); process.exit(1); }
  if (!ENTRY_RES.length) { console.log("错误: --entry-res 不能为空（可选 30S/3/15/60）"); process.exit(1); }
  let client;
  try {
    const targets = await CDP.List({ port: 9222 });
    const pg = targets.find(t => t.type === "page" && t.url.includes("tradingview.com"));
    if (!pg) { console.log("ERROR: 未找到 TradingView 页面"); process.exit(1); }
    client = await CDP({ target: pg.id, port: 9222 });
    await client.Page.enable();
    await client.Runtime.enable();
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    const curRes = await client.Runtime.evaluate({
      expression: `(function() {
        const chart = TradingViewApi.activeChart();
        return { symbol: chart.symbol(), resolution: String(chart.resolution()) };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    const SYMBOL = curRes.result.value.symbol;
    const originalRes = curRes.result.value.resolution;
    console.log("品种:", SYMBOL, "当前周期:", originalRes);
    console.log(`强分型均线V1：进出场周期 ${ENTRY_RES.join(",")}，类别 ${[...POINT_CLASSES].join(",")}，`
      + `强分型${STRONG_FX_ON ? "开" : "关"}，均线分离${MA_ON ? "开" : "关"}`
      + (MA_ON ? `（${MA_TYPE} ${MA_FAST1}/${MA_SLOW1}+${MA_FAST2}/${MA_SLOW2}，分离≥${CROSS_MIN_PTS}点）` : "")
      + `，收盘站线${MA_STAND_ON ? "开" : "关"}`
      + (MA_STAND_ON ? `（${MA_STAND_1}/${MA_STAND_2}）` : "")
      + `，黄金分割附近${FIB_NEAR_ON ? "开" : "关"}`
      + (FIB_NEAR_ON ? `（${FIB_LEVELS.join(",")}±${FIB_NEAR_PTS}点）` : "")
      + `，上级同向${UPPER_DIR_ON ? "开" : "关"}`
      + `，次级别背驰${DIV_LOWER_ON ? "开" : "关"}`
      + `，止损${STOP_PTS}/止盈${TP_PTS}点`
      + (TP_MODE === "structure"
         ? `（止盈方式=结构组合：每笔 ${LOTS / 2} 手半仓、容差 ${TP_NEAR_PTS}、提损滑点 ${TP_TRAIL_SLIP_PTS}）`
         : "（止盈方式=固定点数）")
      + `，互斥 ${MUTEX_SCOPE}`);

    // 依赖：画笔落盘（chan-bi 产出）
    const bisFile = cacheFile("bis", SYMBOL);
    if (!fs.existsSync(bisFile)) {
      console.log(`ERROR: 未找到笔数据文件 ${bisFile}，请先运行「画笔」（chan-bi）。`);
      process.exit(1);
    }
    const bisData = JSON.parse(fs.readFileSync(bisFile, "utf8"));
    if (bisData && bisData.analysisInvalidReason) throw new Error(bisData.analysisInvalidReason);
    if (bisData.symbol !== SYMBOL) {
      console.log(`ERROR: 笔数据属于 ${bisData.symbol}，与当前品种 ${SYMBOL} 不一致`);
      process.exit(1);
    }
    const periodBis = bisData.periods || {};
    for (const P of ENTRY_RES) {
      if (!(periodBis[P] || []).length) {
        console.log(`ERROR: 笔数据缺少周期 ${P}（30S 需画笔开启 30 秒级别）`);
        process.exit(1);
      }
    }

    const ensureResolution = async (targetRes) => {
      await client.Runtime.evaluate({
        expression: `TradingViewApi.activeChart().setResolution(${JSON.stringify(targetRes)});`,
        returnByValue: true, awaitPromise: true, timeout: 10000,
      });
      let lastLen = 0;
      for (let i = 0; i < 40; i++) {
        await sleep(500);
        const r = await client.Runtime.evaluate({
          expression: `(function() {
            const chart = TradingViewApi.activeChart();
            const items = chart.chartModel().mainSeries().data().m_bars._items;
            return { res: String(chart.resolution()), len: items ? items.length : 0 };
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 10000,
        });
        const v = r.result.value;
        if (v.res === targetRes && v.len > 0) { if (v.len === lastLen) break; lastLen = v.len; }
      }
    };

    const FROM_TS = Math.floor(new Date(`${FROM_DATE}T00:00:00Z`).getTime() / 1000);
    // 取数窗口：30S 只取最近 3 天（秒级历史极浅），分钟级取完整窗口
    const WINDOW_DAYS = { "30S": 3 };
    const fetchBars = async (fromTs, buffer, windowDays) => {
      const tolerance = 6 * 3600;
      for (let attempt = 0; attempt < 60; attempt++) {
        const dataRes = await client.Runtime.evaluate({
          expression: `(function() {
            const chart = TradingViewApi.activeChart();
            const items = chart.chartModel().mainSeries().data().m_bars._items;
            if (!items || items.length === 0) return { error: 'no_items' };
            const bars = items.map(i => ({ time: i.value[0], open: i.value[1], high: i.value[2], low: i.value[3], close: i.value[4] }));
            const fromTs = ${JSON.stringify(fromTs)};
            const windowDays = ${JSON.stringify(windowDays || null)};
            const tolerance = ${JSON.stringify(tolerance)};
            const latestTs = bars.length ? bars[bars.length - 1].time : 0;
            const effFrom = (fromTs && windowDays) ? Math.max(fromTs, latestTs - windowDays * 86400) : fromTs;
            if (bars[0].time > effFrom + tolerance) return { bars: [], notCovered: true, resolution: String(chart.resolution()) };
            const fromIdx = bars.findIndex(k => k.time >= effFrom);
            const start = Math.max(0, fromIdx - ${buffer || 0});
            return { bars: bars.slice(start), resolution: String(chart.resolution()) };
          })()`,
          returnByValue: true, awaitPromise: true, timeout: 15000,
        });
        const d = dataRes.result.value;
        if (!d || d.error) { await sleep(1200); continue; }
        if (d.notCovered) {
          await client.Runtime.evaluate({
            expression: `(function() {
              const chart = TradingViewApi.activeChart();
              const widget = chart._chartWidget || (chart.chartModel && chart.chartModel()._chartWidget);
              const ts = widget && widget.model ? widget.model().timeScale() : chart.chartModel().timeScale();
              ts.scrollToFirstBar(); return 'ok';
            })()`,
            returnByValue: true, awaitPromise: true, timeout: 10000,
          });
          await sleep(2500);
          continue;
        }
        return d;
      }
      return null;
    };

    // 次级别背驰条件：预取各进出场周期更低级别链的K线（lowerDiverge 需要次/次次级别
    // MACD；链映射与缠论V1 levelsBelow 一致——lowerResOf 逐级、3 之下挂 30S、无笔数据
    // 则止）。30S 沿用 3 天窗口；MACD 预热取 60 根缓冲。bis 用画笔落盘快照（与上级
    // 同向条件同口径）；级别归属由 lowerDiverge 下沉链自动决定。
    const lowerBarsByRes = {};
    const lowerCtxByP = {};
    if (DIV_LOWER_ON) {
      const chainBelow = (res) => {
        const chain = [];
        let cur = res;
        for (;;) {
          let nxt = lowerResOf(cur);
          if (nxt === null && String(cur) === "3") nxt = "30S";
          if (!nxt || !(periodBis[nxt] || []).length) break;
          chain.push(nxt);
          cur = nxt;
        }
        return chain;
      };
      const needed = new Set();
      for (const P of ENTRY_RES) chainBelow(P).forEach(r => needed.add(r));
      for (const res of needed) {
        await ensureResolution(res);
        const d = await fetchBars(FROM_TS, 60, WINDOW_DAYS[res] || null);
        if (d && d.bars && d.bars.length) {
          lowerBarsByRes[res] = d.bars;
          console.log(`[次级别背驰] 周期 ${res}：${d.bars.length} 根K线（MACD 源）`);
        } else {
          console.log(`[次级别背驰] 周期 ${res}：未读到K线，该级别不参与背驰候选`);
        }
      }
      for (const P of ENTRY_RES) {
        const ctx = {};
        for (const [res, bis] of Object.entries(periodBis)) ctx[res] = { bis };
        for (const [res, bars] of Object.entries(lowerBarsByRes)) {
          ctx[res] = { bis: periodBis[res] || [], macdArr: calcMACD(bars) };
        }
        lowerCtxByP[P] = ctx;
      }
    }

    // 逐周期：取K线 → 扫描信号（ptsByP=全部买卖点供提损扫描；biEndsByP=已确认
    // 笔端点流供主动止盈目标）
    const barsByP = {};
    const ptsByP = {};
    const biEndsByP = {};
    let allSignals = [];
    let drawRes = originalRes;
    for (const P of ENTRY_RES) {
      await ensureResolution(P);
      drawRes = P;
      const maWarm = Math.max(MA_SLOW1, MA_SLOW2) + 10;
      const d = await fetchBars(FROM_TS, maWarm > BAR_BUFFER ? maWarm : BAR_BUFFER, WINDOW_DAYS[P] || null);
      if (!d || !d.bars || !d.bars.length) {
        console.log(`[周期 ${P}] 未读到K线，跳过`);
        continue;
      }
      barsByP[P] = d.bars;
      if (TP_MODE === "structure") {
        ptsByP[P] = collectPeriodPoints(P, d.bars, periodBis, UPPER_OF[P]);
        biEndsByP[P] = collectBiEnds(P, periodBis);
      }
      const sigs = scanPeriodSignals(P, d.bars, periodBis, UPPER_OF[P], MODULE_OPTS, lowerCtxByP[P] || null);
      console.log(`[周期 ${P}] ${d.bars.length} 根K线，信号 ${sigs.length} 个`
        + (WINDOW_DAYS[P] ? `（窗口最近 ${WINDOW_DAYS[P]} 天）` : ""));
      allSignals = allSignals.concat(sigs);
    }

    // 同向容量 + 出场模拟
    allSignals = applyMutexAndSimulate(allSignals, barsByP, 1.0, { ptsByP, biEndsByP });
    const filled = allSignals.filter(s => !s.suppressed);
    const suppressed = allSignals.filter(s => s.suppressed);
    const nOf = t => filled.filter(s => s.exitType === t).length;
    console.log(`\n成交 ${filled.length} 笔（容量过滤 ${suppressed.length}）：`
      + (TP_MODE === "structure"
         ? `主动止盈 ${nOf("activeTp")} / 跟踪止损 ${nOf("trailStop")} / 固定止损 ${nOf("stop")}`
             + ` / 主动止盈部分平仓 ${filled.reduce((n, s) => n + (s.exits || []).filter(e => e.type === "activeTp" && e.lots && s.exitType !== "activeTp").length, 0)}`
         : `止损 ${nOf("stop")} / 止盈 ${nOf("takeProfit")}`)
      + ` / 仍持仓 ${filled.filter(s => s.state === "open").length}（每笔 ${TP_MODE === "structure" ? LOTS / 2 : LOTS} 手）`);

    // 落盘（工作台结果缓存；回测/监控引擎结果以 py_chain 为准）
    const outFile = cacheFile("fxma", SYMBOL);
    fs.writeFileSync(outFile, JSON.stringify({
      symbol: SYMBOL, strategy: "fxma_v1", generatedAt: new Date().toISOString(),
      params: { entryRes: ENTRY_RES, pointClasses: [...POINT_CLASSES],
                maOn: MA_ON, maType: MA_TYPE,
                maFast1: MA_FAST1, maSlow1: MA_SLOW1, maFast2: MA_FAST2, maSlow2: MA_SLOW2,
                crossMinPts: CROSS_MIN_PTS,
                maStandOn: MA_STAND_ON, maStand1: MA_STAND_1, maStand2: MA_STAND_2,
                fibNearOn: FIB_NEAR_ON, fibLevels: FIB_LEVELS, fibNearPts: FIB_NEAR_PTS,
                upperDirOn: UPPER_DIR_ON, divLowerOn: DIV_LOWER_ON,
                strongFxOn: STRONG_FX_ON, strongFxMinPts: STRONG_FX_MIN_PTS,
                strongFxReq: STRONG_FX_REQ, maReq: MA_REQ,
                maStandReq: MA_STAND_REQ, fibReq: FIB_REQ, entryPick: ENTRY_PICK,
                pointValidBars: POINT_VALID_BARS, pointValidPts: POINT_VALID_PTS,
                stopPts: STOP_PTS, tpPts: TP_PTS,
                tpMode: TP_MODE, tpNearPts: TP_NEAR_PTS, tpTrailSlipPts: TP_TRAIL_SLIP_PTS,
                sameBarPriority: SAME_BAR_PRIORITY, mutexScope: MUTEX_SCOPE, lots: LOTS },
      signals: allSignals,
    }), "utf8");
    console.log("已落盘:", outFile);

    if (DRY) { // --dry：不画图（分析/测试用）
      await ensureResolution(originalRes);
      await client.close();
      return;
    }

    // 绘图：清除旧标记 → 画进场箭头 + 出场标记（只本周期显示）
    const clearTitle = async (TITLE) => {
      const r = await client.Runtime.evaluate({
        expression: `(function() {
          const chart = TradingViewApi.activeChart();
          const out = { cleared: 0 };
          const readTitle = (id) => {
            try {
              const sh = chart.getShapeById(id);
              const props = sh && sh._source && sh._source._properties;
              return props && props.title ? String(props.title._value) : '';
            } catch(e) { return ''; }
          };
          try {
            for (const s of chart.getAllShapes()) {
              if (readTitle(s.id) === "${TITLE}") {
                try { chart.removeEntity(s.id); out.cleared++; } catch(e) {}
              }
            }
          } catch(e) {}
          return out;
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };
    const drawShapes = async (TITLE, IV, items) => {
      const r = await client.Runtime.evaluate({
        expression: `(async function() {
          const chart = TradingViewApi.activeChart();
          const ITEMS = ${JSON.stringify(items)};
          const IV_CFG = ${JSON.stringify(IV)};
          const out = { ok: 0, err: [] };
          const applyIV = (id) => {
            if (!IV_CFG) return;
            try {
              const iv = chart.getShapeById(id)._source._properties.intervalsVisibilities;
              for (const [k, v] of Object.entries(IV_CFG)) { try { iv[k].setValue(v); } catch(e) {} }
            } catch(e) {}
          };
          for (const it of ITEMS) {
            try {
              const id = await chart.createMultipointShape(
                [{ time: it.time, price: it.price }],
                { shape: it.shape, lock: false, text: it.text || "",
                  overrides: { color: it.color, arrowColor: it.color, textColor: it.color, title: "${TITLE}" } });
              applyIV(id);
              out.ok++;
            } catch(err) { out.err.push(err.message); }
          }
          return out;
        })()`,
        returnByValue: true, awaitPromise: true, timeout: 30000,
      });
      return r.result.value;
    };

    const EXIT_LABEL = { stop: "止损", takeProfit: "止盈", activeTp: "主动止盈", trailStop: "跟踪止损" };
    for (const P of ENTRY_RES) {
      if (P !== drawRes) { await ensureResolution(P); drawRes = P; }
      await clearTitle(`ENTRY_FX_${P}`);
      await clearTitle(`EXIT_FX_${P}`);
      const entries = filled.filter(s => s.periodX === P);
      if (!entries.length) continue;
      const entryItems = entries.map(s => ({
        time: s.entryTime, price: s.entryPrice,
        shape: s.direction === "long" ? "arrow_up" : "arrow_down",
        color: s.direction === "long" ? BUY_COLOR : SELL_COLOR,
        text: s.strategyKey,
      }));
      // 出场标记：逐 exits 事件（structure 部分主动止盈+终局各一个；仍持仓不画）
      const exitItems = [];
      for (const s of entries) {
        for (const e of (s.exits || [])) {
          exitItems.push({
            time: e.time, price: e.price,
            shape: s.direction === "long" ? "arrow_down" : "arrow_up",
            color: EXIT_COLOR,
            text: EXIT_LABEL[e.type] || e.type,
          });
        }
      }
      const r1 = await drawShapes(`ENTRY_FX_${P}`, onlyThisInterval(P), entryItems);
      const r2 = exitItems.length ? await drawShapes(`EXIT_FX_${P}`, onlyThisInterval(P), exitItems) : { ok: 0 };
      console.log(`[周期 ${P}] 进场箭头 ${r1.ok} 个，出场标记 ${r2.ok} 个`);
    }
    await ensureResolution(originalRes);
    console.log("\n已切回原周期:", originalRes);
    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
    process.exitCode = 1;
  }
}

module.exports = { maSeries, strongFxAfter, scanPeriodSignals, collectPeriodPoints,
                   collectBiEnds, applyMutexAndSimulate,
                   parseFibLevels, MODULE_OPTS, POINT_CLASS, POINT_SEL, UPPER_OF };

if (require.main === module) {
  main();
}
