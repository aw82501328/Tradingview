# chan-core 缠论算法核心规格（SPEC）

> 文档用途：chan-core 是**唯一缠论算法源**，纯函数模块（不依赖 CDP、不绘图），被 `chan-bi`（画笔）、`mark-buy-sell`（买卖点）两个 SKILL 复用，`mark-entry`/`mark-sr-flip` 复用其工具函数。本规格描述其全部导出函数的行为契约，是上层 SKILL 规格的基础。
> 对应脚本：`.cursor/skills/chan-core/scripts/chan_core.js`（1612 行）。

## 1. 统一约定

| 项 | 约定 |
|----|------|
| 笔对象字段 | `type`(up/down)、`startIdx/endIdx`(合并K线索引)、`startTime/endTime`(校准后端点时间)、`startPrice/endPrice`、`rawCount`(覆盖原始K线数)、`span`(幅度)、`gapLocked`(跳空成笔)、`macdCross`(MACD变色成笔) |
| 时间 | 全部 Unix 秒（UTC），与 TradingView K线时间一致 |
| 配置 | `CHAN_CFG.gapFilter`（跳空独立成笔阈值，默认 1.0）、`CHAN_CFG.wickRatio`（长影压平影线占比阈值，默认 0.70）、`CHAN_CFG.wickAtrK`（影线绝对长度下限系数，默认 0.5）、`CHAN_CFG.divergeDurRatio`（背驰面积判据时长可比上限，默认 3）、`CHAN_CFG.nearDoubleAtrK/nearDoublePct`（近等双顶平台取后顶阈值，默认 0.3/0.001）、`CHAN_CFG.debug`（调试打印） |
| 合并K线字段 | 含 `_rawCount`(覆盖原始K线数)、`highTime/lowTime`(极值原始K线时间)、`rawHigh/rawLow/rawHighTime/rawLowTime`(覆盖原始K线真实极值及时间)、`_topCand/_topCandTime`(覆盖范围内「可成顶分型的长影 bar」的影线端点价及所在原始K线时间)、`_origLow/_origLowTime`(覆盖范围内 markWickBars 压平的长下影真低及所在原始K线时间，供 `fixBiExtremes` 端点恢复) |

## 2. 导出函数清单

### 2.0 长影线处理（冲高/探底插针）

**`markWickBars(rawBars) → bars[]`**（须在 `mergeBars` 之前调用）
- **稳定波动基准**：影线绝对长度下限使用**全窗口 TR 均值**（非 `calcATR` 的尾部 14 根——3分钟仅 42 分钟，行情急涨段会使 ATR 数倍放大（实测 3.4→9.0），导致同一插针在平静行情被剔、急涨行情保留，剔除结果随行情抖动）；
- 判定（每根K线独立）：上影 = `high − max(open, close)`、下影 = `min(open, close) − low`、振幅 = `high − low`；
  - 上影 ≥ `wickRatio`×振幅 且 ≥ `wickAtrK`×稳定ATR → **长上影（冲高插针）**；
  - 否则下影满足同条件 → **长下影（探底插针）**；十字星/普通K线不受影响（同根K线两影不可能同时 ≥70%）。
- **长上影处理（high 一律压平至实体顶，保持历史验收的合并/笔结构——影线价参与合并会改变结构或污染笔区间）**：
  - 若该 bar 的 `low ≥ 左右相邻原始K线低点`（保留影线价可成为顶分型中心端点）→ 记 `_topCand = 原 high`，`findFractals` 在该 bar（或其合并 bar）成为顶分型中心时用 `_topCand` 作端点价与时间（**影线可成端点**）；
  - 否则（low 条件不满足——冲高插针本就不成顶分型）→ 纯压平，影线价不出现、不阻止后续合法顶成笔；
