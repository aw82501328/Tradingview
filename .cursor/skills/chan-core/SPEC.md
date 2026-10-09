# chan-core 缠论算法核心规格（SPEC）

> 文档用途：chan-core 是**唯一缠论算法源**，纯函数模块（不依赖 CDP、不绘图），被 `chan-bi`（画笔）、`mark-buy-sell`（买卖点）两个 SKILL 复用，`mark-entry`/`mark-sr-flip` 复用其工具函数。本规格描述其全部导出函数的行为契约，是上层 SKILL 规格的基础。
> 对应脚本：`.cursor/skills/chan-core/scripts/chan_core.js`（约 1760 行）。

## 1. 统一约定

| 项 | 约定 |
|----|------|
| 笔对象字段 | `type`(up/down)、`startIdx/endIdx`(合并K线索引)、`startTime/endTime`(校准后端点时间)、`startPrice/endPrice`、`rawCount`(覆盖原始K线数)、`span`(幅度)、`gapLocked`(跳空成笔)、`macdCross`(MACD变色成笔) |
| 时间 | 全部 Unix 秒（UTC），与 TradingView K线时间一致 |
| 配置 | `CHAN_CFG.gapFilter`（跳空独立成笔阈值，默认 1.0）、`CHAN_CFG.wickRatio`（长影压平影线占比阈值，默认 0.70）、`CHAN_CFG.wickAtrK`（已废除，2026-10-02 改 `CHAN_CFG.wickMinLen`）、`CHAN_CFG.wickMinLen`（影线绝对长度下限，具体数值（品种报价单位价差），默认 0.5）、`CHAN_CFG.wickMinRange`（长影压平前提：整根K线价差（最高-最低）须 > 该值才判插针，品种报价单位绝对价差，默认 15；0=不限，2026-10-02 起）、`CHAN_CFG.divergeDurRatio`（背驰面积判据时长可比上限，默认 3）、`CHAN_CFG.nearDoubleAtrK/nearDoublePct/nearDoubleFixed`（近等双顶平台取后顶容差三项：ATR 系数/价格比例/固定价差，默认 0.3/0.001/0.0，取 max 并集）、`CHAN_CFG.nearDouble3/15/60/240/D`（近等双顶每周期开关，默认关/关/开/开/开，gating 统一走 `nearDoubleOn(res)`）、`CHAN_CFG.nearDoubleRebound`（近等后顶/后底反弹不成笔取后总开关，默认 true，规则 #6b）、`CHAN_CFG.debug`（调试打印） |
| 合并K线字段 | 含 `_rawCount`(覆盖原始K线数)、`highTime/lowTime`(极值原始K线时间)、`rawHigh/rawLow/rawHighTime/rawLowTime`(覆盖原始K线真实极值及时间)、`_topCand/_topCandTime`(覆盖范围内「可成顶分型的长影 bar」的影线端点价及所在原始K线时间)、`_origLow/_origLowTime`(覆盖范围内 markWickBars 压平的长下影真低及所在原始K线时间，供 `fixBiExtremes` 端点恢复) |

## 2. 导出函数清单

### 2.0 长影线处理（冲高/探底插针）

**`markWickBars(rawBars) → bars[]`**（须在 `mergeBars` 之前调用）
- **稳定波动基准已废除**（2026-10-02）：影线长度下限 `wickMinLen` 为**具体数值**（品种报价单位价差，默认 0.5），不再用 wickAtrK×全窗口 TR 均值的系数口径——下限不随行情波动漂移；
- 判定（每根K线独立）：上影 = `high − max(open, close)`、下影 = `min(open, close) − low`、振幅 = `high − low`；
  - **前提**（2026-10-02）：振幅（最高−最低）> `wickMinRange`（绝对价差，默认 15）——窄幅K线即使影线占比/长度达标也不判插针（`wickMinRange=0` 回退旧口径）；
  - 上影 ≥ `wickRatio`×振幅 且影线长度 ≥ `wickMinLen` → **长上影（冲高插针）**；
  - 否则下影满足同条件 → **长下影（探底插针）**；十字星/普通K线不受影响（同根K线两影不可能同时 ≥70%）。
- **长上影处理（high 一律压平至实体顶，保持历史验收的合并/笔结构——影线价参与合并会改变结构或污染笔区间）**：
  - 若该 bar 的 `low ≥ 左右相邻原始K线低点`（保留影线价可成为顶分型中心端点）→ 记 `_topCand = 原 high`，`findFractals` 在该 bar（或其合并 bar）成为顶分型中心时用 `_topCand` 作端点价与时间（**影线可成端点**）；
  - 否则（low 条件不满足——冲高插针本就不成顶分型）→ 纯压平，影线价不出现、不阻止后续合法顶成笔；
- **长下影处理**：low 压平至实体底（结构高低保持压平语义，不产生候选价）；压平前把原低记入 `_origLow = 原 low`、`_origLowTime = 该 bar 时间`，随 `mergeBars` 包含传播（§2.1），由 `fixBiExtremes` 恢复为更低的真实笔底端点（§2.4.5 方向A）——压平只作用于结构高低，不销毁端点恢复通道。包含判断不看压平后的高低，看压平前的真实高低（`_preHigh`/`_preLow`）：真实区间没有包含就不能合并（15m 9-30 20:00 低 4185.16 与 20:15 低 4182.89 没有包含；20:30 与 20:45 有包含，仍然合并）。合并发生后清掉这两个字段，后续相邻K用高高/低低之后的高低继续判包含。
  - 恢复触发条件：`_origLow` 是某下跌笔终点分型之后、下一笔顶分型之前区间内的**绝对最低**（比笔底更低），如 1h 9-2 11:00 bar（下影 93%，真低 4282.625 < 笔底 4287.27）。若不恢复，笔底虚高会派生伪 2买/类2买（4287.27 被误判为高于 4h 段起点 4282.625 的回踩低点）；
  - 探底端点（如 60m 7-29 4010.41、7-15 16:00 底）不受影响——其 bar 影线占比不足，或由分型/端点修正（rawLow 通道）正常产生；
  - 与上影不对称：上影刻意不回 rawHigh（4443.715 先例，见下），下影真低只走 `_origLow`→`fixBiExtremes` 恢复通道，**不写入 rawLow/rawHigh**——跳空检测（§2.3）保持压平语义。
