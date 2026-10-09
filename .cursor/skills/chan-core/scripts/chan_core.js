/**
 * 缠论算法核心（唯一算法源）
 * 纯函数模块，不依赖 CDP、不绘图。供 chan-bi（画笔）与 mark-buy-sell（买卖点）两个 SKILL 复用。
 *
 * 统一约定：
 *   - 笔对象字段：type(up/down)、startIdx/endIdx(合并K线索引)、startTime/endTime(校准后端点时间)、
 *     startPrice/endPrice、rawCount(覆盖原始K线数)、span(幅度)、gapLocked(跳空成笔)、macdCross(MACD变色成笔)
 *   - 配置：CHAN_CFG.gapFilter（跳空独立成笔阈值，默认 1.0）、CHAN_CFG.debug（调试打印）
 *   - 所有时间均为 Unix 秒（UTC），与 TradingView K线时间一致
 *
 * 用法：
 *   const core = require("./chan_core.js");
 *   core.CHAN_CFG.debug = DEBUG;
 *   core.CHAN_CFG.gapFilter = GAP_FILTER;
 *   const merged = core.mergeBars(rawBars);
 *   ...
 */

// ============================================================
// 配置
// ============================================================

const CHAN_CFG = {
  expectBiEnough: true,
  expectBiMinBars: 5,
  gapFilter: 1.0, // 跳空独立成笔阈值：相邻K线缺口 >= gapFilter*ATR 时强制独立成笔
  // ---- 长影线修复（markWickBars：冲高/探底插针压平 + 端点候选价；关=整体停用）----
  wickMarkOn: true,
  wickRatio: 0.70, // 长影剔除：影线占整根K线振幅的比例阈值（>= 时视为冲高/探底插针）
  wickMinLen: 0.5, // 长影剔除：影线绝对长度下限（2026-10-02 起为具体数值，品种报价单位
                   // 价差；原 wickAtrK×ATR 系数口径废除，与 py_chain/chan_core.py 同步）
  wickMinRange: 15, // 长影剔除前提（2026-10-02，与 py_chain/chan_core.py 同步）：整根K线价差
                    // （最高-最低）须 > 该值才判插针压平（绝对价差，窄幅K线即使占比/长度
                    // 达标也不处理）。0=不限价差（回退旧口径）
  // 分型邻侧影线真实价（2026-10-02，默认关=现行行为）：开启后 findFractals 的左右邻
  // 比较用压平前真实影线价——高点取 max(high, _wickHigh)、低点取 min(low, _origLow)。
  // 动机：邻K长上影被 markWickBars 压平后会误杀中间分型（60m 2026-09-29 04:00 底
  // 4111.52 被 06:00 压平上影 4121.69 卡掉高点侧，差 2.00，近等双底候选直接不存在）。
  // 中心K仍用压平结构价（压平语义不变），只修邻侧；不含压平K的块零影响。
  // 与 py_chain/chan_core.py CHAN_CFG.fractalSideRealWick 同步。
  fractalSideRealWick: false,
  // 端点内部极值恢复（2026-10-09，默认开）：fixBiExtremes 除「终点分型中心及其后」外，
  // 增加「笔内部块」（startIdx+1..endIdx-1）后向扫描——被包含合并吞掉的更极端真低/真高
  // （如 60m 2026-10-07 20:00 插针 4066.53 被升序合并吃掉后底分型落在 21:00 4082.42）
  // 恢复为笔端点价/时。只改端点，不动合并结构与分型；关=旧行为。
  // 与 py_chain/chan_core.py CHAN_CFG.endInnerRecoverOn 同步。
  endInnerRecoverOn: true,
  // 顶底分形不能包含（单根长K豁免）：该周期单根K线振幅（高-低）≥ 对应点数时，
  // 不参与分型终点侧三根的反向贯穿检查。按周期取值（wideBarPointsOf）；0=该周期不豁免。
  // wideBarOn=false 时所有周期一律不豁免（wideBarPointsOf 直接返回 0）。
  // 日线/4小时/1小时/15分钟/3分钟默认均为 30 点。
  wideBarOn: true,
  wideBarPointsD: 30,
  wideBarPoints240: 30,
  wideBarPoints60: 30,
  wideBarPoints15: 30,
  wideBarPoints3: 30,
  divergeDurRatio: 3, // 背驰面积判据的时长可比上限：面积Σ = 柱高×K线根数、与区间时长线性相关，
                      // 两段时长比 > 该值时不具可比性，面积项不计入背驰（只用 DIF/柱高判据）
  debug: false,   // 调试打印（buildBi / 买卖点识别过程）
  nearDoubleFixed: 2.0, // 近等双顶/双底固定容差（品种报价单位绝对价差，如黄金 2.0=2 美元）；
                        // thr = 该值（2026-10-02 起取消 ATR 项/价格比例项/15m双动能确认）。
                        // 比较顺序：先影线（更极端直接替换后移），影线不满足再比实体——
                        // 后实体不低于前实体直接后移，更低则差 ≤ 该值才后移；
                        // 中间真实回调深度闸门、反弹不成笔分支同用该值
  // ---- 近等双顶每周期开关（2026-09-25 参数化；此前硬编码仅 ≥1h 开启，默认=现行行为）----
  // 开启该周期「近等双顶/双底平台取后顶/后底」；gating 统一走 nearDoubleOn(res)，
  // 五周期之外（30S/5/30/W 等）一律不开启。3m/15m 默认开（2026-09-28 与参数页已改值对齐）。
  nearDouble3: true,
  nearDouble15: true,
  nearDouble60: true,
  nearDouble240: true,
  nearDoubleD: true,
  // 近等后顶/后底（反弹不成笔）取后：阶段二「间隔不足→回溯替换」分支的扩展，详见该分支注释。
  // 关闭后仅 k.locked（上级笔端点，区间套强制落地）路径仍生效。
  nearDoubleRebound: true,
  // 近等取后让位锁定（2026-10-02，默认关=现行行为）：锁定端点唯一允许的移动方式 =
  // 近等双顶/双底平台取后（biStep 同类型分支锁定提前 continue 处放行，全部闸门照常）。
  // 代价：开启后下级端点可能不再与上级端点重合（区间套一致性让位于平台取后），
  // 历史上被锁定的近等平台都会取后，回测基线不可比。全周期统一（还需各周期
  // nearDouble3/15/60/240/D 开启；60m 的 04:00 型案例还需 fractalSideRealWick）。
  // 与 py_chain/chan_core.py CHAN_CFG.nearDoubleShiftLocked 同步。
  nearDoubleShiftLocked: false,
  // ---- 三处规则修复 + 跨级下沉（2026-09-26；与 py_chain/chan_core.py 对齐）----
  anchorUndecidedSkip: true,    // A 未定型不接管：2/3类点 after 不存在/未达根数时不接管锚点（默认开）
  anchorUndecidedMinBars: 2,    // A 定型阈值（点后反向段本级合并块数；2=右肩+1根确认）
  divergeReferByZs: true,       // B 背驰中枢参照：中枢内部段不参与比较，参照=入中枢段（默认开）
  sinkSkipLevel: true,          // D 跨级下沉：次级展开<3笔/方向不符/端点含糊时跳级继续向下（默认开）
  pointEnoughForming: true,     // C-2 成笔可能够笔：形成段 enough 计数只到极值块（默认开）
  synthIntrabarBars: true,      // C-1 盘中合成K（回测引擎侧实现；JS 标记端仅透传；默认开）
  // ---- 小周期绘制/加载窗口（2026-10-02 参数化；与 py_chain/chan_core.py CHAN_CFG 同步）----
  // 由 chan-bi/mark-buy-sell/mark-entry 三脚本消费：DRAW_WINDOW_DAYS 改由此构造。
  // 3分钟/15分钟/30秒 只画（并只加载）最近 N 天；60m/240m/D 不限、从 --from 全量。
  // 值 0 = 该周期不限窗口（仅手动 --chan-cfg 可传 0；参数页 min=1）。
  windowDays3: 15,
  windowDays15: 30,
  windowDays30S: 3,
};

// ============================================================
// 0. 长影线标记（冲高/探底插针：影线可成端点、不参与区间竞争）
// ============================================================

/**
 * 长影线处理（冲高插针，压平 + 端点候选价）：
 *   前提：整根K线价差（high-low）> wickMinRange（绝对价差，窄幅K线整体不判插针）。
 *   影线占比 >= wickRatio 且影线长度 >= wickMinLen（绝对长度下限，2026-10-02 起为
 *   具体数值，原 wickAtrK×稳定ATR 系数口径废除）的长上影K线一律压平 high 至实体顶
 *   （保持历史验收的合并/笔结构——避免影线价参与合并改变结构或污染笔区间），但：
 *   若该 bar 的 low 不低于左右相邻原始K线低点（压平会消灭一个本可成立的顶分型中心，
 *   如 60m 7-16 02:00 bar L4058.10 > 01:00 L4033.11 且 > 03:00 L4048.10），
 *   记 `_topCand = 原 high`——findFractals 在该 bar（或其合并 bar）成为顶分型中心时
 *   用影线价作端点价（7-15 反弹笔顶 = 4081.52），结构本身保持压平版。
 *   反之（low 条件不满足，如 15m 9-3 16:00 bar L4428.135 < 16:15 L4430.735、
 *   60m 8-28 22:00 bar L4530.02 < 23:00 L4524.125）→ 纯压平：插针本就不成顶分型，
 *   影线价不出现，不会阻止 17:00 4442.04 等合法顶成笔。
 * 下影探底插针不处理（原值保留）：探底低点可被 fixBiExtremes 恢复为端点
 * （60m 7-29 4010.41、7-15 16:00 底），属用户认可行为。
 * ATR 基准已废除（2026-10-02）：长度下限 wickMinLen 为具体数值，不随行情波动漂移。
 *
 * @param {Array} rawBars 原始K线 [{time,open,high,low,close}, ...]（不原地修改）
 * @returns {Array} 处理后的K线数组：长上影 high 压平，可成顶分型中心的带 _topCand
 */
function markWickBars(rawBars) {
  if (CHAN_CFG.wickMarkOn === false) return rawBars.map((b) => ({ ...b }));
  const ratio = CHAN_CFG.wickRatio;
  const minWick = CHAN_CFG.wickMinLen ?? 0.5;
  const minRange = CHAN_CFG.wickMinRange ?? 0;
  const out = [];
  const len = rawBars.length;
  for (let idx = 0; idx < len; idx++) {
    const bar = rawBars[idx];
    const b = { ...bar };
    const amp = b.high - b.low;
    if (amp > minRange) {
      const bodyTop = Math.max(b.open, b.close);
      const bodyBottom = Math.min(b.open, b.close);
      const upper = b.high - bodyTop;
      const lower = bodyBottom - b.low;
      if (upper >= ratio * amp && upper >= minWick) {
        // 长上影（冲高插针）：high 一律压平至实体顶（与历史验收的合并/笔结构一致，
        // 避免影线价参与合并改变结构或污染笔区间——基线中 60m 8-28 的 4631.98
        // 弱反弹、15m 9-3 16:00 的 4443.715 均因此被拒/不成端点）；但若该 bar 的
        // low 不低于左右相邻原始K线低点（压平会消灭一个本可成立的顶分型中心端点，
        // 如 60m 7-16 02:00 bar L4058.10 > 01:00 L4033.11 且 > 03:00 L4048.10，
        // 用户要求其 4081.52 成为 7-15 反弹笔顶），记 _topCand = 原 high ——
        // findFractals 在该 bar 成为顶分型中心时用 _topCand 作端点价（影线可成端点），
        // 结构本身保持压平版。
        const prev = rawBars[idx - 1];
        const next = rawBars[idx + 1];
        if (prev && next && b.low >= prev.low && b.low >= next.low) {
          b._topCand = b.high;
        }
        // 包含判断仍用压平前的真实高点，避免压平造出原本不存在的包含
        b._preHigh = b.high;
        b.high = bodyTop;
      } else if (lower >= ratio * amp && lower >= minWick) {
        // 长下影（探底插针）：low 压平至实体底（结构/区间竞争保持压平语义，
        // 与历史验收结构一致——4010.41/4017.475 等探底端点不受影响，其 bar 的
        // 下影占比不足或由分型/端点修正正常产生）。
        // 但压平会销毁真低（rawLow 记录的是压平值），若该真低是笔底区域的
        // 绝对极值（如 1h 9-2 11:00 4282.625，下影 93%、实底更高），笔底将虚高
        // 并派生出伪 2买/类2买。故压平前把原低记入 _origLow/_origLowTime，
        // 经 mergeBars 传播，由 fixBiExtremes 恢复为更低的真实笔底端点
        //（只进端点恢复通道，不进 rawLow/rawHigh——跳空检测保持压平语义）。
        b._origLow = b.low;
        b._origLowTime = b.time;
        // 包含判断仍用压平前的真实低点（如 15m 9-30 20:15 低 4182.89
        // 低于 20:00 低 4185.16，两根没有包含，不能因压平并进 20:00）
        b._preLow = b.low;
        b.low = bodyBottom;
      }
    }
    out.push(b);
  }
  return out;
}

// ============================================================
// 1. 包含关系处理（合并K线）
// ============================================================

/**
 * mergeBars 的单步逻辑（与 py_chain.chan_core._mergeStep 镜像）：把 bar 并入
 * merged 尾部（原地修改），返回更新后的 direction。供 mark_entry.js 出场
 * 形成段「合并后≥5根K」计数回放使用（逐根推进时记录每块诞生时间）。
 */
function absorbBody(m, bar) {
  // 合并K记录覆盖范围内的实体极值（顶=max(open,close)，底=min(open,close)）。
  // 近等容差只读这两个字段，不读影线 high/low。
  const o = bar.open, c = bar.close;
  if (o == null || c == null) return;
  const bt = Math.max(o, c), bb = Math.min(o, c);
  if (m.bodyTop == null || bt > m.bodyTop) m.bodyTop = bt;
  if (m.bodyBottom == null || bb < m.bodyBottom) m.bodyBottom = bb;
}

function nearBodyPx(merged, idx, isTop) {
  // 近等容差用的实体价。没有实体字段则返回 null，近等不成立。
  if (!merged || idx == null || idx < 0 || idx >= merged.length) return null;
  const m = merged[idx];
  if (isTop) {
    if (m.bodyTop != null) return m.bodyTop;
    if (m.open != null && m.close != null) return Math.max(m.open, m.close);
    return null;
  }
  if (m.bodyBottom != null) return m.bodyBottom;
  if (m.open != null && m.close != null) return Math.min(m.open, m.close);
  return null;
}

function containHigh(bar) {
  // 包含判断用的高点：长上影压平前的真实高点，未压平则用 high
  return bar._preHigh !== undefined ? bar._preHigh : bar.high;
}

function containLow(bar) {
  // 包含判断用的低点：长下影压平前的真实低点，未压平则用 low
  return bar._preLow !== undefined ? bar._preLow : bar.low;
}

function sideHigh(bar) {
  // 分型邻侧比较用高点（fractalSideRealWick）：含块内被压平的上影真实高点 _wickHigh
  return bar._wickHigh !== undefined ? Math.max(bar.high, bar._wickHigh) : bar.high;
}

function sideLow(bar) {
  // 分型邻侧比较用低点（fractalSideRealWick）：含块内被压平的下影真实低点 _origLow
  return bar._origLow !== undefined ? Math.min(bar.low, bar._origLow) : bar.low;
}

