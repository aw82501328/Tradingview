# 标记进出场 mark-entry 规格（SPEC）

> 文档用途：描述「标记进出场」SKILL 的功能规格——读取交易计划落盘结果判定各周期当前进场状态，映射到 6 种进场策略，校验策略进场条件（够笔/破前底过前高/MACD 0轴/出中枢力度变弱/以下级别背驰/支阻位附近），在「背驰级别」（更低周期）标记买点（向上红箭头）/卖点（向下绿箭头）。
> 对应脚本：`.cursor/skills/mark-entry/scripts/mark_entry.js`。
> 算法来源：缠论算法复用 `chan-core`（唯一算法源）；**进场状态判定不自行实现**，直接读取 `trading-plan` 落盘的 `plan_<品种>.json`。

## 1. 输入

| 项 | 来源 | 说明 |
|----|------|------|
| 笔数据 | `.cursor/cache/bis_<品种>.json`（chan-bi 画笔落盘） | **强制依赖**；缺文件或品种不符 → 报错退出 |
| 支阻位数据 | `.cursor/cache/srflip_<品种>.json`（mark-sr-flip 落盘） | **强制依赖**：取 `merged` 字段（跨周期合并后的支阻位列表）；为空 → 报错退出 |
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
2. **以下级别出现背驰** `lowerDiverge`：所有更低周期（`intervalSecOf` 更小）中方向匹配的最新背驰点（short→顶背驰、long→底背驰），取时间最新者 → 背驰点所在周期即「背驰级别」（标记位置）；
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
- **以下级别背驰**：`findDivergePoints(bis, macdArr)`（复用，不做区间套/锚定）识别各更低周期背驰点。

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

### 3.4 落盘

写 `.cursor/cache/entry_<品种>.json`：

```json
{
  "symbol": "OANDA:XAUUSD",
  "from": "2026-07-02",
  "fromTs": 1782086400,
  "generatedAt": "...",
  "nearAtr": 1.0,
  "periods": { "15": [{ "periodX": "60", "time": 1724908800, "price": 4631.98, "direction": "short", "strategyKey": "wait2Sell", "nearSr": 4640.1, "color": "#089981" }] }
}
```

`periods` 按**背驰级别**聚合（同级别绘制/清除）。

### 3.5 绘制

- **清除阶段**：遍历 `PERIODS ∪ 标记级别`，先清除该周期旧箭头（title = `ENTRY_<周期>`），避免「某周期本次无信号」时旧箭头残留；
- **绘制阶段**：对每个有信号的标记级别：
  - **创建周期选择**：箭头创建在「低一级」周期（`lowerResOf(res) || res`）——低一级周期 bar 边界更细，锚点时间在其上精确定位；3 分钟是最小周期，锚点在 3 分钟读取始终返回原始时间，是天然稳定锚定周期；
  - 绘制前 `ensureBarsCover` 确保图表数据覆盖最早信号时间（避免箭头被吸附到数据边缘）；
  - 创建 `arrow_up`（做多）/ `arrow_down`（做空）：买点红色 `#F23645`、卖点绿色 `#089981`（`color` 与 `arrowColor` 都设置）、`lock:false`、title = `ENTRY_<背驰级别>`，并设置可见范围（只本周期显示）；
- **最后切回原周期并恢复完整历史**：切周期会让当前周期数据重置为「默认加载」（最近若干根K线），早期箭头会被吸附到数据边缘；切回原周期后重新 `ensureBarsCover` 加载完整历史，确保原周期的早期箭头锚点正确。

## 4. 输出

| 项 | 说明 |
|----|------|
| 图上箭头 | 买点 `arrow_up` 红 `#F23645` / 卖点 `arrow_down` 绿 `#089981`，只在本周期显示，title = `ENTRY_<背驰级别>` |
| 进出场缓存 | `.cursor/cache/entry_<品种>.json`（按背驰级别聚合，含 `strategyKey`） |

## 5. 边界情况

| 场景 | 处理 |
|------|------|
| 笔数据文件不存在/品种不符 | 报错退出，提示先运行「画笔」 |
| 支阻位文件不存在/`merged` 为空 | 报错退出，提示先运行「支阻互换位」 |
| 交易计划文件不存在/品种不符/`periods` 为空 | 报错退出，提示先运行「交易计划」 |
| 某周期无周期数据（K线读取失败） | 跳过该周期 |
| 某周期交易计划为观望（震荡/无匹配） | 跳过该周期（无进场状态） |
| 某周期策略条件未满足 | 记录原因并跳过（--debug 可见） |
| 3 分钟周期 | 为最小周期，无更低级别背驰 → 状态不产生进场信号 |
| 数据未覆盖起始日期 | `scrollToFirstBar` 加载完整历史（周期性重新触发，防回弹） |
| 早期信号超数据范围 | 绘制前 ensureBarsCover 强制加载，仅在连续 30 次（约 36 秒）无进展时兜底放弃 |

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
| `chan-core` | 复用 `calcATR`/`calcMACD`/`isBiDiverge`/`buildZSByUpper`/`fmtT`/`lowerResOf`/`intervalSecOf` |

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