- **案例**（规则动机）：
  - 60m 7-16 02:00 bar（O4062.41 H4081.52 L4058.10 C4059.29，上影 81.6%）：low 4058.10 > 01:00 L4033.11 且 > 03:00 L4048.10 → `_topCand = 4081.52`，成为 7-15 反弹笔（4017.475→4081.52）的真实顶；
  - 15m 9-3 16:00 bar（O4428.86 H4443.715 C4431.405，上影 79%）：low 4428.135 < 16:15 bar low 4430.735 → 纯压平——插针不成端点，也不会阻止 17:00 顶 4442.04 成笔；
  - 60m 8-28 22:00 bar（H4631.98，上影 27.6% 不触发）与 240 同型冲高 → 不涉及本函数，由 §2.4 `fractalRangeClear` 治理。

### 2.1 包含关系处理

**`mergeBars(rawBars) → merged[]`**
- 相邻K线有包含关系时合并，方向由前序趋势决定：向上合并取「高高」，向下合并取「低低」；包含与否用压平前的真实高低（见 §2.0 `_preHigh`/`_preLow`），高高/低低仍写压平后的结构高低。向下合并的高点按「压平前真实高点」取较小者（2026-10-01 起，替代旧 `_hiKeep` 创新高保留）：两侧都未压平时即标准低低（10-1 04:45+06:00 取 4159.77，06:00 的 4160.615 不再抬升块高点压制 04:30 顶分型）；块内含长影压平K（结构高点低于自身真实高点）且新K真实高点更高时，结构高点不低于两者的真实较小值（60m 9-21 18:00 上影压平到 4345.73，19:00 真实高点 4371.11 不能被吃掉，块高点取真实较小值 4356.77——与完全不压平的低低一致，17:00 底分型得以存活）；
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
> 「层级」= 规则实现在哪一层：**核心** = `chan_core.js`（唯一算法源）；**画笔层** = `chan_bi.js`（管线编排：嵌套周期迭代、ATR 过滤、窗口过滤、绘制）。

| # | 规则 | 触发条件（简） | 效果 / 约束 | 适用周期 | 层级 |
|---|---|---|---|---|---|
| 1 | 包含关系合并（`mergeBars`）§2.1 | 相邻K线存在包含，方向由前序趋势定 | 向上取高高 / 向下取低低；`rawLow/rawHigh` 记录覆盖真极值 | 全部 | 核心 |
| 2 | 长影处理（`markWickBars`）§2.0 | 振幅（高−低）> `wickMinRange`（默认15）；上影 ≥70% 且长度 ≥ `wickMinLen`（默认0.5 绝对值）；下影对称 | 冲高插针压平 high（`_topCand` 可成顶端点）/ 探底插针压平 low（`_origLow` 供端点恢复）；插针不污染结构 | 全部 | 核心 |
| 3 | 分型识别（`findFractals`）§2.2 | 中K高于/低于左右（价 + 反向区间双侧条件） | 顶/底分型；`_topCand` 影线价可作端点价 | 全部 | 核心 |
| 4 | 阶段一 严格交替序列 §2.4.1 | 连续同类型分型 | 顶取最高、底取最低；`lockedPivots` 命中标记 `locked` | 全部 | 核心 |
| 5 | 同类型更极端替换 §2.4.2-1 | 后顶 ≥ 前顶 / 后底 ≤ 前底 | 端点更新为突破极值（`locked`/`gapLocked` 除外） | 全部 | 核心 |
| 6 | **近等双顶/双底平台取后顶/后底** §2.4.2-2 | 后点略不极端且差 ≤ max(0.3×ATR, 0.1%×价, nearDoubleFixed 固定项)；last→k 间**已有两段首尾相接的成笔则禁止后移**（单独一段成笔不挡）；中间含 ≥阈值回调；非 locked/gapLocked/已 nearDouble（单跳封顶）；**macdCross 不豁免** | 笔端点取更晚的后顶/后底，前顶/前底视为插针性极值（走势终完美） | **按周期开关 nearDouble3/15/60/240/D（默认 60/240/D 开）**，调用方经 `nearDoubleOn(res)` 判定后传 `nearDouble` | 核心 |
| 6b | **近等后顶/后底（反弹不成笔）取后** §2.4.2-2b | 回溯分支：last→k 反弹/回撤腿 gap<4 本身拆不出笔；k 与 prev（result[-2]）同类型近等（差 ≤ #6 同容差，仅60m可 1.5×thr+15m双动能）；prev→last 为 ≥thr 真实回调；k.locked 或（nearDoubleOn 周期 且 `nearDoubleRebound`） | 端点后移到后顶/后底（「回调够深、反弹太短」的走势终完美）；k.locked=区间套落地——上级已后移的端点在本级复现 | k.locked 全周期；否则同 #6 周期开关 × `nearDoubleRebound`（默认开） | 核心 |
| 6r | **平台取后可证伪回退** §2.4.2-2r（2026-10-01） | 间隔不足分支入口（优先于 moreExtreme 顶替/近等后移）：last 带 `_platAnchor`（#6 后移的原端点）且未锁定；k 与 last 间隔不足连不上，而 `_platAnchor→k` 能完整成笔（间隔/笔内极值/范围脱离）；且起点侧极值守卫——`_platAnchor→k` 区间内无比起点的同向更极值 | 回退原端点并接入 k（后移断言被市场否定，与 `_shiftBreakRestore` 同哲学）。例：15m 10-1 02:00 底后移到 03:0 后 04:30 顶只剩 4 根合并K，02:00→04:30 有 7 根 → 回退成笔；60m 9-18 04:00 底上方藏 07:00 更低点 4339.72 → 守卫拦下不回退 | 同 #6 周期开关（_platAnchor 仅由 #6 产生） | 核心 |
| 7 | MACD 端点让位 §2.4.2-3 | 原笔起点存在，`result[-2]` 为 MACD 变色端点且 k 更极端，整笔双向极值合法且被移除端点未锁定 | k 顶替 `result[-2]` 移除中间分型 | 全部 | 核心 |
| 8 | 跳空独立成笔 §2.4.2-4 | 相邻合并K线缺口 ≥ `gapFilter`×ATR | 强制成笔并锁定端点；后续严格突破锁定价才解锁 | 全部（`--gap` 可调） | 核心 |
| 9 | 最小间隔 §2.4.2-6/§2.4.4 | 合并K线间隔 ≥4（覆盖原始K线 ≥5） | 不足不成笔（进入回溯/MACD/作废分支） | 全部 | 核心 |
| 10 | 前顶/前底作废（弱分型突破）§2.4.2-5 | 脆弱端点 + 浅回调 <50% + last 为弱分型（MACD 变色且 raw<5） | k 顶替 prev2、移除中间 last | 全部 | 核心 |
| 11 | 最小间隔脆弱笔例外 §2.4.2-8 | `prev→last` 间隔恰 4 且回调/反弹浅（<50%） | 该笔未确认，允许被后续更极端分型顶替（上涨/下跌延伸） | 全部 | 核心 |
| 12 | 回溯替换保护 §2.4.2-8 | last 比 `result[-3]` 更极端，且 k 未锁定 | 保留 last 为端点、作废 prev、k 暂不接入（笔内极值与区间套一致）。`k.locked` 不走本保护，改由更极端替换落地 | 全部 | 核心 |
| 13 | 分型范围双向检查（`fractalRangeClear`）§2.4.4 | 顶/底分型范围未脱离（互相包含） | 不成笔，等待更极端分型 | 全部 | 核心 |
| 14 | 笔内极值（`noMoreExtremeInside`）§2.4.4 | 笔区间内藏更极值点 | 不成笔 | 全部 | 核心 |
| 15 | MACD 变色成笔 §2.4.2-7 | 间隔恰 3（合并 4 根）+ 方向性红绿转换 + 无更极值 | 允许不足 4 间隔成笔（`macdCross`，端点让位规则仍适用）；对原始K线覆盖数无下限（`macdRaw` 仅记录） | 全部 | 核心 |
| 16 | 未完成笔延伸（`extendLastBi`）§2.7 | 末端单调上涨/下跌无新分型 | 最后一笔延伸到最新极端K线 | 全部 | 核心 |
| 17 | ATR 噪音过滤 §2.4.7 | 笔幅度 < 0.5×ATR（稳定全窗口均值基准） | 剔除噪音小笔 | 全部（`--atr` 可调） | 画笔层（chan_bi.js） |
| 18 | 端点极值修正（`fixBiExtremes`）§2.4.5 | `rawLow/_origLow < low` 且比端点更极端 | 笔终点平移到被合并/压平掩盖的真低（底分支含分型中心） | 全部 | 核心 |
| 19 | 逐级端点时间校准（`calibrateBiTimes`）§2.7 | 用低一级周期K线定位极值时间 | 端点时间精确落在低一级 bar 上 | 15←3、60←15、240←60、D←240（3m 无） | 核心（画笔层编排） |
| 20 | 区间套强制对齐（`alignBiToUpper`）§2.4.6 | 上级笔端点在本级须复现 | 端点与上级极值重合；断口合并 + 补幅度过滤治理缝合 | 全部（非最外层，依上级笔） | 核心（画笔层编排） |
| 21 | 小周期绘制窗口 §2.4.7 | 只保留窗口内结束的笔 | 避免图上过密 | 仅 15m（30 天）/ 3m（15 天） | 画笔层（chan_bi.js） |
| 22 | 背驰面积时长可比门（`divergeDurRatio`）§2.8 | 两段时长比 >3（或某段 0/负） | 面积 Σ 不计入背驰，DIF 单判据兜底 | 全部（`isBiDiverge`，买卖点/计划层） | 核心（买卖点层用） |

