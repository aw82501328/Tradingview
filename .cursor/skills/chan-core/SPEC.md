# chan-core 缠论算法核心规格（SPEC）

> 文档用途：chan-core 是**唯一缠论算法源**，纯函数模块（不依赖 CDP、不绘图），被 `chan-bi`（画笔）、`mark-buy-sell`（买卖点）两个 SKILL 复用，`mark-entry`/`mark-sr-flip` 复用其工具函数。本规格描述其全部导出函数的行为契约，是上层 SKILL 规格的基础。
> 对应脚本：`.cursor/skills/chan-core/scripts/chan_core.js`（约 1427 行）。

## 1. 统一约定

| 项 | 约定 |
|----|------|
| 笔对象字段 | `type`(up/down)、`startIdx/endIdx`(合并K线索引)、`startTime/endTime`(校准后端点时间)、`startPrice/endPrice`、`rawCount`(覆盖原始K线数)、`span`(幅度)、`gapLocked`(跳空成笔)、`macdCross`(MACD变色成笔) |
| 时间 | 全部 Unix 秒（UTC），与 TradingView K线时间一致 |
| 配置 | `CHAN_CFG.gapFilter`（跳空独立成笔阈值，默认 1.0）、`CHAN_CFG.debug`（调试打印） |
| 合并K线字段 | 含 `_rawCount`(覆盖原始K线数)、`highTime/lowTime`(极值原始K线时间)、`rawHigh/rawLow/rawHighTime/rawLowTime`(覆盖原始K线真实极值及时间) |

## 2. 导出函数清单

### 2.1 包含关系处理

**`mergeBars(rawBars) → merged[]`**
- 相邻K线有包含关系时合并，方向由前序趋势决定：向上合并取「高高」，向下合并取「低低」；
- 每根合并K线记录 `_rawCount`、`highTime/lowTime`（合并后的极值时间）、`rawHigh/rawLow/rawHighTime/rawLowTime`（覆盖原始K线的**真实**极值，供跳空检测与端点修正）；
- 方向确定：首根后若前序无方向，用前两根合并K线高低比较（`last.high >= prev.high ? 1 : -1`）；首根默认向上（`dir=1`）。

**`countRaw(merged, startIdx, endIdx) → number`**
- 统计 `(startIdx, endIdx]` 覆盖的原始K线数（各合并K线 `_rawCount` 累加）。

### 2.2 分型识别

**`findFractals(merged) → fractals[]`**
- 顶分型：`cur.high > prev.high && cur.high > next.high && cur.low > prev.low && cur.low > next.low`，`time` 取最高价原始K线时间；
- 底分型：`cur.low < prev.low && cur.low < next.low && cur.high < prev.high && cur.high < next.high`，`time` 取最低价原始K线时间；
- 字段：`{ mergedIdx, type(top/bottom), high, low, time }`。

### 2.3 跳空检测

**`hasGapBetween(merged, aIdx, bIdx, atr, gapFilter) → boolean`**
- 检测 `[aIdx, bIdx)` 相邻合并K线之间是否存在跳空缺口；
- 用覆盖原始K线的**真实极值**（`rawHigh/rawLow`）判断，避免合并K线（向下合并压低高点/向上合并抬高低点）造成「假缺口」；
- 向上跳空：`nextLow - curHigh >= atr * gapFilter`；向下跳空：`curLow - nextHigh >= atr * gapFilter`。

### 2.4 笔构建

**`buildBi(fractals, merged, atr, macdArr, lockedPivots) → bis[]`**

三阶段：
1. **阶段一（交替分型序列）**：连续同类型分型取更极端者（顶取最高、底取最低），得到严格交替序列；区间套强制对齐：与 `lockedPivots`（上级笔端点）方向一致且价格一致的分布标记 `locked`；
2. **阶段二（回溯替换）**，按顺序处理每个分型 k，规则优先级如下：
   - 同类型分型：`locked` 端点不可替换；非 gapLocked 时更极端才替换；gapLocked 端点仅当后续突破锁定价格时替换；
   - **MACD 端点让位**：`result[-2]` 是 MACD 变色端点且 k 更极端 → k 顶替 `result[-2]`、移除中间分型；
   - **跳空成笔**：`last→k` 间存在 ≥ `gapFilter*ATR` 跳空 → 强制独立成笔（`k.gapLocked = true`）；
   - **前顶/前底作废**：`prev2` 与 k 同类型、`prev2→last` 不构成有效笔（间隔不足）、k 更极端、last 不得比 `prev3` 更极端（否则 last 是深回调的真实转折）、回调/反弹 < 50% 幅度、last 是弱分型（MACD变色且原始K线<5根）、`last`/`prev2` 均未锁定时，k 顶替 `prev2` 移除 last；
   - **有效笔接入**：`isValid(last, k)`（合并K线间隔 ≥ 4，即合并后至少 5 根）且（`noMoreExtremeInside` 或 last.gapLocked）且（`fractalRangeClear` 或 last.gapLocked）→ 接入；
   - **间隔不足 + MACD 变色**：`gap === 3` 且方向性红绿转换（底到顶绿变红/顶到底红变绿）且无更极值 → MACD 变色成笔（`k.macdCross = true, k.macdRaw = countRaw`）；
   - **回溯替换**：间隔不足且无 MACD 变色，k 与 `result[-2]` 同类型且更极端、且 `prev→last` 不构成有效笔时：若 last 比 `result[-3]` 更极端（回溯替换保护，保证区间套一致性）→ 保留 last 取代 `result[-3]`、作废 prev、暂不接入 k；否则 k 顶替 prev；
