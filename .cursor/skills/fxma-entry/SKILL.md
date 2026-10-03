---
name: fxma-entry
description: Mark Strong-Fractal-Moving-Average V1 (强分型均线V1, fxma_v1) entry/exit signals on the TradingView Desktop chart via CDP. The second strategy running in parallel with 缠论V1. Signal = selected-class Chan buy/sell points (1买/1卖→class 1, 2买/类2买 & sell mirror→class 2, 3买/类3买→class 3, class 4 excluded) on each selected entry timeframe (30S/3m/15m/1h, multi-select) + strong fractal after the point (entity-only: bottom = right-shoulder close above left-shoulder open, top mirrored, gap ≥ strongFxMinPts) + MA separation at close (class 1 uses maFast1/maSlow1, classes 2/3 use maFast2/maSlow2; fast above slow ≥ crossMinPts for buys / below for sells) + close standing on the stand MA (class 1 uses maStand1, classes 2/3 use maStand2; buy close strictly above / sell strictly below, maStandOn toggles) + optional fib-near gate (fibNearOn, default off; classes 2/3 only: point price within fibNearPts absolute points of any fibLevels retracement of the swing from the previous same-side point price to the real-bar extreme before this point) + optional upper-timeframe alignment (upperDirOn, default off; the current upper bi of 30S→3→15→60→240 must point the same way as the signal). Fill at next bar open of the same timeframe; stop = entry ∓ stopPts / take-profit = entry ± tpPts (absolute points, intrabar touch fills at the trigger price, same-bar double-touch per sameBarPriority). Mutual exclusion global or per-period (mutexScope). Long = red up arrow, short = green down arrow; exits = yellow arrows titled EXIT_FX_<timeframe>. Requires chan-bi stroke data (bis_<symbol>.json); 30S strokes need --with-30s on the bi stage.
disable-model-invocation: true
---

# 强分型均线V1 · 进出场标记（fxma-entry）

「强分型均线V1」（fxma_v1）的工作台侧实现——与缠论V1并行的第二个交易策略。
通过 CDP 连接 TradingView Desktop，读取画笔落盘的笔数据，在所选进出场周期上按策略规则
回放评估并标记进出场：

- **买点（多头）** → 向上**红色**箭头（`arrow_up`，title `ENTRY_FX_<周期>`）
- **卖点（空头）** → 向下**绿色**箭头（`arrow_down`）
- **出场**（止损/止盈终局）→ **黄色**箭头（多头出场 ↓ / 空头出场 ↑，title `EXIT_FX_<周期>`）

> **模块分类**：交易策略 · **强分型均线V1**（注册表 id `fxma_v1`，引擎标识 `fx_ma`；
> 分层与接入清单见 `.cursor/skills/README.md`）。
>
> **算法来源**：买卖点识别复用 `chan-core` 的 `findBuyPoints/findSellPoints`（唯一算法源）；
> 强分型为实体口径（与 `trading_plan.strong_fractal_after` 同式）；均线为 SMA/EMA 收盘价。
> Python 侧引擎 = `py_chain/fx_ma.py`（回测/回放/监控/MT5 实盘共用）。
>
> **强制依赖**：画笔（chan-bi）落盘的笔数据 `.cursor/cache/bis_<品种>.json`
> （选 30S 周期时画笔须开 30 秒级别）。缺失或品种不匹配会**报错退出**。
>
> **运行依赖链**：画笔（chan-bi）→ 标记买卖点（mark-buy-sell，可选参考）→ 本脚本。

## 信号规则（与引擎同一套，参数默认=参数中心 fxma 模块）

1. **买卖点**：进出场周期 P 上最新买卖点属所选类别（`pointClasses`）——
   1买/1卖→1类、2买/类2买（卖侧对称）→2类、3买/类3买→3类，**4类不交易**；
   反向点出现后该点失效；