**`buildBi(fractals, merged, atr, macdArr, lockedPivots, nearDouble, lowerContext) → bis[]`**（`nearDouble` 默认 falsy：关闭规则 #6；生产调用方按 `nearDoubleOn(res)` 每周期开关取值）

#### 2.4.1 阶段一：严格交替序列 + 区间套锁定

- 连续同类型分型取更极端者（顶取最高、底取最低），得到顶底严格交替的序列；
- **区间套锁定**：与 `lockedPivots`（上级笔端点）方向一致且价差 ≤0.001 的分型标记 `locked`——它是上级确认过的拐点，阶段二不可吞。

#### 2.4.2 阶段二：回溯替换（按序处理每个分型 k，按以下优先级）

**2.4.2-1 同类型分型替换（速查表 #5）**

- 一句话：新顶 ≥ 前顶 / 新底 ≤ 前底，就用新分型替换端点。
- `locked` 端点跳过（不可替换）；非 gapLocked 端点更极端即替换（顶 `k.high >= last.high`、底 `k.low <= last.low`）；`gapLocked` 端点**仅当严格突破**锁定价格（顶 `k.high > last.high`、底 `k.low < last.low`，严格大于/小于）才解锁替换。

**2.4.2-2 近等双顶/双底平台取后顶/后底（速查表 #6）**

2026-10-02 起容差与比较顺序简化：取消 ATR 项/价格比例项/15m 双动能确认，thr = `nearDoubleFixed`（固定容差，默认 2.0）；比较「先影线后实体」——影线更极端（>= / <=）直接后移（更极端替换，不走本规则、不带 nearDouble 封顶）；影线不满足比实体（bodyTop/bodyBottom）：后实体价不低于前实体（方向化 diff ≤ 0）直接后移，更低则差 ≤ thr 才后移；中间真实回调深度闸门同用 thr。双底完全镜像。

- 一句话：平台里两个几乎同价的高点，取更晚的那个，前面的视为插针（走势终完美）。
- 仅当 `nearDouble=true`（生产调用方按 `nearDoubleOn(res)` 每周期开关判定，五周期默认全开）：k 与 last 同类型、影线不满足且实体差 ≤ `nearDoubleFixed`（后实体更低时；不低于则直接后移）、last→k 分型链上没有两段首尾相接的成笔（成笔口径同阶段二：间隔≥4 且无更极值且分型范围脱离，或间隔恰为 3 且方向性 MACD 变色且无更极值；单独一段成笔不挡后移）、且中间存在 ≥ 同阈值的真实回调分型，且 last 非 `locked`/`gapLocked` 且未被本规则替换过（单跳封顶）→ 用后顶/后底 k 替换 last，前顶/前底视为插针性极值不终止本段。
- `macdCross` 端点不豁免（该端点本就是间隔不足靠 MACD 变色凑出的脆弱顶/底，如 1h 8-31 顶 4464.23，与近等平台取后顶语义一致）。
- 例：1h 8-31 19:00 顶 4464.23 → 9-1 08:00 顶 4461.7（实体差 ≤ thr），12h 平台（4415.75~4464）全程分型间隔 <4。单跳封顶防平台内连续近等端点累积漂移（实测 3 跳累计可超 1×ATR）。
- 先影线闸门（2026-10-02）：影线已更极端时**不走**实体近等——已被更极端替换的端点若再补 nearDouble 单跳封顶，会挡住后续真正的近等后移（例：60m 8-7 00:00 前底 4223.505 影线更极端已替换，08:00 后底 4229.875 实体更低应可近等后移）。

