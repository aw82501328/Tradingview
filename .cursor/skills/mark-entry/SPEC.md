# 标记进出场 mark-entry 规格（SPEC）

> 文档用途：描述「标记进出场」SKILL 的功能规格——读取交易计划落盘结果判定各周期当前进场状态，映射到 6 种进场策略，校验策略进场条件（够笔/破前底过前高/MACD 0轴/出中枢力度变弱/以下级别背驰/支阻位附近），在「背驰级别」（更低周期）标记买点（向上红箭头）/卖点（向下绿箭头）；并对每个进场模拟**出场**（止损±滑点+兜底 + 三档止盈，出场阶梯重构 2026-09-09）与**同向持仓互斥**，出场点以统一**黄色箭头**标记（多头出场 ↓ / 空头出场 ↑，title = `EXIT_<背驰级别>`，与 Web 控制台一致）。
> 对应脚本：`.cursor/skills/mark-entry/scripts/mark_entry.js`。
> 算法来源：缠论算法复用 `chan-core`（唯一算法源）；**进场状态判定不自行实现**，直接读取 `trading-plan` 落盘的 `plan_<品种>.json`；「以下级别背驰」的区间套下沉判定与出场规则均与 `py_chain` 回测引擎（mark_entry.py / backtest.py）对齐。

## 1. 输入

| 项 | 来源 | 说明 |
|----|------|------|
| 笔数据 | `.cursor/cache/bis_<品种>.json`（chan-bi 画笔落盘） | **强制依赖**；缺文件或品种不符 → 报错退出 |
| 支阻位数据 | `.cursor/cache/srflip_<品种>.json`（mark-sr-flip 落盘） | **强制依赖**：取 `merged` 字段（跨周期合并后的支阻位列表）；为空 → 报错退出。2026-09-08 起 merged 含三类来源：密集区（跨周期合并结果）+ 黄金分割（非一类买卖点参照笔回撤位）+ BOLL（末根已收盘K线布林带）——本脚本消费口径不变（nearSr/止损参考位只读 `price`），三类自动同等参与判定 |
| 交易计划数据 | `.cursor/cache/plan_<品种>.json`（trading-plan 落盘） | **强制依赖**：取 `periods` 字段（各周期 `direction/strategy`）；缺失/为空 → 报错退出 |
| K 线 | TradingView 图表实时读取 | 用于 ATR、MACD 0轴、以下级别背驰判定 |
| 品种/当前周期 | 图表自动读取 | `chart.symbol()` / `chart.resolution()` |
| 参数 | 命令行 | 见下表 |