function mergeStep(merged, direction, bar) {
  const pushBar = (b) => {
    const m = {
      ...b, _rawCount: 1, _firstTime: b.time,
      highTime: b.time, lowTime: b.time,
      rawHigh: b.high, rawLow: b.low, rawHighTime: b.time, rawLowTime: b.time,
    };
    if (b._preHigh !== undefined) m._wickHigh = b._preHigh;
    absorbBody(m, b);
    merged.push(m);
  };
  if (merged.length === 0) {
    pushBar(bar);
    return direction;
  }
  const last = merged[merged.length - 1];
  // 包含看压平前的真实高低。合并一旦发生，合成K的高低改用高高/低低的结果，
  // 清掉 _preHigh/_preLow，后续相邻K不再拿影线极值去判包含。
  const lastHigh = containHigh(last);
  const lastLow = containLow(last);
  const barHigh = containHigh(bar);
  const barLow = containLow(bar);
  const containUp = barHigh >= lastHigh && barLow <= lastLow;
  const containDown = barHigh <= lastHigh && barLow >= lastLow;
  const hasContain = containUp || containDown;
  if (hasContain) {
    let dir = direction;
    if (dir === 0 && merged.length >= 2) {
      dir = last.high >= merged[merged.length - 2].high ? 1 : -1;
    }
    if (dir === 0) dir = 1;
    if (dir === 1) {
      if (bar.high > last.high) { last.high = bar.high; last.highTime = bar.time; }
      if (bar.low > last.low) { last.low = bar.low; last.lowTime = bar.time; }
    } else {
      // 向下合并取标准低低（2026-10-01 起）：高点按「压平前真实高点」取较小者。
      // 两侧都未压平时即 orthodox 低低（如 10-1 04:45+06:00 取 4159.77）。
      // 块内含长影压平K（结构高点低于自身真实高点 _preHigh）且新K真实高点更高时，
      // 结构高点不能低于两者的真实较小值——否则真实高点从结构消失、分型判定失真
      // （例：60m 9-21 18:00 上影压平到 4345.73，19:00 真实高点 4371.11 不能被
      // 吃掉，块高点取真实较小值 4356.77，17:00 底分型得以存活）。
      const rl = containHigh(last);
      const rb = containHigh(bar);
      if (rb < rl) {
        if (bar.high < last.high) { last.high = bar.high; last.highTime = bar.time; }
      } else if (rb > rl && last.high < rl) {
        if (rb > last.high) {
          last.high = rl;
          last.highTime = last.rawHighTime !== undefined ? last.rawHighTime : last.time;
        }
      }
      if (bar.low < last.low) { last.low = bar.low; last.lowTime = bar.time; }
    }
    // 记录覆盖原始K线的真实极值范围（跳空检测用，不受合并方向高低取舍影响），
    // 同时记录极值出现的原始K线时间（端点极值修正用，见 fixBiExtremes）
    if (bar.high > last.rawHigh) { last.rawHigh = bar.high; last.rawHighTime = bar.time; }
    if (bar.low < last.rawLow) { last.rawLow = bar.low; last.rawLowTime = bar.time; }
    // 端点候选价（_topCand）随覆盖范围传播：覆盖范围内「可成顶分型中心」的
    // 长影 bar（markWickBars 记 _topCand）的影线价，作为合并 bar 成为顶分型
    // 中心时的端点价（影线可成端点——60m 7-16 02:00 的 4081.52），
    // 同时记录影线价所在原始K线时间（端点时间用——合并 bar 的 highTime 可能
    // 被抬高的普通 bar 占据，需用 _topCandTime 定位真实冲高 bar）
    if (bar._topCand !== undefined && bar._topCand > (last._topCand || 0)) {
      last._topCand = bar._topCand;
      last._topCandTime = bar.time;
    }
    // 探底插针真低（_origLow）随覆盖范围传播：markWickBars 压平长下影时保留的
    // 原低（及所在原始K线时间），供 fixBiExtremes 在笔终点后恢复为更低的真实
    // 端点。只进端点恢复通道，不写入 rawLow/rawHigh——跳空检测与分型结构
    // 保持压平语义（与 _topCand 同模式：结构压平、真值旁路保留）
    if (bar._origLow !== undefined && (last._origLow === undefined || bar._origLow < last._origLow)) {
      last._origLow = bar._origLow;
      last._origLowTime = bar._origLowTime !== undefined ? bar._origLowTime : bar.time;
    }
    // 冲高插针真高（_wickHigh）随覆盖范围传播：markWickBars 压平长上影前的真实高点
    // （_preHigh，块级旁路字段）。fractalSideRealWick 开启时分型邻侧比较用
    // max(high, _wickHigh)——被压平的邻侧上影不再误杀中间分型。只进邻侧比较通道，
    // 不写入 rawHigh/high——合并结构与跳空检测保持压平语义（与 _origLow 同模式）。
    if (bar._preHigh !== undefined && bar._preHigh > (last._wickHigh !== undefined ? last._wickHigh : -Infinity)) {
      last._wickHigh = bar._preHigh;
    }
    last._rawCount += 1;
    last.time = bar.time;
    absorbBody(last, bar);
    delete last._preHigh;
    delete last._preLow;
    return dir;
  }
  direction = bar.high > last.high ? 1 : -1;
  pushBar(bar);
  return direction;
}

/**
 * 包含关系处理（合并K线）
 * 相邻K线有包含关系时合并，方向由前序趋势决定：
 *   向上合并取「高高」，向下合并取「低低」
 * 每根合并K线记录 _rawCount（覆盖的原始K线数），以及 highTime/lowTime（极值原始K线时间）
 */
function mergeBars(rawBars) {
  const merged = [];
  let direction = 0;
  for (const bar of rawBars) {
    direction = mergeStep(merged, direction, bar);
  }
  return merged;
}

// ============================================================
// 2. 分型识别
// ============================================================

/**
 * 分型识别（顶分型/底分型）
 * 顶分型：中间K线最高，且整体高于左右
 * 底分型：中间K线最低，且整体低于左右
 * time 取极值所在的原始K线时间（顶分型用最高价时间，底分型用最低价时间）
 *
 * fractalSideRealWick（默认关）：左右邻的比较价改用压平前真实影线价
 * （sideHigh/sideLow——被 markWickBars 压平的邻侧上/下影不再误杀中间分型，
 * 如 60m 2026-09-29 04:00 底 4111.52 被 06:00 压平上影 4121.69 卡掉高点侧）；
 * 中心K仍用压平结构价，压平语义不变。与 py_chain/chan_core.py fractalAt 同步。
 */
function findFractals(merged) {
  const fractals = [];
  const sideReal = CHAN_CFG.fractalSideRealWick === true;
  for (let i = 1; i < merged.length - 1; i++) {
    const prev = merged[i - 1], cur = merged[i], next = merged[i + 1];
    const ph = sideReal ? sideHigh(prev) : prev.high;
    const pl = sideReal ? sideLow(prev) : prev.low;
    const nh = sideReal ? sideHigh(next) : next.high;
    const nl = sideReal ? sideLow(next) : next.low;
    if (cur.high > ph && cur.high > nh && cur.low > pl && cur.low > nl) {
      // 端点价：覆盖范围内若含「可成顶分型的长影 bar」（markWickBars _topCand，
      // 如 60m 7-16 02:00 bar 的 4081.52），顶分型价用其影线价——结构保持压平版，
      // 影线价只在该 bar 成为分型中心端点时生效（用户要求：7-15 反弹笔顶 = 4081.52）；
      // 端点时间用影线价所在原始K线时间（_topCandTime，缺省回落 cur.highTime）
      const useCand = cur._topCand !== undefined && cur._topCand > cur.high;
      fractals.push({
        mergedIdx: i, type: "top",
        high: useCand ? cur._topCand : cur.high,
        low: cur.low,
        time: useCand && cur._topCandTime !== undefined ? cur._topCandTime : cur.highTime,
      });
    }
    if (cur.low < pl && cur.low < nl && cur.high < ph && cur.high < nh) {
      fractals.push({ mergedIdx: i, type: "bottom", high: cur.high, low: cur.low, time: cur.lowTime });
    }
  }
  return fractals;
}

// ============================================================
// 3. 笔构建辅助函数
// ============================================================

/** 统计 (startIdx, endIdx] 覆盖的原始K线数 */
function countRaw(merged, startIdx, endIdx) {
  let t = 0;
  for (let k = startIdx + 1; k <= endIdx; k++) t += merged[k]._rawCount;
  return t;
}

/**
 * 检测两个分型（合并K线索引区间）之间是否存在跳空缺口。
 * 跳空 = 相邻合并K线之间的价格缺口（向上跳空：后K最低价 > 前K最高价；
 * 向下跳空：后K最高价 < 前K最低价），且缺口幅度 >= gapFilter*ATR。
 */
function hasGapBetween(merged, aIdx, bIdx, atr, gapFilter) {
  const th = atr * gapFilter;
  for (let i = aIdx; i < bIdx; i++) {
    const cur = merged[i], next = merged[i + 1];
    // 用覆盖原始K线的真实极值范围判断跳空，避免合并K线（向下合并压低高点/向上合并抬高低点）
    // 造成「假缺口」：真实原始K线之间若无价格跳空，不应被判为跳空。
    const curHigh = cur.rawHigh !== undefined ? cur.rawHigh : cur.high;
    const curLow = cur.rawLow !== undefined ? cur.rawLow : cur.low;
    const nextHigh = next.rawHigh !== undefined ? next.rawHigh : next.high;
    const nextLow = next.rawLow !== undefined ? next.rawLow : next.low;
    const gapUp = nextLow - curHigh;
    const gapDown = curLow - nextHigh;
    if (gapUp >= th || gapDown >= th) return true;
  }
  return false;
}

// ============================================================
// 4. 笔构建（交替分型序列 + 回溯替换）
// ============================================================

/**
 * 笔的构建：
 *   - 阶段一：构建严格交替的分型序列（连续同类型分型：顶取最高、底取最低）
 *   - 阶段二：遍历序列，处理跳空成笔 / MACD变色成笔 / 前顶前底作废 / 分型范围脱离 / 极值规则
 *   - 阶段三：两两连笔（此时首尾自然连续）
 */
