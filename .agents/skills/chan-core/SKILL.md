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
| `buildBi(fractals, merged, atr, macdArr, lockedPivots, nearDouble, lowerContext)` | 笔构建（交替分型 + 回溯 + 跳空/MACD成笔 + 前顶前底作废 + 分型范围脱离 + 极值规则）；`nearDouble` 由调用方按 `nearDoubleOn(res)` 每周期开关取值 |
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
| `nearDoubleOn(res)` | 该周期是否开启「近等双顶/双底平台取后顶/后底」（读 `CHAN_CFG.nearDouble3/15/60/240/D` 每周期开关，默认 60/240/D 开；res 接受周期码或 barSec 秒数，五周期之外一律 false） |

### MACD 背驰

| 函数 | 说明 |
|------|------|
| `fmtT(ts)` | 时间格式化（调试打印用） |
| `biMacdMetrics(bi, macdArr)` | 一笔区间内的 `{redArea, greenArea, difHigh, difLow, redMax, greenMax}` |
| `isBiDiverge(bi, refer, macdArr)` | 背驰判定：柱面积变小 **或** 黄白线动能减弱 **或** 柱最大高度变小（OR） |

### 买卖点识别（2026-10-14 规则v2：背驰彻底退出买卖点判定，仅收笔判定用）

| 函数 | 说明 |
|------|------|
| `findBuyPoints(bis, upperBis, macdArr, barSec, class2ZsTol, thirdZsTol, upperMacd)` | 买点识别（规则v2）。**无上级（日线）返回空**。1买=已收笔上级上涨笔内部的小级别起点（`projectFirstPoints` 投影，同笔也标记，2026-10-16 起）；2买族=`secondFamilyV2`（1买锚后低点逐一抬高：首个=2买、与主点同一个中枢的后续=类2买）；3类族=`thirdFamilyV2`（同向离开中枢后回踩不进，逐中枢序号 3/类3/4/类4）。二三类按 `directionWindows` 方向开窗过滤：已收笔上涨笔窗只留买点、未收笔尾段双向放行（一类天然合规不滤）。3/4族点带 strong（`markThirdFourthStrong`：点后同向腿背驰=弱）——macdArr 参与该判定，upperMacd/class2ZsTol 仅兼容保留 |
| `findSellPoints(...)` | 卖点识别，与买点镜像（1卖=上涨笔终点/下跌笔起点投影；2卖族=1卖锚后压低；3类族=向下离开中枢反抽不进） |
| `projectFirstPoints(lowerBis, upperBis, barSec)` | 一类点投影（2026-10-13）：上级已确认笔端点 → 大级别笔**内部的小级别起点/终点**。落点阶梯（`lowerVertexOf`）：±1根本级K同价（最近）→ 上级一根K覆盖窗同价（最晚）→ 笔跨度内同价（最近）→ 跨度内同角色极值（底=上涨笔起点/下跌笔终点取最低，顶对称）→ 跨度内无同角色顶点才保持大级别时刻。搜索上界不越过下一个上级顶点。同笔（`isSameAsUpperBi`）两端也照常标记（2026-10-16 起），相邻笔共用端点先到先得只标一次 |
| `directionWindows(upperBis)` | 方向开窗（规则v2）：已收笔上涨笔窗=只买、下跌笔窗=只卖、最后已收笔终点之后（未收笔尾段）双向 |
| `secondFamilyV2(bis, firstPts, zss, c2tol, isSell)` | 二类族（规则v2）：锚后低点/高点逐一抬高/压低（未抬高只更新参照不标）；主点后与主点**同一个中枢**（按时间归属）的抬低=类2。每点带 strong（2026-10-27 锁弱口径）：反弹=锚后到**主点**为止的反向极值（买=锚后最高点）、前高=锚之前最近顶点——过=强、未过=弱；**只在主点判一次，同锚序列类2沿用主点档位**（主点后新高不改档；锚前无参照=null 弱先行） |
| `markThirdFourthStrong(points, bis, macdArr, barSec, isSell)` | 3/4 族强弱标注（2026-10-24 用户口径，find*Points 收笔前调用）：**弱=点后同向腿背驰**——弱3=2类点后首根上推腿(R2)对1类点后首根反弹腿(R1)顶背驰、弱4=3类点后首根上推腿(R3)对2类点后首根上推腿(R2)顶背驰（卖侧镜像=点后首根下推腿底背驰）；前点=同侧更早最近点（3族取(1类,2族含类2)、4族取(2族,3族含类3)）、refer=较早前点的腿/cur=较晚前点的腿、isBiDiverge 本周期 macd 三判据+时长闸；前点/腿缺失或 macd 空=判不出即强。语义=推动腿动能逐段衰竭，踩在衰竭趋势上的 3/4 类点为弱 |
| `thirdFamilyV2(bis, upperBis, zss, t3tol, isSell)` | 三类及以上族（规则v2，逐中枢序号制）：同向离开中枢边沿后回踩/反抽不再进该中枢（容差 thirdZsTol）→ 合格点。序号窗口=每个已收笔同向上级笔且**窗口内重新起算**：第1个被离开中枢出 3类/类3、第2个出 4类/类4、≥3 封顶类4；回踩重进则本轮结束（每枢计数不重置）；**本枢序列收到紧邻下一中枢的离开笔起点（含该时刻回抽；下一中枢未离开仍在 trioEndTime 截断）**；结束于中枢 rawEnd 的笔参与离开判定；同刻去重保留序号较新中枢的类型 |
| `isSameAsUpperBi(bi, upperBis, barSec)` | 本笔与上级某笔完全重合判断（时间容差=本周期1个bar；返回命中笔对象或 null） |
| `snapToOwnBar(price, refTime, bars)` | 极值价格/时间映射到本周期bar边界 |
| `keepRecentAll(points, keep=100)` | 买卖点不分类（买+卖合并）只保留时间上最近 keep 个（现行主流程保留策略；2026-10-14 起在标记层区间套合并后的完整列表上截断；2026-10-25 起签名默认随 KEEP 固化 100、此前 10——主流程均显式传 keep） |