**2.4.2-2b 近等后顶/后底（反弹不成笔）取后（速查表 #6b，2026-09-26；2026-10-02 容差改固定值）**

- 一句话：深回调后反弹到与前顶几乎同价、但反弹腿太短不成笔时，端点后移到后顶——「回调够深、反弹太短」的走势终完美，与 #6 平台场景互补。
- 与 #6 的区别：#6 在同类型近等端点上后移，仅当两顶/两底之间已有两段首尾相接的成笔时禁止；本规则在「间隔不足→回溯替换」分支内触发（k 与 prev=result[-2] 同类型、last→k gap<4 本身拆不出反弹笔），处理中间**已有合格反向分型**（prev→last 甚至已成有效笔）的情形。
- 条件：
  - 近等（先影线后实体，同 #6）：影线更极端时走 moreExtreme 顶替或 prev 受有效笔保护；影线不满足比实体，`prev−k ≤ thr`（thr = `nearDoubleFixed`，后实体不低于前实体直接取后）；
  - 真实回调：prev→last 反向幅度 ≥ thr（镜像 #6 的 pull 条件）；
  - 权限二选一：**k.locked**（上级笔端点，区间套强制落地——任何周期生效，不受 `nearDoubleRebound` 开关限制；上级已后移的端点必须在本级复现）或 nearDouble（`nearDoubleOn(res)`）且 `CHAN_CFG.nearDoubleRebound`（默认 true）；
  - 排除：prev.gapLocked（跳空锁定只被严格突破替换）、prev.locked（锁定前顶不让位）、last.locked（不吞锁定中间分型）、prev.nearDouble（单跳封顶，同 #6）；「回溯替换保护」（#12，lastIsDeeper）在 k 未锁定时仍优先于替换，`k.locked` 不走该保护（见 §2.4.2-8）。
- 效果：prev 让位、`result[-2] = k` 并 pop last，笔端点后移到 k（k 打 nearDouble 标记）。前顶/前底成为笔内极值（#6 同类先例，如 240 层 9-16 顶 4361.15→4357.64）。
- 例：60m 2026-09-18 15:00 顶 4399.67 → 22:00 底 4342.73（22:00 与 23:00 包含合并成一根）→ 9-19 01:00 顶 4397.045：反弹腿仅 3 根合并K（gap=2<4；MACD 例外要求 gap=3 也不满足），实体差 4393.49−4393.05=0.44 ≤ thr，回调 56.94 ≥ thr。240 层 #6 已把 4h 顶后移到 9-19 01:00（4h 层两顶间隔全 <4）→ 60m 经 k.locked 路径复现：上涨笔 09-18 11:00 4334.295 → 09-19 01:00 4397.045，随后下跌笔至 09-21 22:00 4322.81；若 240 未后移，60m 亦可经 nearDouble60 路径自行后移。底对称（近等双底取后底）。
- 测试：`py_chain/test_near_double.cjs/.py`（fixture `near_double_rebound_xauusd_20260919.json.gz`；断言开关关=端点停 4399.67、k.locked 路径不受开关限制、镜像双底案例）。

**2.4.2-3 MACD 端点让位（速查表 #7）**

- 一句话：MACD 变色笔的端点被更极端的同类型分型超越时，让位给新分型。
- 条件：原笔起点 `result[-3]` 必须存在，`result[-2]` 是 MACD 变色端点且 k 更极端，被替换与被删除的端点均未锁定。替换前检查整笔内部合并K线和中间分型（含影线端点价）：下跌笔内部不得高于起点、低于新终点，上涨笔对称，相等允许（`replacementExtremesClear`，见 §2.4.4）。通过才用 k 顶替 `result[-2]`、移除中间分型（保证 MACD 成笔终点是区间内最新绝对极值，例 92.83→92.74）；不通过则保留转折，k 继续正常成笔判断。
- 黄金 2026-09-09 15m 必须保留 19:45→20:30 的 MACD 短笔和 20:30→21:30 的上涨笔，不能被后续低点吞掉。

**2.4.2-4 跳空成笔（速查表 #8）**

- 一句话：相邻合并K线之间缺口 ≥ 1×ATR，缺口本身就是一段独立走势，强制成笔。
- `last→k` 间存在 ≥ `gapFilter*ATR` 跳空（用真实极值 `rawHigh/rawLow` 判定，见 §2.3）→ 强制独立成笔（`k.gapLocked = true`），豁免最小间隔/笔内极值/范围检查；端点不参与末笔延伸与端点修正，直到被严格突破解锁（见 2.4.2-1）。

**2.4.2-5 前顶/前底作废（速查表 #10）**

- 一句话：浅回调的脆弱小顶被新高突破，前顶不作数。
- 条件：`prev2` 与 k 同类型、`prev2→last` 不构成有效笔（间隔不足）、k 更极端、last 不得比 `prev3` 更极端（否则 last 是深回调的真实转折）、回调/反弹 < 50% 幅度、last 是弱分型（MACD变色且原始K线<5根）、`last`/`prev2` 均未锁定 → k 顶替 `prev2` 移除 last（若 `prev2.macdCross` 则 k 继承标记）。
- 例：1h 7-22 23:00 底（MACD成笔 raw=7）结构充足，7-22 17:00 顶是真实顶，不作废。

**2.4.2-6 普通成笔三门槛（速查表 #9/#14/#13）**

