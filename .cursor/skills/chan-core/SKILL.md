---
name: chan-core
description: 缠论算法核心库（唯一算法源），供 chan-bi（画笔）、chan-zs（中枢）与 mark-buy-sell（买卖点）三个 SKILL 共用。纯函数模块，不连接 CDP、不绘图。Use when 修改缠论算法规则（包含处理/分型/笔构建/中枢/MACD背驰/买卖点识别），或需要定位画笔与买卖点算法不一致的问题。
disable-model-invocation: true
---

# 缠论算法核心（chan-core）

**唯一算法源**：所有缠论算法（包含关系处理 → 分型识别 → 笔构建 → ATR/MACD → 背驰判定 → 买卖点识别 → 跨周期校准）都集中在本模块 `scripts/chan_core.js`。

> 这是本次重构（技能拆分）的核心：此前 `chan-bi`（画笔）与 `mark-buy-sell`（买卖点）各维护一份几乎相同的算法实现，改动容易不同步、难以维护。现在**两处都改为 `require` 本模块**，算法只有一份。

## 目录结构

```
.cursor/skills/chan-core/
└── scripts/
    └── chan_core.js    # 纯算法模块（无 CDP、无绘图依赖）
```

## 职责边界

| 模块 | 是否引用 chan-core | 职责 |
|------|------------------|------|
| `chan-bi`（画笔） | ✅ 引用 | 取K线 → 调算法算笔 → 绘制线段 → **落盘笔数据** |
| `chan-zs`（中枢） | ✅ 引用 | 读取 chan-bi 落盘的笔数据 → 调算法算中枢 → 绘制矩形 |
| `mark-buy-sell`（买卖点） | ✅ 引用 | 读取 chan-bi 落盘的笔数据 → 调算法算买卖点 → 绘制文字标记 |
| `chan-core`（本模块） | — | 纯算法，**不**连接 TradingView、**不**绘图、**不**读写缓存 |

## 使用方式

```javascript
const core = require("../../chan-core/scripts/chan_core.js");
core.CHAN_CFG.debug = true;        // 打印 buildBi / 买卖点识别过程
core.CHAN_CFG.gapFilter = 1.0;     // 跳空独立成笔阈值（×ATR）

const merged = core.mergeBars(rawBars);          // ① 包含关系处理
const fractals = core.findFractals(merged);      // ② 分型识别
const atr = core.calcATR(rawBars, 14);           // ATR
const macdArr = core.calcMACD(rawBars);          // MACD（含 dif/dea）
let bis = core.buildBi(fractals, merged, atr, macdArr); // ③ 笔构建
```

## 导出的函数

### K线/分型/笔

| 函数 | 说明 |
|------|------|
| `mergeBars(rawBars)` | 包含关系处理（合并K线），记录 `_rawCount`/`highTime`/`lowTime` |
| `findFractals(merged)` | 顶/底分型识别，`time` 取极值所在原始K线时间 |
| `countRaw(merged, aIdx, bIdx)` | 统计 (aIdx, bIdx] 覆盖的原始K线数 |
| `hasGapBetween(merged, aIdx, bIdx, atr, gapFilter)` | 区间内是否存在跳空缺口 |
| `buildBi(fractals, merged, atr, macdArr, lockedPivots, nearDouble, lowerContext)` | 笔构建（交替分型 + 回溯 + 跳空/MACD成笔 + 前顶前底作废 + 分型范围脱离 + 极值规则） |
| `calcATR(rawBars, period=14)` | ATR（平均真实波幅） |
| `calcMACD(rawBars)` | MACD（返回 `{time, macd, dif, dea}`，macd>0 红柱 / <0 绿柱） |
| `hasMacdCrossBetween(macdArr, merged, aIdx, bIdx, aTime, bTime)` | 区间内 MACD 红绿转换检测（用分型极值时间作边界） |

### 未完成笔延伸 / 周期映射 / 端点校准

| 函数 | 说明 |
|------|------|
| `extendLastBi(bisArr, bars)` | 未完成笔延伸（最后一笔推进到最新极端价） |
| `lowerResOf(res)` | 逐级校准映射：15分钟←3分钟，1小时←15分钟，4小时←1小时，日线←4小时 |
| `calibrateBiTimes(bis, bigBars, refBars, bigIntervalSec)` | 跨周期端点时间校准（用低一级K线确定极值精确位置） |
| `intervalSecOf(res)` | 周期 → 单根K线时长（秒） |

### MACD 背驰

| 函数 | 说明 |
|------|------|
| `fmtT(ts)` | 时间格式化（调试打印用） |
| `biMacdMetrics(bi, macdArr)` | 一笔区间内的 `{redArea, greenArea, difHigh, difLow, redMax, greenMax}` |
| `isBiDiverge(bi, refer, macdArr)` | 背驰判定：柱面积变小 **或** 黄白线动能减弱 **或** 柱最大高度变小（OR） |

### 买卖点识别