function buildBi(fractals, merged, atr, macdArr, lockedPivots, nearDouble, res = null) {
  const gapThreshold = atr ? atr * CHAN_CFG.gapFilter : 0;
  // 阶段一：严格交替分型序列
  const seq = [];
  for (const f of fractals) {
    if (seq.length === 0) { seq.push(f); continue; }
    const last = seq[seq.length - 1];
    if (f.type === last.type) {
      if (f.type === "top") { if (f.high >= last.high) seq[seq.length - 1] = f; }
      else { if (f.low <= last.low) seq[seq.length - 1] = f; }
    } else {
      seq.push(f);
    }
  }

  // 区间套强制对齐（优先级最高）：上级笔端点（lockedPivots）必须在下级笔中被保留为端点，
  // 不能被阶段二的任何「移除中间分型」逻辑（MACD端点让位/前顶前底作废/回溯替换）吞掉。
  // 在阶段一序列上，把与上级端点「方向一致且价格一致」的分型标记为 locked。
  if (lockedPivots && lockedPivots.length) {
    for (const f of seq) {
      const p = f.type === "top" ? f.high : f.low;
      for (const lp of lockedPivots) {
        if (lp.dir === f.type && Math.abs(lp.price - p) <= 0.001) {
          f.locked = true;
          break;
        }
      }
    }
  }

  // 有效笔判断：合并后K线从起点分型到终点分型（含两端分型）至少 5 根即可成笔。
  // gap = b.mergedIdx - a.mergedIdx，等价于合并K线数 gap+1 >= 5。
  const isValid = (a, b) => {
    const gap = b.mergedIdx - a.mergedIdx;
    return gap >= 4;
  };

  // 笔内极值检查：一笔的顶/底必须是该笔范围内所有K线的最高/最低点。
  //（9-3 16:00 插针已由 markWickBars 压平（其 low 条件不满足），影线价不再出现在
  // merged 中，无需额外免疫；可成端点的长影 bar（_topCand）其端点价=影线价，
  // 作为本区间端点时不在此区间内检查。）
  const noMoreExtremeInside = (a, b) => {
    for (let i = a.mergedIdx + 1; i < b.mergedIdx; i++) {
      if (b.type === "bottom" && merged[i].low < b.low) return false;
      if (b.type === "top" && merged[i].high > b.high) return false;
    }
    return true;
  };

  // MACD 让位检查整笔双向极值，含中间分型影线价；等价允许。
  const replacementExtremesClear = (origin, old, middle, end) => {
    const ceiling = origin.type === "top" ? origin.high : end.high;
    const floor = end.type === "bottom" ? end.low : origin.low;
    for (const x of [old, middle]) {
      if (x.high > ceiling || x.low < floor) return false;
    }
    for (let i = origin.mergedIdx + 1; i < end.mergedIdx; i++) {
      if (merged[i].high > ceiling || merged[i].low < floor) return false;
    }
    return true;
  };

  // 分型范围脱离检查（双向）：一笔的两端分型不能互相"包含"。
  // 起点侧（对称两根，排除段外的反向结构 bar）：
  //   下跌笔（顶@a → 底@b）用顶分型 [中心, 右 bar] 的最低——不用左 bar，否则主升前夜/
  //   起涨点的旧低点会错误抬高"必须跌破"的阈值，误杀后续健康反弹。
  //   例：60m 7-14 20:00 顶 4104.05 的顶分型左 bar（19:00 主升前夜 bar）低点 4015.485
  //   只比 7-15 16:00 的真实底 4017.475 低 2 点，旧规则（三根最低）使该底被拒、
  //   60 点反弹（→7-15 21:00 顶 4074.175）整段消失；对称化后阈值 =
  //   min(中心 4071.00, 右 4064.725) → 底通过 → 反弹成笔。
  //   上涨笔（底@a → 顶@b）对称用 [左 bar, 中心] 的最高。
  // 终点侧（三根，防反向吞没）：
  //   下跌笔的底分型三根K线最高价不得涨回起点顶价之上；上涨笔的顶分型三根K线最低价
  //   不得跌破起点底价（顶后崩盘 bar 跌回起点之下 = 中继弱反弹，不成笔；
  //   中心 bar 的崩盘低点可能被包含合并抬高，须依赖三根中的右 bar 提供证据）。
  //   例：240 笔16 顶分型中心 8-28 21:00 bar（H4631.98 开盘1小时内冲高，bar 内暴跌收 4479），
  //   三根最低 4445.455（8-29 01:00 bar）< 起点底 4564.27 → 该上涨笔被拒；
  //   60m 8-28 22:00 bar 同型（H4631.98→L4530.02，崩盘低点被 up 合并抬到 4596.26）
  //   → 右 bar（23:00 L4524.125）< 起点 4571.66 → 同样被拒。
  //   对比 7-15 反弹顶 4074.175：三根最低 4035.955 > 起点底 4017.475 → 健康反弹通过。
  const fractalRangeClear = (a, b) => {
    const i = a.mergedIdx;
    const j = b.mergedIdx;
    // 起点侧：与段同侧的两根（中心 + 终点方向邻 bar）
    const rangeLow = a.type === "top"
      ? Math.min(merged[i].low, merged[i + 1].low)
      : Math.min(merged[i - 1].low, merged[i].low);
    const rangeHigh = a.type === "top"
      ? Math.max(merged[i].high, merged[i + 1].high)
      : Math.max(merged[i - 1].high, merged[i].high);
    // 终点侧三根。本周期振幅 ≥ 单根长K豁免点数的K线不参与（大振幅K不作为反向贯穿证据）。
    // 三根都被豁免时，终点侧不构成反向贯穿。证据为实体极值（bodyTop/bodyBottom，
    // 缺失回退 open/close 再回退影线）：影线刺穿起点极值不算（2026-10-01 起，如
    // 10-1 09:00 长阳高 4161.385 刺穿 04:30 顶 4160.41 但实体顶 4159.67 未越过）。
    const thr = wideBarPointsOf(res);
    const skipWide = thr > 0;
    let endLow = null, endHigh = null;
    for (const idx of [j - 1, j, j + 1]) {
      const m = merged[idx];
      if (skipWide && (m.high - m.low) >= thr) continue;
      const bt = m.bodyTop != null ? m.bodyTop
        : (m.open != null && m.close != null ? Math.max(m.open, m.close) : m.high);
      const bb = m.bodyBottom != null ? m.bodyBottom
        : (m.open != null && m.close != null ? Math.min(m.open, m.close) : m.low);
      endLow = endLow === null ? bb : Math.min(endLow, bb);
      endHigh = endHigh === null ? bt : Math.max(endHigh, bt);
    }
    if (a.type === "top" && b.type === "bottom") {
      const endOk = endHigh === null || endHigh < a.high;
      return b.low < rangeLow && endOk;
    }
    if (a.type === "bottom" && b.type === "top") {
      const endOk = endLow === null || endLow > a.low;
      return b.high > rangeHigh && endOk;
    }
    return true;
  };

  // 相邻分型是否已成笔（与阶段二追加成笔同口径）。同类型不成笔。
  const pairFormsBi = (a, b) => {
    // 未确认末根不是分型，不能当作已成笔的一端（右邻合并K尚不存在）。
    if (a._openEndpoint || b._openEndpoint) return false;
    if (a.type === b.type) return false;
    const gap = b.mergedIdx - a.mergedIdx;
    if (gap >= 4 && noMoreExtremeInside(a, b) && fractalRangeClear(a, b)) return true;
    if (gap === 3 && noMoreExtremeInside(a, b) && macdArr && macdArr.length) {
      const direction = a.type === "bottom" ? "up" : "down";
      return hasMacdCrossBetween(macdArr, merged, a.mergedIdx, b.mergedIdx, a.time, b.time, direction);
    }
    return false;
  };

  // 近等后移腿终局守卫（2026-09-30）：近等后移断言「last→k 这条腿终结于 k、中间反弹
  // 不成笔」。该断言被后续数据否定时不应提交：若 k 被更极端同类型分型突破，且被弹出
  // 的反弹段（本段结构极值 anchor→区间实际最优反向极值 best→突破点 k2）本可构成两笔
  // 有效笔，则拒绝合并——prev/last 留在序列里，正确结构由标准成笔规则自然长出（例：
  // 15m 2026-09-28 17:00底4140.775→20:15顶4171.42 上涨笔；60m 2026-08-31 10:00底→
  // 9-1 08:00顶→9-2 11:00底）。判据与 shiftBreakRestore 的补回条件同源（可证成笔才拦），
  // 已被市场走势"消化"的历史合并不受影响。k.locked（区间套落地）豁免。
  const nearEqualShiftFalsified = (origin, prev, k) => {
    const startIdx = origin ? origin.mergedIdx : prev.mergedIdx;
    // anchor：本段腿内的结构极值（窗口内最低底/最高顶，比 prev 更本质）
    let anchor = prev;
    for (const f of fractals) {
      if (f.mergedIdx <= startIdx || f.mergedIdx >= k.mergedIdx || f.type !== k.type) continue;
      if (k.type === "bottom" ? f.low < anchor.low : f.high > anchor.high) anchor = f;
    }
    let tries = 0;
    for (const k2 of fractals) {
      if (k2.mergedIdx <= k.mergedIdx || k2.type !== k.type) continue;
      if (!(k.type === "bottom" ? k2.low < k.low : k2.high > k.high)) continue;
      if (++tries > 8) break; // 突破点只看近处，防长程扫描
      let best = null;
      for (const f of fractals) {
        if (f.mergedIdx <= anchor.mergedIdx || f.mergedIdx >= k2.mergedIdx || f.type === k.type) continue;
        if (!best || (f.type === "top" ? f.high > best.high : f.low < best.low)) best = f;
      }
      if (best && pairFormsBi(anchor, best) && pairFormsBi(best, k2)) {
        if (CHAN_CFG.debug) console.log(`[阶段二] 腿终局守卫拦截: ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 被 ${k2.type === "top" ? "顶" : "底"}@${k2.mergedIdx} 突破且 ${anchor.mergedIdx}→${best.mergedIdx}→${k2.mergedIdx} 可成两笔，反弹是真实笔，不取后`);
        return true;
      }
    }
    return false;
  };

  // 同类型近等取后。先影线后实体（2026-10-02）：影线更极端的直接后移在同类型分支
  // （更极端替换）已做，这里处理影线不满足后的实体比较——后实体（bodyTop/bodyBottom）
  // 不低于前实体直接后移，更低则差 ≤ nearDoubleFixed 才后移；中间真实回调仍用高低点。
  // k 可以是已确认分型，也可以是未等右邻收盘的末根合并K。成功则打 nearDouble 并返回 true。
  const tryNearEqualSameType = (last, k) => {
    if (!nearDouble || !last || k.type !== last.type) return false;
    if (last.gapLocked || k.locked || last.nearDouble) return false;
    if (k.mergedIdx === last.mergedIdx) return false;
    const isTop = k.type === "top";
    // 先影线后实体：后影线已更极端（>= / <=）时由同类型分支的更极端替换处理，
    // 不走实体近等——避免给已被影线替换的端点补 nearDouble 单跳封顶，
    // 挡住后续真正的近等后移（例：8-7 00:00 底 4223.505 影线更极端已替换，
    // 若再标 nearDouble 会挡住 08:00 后底 4229.875 的近等后移）。
    if (isTop ? k.high >= last.high : k.low <= last.low) return false;
    const refPrice = nearBodyPx(merged, last.mergedIdx, isTop);
    const newPrice = nearBodyPx(merged, k.mergedIdx, isTop);
    if (refPrice == null || newPrice == null) return false;
    const thr = CHAN_CFG.nearDoubleFixed;
    const diff = isTop ? refPrice - newPrice : newPrice - refPrice;
    if (diff > thr) return false;
    let pull = false, cnt = 0;
    const chain = [last];
    for (const f of fractals) {
      if (f.mergedIdx <= last.mergedIdx || f.mergedIdx >= k.mergedIdx) continue;
      cnt++;
      if (isTop && f.type === "bottom" && last.high - f.low >= thr) pull = true;
      if (!isTop && f.type === "top" && f.high - last.low >= thr) pull = true;
      chain.push(f);
    }
    chain.push(k);
    let joined = false;
    if (cnt > 0) {
      for (let i = 0; i < chain.length - 2; i++) {
        if (pairFormsBi(chain[i], chain[i + 1]) && pairFormsBi(chain[i + 1], chain[i + 2])) {
          joined = true;
          break;
        }
      }
    }
    if (!(cnt > 0 && !joined && pull)) return false;
    if (CHAN_CFG.debug) console.log(`[阶段二] 近等双顶/双底平台取后: ${k.type === "top" ? "顶" : "底"}@${last.mergedIdx}(${refPrice}) → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low})（实体差 ${diff.toFixed(2)} ≤ ${thr.toFixed(2)}，两顶间无相接成笔）`);
    k.nearDouble = true;
    // 取后可证伪回退锚（2026-10-01）：后移端点使后续反向分型间隔不足连不上、
    // 而原端点可与其成笔时回退（见间隔不足分支的平台取后回退）。
    k._platAnchor = last;
    return true;
  };

  if (CHAN_CFG.debug) {
    const ft = (s) => `${s.type === "top" ? "顶" : "底"}@${s.mergedIdx}(${s.type === "top" ? s.high : s.low})${s.locked ? "(锁定)" : ""}`;
    console.log("[阶段一] 交替分型序列:", seq.map(ft).join(" → "));
  }

  // 近等后移的端点被更极端的同类型分型破坏，且被弹出的转折与新端点已能成笔时，补回被吞掉的笔。
  // 只改端点序列，不回头重开已错过的单。能补回返回 true。
  const shiftBreakRestore = (last, k) => {
    const mid = last._shiftMid;
    const anchor = last._shiftAnchor;
    if (!mid || !anchor || k.type !== last.type) return false;
    // 只修被破坏的后底。后顶被更高顶打断仍沿用原替换。
    if (k.type !== "bottom" || k.low >= last.low) return false;
    // 被弹出的那段当时已在笔栈里，不再用分型范围重审。新的一笔必须完整成笔。
    if (!(isValid(anchor, mid) && noMoreExtremeInside(anchor, mid))) return false;
    if (!(isValid(mid, k) && noMoreExtremeInside(mid, k) && fractalRangeClear(mid, k))) return false;
    if (CHAN_CFG.debug) {
      const kind = k.type === "bottom" ? "底" : "顶";
      const midKind = mid.type === "top" ? "顶" : "底";
      console.log(`[阶段二] 近等端点被破坏，补回笔: ${kind}@${anchor.mergedIdx} → ${midKind}@${mid.mergedIdx} → ${kind}@${k.mergedIdx}`);
    }
    result.pop();
    result.push(anchor, mid, k);
    return true;
  };

  // 阶段二：移除间隔不足的中间分型（回溯替换）
  const result = [];
  for (const k of seq) {
    if (result.length === 0) { result.push(k); continue; }
    const last = result[result.length - 1];
    if (k.type === last.type) {
      if (last.locked) {
        // locked 端点（上级笔端点，区间套强制对齐）不可被同类型分型替换——
        // 唯一例外（nearDoubleShiftLocked，2026-10-02）：近等双顶/双底平台取后
        // （闸门照常：影线不更极端才比实体、差≤nearDoubleFixed、回调够深、无相接
        // 成笔、单跳封顶）。例：60m 9-28 22:00 锁定底 4110.87 让位 9-29 04:00
        // 近等后底 4111.52（15m 二次背驰转折）。更极端替换等其余锁定拦截不变。
        // 与 py_chain/chan_core.py biStep 同步。
        if (CHAN_CFG.nearDoubleShiftLocked === true) {
          if (tryNearEqualSameType(last, k)) result[result.length - 1] = k;
        }
        continue;
      }
      if (!last.gapLocked) {
        const more = k.type === "top" ? k.high >= last.high : k.low <= last.low;
        // 近等后移的端点被更极端同类型分型破坏：能与被弹出的转折成笔就补回，
        // 否则端点后移并带走修正线索，等后续真正成笔的底/顶再补。
        if (more && last._shiftMid) {
          if (shiftBreakRestore(last, k)) continue;
          k._shiftAnchor = last._shiftAnchor;
          k._shiftMid = last._shiftMid;
        }
        if (more) result[result.length - 1] = k;
      } else {
        // 跳空锁定的端点：仅当后续同类型分型「突破」锁定价格时才解锁替换
        if (k.type === "top") { if (k.high > last.high) result[result.length - 1] = k; }
        else { if (k.low < last.low) result[result.length - 1] = k; }
      }
      // 近等双顶/双底：价差只比实体；锚点用替换前的 last
      // （更极端的影线替换已先改 result，近等仍相对原端点判断）。
      if (tryNearEqualSameType(last, k)) result[result.length - 1] = k;
      continue;
    }
    // 异类型
    // MACD 变色成笔端点让位：若 result[-2] 是 MACD 变色成笔的端点，且当前分型 k 是
    // 更极端的同类型分型，让位更新该端点并移除中间分型，保证 MACD 变色成笔的终点是
    // 区间内最新的绝对极值（例：92.83 应让位给更低的 92.74）。
    if (result.length >= 3) {
      const origin = result[result.length - 3];
      const prev2 = result[result.length - 2];
      const topOne = result[result.length - 1];
      if (prev2.macdCross === true && prev2.type === k.type &&
          !topOne.locked && !prev2.locked &&
          ((k.type === "top" && k.high > prev2.high) || (k.type === "bottom" && k.low < prev2.low)) &&
          replacementExtremesClear(origin, prev2, topOne, k)) {
        if (CHAN_CFG.debug) console.log(`[阶段二] MACD端点让位: ${prev2.type === "top" ? "顶" : "底"}@${prev2.mergedIdx}(${prev2.type === "top" ? prev2.high : prev2.low}) → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 更新为更极端${k.type === "top" ? "高点" : "低点"}，移除中间分型`);
        k.macdCross = true;
        result[result.length - 2] = k;
        result.pop();
        continue;
      }
    }
    // 跳空优先：若 last→k 之间存在幅度 >= gapFilter*ATR 的跳空缺口，则强制独立成笔
    const hasGap = gapThreshold > 0 && hasGapBetween(merged, last.mergedIdx, k.mergedIdx, atr, CHAN_CFG.gapFilter);
    if (hasGap) {
      if (CHAN_CFG.debug) console.log(`[阶段二] 跳空成笔: ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx} → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx} (缺口≥${gapThreshold.toFixed(2)})`);
      k.gapLocked = true;
      result.push(k);
      continue;
    }
    // 前顶/前底作废：prev2（result[-2]）与 k 同类型，且 prev2→last 不构成有效笔
    // （间隔不足，prev2 是脆弱端点），而 k 比 prev2 更极端（创新高/新低）时，
    // prev2 作为笔端点已被市场否定，应让 k 顶替 prev2 并移除中间的 last。
    // 附加约束（防止误伤真实顶/底）：
    //   1) last 必须不是极端点：last 不得比 prev3 更极端（否则 last 是深回调的真实转折）；
    //   2) 回调/反弹必须浅：pull/bounce < 上涨/下跌幅度的 50%；
    //   3) last 必须是「弱分型」：MACD 变色成笔且原始K线 < 5 根。
    if (result.length >= 3) {
      const prev3 = result[result.length - 3];
      const prev2 = result[result.length - 2];
      const lastMoreExtremeThanPrev3 =
        (prev3.type === "top" && last.high > prev3.high) ||
        (prev3.type === "bottom" && last.low < prev3.low);
      let shallow = true;
      if (prev2.type === "top") {
        const rise = prev2.high - prev3.low;
        const pull = prev2.high - last.low;
        shallow = pull < rise * 0.5;
      } else {
        const drop = prev3.high - prev2.low;
        const bounce = last.high - prev2.low;
        shallow = bounce < drop * 0.5;
      }
      if (prev2.type === k.type &&
          !isValid(prev2, last) &&
          !lastMoreExtremeThanPrev3 &&
          shallow &&
          last.macdCross === true && last.macdRaw < 5 &&
          !last.locked && !prev2.locked &&
          ((k.type === "top" && k.high > prev2.high) || (k.type === "bottom" && k.low < prev2.low))) {
        if (CHAN_CFG.debug) console.log(`[阶段二] 前顶/前底作废: ${prev2.type === "top" ? "顶" : "底"}@${prev2.mergedIdx}(${prev2.type === "top" ? prev2.high : prev2.low}) 被 ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 突破，k 顶替 prev2，移除中间 ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}`);
        if (prev2.macdCross === true) k.macdCross = true;
        result[result.length - 2] = k;
        result.pop();
        continue;
      }
    }
    if (isValid(last, k) && (noMoreExtremeInside(last, k) || last.gapLocked) && (fractalRangeClear(last, k) || last.gapLocked)) {
      result.push(k);
    } else if (isValid(last, k)) {
      // 间隔足够但区间内存在更极端的点 或 分型范围未脱离：k 不能作为笔端点，
      // 等待后续更极端的分型或并入更大的笔
      // 前顶/前底作废（区间极值版，2026-10-09，与 py_chain/chan_core.py biStep 同步）：
      // k 间隔足够但未通过成笔判据时，若 k 与 prev（result[-2]）同类型且严格更极端，
      // 且 prev3（result[-3]）与 k 能构成完整有效笔，则 prev 被作废、吞掉中间 last，
      // 端点推进到 k——否则更高点将被永久吞进 prev 起步的反向笔，笔起点不再是区间
      // 极值（例：15m 2026-07-20 顶 16:15 4030.88 被 19:30 4040.82 突破，17:30→19:30
      // 反弹腿因终点侧反向贯穿不成笔，作废后上涨笔延伸为 15:00 4002.18 → 19:30 4040.82）。
      // 回溯替换保护（与间隔不足分支同源）：last 比 prev3 更极端时不吞。
      if (result.length >= 3 && result[result.length - 2].type === k.type &&
          !result[result.length - 2].locked && !last.locked) {
        const prev = result[result.length - 2];
        const prev3 = result[result.length - 3];
        const lastDeeper = (k.type === "top" && last.low < prev3.low) ||
                           (k.type === "bottom" && last.high > prev3.high);
        if (!lastDeeper &&
            ((k.type === "top" && k.high > prev.high) || (k.type === "bottom" && k.low < prev.low)) &&
            pairFormsBi(prev3, k)) {
          if (CHAN_CFG.debug) {
            console.log(`[阶段二] 前顶/前底作废(区间极值): ${prev.type === "top" ? "顶" : "底"}@${prev.mergedIdx}(${prev.type === "top" ? prev.high : prev.low}) 被 ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 突破，k 顶替 prev，移除中间 ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}`);
          }
          if (prev.macdCross === true) k.macdCross = true;
          result[result.length - 2] = k;
          result.pop();
          continue;
        }
      }
      if (CHAN_CFG.debug) {
        const fr = fractalRangeClear(last, k);
        const ex = noMoreExtremeInside(last, k);
        console.log(`[阶段二] 忽略 k: ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 极值冲突=${!ex} 分型范围未脱离=${!fr}`);
      }
    } else {
      // 间隔不足：先检查 last→k 是否满足「合并后只有4根K + 方向性 MACD 变色」成笔。
      // 方向性变色：底到顶(上涨) 柱状体由绿变红；顶到底(下跌) 柱状体由红变绿。
      const gap = k.mergedIdx - last.mergedIdx;
      const direction = last.type === "bottom" ? "up" : "down";
      const macdCross = macdArr && hasMacdCrossBetween(macdArr, merged, last.mergedIdx, k.mergedIdx, last.time, k.time, direction);
      const macdRawCount = countRaw(merged, last.mergedIdx, k.mergedIdx);
      if (gap === 3 && macdCross && noMoreExtremeInside(last, k)) {
        if (CHAN_CFG.debug) console.log(`[阶段二] MACD变色成笔: ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx} → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx} (合并4根K, ${direction === "up" ? "绿变红" : "红变绿"})`);
        k.macdCross = true;
        k.macdRaw = macdRawCount;
        result.push(k);
      } else {
        // 平台取后可证伪回退（2026-10-01，与 shiftBreakRestore 同哲学：断言让位于
        // 可证结构，优先于 moreExtreme 顶替/近等后移执行）：近等平台取后把端点后移到
        // last，若随后反向分型 k 与 last 间隔不足连不上、而原端点 _platAnchor 与 k
        // 能成笔（含间隔/笔内极值/范围脱离全套判据），说明「两底/两顶近等取哪个
        // 无所谓」的断言被否定——回退原端点并接入 k。例：15m 10-1 02:00 底近等
        // 后移到 03:0 后，04:30 顶只剩 4 根合并K，02:00→04:30 有 7 根可成笔。
        if (last._platAnchor && !last.locked && pairFormsBi(last._platAnchor, k)) {
          // 起点侧极值守卫（双向）：回退生成的笔，其起点也必须是区间极值——
          // 起点之后、k 之前藏着比起点更极端的同向极值（如 60m 9-18 04:00 底
          // 4340.655 上方有 07:00 低点 4339.72）说明原端点不是该段真实转折，
          // 不回退，维持取后，等待更极端分型按标准路径替换。
          let startClear = true;
          for (let si = last._platAnchor.mergedIdx + 1; si < k.mergedIdx; si++) {
            const sm = merged[si];
            if ((last._platAnchor.type === "bottom" && sm.low < last._platAnchor.low) ||
                (last._platAnchor.type === "top" && sm.high > last._platAnchor.high)) {
              startClear = false;
              break;
            }
          }
          if (startClear) {
            if (CHAN_CFG.debug) console.log(`[阶段二] 平台取后回退: ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx} → 原${last._platAnchor.type === "top" ? "顶" : "底"}@${last._platAnchor.mergedIdx}，接入 ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}`);
            result[result.length - 1] = last._platAnchor;
            result.push(k);
            continue;
          }
        }
        // 间隔不足且无 MACD 变色：中间分型 last 作废，k 回溯与 result[-2]（同类型）比较
        if (result.length >= 2 && result[result.length - 2].type === k.type) {
          const prev = result[result.length - 2];
          const moreExtreme = k.type === "top" ? k.high >= prev.high : k.low <= prev.low;
          const gapPrevLast = last.mergedIdx - prev.mergedIdx;
          if (CHAN_CFG.debug) console.log(`[阶段二] 间隔不足: ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx} 与 ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}, 回溯比较同类型 prev, moreExtreme=${moreExtreme}, gapPrevLast=${gapPrevLast}`);
          // 前顶/前底作废原则（缠论）：顶被更高顶突破时，作废前顶的条件是「前顶右侧是否已有足够K线构成笔」。
          //   prev→last 构成有效笔（间隔>=4 且 笔内无更极值 且 分型范围脱离）→ 前顶有效，保留，
          //     不能被更高顶作废（如 8-20 04:00顶→20:00底 间隔 11 根合并K线，已成有效下跌笔，
          //     23:00 的更高顶 4541.045 无法与右侧成笔，应作废的是新顶而非前顶）；
          //   仅当 prev→last 不构成有效笔（前顶右侧不足以成笔）时，更高顶 k 才能顶替 prev。
          //   例外：k.locked（上级笔端点）且更极端、与 last 间隔不足无法自成笔时，仍顶替 prev 落地
          //   （区间套锁定端点阶段二不可吞）。prev.locked / last.locked 仍不让位。
          const prevLastValidBi = gapPrevLast >= 4 && noMoreExtremeInside(prev, last) && fractalRangeClear(prev, last);
          // 最小间隔脆弱笔例外：prev→last 虽构成有效笔，但间隔恰为最小值（gapPrevLast === 4，
          // 即刚够 5 根合并K线）且回调/反弹浅（< 前段涨跌幅的 50%）时，该笔尚未被确认——
          // 随后 k 即创更高顶/更低底说明整段仍是同一笔的延伸（缠论：顶被更高顶突破即作废，
          // 上涨笔延伸到新极值），prev 应被 k 顶替。
          //   例：15m 9-3 顶 4496.01(21:03)→底 4466.02 间隔恰 4、回调 39%（浅），
          //   23:15 新高 4510.93 顶替前顶，上涨笔延伸至 4510.93（与 60m/3m 端点一致）。
          //   8-20 04:00 顶→20:00 底（间隔 11 根）等坚实笔不受影响；深回调（≥50%）时
          //   last 是真实转折点，同样不受影响（如 8-14 早盘 底4327.27→顶4347.42 反弹 121%）。
          let fragileMinimal = false;
          if (prevLastValidBi && gapPrevLast === 4 && result.length >= 3) {
            const p3 = result[result.length - 3];
            if (prev.type === "top") {
              const rise = prev.high - p3.low;
              fragileMinimal = rise > 0 && (prev.high - last.low) < rise * 0.5;
            } else {
              const drop = p3.high - prev.low;
              fragileMinimal = drop > 0 && (last.high - prev.low) < drop * 0.5;
            }
            if (CHAN_CFG.debug && fragileMinimal) console.log(`[阶段二] 最小间隔脆弱笔: ${prev.type === "top" ? "顶" : "底"}@${prev.mergedIdx}(${prev.type === "top" ? prev.high : prev.low})→${last.type === "top" ? "顶" : "底"}@${last.mergedIdx} 间隔恰4且回调浅，允许被 ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 顶替`);
          }
          // 近等后顶/后底（反弹不成笔）取后（2026-09-26）：本分支前提即 last→k 反弹/回撤腿
          // gap<4 本身不成笔（拆不出独立反弹笔）。若 k 与 prev 近等（先影线后实体：影线更极端
          // 已由更极端替换处理，这里比实体——后实体不低于前实体直接取后，更低则差 ≤
          // thr=nearDoubleFixed）且 prev→last 是 ≥thr 的真实回调，则 prev 让位、
          // 端点后移到 k——「回调够深、反弹太短」的走势终完美（例：60m 2026-09-18 15:00 顶
          // 4399.67 → 底 4342.73(22:00+8，与23:00包含合并) → 9-19 01:00 顶 4397.05，反弹腿
          // 仅 3 根合并K；与平台取后顶（同类型分支）互补，平台场景两顶间分型间隔全 <4）。
          // 权限：k.locked（上级笔端点）任何周期生效——区间套强制落地，上级已后移的端点
          // 必须在本级复现（240 顶 4397.05@9-19 01:00 已由平台规则后移、60m 顶 4399.67 未跟
          // 的跨级错位即靠此修复）；否则仅 nearDouble（nearDoubleOn(res)，60/240/D）。
          // 排除：prev.gapLocked（跳空锁定只被严格突破替换）、prev.locked（锁定前顶不让位）、
          // last.locked（不吞锁定的中间分型）、prev.nearDouble（单跳封顶，防平台内连续后移漂移）。
          let nearEqualShift = false;
          if (!prev.gapLocked && !prev.locked && !last.locked && !prev.nearDouble) {
            const isTopR = k.type === "top";
            const refPriceR = nearBodyPx(merged, prev.mergedIdx, isTopR);
            const newPriceR = nearBodyPx(merged, k.mergedIdx, isTopR);
            if (refPriceR != null && newPriceR != null) {
              const thrR = CHAN_CFG.nearDoubleFixed;
              const diffR = isTopR ? refPriceR - newPriceR : newPriceR - refPriceR;
              const pulledR = isTopR ? prev.high - last.low >= thrR
                : last.high - prev.low >= thrR;
              if (diffR <= thrR && !moreExtreme && pulledR
                  && (k.locked || !nearEqualShiftFalsified(result.length >= 3 ? result[result.length - 3] : null, prev, k))
                  && (k.locked || (nearDouble && CHAN_CFG.nearDoubleRebound))) {
                nearEqualShift = true;
              }
            }
          }
          if ((moreExtreme && (!prevLastValidBi || fragileMinimal || k.locked)) || nearEqualShift) {
            // 回溯替换保护（区间套一致性）：当 last 比更早的同类型分型 result[-3] 更极端时，
            // last 是笔内真实转折点（如插针低点/插针高点），不能无条件 pop 掉——吞掉会导致
            // 该笔内部藏着更极值（违反笔内极值原则），且本级别笔端点与上级周期（区间套）不重合。
            // 此时保留 last 取代 result[-3]，prev 被更高顶/更低底突破而作废移除，
            // k 与 last 间隔不足、暂不接入，等待后续满足最小间隔的分型成笔。
            // k.locked 不走本保护：上级锁定端点必须落地，不能被保护丢掉。
            if (result.length >= 3) {
              const prev3 = result[result.length - 3];
              const lastIsDeeper =
                (k.type === "top") ? (last.low < prev3.low) : (last.high > prev3.high);
              if (lastIsDeeper && !prev.locked && !prev3.locked && !k.locked) {
                if (CHAN_CFG.debug) console.log(`[阶段二] 回溯替换保护: ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}(${last.type === "top" ? last.high : last.low}) 比 ${prev3.type === "top" ? "顶" : "底"}@${prev3.mergedIdx}(${prev3.type === "top" ? prev3.high : prev3.low}) 更极端，保留 last 为端点，作废 ${prev.type === "top" ? "顶" : "底"}@${prev.mergedIdx}(${prev.type === "top" ? prev.high : prev.low})，暂不接入 ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}`);
                result[result.length - 3] = last;
                result.pop();
                result.pop();
                continue;
              }
            }
            if (!last.locked && !prev.locked) {
              if (k.locked && moreExtreme && prevLastValidBi && CHAN_CFG.debug) {
                console.log(`[阶段二] 锁定端点落地: ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low}) 顶替已成笔的 ${prev.type === "top" ? "顶" : "底"}@${prev.mergedIdx}，去掉 ${last.type === "top" ? "顶" : "底"}@${last.mergedIdx}`);
              }
              if (nearEqualShift) {
                k.nearDouble = true; // 单跳封顶：被近等后移的端点不允许二次后移
                // 记下被替换的端点和被弹出的转折，供后续分型破坏时补回笔
                k._shiftAnchor = prev;
                k._shiftMid = last;
                if (CHAN_CFG.debug) console.log(`[阶段二] 近等后顶/后底(反弹不成笔)取后: ${k.type === "top" ? "顶" : "底"}@${prev.mergedIdx}(${k.type === "top" ? prev.high : prev.low}) → ${k.type === "top" ? "顶" : "底"}@${k.mergedIdx}(${k.type === "top" ? k.high : k.low})${k.locked ? "（k为上级锁定端点，区间套落地）" : ""}，prev→last 真实回调、last→k 反弹不成笔`);
              }
              result[result.length - 2] = k;
              result.pop();
            }
          }
        }
      }
    }
  }

  // 末根合并K不等右邻收盘成顶/底分型，即可参与同类型近等取后。
  // 只走近等后移，不走更极端替换、也不新成笔。
  if (nearDouble && result.length > 0 && merged.length > 0) {
    const lastOpen = result[result.length - 1];
    const openIdx = merged.length - 1;
    if (openIdx > lastOpen.mergedIdx) {
      const bar = merged[openIdx];
      const isTopOpen = lastOpen.type === "top";
      const openK = {
        mergedIdx: openIdx,
        type: lastOpen.type,
        high: bar.high,
        low: bar.low,
        time: (isTopOpen ? bar.highTime : bar.lowTime) || bar.time,
        _openEndpoint: true,
      };
      if (tryNearEqualSameType(lastOpen, openK)) result[result.length - 1] = openK;
    }
  }

  if (CHAN_CFG.debug) {
    const ft = (s) => `${s.type === "top" ? "顶" : "底"}@${s.mergedIdx}(${s.type === "top" ? s.high : s.low})`;
    console.log("[阶段二] 结果序列:", result.map(ft).join(" → "));
  }

  // 阶段三：两两连笔
  const bis = [];
  for (let i = 0; i + 1 < result.length; i++) {
    const a = result[i], b = result[i + 1];
    const startPrice = a.type === "top" ? a.high : a.low;
    const endPrice = b.type === "top" ? b.high : b.low;
    const isUp = b.type === "top";
    bis.push({
      type: isUp ? "up" : "down",
      startIdx: a.mergedIdx,
      endIdx: b.mergedIdx,
      startTime: a.time,
      endTime: b.time,
      startPrice,
      endPrice,
      rawCount: countRaw(merged, a.mergedIdx, b.mergedIdx),
      span: Math.abs(endPrice - startPrice),
      gapLocked: b.gapLocked === true,
      macdCross: b.macdCross === true,
    });
  }
  return bis;
}