### 参数表

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--from=YYYY-MM-DD` | 无（必填） | 起始日期，应与画笔/支阻位/交易计划一致 |
| `--periods=...` | `240,60,15,3` | 检测周期（注意：默认不含日线） |
| `--near=K` | 1.0 | 靠近支阻位阈值（×状态所在周期ATR） |
| `--lots=N` | 4 | 每笔进场手数（仅落盘记录；盈亏口径 = 价格差×方向×手数，JS 端不算盈亏） |
| `--slip-stop=K` | 3 | 止损位滑点（绝对价格：正确侧支阻位外侧偏移，short +/long −） |
| `--slip-fallback=K` | 10 | 兜底止损滑点（无正确侧支阻位 → 止损 = 进场价±该值；止损位永不为 null） |
| `--slip-be=K` | 3 | 保本滑点（beStop = 进场K线极值±该值，short: high+/long: low−） |
| `--dry` | 关闭 | 只计算不绘图 |
| `--debug` | 关闭 | 打印调试信息 |

## 2. 信号定义

**进场信号 = 交易计划状态映射策略 + 该策略进场条件全部满足**。

### 2.1 状态 → 策略映射（entryStrategyOf）

对每个检测周期 X，读取 `plan_<品种>.json` 的 `periods[X].strategy`（trading-plan 的 `strategyOf` 输出）：

| plan.strategy | 策略标识 key | direction | 箭头 |
|------|------|------|------|
| 等待反弹后做2卖 | `wait2Sell` | short | 向下绿箭头 |
| 等待回调后做2买 | `wait2Buy` | long | 向上红箭头 |
| 等待高点附近的一卖 | `wait1Sell` | short | 向下绿箭头 |
| 等待低点附近的一买 | `wait1Buy` | long | 向上红箭头 |
| 等待回调后的新买点 | `waitBuy` | long | 向上红箭头 |
| 等待反弹后的新卖点 | `waitSell` | short | 向下绿箭头 |
| 其他（震荡/数据不足/趋势中无匹配，方向=观望） | 无 | — | 不触发 |

### 2.2 进场条件（evaluateEntry，全部同时满足）

公共条件：

1. **够笔** `lastBiOk`：最后一笔 type 为预期方向（short→up「反弹够笔」、long→down「回调够笔」）；
2. **以下级别出现背驰（区间套下沉判定，2026-09-08 与 `py_chain` 对齐）** `lowerDiverge`：收集所有更低周期（`intervalSecOf` 更小）中方向匹配的背驰点（short→顶背驰、long→底背驰）后，逐候选做**下沉链校验**（`sinkChainConfirm`，规则见 `py_chain/SPEC_divergence_chanset.md`）：
   - **规则 1（展开下沉）**：候选顶/底 P，从检测周期 X 的「以 P 为终点的笔」开始——该级笔内部次一级别存在 ≥3 笔结构（段间不跨上级笔边界、含形成中延伸段计 1 笔）且末段终点即 P → 下沉到次级别，重复；展开不足 3 笔 → 停止下沉。链逐级连续（60→15→3，`--with-30s` 时 3→30S），**不可跳级**（缺中间级别则链截断）；下沉上限 = X；
   - **规则 2（跨级禁止）**：候选背驰段的参照笔必须与候选段**同处其所属上级笔内部**——参照笔起点早于所属上级笔起点 → 候选无效（丢弃，不回退更早参照）；
   - **规则 3（归属）**：仅保留「候选级别 == 该点下沉停止级」的候选；通过支阻位校验的候选所在周期即「背驰级别」markRes（标记位置）；
   - **虚拟形成笔**：P 晚于 X 末段端点且 X 级无精确端点笔时（如近等双顶平台：X 顶已过、后续反向结构未确认为笔——图表最终结构经「近等双顶取后顶」并入同一笔），以「末段端点 → P」开放段作容器参与展开计数；
   - `evaluateEntry` 的回退次新循环不变——过滤后所见候选已全部下沉合法，**不会把高级别候选替换成跨级 3m/30S 微点**（案例 A/B 类错归属由此消除：2026-09-01 60m 反弹笔内 15m 五笔结构归属 15 而非 3；2026-09-04 15:48 同笔顶不再出信号）。
   > **当下背驰模式（`py_chain` 回测已实现，JS 端暂未实现）**：`signal_mode="realtime"` 时条件②改为 `realtimeLowerDiverge`（同样走下沉链，只在停止级产候选）——低级别**形成中段**（引擎增量状态的最后一笔，已延伸到当下极值）创新低/新高 + 当拍 MACD 对比弱于参照笔即出信号，**不等反向笔确认**（交易是基于当下的预测）；条件①改为「检测周期形成中回调段方向匹配 + 段长 ≥5 根K线」（否则单笔回调场景当下信号永不触发）；条件③不变。去重按段起点（每个形成段只发一次）。详见 `py_chain/SPEC.md` §1 信号模式。
3. **在支阻位附近** `nearSr`：背驰点价与 `srflip.merged` 任一支阻位价差 ≤ `NEAR_ATR × 状态所在周期ATR`。

各策略专属条件：

| key | 专属条件 |
|------|------|
| `wait2Sell` | `brokePrevLow`（下跌段破前底）+ `macdBelowZero`（MACD 下0轴后反弹不过0轴） |
| `wait2Buy` | `brokePrevHigh`（上涨段过前高）+ `macdAboveZero`（MACD 上0轴后回调不破0轴） |
| `wait1Sell` | `brokePrevHigh`（够笔且过高点）+ `zsExitWeak(..., "short")`（出中枢力度变弱，离开笔为 up） |
| `wait1Buy` | `brokePrevLow`（够笔且过低点）+ `zsExitWeak(..., "long")`（出中枢力度变弱，离开笔为 down） |
| `waitBuy` | （仅公共条件） |
| `waitSell` | （仅公共条件） |

### 2.3 关键量定义

- **破前底/过前高**：最近完成的下/上涨笔终点跌破/突破更早同向笔终点（参照 `findDivergePoints` 的「创新低/创新高」，跳过幅度 < 当前笔 50% 的次级别笔）。
- **出中枢力度变弱**（`zsExitWeak`，`buildZSByUpper` 取最后一个中枢）：离开笔相对进入笔 `isBiDiverge` 为 true **或** 离开笔 `span < 进入笔 span`；无离开笔（中枢未离开）→ false。
- **MACD 0轴**：`calcMACD` 返回的 `dif`，当前值 <0（下0轴）/ >0（上0轴）。
- **以下级别背驰**：`findDivergePoints(bis, macdArr)`（复用，不做区间套/锚定）识别各更低周期背驰点，候选点含 `referStart`（参照笔起点，供下沉判定的规则 2 参照 containment）。

### 2.4 出场条件（simulatePosition，纯函数）

**出场阶梯**：持仓后每根 K 线收盘判定一次，**同一根 K 线内按固定顺序只看第一个命中的事件**：

> **保本 → 平一半 → 全平 → 止损**

每个进场信号从进场时刻起按时间顺序模拟，事件按时间归并处理，**同拍只挂一个成交型事件（与 py 引擎 `advance_exit_decision` 一致）**。所有成交型出场统一在**触发 K 线的下一根开盘价**成交；触发 K 线若已是最后一根则未成交（`state:'open'`）。

| 事件 | 触发条件 | 动作 | 图标 |
|------|----------|------|------|
| 止损 `stopSr` | 背驰级别（markRes）K线**盘中**破坏止损位：short `high > 位` / long `low < 位`（跳空按开盘价成交） | 全平终局（下一开盘成交） | 黄 `↓/↑` |
| 保本 `breakeven` | **背驰周期够笔**：markRes 首个 `endTime > 进场时间`、type 为有利方向（short→down / long→up）的笔完成 | 止损位上移至**保本止损位 beStop** | 仅落盘 |
| 平一半 `half` | **仅顺势**（plan.direction ∈ {多头多, 空头空}）：periodX 首个有利方向、**合并后 ≥5 根K且有成笔预期**的形成段（`favSeg5Time`） | 平一半（下一开盘成交），剩余半仓止损移至 beStop（不要求保本先触发） | 黄 `↓/↑` |
| 全平 `close` | **顺势**：periodX 进场后**有利方向**笔破前高/前低（`findBiEvent(..., fav, breakPrev)`）；**逆势**（多头空/空头多）：periodX 首个有利方向形成段（合并后 ≥5 根K成笔预期） | 全平终局（下一开盘成交） | 黄 `↓/↑` |
| 保本止损 `stopBe` | 保本触发后盘中破坏 **beStop**（short `high > beStop` / long `low < beStop`） | 全平终局（跳空按开盘价成交） | 黄 `↓/↑` |
| 仍持仓 `stillOpen` | 数据末尾未终局 | `state:'open'` | 无 |

**止损位**（`stopRefOf`，方向感知，**永不为 null**）：short 取进场价**上方**最近支阻位（阻力）+ 止损滑点、long 取**下方**最近（支撑）− 止损滑点；信号自带 `nearSr`（进场校验按绝对价差最近命中，不分上下方）已在正确侧则直接沿用（± 滑点），否则从 `srLevels` 重选正确侧最近位（**进场判定逻辑不变**）。**无正确侧位 → 兜底止损 = 进场价 ± 兜底滑点**（不再有「不设止损」仓位）。

**保本止损位 beStop** = 进场K线极值 ± 保本滑点（short: high+ / long: low−）。JS 取 `sig.time` 对应 markRes bar 的极值；py 引擎取成交那根 fine bar 的极值（run() 批量路径该 bar 当拍未收盘，存在 ≤1 根 fine bar 的微前视；step_to 实时路径无前视——研究口径可接受）。

**合并后 ≥5 根K且有成笔预期**（TP2 / 逆势 TP3 条件）：检测周期形成段自段起点合并块起 **≥5 块**（chan_core `isValid` 的 gap≥4 成笔门槛同口径）。JS 用 `favSeg5Time`（`mergeStep` 回放记录每块诞生时间，触发 = 段内第 5 块诞生 bar，成交 = 其后第一根 markRes bar 开盘）；py 引擎用 `forming_seg_ready`（增量 `_merged_times` 按末笔延伸终点二分计数）。**已知近似差异**：形成段达 5 后若被最终笔结构吸收（未成笔），py 引擎当下已触发、JS hindsight 不触发。

**顺势 / 逆势**（决定半平与全平的分支）：**顺势** = 计划方向 ∈ {多头多, 空头空}（计划结构方向 = 操作方向）；**逆势** = {多头空, 空头多}。计划方向缺失时按 `strategyKey` 兜底（`wait2Buy`/`waitBuy`/`wait2Sell`/`waitSell` → 顺势，`wait1Buy`/`wait1Sell` → 逆势）。**顺势**阶梯完整——保本 → 半平 → 破前高/前低全平；**逆势没有半平**——「形成段 ≥5 根 K」一到就全平快速离场。

**结算：手数与盈亏**：手数 `--lots` 默认 4（JS 端仅落盘记录，不算盈亏）。平仓盈亏 =（0.5 × 半平价 + 0.5 × 终局价 − 进场价）× 方向 × 手数；未触发半平则 =（终局价 − 进场价）× 方向 × 手数。已在 Web 控制台显示。

**已知口径**：bis 最后一笔为延伸中的形成笔（chan-bi 落盘口径，`extendLastBi` 延伸至最新极值），其「完成」事件按延伸端点时间计（当下语义）。

**与 py 引擎的口径差异**：本技能读 `bis_<品种>.json` 的**最终笔快照**（全 hindsight），py 引擎用**当下快照**（无未来窥视）；另有 beStop 取价 bar、形成段 ≥5 块计数锚点、信号模式（本技能只有确认制，引擎默认当下制）等差异。**完整七条逐项差异表见 `py_chain/SPEC.md` §2.1.5.2**——两者不能混用，同一信号的出场时序可能略有差异，属研究口径差。

### 2.5 同向持仓互斥

- **多空双向互不影响**：long / short 各自独立状态机，可同时持仓；
- **同方向持仓未终局**（未止损/未全平）时，新的同方向信号**被过滤**：落盘保留 `suppressed: true` + `suppressedBy`（占用仓位的信号时间），**不画箭头**；
- 信号时间**晚于**该方向终局时间（平仓后）→ 放行，可再进场；
- **同时刻同向共振信号**（多个检测周期命中同一背驰点）按检测周期**从大到小**取一条（D>240>60>15>3>30S，`intervalSecOf` 比较），其余 suppressed；
- 排序处理：全部信号按 `time` 升序、同时刻按周期大者优先，逐个判定互斥并模拟出场。

## 3. 处理流程

### 3.1 强制依赖数据

- 读取 `bis_<品种>.json`（笔数据）、`srflip_<品种>.json`（支阻位 `merged` 字段）、`plan_<品种>.json`（交易计划 `periods` 字段），任一缺失/为空 → **报错退出**。

### 3.2 数据预取（periodData）

- 遍历 `["D","240","60","15","3"]`（取笔数据存在的周期），逐周期 `ensureResolution` + `fetchBars`，计算 `{bis, bars, atr, macdArr}` 存入 `periodData[res]`；
- `upperResOf(res)`：更大一级周期（240→D、60→240、15→60、3→15），供 `buildZSByUpper` 分解中枢。

### 3.3 逐周期判定（主循环）

对每个检测周期 X（`PERIODS`）：
1. 取 `planPeriods[X].strategy`；为空或方向=观望 → 跳过（无进场状态）；
2. `entryStrategyOf` 映射策略；无 → 跳过；
3. `evaluateEntry` 逐条件校验；任一不满足 → 记录原因并跳过；
4. 命中 → 生成信号 `{periodX, time, price, direction, strategyKey, nearSr, color}`，按 `markRes`（背驰级别）聚合存入 `allEntries[markRes]`。

### 3.4 同向互斥 + 出场模拟

- 展平 `allEntries` → 全部信号按 `time` 升序、同时刻按检测周期大者优先（`intervalSecOf` 比较）；
- 维护 `openPos = {long, short}`（各方向 `{sig, endTime}`，endTime = 终局时间 / 仍持仓 = Infinity）；
- 逐信号：同方向 `endTime > 信号时间` → 标记 `suppressed/suppressedBy` 跳过；否则 `stopRefOf` 选止损参考位 → `simulatePosition` 模拟出场 → 写入 `stopRef/exits/state`，更新 `openPos[direction]`；
- 重新按 `markRes` 聚合（带出场信息的信号对象用于落盘与绘制）。

### 3.5 落盘

写 `.cursor/cache/entry_<品种>.json`：

```json
{
  "symbol": "OANDA:XAUUSD",
  "from": "2026-07-02",
  "fromTs": 1782086400,
  "generatedAt": "...",
  "nearAtr": 1.0,
  "lots": 4,
  "slipStop": 3,
  "slipFallback": 10,
  "slipBe": 3,
  "periods": { "15": [{ "periodX": "60", "time": 1724908800, "price": 4631.98, "direction": "short", "strategyKey": "wait2Sell", "nearSr": 4640.1, "planDirection": "空头空", "color": "#089981",
                         "stopRef": 4643.1, "beStop": 4636.5, "state": "closed",
                         "exits": [{ "type": "breakeven", "time": 1724913000, "price": 4620.5 },
                                    { "type": "stopBe", "time": 1724918000, "price": 4636.5 }] }] }
}
```

`periods` 按**背驰级别**聚合（同级别绘制/清除）；出场字段：`stopRef`（止损位 = 支阻位±止损滑点或兜底进场价±兜底滑点，永不为 null）、`beStop`（保本止损位 = 进场K线极值±保本滑点）、`state`（closed/open）、`exits`（事件列表，含 breakeven）；互斥过滤信号带 `suppressed: true` + `suppressedBy`。

### 3.6 绘制

- **清除阶段**：遍历 `PERIODS ∪ 标记级别`，先清除该周期旧箭头（title = `ENTRY_<周期>`）与旧出场标记（title = `EXIT_<周期>`），避免「某周期本次无信号」时旧标记残留；
- **绘制阶段**：对每个有信号的标记级别：
  - **互斥过滤**：`suppressed` 信号不画箭头（仅落盘）；
  - **创建周期选择**：箭头与出场标记创建在「低一级」周期（`lowerResOf(res) || res`）——低一级周期 bar 边界更细，锚点时间在其上精确定位；最小周期（默认 3 分钟、`--with-30s` 时 30 秒）无更低级别，锚点在其自身读取始终返回原始时间，是天然稳定锚定周期；
  - 绘制前 `ensureBarsCover` 确保图表数据覆盖最早信号/出场时间（避免标记被吸附到数据边缘）；
  - 创建 `arrow_up`（做多）/ `arrow_down`（做空）：买点红色 `#F23645`、卖点绿色 `#089981`（`color` 与 `arrowColor` 都设置）、`lock:false`、title = `ENTRY_<背驰级别>`，并设置可见范围（只本周期显示）；
  - 创建出场标记（统一黄 `#FFEB3B`、title = `EXIT_<背驰级别>`、`lock:false`、只本周期显示）：方向 = 平仓方向（多头出场 ↓ / 空头出场 ↑），文本 = 事件名 + 价；保本/仍持仓不画图；