### 缠论中枢

| 函数 | 说明 |
|------|------|
| `buildZS(bis, barSec)` | 构建笔中枢：三笔重叠形成 → 向后延伸 → 离开笔结束（终点破边即离开，起点不论——含穿越式离开；2026-10-07 起）；垂直区间=全部笔重叠（ZG=min高点，ZD=max低点），离开/回踩笔挤空重叠时回退为不含离开笔的重叠；水平边缘=[进入笔终点−5×barSec, 离开笔起点+5×barSec]；至少 3 笔即可输出 |
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
- 2026-10-14 规则v2 起：买卖点判定**不再使用背驰**（`isBiDiverge`/`biMacdMetrics`/`divergeDurRatio` 仅保留给收笔判定 `buildStructureContext`）；无上级（日线）`findBuyPoints/findSellPoints` 返回空；旧 API（`anchorFirstBuy/Sell`、`keepRecentEach`、`unformedTailSpan` 等）已随旧逻辑删除，备份见 git 历史 6971db6。


## 一小时近等端点补充确认（2026-09-10）

原阈值 max(0.3×ATR, 0.001×价格) 保持。仅60分钟价差超过原阈值但不超过1.5倍时，可由15分钟双动能确认：后段同色柱峰值与同侧DIF极值绝对值均不超过前段50%，两段柱峰值均非零，双底DIF均负、双顶均正。保留平台间隔、原阈值反向波动、锁定端点和单次替换保护；数据不足不走补充分支，不改变买卖点创新极值背驰定义。2026-09-25 起：阈值含固定容差并集项 `nearDoubleFixed`（默认 0 不启用）；本补充分支随 60m 周期开关 `nearDouble60` 可关。

JS/Python的`buildBi`增加可选末参`lowerContext`；用`makeBiLowerContext(res,bars,cutoff,macd)`准备15分钟数据和MACD索引，缺省参数保持旧行为。新增配置`nearDoubleLowerRelax=1.5`、`nearDoubleLowerRatio=0.5`。

