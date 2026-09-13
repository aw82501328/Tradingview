# 支阻位标记功能规格（mark-sr-flip）

> 文档用途：作为 mark-sr-flip 脚本的**规格说明（spec）**，描述输入、处理规则、判定逻辑、输出与边界情况，使实现与使用有唯一共识。
> 对应脚本：`.cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js`（2026-09-12 取消跨周期合并版本：三类候选不合并、逐条展平为全量候选池，显示改为各周期独立选取；此前为 2026-09-08 三类同池合并 + 按周期显示版本）。

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
| `--sr-types` | `cluster,boll` | **叠加层**开关（逗号分隔，可选 `cluster`=密集区 / `fib`=黄金分割 / `boll`=布林带；全关报错退出）。2026-09-13 起 fib/boll 为独立叠加层，勾选即叠加到支阻位上，人工周期照样叠加 |
| `--manual` | 空 | **人工支阻位**（分号分周期、冒号后价位表：`60:4450,4460.5;D:1900`）：键存在 = 该周期支阻位来源=人工——替换该周期密集区、全部画出；**空价位 = 该周期没有支阻位**（不回退系统计算）；叠加层不受影响 |
| `--fib-levels` | `0.382,0.5,0.618` | 黄金分割比率（逗号分隔，须在 (0,1) 内，去重升序；全非法回退默认；仅 fib 开启时生效） |
| `--boll-length` | `26` | BOLL SMA 周期（已收盘K线口径） |
| `--boll-mult` | `2` | BOLL 标准差倍数 |
| `--side-count` | `2` | 每周期图每侧条数（2 → 每图最多 4 条） |
| `--cluster` | `0.5` | 强支阻位聚类容差（×ATR） |
| `--recent-cluster` | `1.0` | 近期极值位聚类容差（×ATR） |
| `--min-touch` | 按级别（D/240/60=4，15=3，3=8） | 强支阻位最少触及次数；显式指定则全局覆盖 |
| `--max-dist` | `3.0` | 选取距离上限（×本周期ATR） |
| `--max-per-period` | `50` | 每周期候选数量上限（超出按强度评分降序截断；仅约束密集区，fib/boll 豁免） |
| `--dry` | 关闭 | 只计算不绘图 |
| `--debug` | 关闭 | 打印调试信息 |

## 2. 候选识别（每个周期独立）

**支阻位来源按周期二选一**（2026-09-13 起）：

- **系统计算（默认）= 密集区（cluster）**：基于价格聚类的两类来源（§2.1 强支阻互换位、§2.2 近期极值位），走评分/截断；
- **人工输入（manual，`--manual` / Web `manualLevels`）**：手动价位直接作为该周期支阻位（§2.7），替换密集区、全部画出。

**叠加层**（`--sr-types` 开关，独立于支阻位来源、人工周期照样叠加、仍进候选池供 nearSr/止损参考）：

- **黄金分割（fib）**：非一类买卖点参照笔的回撤分割位（§2.4），豁免评分/截断，直接进全量候选池（§4）；
- **BOLL 布林带（boll）**：末根已收盘K线的布林带三轨（§2.6），豁免评分/截断，直接进全量候选池（§4）。

全部候选（密集区截断后 + fib + boll + manual）进全量候选池（§4）、按显示周期独立选取（§5.1）。

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
- 全量候选池 `merged` 不设上限（三类候选全部展平保留，供 `mark-entry` 读取判断靠近支阻位）。

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

**候选池口径**（fib 直接进 §4 全量候选池，与 cluster/boll 同池、不合并）：

- **豁免 capPerPeriod**：见 §2.3；
- **不合并**：fib 候选独立成线，保留 `fib: true`、`ratio`、`fromPoint`、`referBi` 标记，价格=原始分割位（同价位的 cluster/boll 候选也各自保留，不并条、不平均）；
- **不走按级别评分选取**：三条比率位是围绕同一信号点的成组结构，按上下各1评分选取会拆散；改为按显示周期就近选取（见 §5.1）；
- 同一买卖点的各比率位**互不聚类**（0.382/0.5/0.618 语义不同，不可平均）。

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

### 2.7 人工支阻位（`manual`，2026-09-13 新增）

**Web `/sr` 调试页**：周期表「支阻来源」列逐周期选 系统计算/人工输入，选人工展开价位输入（逗号/空格分隔，配置键 `manualLevels = {周期: 价位文本}`，服务端解析）；**JS CLI**：`--manual=60:4450,4460.5;D:1900`。

- **键存在 = 该周期支阻位来源=人工**：替换该周期密集区计算（`buildManualCandidates`）；
  **叠加层（fib/BOLL）独立**，人工周期照样生成并按就近选取叠加；