/**
 * 端点极值修正（方向A）：
 * 包含关系合并时（如向上合并取「高高」会把更低的插针低点抬高，向下合并取「低低」会把更高的插针高点压低），
 * 笔的端点分型可能不是该区域内的真实极值，导致笔终点落在次极值上（例：60分钟 7-29 09:00 真实低点 4010.41
 * 被 08:00/09:00 的包含合并吞掉，下跌笔终点停在 7-28 22:00 的 4011.765）。
 * 本函数对每笔检查「终点分型之后、下一笔终点分型之前」的合并K线，若存在「被包含合并掩盖」
 * （rawLow<low / rawHigh>high）且比当前端点更极端的真实极值，把本笔终点与下一笔起点同步平移到该极值
 * 所在K线（保持首尾连续）。只处理被掩盖的极值——未掩盖的极值若重要，会正常形成分型、由 buildBi 处理。
 * 下影方向额外恢复 markWickBars 压平的长下影真低（_origLow：压平发生在合并前，rawLow 已是
 * 压平值；若该真低是笔底区域绝对极值，不恢复则笔底虚高——如 1h 9-2 11:00 4282.625）。
 * 跳空独立成笔（gapLocked）端点固定在缺口处，不参与修正。
 *
 * @param {Array} bis    buildBi 产出的笔数组（原地修改并返回）
 * @param {Array} merged mergeBars 产出的合并K线数组（需含 rawLow/rawHigh/rawLowTime/rawHighTime）
 */
function fixBiExtremes(bis, merged) {
  if (!bis || bis.length === 0 || !merged || merged.length === 0) return bis;
  const eps = 1e-9;
  for (let i = 0; i < bis.length; i++) {
    const b = bis[i];
    if (b.gapLocked) continue; // 跳空成笔端点固定在缺口处
    const next = bis[i + 1];
    if (!next) continue; // 最后一笔由 extendLastBi 负责延伸
    // 不含下一笔终点分型（避免笔退化）；顶/底分支各自决定是否扫本笔端点分型中心：
    const toIdx = next.endIdx - 1;
    let extreme = null;
    // 内部块后向扫描（endInnerRecoverOn，2026-10-09，与 py_chain/chan_core.py 同步）：
    // 被包含合并吞掉的更极端真低/真高也可能藏在终点分型**之前**的笔内部块（例：60m
    // 2026-10-07 19:00 被 20:00 插针K线包含、升序合并取高低后 4066.53 从结构消失，
    // 底分型落在 21:00 4082.42）——笔端点应为笔区间真实极值。内部块
    // （startIdx+1..endIdx-1）的原始K线必然属于本笔区间（早于本笔的老蜡烛只可能被吞进
    // 分型中心块链内，见中心注释），可信任 rawLow/_origLow。
    const innerOn = CHAN_CFG.endInnerRecoverOn !== false;
    if (b.type === "down") {
      // 终点是底：从 b.endIdx 起扫（含分型中心）——中心合并K线可能因包含合并/长下影
      // 压平把更低的真低藏在自身 low 之下（如 1h 9-2 10:00 底分型中心吞并 11:00
      // 插针 bar，_origLow=4282.625 < 分型价 4287.27），仅扫 endIdx 之后会漏掉。
      // 若真低恰在分型中心上（k === b.endIdx），只改价/时间、idx 不动，笔结构无损。
      if (b.endIdx > toIdx) continue;
      // 候选真低 = _origLow（markWickBars 压平的长下影原低）或 rawLow 原值
      // （探底插针低点允许恢复为端点：60m 7-29 09:00 4010.41 被包含合并吞掉
      //   —— 走 rawLow；1h 9-2 4282.625 被长下影压平 —— 走 _origLow；
      //   240 7-29 底 4009.39 在本次修复后进一步下移到压平真低 3996.055）
      const k0 = innerOn ? b.startIdx + 1 : b.endIdx;
      for (let k = k0; k <= toIdx; k++) {
        const mk = merged[k];
        // 分型中心 bar（k === b.endIdx）只认 _origLow：中心可能因向上合并把早于
        // 本笔结构的老蜡烛吞入链内，其 rawLow 未必属于笔底区间（恢复 rawLow 会
        // 过度下移，超出本次修复目标）；而中心上被 markWickBars 压平的真低
        // （_origLow，如 1h 9-2 4282.625）是本次修复目标，必须恢复。
        // 中心之后的 bar 两者都认（rawLow 恢复 = 原行为，4010.41 先例）。
        const isCenter = k === b.endIdx;
        const candLow = isCenter && mk._origLow === undefined
          ? undefined
          : (mk._origLow !== undefined ? mk._origLow : mk.rawLow);
        if (candLow === undefined || candLow >= mk.low) continue; // 未被合并/压平掩盖
        if (candLow < b.endPrice - eps && (!extreme || candLow < extreme.price)) {
          extreme = {
            price: candLow,
            time: mk._origLowTime !== undefined ? mk._origLowTime : mk.rawLowTime,
            idx: k,
          };
        }
      }
    } else {
      // 终点是顶：保持 endIdx+1 起扫（上影压平不产生 _origHigh，分型中心自身即端点
      // 价，无同类需求；避免影响 4443.715 类历史验收结构）
      let k0 = b.endIdx + 1;
      if (k0 > toIdx) {
        if (!innerOn) continue;
        k0 = b.startIdx + 1; // 相邻分型（toIdx==endIdx）时内部块仍可扫
      } else if (innerOn) {
        k0 = b.startIdx + 1;
      }
      // 被包含合并掩盖的更高真实高点（rawHigh 原值；9-3 16:00 类插针已被
      // markWickBars 压平，其影线价不再出现在 rawHigh 中，不会把 17:00 顶
      // 4442.04 平移回插针价）
      for (let k = k0; k <= toIdx; k++) {
        if (k === b.endIdx) continue; // 分型中心自身 high 即端点价，保持原语义不扫
        const mk = merged[k];
        if (mk.rawHigh === undefined || mk.rawHigh <= mk.high) continue; // 未被掩盖
        if (mk.rawHigh > b.endPrice + eps && (!extreme || mk.rawHigh > extreme.price)) {
          extreme = { price: mk.rawHigh, time: mk.rawHighTime, idx: k };
        }
      }
    }
    if (!extreme) continue;
    if (CHAN_CFG.debug) console.log(`[端点极值修正] ${b.type === "up" ? "上涨" : "下跌"}笔终点 ${b.endPrice} 平移到更极端 ${extreme.price}@idx=${extreme.idx}`);
    // 本笔终点更新
    b.endPrice = extreme.price;
    b.endTime = extreme.time;
    b.endIdx = extreme.idx;
    b.span = b.type === "up" ? b.endPrice - b.startPrice : b.startPrice - b.endPrice;
    b.rawCount = countRaw(merged, b.startIdx, b.endIdx);
    // 下一笔起点联动（保持两笔端点连续）
    next.startPrice = extreme.price;
    next.startTime = extreme.time;
    next.startIdx = extreme.idx;
    next.span = next.type === "up" ? next.endPrice - next.startPrice : next.startPrice - next.endPrice;
    next.rawCount = countRaw(merged, next.startIdx, next.endIdx);
  }
  return bis;
}