画笔构建60分钟前需要完整15分钟历史（最近30天仅限显示）；回测、回放和监控仅使用当前决策时刻已收盘数据，先更新低周期。目标小时K线为8月7日08:00、4229.875，15分钟精确极值为08:45；固定样本仅目标相邻两笔变化，实际全量影响需回归检查。

完整条件、接口、保护和测试见[现行补充规范](../../../spec/plans/SPEC_near_double_lower_confirmation.md)。早期章节中原阈值的说明描述基础分支，与本补充分支共同适用。


## 近等后顶/后底（反弹不成笔）取后（2026-09-26）

「间隔不足→回溯替换」分支扩展，与近等平台取后顶（2026-09-25 参数化那组）互补：平台规则仅在两顶间已有两段首尾相接的成笔时禁止后移（单独一段成笔不挡）；本规则处理中间**已有合格反向分型**（回调甚至已成有效笔）、但 last→k 反弹/回撤腿 gap<4 拆不出反弹笔的「**回调够深、反弹太短**」场景。

- 条件（k 与 prev=result[-2] 同类型、分支前提 last→k 间隔 <4）：
  - 近等：`0 ≤ prev−k ≤ thr`（thr 同平台规则 max(nearDoubleAtrK×ATR, nearDoublePct×价, nearDoubleFixed)；仅60m可 1.5×thr + 15m 双动能确认）；
  - 真实回调：prev→last 反向幅度 ≥ thr；
  - 权限二选一：**k.locked**（上级笔端点=区间套强制落地，任何周期生效、不受 nearDoubleRebound 开关限制——上级已后移的端点必须在本级复现）或 nearDoubleOn(res) 开启周期（60/240/D）且 `nearDoubleRebound`（默认 true）；
  - 排除：prev.gapLocked / prev.locked / last.locked / prev.nearDouble（单跳封顶）；回溯替换保护（lastIsDeeper）仍优先。
- 效果：prev 让位、笔端点后移到 k（k 打 nearDouble 标记）；前顶/前底成为笔内极值（平台规则同类先例）。
- 例：60m 2026-09-18 15:00 顶 4399.67 → 22:00 底 4342.73（22:00/23:00 包含合并）→ 9-19 01:00 顶 4397.045，反弹腿仅 3 根合并K、近等差 2.625 ≤ thr；240 层平台规则已把 4h 顶后移到 9-19 01:00 → 60m 经 k.locked 复现：上涨笔 09-18 11:00 4334.295 → 09-19 01:00 4397.045，下跌笔至 09-21 22:00 4322.81。底对称。
- 测试：py_chain/test_near_double.cjs / test_near_double.py（fixture near_double_rebound_xauusd_20260919.json.gz：开关关=端点停 4399.67；k.locked 路径不受开关限制；镜像双底案例）。完整条件见 chan-core SPEC §2.4.2-2b（速查表 #6b）。


## 笔内极值守卫/极值落地（2026-10-25）

`biStep` 五处挂钩（总开关 `innerExtremeGuardOn` 默认开，参数中心 chan 桶；详见 chan-core SPEC §2.4.2-1b/速查表 #14b）：阶段二替换/顶替产生的**新配对**笔内藏着超近等容差（影线差与实体差都 > `nearDoubleFixed`）的更极值时，落地该极值为端点。动因：15m 2026-09-04 下跌笔终点 12:00@4465.49 后 14:45 更低底 4460.155 被间隔不足丢弃，15:45 更高顶同型替换 13:30 顶后上涨笔内藏比起点低 5.34 的低点（用户拍板端点应在 14:45）——落地后下跌笔 09:00→14:45@4460.155 + 上涨笔 14:45→15:45。