- **长下影处理**：low 压平至实体底（结构/区间竞争保持压平语义，不产生候选价）；压平前把原低记入 `_origLow = 原 low`、`_origLowTime = 该 bar 时间`，随 `mergeBars` 包含传播（§2.1），由 `fixBiExtremes` 恢复为更低的真实笔底端点（§2.4 方向A）——压平只作用于结构，不销毁端点恢复通道。
  - 恢复触发条件：`_origLow` 是某下跌笔终点分型之后、下一笔顶分型之前区间内的**绝对最低**（比笔底更低），如 1h 9-2 11:00 bar（下影 93%，真低 4282.625 < 笔底 4287.27）。若不恢复，笔底虚高会派生伪 2买/类2买（4287.27 被误判为高于 4h 段起点 4282.625 的回踩低点）；
  - 探底端点（如 60m 7-29 4010.41、7-15 16:00 底）不受影响——其 bar 影线占比不足，或由分型/端点修正（rawLow 通道）正常产生；
  - 与上影不对称：上影刻意不回 rawHigh（4443.715 先例，见下），下影真低只走 `_origLow`→`fixBiExtremes` 恢复通道，**不写入 rawLow/rawHigh**——跳空检测（§2.3）保持压平语义。
- **案例**（规则动机）：
  - 60m 7-16 02:00 bar（O4062.41 H4081.52 L4058.10 C4059.29，上影 81.6%）：low 4058.10 > 01:00 L4033.11 且 > 03:00 L4048.10 → `_topCand = 4081.52`，成为 7-15 反弹笔（4017.475→4081.52）的真实顶；
  - 15m 9-3 16:00 bar（O4428.86 H4443.715 C4431.405，上影 79%）：low 4428.135 < 16:15 bar low 4430.735 → 纯压平——插针不成端点，也不会阻止 17:00 顶 4442.04 成笔；
  - 60m 8-28 22:00 bar（H4631.98，上影 27.6% 不触发）与 240 同型冲高 → 不涉及本函数，由 §2.4 `fractalRangeClear` 治理。

### 2.1 包含关系处理

**`mergeBars(rawBars) → merged[]`**
- 相邻K线有包含关系时合并，方向由前序趋势决定：向上合并取「高高」，向下合并取「低低」；
- 每根合并K线记录 `_rawCount`、`highTime/lowTime`（合并后的极值时间）、`rawHigh/rawLow/rawHighTime/rawLowTime`（覆盖原始K线的**真实**极值，供跳空检测与端点修正）；
- 覆盖范围内若含带 `_topCand` 的长影 bar（见 §2.0），`_topCand` 取覆盖 bar 的最大值、`_topCandTime` 取对应 bar 时间，随合并传播；
- 覆盖范围内若含带 `_origLow` 的探底插针 bar（见 §2.0），`_origLow` 取覆盖 bar 的最小值、`_origLowTime` 取对应 bar 时间，随合并传播（只进端点恢复通道，不影响 `rawLow/rawHigh`）；
- 方向确定：首根后若前序无方向，用前两根合并K线高低比较（`last.high >= prev.high ? 1 : -1`）；首根默认向上（`dir=1`）。

**`countRaw(merged, startIdx, endIdx) → number`**
- 统计 `(startIdx, endIdx]` 覆盖的原始K线数（各合并K线 `_rawCount` 累加）。

### 2.2 分型识别

**`findFractals(merged) → fractals[]`**
- 顶分型：`cur.high > prev.high && cur.high > next.high && cur.low > prev.low && cur.low > next.low`，`time` 取最高价原始K线时间；
- 底分型：`cur.low < prev.low && cur.low < next.low && cur.high < prev.high && cur.high < next.high`，`time` 取最低价原始K线时间；
- 字段：`{ mergedIdx, type(top/bottom), high, low, time }`；
- **顶分型端点价/时间**：中心合并K线若带 `_topCand` 且 `_topCand > cur.high`（覆盖范围内含「可成顶分型的长影 bar」，见 §2.0）→ `high = _topCand`、`time = _topCandTime`——影线价只在该 bar 成为分型中心端点时生效，结构本身保持压平版。

### 2.3 跳空检测

**`hasGapBetween(merged, aIdx, bIdx, atr, gapFilter) → boolean`**
- 检测 `[aIdx, bIdx)` 相邻合并K线之间是否存在跳空缺口；
- 用覆盖原始K线的**真实极值**（`rawHigh/rawLow`）判断，避免合并K线（向下合并压低高点/向上合并抬高低点）造成「假缺口」；
- 向上跳空：`nextLow - curHigh >= atr * gapFilter`；向下跳空：`curLow - nextHigh >= atr * gapFilter`。

