# 支阻位标记功能规格（mark-sr-flip）

> 文档用途：作为 mark-sr-flip 脚本的**规格说明（spec）**，描述输入、处理规则、判定逻辑、输出与边界情况，使实现与使用有唯一共识。
> 对应脚本：`.cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js`（2026-09-08 三类型可配置 + 统一管线 + 按周期显示版本：密集区/黄金分割/BOLL 三类同池合并，`drawn`/`drawnFib` 改为 `drawnByPeriod`）。

## 1. 输入

| 项 | 来源 | 说明 |
|----|------|------|
| 笔数据 | `.cursor/cache/bis_<品种>.json`（chan-bi 画笔落盘） | 各周期笔列表，含 `startPrice`/`endPrice`/`startTime`/`endTime`/`type`；缺文件或品种不符则报错退出 |
| 买卖点 | 本脚本内现算（chan-core `findBuyPoints`/`findSellPoints`，买卖点不落盘） | 仅黄金分割位用：非一类点（2买/类2买/3买、2卖/类2卖/3卖） |
| K 线 | 从 TradingView 图表实时读取（`--from` 起 + 30 根缓冲） | 用于 ATR 计算、MACD（买卖点背驰判定）、突破判定、经过 K 线数统计 |
| 当前价 | 最小有数据周期最后一根 K 线收盘价 | 优先级：3 > 15 > 60 > 240 > D |
| 参数 | 命令行参数 | `--from` 必填；其余可选 |

