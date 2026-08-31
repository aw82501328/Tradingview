# 交易计划 trading-plan 规格（SPEC）

> 文档用途：描述「trading-plan」SKILL 的功能规格——逐周期区分当前品种是**震荡**还是**趋势**，趋势时依据「最近笔端点的买卖点类型」生成对应交易策略，输出各周期交易计划表（品种、周期、方向、策略），并把策略以蓝色文字标记显示在各周期图上。
> 对应脚本：`.cursor/skills/trading-plan/scripts/trading_plan.js`。
> 算法来源：缠论算法复用 `chan-core`（唯一算法源）；震荡判定 `isRangeBound` 复制自 `chan-status`（不迁移、不修改）；策略映射为本脚本 `predictPlan` 纯函数（可单测）。

## 1. 输入

| 项 | 来源 | 说明 |
|----|------|------|
| 笔数据 | `chan-bi` 画笔落盘 `.cursor/cache/bis_<品种>.json`（**强制依赖**） | 各周期已 ATR 过滤、未完成笔延伸、跨周期端点校准的最终笔，与图上所画笔一致 |
| 最新K线 | TradingView 图表实时读取（逐周期切换） | 计算 ATR / MACD / 最新收盘价（判断是否在中枢内、供给 isRangeBound 震荡判定） |
| 品种/当前周期 | 图表自动读取 | `chart.symbol()` / `chart.resolution()` |
| 参数 | 命令行 | 见下表 |