3. **阶段三（两两连笔）**：相邻端点连笔，计算 `rawCount`（`countRaw`）、`span`（`|endPrice-startPrice|`）。

辅助判定：
- `isValid(a,b)`：`b.mergedIdx - a.mergedIdx >= 4`；
- `noMoreExtremeInside(a,b)`：笔内（`a.mergedIdx+1 .. b.mergedIdx-1`）不存在比端点更极端的点（严格比较，无容差）；
- `fractalRangeClear(a,b)`：用分型K线**三根K线区间**判定脱离——顶→底需 `b.low < min(顶分型三根K线.low)`；底→顶需 `b.high > max(底分型三根K线.high)`（含左右邻K线）。

**`fixBiExtremes(bis, merged) → bis[]`**（端点极值修正，方向A）
- 对每笔检查「终点分型之后、下一笔终点分型之前」的合并K线，若存在被包含合并掩盖（`rawLow < low` / `rawHigh > high`）且比当前端点更极端的真实极值，把本笔终点与下一笔起点同步平移到该极值所在K线（保持首尾连续）；
- 只处理被掩盖的极值；`gapLocked` 端点固定不参与。

**`lockedPivotsOf(prevBis) → [{dir, price}]`**
- 由上级笔提取锁定端点：上涨笔起点→bottom、终点→top；下跌笔反之。

**`alignBiToUpper(lowerBis, upperBis, upperIntervalSec, lowerBars) → lowerBis`**（区间套强制对齐）
- 上级笔的每个起点/终点都是明确极值，下级周期必须复现相同极值；
- 每个上级拐点找「同方向、时间最近（≤ 上级间隔秒）且未使用」的下级拐点，快照对齐；
- 仅当下级拐点更不极端（漏掉上级真极值）时同时对齐时间+价格；否则只对齐价格保留下级更精确时间；
- **幽灵端点防御（第4参 `lowerBars`，可选）**：上级极值可能只存在于上级聚合数据中（跨周期数据源聚合差异，如日K聚合低点低于该日所有日内K线），本级K线无法复现该极值。判定：取上级拐点所在上级bar时间跨度 `[up.time, up.time + upperIntervalSec)` 内本级K线的**局部价格范围**，若上级极值超出该范围（底低于局部最低 / 顶高于局部最高），视为幽灵端点，跳过对该拐点的对齐（保留本级别真实极值）。未传 `lowerBars` 时跳过校验（向后兼容）。

### 2.5 中枢

**`buildZS(bis, barSec) → zss[]`**
- 取连续三笔的重叠区间构成中枢：`ZG = min(三笔高点)`、`ZD = max(三笔低点)`，`ZG > ZD` 才成立；
- 延伸：后续笔与 `[ZD, ZG]` 有重叠则纳入（`dd/gg` 扩展）；
- 离开：笔与中枢区间完全无重叠；或笔起点在中枢内、终点突破中枢边界（`startIn && endBreak`）→ 中枢结束；
- **最少 5 笔才输出**（用户要求：只有上下上/下上下 3 笔的不画）；`biCount < 5` 跳过并继续向后扫描；
- 中枢区间 = 构成中枢全部笔（含离开笔）的重叠：`ZG = min(全部笔高点)`、`ZD = max(全部笔低点)`；`zsZg <= zsZd` 时防御性跳过；
- 水平边缘：左 = 进入笔终点 - 5×barSec，右 = 离开笔起点 + 5×barSec（无离开笔时 = 最后一笔终点 + 5×barSec）；
- 输出：`{ startTime, endTime, zd, zg, dd, gg, biCount, extended, exitTime, enterEndTime, exitStartTime }`。

