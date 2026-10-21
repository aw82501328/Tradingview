---
name: mark-entry
description: Mark entry signals (进出场) on the TradingView Desktop chart via CDP. Since 2026-10-16 consumes chan-core buy/sell points directly (same source as mark-buy-sell marks; no longer reads plan_<symbol>.json) — each detection timeframe takes the latest buy-side and sell-side anchors on recent stroke endpoints, maps point type + strong flag to one of 12 entry strategies via pointStrategyOf (1买→wait1Buy direct entry, 2买/类2买 strong→wait2Buy / weak→wait2BuyBear, 3买→wait3Buy, 类3/4族→waitBuy; sell side mirrored), validates that strategy's entry conditions (enough strokes / breaking previous low-high / MACD zero axis / weaker momentum leaving ZhongShu / lower-timeframe divergence / near S/R level), and marks buy/sell arrows on the divergence timeframe. Long = up red arrow, short = down green arrow. Optional 震荡 gate (--range-res, off by default, from trading-plan computeRegime) and 顺势 filter (--trend-res, off by default). Also simulates exits per position (stop = intrabar break of the direction-aware S/R reference; TP1 breakeven after divergence-TF stroke completes; TP2 half-close after detection-TF stroke completes; TP3 full close on detection-TF breaking prior low/high) with same-direction mutual exclusion, marking exits as yellow arrows (title EXIT_<divergence TF>).
disable-model-invocation: true
---

# 进出场标记

通过 CDP 连接 TradingView Desktop，**直接用 chan-core 计算各周期买卖点**（`findBuyPoints`/`findSellPoints`，与图上 mark-buy-sell 标记同源；2026-10-16 起不再读取交易计划落盘文件），买卖双侧各自取**最近笔端点锚**，按点类型+强弱直映射到 **12 种进场策略**（`pointStrategyOf`），校验该策略的进场条件后，在「背驰级别」（更低周期）标记进场箭头：

- **买点（多头）** → 向上**红色**箭头（`arrow_up`）
- **卖点（空头）** → 向下**绿色**箭头（`arrow_down`）

并对每个进场模拟**出场**（止损 + 三档止盈），在图上以**统一黄色箭头**标记出场点（多头出场 ↓ / 空头出场 ↑，与 Web 控制台一致）：

- **终局出场**（支阻位止损 / 保本止损 / 全平）→ 黄 `↓/↑`
- **平一半** → 黄 `↓/↑`（保本/仍持仓仅落盘，不画图）

> **模块分类**：交易策略 · **缠论V1**（进出场直接消费买卖点；分层与新增策略接入见 `.cursor/skills/README.md`）。
>
> **算法来源**：买卖点识别复用 `chan-core` 的 `findBuyPoints`/`findSellPoints`（唯一算法源，与 mark-buy-sell 同源），背驰判定复用 `isBiDiverge`，中枢复用 `buildZSByUpper`；**交易计划文案中转已取消**（2026-10-16）——买卖点 → 进场键在本脚本 `pointStrategyOf` 直映射；震荡 regime（`computeRegime`）与顺势方向（`trendStateOf`）复用 `trading-plan` 模块导出（默认均关闭）。
>
> **强制依赖两个前置数据**：
> 1. **画笔**（chan-bi）落盘的笔数据 `.cursor/cache/bis_<品种>.json`；
> 2. **支阻互换位**（mark-sr-flip）落盘的支阻位数据 `.cursor/cache/srflip_<品种>.json`（读取其 `merged` 全量候选池支阻位）。
>
> 任一数据文件缺失、或品种不匹配，脚本会**报错退出**。**运行依赖链**（依序执行）：**画笔（chan-bi）→ 支阻互换位（mark-sr-flip）→ 本脚本**（2026-10-16 起不再依赖 trading-plan）。

## 前置条件

1. TradingView Desktop 以调试模式启动：`TradingView.exe --remote-debugging-port=9222`
2. 已打开至少一张图表，且图表停留在**要标记的品种**上（脚本自动读取当前品种和周期）
3. chrome-remote-interface 已安装（在 `server-cdp/node_modules/`）
4. **已运行「画笔」**（chan-bi），生成笔数据文件
5. **已运行「支阻互换位」**（mark-sr-flip），生成支阻位数据文件

## 使用方式