- 一句话：间隔够、区间干净、真转势，三关全过才成笔。
- ① 最小间隔 `isValid(last, k)`：合并K线间隔 ≥ 4，即合并后至少 5 根（覆盖原始K线 ≥5）；
- ② 笔内无更极值 `noMoreExtremeInside(last, k)`；
- ③ 分型范围脱离 `fractalRangeClear(last, k)`（起点侧同侧两根、终点侧三根防反向吞没，细节见 §2.4.4）；
- 三门槛全过 → 接入；间隔足够但极值冲突/范围未脱离 → 进入 2.4.2-6b 的前顶作废判定，仍不满足才忽略 k。

**2.4.2-6b 前顶/前底作废·区间极值版（2026-10-09）**

- 一句话：间隔够但成笔判据不过的更极端同型分型，若能与 prev3 直接成笔，则前顶/前底被作废、端点推进到 k，不让更高/更低点被吞进反向笔。
- 背景：三门槛失败（②极值冲突或③范围未脱离）时旧逻辑直接忽略 k——若 k 比 prev（result[-2]）更极端，该高点/低点将永久留在 prev 起步的反向笔内部，笔起点不再是区间极值（违反笔内极值原则）。例：15m 2026-07-20 顶 16:15 4030.88 被 19:30 4040.82 突破，17:30→19:30 反弹腿因终点侧反向贯穿（20:00 实体底 4012.86 < 起点底 4014.85）不成笔，忽略 19:30 顶会使 16:15 起的下跌笔内藏 10 元更高点；作废后上涨笔延伸为 15:00 4002.18 → 19:30 4040.82。
- 条件：k 与 prev（result[-2]）同类型且**严格**更极端（`>`/`<`，等价不作废）；`prev3`（result[-3]）与 k 构成完整有效笔（`_pair_forms_bi` 全套：间隔/笔内极值/范围脱离，隐含回溯替换保护——last 比 prev3 更极端时 `_pair_forms_bi(prev3,k)` 因笔内极值不过而自动拦截）；`prev`/`last` 均未锁定 → k 顶替 prev、移除中间 last（`prev.macdCross` 时 k 继承标记）。
- 与间隔不足回溯替换（2.4.2-8 的 moreExtreme 顶替）互补：那边是 last→k 拆不出反弹笔（间隔不足），这边是间隔够、但反弹笔被终点侧贯穿证伪——两种失败下前顶都让位于更极端的新极值。
- 注意：k 落地后可能触发既有的 MACD 端点让位（2.4.2-3）继续收敛（如 15m 2026-01-21：顶后移到 22:00 后，22:15 崩破使 MACD 成笔底 15:15 让位 23:30，整段收敛为一笔下跌 14:15→次日 01:30，区间极值不变量保持）。

**2.4.2-7 MACD 变色成笔（速查表 #15）**

- 一句话：间隔只有 3 根合并K线，但 MACD 柱子发生方向性红绿切换，动能切换视作走势段落切换，特批成笔。
- 条件：`gap === 3` 且方向性红绿转换（底到顶绿变红/顶到底红变绿，检测边界用分型极值时间，见 §2.6）且无更极值 → 成笔（`k.macdCross = true, k.macdRaw = countRaw(...)`）。
- **对原始K线覆盖数不设下限**，`macdRaw` 仅作记录（用于前顶/前底作废的弱分型判定，见 2.4.2-5）。

**2.4.2-8 间隔不足回溯替换（速查表 #11/#12）**

- 一句话：间隔不足且无 MACD 变色，新分型与倒数第二个端点同类型且更极端时回溯作废旧端点；但已成有效笔的前顶/前底受保护。
- **前顶有效原则**：`prev→last` 已构成有效笔（间隔 ≥ 4 且无更极值且范围脱离）→ 前顶/前底保留，更高顶/更低底 k 不能作废它（缠论："前顶右侧已有足够K线构成笔则前顶有效"，例 8-20 04:00 顶→20:00 底间隔 11 根）。**例外**：`k.locked`（上级笔端点）且比 prev 更极端、与 last 间隔不足无法自成笔时，不受本保护限制，k 顶替 prev 并移除 last（不新开一根间隔不足的笔）。`prev.locked` / `last.locked` 仍不让位。例：60m 2026-09-17 03:00 底 4235.165 为 4h 下跌笔终点，与左侧 02:00 顶只隔 1 根合并 K，9-14 21:00 底到该顶已隔 42 根成笔；锁定后下跌笔延伸到 4235.165，与 4h 终点重合。
- **最小间隔脆弱笔例外**：`prev→last` 虽构成有效笔，但间隔恰为最小值（`gapPrevLast === 4`，刚够 5 根合并K线）且回调/反弹浅（< 前段涨跌幅的 50%，前段 = `result[-3]` 极值到 prev）时，该笔尚未被确认——随后 k 即创更高顶/更低底说明整段仍是同一笔的延伸（缠论：顶被更高顶突破即作废），prev 仍被 k 顶替。例：15m 9-3 顶 4496.01(21:03)→底 4466.02 间隔恰 4、回调 39%（浅），23:15 新高 4510.93 顶替前顶、上涨笔延伸至 4510.93（与 60m/3m 端点一致）；对称场景 8-19 底 4327.27 被 09:00 更低底 4324.68 顶替。8-20 04:00 顶→20:00 底（间隔 11 根）等坚实笔、深回调（≥50%）场景不受影响。
- **回溯替换保护**：last 比 `result[-3]` 更极端（k 为顶时 `last.low < prev3.low`，k 为底时 `last.high > prev3.high`）且 `prev`/`prev3`/`k` 均未锁定 → 保留 last 取代 `result[-3]`、作废 prev、k 暂不接入（保证笔内极值与区间套一致）。`k.locked` 不走本保护，改走上面的锁定落地。
- 否则（`last`/`prev` 均未锁定）：k 顶替 prev、移除 last。未锁定分型的行为不变。

#### 2.4.3 阶段三：两两连笔

- 相邻端点连笔：`isUp = b.type==="top"`；字段 `type/startIdx/endIdx/startTime/endTime/startPrice/endPrice/rawCount(=countRaw)/span(=|endPrice−startPrice|)/gapLocked(=b.gapLocked)/macdCross(=b.macdCross)`。

#### 2.4.4 辅助判定

