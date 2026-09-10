---
name: mark-entry
description: Mark entry signals (进出场) on the TradingView Desktop chart via CDP. Reads the trading-plan result (plan_<symbol>.json) to determine each timeframe's current entry state, maps it to one of 6 entry strategies, validates that strategy's entry conditions (enough strokes / breaking previous low-high / MACD zero axis / weaker momentum leaving ZhongShu / lower-timeframe divergence / near S/R level), and marks buy/sell arrows on the divergence timeframe. Long = up red arrow, short = down green arrow. Also simulates exits per position (stop = intrabar break of the direction-aware S/R reference; TP1 breakeven after divergence-TF stroke completes; TP2 half-close after detection-TF stroke completes; TP3 full close on detection-TF breaking prior low/high) with same-direction mutual exclusion (no new same-direction entry while one is open; long/short independent), marking exits as yellow arrows (down = close long, up = close short, title EXIT_<divergence TF>).
disable-model-invocation: true
---

# 进出场标记

通过 CDP 连接 TradingView Desktop，**读取「交易计划」（trading-plan）落盘的结果文件**判定各周期当前进场状态，映射到 **6 种进场策略**，校验该策略的进场条件后，在「背驰级别」（更低周期）标记进场箭头：

- **买点（多头）** → 向上**红色**箭头（`arrow_up`）
- **卖点（空头）** → 向下**绿色**箭头（`arrow_down`）

并对每个进场模拟**出场**（止损 + 三档止盈），在图上以**统一黄色箭头**标记出场点（多头出场 ↓ / 空头出场 ↑，与 Web 控制台一致）：

- **终局出场**（支阻位止损 / 保本止损 / 全平）→ 黄 `↓/↑`
- **平一半** → 黄 `↓/↑`（保本/仍持仓仅落盘，不画图）

> **算法来源**：背驰判定复用 `chan-core` 的 `isBiDiverge`，中枢复用 `buildZSByUpper`（唯一算法源）；**进场状态判定不自行实现**，直接读取 `trading-plan` 落盘的 `.cursor/cache/plan_<品种>.json`（各周期 `direction/strategy`）。
>
> **强制依赖三个前置数据**：
> 1. **画笔**（chan-bi）落盘的笔数据 `.cursor/cache/bis_<品种>.json`；
> 2. **支阻互换位**（mark-sr-flip）落盘的支阻位数据 `.cursor/cache/srflip_<品种>.json`（读取其 `merged` 合并后支阻位）；
> 3. **交易计划**（trading-plan）落盘的计划数据 `.cursor/cache/plan_<品种>.json`（读取各周期 `strategy` 判定进场状态）。
>
> 任一数据文件缺失、或品种不匹配，脚本会**报错退出**。**运行依赖链**（依序执行）：**画笔（chan-bi）→ 标记买卖点（mark-buy-sell）→ 支阻互换位（mark-sr-flip）→ 交易计划（trading-plan）→ 本脚本**。

## 前置条件

1. TradingView Desktop 以调试模式启动：`TradingView.exe --remote-debugging-port=9222`
2. 已打开至少一张图表，且图表停留在**要标记的品种**上（脚本自动读取当前品种和周期）
3. chrome-remote-interface 已安装（在 `server-cdp/node_modules/`）
4. **已运行「画笔」**（chan-bi），生成笔数据文件
5. **已运行「标记买卖点」**（mark-buy-sell），标记历史买卖点
6. **已运行「支阻互换位」**（mark-sr-flip），生成支阻位数据文件
7. **已运行「交易计划」**（trading-plan），生成计划数据文件

## 使用方式

```bash
# 在图表上标记进出场（默认 4小时/1小时/15分钟/3分钟）
node .cursor/skills/mark-entry/scripts/mark_entry.js --from=2026-06-30

# 只计算并打印，不绘图（先验证再标）
node .cursor/skills/mark-entry/scripts/mark_entry.js --dry --from=2026-06-30

# 调整靠近支阻位阈值（×状态所在周期ATR）
node .cursor/skills/mark-entry/scripts/mark_entry.js --from=2026-06-30 --near=1.0
```