### 参数表

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--from=YYYY-MM-DD` | 无（必填） | 起始日期，与画笔 chan-bi 一致 |
| `--periods` | `D,240,60,15,3` | 要标记的周期列表 |
| `--sr-types` | `cluster,boll` | 支阻位类型开关（逗号分隔，可选 `cluster`=密集区 / `fib`=黄金分割 / `boll`=布林带；全关报错退出） |
| `--fib-levels` | `0.382,0.5,0.618` | 黄金分割比率（逗号分隔，须在 (0,1) 内，去重升序；全非法回退默认；仅 fib 开启时生效） |
| `--boll-length` | `26` | BOLL SMA 周期（已收盘K线口径） |
| `--boll-mult` | `2` | BOLL 标准差倍数 |
| `--side-count` | `2` | 每周期图每侧条数（2 → 每图最多 4 条） |
| `--cluster` | `0.5` | 强支阻位聚类容差（×ATR） |
| `--merge` | `0.5` | 跨周期合并容差（×最小周期ATR；三类同池合并） |
| `--recent-cluster` | `1.0` | 近期极值位聚类容差（×ATR） |
| `--min-touch` | 按级别（D/240/60=4，15=3，3=8） | 强支阻位最少触及次数；显式指定则全局覆盖 |
| `--max-dist` | `3.0` | 选取距离上限（×线自身级别ATR） |
| `--max-per-period` | `50` | 每周期候选数量上限（超出按强度评分降序截断；仅约束密集区，fib/boll 豁免） |
| `--dry` | 关闭 | 只计算不绘图 |
| `--debug` | 关闭 | 打印调试信息 |

## 2. 候选识别（每个周期独立）

支阻位分三类来源，`--sr-types` 分别开关，可共存：

- **密集区（cluster）**：基于价格聚类的两类来源（§2.1 强支阻互换位、§2.2 近期极值位），走评分/截断，随后与 fib/boll 同池跨周期合并（§4）、按显示周期选取（§5.1）；
- **黄金分割（fib）**：非一类买卖点参照笔的回撤分割位（§2.4），豁免评分/截断，但**参与统一合并池**（§4）；
- **BOLL 布林带（boll）**：末根已收盘K线的布林带三轨（§2.6），豁免评分/截断，**参与统一合并池**（§4）。

### 2.1 强支阻互换位（密集区）

1. 从笔列表提取 swing 高低转折点（`extractSwingPoints`）：首尾相连，每笔终点即一次转折，另补第一笔起点；
2. 按价格聚类，相邻价差 ≤ `cluster × ATR` 的点并入同一价位簇（`clusterPoints`）；
3. 簇触及次数 `< minTouch` 则丢弃；
4. 互换判定（`detectFlip`）：
   - **情况 1**：簇内首尾触及角色相反（先高后低 → R2S，先低后高 → S2R），直接判定角色已反转，`breakTime = lastTouch`；
   - **情况 2**：角色未反转，按主导角色（高点≥低点 → 阻力；否则支撑），在末触之后找收盘价有效穿越（`bar.close > price + tol` → R2S；`bar.close < price - tol` → S2R），找到即判定。

### 2.2 近期极值位（`recent`，密集区）

- 取最近 `RECENT_BI_COUNT`（默认 20）根笔的 swing 端点（`extractRecentExtremes`）；
- 聚类容差放宽为 `recent-cluster × ATR`（默认 1.0），把同一天密集高低点聚成一条「近期价位区」；
- **不要求触及次数**；簇内同时有高、低点按首尾判定 R2S/S2R，否则记为纯阻力 RES / 纯支撑 SUP；
- 作用：弥补强支阻互换位在最新价格行为上的滞后（如 3 分钟 8-29 高开后 4467 一线形成的阻力）。

### 2.3 候选数量上限（数据层截断，仅密集区）

每周期识别出的密集区候选（强支阻互换位 + 近期极值位）最多保留 `--max-per-period`（默认 `50`）个：

- 超出上限时按「强度评分降序」截断，保留 Top N——保留市场测试/停留最多（最重要）的位置，
  丢弃最弱候选（如 3 分钟识别出的海量低分噪音位）；
- 评分沿用 `flipScore`，以该周期全部候选为归一化集（与选取阶段一致）；
- **fib / boll 豁免截断**（结构性豁免：根本不进截断输入）——fib 由结构点派生、boll 为当前带宽值，评分语义不适用；
- **只约束「标记/落盘」的数据层**（`periods`），**不影响显示层**（按显示周期就近选取，见 §5.1）；
- 跨周期合并后的 `merged` 不设上限（三类同池合并后保留全部，供 `mark-entry` 读取判断靠近支阻位）。

### 2.4 黄金分割支阻位（`fib`）

对每个周期、每个方向（买/卖），取**最新的非一类买卖点**，以其回调笔**紧邻前方的顺势笔**为参照笔，画经典回撤分割位：

1. **现算买卖点**（`findBuyPoints`/`findSellPoints`，买卖点不落盘）：
   - 买卖点在本周期 `endTime >= fromTs` 过滤后的笔上计算（与 mark-buy-sell 图上口径一致），
     上级笔取 `UPPER_OF` 映射周期（240→D、60→240、15→60、3→15）同样过滤后的笔，D 无上级；
   - MACD 由本周期 K 线现算（`calcMACD`）；
   - 非一类 = `2买/类2买/3买`（买向）、`2卖/类2卖/3卖`（卖向）；
     `真1买/真1卖` 是 mark-buy-sell 层后处理的合并标注，原始输出不存在，白名单过滤天然排除一类；
2. **每方向只取最新点**（`pickLatestFibPoint`）：白名单内 `time` 最大者（同 time 取数组靠后者）；无 → 该方向跳过；
3. **定位回调笔与参照笔**（`referBiOfPoint`，在本周期**未过滤的全量笔**上匹配——最新点的回调笔可能横跨 from 窗口边界）：
   - 回调笔 = `endTime === point.time && type === pullbackType` 的笔（买点 "down"、卖点 "up"）；
   - 参照笔 = 紧邻回调笔前方的反向笔（买点取前方上涨笔、卖点取前方下跌笔）；
   - 匹配不到 / 回调笔是首笔（无前方笔）/ 前一笔同向（笔应交替，同向为脏数据）→ 该方向跳过，**不回退更早点**；
4. **分割位**（`fibLevelsOf`）：
   - 买点（参照笔为上涨笔 L→H）：`H - r×(H-L)`，即回撤**支撑**（type=`SUP`）；
   - 卖点（参照笔为下跌笔 H→L）：`L + r×(H-L)`，即反弹**阻力**（type=`RES`）；
   - 比率 `r ∈ FIB_LEVELS`（默认 0.382/0.5/0.618）；span=0（退化笔）→ 无候选；
5. **候选字段**：`{ price, type: SUP|RES, fib: true, ratio, fromPoint: {type,time,price}, referBi: {startTime,endTime,startPrice,endPrice}, touchCount: 1, firstTouch: refer.startTime, lastTouch: breakTime: point.time, barsPassed }`
   （`touchCount=1` 仅为数据形状对齐；fib 不进任何评分组。绘线锚点 = 信号点时间）。

**统一合并池口径**（fib 参与 §4 跨周期合并，与 cluster/boll 同池同规则）：

- **豁免 capPerPeriod**：见 §2.3；
- **参与跨周期合并**：三类候选全部进 `mergeFlipsAcrossPeriods`——价差 ≤ 合并容差并成一条**位置线**；纯单来源 fib 独立线保留 `fib: true`、`ratio`、`fromPoint`、`referBi` 标记；与其它来源并簇的**混合线删除 fib/pending/boll 标记**，统一按「位置线」口径（来源标注文字 `位置线`）；
- **不走 pickByLevel 上下各1**：三条比率位是围绕同一信号点的成组结构，按上下各1选取会拆散；改为按显示周期就近选取（见 §5.1）；
- 同一买卖点的各比率位**互不聚类**（0.382/0.5/0.618 语义不同，不可平均）——除非与其它来源并簇（此时按位置线口径）。

### 2.5 预期回退（pending，2026-09-07 新增）

fib 锚定「上一个已确认点」属**滞后锚定**——对「等待2卖/2买」的决策窗口无用（点未形成则无位；240 级别上 2卖 更要等上级笔确认后才补出现）。pending 回退把锚定推进到**正在形成的下一个点**，与实盘口径一致（实盘即「预期位置 + 够笔/小级别背驰确认」）：

- **触发**：某方向（买/卖）**无已形成非一类点**时（每方向独立；有已形成点的方向不回退，预期位只补位）；
- **卖向**（`pendingReferOf`）：末笔为**形成中上涨笔**（bis 落盘口径：末笔延伸至最新极值）且其现高点 < 前方下跌笔起点（次高点结构未破坏）→ 参照笔 = 该下跌笔，预期阻力 = `L + r×(H-L)`；
- **买向**对称：末笔为形成中下跌笔且现低点 > 前方上涨笔起点 → 参照笔 = 该上涨笔，预期支撑 = `H - r×(H-L)`；
- **失效**：形成笔突破前方笔起点（次高点结构被市场否定）→ 条件不成立，下次重算自动消失；2卖/2买 真正形成后已确认 fib 接管（参照笔相同、价位连续）；
- **候选字段**：同已形成 fib + `pending: true`；`fromPoint.type = 预期2卖/预期2买`，`fromPoint.time = lastTouch = breakTime = 形成笔 endTime`（当前极值时间，每次重跑刷新）；
- **边界**：末笔是首笔（无前方笔）/ 前方笔同向（脏数据）/ span=0 → 该方向无预期位；
- **进 merged**：与已形成 fib 同等参与 mark-entry nearSr/止损参考（回测同步——增量重放中 pending 位随结构演变消失/重生，无未来函数）；
- 来源标注：pending 预期位标注为 `预期2卖`/`预期2买`（见 §5.2 来源标注）。

### 2.6 BOLL 布林带（`boll`）

每周期取**最后一根已收盘K线**的布林带上/中/下轨作为支阻位：

1. **口径**（`calcBOLL`）：剔除取数最后一根（形成中）K线后取末 `--boll-length`（默认 26）根收盘价，
   计算 SMA（中轨）与**总体标准差（÷N）**（与 TradingView 布林带同口径），上/下轨 = SMA ± `--boll-mult`（默认 2）× σ；
2. **三轨类型**（`buildBollCandidates`）：上轨 = 阻力 `RES`、下轨 = 支撑 `SUP`、中轨按现价侧（现价 ≥ 中轨 → `SUP`，否则 → `RES`）；
3. **候选字段**：`{ price, type, boll: "upper"|"mid"|"lower", touchCount: 1, barsPassed: 0, firstTouch/lastTouch/breakTime = 末根已收盘K线 time }`；
4. **边界**：bars < boll-length 的周期无布林位（正常降级，如日线窗口 13 根）；无 pending 概念；带宽值随每次重跑的最新已收盘K线刷新。

## 3. 强度评分（仅密集区）

每个密集区候选（含合并后）计算强度评分，用于 `capPerPeriod` 截断；**fib/boll 不参与评分**（由结构点/带宽派生，非市场测试强度）：

```
score = 0.6 × norm(touchCount) + 0.4 × norm(barsPassed)
```

- `touchCount`（权重 60%）：价位被 swing 端点命中的次数；
- `barsPassed`（权重 40%）：价位带 `price ± 聚类容差` 被多少根 K 线覆盖/穿越（含影线，`low ≤ price + tol && high ≥ price - tol`）；
- `norm()` = 同一级别候选集内 min-max 归一化，消除量纲差异；
- 权重常量：`TOUCH_WEIGHT = 0.6`、`BARS_WEIGHT = 0.4`。

## 4. 跨周期合并（三类同池）

- 三类候选（密集区截断后 + fib + boll）**全部**进 `mergeFlipsAcrossPeriods`，同一池、同一规则（不再有 fib 并行双轨）；
- 合并容差：`merge × 最小有数据周期 ATR`（默认 `0.5 × minAtr`）；
- 价差 ≤ 容差的候选合并：
  - 价格按触及次数加权平均；
  - `touchCount`、`barsPassed` 累加；
  - `sources` 记录来源周期；
  - `firstTouch` 取更早、`breakTime` 取更晚；
  - 类型冲突时以触及次数更多者为准；
- **多来源混合线**（`_kinds` 含 >1 种）删除 `fib/pending/ratio/fromPoint/referBi/boll` 标记，置 `srcType="mixed"`（来源标注文字 `位置线`）；**纯单来源独立线**保留标记，置 `srcType="cluster"|"fib"|"boll"`；
- `level`（主要来源级别）= 来源中**最大的级别**（大级别支阻位更重要，决定颜色与可见范围，不被小级别「淹没」）。

## 5. 选取与绘制

### 5.1 选取（`pickNearestForDisplay`，按显示周期）

对每个显示周期 L，候选池 = **该级别及以上级别的合并线**（`LEVEL_ORDER.indexOf(line.level) ≤ LEVEL_ORDER.indexOf(L)`，高级别线继承到低周期图），
取「距现价最近的上方 `sideCount` 条 + 下方 `sideCount` 条」（`--side-count` 默认 2 → 每图最多 4 条）：

1. **距离范围限制**：每条线距现价 ≤ `max-dist × 线自身级别ATR`（默认 `3.0 × ATR`；`periodAtrs[line.level]` 缺失时 `Infinity` 不限距），避免远古强位挤掉当前价附近支阻位；
2. **就近排序**：上方按 `price` 升序、下方按 `price` 降序各取前 `sideCount` 条（**纯距离最近**，不再按强度评分）；
3. **允许上下不对称**：一侧不足 `sideCount` 条时不补。

选取结果落盘为 `drawnByPeriod = { 周期: [line,...] }`（每条附 `label` 来源标注）。

### 5.2 绘制

- **所有线统一灰色 `#787B86` 实线**（`linestyle=0`），来源用 title/text 标注区分（不再按级别/来源配色）；
- 形状：`horizontal_line`，title = `SR_<来源标注>`、text = 来源标注（悬停可见；TV 若该线形不支持 text 则退化为仅 title）；
- **来源标注**（`labelOf` = `sourceLabelOf + "+" + periodNameOf(level)`）：
  - boll → `BOLL上轨`/`BOLL中轨`/`BOLL下轨`（如 `BOLL上轨+4小时`）；
  - fib 已形成 → `黄金分割<ratio>`（如 `黄金分割0.5+1小时`）；pending → `预期2卖`/`预期2买`（如 `预期2卖+240`）；
  - cluster → `密集区`（如 `密集区+15分钟`）；
  - mixed → `位置线`（如 `位置线+60`）；