- `isValid(a,b)`：`b.mergedIdx - a.mergedIdx >= 4`；
- `noMoreExtremeInside(a,b)`：笔内（`a.mergedIdx+1 .. b.mergedIdx-1`）不存在比端点更极端的点（严格比较，无容差）；
- `replacementExtremesClear(origin, old, middle, end)`：MACD 端点让位的整笔检查——ceiling/floor 由 origin/end 确定，old、middle 两分型（含影线价）及区间内所有合并K线不得越界（相等允许）；
- `fractalRangeClear(a,b)`（分型范围双向检查，被阶段二主分支与前顶作废判定共用）：
  - **起点侧「与段同侧的两根」**（排除段外反向结构 bar）——顶→底（下跌笔）用 `min(中心, 右 bar).low`：下跌只需跌破「顶分型及之后」的结构低点，顶分型**左 bar**（顶之前主升前夜低点）不抬高"必须跌破"的阈值——否则误杀健康反弹底（60m 7-14 20:00 顶 4104.05 的左 bar 19:00 低点 4015.485 只比 7-15 16:00 真实底 4017.475 低 2 点，旧"三根"规则使该底被拒、60 点反弹整段消失）；底→顶（上涨笔）对称用 `max(左 bar, 中心).high`；
  - **终点侧三根（防反向吞没，实体口径）**——下跌笔的底分型三根K线**实体**最高价（bodyTop，缺失回退 open/close 再回退影线）不得涨回起点顶价之上；上涨笔对称（bodyBottom 不跌破起点底价）。2026-10-01 起影线刺穿不算（15m 10-1 09:00 长阳高 4161.385 刺穿 04:30 顶 4160.41 但实体顶 4159.67 未越过 → 04:30→08:45 下跌笔成立、02:00→04:30 反弹笔得以保留）；此前影线口径会把这类插针误判为反向贯穿。顶/底后**立即实体反向贯穿起点**的中继弱反弹仍不成笔（240 8-28 冲高顶 4631.98 后崩盘 bar 最低 4445.455 < 起点底 4564.27 → 该"上涨笔"被拒 → 4564.27 底被更低的 4282.625 底吸收 → 8-25 顶 4697.105→9-2 底 4282.625 连成单笔下跌，与日线一致）；
  - 两案例对照：60m 7-15 反弹（顶 4081.52 后缓跌，7-16 15:00 最低 4023 未破起点 4017.475）→ 成笔；8-28 反弹（顶 4631.98 后立即崩破起点 4571.66 至 4396.525）→ 不成笔。

#### 2.4.5 端点极值修正（速查表 #18）

**`fixBiExtremes(bis, merged) → bis[]`**（方向A）
- 一句话：被包含合并或长影压平藏起来的真实极值，恢复为笔端点。
- 对每笔检查「（含本笔端点分型中心的）终点分型之后、下一笔终点分型之前」的合并K线，若存在被掩盖且比当前端点更极端的真实极值，把本笔终点与下一笔起点同步平移到该极值所在K线（保持首尾连续；真低在分型中心上时只改价/时间、idx 不动）；
- 被掩盖来源二：① 包含合并掩盖（`rawLow < low`）；② **markWickBars 长下影压平掩盖**（`_origLow < low`，§2.0）——候选真低 = `_origLow ?? rawLow`，时间取 `_origLowTime ?? rawLowTime`；
- **底分支从 `b.endIdx` 起扫**（含分型中心——中心合并K线可能因包含合并/压平把更低真低藏在自身 low 下，如 1h 9-2 底分型中心吞并 11:00 插针 bar）；**顶分支保持 `endIdx+1` 起扫**（上影压平不产生候选价，中心自身即端点价，避免影响 4443.715 类验收结构）；
- 只处理被掩盖的极值；`gapLocked` 端点固定不参与。

#### 2.4.6 区间套锁定与强制对齐（速查表 #20）

**`lockedPivotsOf(prevBis) → [{dir, price}]`**
- 由上级笔提取锁定端点：上涨笔起点→bottom、终点→top；下跌笔反之。

**`alignBiToUpper(lowerBis, upperBis, upperIntervalSec, lowerBars) → lowerBis`**（区间套强制对齐）
- 上级笔的每个起点/终点都是明确极值，下级周期必须复现相同极值；
- 每个上级拐点找「同方向、时间最近（≤ 上级间隔秒）且未使用」的下级拐点，快照对齐；
- 仅当下级拐点更不极端（漏掉上级真极值）时同时对齐时间+价格；否则只对齐价格保留下级更精确时间；
- **幽灵端点防御（第4参 `lowerBars`，可选）**：上级极值可能只存在于上级聚合数据中（跨周期数据源聚合差异，如日K聚合低点低于该日所有日内K线），本级K线无法复现该极值。判定：取上级拐点所在上级bar时间跨度 `[up.time, up.time + upperIntervalSec)` 内本级K线的**局部价格范围**，若上级极值超出该范围（底低于局部最低 / 顶高于局部最高），视为幽灵端点，跳过对该拐点的对齐（保留本级别真实极值）。未传 `lowerBars` 时跳过校验（向后兼容）；
- **Python 镜像（2026-10-09 起）**：`py_chain/chan_core.py` 已含同语义 `alignBiToUpper`（供点击定位的次级别笔链 `py_chain/locate_bi.py` 使用；对齐后的断口治理同款在 locate_bi._merge_aligned_gaps，与 chan_bi.js 编排层位置对应）。

#### 2.4.7 画笔层管线九步概述（速查表 #16/#17/#19/#21）

画笔（chan-bi）把核心函数串成整条管线：`markWickBars → mergeBars → findFractals → lockedPivotsOf+buildBi → fixBiExtremes → ATR 噪音过滤 → extendLastBi → calibrateBiTimes → alignBiToUpper+断口治理 → 小周期窗口过滤`（完整九步管线及各步细节见 chan-bi/SPEC.md §3.3）：

- ATR 噪音过滤（速查表 #17）：笔幅度 < 稳定ATR×0.5 剔除（`chan_bi.js` 实现，阈值基准用全窗口 TR 均值，见 chan-bi/SPEC.md 3.3 第 5 步）；
- 小周期窗口（速查表 #21）：15m 只留 30 天、3m 只留 15 天（`chan_bi.js` 实现）；
- `extendLastBi`（速查表 #16）与 `calibrateBiTimes`（速查表 #19）详见 §2.7。