// ============================================================
// 5. ATR / MACD
// ============================================================

/**
 * 构建笔中枢（基于笔序列，标准缠论笔中枢）
 * 取连续三笔（笔序列天然交替）的重叠区间构成中枢：
 *   中枢上沿 ZG = min(三笔高点)，中枢下沿 ZD = max(三笔低点)，ZG > ZD 时成立。
 * 中枢形成后支持延伸：后续笔与 [ZD, ZG] 有重叠则纳入中枢（GG/DD 扩展），
 * 出现离开中枢的笔时中枢结束：
 *   - 终点突破中枢边界即离开，起点不论（2026-10-07 起；含从中枢下方直破上沿的
 *     穿越式离开。此前要求起点在中枢内，穿越式离开被误当延伸）；
 *   - 笔与中枢区间完全无重叠 → 离开。
 *     下一笔重新与已纳入笔重叠区相交则本笔只是回抽，中枢继续延伸（二卖与后面
 *     类2卖的公共重叠留在同一中枢）。
 * biCount 包含离开笔。
 * 中枢区间 [zd, zg] 取「构成中枢的全部笔（含离开笔）的重叠部分」：
 *   ZG = min(全部笔高点)，ZD = max(全部笔低点)。
 *   离开/回踩笔与构成笔无重叠（重叠被挤空）时，回退为不含离开笔的重叠（2026-10-07 起）。
 * 中枢水平边缘（用户规则：[进入笔最后一根K-5, 离开笔第一根K+5]）：
 *   - 左边缘 = 进入笔（三笔重叠形成中枢的第一笔 bis[i]）的终点 - 5×barSec
 *   - 右边缘 = 离开笔（bis[j]，识别出 exitTime 的笔）的起点 + 5×barSec
 *   - 无离开笔时右边缘 = 构成中枢最后一笔的终点 + 5×barSec
 * 从该离开笔之后重新扫描下一个中枢。
 *
 * @param {Array} bis 笔数组（已排序，含 startTime/endTime/startPrice/endPrice）
 * @param {number} barSec 本周期单根K线时长（秒），用于左右各外扩 5 根K线；默认 0 表示不外扩
 * @returns {Array} 中枢列表 [{ startTime, endTime, zd, zg, dd, gg, biCount, extended, exitTime, enterEndTime, exitStartTime }]
 */
/** 候选离开笔的下一笔是否仍与已纳入笔的重叠区相交（回抽未离开则中枢继续）。 */
function nextBiReturns(bis, i, j, hi, lo) {
  if (j + 1 >= bis.length) return false;
  let runZd = -Infinity, runZg = Infinity;
  for (let k = i; k <= j; k++) {
    runZd = Math.max(runZd, lo(bis[k]));
    runZg = Math.min(runZg, hi(bis[k]));
  }
  if (runZg <= runZd) return false;
  const nlo = lo(bis[j + 1]), nhi = hi(bis[j + 1]);
  if (nlo > runZg || nhi < runZd) return false;
  return Math.min(runZg, nhi) > Math.max(runZd, nlo);
}

function buildZS(bis, barSec) {
  if (!bis || bis.length < 3) return [];
  const n = bis.length;
  const pad = (barSec || 0) * 5; // 左右各外扩 5 根K线（本周期时长）
  const hi = (b) => Math.max(b.startPrice, b.endPrice);
  const lo = (b) => Math.min(b.startPrice, b.endPrice);
  const zss = [];
  let i = 0;
  while (i + 2 < n) {
    const b1 = bis[i], b2 = bis[i + 1], b3 = bis[i + 2];
    const H1 = hi(b1), L1 = lo(b1);
    const H2 = hi(b2), L2 = lo(b2);
    const H3 = hi(b3), L3 = lo(b3);
    const zg = Math.min(H1, H2, H3);
    const zd = Math.max(L1, L2, L3);
    if (zg > zd) {
      // 三笔重叠 → 形成中枢，向后延伸扫描
      let j = i + 3;
      let dd = Math.min(L1, L2, L3);
      let gg = Math.max(H1, H2, H3);
      let exitTime = null; // 离开中枢的笔的起点时间（若有），中枢右边缘以此为基础外扩
      const eps = 1e-9;
      while (j < n) {
        const bj = bis[j];
        const Hj = hi(bj), Lj = lo(bj);
        if (Lj <= zg && Hj >= zd) { // 与中枢区间有重叠 → 判断延伸还是离开
          // 离开判定：终点突破中枢边界即离开，起点不论（2026-10-07 起；此前还要求
          // 起点在中枢内——从中枢下方直破上沿的「穿越式离开」会被误当延伸，其后悬在
          // 中枢外的干净回踩笔（标准 3买 形态）反被记成离开笔并挤空重叠、整枢被丢弃）。
          const endBreak = bj.endPrice < zd - eps || bj.endPrice > zg + eps;
          // 下一笔又回到当前重叠区（回抽未离开）则本笔仍算延伸，
          // 使二卖与其后类2卖的公共重叠留在同一个中枢里。
          if (endBreak && !nextBiReturns(bis, i, j, hi, lo)) {
            exitTime = bj.startTime; // 离开笔起点
            break;
          }
          dd = Math.min(dd, Lj);
          gg = Math.max(gg, Hj);
          j++;
        } else {
          exitTime = bj.startTime; // 与中枢区间完全无重叠 → 离开中枢，中枢结束
          break;
        }
      }
      // 笔数 = 构成中枢的笔（i..j-1）+ 离开笔（若有 1 笔）
      const biCount = (exitTime !== null ? 1 : 0) + (j - i);
      // 至少 3 笔即可输出中枢（三笔重叠即成）
      if (biCount < 3) { i = j; continue; }
      // 中枢区间 = 构成中枢的全部笔（i..i+biCount-1，含离开笔）的重叠部分：
      //   ZG = min(全部笔高点)，ZD = max(全部笔低点)。
      // 中枢上沿收敛到全部构成笔（含回抽后仍重叠的类2卖笔、以及最终离开笔）的最低高点。
      // 1小时 8-25 起：二卖 4670.83、类2卖 4643.21、类2卖 4631.98 的公共重叠上沿 = 4631.98。
      // 离开/回踩笔与已纳入笔完全无重叠（悬在中枢外）时会把重叠挤空——回退为不含
      // 离开笔的构成笔重叠（2026-10-07 起；此前整枢被防御性丢弃，标准 3买 的低中枢
      // 因此消失、其后高点被误标类2买）。
      let zsZd = -Infinity, zsZg = Infinity;
      for (let k = i; k < i + biCount; k++) {
        const bk = bis[k];
        zsZd = Math.max(zsZd, lo(bk));
        zsZg = Math.min(zsZg, hi(bk));
      }
      if (zsZg <= zsZd && exitTime !== null) {
        zsZd = -Infinity; zsZg = Infinity;
        for (let k = i; k < j; k++) {
          const bk = bis[k];
          zsZd = Math.max(zsZd, lo(bk));
          zsZg = Math.min(zsZg, hi(bk));
        }
      }
      // 全部笔重叠后仍可能 zg <= zd（如笔数过多、覆盖区间收窄为空），防御性跳过
      if (zsZg <= zsZd) { i = j; continue; }
      // 水平边缘（用户规则：[进入笔最后一根K-5, 离开笔第一根K+5]）：
      //   进入笔 = 三笔重叠形成中枢的第一笔 bis[i]，其最后一根K即终点；
      //   离开笔 = 中枢结束时的笔（exitTime 的笔，即 bis[j]），其第一根K即起点。
      //   无离开笔时右边缘取构成中枢最后一笔的终点。
      const enterEndTime = b1.endTime;                                  // 进入笔终点（外扩前）
      const exitStartTime = exitTime !== null ? exitTime : bis[j - 1].endTime; // 离开笔起点（外扩前）
      zss.push({
        startTime: enterEndTime - pad,  // 左边缘 = 进入笔终点 - 5根K
        endTime: exitStartTime + pad,   // 右边缘 = 离开笔起点 + 5根K
        zd: zsZd, zg: zsZg, dd, gg,
        biCount,
        extended: biCount > 3,
        exitTime,               // 离开中枢的笔的起点时间（记录，供排查）
        enterEndTime,           // 进入笔终点（外扩前原始时间）
        exitStartTime,          // 离开笔起点（外扩前原始时间）
      });
      i = j; // 从离开中枢的笔开始重新扫描
    } else {
      i++; // 三笔不重叠，滑窗
    }
  }
  return zss;
}

/**
 * 按上级笔分解构建中枢（分解原则，不跨周期）：
 * 本级别中枢只能构建在「同一个上级笔」内部。用上级笔时间区间把本级别笔切段，
 * 每段内独立运行 buildZS，保证中枢不跨上级笔端点。
 * 例如 15分钟中枢一定落在同一个 60分钟笔内（上级笔 = 上一层的最终绘制笔）。
 *
 * @param {Array} lowerBis   本级别笔（已校准/对齐的最终绘制笔）
 * @param {Array} upperBis   上一级别笔（用于分解约束，可为空数组）
 * @param {number} tolSec    时间容差（秒）：本级别端点经低一级校准后可能与上级端点有
 *                           最多一个本级别bar的偏移，如 15分钟 09:03 vs 60分钟 09:00；
 *                           同时作为 buildZS 的 barSec——中枢水平边缘左右各外扩 5×tolSec
 *                           （如 1小时周期 tolSec=3600 → 外扩 5 小时）
 * @returns {Array} 中枢列表，每项额外含 upperStart/upperEnd（所属上级笔时间范围）
 */
function buildZSByUpper(lowerBis, upperBis, tolSec, openLast = true) {
  lowerBis = pointEligibleBis(lowerBis);
  if (!lowerBis || lowerBis.length < 3) return [];
  const tol = tolSec || 0;
  const out = [];
  if (!upperBis || upperBis.length === 0) {
    // 无上级约束（如最外层）：直接用本级别全部笔构建
    for (const z of buildZS(lowerBis, tol)) {
      z.upperStart = lowerBis[0].startTime;
      z.upperEnd = lowerBis[lowerBis.length - 1].endTime;
      out.push(z);
    }
    return out;
  }
  // 按时间完整归属到上级笔区间：笔必须 startTime 与 endTime 都落在同一上级笔内
  // （含 tol 容差，因本级别端点经低一级校准可能与上级端点有最多一个bar的偏移）。
  // 只按 startTime 归属会把「起点在段内、终点已越出段边界」的笔误纳入本段，
  // 导致中枢延伸跨越上级笔端点（违反分解原则）。不完整落在任何上级笔内的笔不参与中枢。
  const segments = [];
  let cur = null; // { upper, bis: [] }
  for (const b of lowerBis) {
    let ub = null;
    for (const u of upperBis) {
      if (b.startTime >= u.startTime - tol && (b.endTime <= (u.coverageEnd ?? u.endTime) + tol || (openLast && u === upperBis[upperBis.length-1] && u.coverageEnd == null))) { ub = u; break; }
    }
    if (!ub) continue; // 不完整归属任何上级笔的零散笔不参与中枢
    if (!cur || cur.upper !== ub) {
      if (cur && cur.bis.length) segments.push(cur);
      cur = { upper: ub, bis: [] };
    }
    cur.bis.push(b);
  }
  if (cur && cur.bis.length) segments.push(cur);
  for (const seg of segments) {
    for (const z of buildZS(seg.bis, tol)) {
      z.upperStart = seg.upper.startTime;
      z.upperEnd = seg.upper.endTime;
      out.push(z);
    }
  }
  return out;
}

// ============================================================
// 5. ATR / MACD
// ============================================================

/** 计算 ATR（14周期平均真实波幅） */
function calcATR(rawBars, period = 14) {
  const trs = [];
  for (let i = 1; i < rawBars.length; i++) {
    const h = rawBars[i].high, l = rawBars[i].low, pc = rawBars[i - 1].close;
    trs.push(Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc)));
  }
  const start = Math.max(0, trs.length - period);
  const slice = trs.slice(start);
  if (slice.length === 0) return 0;
  return slice.reduce((a, b) => a + b, 0) / slice.length;
}

/**
 * 计算 MACD（基于原始K线收盘价 EMA12/EMA26/DIF/DEA）
 * 约定：macd > 0 为红柱（多头动能），macd < 0 为绿柱（空头动能）。
 * 返回：[{ time, macd, dif, dea }, ...]，time 与原始K线一一对应。
 */
function calcMACD(rawBars) {
  if (!rawBars || rawBars.length < 2) return [];
  const closes = rawBars.map(b => b.close);
  const ema = (period) => {
    const k = 2 / (period + 1);
    const out = [];
    let prev = closes[0];
    out.push(prev);
    for (let i = 1; i < closes.length; i++) {
      prev = closes[i] * k + prev * (1 - k);
      out.push(prev);
    }
    return out;
  };
  const ema12 = ema(12);
  const ema26 = ema(26);
  const dif = closes.map((_, i) => ema12[i] - ema26[i]);
  const dea = [];
  let prevDea = dif[0];
  dea.push(prevDea);
  for (let i = 1; i < dif.length; i++) {
    prevDea = dif[i] * (2 / (9 + 1)) + prevDea * (1 - (2 / (9 + 1)));
    dea.push(prevDea);
  }
  return rawBars.map((b, i) => ({
    time: b.time,
    macd: (dif[i] - dea[i]) * 2,
    dif: dif[i],
    dea: dea[i],
  }));
}

// ============================================================
// 6. MACD 背驰判定
// ============================================================

/** 模块级时间格式化（供 DEBUG 打印使用） */
function fmtT(ts) {
  const dt = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${dt.getMonth()+1}-${dt.getDate()} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
}

/**
 * 计算一笔区间内的 MACD 动能指标（用于背驰判定）
 * 返回：{ redArea, greenArea, difHigh, difLow, redMax, greenMax }
 */
function biMacdMetrics(bi, macdArr) {
  const metrics = { redArea: 0, greenArea: 0, difHigh: -Infinity, difLow: Infinity, redMax: 0, greenMax: 0 };
  if (!macdArr || macdArr.length === 0) return null;
  const t0 = bi.startTime, t1 = bi.endTime;
  let found = false;
  for (const m of macdArr) {
    if (m.time < t0) continue;
    if (m.time > t1) break;
    found = true;
    if (m.macd > 0) {
      metrics.redArea += m.macd;
      if (m.macd > metrics.redMax) metrics.redMax = m.macd;
    } else {
      metrics.greenArea += -m.macd;
      if (-m.macd > metrics.greenMax) metrics.greenMax = -m.macd;
    }
    if (m.dif > metrics.difHigh) metrics.difHigh = m.dif;
    if (m.dif < metrics.difLow) metrics.difLow = m.dif;
  }
  if (!found) return null;
  return metrics;
}

/**
 * 面积判据的时长可比门：面积Σ = 柱高×K线根数的累加，与区间时长线性相关——
 * 时长悬殊的两段（如 15.65h 缓跌 vs 4.7h 急跌，Σ=136.2 vs 22.7）面积差主要来自
 * 时长而非动能，直接比较会把「短时急跌」误判为背驰。两段时长比 > divergeDurRatio
 * （或某段时长为 0/负）时返回 false → 面积项不计入背驰，只用 DIF/柱高判据。
 */
function areaDurComparable(a, b) {
  const da = (a.endTime || 0) - (a.startTime || 0);
  const db = (b.endTime || 0) - (b.startTime || 0);
  const mx = Math.max(da, db), mn = Math.min(da, db);
  return mn > 0 && mx / mn <= CHAN_CFG.divergeDurRatio;
}

/**
 * MACD 背驰判定（OR 关系，满足其一即算背驰）：
 *   底背驰（对应一买，下跌笔）：绿柱面积变小 或 黄白线低点抬高 或 绿柱最大高度变小（下跌动能减弱）
 *   顶背驰（对应一卖，上涨笔）：红柱面积变小 或 黄白线高点变低 或 红柱最大高度变小（上涨动能减弱）
 *   面积两项受 areaDurComparable 时长门约束（两段时长不可比时仅用 DIF/柱高判据）。
 */