- 挂钩：①同型更极端替换（#5）前 (prev,k) 违例；②push 起点侧 (last,k) 违例（noMoreExtremeInside 只护终点侧；bridge 与起点同型 `_deeper_same_type_bar`）；③-⑤ `result[-2]=k` 顶替族（作废/区间极值版/间隔不足顶替）(prev3,k) 违例（起点/终点两形态）。全部**修复优先**——落地不成维持原行为（拒绝式拦截会死锁端点推进，A/B 实证违例爆炸后废弃）。
- 触发判据 `_pair_inner_violation_idx`：影线差与实体差**都超**容差才修——近等族（#6/#6b/#6c）按实体容差刻意取后者不修（60m 2026-08-06 取后底 4229.875 vs 前底影线 4223.505 实体差 0.915 不动）。锁定端点（区间套优先）与跳空锁定笔不参与。
- 测试：py_chain/test_inner_extreme.py（真实 fixture inner_extreme_xauusd_20260904.json.gz + 合成边界）+ chan_core.test.js 镜像；py↔JS 对拍五窗口逐位一致；bi_inc 增量经 biStep 共用自动继承（test_engine_lock_parity 过）。全部回测基线不可比；LOGIC_V/ENGINE_LOGIC_VERSION → 20261025-inner-extrem-guard。




## 预期笔与够笔运行笔（2026-09-17 更新）

下跌够笔且普通底分型右肩收盘后，预期上涨段立即参与下级买卖点、中枢归属和方向判断；卖侧镜像。合并后从起点块起满5块（含起点）才通过够笔门槛；进场预期够笔同样使用合并K线。活动端点不锚定已确认一类点，原始画笔缓存与结构视图分开。Python与JS共用同构 `buildStructureContext` / `mergedSegmentCount` 规则，详见 [统一规范](../../../../spec/plans/SPEC_structure_context.md)。

## 缠论结构修正三件套（2026-10-01）

起因：15m XAUUSD 10-1 02:00–06:00 反弹（4147.415→4160.615）不成笔。三层规则过定共同吞掉该反弹，逐层修正后按标准缠论成笔。JS/Python 同步实现，跨端四周期逐笔一致。

1. **向下合并改标准低低（删 `_hiKeep` 创新高保留）**：高点按「压平前真实高点」（`_contain_high`）取较小者。两侧未压平=正统低低（04:45+06:00 取 4159.77，不再保留 4160.615 压制 04:30 顶）；块内含长影压平K且新K真实高点更高时，块高点不低于两者真实较小值（60m 9-21 18:00 压平 4345.73 vs 19:00 真实高 4371.11 → 块高 4356.77，17:00 底分型存活）。
2. **终点侧反向贯穿改实体口径**：`fractalRangeClear` 终点侧三根证据从影线极值改为 bodyTop/bodyBottom（回退 open/close、再回退影线）；影线刺穿不算（10-1 09:00 长阳高 4161.385 刺穿 04:30 顶 4160.41、实体顶 4159.67 未越过）。单根长K豁免（影线振幅口径）不变。
3. **平台取后可证伪回退（新规则 `6r`）**：`tryNearEqualSameType` 后移时记 `_platAnchor`；间隔不足分支**入口**（优先于 moreExtreme 顶替/近等后移）若原端点与 k 能完整成笔（`_pair_forms_bi`）且起点侧极值守卫通过（区间内无比起点更极值的同向极值，如 60m 9-18 04:00 底上方藏 4339.72 则不回退）→ 回退原端点并接入 k。例：15m 10-1 02:00 底被近等后移到 03:0、04:30 顶只剩 4 根合并K → 回退后 02:00→04:30 成笔（04:00 长下影压平使包含链多并一块是间隔被挤的诱因）。

回归：15m 全历史 294→342 笔、60m 74→82、240 22→24、D 不变；`_cmp_merge.py`/`_cmp_merge_js.cjs` 生产口径对拍；test_chan_core_rules / test_near_double(.py/.cjs) 断言已按新口径更新。详见 chan-core SPEC §2.1、§2.4.4 与速查表 #6r。


## 买卖点规则v2（2026-10-14，旧逻辑整体作废）