- **最后切回原周期并恢复完整历史**：切周期会让当前周期数据重置为「默认加载」（最近若干根K线），早期箭头会被吸附到数据边缘；切回原周期后重新 `ensureBarsCover` 加载完整历史，确保原周期的早期箭头锚点正确。

## 4. 输出

| 项 | 说明 |
|----|------|
| 图上箭头 | 买点 `arrow_up` 红 `#F23645` / 卖点 `arrow_down` 绿 `#089981`，只在本周期显示，title = `ENTRY_<背驰级别>` |
| 图上出场标记 | 统一黄 `#FFEB3B` 箭头（多头出场 ↓ / 空头出场 ↑，文本=事件名+价），只在本周期显示，title = `EXIT_<背驰级别>` |
| 进出场缓存 | `.cursor/cache/entry_<品种>.json`（按背驰级别聚合，含 `strategyKey`、`stopRef/state/exits`、`suppressed/suppressedBy`） |

## 5. 边界情况

| 场景 | 处理 |
|------|------|
| 笔数据文件不存在/品种不符 | 报错退出，提示先运行「画笔」 |
| 支阻位文件不存在/`merged` 为空 | 报错退出，提示先运行「支阻互换位」 |
| 交易计划文件不存在/品种不符/`periods` 为空 | 报错退出，提示先运行「交易计划」 |
| 某周期无周期数据（K线读取失败） | 跳过该周期 |
| 某周期交易计划为观望（震荡/无匹配） | 跳过该周期（无进场状态） |
| 某周期策略条件未满足 | 记录原因并跳过（--debug 可见） |
| 3 分钟周期 | 默认为最小周期，无更低级别背驰 → 状态不产生进场信号；`--with-30s` 启用后可检测 30S 背驰 → 状态产生信号（箭头画在 30S 级别） |
| 数据未覆盖起始日期 | `scrollToFirstBar` 加载完整历史（周期性重新触发，防回弹） |
| 早期信号超数据范围 | 绘制前 ensureBarsCover 强制加载，仅在连续 30 次（约 36 秒）无进展时兜底放弃 |
| 无正确侧支阻位 | 兜底止损 = 进场价 ± 兜底滑点（`--slip-fallback`，默认 10；止损位永不为 null） |
| 数据末尾仍未终局 | `state: "open"`，`exits` 记录已触发事件，无终局图标 |
| 同时刻多周期同向共振信号 | 按检测周期从大到小取一条，其余 `suppressed`（落盘不画箭头） |
| 同方向持仓期间的新信号 | `suppressed: true` + `suppressedBy`（持仓信号时间），不画箭头；平仓后放行 |