### 参数表

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--periods=...` | `D,240,60,15,3` | 要生成计划的周期列表（从大到小） |
| `--dry` | 关闭 | 只输出文本计划表，不绘图 |
| `--debug` | 关闭 | 打印买卖点列表、匹配过程等调试信息 |

## 2. 处理流程

### 2.1 数据读取与覆盖

- 逐周期 `ensureResolution(res)` 切换周期并等待K线加载稳定；
- `fetchBars(expectedIntervalSec)` 读取K线（用最后 20 个相邻间隔中位数判断真实周期，与期望不符重试）；
- 强制读取笔数据缓存：文件不存在 / 品种不匹配 → 报错退出（必须先运行「画笔」）。

**数据来源时效性**：
- **价格实时**：`lastPrice`/`lastBarTime`/`macdArr`/`atr` 均来自 `fetchBars` 实时读取的页面K线（含最新未成笔K线）。
- **笔结构为快照**：`bis`/`upperBis` 来自 chan-bi 最近一次运行的缓存文件，本脚本不会用实时K线重算、延伸或补画新笔。在关键行情节点判断前建议先运行 chan-bi 刷新缓存。

### 2.2 每周期计划计算（predictPlan）

输入：`{ res, bis, upperBis, macdArr, atr, lastPrice, bars, barSec }`（`bis` 取最近 60 笔，`bars` 为本周期实时K线）。
输出：`{ res, direction, strategy, reason, label }`。

判定顺序：

0. **数据不足**：`bis` 长度 < 2 → 方向「观望」，策略「数据不足」。
1. **震荡判定（A 或 B 任一命中即震荡）**：
   - A：`isRangeBound(bis, bars, atr)`（复制自 chan-status）：最近 `rangeBarN=40` 根K线 `maxHigh-minLow ≤ 5×ATR`；笔端点区间（≤ 7×ATR）与涨跌交替只取「最近 40 根K线时间范围内」的笔判断（窗口内无笔则跳过这两条）→ `range === true`；
   - A-突破跳过：最后一笔终点相对窗口区间 `[minL, maxH]` 的另一端明显偏移（up 笔 `endPrice - minL > rangeBreakMult×ATR`，down 笔 `maxH - endPrice > rangeBreakMult×ATR`，`rangeBreakMult` 默认 1.0）→ 视为突破盘整，跳过 A 判定（`range:false, breakOut:true`）；
   - B：中枢必须构建在**上一级别同一笔**内：`buildZSByUpper(bis, upperBis, barSec)`（分解原则：用上级笔时间区间把本级别笔切段，每段内独立 `buildZS`，中枢不跨上级笔端点；无上级笔时退化为 `buildZS(bis, barSec)`）。取**上一级别最后一笔**对应段（`z.upperStart ≥ 上级最后一笔.startTime - barSec`）内最后一个中枢，`exitTime === null`（未离开）且 `lastPrice` 位于中枢 `[zd, zg]` 内；
   - 命中 → 方向「观望」，策略「震荡整理，观望等待方向选择」（B 命中时文案为「震荡整理（中枢内）…」）。
2. **趋势 → 获取本周期买卖点**：`findBuyPoints(bis, upperBis, macdArr, barSec)` + `findSellPoints(...)`（chan-core，带上级笔区间套 + 实时 MACD 背驰），可能抛错 → try/catch 忽略（同 chan-status）。
3. **匹配最近笔端点的买卖点**：匹配容差 `tolSec = barSec`（本周期 1 个 bar）、`tolPrice = max(atr×0.2, 0.05)`：
   - 先匹配最后一笔终点 → 命中按类型映射精确策略（`strategyOf`）；2/3 类买卖点先经 `classifySecond` 分类（过左高/过左低不背驰 vs 其他）再映射；
   - 最后一笔终点为空 → 从倒数第二笔起**逐笔向前扫描**，取最近的有买卖点的笔端点 → **同样按类型精确映射**（`strategyOf` + `classifySecond`，不降级为只判断买卖方向）；
   - 仍无 → 方向「观望」，策略「趋势中无匹配买卖点」。

### 2.3 策略映射（strategyOf + classifySecond）

| 买卖点类型 | direction | strategy |
|------|------|------|
| `1卖` | 空头 | 等待反弹后做2卖 |
| `1买` | 多头 | 等待回调后做2买 |
| `2买` / `类2买` / `3买` + `过左高不背驰` | 多头 | 等待回调后的新买点 |
| `2买` / `类2买` / `3买` + 其他 | 多头（逆势） | 等待高点附近的一卖 |
| `2卖` / `类2卖` / `3卖` + `过左低不背驰` | 空头 | 等待反弹后的新卖点 |
| `2卖` / `类2卖` / `3卖` + 其他 | 空头（逆势） | 等待低点附近的一买 |

> **说明**：最后一笔终点无买卖点时向前扫描到的最近买卖点**同样按上表精确映射**（不再有「向前一笔为买点/卖点」的兜底行）。

**classifySecond（2/3类买卖点分类）**：
- 左高 = 买卖点之前最近顶端点（上涨笔终点 / 下跌笔起点，取最高）；左低 = 之前最近底端点（下跌笔终点 / 上涨笔起点，取最低）；
- 取买卖点后第一笔同向笔（买点后上涨 / 卖点后下跌），其终点**突破左高**（买点）或**跌破左低**（卖点）→ 过左高/过左低；
- 该笔相对前一同向参照笔（span ≥ 该笔×50%）`isBiDiverge` 为 false（MACD 动能未减弱）→ **不背驰**；
- 过左高/过左低且不背驰 → 返回「过左高不背驰 / 过左低不背驰」，否则「其他」。

### 2.4 表格汇总输出

逐周期计算后统一输出一张汇总表（列：品种 | 周期 | 方向 | 策略 | 理由），每周期一行：

- `symbol`：品种名；`res`：周期；`direction`：方向（做多/做空/观望）；`strategy`：策略文案；
- `reason`：判定理由，趋势时**标注找到的最近买卖点**（`找到最近买卖点 1卖 @ 8-17 18:00 30262.95（最后一笔端点/向前扫描最近笔端点）`），震荡时描述 A/B 判定依据；
- `pointDesc`：找到的最近买卖点简写（`1卖@8-17 18:00(30262.95)`），供图上标注第二行使用；
- 表格边框用 `printPlanTable(rows)` 输出（中文按全角宽度对齐，列宽自适应，复用 chan-status 的 `dispWidth`/`padCell` 逻辑）。

```
┌──────────────┬──────┬────────────────┬────────────────────────────┬──────────────────────────────────────────────┐
│ 品种         │ 周期 │ 方向           │ 策略                       │ 理由                                         │
├──────────────┼──────┼────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
│ OANDA:XAUUSD │ 60   │ 空头           │ 等待反弹后做2卖            │ 找到最近买卖点 1卖 @ 8-28 22:00 29759.05（最后一笔端点）│
└──────────────┴──────┴────────────────┴────────────────────────────┴──────────────────────────────────────────────┘
```

### 2.5 图上蓝色策略标记

- **清除**：切到各周期，按 title `CHAN_PLAN_<周期>` 清除该周期旧策略标记；
- **位置**：`最近 40 根K线最高点 + max(1.5×ATR, 0.5)`（顶部空白），时间 = `最新bar时间 + 2×本周期间隔`（最新bar右侧 2 根）；
- **内容**：`方向:${direction} | 策略:${strategy}`；若 `pointDesc` 存在，**换行追加** `最近买卖点:${pointDesc}`（震荡/无匹配时不追加）；
- **样式**：`text` 蓝色 `#2962FF`、粗体、title `CHAN_PLAN_<周期>`、`intervalsVisibilities` 仅本周期显示、`lock:false`。