### 2.5 中枢

**`buildZS(bis, barSec) → zss[]`**
- 取连续三笔的重叠区间构成中枢：`ZG = min(三笔高点)`、`ZD = max(三笔低点)`，`ZG > ZD` 才成立；
- 延伸：后续笔与 `[ZD, ZG]` 有重叠则纳入（`dd/gg` 扩展）；
- 离开：笔与中枢区间完全无重叠；或笔起点在中枢内、终点突破中枢边界（`startIn && endBreak`）且下一笔没有回到已纳入笔的重叠区 → 中枢结束。下一笔重新相交则本笔只是回抽，继续延伸；
- **最少 3 笔即可输出**（三笔重叠即成中枢）；`biCount < 3` 跳过并继续向后扫描；
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

**`nearDoubleOn(res) → bool`**：该周期是否开启「近等双顶/双底平台取后顶/后底」——读 `CHAN_CFG.nearDouble3/15/60/240/D` 每周期开关。res 接受周期码（含 1H/4H/1D 别名）或 barSec 秒数 int（`buildStructureContext` 只有秒数）。五周期之外（'30S'/'5'/'30'/'W'/未知秒数/bool）一律 false——沿用旧口径 `intervalSecOf(res) >= 3600` 的失败安全语义；注意旧口径对 W(604800) 会开启、此处收窄为 false（全链路无 W 调用点，零实际影响）。

### 2.8 MACD 背驰

**`biMacdMetrics(bi, macdArr) → {redArea, greenArea, difHigh, difLow, redMax, greenMax} | null`**
- 计算一笔的 MACD 指标：红柱面积（macd>0 部分累加）、绿柱面积（macd<0 部分绝对值累加）、DIF 高点/低点、红柱最大高度（单根柱最大值）、绿柱最大高度（单根柱绝对值最大值）；笔内无数据返回 null。

**`isBiDiverge(bi, refer, macdArr) → boolean`**（MACD 背驰判定，OR 关系满足其一）
- **底背驰**（对应一买，下跌笔，2026-10-01 起双判据 AND）：黄白线低点抬高（`cur.difLow > ref.difLow`）**且**（时长可比时）绿柱面积变小（`cur.greenArea < ref.greenArea`）；
- **顶背驰**（对应一卖，上涨笔）：黄白线高点变低（`cur.difHigh < ref.difHigh`）**且**（时长可比时）红柱面积变小（`cur.redArea < ref.redArea`）；
- **面积判据受时长可比门约束**（`areaDurComparable`）：面积 Σ = 柱高×K线根数、与区间时长线性相关——两段时长比 > `CHAN_CFG.divergeDurRatio`（默认 3，某段时长为 0/负视为不可比）时面积项不计入，**DIF 单判据兜底**（长慢段对短急段的经典 DIF 背驰仍可判出）。
- （旧口径为 面积/DIF/单根最大柱高 三项 OR 任一命中——2026-10-01 废除柱高项并收紧为 AND：柱高型「时间稀释」背驰（面积、DIF 均走强仅单根柱变矮）不再判背驰，1买/1卖 与上级收笔同步收紧。）

### 2.9 买卖点

**`findBuyPoints(bis, upperBis, macdArr, barSec) → points[]`**

- **1买**：下跌笔创新低（`cur.endPrice < refer.endPrice`，参照为之前最近的幅度 ≥ 当前 50% 的下跌笔）+ MACD 背驰；**同笔例外**（2026-09-15）：与上级笔完全重合且命中的上级笔已结束（非末笔）→ 纯结构标记 1买（不选参照、不比创新低/背驰——本级无内部结构，跨上级笔边界的比较无意义）；命中上级末笔（延伸中、反向笔未确认）仍跳过；全部保留；
- **2买**（区间套，不依赖中枢）：上级上涨笔内首个 `price > up.startPrice`；无上级用「结构底」；
- **类2买**（依赖中枢）：2买之后、同一中枢内每一个更高抬低；低点可低于收敛后的 `zd`，但该下跌笔须与 `[zd - class2ZsTol, zg]` 重叠；无中枢不标；
- **3买 / 类3买 / 4买 / 类4买**（依赖中枢）：上涨破 `zg` 后回踩 `price > zg - thirdZsTol`；同段按时间顺序全部标出：第1个=3买、第2个=类3买、第3个=4买、第4个及以后=类4买；无中枢不标；
- 输出类型：`{ type: "1买"|"2买"|"类2买"|"3买"|"类3买"|"4买"|"类4买", time, price }`。
- 返回序（2026-10-01 起）：按 `time` 升序**稳定排序**（识别仍按 2/类2→1买→3/4类 三段写入，return 前统一排序）；同刻多笔保持识别序（2/类2 前、1买 后、3/4类 最后），**尾点=时间最新**。

**`findSellPoints(bis, upperBis, macdArr, barSec, class2ZsTol, thirdZsTol) → points[]`**（与买点对称）
- 返回序（2026-10-01 起）：同 findBuyPoints——`time` 升序稳定排序，同刻保识别序（2/类2→1卖→3/4类），尾点=时间最新。
- **1卖**：上涨笔创新高 + MACD 背驰；**锚定**（`anchorFirstSell`）到上级上涨笔结束点，多个候选锚到同一位置去重；同笔例外与 1买 对称（上级笔已结束 → 纯结构标记，命中上级末笔仍跳过）；
- **2卖**（不依赖中枢）：上级下跌笔内首个 `price < dn.startPrice`；无上级用「结构顶」；
- **类2卖 / 3卖 / 类3卖 / 4卖 / 类4卖**：依赖中枢。类2卖 = 2卖之后、同一中枢内每一个更低次高（高点可高于收敛后的 `zg`，但该上涨笔须与 `[zd, zg + class2ZsTol]` 重叠）。离枢反弹同段按顺序全部标出：第1个=3卖、第2个=类3卖、第3个=4卖、第4个及以后=类4卖（反弹 `< zd + thirdZsTol`）。

**`anchorFirstBuy(cand, upperBis) → {time, price}|null`**
- 低级别一买锚定到「上一级别某笔的起点」（时间上最近的一个底部端点）；找不到返回 null。