- **周期中文名**：`D→日线、240→4小时、60→1小时、15→15分钟、3→3分钟、W→周线`；
- **可见范围**：每条线只在**其显示周期**可见（`srVisibilitySingle`，仅该周期），高级别线继承到低周期图时为各周期生成独立实例，互不重叠；
- 绘制前先清除所有 title 前缀为 `SR_` 的旧横线（兼容历史 `SR_FLIP`/`SR_FIB`）；
- 绘制后切回原周期。

## 6. 输出

1. **图上绘制**：按上述规则的 `horizontal_line`（统一灰色实线 + 来源标注）；
2. **缓存落盘** `.cursor/cache/srflip_<品种>.json`：
   - `periods`：各周期原始候选（含 `price`/`type`/`breakTime`/`touchCount`/`barsPassed`/`firstTouch`/`lastTouch`/`recent`；**密集区截断后 + fib + boll**，fib 候选额外含 `fib`/`ratio`/`fromPoint`/`referBi`，boll 候选含 `boll`）；
   - `merged`：**三类统一跨周期合并结果**（含 `sources`/`level`/`srcType`；下游 `mark-entry` 只读 `price`）；
   - `drawnByPeriod`：各显示周期选中的 ≤2×`sideCount` 条线（含 `label` 来源标注）；
   - 元信息：`from`/`currentPrice`/`minTouch`/`sideCount`/`recentBiCount`/`scoreWeights`/`maxDistAtr`/`maxPerPeriod`/`srTypes`/`fibLevels`/`bollCfg:{length,mult}` 等。