### 2.4 笔构建

#### 2.4.0 规则速查总表（含适用周期）

> 缠论处理全链规则一览（细节见 §2.0~2.4 正文）。「全部」指 D/240/60/15/3 各周期一致生效；个别规则按周期或调用方式区分，已标注。`--atr/--gap` 为 chan-bi CLI 可调参数。

| # | 规则 | 触发条件（简） | 效果 / 约束 | 适用周期 |
|---|---|---|---|---|
| 1 | 包含关系合并（`mergeBars`） | 相邻K线存在包含，方向由前序趋势定 | 向上取高高 / 向下取低低；`rawLow/rawHigh` 记录覆盖真极值 | 全部 |
| 2 | 长影处理（`markWickBars`） | 上影 ≥70% 且 ≥0.5×稳定ATR；下影对称 | 冲高插针压平 high（`_topCand` 可成顶端点）/ 探底插针压平 low（`_origLow` 供端点恢复）；插针不污染结构 | 全部 |
| 3 | 分型识别（`findFractals`） | 中K高于/低于左右（价 + 反向区间双侧条件） | 顶/底分型；`_topCand` 影线价可作端点价 | 全部 |
| 4 | 阶段一 严格交替序列 | 连续同类型分型 | 顶取最高、底取最低；`lockedPivots` 命中标记 `locked` | 全部 |
| 5 | 同类型更极端替换 | 后顶 ≥ 前顶 / 后底 ≤ 前底 | 端点更新为突破极值（`locked`/`gapLocked` 除外） | 全部 |
| 6 | **近等双顶/双底平台取后顶/后底**（见下） | 后点略不极端且差 ≤ max(0.3×ATR, 0.1%×价)；last→k 间**所有相邻分型间隔 <4**（拆不出笔的平台/直拉）；中间含 ≥阈值回调；非 locked/gapLocked/macdCross；单跳封顶 | 笔端点取更晚的后顶/后底，前顶/前底视为插针性极值（走势终完美） | **仅 ≥1h（60/240/D）**，由调用方传 `nearDouble` 开启 |
| 7 | MACD 端点让位 | `result[-2]` 为 MACD 变色端点且 k 更极端 | k 顶替 `result[-2]` 移除中间分型 | 全部 |
| 8 | 跳空独立成笔 | 相邻合并K线缺口 ≥ `gapFilter`×ATR | 强制成笔并锁定端点；后续突破锁定价才解锁 | 全部（`--gap` 可调） |
| 9 | 最小间隔 | 合并K线间隔 ≥4（覆盖原始K线 ≥5） | 不足不成笔（进入回溯/MACD/作废分支） | 全部 |
| 10 | 前顶/前底作废（弱分型突破） | 脆弱端点 + 浅回调 <50% + last 为弱分型（MACD 变色且 raw<5） | k 顶替 prev2、移除中间 last | 全部 |
| 11 | 最小间隔脆弱笔例外 | `prev→last` 间隔恰 4 且回调/反弹浅（<50%） | 该笔未确认，允许被后续更极端分型顶替（上涨/下跌延伸） | 全部 |
| 12 | 回溯替换保护 | last 比 `result[-3]` 更极端 | 保留 last 为端点、作废 prev、k 暂不接入（笔内极值与区间套一致） | 全部 |
| 13 | 分型范围双向检查（`fractalRangeClear`） | 顶/底分型范围未脱离（互相包含） | 不成笔，等待更极端分型 | 全部 |
| 14 | 笔内极值（`noMoreExtremeInside`） | 笔区间内藏更极值点 | 不成笔 | 全部 |
| 15 | MACD 变色成笔 | 间隔恰 3（合并 4 根）+ 方向性红绿转换 + 无更极值 + raw ≥4 | 允许不足 4 间隔成笔（`macdCross`，端点让位规则仍适用） | 全部 |
| 16 | 未完成笔延伸（`extendLastBi`） | 末端单调上涨/下跌无新分型 | 最后一笔延伸到最新极端K线 | 全部 |
| 17 | ATR 噪音过滤 | 笔幅度 < 0.5×ATR（稳定全窗口均值基准） | 剔除噪音小笔 | 全部（画笔层，`--atr` 可调） |
| 18 | 端点极值修正（`fixBiExtremes`） | `rawLow/_origLow < low` 且比端点更极端 | 笔终点平移到被合并/压平掩盖的真低（底分支含分型中心） | 全部 |
| 19 | 逐级端点时间校准（`calibrateBiTimes`） | 用低一级周期K线定位极值时间 | 端点时间精确落在低一级 bar 上 | 15←3、60←15、240←60、D←240（3m 无） |
| 20 | 区间套强制对齐（`alignBiToUpper`） | 上级笔端点在本级须复现 | 端点与上级极值重合；断口合并 + 补幅度过滤治理缝合 | 全部（非最外层，依上级笔） |
| 21 | 小周期绘制窗口 | 只保留窗口内结束的笔 | 避免图上过密 | 仅 15m（30 天）/ 3m（15 天）——画笔层 |
| 22 | 背驰面积时长可比门（`divergeDurRatio`） | 两段时长比 >3（或某段 0/负） | 面积 Σ 不计入背驰，仅用 DIF/柱高判据 | 全部（`isBiDiverge`，买卖点/计划层） |