**`buildZSByUpper(lowerBis, upperBis, tolSec) → zss[]`**（按上级笔分解）
- 本级别中枢只能构建在「同一个上级笔」内部：用上级笔时间区间把本级别笔切段（`startTime ≥ upper.startTime - tol` 且 `endTime ≤ upper.endTime + tol`），每段内独立运行 `buildZS`；
- 无上级约束（最外层）时直接用全部笔构建；
- 不完整落在任何上级笔内的零散笔不参与中枢；
- 每项额外含 `upperStart/upperEnd`（所属上级笔时间范围）。

### 2.6 ATR / MACD

**`calcATR(rawBars, period=14) → number`**
- 平均真实波幅：`TR = max(H-L, |H-前收盘|, |L-前收盘|)`，取最近 `period` 根均值。

**`calcMACD(rawBars) → [{time, macd, dif, dea}]`**
- EMA12/EMA26 → DIF = EMA12-EMA26；DEA = DIF 的 EMA9；`macd = (DIF-DEA)*2`（约定 macd>0 红柱/多头，macd<0 绿柱/空头）；
- time 与原始K线一一对应。

**`hasMacdCrossBetween(macdArr, merged, aIdx, bIdx, aTime, bTime, direction) → boolean`**
- 检测两个分型之间是否发生方向性 MACD 红绿转换：`up`=底到顶绿变红（`≤0 → >0`）、`down`=顶到底红变绿（`>0 → ≤0`）、未指定=任意转换；
- 检测区间用「分型的极值时间」作边界（非合并K线最新时间），避免把极值之后（包含区间内）的 MACD 变化误算进来。

### 2.7 未完成笔延伸 / 周期映射 / 端点校准

**`extendLastBi(bisArr, bars) → bisArr`**
- 缠论要求最新一笔延伸到当前K线：最后一笔方向上的极端价出现在窗口末尾（当前笔终点之后）时，把终点推进到该极端价所在K线；
- `gapLocked` 笔不参与延伸。

**`lowerResOf(res) → string|null`**：逐级校准映射——D→240、240→60、60→15、15→3、其余 null。

**`calibrateBiTimes(bis, bigBars, refBars, bigIntervalSec) → bis`**
- 跨周期端点时间校准：大周期K线时间戳是 bar 起点，内部极值可能发生在更晚的低一级K线上；
- 对每个端点，在所属大周期K线区间内的低一级K线中找高低价与该端点价格一致的K线，把时间校准到该K线。

**`intervalSecOf(res) → number`**：周期→单根K线时长秒（3→180, 5→300, 15→900, 30→1800, 60/1H→3600, 240/4H→14400, D/1D→86400, W/1W→604800）。

### 2.8 MACD 背驰

**`biMacdMetrics(bi, macdArr) → {redArea, greenArea, difHigh, difLow, redMax, greenMax} | null`**
- 计算一笔的 MACD 指标：红柱面积（macd>0 部分累加）、绿柱面积（macd<0 部分绝对值累加）、DIF 高点/低点、红柱最大高度（单根柱最大值）、绿柱最大高度（单根柱绝对值最大值）；笔内无数据返回 null。

**`isBiDiverge(bi, refer, macdArr) → boolean`**（MACD 背驰判定，OR 关系满足其一）
- **底背驰**（对应一买，下跌笔）：绿柱面积变小（`cur.greenArea < ref.greenArea`）**或** 黄白线低点抬高（`cur.difLow > ref.difLow`）**或** 绿柱最大高度变小（`cur.greenMax < ref.greenMax`）；
- **顶背驰**（对应一卖，上涨笔）：红柱面积变小（`cur.redArea < ref.redArea`）**或** 黄白线高点变低（`cur.difHigh < ref.difHigh`）**或** 红柱最大高度变小（`cur.redMax < ref.redMax`）。

### 2.9 买卖点

**`findBuyPoints(bis, upperBis, macdArr, barSec) → points[]`**

- **1买**：下跌笔创新低（`cur.endPrice < refer.endPrice`，参照为之前最近的幅度 ≥ 当前 50% 的下跌笔）+ MACD 背驰；与上级笔完全重合的笔跳过（`isSameAsUpperBi`）；全部保留；
- **2买/类2买**（区间套）：在「上一级别上涨笔」段内找抬高低点——首个 `price > up.startPrice` 的下跌笔终点为 2买，其后更低的抬高低点为类2买；无上级笔时用「结构底」（最近一买之前或全窗口最低底）作为上涨段起点找抬高低点；
- **3买**：2买过后的上涨段未出现背驰（上涨笔创新高、突破前顶，`prevTop`=2买前最近上涨笔终点），其后的回调不破前顶（`bp > prevTop`）且位于上级上涨笔段内（`bt ∈ [up.startTime, up.endTime]` 且 `bp > up.startPrice`）；每段最多标一个；
- 输出类型：`{ type: "1买"|"2买"|"类2买"|"3买", time, price }`。