## 7. 边界情况

| 场景 | 处理 |
|------|------|
| 笔数据文件不存在或品种不符 | 报错退出，提示先运行「画笔」 |
| 某周期笔数 < 3 | 跳过该周期 |
| K 线读取失败 | 跳过该周期 |
| 数据未覆盖 `--from` 起始日期 | 自动滚动加载完整历史（`scrollToFirstBar`）重试 |
| 当前价未知 | 不生成 `drawnByPeriod`（无绘制） |
| 某周期 bars < boll-length | 该周期无 BOLL 位（正常降级） |
| 某级别某侧无候选 | 该侧不画（允许上下不对称） |
| 近期极值位与强支阻位同价位 | 跨周期/同级别合并时按价格并簇 |
| 某周期/某方向无非一类买卖点 | 该方向走预期回退（§2.5）生成 pending 预期位；结构不满足（末笔方向不符/首笔/脏数据/次高点破坏）则无，正常降级不报错 |
| 最新非一类点匹配不到参照笔（首笔/脏数据） | 该方向跳过，**不回退**更早点 |
| fib/boll 与密集区同价位 | **并簇为混合位置线**：merged 中合并为一条 `srcType="mixed"`（删除 fib/boll 标记，见 §4） |
| pending 预期结构被后续行情否定（形成笔突破前方笔起点） | 预期位条件失效，下次重算自动消失；已确认点形成后已形成 fib 接管 |
| `--sr-types` 全部关闭或全部非法 | 报错退出 |
| `--fib-levels` 全部非法 | 警告并回退默认 0.382/0.5/0.618 |