// 2026-10-01 起双判据 AND（与 Python 一致）：
//   底背驰（下跌笔）：黄白线低点抬高 且（时长可比时）绿柱面积变小；
//   顶背驰（上涨笔）：黄白线高点变低 且（时长可比时）红柱面积变小。
// 面积受 areaDurComparable 时长门约束：两段时长不可比时面积不计入，DIF 单判据兜底。
// （旧口径 面积/DIF/单根最大柱高 三项 OR 任一命中——柱高项已废除并收紧为 AND。）
function isBiDiverge(bi, refer, macdArr) {
  const cur = biMacdMetrics(bi, macdArr);
  const ref = biMacdMetrics(refer, macdArr);
  if (!cur || !ref) return false;
  if (bi.type === "down") {
    if (!(cur.difLow > ref.difLow)) return false;
    return !areaDurComparable(bi, refer) || cur.greenArea < ref.greenArea;
  }
  if (!(cur.difHigh < ref.difHigh)) return false;
  return !areaDurComparable(bi, refer) || cur.redArea < ref.redArea;
}

// ============================================================
// 7. MACD 红绿转换检测
// ============================================================

/**
 * 检测两个分型（合并K线索引区间）之间是否发生方向性 MACD 红绿转换。
 *   direction === "up"  ：底到顶（上涨），柱状体由绿变红（<=0 转 >0）
 *   direction === "down"：顶到底（下跌），柱状体由红变绿（>0 转 <=0）
 *   其余（undefined）：任意红绿转换（历史兼容）
 * 检测区间用「分型的极值时间」作为边界（而不是合并K线的最新时间），
 * 避免把顶底极值之后（合并K线包含区间内）的 MACD 变化误算进来。
 */
function hasMacdCrossBetween(macdArr, merged, aIdx, bIdx, aTime, bTime, direction) {
  if (!macdArr || macdArr.length === 0) return false;
  const t0 = aTime !== undefined ? aTime : merged[aIdx].time;
  const t1 = bTime !== undefined ? bTime : merged[bIdx].time;
  let prev = null;
  for (const mm of macdArr) {
    if (mm.time < t0) continue;
    if (mm.time > t1) break;
    if (prev !== null) {
      let crossed;
      if (direction === "up") {
        crossed = prev.macd <= 0 && mm.macd > 0;
      } else if (direction === "down") {
        crossed = prev.macd > 0 && mm.macd <= 0;
      } else {
        crossed = (prev.macd >= 0 && mm.macd < 0) || (prev.macd <= 0 && mm.macd > 0);
      }
      if (crossed) return true;
    }
    prev = mm;
  }
  return false;
}

// ============================================================
// 8. 未完成笔延伸 / 周期映射 / 端点校准
// ============================================================

/**
 * 未完成笔延伸：缠论要求最新一笔延伸到当前K线。
 * 当最后一笔方向上的极端价出现在窗口末尾（当前笔终点之后）时，
 * 把最后一笔的终点推进到该极端价所在K线。只处理最后一笔。
 */
function extendLastBi(bisArr, bars) {
  if (!bisArr || bisArr.length === 0) return bisArr;
  const last = bisArr[bisArr.length - 1];
  // 跳空独立成笔锁定的笔：终点固定在跳空缺口处，不参与延伸（避免吞并跳空笔）
  if (last.gapLocked) return bisArr;
  const startIdx = bars.findIndex(k => k.time >= last.startTime);
  if (startIdx === -1) return bisArr;
  const tail = bars.slice(startIdx);
  if (tail.length < 2) return bisArr;

  if (last.type === "up") {
    let maxBar = tail[0];
    for (const k of tail) if (k.high > maxBar.high) maxBar = k;
    if (maxBar.time > last.endTime && maxBar.high > last.endPrice) {
      last.endTime = maxBar.time;
      last.endPrice = maxBar.high;
      last.span = maxBar.high - last.startPrice;
    }
  } else {
    let minBar = tail[0];
    for (const k of tail) if (k.low < minBar.low) minBar = k;
    if (minBar.time > last.endTime && minBar.low < last.endPrice) {
      last.endTime = minBar.time;
      last.endPrice = minBar.low;
      last.span = last.startPrice - minBar.low;
    }
  }
  return bisArr;
}

/** 逐级校准映射：每个周期用其「低一级」周期校准端点时间（15分钟←3分钟，1小时←15分钟，4小时←1小时，日线←4小时） */
function lowerResOf(res) {
  const s = String(res).toUpperCase();
  if (s === "D" || s === "1D") return "240";
  if (s === "240" || s === "4H") return "60";
  if (s === "60" || s === "1H") return "15";
  if (s === "15") return "3";
  return null;
}

/**
 * 跨周期端点时间校准（逐级校准）：
 * 大周期K线的时间戳是 bar 起点，其内部最高/最低点可能发生在更晚的低一级K线上。
 * 每个周期用「低一级」周期K线校准：把本周期笔的端点时间校准到
 * 「低一级K线极值所在位置」，使不同周期对同一极值的标记位置在图上重合。
 */
function calibrateBiTimes(bis, bigBars, refBars, bigIntervalSec) {
  if (!bis || bis.length === 0 || !refBars || refBars.length === 0) return bis;
  const eps = 0.001;
  const calibrateTime = (t, price) => {
    const big = bigBars.find(k => k.time <= t && t < k.time + bigIntervalSec);
    if (!big) return t;
    const rangeEnd = big.time + bigIntervalSec;
    let best = null;
    for (const rb of refBars) {
      if (rb.time < big.time || rb.time >= rangeEnd) continue;
      if (Math.abs(rb.high - price) < eps || Math.abs(rb.low - price) < eps) {
        best = rb;
      }
    }
    return best ? best.time : t;
  };
  for (const b of bis) {
    b.startTime = calibrateTime(b.startTime, b.startPrice);
    b.endTime = calibrateTime(b.endTime, b.endPrice);
  }
  return bis;
}

/** 周期 → 单根K线时长（秒）。注意 "30" = 30分钟，"30S" = 30秒（TradingView resolution 后缀 S 表秒级） */
function intervalSecOf(res) {
  const r = String(res).toUpperCase();
  if (/^\d+S$/.test(r)) return parseInt(r, 10) || 0; // "30S" → 30（秒级）
  if (r === "3") return 180;
  if (r === "5") return 300;
  if (r === "15") return 900;
  if (r === "30") return 1800;
  if (r === "60" || r === "1H") return 3600;
  if (r === "240" || r === "4H") return 14400;
  if (r === "D" || r === "1D") return 86400;
  if (r === "W" || r === "1W") return 604800;
  return 0;
}

// 每周期近等双顶开关：周期码（含 1H/4H/1D 别名）或 barSec 秒数 → CHAN_CFG 开关键。
// 五周期之外（'30S'/'5'/'30'/'W'/未知秒数/非 str|number）一律 false——沿用旧口径
// intervalSecOf(res) >= 3600 的失败安全语义。注意：旧口径对 W(604800) 会开启，
// 此处收窄为 false；全链路（画笔/回测/对拍）无 W 调用点，零实际影响。
const NEAR_DOUBLE_SWITCH_BY_SEC = { 180: "nearDouble3", 900: "nearDouble15", 3600: "nearDouble60",
                                   14400: "nearDouble240", 86400: "nearDoubleD" };
const WIDE_BAR_POINTS_BY_SEC = { 180: "wideBarPoints3", 900: "wideBarPoints15", 3600: "wideBarPoints60",
                                 14400: "wideBarPoints240", 86400: "wideBarPointsD" };
function wideBarPointsOf(res) {
  // 该周期「顶底分形不能包含」的单根长K豁免点数。未列周期、缺省、或配置为 0 时返回 0（不豁免）。
  // wideBarOn=false：总开关关闭，所有周期一律 0。
  if (CHAN_CFG.wideBarOn === false) return 0;
  if (typeof res !== "number" && typeof res !== "string") return 0;
  const sec = typeof res === "number" ? res : intervalSecOf(res);
  const key = WIDE_BAR_POINTS_BY_SEC[sec];
  const v = key ? Number(CHAN_CFG[key]) : 0;
  return v > 0 ? v : 0;
}
function nearDoubleOn(res) {
  if (typeof res !== "number" && typeof res !== "string") return false;
  const sec = typeof res === "number" ? res : intervalSecOf(res);
  const key = NEAR_DOUBLE_SWITCH_BY_SEC[sec];
  return key ? CHAN_CFG[key] === true : false;
}

// ============================================================
// 9. 买卖点识别
// ============================================================

/**
 * 判断本周期某笔是否与上一级别某笔完全重合（起点、终点时间与价格一致）。
 * 完全重合说明本周期该笔内部无更细结构（整笔就是上一级别的一笔），
 * 本级别无从选有效参照（跨上级笔边界的比较无意义）→ 上级笔已结束时由
 * 本周期做「同笔」纯结构标记（免参照/创新低/背驰）；上级笔仍为末笔
 * （延伸中、反向笔未确认）时不标记。
 * 时间容差 = 本周期 1 个 bar（低级别笔用更低一级校准，与上级笔端点可能有最多一个bar的偏差）。
 * @returns 命中的上级笔对象（与 upperBis 内元素同引用）| null（未命中/空表），
 *          调用方可按真值使用（旧 bool 契约兼容）。
 */
function isSameAsUpperBi(bi, upperBis, barSec) {
  if (!upperBis || upperBis.length === 0) return null;
  const tEps = barSec || 900;
  const pEps = 0.01;
  for (const ub of upperBis) {
    if (ub.type !== bi.type) continue;
    if (Math.abs(ub.startTime - bi.startTime) <= tEps &&
        Math.abs(ub.endTime - bi.endTime) <= tEps &&
        Math.abs(ub.startPrice - bi.startPrice) <= pEps &&
        Math.abs(ub.endPrice - bi.endPrice) <= pEps) {
      return ub;
    }
  }
  return null;
}

/**
 * 一买锚定：低级别的一买必须锚定到「上一级别某笔的起点」。
 * 在上级笔列表中取「候选一买时间之前、时间上最近的一个底部端点」。
 * 找不到则返回 null（该周期不标记一买）。
 */
function anchorFirstBuy(cand, upperBis) {
  upperBis = confirmedStructureBis(upperBis);
  if (!upperBis || upperBis.length === 0) return null;
  let best = null;
  for (const b of upperBis) {
    const t = b.type === "up" ? b.startTime : b.endTime;
    const p = b.type === "up" ? b.startPrice : b.endPrice;
    if (t > cand.time) continue;
    if (!best || cand.time - t < cand.time - best.time) best = { time: t, price: p };
  }
  return best;
}

/**
 * 一卖锚定：低级别的一卖必须锚定到「上一级别上涨笔的结束点」（上涨线段的终点）。
 * 1) 若候选一卖位于某上级上涨笔内部（该上涨笔尚未结束），上移到上级上涨笔的结束点；
 * 2) 否则取「候选一卖之前、时间上最近的一个顶部端点」。
 * 找不到则返回 null。
 */
function anchorFirstSell(cand, upperBis) {
  upperBis = confirmedStructureBis(upperBis);
  if (!upperBis || upperBis.length === 0) return null;
  for (const b of upperBis) {
    if (b.type !== "up") continue;
    if (b.startTime <= cand.time && b.endTime >= cand.time) {
      return { time: b.endTime, price: b.endPrice };
    }
  }
  let best = null;
  for (const b of upperBis) {
    const t = b.type === "up" ? b.endTime : b.startTime;
    const p = b.type === "up" ? b.endPrice : b.startPrice;
    if (t > cand.time) continue;
    if (!best || cand.time - t < cand.time - best.time) best = { time: t, price: p };
  }
  return best;
}

/** 把极值价格/时间映射到本周期K线的 bar 边界 */
function snapToOwnBar(price, refTime, bars) {
  const eps = 0.001;
  let best = null, bestDist = Infinity;
  for (const k of bars) {
    if (Math.abs(k.high - price) < eps || Math.abs(k.low - price) < eps) {
      const d = Math.abs(k.time - refTime);
      if (d < bestDist) { bestDist = d; best = k.time; }
    }
  }
  if (best !== null) return best;
  let nearest = bars.length > 0 ? bars[0].time : refTime;
  let nd = Infinity;
  for (const k of bars) { const d = Math.abs(k.time - refTime); if (d < nd) { nd = d; nearest = k.time; } }
  return nearest;
}

/**
 * 取与上级笔段重叠的中枢（优先 upperStart/End 精确匹配）。
 */
function pickZsForSeg(zss, segStart, segEnd) {
  if (!zss || !zss.length) return null;
  let exact = zss.filter(z => z.upperStart === segStart && z.upperEnd === segEnd);
  let pool = exact;
  if (!pool.length) {
    pool = [];
    for (const z of zss) {
      if (z.upperStart != null && z.upperEnd != null) {
        if (z.upperStart <= segEnd && z.upperEnd >= segStart) pool.push(z);
      } else if (z.startTime <= segEnd && z.endTime >= segStart) {
        pool.push(z);
      }
    }
  }
  if (!pool.length) return null;
  return pool.reduce((a, b) => ((a.enterEndTime || a.startTime) >= (b.enterEndTime || b.startTime) ? a : b));
}

/** 为 2买/2卖选取关联中枢：优先覆盖该点，否则取其后最早形成。 */
function pickZsForTwo(zss, segStart, segEnd, twoTime) {
  if (!zss || !zss.length) return null;
  const pool = [];
  for (const z of zss) {
    if (z.upperStart != null && z.upperEnd != null) {
      if (!(z.upperStart <= segEnd && z.upperEnd >= segStart)) continue;
    } else if (!(z.startTime <= segEnd && z.endTime >= segStart)) continue;
    pool.push(z);
  }
  if (!pool.length) return null;
  const t0 = z => (z.enterEndTime != null ? z.enterEndTime : z.startTime);
  const t1 = z => (z.exitTime != null ? z.exitTime : (z.exitStartTime != null ? z.exitStartTime : z.endTime));
  const containing = pool.filter(z => t0(z) <= twoTime && twoTime <= t1(z));
  if (containing.length) return containing.reduce((a, b) => (t0(a) <= t0(b) ? a : b));
  const after = pool.filter(z => t0(z) >= twoTime);
  if (after.length) return after.reduce((a, b) => (t0(a) <= t0(b) ? a : b));
  return pool.reduce((a, b) => (t0(a) >= t0(b) ? a : b));
}

/** 同段离枢回踩/反弹的序号。第1个=3，第2个=类3，第3个=4，其后都是类4。 */
function leavePointType(index, isSell) {
  const side = isSell ? "卖" : "买";
  if (index <= 0) return "3" + side;
  if (index === 1) return "类3" + side;
  if (index === 2) return "4" + side;
  return "类4" + side;
}

const LEAVE_BUY = ["3买", "类3买", "4买", "类4买"];
const LEAVE_SELL = ["3卖", "类3卖", "4卖", "类4卖"];

function appendThirdPoints(points, thirdList, familyTypes, class2Type) {
  const family = new Set(familyTypes);
  for (const t of thirdList) {
    if (points.some(p => family.has(p.type) && p.time === t.time)) continue;
    const dup = points.findIndex(p => p.type === class2Type && p.time === t.time);
    if (dup >= 0) points.splice(dup, 1);
    points.push(t);
  }
}

/**
 * 买点识别：2买抬高结构（不依赖中枢）；类2/3/类3 依赖中枢；1买不变。
 * 返回按 time 升序（稳定排序；同刻保持识别序 2/类2→1买→3/4类，尾点=时间最新）。
 */