- **全部画出**：人工候选不受 `sideCount` 与距离上限限制，输入几条画几条（就近选取池不含人工候选）；
- **type 按现价侧推导**（`buildManualCandidates`，仿 boll 中轨）：现价 ≥ 价位 → `SUP`，否则 → `RES`；currentPrice 未知时统一 `RES`；
- **空列表 = 该周期没有支阻位**（不画线、不进候选池，**不回退**系统计算——选了人工就是人工）；
- 人工周期**不要求 bis≥3**（无笔也能用），但仍需 K线（ATR/末收盘价/锚点）；
- 候选字段：`{ price, type, manual: true, touchCount: 1, barsPassed: 0, firstTouch/lastTouch/breakTime = 末根已收盘K线 time }`；`srcType="manual"`，标注 `手动位+<周期中文名>`；
- 豁免 `capPerPeriod`（无密集区可截）；进 `merged` 供 mark-entry nearSr/止损参考（下游只读 `price`）；
- 校验（Web `normalize_sr_cfg`）：周期键别名归一（1H→60 等）；未勾选周期键丢弃；字符串/列表 → 正有限 float 去重升序，上限 50 条；空值归一为空列表（不报错）。

## 3. 强度评分（仅密集区）

每个密集区候选计算强度评分，用于 `capPerPeriod` 截断；**fib/boll 不参与评分**（由结构点/带宽派生，非市场测试强度）：

```
score = 0.6 × norm(touchCount) + 0.4 × norm(barsPassed)
```

- `touchCount`（权重 60%）：价位被 swing 端点命中的次数；
- `barsPassed`（权重 40%）：价位带 `price ± 聚类容差` 被多少根 K 线覆盖/穿越（含影线，`low ≤ price + tol && high ≥ price - tol`）；
- `norm()` = 同一级别候选集内 min-max 归一化，消除量纲差异；
- 权重常量：`TOUCH_WEIGHT = 0.6`、`BARS_WEIGHT = 0.4`。

## 4. 全量候选池（不合并）

2026-09-12 起取消跨周期合并（原 `mergeFlipsAcrossPeriods` 移除），改为逐条展平：

- 全部候选（密集区截断后 + fib + boll + manual）进 `flattenCandidates` 展平为全量候选池 `merged`：每条候选独立成线，**价格=原始识别价**（不做任何加权平均）；
- **同价位不并条**：不同周期、不同来源识别出的同/近价位候选各自保留为独立条目（`touchCount`/`barsPassed` 不累加，无 `sources` 聚合）；
- 每项附加两个字段：`level`（候选自身周期）与 `srcType`（`"cluster"|"fib"|"boll"|"manual"`，由自身标记反推；不再有 `"mixed"`）；
- fib/boll 标记**永不清理**（原混合线删标记的规则随合并一并移除）；
- 排序：先按 `LEVEL_ORDER` 级别序（大→小，未知键落尾），再按 `price` 升序（确定性输出）。

## 5. 选取与绘制

### 5.1 选取（`pickNearestForDisplay`，按显示周期，各周期独立）

对每个显示周期 L，候选池 = **仅 L 自身周期的非人工候选**（`merged` 中 `line.level === L && !line.manual`，不继承其它周期线），
取「距现价最近的上方 `sideCount` 条 + 下方 `sideCount` 条」（`--side-count` 默认 2 → 每图最多 4 条）：

1. **距离范围限制**：每条线距现价 ≤ `max-dist × 本周期ATR`（默认 `3.0 × ATR`；`periodAtrs[L]` 缺失时 `Infinity` 不限距），避免远古强位挤掉当前价附近支阻位；
2. **就近排序**：上方按 `price` 升序、下方按 `price` 降序各取前 `sideCount` 条（**纯距离最近**，不按强度评分）；
3. **允许上下不对称**：一侧不足 `sideCount` 条时不补；本周期无候选则该图不画线；
4. **人工周期覆写**：`drawnByPeriod[L] = 全部人工候选（不受条数/距离限制）+ 叠加层就近结果`；currentPrice 未知时不画（与 boll 中轨降级口径一致）。

选取结果落盘为 `drawnByPeriod = { 周期: [line,...] }`（每条附 `label` 来源标注）。

### 5.2 绘制

- **所有线统一灰色 `#787B86` 实线**（`linestyle=0`），来源用 title/text 标注区分（不再按级别/来源配色）；
- 形状：`horizontal_line`，title = `SR_<来源标注>`、text = 来源标注（悬停可见；TV 若该线形不支持 text 则退化为仅 title）；
- **来源标注**（`labelOf` = `sourceLabelOf + "+" + periodNameOf(level)`）：
  - boll → `BOLL上轨`/`BOLL中轨`/`BOLL下轨`（如 `BOLL上轨+4小时`）；
  - fib 已形成 → `黄金分割<ratio>`（如 `黄金分割0.5+1小时`）；pending → `预期2卖`/`预期2买`（如 `预期2卖+240`）；
  - cluster → `密集区`（如 `密集区+15分钟`）；
- **周期中文名**：`D→日线、240→4小时、60→1小时、15→15分钟、3→3分钟、W→周线`；
- **可见范围**：每条线只在**其显示周期**可见（`srVisibilitySingle`，仅该周期），各周期图互不重叠；
- 绘制前先清除所有 title 前缀为 `SR_` 的旧横线（兼容历史 `SR_FLIP`/`SR_FIB`）；
- 绘制后切回原周期。