### 2.6 结果落盘（供 mark-entry 进出场读取）

- 每次运行（含 `--dry`）把逐周期计划结果写入 `.cursor/cache/plan_<品种>.json`；
- 结构：`{ symbol, generatedAt, periods: { <res>: { direction, strategy, reason, pointDesc } } }`；
- **`mark-entry`（进出场）SKILL 强制依赖该文件**：读取各周期 `direction/strategy` 判定进场状态，不再自行实现状态判定。

## 3. 输出

| 项 | 说明 |
|----|------|
| 文本计划表 | 各周期「方向 + 策略 + 理由（含最近买卖点）」输出到控制台 |
| 图上蓝色策略标记 | `CHAN_PLAN_<周期>` 蓝色文字，仅本周期可见，位于该周期图顶部空白处；趋势时第二行标注最近买卖点 |
| 计划结果落盘 | `.cursor/cache/plan_<品种>.json`（各周期 `direction/strategy/pointDesc`，供 `mark-entry` 读取） |

## 4. 边界情况

| 场景 | 处理 |
|------|------|
| 未找到 TradingView 页面 | 报错退出 |
| 笔数据文件缺失 / 品种不匹配 | 报错退出，提示先运行「画笔」 |
| 某周期无K线或切换失败 | 跳过该周期 |
| 某周期笔数据为空（画笔未覆盖） | 跳过该周期并提示 |
| 笔数量不足 2 笔 | 方向「观望」，策略「数据不足」 |
| `findBuyPoints/findSellPoints` 抛错（数据不足） | try/catch 忽略，按「无买卖点」处理 |
| `buildZS/buildZSByUpper` 抛错 | try/catch 忽略，跳过中枢震荡判定 |
| 趋势但最近笔端点均无买卖点 | 方向「观望」，策略「趋势中无匹配买卖点」 |
| 2/3类买卖点后无同向笔 / 无前一同向参照笔 / MACD 背驰 | classifySecond 返回「其他分类」（逆势方向 + 等待高点附近的一卖 / 等待低点附近的一买） |

## 5. 与其他模块的依赖

| 模块 | 关系 |
|------|------|
| `chan-core` | 复用 `calcATR/calcMACD/buildZS/buildZSByUpper/isBiDiverge/findBuyPoints/findSellPoints/intervalSecOf/fmtT` |
| `chan-bi`（画笔） | **强制读取**本脚本落盘笔数据 |
| `chan-status`（状态） | **复制**其内部 `isRangeBound`（不 require、不修改，保持 chan-status 独立） |
| `mark-buy-sell`（买卖点） | 独立（本 SKILL 只生成计划，不重复标记历史买卖点） |
| `mark-sr-flip`（支阻位） | 独立（本 SKILL 不读取支阻位） |
| `mark-entry`（进出场） | **下游依赖**：本脚本落盘 `plan_<品种>.json`，`mark-entry` 读取其判定进场状态 |

**运行依赖链**（完整执行顺序，各技能依序运行）：

```
画笔（chan-bi）→ 标记买卖点（mark-buy-sell）→ 支阻互换位（mark-sr-flip）→ 交易计划（trading-plan）→ 进出场（mark-entry）
```

- 本 SKILL（交易计划）在链中位于支阻位之后、进出场之前，只**强制依赖画笔**的笔数据；
- 落盘的 `plan_<品种>.json` 供下游 `mark-entry` 判定进场状态。