**`buildBi(fractals, merged, atr, macdArr, lockedPivots, nearDouble) → bis[]`**（`nearDouble` 默认 falsy：关闭规则 #6）

三阶段：
1. **阶段一（交替分型序列）**：连续同类型分型取更极端者（顶取最高、底取最低），得到严格交替序列；区间套强制对齐：与 `lockedPivots`（上级笔端点）方向一致且价格一致的分布标记 `locked`；
2. **阶段二（回溯替换）**，按顺序处理每个分型 k，规则优先级如下：
   - 同类型分型：`locked` 端点不可替换；非 gapLocked 时更极端才替换；gapLocked 端点仅当后续突破锁定价格时替换；
   - **近等双顶/双底平台取后顶/后底**（仅当 `nearDouble=true`，chan-bi 对 ≥60m 开启）：k 与 last 同类型、k 略不极端（差 ≤ max(`nearDoubleAtrK`×ATR, `nearDoublePct`×价)）、last→k 间所有相邻分型间隔 <4（整段为拆不出笔的平台/直拉，段内无任何可确认回调 → 走势未完美）、且中间存在 ≥ 同阈值的真实回调分型，且 last 非 `locked`/`gapLocked` 且未被本规则替换过（单跳封顶）→ 用后顶/后底 k 替换 last，前顶/前底视为插针性极值不终止本段。`macdCross` 端点不豁免（该端点本就是间隔不足靠 MACD 变色凑出的脆弱顶/底，如 1h 8-31 顶 4464.23，与近等平台取后顶语义一致）。例：1h 8-31 19:00 顶 4464.23 → 9-1 08:00 顶 4461.7（差 2.53 ≤ 8.05），12h 平台（4415.75~4464）全程分型间隔 <4。单跳封顶防平台内连续近等端点累积漂移（实测 3 跳累计可超 1×ATR）；15m/3m 平台尾噪声多（实测 61 次触发/半数端点重排）不启用；
   - **MACD 端点让位**：`result[-2]` 是 MACD 变色端点且 k 更极端 → k 顶替 `result[-2]`、移除中间分型；
   - **跳空成笔**：`last→k` 间存在 ≥ `gapFilter*ATR` 跳空 → 强制独立成笔（`k.gapLocked = true`）；
   - **前顶/前底作废**：`prev2` 与 k 同类型、`prev2→last` 不构成有效笔（间隔不足）、k 更极端、last 不得比 `prev3` 更极端（否则 last 是深回调的真实转折）、回调/反弹 < 50% 幅度、last 是弱分型（MACD变色且原始K线<5根）、`last`/`prev2` 均未锁定时，k 顶替 `prev2` 移除 last；
   - **有效笔接入**：`isValid(last, k)`（合并K线间隔 ≥ 4，即合并后至少 5 根）且（`noMoreExtremeInside` 或 last.gapLocked）且（`fractalRangeClear` 或 last.gapLocked）→ 接入；
   - **间隔不足 + MACD 变色**：`gap === 3` 且方向性红绿转换（底到顶绿变红/顶到底红变绿）且无更极值 → MACD 变色成笔（`k.macdCross = true, k.macdRaw = countRaw`）；
   - **回溯替换**：间隔不足且无 MACD 变色，k 与 `result[-2]` 同类型且更极端、且 `prev→last` 不构成有效笔时：若 last 比 `result[-3]` 更极端（回溯替换保护，保证区间套一致性）→ 保留 last 取代 `result[-3]`、作废 prev、暂不接入 k；否则 k 顶替 prev；
     - **最小间隔脆弱笔例外**：`prev→last` 虽构成有效笔，但间隔恰为最小值（`gapPrevLast === 4`，刚够 5 根合并K线）且回调/反弹浅（< 前段涨跌幅的 50%，前段 = `result[-3]` 极值到 prev）时，该笔尚未被确认——随后 k 即创更高顶/更低底说明整段仍是同一笔的延伸（缠论：顶被更高顶突破即作废），prev 仍被 k 顶替。例：15m 9-3 顶 4496.01(21:03)→底 4466.02 间隔恰 4、回调 39%（浅），23:15 新高 4510.93 顶替前顶、上涨笔延伸至 4510.93（与 60m/3m 端点一致）；对称场景 8-19 底 4327.27 被 09:00 更低底 4324.68 顶替。8-20 04:00 顶→20:00 底（间隔 11 根）等坚实笔、深回调（≥50%）场景不受影响；