function findBuyPoints(bis, upperBis, macdArr, barSec, class2ZsTol, thirdZsTol) {
  bis = pointEligibleBis(bis);
  const knownUpper = confirmedStructureBis(upperBis);
  if (bis.length < 3) return [];
  const c2tol = +(class2ZsTol || 0);
  const t3tol = +(thirdZsTol || 0);
  const downIdx = [];
  bis.forEach((b, i) => { if (b.type === "down") downIdx.push(i); });
  const downLows = downIdx.map(i => ({ biIdx: i, time: bis[i].endTime, price: bis[i].endPrice }));
  const idxByEndTime = {};
  bis.forEach((b, i) => { if (idxByEndTime[b.endTime] == null) idxByEndTime[b.endTime] = i; });
  let upperByType = null;
  if (knownUpper.length > 0) {
    upperByType = {
      up: knownUpper.filter(u => u.type === "up"),
      down: knownUpper.filter(u => u.type === "down"),
    };
  }

  const firstBuys = [];
  for (let k = 1; k < downIdx.length; k++) {
    const cur = bis[downIdx[k]];
    if (cur._forming) continue;
    const sameUpper = upperByType != null
      ? isSameAsUpperBi(cur, upperByType[cur.type] || [], barSec)
      : null;
    if (sameUpper) {
      if (sameUpper === knownUpper[knownUpper.length - 1]) {
        if (CHAN_CFG.debug) console.log(`[一买跳过-上级末笔延伸中] ${fmtT(cur.endTime)}(${cur.endPrice}) 与上级末笔重合，上级反向笔未确认`);
        continue;
      }
      if (CHAN_CFG.debug) console.log(`[一买同笔] ${fmtT(cur.endTime)}(${cur.endPrice}) 与上级已结束下跌笔重合，结构同笔标记1买`);
      firstBuys.push({ biIdx: downIdx[k], time: cur.endTime, price: cur.endPrice });
      continue;
    }
    let refer = null;
    for (let j = k - 1; j >= 0; j--) {
      const cand = bis[downIdx[j]];
      if (cand.span < cur.span * 0.5) continue;
      refer = cand;
      break;
    }
    if (refer && cur.endPrice < refer.endPrice) {
      const diverge = isBiDiverge(cur, refer, macdArr);
      if (CHAN_CFG.debug) {
        const cm = biMacdMetrics(cur, macdArr);
        const rm = biMacdMetrics(refer, macdArr);
        console.log(
          `[一买候选] ${fmtT(cur.endTime)}(${cur.endPrice}) vs 参照 ${fmtT(refer.endTime)}(${refer.endPrice}) ` +
          `| 创新低=${cur.endPrice < refer.endPrice} ` +
          `| 绿柱面积 ${cm ? cm.greenArea.toFixed(2) : "-"} vs ${rm ? rm.greenArea.toFixed(2) : "-"} ` +
          `| DIF低点 ${cm ? cm.difLow.toFixed(3) : "-"} vs ${rm ? rm.difLow.toFixed(3) : "-"} | 背驰=${diverge}`
        );
      }
      if (diverge) firstBuys.push({ biIdx: downIdx[k], time: cur.endTime, price: cur.endPrice });
    }
  }
  const firstBuy = firstBuys.length ? firstBuys[firstBuys.length - 1] : null;

  const points = [];
  const twoBuyMeta = [];

  if (upperBis && upperBis.length > 0) {
    const zss = buildZSByUpper(bis, upperBis, barSec);
    for (const up of upperBis) {
      if (up.type !== "up") continue;
      const lows = downLows
        .filter(l => l.time >= up.startTime && l.time <= (up.coverageEnd ?? up.endTime) + 1)
        .slice()
        .sort((a, b) => a.time - b.time);
      if (!lows.length) continue;
      const firstLow = lows.find(l => l.price > up.startPrice);
      if (!firstLow) continue;
      points.push({ type: "2买", time: firstLow.time, price: firstLow.price });
      const zs = pickZsForTwo(zss, up.startTime, up.endTime, firstLow.time);
      if (!zs) continue;
      const zd = zs.zd, zg = zs.zg;
      twoBuyMeta.push({
        time: firstLow.time, price: firstLow.price,
        zg, segStart: up.startTime, segEnd: up.coverageEnd ?? up.endTime,
      });
      // 同一中枢内每一个更高抬低都标类2买（低点可低于收敛后的 zd）
      let prevPx = firstLow.price;
      for (const l of lows) {
        if (l.time <= firstLow.time || !(l.price > prevPx)) continue;
        const b = bis[l.biIdx];
        const bhi = Math.max(b.startPrice, b.endPrice);
        if (l.price <= zg && bhi >= (zd - c2tol)) {
          points.push({ type: "类2买", time: l.time, price: l.price });
          prevPx = l.price;
        }
      }
    }
  } else {
    let structBottomIdx = null;
    if (firstBuy) {
      let minP = Infinity;
      for (const i of downIdx) {
        if (i >= firstBuy.biIdx) break;
        if (bis[i].endPrice < minP) { minP = bis[i].endPrice; structBottomIdx = i; }
      }
    }
    if (structBottomIdx == null) {
      let minP = Infinity;
      for (const i of downIdx) {
        if (bis[i].endPrice < minP) { minP = bis[i].endPrice; structBottomIdx = i; }
      }
    }
    if (structBottomIdx != null) {
      const bottom = bis[structBottomIdx];
      let secondBuy = null;
      for (let i = structBottomIdx + 1; i < bis.length; i++) {
        if (bis[i].type !== "down") continue;
        if (bis[i].endPrice > bottom.endPrice) {
          secondBuy = { biIdx: i, time: bis[i].endTime, price: bis[i].endPrice };
          break;
        }
      }
      if (secondBuy) {
        points.push({ type: "2买", time: secondBuy.time, price: secondBuy.price });
        const zss = buildZS(bis, barSec);
        const zs = pickZsForTwo(zss, secondBuy.time, secondBuy.time, secondBuy.time);
        if (zs) {
          const zd = zs.zd, zg = zs.zg;
          const t0 = zs.enterEndTime != null ? zs.enterEndTime : zs.startTime;
          const t1 = zs.exitTime != null ? zs.exitTime : zs.endTime;
          twoBuyMeta.push({
            time: secondBuy.time, price: secondBuy.price,
            zg, segStart: t0, segEnd: t1,
          });
          let prevPx = secondBuy.price;
          for (let i = secondBuy.biIdx + 1; i < bis.length; i++) {
            if (bis[i].type !== "down") continue;
            const p = bis[i].endPrice;
            const bhi = Math.max(bis[i].startPrice, bis[i].endPrice);
            if (p > prevPx && p <= zg && bhi >= (zd - c2tol)) {
              points.push({ type: "类2买", time: bis[i].endTime, price: p });
              prevPx = p;
            }
          }
        }
      }
    }
  }

  for (const fb of firstBuys) points.push({ type: "1买", time: fb.time, price: fb.price });

  twoBuyMeta.sort((a, b) => a.time - b.time);
  const allTwoBuy = points.filter(p => p.type === '2买').slice().sort((a, b) => a.time - b.time);
  const thirdOut = [];
  for (const tb of twoBuyMeta) {
    let twoIdx = idxByEndTime[tb.time];
    if (twoIdx == null) twoIdx = -1;
    if (twoIdx < 0) continue;
    let endScan = bis.length;
    for (const nxt of allTwoBuy) {
      if (nxt.time > tb.time) {
        endScan = idxByEndTime[nxt.time];
        if (endScan == null) endScan = bis.length;
        break;
      }
    }
    const zg = tb.zg;
    const valids = [];
    for (let i = twoIdx + 1; i < endScan; i++) {
      if (bis[i].type !== "up") continue;
      if (bis[i].endPrice <= zg) continue;
      for (let mm = i + 1; mm < endScan; mm++) {
        if (bis[mm].type !== "down") continue;
        const bt = bis[mm].endTime, bp = bis[mm].endPrice;
        if (bp > zg - t3tol && bt >= tb.segStart && bt <= tb.segEnd + 1) {
          valids.push({ time: bt, price: bp });
        }
        break;
      }
    }
    if (valids.length > 0) {
      valids.forEach((v, k) => {
        thirdOut.push({ type: leavePointType(k, false), time: v.time, price: v.price });
      });
    }
  }
  appendThirdPoints(points, thirdOut, LEAVE_BUY, "类2买");
  points.sort((a, b) => a.time - b.time); // 时间升序稳定排序；同刻保持识别序（2/类2→1买→3/4类）
  return points;
}

/**
 * 卖点识别：2卖次高结构（不依赖中枢）；类2/3/类3 依赖中枢；与买点对称。
 * 返回按 time 升序（稳定排序；同刻保持识别序 2/类2→1卖→3/4类，尾点=时间最新）。
 */
function findSellPoints(bis, upperBis, macdArr, barSec, class2ZsTol, thirdZsTol) {
  bis = pointEligibleBis(bis);
  const knownUpper = confirmedStructureBis(upperBis);
  if (bis.length < 3) return [];
  const c2tol = +(class2ZsTol || 0);
  const t3tol = +(thirdZsTol || 0);
  const upIdx = [];
  bis.forEach((b, i) => { if (b.type === "up") upIdx.push(i); });
  const upHighs = upIdx.map(i => ({ biIdx: i, time: bis[i].endTime, price: bis[i].endPrice }));
  const idxByEndTime = {};
  bis.forEach((b, i) => { if (idxByEndTime[b.endTime] == null) idxByEndTime[b.endTime] = i; });
  let upperByType = null;
  if (knownUpper.length > 0) {
    upperByType = {
      up: knownUpper.filter(u => u.type === "up"),
      down: knownUpper.filter(u => u.type === "down"),
    };
  }

  const firstSells = [];
  for (let k = 1; k < upIdx.length; k++) {
    const cur = bis[upIdx[k]];
    if (cur._forming) continue;
    const sameUpper = upperByType != null
      ? isSameAsUpperBi(cur, upperByType[cur.type] || [], barSec)
      : null;
    if (sameUpper) {
      if (sameUpper === knownUpper[knownUpper.length - 1]) {
        if (CHAN_CFG.debug) console.log(`[一卖跳过-上级末笔延伸中] ${fmtT(cur.endTime)}(${cur.endPrice}) 与上级末笔重合，上级反向笔未确认`);
        continue;
      }
      if (CHAN_CFG.debug) console.log(`[一卖同笔] ${fmtT(cur.endTime)}(${cur.endPrice}) 与上级已结束上涨笔重合，结构同笔标记1卖`);
      firstSells.push({ biIdx: upIdx[k], time: cur.endTime, price: cur.endPrice });
      continue;
    }
    let refer = null;
    for (let j = k - 1; j >= 0; j--) {
      const cand = bis[upIdx[j]];
      if (cand.span < cur.span * 0.5) continue;
      refer = cand;
      break;
    }
    if (refer && cur.endPrice > refer.endPrice) {
      const diverge = isBiDiverge(cur, refer, macdArr);
      if (CHAN_CFG.debug) {
        const cm = biMacdMetrics(cur, macdArr);
        const rm = biMacdMetrics(refer, macdArr);
        console.log(
          `[一卖候选] ${fmtT(cur.endTime)}(${cur.endPrice}) vs 参照 ${fmtT(refer.endTime)}(${refer.endPrice}) ` +
          `| 创新高=${cur.endPrice > refer.endPrice} ` +
          `| 红柱面积 ${cm ? cm.redArea.toFixed(2) : "-"} vs ${rm ? rm.redArea.toFixed(2) : "-"} ` +
          `| DIF高点 ${cm ? cm.difHigh.toFixed(3) : "-"} vs ${rm ? rm.difHigh.toFixed(3) : "-"} | 背驰=${diverge}`
        );
      }
      if (diverge) firstSells.push({ biIdx: upIdx[k], time: cur.endTime, price: cur.endPrice });
    }
  }
  const firstSell = firstSells.length ? firstSells[firstSells.length - 1] : null;

  const anchoredSells = [];
  const seenSellPos = new Set();
  for (const fs of firstSells) {
    let anchored = fs;
    if (upperBis && upperBis.length > 0) {
      const a = anchorFirstSell(fs, upperBis);
      if (a) {
        let bestBi = null, bestDist = Infinity;
        for (let i = 0; i < bis.length; i++) {
          const b = bis[i];
          if (b.type !== "up") continue;
          const d = Math.abs(b.endTime - a.time);
          if (d < bestDist) { bestDist = d; bestBi = i; }
        }
        anchored = {
          biIdx: bestBi !== null ? bestBi : fs.biIdx,
          time: a.time,
          price: a.price,
        };
      }
    }
    if (seenSellPos.has(anchored.time)) continue;
    seenSellPos.add(anchored.time);
    anchoredSells.push(anchored);
  }

  const points = [];
  const twoSellMeta = [];

  if (upperBis && upperBis.length > 0) {
    const zss = buildZSByUpper(bis, upperBis, barSec);
    for (const dn of upperBis) {
      if (dn.type !== "down") continue;
      const highs = upHighs
        .filter(h => h.time >= dn.startTime && h.time <= (dn.coverageEnd ?? dn.endTime) + 1)
        .slice()
        .sort((a, b) => a.time - b.time);
      if (!highs.length) continue;
      const firstHigh = highs.find(h => h.price < dn.startPrice);
      if (!firstHigh) continue;
      points.push({ type: "2卖", time: firstHigh.time, price: firstHigh.price });
      const zs = pickZsForTwo(zss, dn.startTime, dn.endTime, firstHigh.time);
      if (!zs) continue;
      const zd = zs.zd, zg = zs.zg;
      twoSellMeta.push({
        time: firstHigh.time, price: firstHigh.price,
        zd, segStart: dn.startTime, segEnd: dn.coverageEnd ?? dn.endTime,
      });
      // 同一中枢内，2卖之后每一个更低次高都标类2卖。
      // 收敛后的 zg 是全部笔公共重叠，更早的类2卖可以高于这个 zg，只要上涨笔仍与中枢重叠。
      let prevPx = firstHigh.price;
      for (const h of highs) {
        if (h.time <= firstHigh.time || !(h.price < prevPx)) continue;
        const b = bis[h.biIdx];
        const blo = Math.min(b.startPrice, b.endPrice);
        if (h.price >= zd && blo <= (zg + c2tol)) {
          points.push({ type: "类2卖", time: h.time, price: h.price });
          prevPx = h.price;
        }
      }
    }
  } else {
    let structTopIdx = null;
    if (firstSell) {
      let maxP = -Infinity;
      for (const i of upIdx) {
        if (i >= firstSell.biIdx) break;
        if (bis[i].endPrice > maxP) { maxP = bis[i].endPrice; structTopIdx = i; }
      }
    }
    if (structTopIdx == null) {
      let maxP = -Infinity;
      for (const i of upIdx) {
        if (bis[i].endPrice > maxP) { maxP = bis[i].endPrice; structTopIdx = i; }
      }
    }
    if (structTopIdx != null) {
      const top = bis[structTopIdx];
      let secondSell = null;
      for (let i = structTopIdx + 1; i < bis.length; i++) {
        if (bis[i].type !== "up") continue;
        if (bis[i].endPrice < top.endPrice) {
          secondSell = { biIdx: i, time: bis[i].endTime, price: bis[i].endPrice };
          break;
        }
      }
      if (secondSell) {
        points.push({ type: "2卖", time: secondSell.time, price: secondSell.price });
        const zss = buildZS(bis, barSec);
        const zs = pickZsForTwo(zss, secondSell.time, secondSell.time, secondSell.time);
        if (zs) {
          const zd = zs.zd, zg = zs.zg;
          const t0 = zs.enterEndTime != null ? zs.enterEndTime : zs.startTime;
          const t1 = zs.exitTime != null ? zs.exitTime : zs.endTime;
          twoSellMeta.push({
            time: secondSell.time, price: secondSell.price,
            zd, segStart: t0, segEnd: t1,
          });
          let prevPx = secondSell.price;
          for (let i = secondSell.biIdx + 1; i < bis.length; i++) {
            if (bis[i].type !== "up") continue;
            const p = bis[i].endPrice;
            const blo = Math.min(bis[i].startPrice, bis[i].endPrice);
            if (p < prevPx && p >= zd && blo <= (zg + c2tol)) {
              points.push({ type: "类2卖", time: bis[i].endTime, price: p });
              prevPx = p;
            }
          }
        }
      }
    }
  }

  for (const as_ of anchoredSells) points.push({ type: "1卖", time: as_.time, price: as_.price });

  twoSellMeta.sort((a, b) => a.time - b.time);
  const allTwoSell = points.filter(p => p.type === '2卖').slice().sort((a, b) => a.time - b.time);
  const thirdOut = [];
  for (const ts of twoSellMeta) {
    let twoIdx = idxByEndTime[ts.time];
    if (twoIdx == null) twoIdx = -1;
    if (twoIdx < 0) continue;
    let endScan = bis.length;
    for (const nxt of allTwoSell) {
      if (nxt.time > ts.time) {
        endScan = idxByEndTime[nxt.time];
        if (endScan == null) endScan = bis.length;
        break;
      }
    }
    const zd = ts.zd;
    const valids = [];
    for (let i = twoIdx + 1; i < endScan; i++) {
      if (bis[i].type !== "down") continue;
      if (bis[i].endPrice >= zd) continue;
      for (let mm = i + 1; mm < endScan; mm++) {
        if (bis[mm].type !== "up") continue;
        const st = bis[mm].endTime, sp = bis[mm].endPrice;
        if (sp < zd + t3tol && st >= ts.segStart && st <= ts.segEnd + 1) {
          valids.push({ time: st, price: sp });
        }
        break;
      }
    }
    if (valids.length > 0) {
      valids.forEach((v, k) => {
        thirdOut.push({ type: leavePointType(k, true), time: v.time, price: v.price });
      });
    }
  }
  appendThirdPoints(points, thirdOut, LEAVE_SELL, "类2卖");
  points.sort((a, b) => a.time - b.time); // 时间升序稳定排序；同刻保持识别序（2/类2→1卖→3/4类）
  return points;
}