## 6. 输出

1. **图上绘制**：按上述规则的 `horizontal_line`（统一灰色实线 + 来源标注）；
2. **缓存落盘** `.cursor/cache/srflip_<品种>.json`：
   - `periods`：各周期原始候选（含 `price`/`type`/`breakTime`/`touchCount`/`barsPassed`/`firstTouch`/`lastTouch`/`recent`；**密集区截断后 + fib + boll + manual**，fib 候选额外含 `fib`/`ratio`/`fromPoint`/`referBi`，boll 候选含 `boll`，人工候选含 `manual`）；
   - `merged`：**全量候选池（展平不合并）**（每项含 `level`/`srcType`，价格=原始识别价；下游 `mark-entry` 只读 `price`）；
   - `drawnByPeriod`：各显示周期选中的 ≤2×`sideCount` 条线（人工周期=全部人工候选+叠加层就近结果，含 `label` 来源标注）；
   - 元信息：`from`/`currentPrice`/`minTouch`/`sideCount`/`recentBiCount`/`scoreWeights`/`maxDistAtr`/`maxPerPeriod`/`srTypes`/`manualLevels`/`fibLevels`/`bollCfg:{length,mult}` 等。

## 7. 边界情况

| 场景 | 处理 |
|------|------|
| 笔数据文件不存在或品种不符 | 报错退出，提示先运行「画笔」 |
| 某周期笔数 < 3 | 系统周期跳过；**人工周期不受此限**（仍需 K线） |
| K 线读取失败 | 跳过该周期 |
| 数据未覆盖 `--from` 起始日期 | 自动滚动加载完整历史（`scrollToFirstBar`）重试 |
| 当前价未知 | 不生成 `drawnByPeriod`（无绘制，人工周期同样不画） |
| 某周期 bars < boll-length | 该周期无 BOLL 位（正常降级） |
| 某级别某侧无候选 | 该侧不画（允许上下不对称） |
| 某周期无候选 | 该图不画线（各周期独立，不继承其它周期线） |
| 某周期/某方向无非一类买卖点 | 该方向走预期回退（§2.5）生成 pending 预期位；结构不满足（末笔方向不符/首笔/脏数据/次高点破坏）则无，正常降级不报错 |
| 最新非一类点匹配不到参照笔（首笔/脏数据） | 该方向跳过，**不回退**更早点 |
| fib/boll 与密集区（或其它周期）同/近价位 | **不并条**：各自独立成线进 merged（价格原样，见 §4） |
| pending 预期结构被后续行情否定（形成笔突破前方笔起点） | 预期位条件失效，下次重算自动消失；已确认点形成后已形成 fib 接管 |
| 人工输入未填价位（空列表） | **该周期没有支阻位**（不画线、不进候选池，不回退系统计算；叠加层照常） |
| Web manualLevels 值非法（非数字/超 50 条） | 400 报错（错误信息含周期名）；未勾选周期的键静默丢弃 |
| `--sr-types` 全部关闭或全部非法 | 报错退出 |
| `--fib-levels` 全部非法 | 警告并回退默认 0.382/0.5/0.618 |

## 8. 与其他模块的依赖

| 模块 | 关系 |
|------|------|
| `chan-bi`（画笔） | **强制依赖**：读取其落盘笔数据；无笔数据不运行 |
| `chan-core` | 密集区仅复用 `calcATR` 等工具函数；黄金分割另复用 `findBuyPoints`/`findSellPoints`/`calcMACD`/`intervalSecOf`（现算非一类买卖点） |
| `mark-entry`（进出场） | **读取本脚本落盘 `srflip_<品种>.json` 的 `merged` 字段**判断「靠近支阻位」（全量候选池，下游只读 `price`）；本脚本字段变更需保持 `merged` 为「含 `price` 的列表」形态 |
| `py_chain/sr_flip.py` | Python 回测移植版，`compute_srflip` 同口径支持三类系统来源 + 人工位（`srTypes`/`fibLevels`/`bollLength`/`bollMult`/`manualLevels` 参数，同样不合并、各周期独立选取、人工全部画出），保持回测与图表一致；Web `/sr` 调试页的「支阻来源」列与预设（`manualLevels`）走 `normalize_sr_cfg`+`engine_kwargs_of` 同一链路 |

**运行依赖链**（完整执行顺序，各技能依序运行）：

```
画笔（chan-bi）→ 标记买卖点（mark-buy-sell）→ 支阻互换位（mark-sr-flip）→ 交易计划（trading-plan）→ 进出场（mark-entry）
```

- 本 SKILL（支阻互换位）在链中位于标记买卖点之后、交易计划之前，只**强制依赖画笔**的笔数据；
- 落盘的 `srflip_<品种>.json` 供下游 `mark-entry` 判断「靠近支阻位」。
