#!/usr/bin/env node
/**
 * 对拍工具：用 chan_core.js（图表算法源，只读复用）从 JSON {res:[bars]} 重建各周期笔序列，
 * 输出 {res:[bis]} 到 stdout。供 py_chain/align_check.py 与 py 侧逐笔 diff。
 *
 * 用途：SPEC_divergence_chanset.md 块1 回归——py↔JS 笔结构对齐，目标 0 差异。
 * 管线与 py_chain.backtest.build_bis 同口径（不含图表管线专属步骤 lockedPivots/
 * alignBiToUpper/ATR过滤/校准——那些依赖运行时图表状态，不在模块级对拍范围）：
 *   markWickBars → mergeBars → findFractals → buildBi(…, null, sec>=3600, lowerContext)
 *   → fixBiExtremes → extendLastBi(trimmed)
 * ATR/MACD 用原始K线（与 chan-bi 一致）。60m上下文来自同份输入的15m已收盘前缀。
 *
 * 用法：node rebuild_bis.js <bars.json> [res1,res2,...]
 */
const fs = require("fs");
const core = require("./chan_core.js");

const file = process.argv[2];
if (!file) {
  console.error("用法: node rebuild_bis.js <bars.json> [res1,res2,...]");
  process.exit(2);
}
const only = process.argv[3] ? process.argv[3].split(",").map((s) => s.trim()) : null;
const data = JSON.parse(fs.readFileSync(file, "utf8"));
const out = {};
for (const res of Object.keys(data)) {
  if (only && !only.includes(res)) continue;
  const bars = data[res];
  if (!Array.isArray(bars) || bars.length < 6) continue;
  const trimmed = core.markWickBars(bars);
  const merged = core.mergeBars(trimmed);
  const fractals = core.findFractals(merged);
  const atr = core.calcATR(bars, 14);
  const macd = core.calcMACD(bars);
  let bis = core.buildBi(fractals, merged, atr, macd, null, core.intervalSecOf(res) >= 3600, core.makeBiLowerContext(res, data['15']));
  bis = core.fixBiExtremes(bis, merged) || bis;
  bis = core.extendLastBi(bis, trimmed);
  out[res] = bis;
}
process.stdout.write(JSON.stringify(out));