| 函数 | 说明 |
|------|------|
| `findBuyPoints(bis, upperBis, macdArr, barSec)` | 买点识别（1/2/3买/类2买，含区间套、MACD背驰、与上级笔重合跳过） |
| `findSellPoints(bis, upperBis, macdArr, barSec)` | 卖点识别（1/2/3卖/类2卖，对称逻辑） |
| `anchorFirstBuy(cand, upperBis)` | 一买锚定：取候选之前最近的上级底部端点 |
| `anchorFirstSell(cand, upperBis)` | 一卖锚定：候选在上级上涨笔内则上移到其结束点，否则取最近上级顶部端点 |
| `isSameAsUpperBi(bi, upperBis, barSec)` | 本笔与上级某笔完全重合判断（时间容差=本周期1个bar） |
| `snapToOwnBar(price, refTime, bars)` | 极值价格/时间映射到本周期bar边界 |
| `keepRecentEach(points)` | 低级别每类买卖点只保留最近一个（历史策略保留，当前主流程已不调用） |
| `keepRecentAll(points, keep=10)` | 买卖点不分类（买+卖合并）只保留时间上最近 keep 个（现行主流程保留策略，mark-buy-sell 每周期邻近合并后调用） |

### 缠论中枢

| 函数 | 说明 |
|------|------|
| `buildZS(bis, barSec)` | 构建笔中枢：三笔重叠形成 → 向后延伸 → 离开笔结束；垂直区间=全部笔重叠（ZG=min高点，ZD=max低点）；水平边缘=[进入笔终点−5×barSec, 离开笔起点+5×barSec]（barSec 为本周期单根K线秒数，左右各外扩5根K线）；至少 5 笔才输出 |
| `buildZSByUpper(lowerBis, upperBis, tolSec)` | 按上级笔分解构建中枢（分解原则不跨周期）：用上级笔时间区间把本级别笔切段，每段内独立运行 buildZS；tolSec 同时用作 buildZS 的 barSec（外扩时长） |

## 数据约定

- **笔对象字段**：`type(up/down)`、`startIdx/endIdx`（合并K线索引）、`startTime/endTime`（校准后端点时间）、`startPrice/endPrice`、`rawCount`（覆盖原始K线数）、`span`（幅度）、`gapLocked`（跳空成笔）、`macdCross`（MACD变色成笔）、`macdRaw`（MACD成笔覆盖原始K线数）
- **中枢对象字段**：`startTime/endTime`（水平边缘，已外扩）、`zd/zg`（中枢区间）、`dd/gg`（全部笔绝对低/高点）、`biCount`（笔数，含离开笔）、`extended`（是否延伸）、`exitTime`（离开笔起点）、`enterEndTime`（进入笔终点，外扩前）、`exitStartTime`（离开笔起点，外扩前）、`upperStart/upperEnd`（所属上级笔范围，buildZSByUpper 附加）
- **时间**：全部为 Unix 秒（UTC），与 TradingView K线时间一致
- **配置**：`CHAN_CFG.gapFilter`（跳空阈值，默认 1.0）、`CHAN_CFG.debug`（调试打印）

## 注意事项

- 本模块为纯函数库，**不产生任何外部副作用**（不连 CDP、不写文件、不绘图）；
- 修改算法规则时（如笔构建、中枢、背驰判定、买卖点定义），**只改本模块**，`chan-bi`、`chan-zs` 与 `mark-buy-sell` 会自动生效；
- 修改后建议用各 SKILL 的 `--dry` 模式回归验证输出与预期一致；
- `keepRecentEach` 为历史保留函数，主流程已不再调用；现行保留策略为 `keepRecentAll`（每周期不分类只保留最近 10 个标记，`--keep=N` 可调）。


## 一小时近等端点补充确认（2026-09-10）

原阈值 max(0.3×ATR, 0.001×价格) 保持。仅60分钟价差超过原阈值但不超过1.5倍时，可由15分钟双动能确认：后段同色柱峰值与同侧DIF极值绝对值均不超过前段50%，两段柱峰值均非零，双底DIF均负、双顶均正。保留平台间隔、原阈值反向波动、锁定端点和单次替换保护；数据不足不走补充分支，不改变买卖点创新极值背驰定义。

JS/Python的`buildBi`增加可选末参`lowerContext`；用`makeBiLowerContext(res,bars,cutoff,macd)`准备15分钟数据和MACD索引，缺省参数保持旧行为。新增配置`nearDoubleLowerRelax=1.5`、`nearDoubleLowerRatio=0.5`。

画笔构建60分钟前需要完整15分钟历史（最近30天仅限显示）；回测、回放和监控仅使用当前决策时刻已收盘数据，先更新低周期。目标小时K线为8月7日08:00、4229.875，15分钟精确极值为08:45；固定样本仅目标相邻两笔变化，实际全量影响需回归检查。

完整条件、接口、保护和测试见[现行补充规范](../../../spec/plans/SPEC_near_double_lower_confirmation.md)。早期章节中原阈值的说明描述基础分支，与本补充分支共同适用。