**`anchorFirstSell(cand, upperBis) → {time, price}|null`**
- 一卖锚定到「上一级别上涨笔的结束点」：若候选位于某上级上涨笔内部 → 上移到该上涨笔结束点；否则取时间最近的顶部端点；找不到返回 null。

**`isSameAsUpperBi(bi, upperBis, barSec) → 命中笔对象|null`**（2026-09-15 起返回命中笔，旧 bool 契约兼容）
- 本周期某笔是否与上一级别某笔完全重合（起终点时间与价格一致，时间容差 = 本周期 1 个 bar、价格容差 0.01）；重合（同笔）说明内部无更细结构——上级笔已结束时由本周期做纯结构标记，上级末笔（延伸中）不标记；返回命中的上级笔对象（与 upperBis 元素同引用，供调用方判末笔）或 null。

**`snapToOwnBar(price, refTime, bars) → time`**
- 把极值价格/时间映射到本周期K线的 bar 边界（找高低价匹配且时间最近的K线；找不到取时间最近K线）。

**`keepRecentEach(points, keep = 1) → points[]`**
- 每类买卖点只保留时间上最近 `keep` 个（默认 1，即每类只保留最近一个）；返回按时间升序排列。
- `keep === 1` 时每类只保留最近一个；`keep > 1` 时每类按时间倒序取最近 `keep` 个再升序返回。
- 历史保留函数，现行主流程不调用（由 `keepRecentAll` 取代）。

**`keepRecentAll(points, keep = 10) → points[]`**
- 买卖点**不分类**（买+卖合并）只保留时间上最近 `keep` 个（默认 10）；返回按时间升序排列。
- 现行主流程保留策略：`mark-buy-sell` 每周期在邻近合并后调用，控制图上标记数量。

## 3. 配置项

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `CHAN_CFG.gapFilter` | 1.0 | 跳空独立成笔阈值（相邻K线缺口 ≥ gapFilter×ATR 强制独立成笔） |
| `CHAN_CFG.wickMarkOn` | true | 长影线修复总开关（参数页「长影线修复」卡）。关闭时 `markWickBars` 原样浅拷贝返回，wickRatio/wickAtrK 不生效（2026-10-02） |
| `CHAN_CFG.wickRatio` | 0.70 | 长影压平/标记的影线占比阈值（影线 ≥ wickRatio×振幅 触发，见 §2.0） |
| `CHAN_CFG.wickAtrK` | 0.5 | 影线绝对长度下限系数（影线 ≥ wickAtrK×稳定ATR 才处理；窄幅盘整小K线免疫） |
| `CHAN_CFG.wideBarOn` | true | 「顶底分形不能包含」单根长K豁免总开关（参数页同卡 checkbox）。关闭时 `wideBarPointsOf` 一律返回 0（所有周期不豁免，2026-10-02） |
| `CHAN_CFG.divergeDurRatio` | 3 | 背驰面积判据的时长可比上限（两段时长比 > 该值则面积项不计入，见 §2.8） |
| `CHAN_CFG.nearDoubleFixed` | 2.0 | 近等双顶/双底固定容差（品种报价单位绝对价差，如黄金 2.0=2 美元）；thr = 该值（2026-10-02 起取消 ATR 项/比例项/15m双动能确认）。比较先影线后实体：后实体不低于前实体直接后移，更低则差≤该值才后移；中间回调深度闸门同用该值 |
| `CHAN_CFG.nearDouble3/15` | true/true | 3m/15m 近等双顶开关（2026-09-28 起默认开） |
| `CHAN_CFG.nearDouble60/240/D` | true/true/true | 60m/240m/D 近等双顶开关；gating 统一走 `nearDoubleOn(res)` |
| `CHAN_CFG.nearDoubleRebound` | true | 近等后顶/后底（反弹不成笔）取后总开关（规则 #6b，§2.4.2-2b，2026-09-26）。关闭后仅 k.locked（上级笔端点，区间套强制落地）路径仍生效；周期门控沿用 nearDoubleOn(res) |
| `CHAN_CFG.debug` | false | 调试打印（buildBi / 买卖点识别过程） |

## 4. 依赖关系

| 上层模块 | 复用关系 |
|----------|----------|
| `chan-bi`（画笔） | `markWickBars`/`mergeBars`/`findFractals`/`countRaw`/`hasGapBetween`/`buildBi`/`fixBiExtremes`/`lockedPivotsOf`/`alignBiToUpper`/`calcATR`/`calcMACD`/`hasMacdCrossBetween`/`extendLastBi`/`lowerResOf`/`calibrateBiTimes`/`intervalSecOf` |
| `mark-buy-sell`（买卖点） | `mergeBars`/`findFractals`/`countRaw`/`hasGapBetween`/`buildBi`/`calcATR`/`calcMACD`/`hasMacdCrossBetween`/`extendLastBi`/`lowerResOf`/`calibrateBiTimes`/`intervalSecOf`/`fmtT`/`biMacdMetrics`/`isBiDiverge`/`findBuyPoints`/`findSellPoints`/`anchorFirstBuy`/`anchorFirstSell`/`isSameAsUpperBi`/`snapToOwnBar`/`keepRecentAll` |
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
| 下影探底 | 不产生候选价（分型结构仍用压平后的 low）；真低走 `_origLow` → `fixBiExtremes` 恢复通道（§2.0/§2.4.5），仅在它是笔底区间被掩盖的绝对极值时生效，不影响分型与跳空判定 |


## 一小时近等端点补充确认（2026-09-10 起；**2026-10-02 已取消**）

本分支已整体删除：取消 `nearDoubleAtrK`/`nearDoublePct`/`nearDoubleLowerRelax`/`nearDoubleLowerRatio` 四个参数与 `makeBiLowerContext`/`lowerEndpointWeaker`/`buildBi.lowerContext` 管线；容差只保留 `nearDoubleFixed`（默认 2.0），比较改为「先影线后实体」（见 §2.4.2-2）。历史背景：原为仅60分钟价差在阈值至 1.5 倍之间时，以 15 分钟同色柱峰值与同侧 DIF 幅度同时衰减至 50% 以内确认后端点（目标小时K线 8-7 08:00 4229.875，15 分钟精确极值 08:45）；新规则下该端点经「后底实体 4232.90 低于前底实体 4233.815 → 直接后移」到达，不再依赖 15m 数据。