## 8. 与其他模块的依赖

| 模块 | 关系 |
|------|------|
| `chan-bi`（画笔） | **强制依赖**：读取其落盘笔数据；无笔数据不运行 |
| `chan-core` | 密集区仅复用 `calcATR` 等工具函数；黄金分割另复用 `findBuyPoints`/`findSellPoints`/`calcMACD`/`intervalSecOf`（现算非一类买卖点） |
| `mark-entry`（进出场） | **读取本脚本落盘 `srflip_<品种>.json` 的 `merged` 字段**判断「靠近支阻位」（三类来源，下游只读 `price` 自动兼容）；本脚本字段变更需保持 `merged` 结构兼容 |
| `py_chain/sr_flip.py` | Python 回测移植版，`compute_srflip` 同口径支持三类（`srTypes`/`fibLevels`/`bollLength`/`bollMult` 参数），保持回测与图表一致 |

**运行依赖链**（完整执行顺序，各技能依序运行）：

```
画笔（chan-bi）→ 标记买卖点（mark-buy-sell）→ 支阻互换位（mark-sr-flip）→ 交易计划（trading-plan）→ 进出场（mark-entry）
```

- 本 SKILL（支阻互换位）在链中位于标记买卖点之后、交易计划之前，只**强制依赖画笔**的笔数据；
- 落盘的 `srflip_<品种>.json` 供下游 `mark-entry` 判断「靠近支阻位」。