```bash
# 在图表上标记进出场（默认 4小时/1小时/15分钟/3分钟）
node .cursor/skills/mark-entry/scripts/mark_entry.js --from=2026-06-30

# 只计算并打印，不绘图（先验证再标）
node .cursor/skills/mark-entry/scripts/mark_entry.js --dry --from=2026-06-30

# 调整靠近支阻位阈值（×状态所在周期ATR）
node .cursor/skills/mark-entry/scripts/mark_entry.js --from=2026-06-30 --near=10
```

> `--from` 起始日期应与画笔/支阻位一致。
> 小周期数据窗口与画笔同源：参数中心「基础组件 → 画笔」的「小周期绘制窗口」（`windowDays3/15/30S`），
> 手动调用时若参数中心有自定义值应加 `--chan-cfg=` 透传（导出方式见 chan-bi SKILL）。

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--from=YYYY-MM-DD` | **必填**：起始日期，与画笔/支阻位一致 | 无（缺少时报错退出） |
| `--periods=...` | 检测周期列表（逗号分隔，从大到小） | `240,60,15,3` |
| `--with-30s` | 启用 30 秒级别：ALL_RES 追加 30S（需先用 `chan-bi --with-30s` 落盘 30S 笔数据），3 分钟状态可用 30S 背驰产生进场信号，箭头画在 30S 级别；30S 数据只取最近 3 天。检测周期列表本身**不变**（30S 无更低级别，不作检测周期） | 关闭 |
| `--near=K` | 靠近支阻位阈值（**绝对价差**，2026-09-13 起不乘 ATR） | `10` |
| `--lots=N` | 每笔进场手数（仅落盘记录；盈亏口径 = 价格差×方向×手数，JS 端不算盈亏） | `4` |
| `--slip-stop=K` | 止损位滑点（绝对价格：正确侧支阻位外侧偏移，short +/long −） | `3` |
| `--slip-fallback=K` | 兜底止损滑点（无正确侧支阻位 → 止损 = 进场价±该值；止损位永不为 null） | `10` |
| `--slip-be=K` | 保本滑点（beStop = 进场K线极值±该值，short: high+/long: low−） | `3` |
| `--slip-stop-atr-k=K` | 止损滑点ATR系数：有效滑点 = 固定值 + K×ATR(14,背驰周期 markRes)；0=关闭。WEB 参数中心透传 | `0` |
| `--slip-fallback-atr-k=K` | 兜底止损滑点ATR系数（同上口径） | `0` |
| `--slip-be-atr-k=K` | 保本滑点ATR系数（同上口径） | `0` |
| `--exit-min-merged=N` | 出场成笔预期门槛（形成段合并后 ≥N 根K；WEB 参数中心「进出场」模块同名透传） | `5` |
| `--zs-weak-ratio=K` | 出中枢力度衰减比例（离开笔幅度 < 进入笔×K 视为变弱；WEB 参数中心同名透传） | `1.0` |
| `--chan-cfg=<json>` | 缠论核心 CHAN_CFG 整体覆盖（WEB 参数中心透传，未知键闲置无害） | 不覆盖 |
| `--dry` | 只计算不绘图 | 关闭 |
| `--trend-res=R` | 顺势参考周期：`240`=4小时 / `D`=日线 / `off`=关闭（默认）。参考周期及以上不作检测周期；其方向与信号方向相反时跳过（规则见 trading-plan SPEC §5 与 WEB 参数页「交易计划」页签） | `off` |
| `--range-res=R` | 震荡判定参考周期：`240`/`D`/`off`（默认）。参考周期及以上不作检测周期（只作锚）；参考周期 regime 震荡/笔不足 → 更低周期观望（trading-plan `computeRegime`，与引擎震荡闸同口径） | `off` |
| `--debug` | 打印背驰、条件判定等调试信息 | 关闭 |

## 顺势参考周期过滤（--trend-res，默认关闭）

低周期开单跟随参考周期方向（`--trend-res` 可配 240/D / WEB 参数中心 plan 模块 `trendRes`；默认关闭）：

- **方向判定**归属 trading-plan 模块（`trendStateOf`），规则：最近买卖点定方向——1买/1卖须端点后出现**强分型**（右肩收盘穿左肩开盘，只比实体、不含影线）才确立；**非1类点出现即确立**；确立后**收盘价**破该点端点价 → 反向延续并**闩锁**到下一个买卖点；无点/强分型未现 → 回退最近一笔方向。详见 trading-plan SKILL 的 SPEC §5。
- **参考周期及以上不再作为检测周期**（只作方向锚，结构性剔除）；`--trend-res=D` 时 240 恢复检测。
- 命中信号附 `trendDirection`/`trendReason`（如 `多（4小时2买）`），回测信号列表方向列同口径显示。
- `--trend-res=off`（默认）时不过滤、参考周期照常检测。

## 震荡参考周期闸（--range-res，默认关闭）

参考周期（240/D）判震荡（trading-plan `computeRegime`：横盘窄带 A 支 / 未离开中枢且价在箱内 B 支）→ 更低检测周期全部观望；参考周期笔 <2 同样观望；参考周期及以上只作锚不作检测周期。默认关闭 = 无震荡闸，各周期直接走买卖点。与回测/实盘引擎同口径。

## 起始日期规则（每次标记必做，且必须输入日期）

**用户说「标记进出场」时，必须先询问「起始日期」（格式 `YYYY-MM-DD`，与画笔/支阻位一致），得到日期后再执行标记。不得在未获得日期的情况下直接运行脚本。**

```
① 询问：本次标记进出场从哪个日期开始？（格式 YYYY-MM-DD，与画笔/支阻位一致）
② 用户给出日期 → 运行 node mark_entry.js --from=YYYY-MM-DD
③ 用户未给出明确日期 → 再次询问，直到获得日期后才执行
```

## 买卖点 → 12 种进场策略直映射（2026-10-16 直连；pointStrategyOf）

对每个检测周期 X（240/60/15/3），买卖双侧各自取**最近笔端点锚**（从最后一笔终点逐笔向前扫描第一个落在笔端点上的买族/卖族点，容差：时间 1 根K / 价格 max(ATR×0.2, 0.05)），按点类型+强弱直映射进场策略（**不再经交易计划文案中转**）：

| 最近买卖点锚 | 进场策略（key） | 方向 | 箭头 |
|------|------|------|------|
| 1买（形成中与已确认统一，直接档） | 等待一买（wait1Buy） | 多头 | 向上红箭头 |
| 1卖（形成中与已确认统一，直接档） | 等待一卖（wait1Sell） | 空头 | 向下绿箭头 |
| 2买 / 类2买 + strong=true | 等待回调后做2买（wait2Buy） | 多头 | 向上红箭头 |
| 2买 / 类2买 + strong=false/null（未定型弱先行） | 等待低点附近的2买（wait2BuyBear，抬低回调） | 多头 | 向上红箭头 |
| 2卖 / 类2卖 + strong=true | 等待反弹后做2卖（wait2Sell） | 空头 | 向下绿箭头 |
| 2卖 / 类2卖 + strong=false/null | 等待高点附近的2卖（wait2SellBear，压低反弹） | 空头 | 向下绿箭头 |
| 3买 | 等待回调后3买点（wait3Buy） | 多头 | 向上红箭头 |
| 3卖 | 等待反弹后3卖点（wait3Sell） | 空头 | 向下绿箭头 |
| 类3买 / 4买 / 类4买 | 等待回调后买点（waitBuy，新买点） | 多头 | 向上红箭头 |
| 类3卖 / 4卖 / 类4卖 | 等待反弹后卖点（waitSell，新卖点） | 空头 | 向下绿箭头 |
| 最近笔端点均无买卖点锚 | 不触发（双侧无锚） | — | — |

> 2026-10-16 直连变更：①交易计划三档路由/未定型滚动翻档/跨侧兜底（如弱2买转等一卖）已删除，按点类型**同名直映射**；②1类点统一走直接档 wait1Buy/wait1Sell（此前已收笔1买→等回调做2买、仅形成中走直接档的口径废除）；③waitLike2Buy/waitLike2Sell 键保留但无映射入口（eval 比对兼容）。
> 买卖双侧并行独立评估，多空可同时持仓（2026-10-10 口径保留）。二买族强弱=点上的 `strong` 字段（首段上涨/下跌未走完按弱先行，过前高后滚动翻强——chan-core 规则，两侧一致）。

## 12 种进场策略的条件（全部需同时满足）

**公共条件**（所有策略）：

1. **够笔**：最后一笔方向符合预期（空头→最后一笔为 up「反弹够笔」、多头→最后一笔为 down「回调够笔」）；
2. **以下级别出现背驰**：在所有更低周期（如 60 的更低级别为 15/3）中，存在方向匹配的背驰点（做多→底背驰、做空→顶背驰），取**时间最新**的背驰点；
3. **锚点窗口（2026-10-18 用户规则、2026-10-21 重定义参数化）**：大周期某根K线满足买卖点条件（锚点所在K线）→ 去小周期找背驰限**锚K起 N 根检测周期K内**（`anchorWinBars` 默认 2、旧口径=1）——背驰点与触发拍所在K都须落窗（拒绝闸 `anchor_bar_window`），且触发价距**窗口极值** ≤ `anchorWinPts` 点（0=不限；极值=锚点价与窗口内实际极值取小/大，买侧最低/卖侧最高）。**无永久闩锁**：锚随新高/新低漂移或新点形成时，每拍按当前锚现算——新锚所在K线自动重开窗（旧锚窗口失败后价格涨过/跌破、重新满足卖点/买点条件时仍会再次触发）；
4. **在支阻位附近**：背驰点价与任一支阻位（`srflip.merged`）价差 ≤ `--near`（绝对价差，默认 10，不乘 ATR）。

**各策略专属条件**：

| 策略 | 专属条件 |
|------|------|
| 等待反弹后做2卖 | MACD 下0轴后反弹不过0轴（当前 DIF<0）+ 下跌段破前底 |
| 等待回调后做2买 | MACD 上0轴后回调不破0轴（当前 DIF>0）+ 上涨段过前高 |
| 等待一卖 | 够笔且过高点（最后一笔 up 且突破前高）+ 出中枢的力度变弱 |
| 等待一买 | 够笔且过低点（最后一笔 down 且跌破前低）+ 出中枢的力度变弱 |
| 等待低点附近的2买 / 等待高点附近的2卖 | 抬低/压低（末笔不破/不过前一同向笔终点） |
| 等待回调后买点 / 等待反弹后卖点 / 等待回调后3买点 / 等待反弹后3卖点 | （仅公共条件） |

**关键量定义**：

- **够笔**：最后一笔 type 为预期方向（空头→up、多头→down），即该反向运动已形成完整笔（chan-bi 已按 0.5×ATR 过滤，全部为有效笔）。
- **破前底/过前高**：最近完成的下/上涨笔终点跌破/突破更早同向笔终点（参照 `findDivergePoints` 的「创新低/创新高」思路，跳过幅度 < 当前笔 50% 的次级别笔）。
- **出中枢力度变弱**（`buildZSByUpper` 取最后一个中枢）：离开中枢的笔相对进入中枢的笔 **MACD 背驰**（`isBiDiverge`）**或** 离开笔幅度 `span` 小于进入笔 `span`。期望的离开方向：等待一卖→向上离开中枢（离开笔为 up）、等待一买→向下离开中枢（离开笔为 down）。
- **MACD 0轴**：`calcMACD` 返回的 `dif`，当前值 <0（下0轴）/>0（上0轴）。
- **以下级别**：所有 `intervalSecOf` 更小的周期；多个更低周期同时背驰时取时间最新者。

## 出场规则与同向持仓互斥

**出场阶梯**：持仓后每根 K 线收盘判定一次，**同一根 K 线内按固定顺序只看第一个命中的事件**：

> **保本 → 平一半 → 全平 → 止损**

每个进场信号从进场时刻起按时间顺序模拟出场（`simulatePosition`，纯函数；与 py_chain 引擎同口径）。成交型出场统一在**触发 K 线的下一根开盘价**成交；触发 K 线若已是最后一根则未成交（`state:'open'`）。

| 顺序 | 事件 | 触发条件 | 动作 |
|------|------|----------|------|
| 1 | **保本 breakeven** | 背驰周期**够笔**：进场后首笔有利方向笔（short→down / long→up）完成 | 止损位上移至**保本止损位 beStop**（仅落盘，不画图） |
| 2 | **平一半 half** | 检测周期首个有利方向、**合并后 ≥5 根K且有成笔预期**的形成段 | 平一半（下一开盘成交），剩余半仓止损移至 beStop，黄 `↓/↑` |
| 3 | **全平 close** | 检测周期**有利方向笔破前高/前低**（确认口径）；或当根收盘K极值破前一同向笔端点（预期口径，2026-10-10 起不分顺势逆势） | 全平终局（下一开盘成交），黄 `↓/↑` |
| 4 | **止损 stopSr** | 背驰级别K线盘中破坏止损位（short `high>` 位 / long `low<` 位；跳空按开盘价成交） | 全平终局，黄 `↓/↑` |
| — | **保本止损 stopBe** | 保本触发后盘中破坏 **beStop**（跳空按开盘价成交） | 全平终局，黄 `↓/↑` |

- **顺势 / 逆势**（出场阶梯分支用，2026-10-16 起按 strategyKey 判定）：**顺势** = wait2Buy/waitBuy/wait3Buy 及卖侧镜像 wait2Sell/waitSell/wait3Sell；**逆势** = wait1Buy/wait1Sell（一买卖直接档）与 wait2BuyBear/wait2SellBear（弱二买卖）。原「计划方向 ∈ {多头多, 空头空}」口径随计划策略行取消。
- **止损位**（`stopRefOf`，方向感知）：short 取进场价**上方**最近支阻位 + 有效止损滑点、long 取**下方**最近 − 有效止损滑点；信号自带 `nearSr` 已在正确侧则直接沿用（± 有效滑点）。**无正确侧位 → 兜底 = 进场价 ± 有效兜底滑点**（止损位永不为 null，不再有「不设止损」仓位）。**有效滑点 = 固定值 + ATR系数 × ATR(14, 背驰周期 markRes)**（系数默认 0=关闭，WEB 参数中心各滑点行内配置）。
- **保本止损位 beStop** = 进场K线极值 ± 有效保本滑点（short: high+ / long: low−；ATR 分量同上）。
- **合并后 ≥5 根K成笔预期**：检测周期形成段自段起点合并块起 ≥5 块（chan_core `isValid` gap≥4 同口径），触发 bar = 计数首次达 5 的已收盘K线、成交 = 下一开盘。已知近似差异：形成段达 5 后被最终笔结构吸收时，py 引擎当下已触发、JS hindsight 不触发。
- **同拍顺序**：保本 → 平一半 → 全平 → 止损（同拍只挂一个成交型事件）。
- **结算**：手数 `--lots` 默认 4（JS 端仅落盘，不算盈亏）；盈亏 =（0.5 × 半平价 + 0.5 × 终局价 − 进场价）× 方向 × 手数，未触发半平则 =（终局价 − 进场价）× 方向 × 手数。
- **同向持仓互斥**：同方向持仓未终局（未止损/未全平）时，新的同方向信号**被过滤**（落盘保留 `suppressed` 标记，不画箭头）；**多空双向互不影响**（各自独立状态机，可同时持有多、空仓）。平仓后（信号时间晚于终局时间）可再进场。
- **同时刻同向共振信号**（多个检测周期命中同一背驰点）：按检测周期**从大到小**取一条（D>240>60>15>3>30S），其余标 `suppressedBy`。
- 已知口径：笔数据最后一笔为延伸中的形成笔（chan-bi 落盘口径），其完成事件按延伸端点时间计。
- **与 py 引擎的口径差异**：本技能读最终笔快照（全 hindsight）、只有确认制；py 引擎用当下快照（无未来窥视）、默认当下制。**完整七条逐项差异表见 `py_chain/SPEC.md` §2.1.5.2**——两者不能混用。

## 标记位置（背驰级别）

- 箭头**画在背驰点所在周期**（「背驰级别」，如 60 周期状态命中、背驰出现在 15 周期 → 箭头标记在 15 周期）；
- title 打上背驰级别标签 `ENTRY_<背驰级别>`，再次标记时按标签只清除该级别旧箭头；
- 出场标记 title 为 `EXIT_<背驰级别>`（同一标记级别），清除时同样按标签清理；
- **只在该周期显示**：箭头与出场标记通过 `intervalsVisibilities` 只在本周期显示（切到其他周期自动隐藏）；
- 箭头创建在「低一级」周期上精确定位（与买卖点一致，3 分钟最稳定）；默认 3 分钟为最小周期，无更低级别背驰可供检测，因此其状态**不会**产生进场信号。`--with-30s` 启用后 30 秒为最小周期：3 分钟状态可检测 **30S 背驰**并产生进场信号（箭头画在 30S 级别）；30S 自身无更低级别，不作为检测周期。

## 落盘

每次标记（含 `--dry`）都会把各周期信号写入 **`.cursor/cache/entry_<品种>.json`**：
顶层含 `nearAtr`、`lots`（手数）、`slipStop`/`slipFallback`/`slipBe`（三个滑点参数，供复现）及 `slipStopAtrK`/`slipFallbackAtrK`/`slipBeAtrK`（滑点 ATR 系数）；
`periods` 按**背驰级别**聚合，字段：`periodX` 状态所在周期、`time` 背驰时间、`price` 背驰点价格、`direction`（`long` 做多 / `short` 做空）、`strategyKey`（策略标识）、`nearSr` 靠近的支阻位价格、`pointType`/`pointStrong`（进场锚买卖点类型与2族强弱，2026-10-16 直连后取代 planDirection）、`color` 箭头颜色；
出场相关新增字段：`stopRef` 止损位（支阻位±滑点或兜底，永不为 null）、`beStop` 保本止损位（进场K线极值±保本滑点）、`state`（`closed` 已终局 / `open` 仍持仓）、`exits` 出场事件列表 `[{type, time, price}]`（type：`breakeven` 保本 / `half` 平一半 / `close` 全平 / `stopSr` 支阻位止损 / `stopBe` 保本止损 / `stillOpen` 仍持仓）、互斥过滤的信号带 `suppressed: true` + `suppressedBy`（占用仓位的信号时间）。

## 常见问题排查

| 问题 | 可能原因 | 检查命令 |
|------|----------|----------|
| ECONNREFUSED | TradingView 未以调试模式启动 | `netstat -ano \| findstr 9222` |
| 未找到页面 | 图表标签未打开 | 打开一张图表即可 |
| **报错「未找到笔数据文件」** | 未先运行「画笔」SKILL（chan-bi） | 先画笔，再运行本脚本 |
| **报错「未找到支阻位数据文件」** | 未先运行「支阻互换位」SKILL（mark-sr-flip） | 先标记支阻位，再运行本脚本 |
| 某周期「最近笔端点无买卖点锚」 | 该周期最近笔端点上没有买族/卖族买卖点 | 运行「标记买卖点」查看该周期买卖点分布；`--debug` 查看锚匹配过程 |
| 某周期「条件未满足」 | 策略专属条件（够笔/破前底过前高/MACD0轴/出中枢力度变弱/以下级别背驰/支阻位附近）未全部满足 | 用 `--debug` 查看各条件判定 |
| **切到小周期后早期箭头错位/漂移** | TradingView 小周期默认只加载最近若干根K线，早期箭头被吸附到数据边缘 | 切到该周期后**滚动到图表最左**加载完整历史，箭头即恢复正确位置 |

## 注意事项

- 脚本自动读取**当前图表**的品种和周期，切换品种后需重新运行。
- **必须先依次运行「画笔」→「支阻互换位」**，本脚本才能标记；否则报错退出（买卖点由本脚本用 chan-core 现算，不读买卖点/计划落盘文件）。
- 本 SKILL 只负责进出场箭头标记，**不画笔、不清除笔/中枢/买卖点/支阻位标记**。
- 进场信号 = 「买卖点锚映射策略」+「该策略全部进场条件」共振，缺一不可；双侧无锚时不产生任何信号。
- **箭头锚点用K线索引存储**：TradingView 切周期时会重置为「默认加载最近K线」，小周期（3/15分钟）默认范围短，早期箭头切周期后可能漂移；这是平台限制，滚动到图表最左加载完整历史即可恢复（画笔的笔同样存在此现象，但箭头是单点所以更明显）。


## 预期笔与够笔运行笔（2026-09-17 更新）

下跌够笔且普通底分型右肩收盘后，预期上涨段立即参与下级买卖点、中枢归属和方向判断；卖侧镜像。合并后从起点块起满5块（含起点）才通过够笔门槛；进场预期够笔同样使用合并K线。活动端点不锚定已确认一类点，原始画笔缓存与结构视图分开。Python与JS共用同构 `buildStructureContext` / `mergedSegmentCount` 规则，详见 [统一规范](../../../../spec/plans/SPEC_structure_context.md)。