> `--from` 起始日期应与画笔/支阻位/交易计划时一致。

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--from=YYYY-MM-DD` | **必填**：起始日期，与画笔/支阻位/交易计划一致 | 无（缺少时报错退出） |
| `--periods=...` | 检测周期列表（逗号分隔，从大到小） | `240,60,15,3` |
| `--with-30s` | 启用 30 秒级别：ALL_RES 追加 30S（需先用 `chan-bi --with-30s` 落盘 30S 笔数据），3 分钟状态可用 30S 背驰产生进场信号，箭头画在 30S 级别；30S 数据只取最近 3 天。检测周期列表本身**不变**（30S 无更低级别，不作检测周期） | 关闭 |
| `--near=K` | 靠近支阻位阈值（×状态所在周期ATR） | `1.0` |
| `--lots=N` | 每笔进场手数（仅落盘记录；盈亏口径 = 价格差×方向×手数，JS 端不算盈亏） | `4` |
| `--slip-stop=K` | 止损位滑点（绝对价格：正确侧支阻位外侧偏移，short +/long −） | `3` |
| `--slip-fallback=K` | 兜底止损滑点（无正确侧支阻位 → 止损 = 进场价±该值；止损位永不为 null） | `10` |
| `--slip-be=K` | 保本滑点（beStop = 进场K线极值±该值，short: high+/long: low−） | `3` |
| `--dry` | 只计算不绘图 | 关闭 |
| `--debug` | 打印背驰、条件判定等调试信息 | 关闭 |

## 起始日期规则（每次标记必做，且必须输入日期）

**用户说「标记进出场」时，必须先询问「起始日期」（格式 `YYYY-MM-DD`，与画笔/支阻位/交易计划一致），得到日期后再执行标记。不得在未获得日期的情况下直接运行脚本。**

```
① 询问：本次标记进出场从哪个日期开始？（格式 YYYY-MM-DD，与画笔/支阻位/交易计划一致）
② 用户给出日期 → 运行 node mark_entry.js --from=YYYY-MM-DD
③ 用户未给出明确日期 → 再次询问，直到获得日期后才执行
```

## 进场状态 → 6 种策略映射

对每个检测周期 X（240/60/15/3），从交易计划结果 `plan_<品种>.json` 取该周期 `strategy`，映射到进场策略：

| trading-plan strategy（plan 结果） | 进场策略 | 方向 | 箭头 |
|------|------|------|------|
| 等待反弹后做2卖（状态=1卖） | 等待反弹后做2卖 | 空头 | 向下绿箭头 |
| 等待回调后做2买（状态=1买） | 等待回调后做2买 | 多头 | 向上红箭头 |
| 等待高点附近的一卖（状态=2/3买+其他） | 等待一卖 | 空头 | 向下绿箭头 |
| 等待低点附近的一买（状态=2/3卖+其他） | 等待一买 | 多头 | 向上红箭头 |
| 等待回调后的新买点（状态=2/3买+过左高不背驰） | 等待回调后买点 | 多头 | 向上红箭头 |
| 等待反弹后的新卖点（状态=2/3卖+过左低不背驰） | 等待反弹后卖点 | 空头 | 向下绿箭头 |
| 震荡/数据不足/趋势中无匹配买卖点（方向=观望） | 不触发 | — | — |

## 6 种进场策略的条件（全部需同时满足）

**公共条件**（所有策略）：

1. **够笔**：最后一笔方向符合预期（空头→最后一笔为 up「反弹够笔」、多头→最后一笔为 down「回调够笔」）；
2. **以下级别出现背驰**：在所有更低周期（如 60 的更低级别为 15/3）中，存在方向匹配的背驰点（做多→底背驰、做空→顶背驰），取**时间最新**的背驰点；
3. **在支阻位附近**：背驰点价与任一支阻位（`srflip.merged`）价差 ≤ `--near × 状态所在周期ATR`。

**各策略专属条件**：

| 策略 | 专属条件 |
|------|------|
| 等待反弹后做2卖 | MACD 下0轴后反弹不过0轴（当前 DIF<0）+ 下跌段破前底 |
| 等待回调后做2买 | MACD 上0轴后回调不破0轴（当前 DIF>0）+ 上涨段过前高 |
| 等待一卖 | 够笔且过高点（最后一笔 up 且突破前高）+ 出中枢的力度变弱 |
| 等待一买 | 够笔且过低点（最后一笔 down 且跌破前低）+ 出中枢的力度变弱 |
| 等待回调后买点 | （仅公共条件） |
| 等待反弹后卖点 | （仅公共条件） |

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
| 2 | **平一半 half** | **仅顺势**（计划 direction ∈ {多头多, 空头空}）：检测周期首个有利方向、**合并后 ≥5 根K且有成笔预期**的形成段 | 平一半（下一开盘成交），剩余半仓止损移至 beStop，黄 `↓/↑` |
| 3 | **全平 close** | 顺势：检测周期**有利方向笔破前高/前低**；逆势（多头空/空头多）：检测周期首个有利方向形成段（合并后 ≥5 根K成笔预期） | 全平终局（下一开盘成交），黄 `↓/↑` |
| 4 | **止损 stopSr** | 背驰级别K线盘中破坏止损位（short `high>` 位 / long `low<` 位；跳空按开盘价成交） | 全平终局，黄 `↓/↑` |
| — | **保本止损 stopBe** | 保本触发后盘中破坏 **beStop**（跳空按开盘价成交） | 全平终局，黄 `↓/↑` |

- **顺势 / 逆势**：**顺势** = 计划方向 ∈ {多头多, 空头空}；**逆势** = {多头空, 空头多}。顺势阶梯完整（保本 → 半平 → 破前高/前低全平）；**逆势没有半平**——「形成段 ≥5 根K」一到就全平快速离场。
- **止损位**（`stopRefOf`，方向感知）：short 取进场价**上方**最近支阻位 + 止损滑点、long 取**下方**最近 − 止损滑点；信号自带 `nearSr` 已在正确侧则直接沿用（± 滑点）。**无正确侧位 → 兜底 = 进场价 ± 兜底滑点**（止损位永不为 null，不再有「不设止损」仓位）。
- **保本止损位 beStop** = 进场K线极值 ± 保本滑点（short: high+ / long: low−）。
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
顶层含 `nearAtr`、`lots`（手数）、`slipStop`/`slipFallback`/`slipBe`（三个滑点参数，供复现）；
`periods` 按**背驰级别**聚合，字段：`periodX` 状态所在周期、`time` 背驰时间、`price` 背驰点价格、`direction`（`long` 做多 / `short` 做空）、`strategyKey`（策略标识）、`nearSr` 靠近的支阻位价格、`planDirection`（该周期计划方向，顺势/逆势判定用）、`color` 箭头颜色；
出场相关新增字段：`stopRef` 止损位（支阻位±滑点或兜底，永不为 null）、`beStop` 保本止损位（进场K线极值±保本滑点）、`state`（`closed` 已终局 / `open` 仍持仓）、`exits` 出场事件列表 `[{type, time, price}]`（type：`breakeven` 保本 / `half` 平一半 / `close` 全平 / `stopSr` 支阻位止损 / `stopBe` 保本止损 / `stillOpen` 仍持仓）、互斥过滤的信号带 `suppressed: true` + `suppressedBy`（占用仓位的信号时间）。

## 常见问题排查

| 问题 | 可能原因 | 检查命令 |
|------|----------|----------|
| ECONNREFUSED | TradingView 未以调试模式启动 | `netstat -ano \| findstr 9222` |
| 未找到页面 | 图表标签未打开 | 打开一张图表即可 |
| **报错「未找到笔数据文件」** | 未先运行「画笔」SKILL（chan-bi） | 先画笔，再运行本脚本 |
| **报错「未找到支阻位数据文件」** | 未先运行「支阻互换位」SKILL（mark-sr-flip） | 先标记支阻位，再运行本脚本 |
| **报错「未找到交易计划数据文件」** | 未先运行「交易计划」SKILL（trading-plan） | 先运行交易计划，再运行本脚本 |
| 某周期「交易计划无进场状态」 | 该周期为震荡/数据不足/趋势中无匹配买卖点 | 运行交易计划查看该周期状态 |
| 某周期「条件未满足」 | 策略专属条件（够笔/破前底过前高/MACD0轴/出中枢力度变弱/以下级别背驰/支阻位附近）未全部满足 | 用 `--debug` 查看各条件判定 |
| **切到小周期后早期箭头错位/漂移** | TradingView 小周期默认只加载最近若干根K线，早期箭头被吸附到数据边缘 | 切到该周期后**滚动到图表最左**加载完整历史，箭头即恢复正确位置 |

## 注意事项

- 脚本自动读取**当前图表**的品种和周期，切换品种后需重新运行。
- **必须先依次运行「画笔」→「标记买卖点」→「支阻互换位」→「交易计划」**，本脚本才能标记；否则报错退出。
- 本 SKILL 只负责进出场箭头标记，**不画笔、不清除笔/中枢/买卖点/支阻位/交易计划**。
- 进场信号 = 「状态映射策略」+「该策略全部进场条件」共振，缺一不可；状态为观望（震荡/无匹配）时不产生任何信号。
- **箭头锚点用K线索引存储**：TradingView 切周期时会重置为「默认加载最近K线」，小周期（3/15分钟）默认范围短，早期箭头切周期后可能漂移；这是平台限制，滚动到图表最左加载完整历史即可恢复（画笔的笔同样存在此现象，但箭头是单点所以更明显）。