/** 低级别每类买卖点只保留时间上最近的一个（历史策略保留，现主流程已不调用） */
function keepRecentEach(points, keep = 1) {
  // keep：每类买卖点保留的个数（默认 1，即每类只保留时间上最近的一个）
  const n = Math.max(1, Math.floor(keep) || 1);
  const byType = {};
  for (const p of points) {
    if (!byType[p.type] || p.time > byType[p.type].time) byType[p.type] = p;
  }
  if (n === 1) {
    return Object.values(byType).sort((a, b) => a.time - b.time);
  }
  // keep > 1：每类按时间倒序取最近 n 个（保证保留的是时间上最新的一组）
  const groups = {};
  for (const p of points) {
    if (!groups[p.type]) groups[p.type] = [];
    groups[p.type].push(p);
  }
  const out = [];
  for (const key of Object.keys(groups)) {
    groups[key].sort((a, b) => b.time - a.time);
    out.push(...groups[key].slice(0, n));
  }
  return out.sort((a, b) => a.time - b.time);
}

/** 每周期买卖点不分类（买+卖合并），只保留时间上最近 keep 个（现行主流程保留策略） */
function keepRecentAll(points, keep = 10) {
  const n = Math.max(1, Math.floor(keep) || 1);
  return [...points].sort((a, b) => b.time - a.time).slice(0, n).sort((a, b) => a.time - b.time);
}

// ============================================================
// 导出
// ============================================================

/**
 * 由上级笔提取「锁定端点」列表（区间套强制对齐用）：
 * 上级笔的每个起点/终点都是一个明确的极值端点（上涨笔起点是底、终点是顶；下跌笔反之）。
 * 下级周期画笔时，把这些端点作为 lockedPivots 传入 buildBi，保证下级笔端点与上级对齐。
 */
function lockedPivotsOf(prevBis) {
  if (!prevBis || prevBis.length === 0) return null;
  const arr = [];
  for (const b of prevBis) {
    if (b.type === "up") {
      arr.push({ dir: "bottom", price: b.startPrice });
      arr.push({ dir: "top", price: b.endPrice });
    } else {
      arr.push({ dir: "top", price: b.startPrice });
      arr.push({ dir: "bottom", price: b.endPrice });
    }
  }
  return arr;
}

/**
 * 区间套强制对齐（优先级最高）：把下级周期笔的拐点对齐到上级周期笔的拐点。
 * 上级笔的每个起点/终点都是明确极值（顶/底），下级周期必须复现相同极值。
 * 当下级周期因包含关系把上级极值吞掉（如插针低点/高点）时，下级拐点会漂移到
 * 次极值上（例：上级底 4311.04 被下级画成 4311.27）；本函数把「同方向且时间最近」
 * 的下级拐点快照到上级拐点的（时间+价格），实现「上级笔与下级笔同笔」。
 *
 * @param {Array} lowerBis 下级周期笔（原地修改并返回）
 * @param {Array} upperBis 上级周期笔
 * @param {number} upperIntervalSec 上级周期K线间隔（秒），作为时间容差
 * @param {Array} lowerBars 本级别原始K线（可选）。幽灵端点防御用：若上级极值超出
 *   上级bar时间跨度内本级别K线的局部价格范围（跨周期数据源聚合差异），跳过对该拐点对齐。
 */
function alignBiToUpper(lowerBis, upperBis, upperIntervalSec, lowerBars) {
  if (!lowerBis || !upperBis || lowerBis.length === 0 || upperBis.length === 0) return lowerBis;
  const tol = upperIntervalSec || 0;

  // 幽灵端点防御（可选，lowerBars 未传时跳过，保持向后兼容）：
  // 上级极值可能只存在于上级聚合数据中（跨周期数据源差异，如日K聚合低点低于该日
  // 所有日内K线），此时在本级K线中无法复现该极值。若把本级拐点强行对齐到该价格，
  // 会画出本级数据中不存在的「幽灵端点」。
  // 判定：取上级拐点所在上级bar时间跨度 [up.time, up.time+tol) 内本级K线的局部价格范围，
  // 若上级极值超出该范围（底低于局部所有K线最低价 / 顶高于局部所有K线最高价），
  // 视为幽灵端点，跳过对该拐点的对齐（保留本级别真实极值）。
  // 注：不能只校验全局范围——本窗口其他时段可能有更极端的低点（如更早的插针），
  // 全局范围校验会漏判「本局部时段内不存在」的上级极值。
  const localPriceRange = (up) => {
    if (!lowerBars || lowerBars.length === 0) return null;
    let mn = null, mx = null;
    const tEnd = up.time + tol;
    for (const b of lowerBars) {
      if (b.time < up.time || b.time >= tEnd) continue;
      if (b.low !== undefined && (mn === null || b.low < mn)) mn = b.low;
      if (b.high !== undefined && (mx === null || b.high > mx)) mx = b.high;
    }
    return (mn !== null && mx !== null) ? { mn, mx } : null;
  };

  // 上级拐点：每笔的起点+终点
  const upperPts = [];
  for (const b of upperBis) {
    if (b.type === "up") {
      upperPts.push({ time: b.startTime, price: b.startPrice, dir: "bottom" });
      upperPts.push({ time: b.endTime, price: b.endPrice, dir: "top" });
    } else {
      upperPts.push({ time: b.startTime, price: b.startPrice, dir: "top" });
      upperPts.push({ time: b.endTime, price: b.endPrice, dir: "bottom" });
    }
  }

  // 下级拐点：n 笔 → n+1 个拐点（相邻两笔共享同一拐点）
  const n = lowerBis.length;
  const pts = new Array(n + 1);
  for (let i = 0; i <= n; i++) {
    if (i === 0) {
      const b = lowerBis[0];
      pts[i] = { time: b.startTime, price: b.startPrice, dir: b.type === "up" ? "bottom" : "top" };
    } else if (i === n) {
      const b = lowerBis[n - 1];
      pts[i] = { time: b.endTime, price: b.endPrice, dir: b.type === "up" ? "top" : "bottom" };
    } else {
      const b = lowerBis[i];
      pts[i] = { time: b.startTime, price: b.startPrice, dir: b.type === "up" ? "bottom" : "top" };
    }
  }

  // 每个上级拐点：找同方向、时间最近且未使用的下级拐点，快照对齐。
  const used = new Array(n + 1).fill(false);
  for (const up of upperPts) {
    let best = -1, bestDiff = Infinity;
    for (let i = 0; i <= n; i++) {
      if (used[i]) continue;
      if (pts[i].dir !== up.dir) continue;
      const diff = Math.abs(pts[i].time - up.time);
      if (diff <= tol && diff < bestDiff) { bestDiff = diff; best = i; }
    }
    if (best >= 0) {
      const p = pts[best];
      // 仅当下级拐点「更不极端」（漏掉上级真极值）时才对齐时间+价格；
      // 否则（下级已找到相同极值）只对齐价格保持严格相等，保留下级更精确的时间。
      const lessExtreme = up.dir === "bottom" ? p.price > up.price : p.price < up.price;
      // 幽灵端点防御：上级极值在本级数据中不存在（跨周期数据源聚合差异）→
      // 跳过对齐，保留本级别真实极值
      if (lessExtreme) {
        const r = localPriceRange(up);
        if (r) {
          const PRICE_TOL = 0.01; // 价格容差，仅防浮点误差
          if (up.dir === "bottom" && up.price < r.mn - PRICE_TOL) continue;
          if (up.dir === "top" && up.price > r.mx + PRICE_TOL) continue;
        }
      }
      used[best] = true;
      p.price = up.price;
      if (lessExtreme) p.time = up.time;
    }
  }

  // 由快照后的拐点重建笔端点
  for (let i = 0; i < n; i++) {
    lowerBis[i].startTime = pts[i].time;
    lowerBis[i].startPrice = pts[i].price;
    lowerBis[i].endTime = pts[i + 1].time;
    lowerBis[i].endPrice = pts[i + 1].price;
    lowerBis[i].span = Math.abs(lowerBis[i].endPrice - lowerBis[i].startPrice);
  }
  return lowerBis;
}

// Structure context mirrors py_chain/chan_core.py. Inputs are closed prefixes;
// prospective legs are separate from confirmed pivots and can contain child points.
function mergedSegmentCount(merged, startTime, barSec = 0) {
  if (!merged || !merged.length) return 0;
  let lo = 0, hi = merged.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (barSec ? merged[mid].time + barSec <= startTime : merged[mid].time < startTime) lo = mid + 1;
    else hi = mid;
  }
  if (lo === merged.length || startTime < (merged[lo]._firstTime ?? merged[lo].time)) return 0;
  return merged.length - lo;
}
function confirmedStructureBis(bis) { return (bis || []).filter(b => !b._forming); }
function pointEligibleBis(bis) { return (bis || []).filter(b => !b._forming || b.enough); }
// 原笔合并块数：起点到端点（含端点块），不含端点之后的反向K
function originMergedCount(merged, last, barSec) {
  const total = mergedSegmentCount(merged, last.startTime, barSec);
  const tail = mergedSegmentCount(merged, last.endTime, barSec);
  if (tail <= 0) return total;
  return Math.max(0, total - tail + 1);
}
// 下一级最近中枢：出中枢笔相对入中枢笔是否背驰（只用 isBiDiverge）。无下一级、无中枢或尚未离开 → 不背驰
function lowerExitEnterDiverge(lowerStroke, upperBi, cutoff) {
  if (!lowerStroke || !upperBi) return false;
  const lb = lowerStroke.bis || [], bars = lowerStroke.bars || [], barSec = lowerStroke.barSec || 0;
  if (lb.length < 3 || !bars.length) return false;
  const macd = calcMACD(bars);
  const seg = {...upperBi, endTime: Math.max(upperBi.endTime || 0, cutoff || 0), coverageEnd: cutoff};
  const zss = buildZSByUpper(lb, [seg], barSec, true);
  if (!zss.length) return false;
  const zs = zss[zss.length - 1];
  const enter = lb.find(b => b.endTime === zs.enterEndTime);
  if (!enter) return false;
  // 第一次离开之后若价格回到中枢，当下出中枢笔取更晚的同向离开
  const zd = zs.zd, zg = zs.zg, eps = 1e-8;
  const leaves = [];
  if (zd != null && zg != null) {
    for (const b of lb) {
      if (b.type !== enter.type) continue;
      if ((b.endTime || 0) <= (enter.endTime || 0)) continue;
      if (b.startTime > (cutoff || 0)) continue;
      const startIn = b.startPrice >= zd - eps && b.startPrice <= zg + eps;
      const endBreak = b.endPrice < zd - eps || b.endPrice > zg + eps;
      if (startIn && endBreak) leaves.push(b);
    }
  }
  let exitBi = leaves.length ? leaves[leaves.length - 1] : null;
  if (!exitBi && zs.exitTime != null) exitBi = lb.find(b => b.startTime === zs.exitStartTime) || null;
  if (!exitBi) return false;
  return !!isBiDiverge(exitBi, enter, macd);
}
// 收笔：反向够笔，或下一级出/入中枢背驰。不收笔：原方向已够笔、反向未够笔、且下一级不背驰。原方向未够笔则照常挂形成段。门槛 5。
function reverseStrokeCloses(last, merged, barSec, enoughCount, lowerStroke, cutoff) {
  if (enoughCount >= 5) return true;
  if (originMergedCount(merged, last, barSec) < 5) return true;
  return lowerExitEnterDiverge(lowerStroke, last, cutoff);
}
function lowerStrokePack(res, periodBis, barsByPeriod) {
  const lower = lowerResOf(res);
  if (!lower) return null;
  const bis = periodBis && periodBis[lower];
  const bars = barsByPeriod && barsByPeriod[lower];
  if (!bis || !bis.length || !bars || !bars.length) return null;
  return {bis, bars, barSec: intervalSecOf(lower)};
}
function buildStructureContext(bis, bars, barSec, tCut = null, merged = null, fractals = null, lowerStroke = null) {
  let raw = bars || [], known = confirmedStructureBis(bis);
  if (tCut != null && raw.length && raw[raw.length - 1].time + barSec > tCut) {
    raw = raw.filter(b => b.time + barSec <= tCut);
    merged = mergeBars(markWickBars(raw));
    fractals = findFractals(merged);
    const resOf = {180: "3", 900: "15", 3600: "60", 14400: "240", 86400: "D"}[barSec] || null;
    known = buildBi(fractals, merged, calcATR(raw), calcMACD(raw), null, nearDoubleOn(barSec), resOf);
    known = fixBiExtremes(known, merged) || known;
    known = extendLastBi(known, markWickBars(raw));
  }
  if (merged == null) merged = mergeBars(markWickBars(raw));
  const cutoff = tCut ?? (raw.length ? raw[raw.length - 1].time + barSec : 0);
  const result = {confirmedBis: known, bis: known.slice(), current: null, merged, cutoff};
  if (!known.length || !raw.length || !merged.length) return result;
  const last = known[known.length - 1];
  let current = {...last, phase: "confirmed", _contextReady: true, coverageEnd: cutoff,
    mergedCount: mergedSegmentCount(merged, last.startTime, barSec), enough: true};
  const count = mergedSegmentCount(merged, last.endTime, barSec), idx = count ? merged.length - count : -1;
  const fs = fractals ?? findFractals(merged), kind = last.type === "down" ? "bottom" : "top";
  const endpoint = fs.find(f => f.mergedIdx === idx && f.type === kind);
  const after = raw.filter(b => b.time + barSec > last.endTime);
  if (endpoint && after.length) {
    const broken = after.some(b => kind === "bottom" ? b.low < last.endPrice - 1e-8 : b.high > last.endPrice + 1e-8);
    const future = raw.filter(b => b.time > merged[idx].time);
    if (!broken && future.length) {
      const field = kind === "bottom" ? "high" : "low";
      const extreme = future.reduce((a,b) => (kind === "bottom" ? b[field] > a[field] : b[field] < a[field]) ? b : a);
      const price = extreme[field];
      if (kind === "bottom" ? price > last.endPrice : price < last.endPrice) {
        // C-2（pointEnoughForming）：够笔计数只到极值块——反向确认（首根抬低/抬高点K）后不计入
        let enoughCount = count;
        if (CHAN_CFG.pointEnoughForming) {
          let iExt = 0;
          for (let i = 0; i < merged.length; i++) if (merged[i].time <= extreme.time) iExt = i;
          enoughCount = Math.max(0, iExt - idx + 1);
        }
        const forming = {type: kind === "bottom" ? "up" : "down", startTime: last.endTime,
          startPrice: last.endPrice, endTime: extreme.time, endPrice: price, span: Math.abs(price-last.endPrice),
          _forming: true, _contextReady: true, mergedCount: count, enough: enoughCount >= 5,
          phase: enoughCount >= 5 ? "running" : "expected", coverageEnd: cutoff};
        // 收笔才挂反向形成段；不收笔时 current 仍是原笔，覆盖延续到 cutoff
        if (reverseStrokeCloses(last, merged, barSec, enoughCount, lowerStroke, cutoff)) {
          current = forming;
          result.bis.push(current);
        }
      }
    }
  }
  if (!current._forming) result.bis[result.bis.length - 1] = current;
  result.current = current;
  return result;
}
function structurePeriods(periodBis, barsByPeriod, tCut = null) {
  if (tCut == null) tCut = Math.max(0, ...Object.entries(barsByPeriod).filter(([,v])=>v.length).map(([r,v])=>v[v.length-1].time+intervalSecOf(r)));
  // 从小周期到大周期：上一级收笔要看下一级已经建好的笔。返回键顺序与输入一致。
  const computed = {};
  const order = Object.keys(periodBis).sort((a, b) => (intervalSecOf(a) || 0) - (intervalSecOf(b) || 0));
  for (const r of order) {
    const bis = periodBis[r] || [];
    if (bis.length && bis[bis.length - 1]._contextReady) { computed[r] = bis; continue; }
    const ctx = buildStructureContext(bis, barsByPeriod[r] || [], intervalSecOf(r), tCut, null, null,
      lowerStrokePack(r, computed, barsByPeriod));
    const view = ctx.bis.slice();
    if (view.length && ctx.current) view[view.length - 1] = {...view[view.length - 1], coverageEnd: tCut};
    computed[r] = view;
  }
  const out = {};
  for (const r of Object.keys(periodBis)) if (computed[r]) out[r] = computed[r];
  return out;
}

module.exports = {
  mergedSegmentCount, confirmedStructureBis, pointEligibleBis, buildStructureContext, structurePeriods,
  lowerStrokePack, lowerExitEnterDiverge,
  CHAN_CFG,
  // K线/分型/笔
  markWickBars,
  mergeBars,
  mergeStep,
  findFractals,
  countRaw,
  hasGapBetween,
  buildBi,
  fixBiExtremes,
  buildZS,
  buildZSByUpper,
  lockedPivotsOf,
  alignBiToUpper,
  calcATR,
  calcMACD,
  hasMacdCrossBetween,
  extendLastBi,
  lowerResOf,
  calibrateBiTimes,
  intervalSecOf,
  nearDoubleOn,
  wideBarPointsOf,
  // MACD 背驰
  fmtT,
  biMacdMetrics,
  isBiDiverge,
  // 买卖点
  findBuyPoints,
  findSellPoints,
  anchorFirstBuy,
  anchorFirstSell,
  isSameAsUpperBi,
  snapToOwnBar,
  keepRecentEach,
  keepRecentAll,
};