3. **阶段三（两两连笔）**：相邻端点连笔，计算 `rawCount`（`countRaw`）、`span`（`|endPrice-startPrice|`）。

辅助判定：
- `isValid(a,b)`：`b.mergedIdx - a.mergedIdx >= 4`；
- `noMoreExtremeInside(a,b)`：笔内（`a.mergedIdx+1 .. b.mergedIdx-1`）不存在比端点更极端的点（严格比较，无容差）；
- `fractalRangeClear(a,b)`（分型范围双向检查，被阶段二主分支与前顶作废判定共用）：
  - **起点侧「与段同侧的两根」**（排除段外反向结构 bar）——顶→底（下跌笔）用 `min(中心, 右 bar).low`：下跌只需跌破「顶分型及之后」的结构低点，顶分型**左 bar**（顶之前主升前夜低点）不抬高"必须跌破"的阈值——否则误杀健康反弹底（60m 7-14 20:00 顶 4104.05 的左 bar 19:00 低点 4015.485 只比 7-15 16:00 真实底 4017.475 低 2 点，旧"三根"规则使该底被拒、60 点反弹整段消失）；底→顶（上涨笔）对称用 `max(左 bar, 中心).high`；
  - **终点侧三根（防反向吞没）**——下跌笔的底分型三根K线最高价不得涨回起点顶价之上；上涨笔的顶分型三根K线最低价不得跌破起点底价：顶/底后**立即反向贯穿起点**的中继弱反弹不成笔（240 8-28 冲高顶 4631.98 后崩盘 bar 最低 4445.455 < 起点底 4564.27 → 该"上涨笔"被拒 → 4564.27 底被更低的 4282.625 底吸收 → 8-25 顶 4697.105→9-2 底 4282.625 连成单笔下跌，与日线一致）；
  - 两案例对照：60m 7-15 反弹（顶 4081.52 后缓跌，7-16 15:00 最低 4023 未破起点 4017.475）→ 成笔；8-28 反弹（顶 4631.98 后立即崩破起点 4571.66 至 4396.525）→ 不成笔。

**`fixBiExtremes(bis, merged) → bis[]`**（端点极值修正，方向A）
- 对每笔检查「（含本笔端点分型中心的）终点分型之后、下一笔终点分型之前」的合并K线，若存在被掩盖且比当前端点更极端的真实极值，把本笔终点与下一笔起点同步平移到该极值所在K线（保持首尾连续；真低在分型中心上时只改价/时间、idx 不动）；
- 被掩盖来源二：① 包含合并掩盖（`rawLow < low`）；② **markWickBars 长下影压平掩盖**（`_origLow < low`，§2.0）——候选真低 = `_origLow ?? rawLow`，时间取 `_origLowTime ?? rawLowTime`；
- **底分支从 `b.endIdx` 起扫**（含分型中心——中心合并K线可能因包含合并/压平把更低真低藏在自身 low 下，如 1h 9-2 底分型中心吞并 11:00 插针 bar）；**顶分支保持 `endIdx+1` 起扫**（上影压平不产生候选价，中心自身即端点价，避免影响 4443.715 类验收结构）；
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