## 6. 已知平台限制

**箭头锚定/漂移**（TradingView 平台约束，SKILL.md 已文档化）：
- TradingView shape 按 bar 索引锚定（非绝对时间），切换周期且数据未完全加载时，较早的箭头可能被吸附到数据边缘导致错位/堆叠；
- 缓解手段：箭头在低一级周期创建（3分钟最稳定）+ 每次绘制前/切回后 `ensureBarsCover` 加载完整历史；
- 手动频繁切换周期时仍可能出现偶发漂移，属平台限制，暂无法完全消除。

## 7. 与其他模块的依赖

| 模块 | 关系 |
|------|------|
| `chan-bi`（画笔） | **强制依赖**：读取其落盘笔数据 `bis_<品种>.json` |
| `mark-sr-flip`（支阻位） | **强制依赖**：读取其落盘 `srflip_<品种>.json` 的 `merged` 字段 |
| `trading-plan`（交易计划） | **强制依赖**：读取其落盘 `plan_<品种>.json` 的 `periods` 字段（判定各周期进场状态） |
| `chan-core` | 复用 `calcATR`/`calcMACD`/`isBiDiverge`/`buildZSByUpper`/`fmtT`/`lowerResOf`/`intervalSecOf`/`mergeStep`（TP2/逆势TP3 的形成段合并K线计数） |

## 8. 依赖链总览

```
chan-bi（画笔）────────────→ mark-buy-sell（买卖点）
      │                            │
      └────→ mark-sr-flip（支阻位）─┼─→ trading-plan（交易计划）→ mark-entry（进出场）
                                    │
chan-core（算法库，被以上全部复用）──┘
```

**运行依赖链（依序执行）**：

```
画笔（chan-bi）→ 标记买卖点（mark-buy-sell）→ 支阻互换位（mark-sr-flip）→ 交易计划（trading-plan）→ 进出场（mark-entry）
```

- `mark-entry` 直接读取三个落盘文件：`bis_<品种>.json`（画笔）、`srflip_<品种>.json`（支阻位）、`plan_<品种>.json`（交易计划）；任一缺失 → 报错退出。
- 依赖链中前序技能执行时使用同一 `--from` 起始日期，保证各缓存数据时间范围一致。