2. **强分型**：点之后出现强分型（`strongFxMinPts` 实体最小落差，0=现口径）；
3. **均线分离**：按类别选均线对——1类 `maFast1/maSlow1`（默认 8/20）、2/3类
   `maFast2/maSlow2`（默认 5/8）；当拍收盘 快线高于慢线≥`crossMinPts`（买）/
   低于慢线≥`crossMinPts`（卖）；
4. **收盘站线**：按类别选站线均线周期——1类 `maStand1`、2/3类 `maStand2`
   （默认均 5，SMA/EMA 同 `maType`）；当拍收盘价 买点须严格站上该均线、
   卖点须严格站下（对称）；`maStandOn`=关 跳过本条件；
5. **黄金分割附近**（`fibNearOn`，**默认关**；仅 2/3 类点，1 类点豁免）：摆动段 =
   前一同侧买卖点价格 → 其后至本点前（时间窗 (前点, 本点]）的 P 周期真实K线极值
   （买取最高价/卖取最低价），按 `fibLevels`（默认 0.382,0.5,0.618）算回撤位
   （买 H−r×(H−L) / 卖 L+r×(H−L)），本点价格须落在任一档位 ±`fibNearPts`
   （绝对点数，默认 5）内；无前一同侧点或摆动段退化（≤0）不触发；
6. **上级周期同向**（`upperDirOn`，**默认关**；全部类别）：上级周期
   （30S→3→15→60→240）当前笔方向须与信号同向（买=上涨笔/卖=下跌笔）；
   上级无笔不触发；
7. **有效期**：`pointValidBars` 根内齐备（0=不限，超时作废）；`pointValidPts`
   盘中价距点极值上限（买=评估根最高价−买点最低价、卖=卖点最高价−评估根最低价；
   0=不限）——超距只跳过该拍、点保持存活（等待语义），价格回到范围内仍可触发；
   每个买卖点只触发一次。

## 成交与出场

- 成交 = 触发拍下一根 P 周期K线开盘价；
- 止损 = 进场价 ∓ `stopPts`（默认10点）、止盈 = 进场价 ± `tpPts`（默认30点，绝对价差）；
- 出场 = 盘中触价即按触发价成交（与实盘 MT5 SL/TP 同口径：不等收盘确认、不等下一开盘，
  进场那根收盘后即判）；同根双触按 `sameBarPriority`（默认止损优先）；
  期末未触发按最新收盘 mark-to-market；
- 互斥：`mutexScope` = global（同向全局一笔，同缠论V1）/ perPeriod（每周期独立）；
  同拍多周期同向共振取最大周期一条。

## 用法

```bash
node .cursor/skills/fxma-entry/scripts/fxma_entry.js --from=2026-08-01 \
  --entry-res=3,15,60 --point-classes=1,2,3 --ma-type=SMA \
  --ma-fast-1=8 --ma-slow-1=20 --ma-fast-2=5 --ma-slow-2=8 \
  --cross-min-pts=2 --ma-stand-on=1 --ma-stand-1=5 --ma-stand-2=5 \
  --fib-near-on=0 --fib-levels=0.382,0.5,0.618 --fib-near-pts=5 \
  --upper-dir-on=0 --strong-fx-min-pts=0 --point-valid-bars=0 --point-valid-pts=0 \
  --stop-pts=10 --tp-pts=30 --same-bar-priority=stop --mutex-scope=global --lots=4
```

WEB 分析工作台按「参数配置 → 交易策略 · 强分型均线V1」的品种桶自动附加全部参数；
结果落盘 `.cursor/cache/fxma_<品种>.json`（`--dry` 只算不画）。

## 与回测/监控引擎的口径差（同 mark-entry 的「工作台 vs 引擎」既有差异）

本脚本用画笔落盘的**最终笔快照** + 全窗口K线（事后视角）回放；引擎（`py_chain/fx_ma.py`）
用当下增量状态逐拍评估（无未来函数）。两者信号可能不完全一致，属研究口径差
（SPEC.md §2.1.5.2），不能混用。30S 周期仅回测/工作台可用（MT5 实盘行情无法生成 30S）。