**`buildZSByUpper(lowerBis, upperBis, tolSec, open_last=True) → zss[]`**（按上级笔分解）
- 本级别中枢只能构建在「同一个上级笔」内部：用上级笔时间区间把本级别笔切段（`startTime ≥ upper.startTime - tol` 且 `endTime ≤ upper.endTime + tol`），每段内独立运行 `buildZS`；
- **最后一段开放段（`open_last`，Python 版 2026-09-05 已实现，JS 端待同步）**：最后一个上级笔的 endTime 边界视为 +∞（当下）——形成中的下级笔归属于形成中的上级笔（正在走的行情天然属于正在走的上级笔），使「出中枢力度对比」（zsExitWeak）在够笔当下即可评估；否则上级形成笔终点只随上级 bar 收盘延伸，下级最新笔因 endTime 超出上级段被丢弃、中枢不存在（曾致 8-21 15:48 信号延后 15 分钟触发）；
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
- **顶背驰**（对应一卖，上涨笔）：红柱面积变小（`cur.redArea < ref.redArea`）**或** 黄白线高点变低（`cur.difHigh < ref.difHigh`）**或** 红柱最大高度变小（`cur.redMax < ref.redMax`）；
- **面积判据受时长可比门约束**（`areaDurComparable`）：面积 Σ = 柱高×K线根数、与区间时长线性相关——两段时长比 > `CHAN_CFG.divergeDurRatio`（默认 3，某段时长为 0/负视为不可比）时面积项不计入（如 15.65h 缓跌 Σ=136.2 vs 4.7h 急跌 Σ=22.7，面积差主要来自时长差），仅用 DIF/柱高两判据。

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
| `CHAN_CFG.wickRatio` | 0.70 | 长影压平/标记的影线占比阈值（影线 ≥ wickRatio×振幅 触发，见 §2.0） |
| `CHAN_CFG.wickAtrK` | 0.5 | 影线绝对长度下限系数（影线 ≥ wickAtrK×稳定ATR 才处理；窄幅盘整小K线免疫） |
| `CHAN_CFG.divergeDurRatio` | 3 | 背驰面积判据的时长可比上限（两段时长比 > 该值则面积项不计入，见 §2.8） |
| `CHAN_CFG.nearDoubleAtrK` | 0.3 | 近等双顶/双底平台取后顶/后底的价差与回调深度 ATR 系数（规则 #6，§2.4） |
| `CHAN_CFG.nearDoublePct` | 0.001 | 近等双顶/双底平台取后顶/后底的价格比例下限（与 ATR 项取 max） |
| `CHAN_CFG.debug` | false | 调试打印（buildBi / 买卖点识别过程） |

## 4. 依赖关系

| 上层模块 | 复用关系 |
|----------|----------|
| `chan-bi`（画笔） | `markWickBars`/`mergeBars`/`findFractals`/`countRaw`/`hasGapBetween`/`buildBi`/`fixBiExtremes`/`lockedPivotsOf`/`alignBiToUpper`/`calcATR`/`calcMACD`/`hasMacdCrossBetween`/`extendLastBi`/`lowerResOf`/`calibrateBiTimes`/`intervalSecOf` |
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
| 长影 bar 位于窗口首/末（无左右相邻原始K线） | 无法判定 low 条件 → 无 `_topCand`，按纯压平处理（见 §2.0） |
| `_topCand` 方向 | 仅顶分型方向（冲高插针）——顶分型中心候选价路径 | 
| 下影探底 | 不产生候选价（分型结构仍用压平后的 low）；真低走 `_origLow` → `fixBiExtremes` 恢复通道（§2.0/§2.4），仅在它是笔底区间被掩盖的绝对极值时生效，不影响分型与跳空判定 |