**`findSellPoints(bis, upperBis, macdArr, barSec) → points[]`**（与买点对称）
- **1卖**：上涨笔创新高 + MACD 背驰；**锚定**（`anchorFirstSell`）到上级上涨笔结束点，多个候选锚到同一位置去重；与上级笔完全重合跳过；
- **2卖/类2卖**：在「上一级别下跌笔」段内找次高点（首个 `price < dn.startPrice` 的上涨笔终点），其后更高的次高点为类2卖；无上级笔时用「结构顶」；
- **3卖**：2卖过后的下跌段未出现背驰（下跌笔创新低、跌破前底），其后的反弹不破前底且位于上级下跌笔段内（`sp < dn.startPrice`）；每段最多标一个。

**`anchorFirstBuy(cand, upperBis) → {time, price}|null`**
- 低级别一买锚定到「上一级别某笔的起点」（时间上最近的一个底部端点）；找不到返回 null。

**`anchorFirstSell(cand, upperBis) → {time, price}|null`**
- 一卖锚定到「上一级别上涨笔的结束点」：若候选位于某上级上涨笔内部 → 上移到该上涨笔结束点；否则取时间最近的顶部端点；找不到返回 null。

**`isSameAsUpperBi(bi, upperBis, barSec) → boolean`**
- 本周期某笔是否与上一级别某笔完全重合（起终点时间与价格一致，时间容差 = 本周期 1 个 bar、价格容差 0.01）；重合说明内部无更细结构，本周期不标记 1类点。

**`snapToOwnBar(price, refTime, bars) → time`**
- 把极值价格/时间映射到本周期K线的 bar 边界（找高低价匹配且时间最近的K线；找不到取时间最近K线）。

**`keepRecentEach(points, keep = 1) → points[]`**
- 每类买卖点只保留时间上最近 `keep` 个（默认 1，即每类只保留最近一个）；返回按时间升序排列。
- `keep === 1` 时每类只保留最近一个；`keep > 1` 时每类按时间倒序取最近 `keep` 个再升序返回。

## 3. 配置项

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `CHAN_CFG.gapFilter` | 1.0 | 跳空独立成笔阈值（相邻K线缺口 ≥ gapFilter×ATR 强制独立成笔） |
| `CHAN_CFG.debug` | false | 调试打印（buildBi / 买卖点识别过程） |

## 4. 依赖关系

| 上层模块 | 复用关系 |
|----------|----------|
| `chan-bi`（画笔） | `mergeBars`/`findFractals`/`countRaw`/`hasGapBetween`/`buildBi`/`fixBiExtremes`/`lockedPivotsOf`/`alignBiToUpper`/`calcATR`/`calcMACD`/`hasMacdCrossBetween`/`extendLastBi`/`lowerResOf`/`calibrateBiTimes`/`intervalSecOf` |
| `mark-buy-sell`（买卖点） | `mergeBars`/`findFractals`/`countRaw`/`hasGapBetween`/`buildBi`/`calcATR`/`calcMACD`/`hasMacdCrossBetween`/`extendLastBi`/`lowerResOf`/`calibrateBiTimes`/`intervalSecOf`/`fmtT`/`biMacdMetrics`/`isBiDiverge`/`findBuyPoints`/`findSellPoints`/`anchorFirstBuy`/`anchorFirstSell`/`isSameAsUpperBi`/`snapToOwnBar`/`keepRecentEach` |
| `mark-sr-flip`（支阻位） | `calcATR`/`fmtT` |
| `mark-entry`（进出场） | `calcATR`/`calcMACD`/`isBiDiverge`/`fmtT`/`lowerResOf` |

## 5. 边界情况

| 场景 | 处理 |
|------|------|
| 笔数 < 3 | `buildZS`/`findBuyPoints`/`findSellPoints` 返回空数组 |
| `atr` 为 0 或无数据 | `buildBi` gapThreshold=0（跳空判定关闭） |
| `macdArr` 为空 | `hasMacdCrossBetween` 返回 false；`isBiDiverge`/`biMacdMetrics` 返回 false/null |
| 上级笔为空 | `findBuyPoints`/`findSellPoints` 走「结构底/结构顶」分支（无区间套）；`anchorFirstBuy/Sell` 返回 null；`alignBiToUpper`/`buildZSByUpper` 退化为本级直接构建 |
| 三笔重叠不成立 | `buildZS` 滑窗继续扫描 |
| 中枢全部笔重叠后 `zg <= zd` | 防御性跳过该段 |