旧买卖点逻辑（每锚序列/背驰闸/破锚价作废/无上级创新极值一类/尾段补点等）整体作废，备份=git 提交 6971db6。新规则（买侧表述，卖侧镜像）：

1. **方向开窗（管全部点类）**：上级已收笔上涨笔窗 (startTime, endTime] 内小级别只保留买点、下跌笔窗只保留卖点、未收笔尾段（最后已收笔终点之后，含 enough 形成段）双向放行。一类点由生成笔方向天然合规，不参与窗口过滤。
2. **一类**：= 已收笔上级笔端点投影（`projectFirstPoints`，2026-10-13 实现）：上涨笔起点=1买、终点=1卖；下跌笔镜像。同笔也标记（2026-10-16 起；此前同笔抑制，60m 8-13 08:30 顶被 240m 下跌笔同笔删标）。**尾段不再补一类**（一类只认已收笔）。
3. **二类**：锚=1买。锚后低点逐一抬高（首个与锚价比、其后与前一候选比；未抬高只更新参照不标）→ 首个抬低=**2买**；其后与 2买**同一个中枢**内（按时间归属，`zsCoveringTime`——价格区间会把常在枢外的主点漏掉）的抬低=**类2买**，不在同中枢的不标（交给三类）。强弱（2026-10-27 锁弱）：**反弹=1买后到主点为止的最高点，前高=1买之前最近的顶点**——过前高=强二买、没过=弱二买；**只在主点（首个抬低）判一次，同锚序列后续类2沿用主点档位**——主点后出现的新高不改档（9-22 15m 案例：弱类2买后新高 4376.01 过前高 4374.94，类2仍弱）；只用已成结构，主点当下即判。
4. **三类及以上**：逐中枢序号制（`thirdFamilyV2`）：价格同向离开中枢边沿（买=上破 zg、卖=下破 zd）后，回踩不再进该中枢（容差 thirdZsTol）→ 合格点。序号窗口=每个已收笔同向上级笔且**窗口内重新起算**（修正：曾跨窗口累计致现窗口一律类4）：第 1 个被离开的中枢出 **3买/类3买**、第 2 个出 **4买/类4买**（用户定义与三类同构）、≥3 封顶类4买；回踩重进中枢则本轮结束等待下一次离开（每枢计数不重置）；**本枢序列收到紧邻下一中枢的离开笔起点（含该时刻回抽）**——下一中枢三笔重叠完成但还没离开时，不回进本枢的回抽仍算本枢的类点；下一中枢还没离开则仍在其 trioEndTime 截断（避免无界认领）；结束于本枢 rawEnd 的笔参与离开判定（上破笔常被 buildZS 吸收为末段延伸笔）；同刻去重保留序号较新中枢的类型。**强弱（2026-10-24）**：3/4 族点全带 strong——弱=点后同向腿背驰（弱3=2类后上推腿对1类后反弹腿、弱4=3类后上推腿对2类后上推腿，卖侧镜像；判不出即强），见 `markThirdFourthStrong`。
5. **背驰不参与点类判定**（2026-10-24 起 3/4 族强弱除外）：secondDivergeGate/firstNoDiverge 随旧逻辑删除（`strong2BuyMacdOn` 属 mark-entry 进场闸保留）；`isBiDiverge` 给收笔判定与 3/4 族强弱（markThirdFourthStrong）用。
6. **无上级（日线）不标**：`findBuyPoints/findSellPoints` 无 upperBis 返回 []；旧无上级路径（创新极值一类/结构底锚定）删除。
7. **动态更新（区间套笔区间粒度）**：`changedBiSpan`+`nestedDirtyPeriods` 级联脏传播到标记层后，脏区间 t0 之前沿用上次标记、之后取新算（mark-buy-sell JS/PY 同步；LOGIC_V → `20261014-point-rules-v2`）。

JS/Python 镜像逐位一致（TestPointRulesV2 ↔ JS「买卖点规则v2 二类/三类族」同名组）；全部回测基线不可比。
