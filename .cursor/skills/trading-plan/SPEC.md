# 交易计划 trading-plan 规格（SPEC）

> 文档用途：描述「trading-plan」SKILL 的功能规格——**2026-10-16 起 regime-only**：
> 只保留①震荡 regime（`--range-res`，默认关闭）与②顺势方向状态（`--trend-res`，默认关闭）
> 两项判定。开单链路改为 mark-entry 直接消费 chan-core 买卖点（点类型+强弱 → 进场键），
> 原「逐周期方向+策略」计划行（predictPlan/strategyOf/classifySecond）、计划表与图上
> 蓝色策略文字已删除；脚本仅输出摘要、落盘 regime-only 结果，并清除图上遗留旧标记。
> 对应脚本：`.cursor/skills/trading-plan/scripts/trading_plan.js`；Python 同构：
> `py_chain/trading_plan.py`（`compute_regime` / `trend_state_of`）。
> 算法来源：缠论算法复用 `chan-core`（唯一算法源）；震荡判定 `isRangeBound` 复制自
> `chan-status`（不迁移、不修改）。

## 1. 输入

| 项 | 来源 | 说明 |
|----|------|------|
| 笔数据 | `chan-bi` 画笔落盘 `.cursor/cache/bis_<品种>.json`（**强制依赖**） | 各周期已 ATR 过滤、未完成笔延伸、跨周期端点校准的最终笔，与图上所画笔一致 |
| 最新K线 | TradingView 图表实时读取（逐周期切换） | 计算 ATR / MACD / 最新收盘价（震荡判定与顺势方向用） |
| 品种/当前周期 | 图表自动读取 | `chart.symbol()` / `chart.resolution()` |
| 参数 | 命令行 | 见下表 |

### 参数表

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--range-res=240\|D\|off` | `off` | 震荡判定参考周期；开启=参考周期 regime 震荡 → 更低检测周期观望（mark-entry 震荡闸消费）；关闭=无震荡闸直接走买卖点 |
| `--trend-res=240\|D\|off` | `off` | 顺势参考周期（§5 方向状态供 mark-entry 顺势过滤） |
| `--periods=...` | `D,240,60,15,3` | 参与的周期列表（从大到小） |
| `--range-bar-n/--range-bi-n/--range-k-mult/--range-bi-mult/--range-break-mult` | `40/4/5/7/1` | 震荡判定阈值（窗口K线数/最近笔数/两 ×ATR 阈值/突破跳过阈值） |
| `--range-bound-on/--range-zs-on` | 开/开 | A 横盘支 / B 中枢内支开关 |
| `--trend-rebound/--rebound-near-pts/--rebound-angle-ref` | 开/5/5 | 相位判定开关与参数（§5） |
| `--dry` | 关闭 | 不动图（不清除旧标记） |
| `--debug` | 关闭 | 打印调试信息 |

## 2. 处理流程

### 2.1 数据读取与覆盖

- 逐周期 `ensureResolution(res)` 切换周期并等待K线加载稳定；
- `fetchBars(expectedIntervalSec)` 读取K线（用最后 20 个相邻间隔中位数判断真实周期，与期望不符重试）；
- 强制读取笔数据缓存：文件不存在 / 品种不匹配 → 报错退出（必须先运行「画笔」）；
- 收笔要下一级笔：按周期从小到大取K线并 `buildStructureContext` 建结构。

**数据来源时效性**：价格/K线实时；`bis` 为 chan-bi 快照（关键行情节点前建议先刷新画笔缓存）。

### 2.2 震荡 regime（computeRegime；对齐 py_chain compute_regime）

- `--range-res` 关闭（默认）→ 返回 `null`：无震荡闸，各周期直接走买卖点；
- 开启时对**参考周期**（4小时/日线）判震荡（`rangeGateRow`）：
  - 参考周期笔 <2（含整行无数据）→ `{range:true, insufficient:true}`（更低周期观望·笔数据不足）；
  - **A：横盘整理**（`isRangeBound`，复制自 chan-status）：最近 `rangeBarN=40` 根K线 `maxHigh-minLow ≤ 5×ATR`；笔端点区间（≤ 7×ATR）与涨跌交替只取「最近 40 根K线时间范围内」的笔判断（窗口内无笔则跳过这两条）；**突破跳过**：最后一笔终点相对窗口区间另一端明显偏移（> `rangeBreakMult`×ATR）→ 视为突破盘整跳过 A；
  - **B：中枢内**：中枢构建在**上一级别同一笔**内（`buildZSByUpper(bis, upperBis, barSec)`；无上级笔退化 `buildZS`）；取上一级别最后一笔对应段内最后一个**未离开**（`exitTime === null`）的中枢，`lastPrice` 位于 `[zd, zg]` 内；
  - A/B 任一命中 → `{range:true, resName:"4小时", reason:"4小时：…判定为震荡整理"}`；否则 `{range:false}`。
- 消费方（mark-entry 两侧）：regime 震荡/笔不足 → 更低检测周期全部观望（`range_watch`）；
  参考周期及以上不作检测周期（`res_ge_range`，只作锚）。

### 2.3 顺势方向状态（trendStateOf）

`--trend-res` 开启时计算参考周期方向状态（规则全文见 §5）；关闭时为 `null`（不过滤）。

### 2.4 摘要输出与旧标记清理

- 控制台输出两行摘要：`[震荡regime] 参考周期 …：震荡·观望/非震荡/已关闭`、`[顺势] 参考周期 …：多/空/无方向`；
- 非 `--dry` 时逐周期清除遗留的旧 `CHAN_PLAN_<周期>` 策略文字（**不再绘制新文字**）。

### 2.5 结果落盘（regime-only）

- 每次运行（含 `--dry`）把结果写入 `.cursor/cache/plan_<品种>.json`：
  `{ symbol, generatedAt, rangeRes, regime, trendRes, trend }`；
- **`mark-entry` 不再读取该文件**（2026-10-16 起自算买卖点与 regime）；仅供工作台查询/审计。

## 3. 输出

| 项 | 说明 |
|----|------|
| 控制台摘要 | 震荡 regime 与顺势方向各一行（开启时） |
| regime-only 落盘 | `.cursor/cache/plan_<品种>.json`（regime/trend 两字段） |
| 旧标记清理 | 清除图上遗留的 `CHAN_PLAN_*` 蓝色策略文字（不再绘制新文字） |

## 4. 边界情况

| 场景 | 处理 |
|------|------|
| 未找到 TradingView 页面 | 报错退出 |
| 笔数据文件缺失 / 品种不匹配 | 报错退出，提示先运行「画笔」 |
| 某周期无K线或切换失败 | 跳过该周期数据 |
| 参考周期笔数据为空 / <2 笔 | regime 预置 `{range:true, insufficient:true}` |
| `buildZS/buildZSByUpper` 抛错 | try/catch 忽略，跳过中枢震荡判定（B 支） |
| `--range-res` 与 `--trend-res` 同时开启 | 两个闸门独立叠加（震荡观望 + 顺势方向过滤） |

## 5. 参考周期方向判定（顺势过滤，2026-09-15 口径 + 2026-09-17 相位树；规则不变）

归属本模块（`trading_plan.js` 导出 `trendDirection/trendStateOf/strongFractalAfter/phaseDirection`；
`py_chain/trading_plan.py` 同构实现），供 `mark-entry` 进出场过滤消费：**参考周期**
（默认关闭 `TREND_RES=""`；可配 `"240"` / `"D"`，WEB 参数中心 plan 模块 `trendRes` /
mark-entry CLI `--trend-res`）判上涨时更低检测周期只出多单，判下跌反之。

> **2026-09-17 统一更新**：Python与JS均实现相位树及预期结构上下文。相位默认开启，关闭保留确立方向至破坏／新点的行为。够笔按合并K线计数，活动端点不确认一类点；详见 [统一规范](../../../spec/plans/SPEC_structure_context.md)。

### 5.1 判定规则（`trendDirection`，逐条与代码核实）

1. **取点**：参考周期最近一个买卖点（`findBuyPoints`/`findSellPoints`，最近 60 笔窗口，按时间取最新）；
2. **1买/1卖**：端点后出现**强分型**才确立方向（强分型未出现 → 回退规则 5）；
3. **非1类点**（2买/类2买/3买、2卖/类2卖/3卖）：出现即确立方向——点锚定的笔本身已够笔成笔（chan-core isValid ≥5 根合并K）；
4. **破坏闩锁**：确立后参考周期任一**收盘价**跌破买点端点价 / 涨破卖点端点价 → 反向（`4小时下跌延续` / `4小时上涨延续`），**闩锁**到下一个买卖点事件——实现口径为从点时间起扫描全部收盘价、命中即反向，价格收回也不翻回，直到更新的买卖点出现（**Python 端闩锁优先于规则 7 相位树**）；
5. **回退**：无任何买卖点、或 1类点强分型未出现 → 参考周期最近一笔方向（末笔 up→多 / down→空，`4小时末笔向上/向下`）；
6. **数据不足**：参考周期笔 <2 → 方向 `None`，消费方不过滤（多空照常）；
7. **相位树**（闩锁 4 未触发时）：锚点确立后按「形成段（延伸中的末笔）方向 → 够笔（段起点以来 ≥5 根本K 或锚点后已有确认同向笔）→ 位置（形成段极值距锚点前最近中枢 ZG/ZD 或前低/前高 ≤ `reboundNearPts` 绝对点数）→ 角度强弱（当前笔平均每根点数 > `reboundAngleRef` 即 >45° 为强；反弹/回调力度同口径）」分相位，可返回**观望**（dir=None 带 reason，双向放行带注记）；1卖后 9 叶、2\3卖后 5 行、买侧完全镜像；典型 reason：`1卖进行中/1卖转多预期/1卖近中枢观望/1卖后方向不明/1卖反弹/1卖强反/2卖预期/2/3卖前低附近/2/3卖反弹中`。

**强分型定义**（`strongFractalAfter`，2026-09-15 与用户确认；合并K/分型与 buildBi 同源：`markWickBars → mergeBars → findFractals`）：

- 只比实体、不含影线：右肩收盘价与左肩开盘价比较；
- 底分型：右肩收盘价 > 左肩开盘价；顶分型：右肩收盘价 < 左肩开盘价；
- 左/右肩不完整（尾部形成中）不计；分型中心时间早于点时间的不计。

### 5.2 消费方式（mark-entry 侧）

- **结构性剔除**：参考周期及以上不作检测周期（只作方向锚；`trendRes="D"` 时 240 恢复检测）。剔除只看 `trend_res` 字符串，与方向状态无关；
- **方向过滤**：状态方向非 `None` 且与策略方向相反 → 跳过该周期信号（确认制与当下制同口径）；`trendRes=""`（关闭）时完全不过滤；**观望态（dir=None 带 reason）双向放行**；
- **成因展示**：命中信号附 `trendDirection`/`trendReason`（观望态 dir=None 也附注记），回测信号列表方向列显示 `多（4小时1卖转多预期）` 式注记；成因格式 =「{参考周期名}{点类型/相位}」/「…下跌·上涨延续」/「…末笔向上·向下」，周期名映射 240→4小时、D→日线；
- **统一入口**：`trendStateOf(periodBis, barsByPeriod, trendRes, periodMacd?)`——上级周期取 `periodBis` 中比参考周期大一级的最小周期（240→D）供区间套使用；Python 端 evaluateRealtimeEntries 无 bars 入参，`trend_state` 由回测引擎在链路重算拍算好复用传入（Python 端额外形参 `cfg`：参数中心 trendRebound/reboundNearPts/reboundAngleRef）。

### 5.3 边界情况

| 场景 | 处理 |
|------|------|
| `trendRes=""`（关闭）或参考周期无笔数据 | 不过滤（多空照常）；结构性剔除仍生效 |
| 参考周期不在已加载周期中 | `trendStateOf` 返回 `dir=None` → 不过滤 |
| 点确立后价格收回（破坏后反弹回端点价上方） | 保持破坏方向（闩锁），直到新买卖点 |
| 同时刻出现买卖点（时间并列） | 按时间排序稳定取末位（确定性） |
| 相位观望态（dir=None + reason） | 双向放行，信号仍带 trendReason 注记 |
| 1卖后形成段向上但下跌未开始（段起点≤锚点） | 视同「1卖进行中」（空）；2\3卖反弹中默认观望，收盘涨破卖点价仍走闩锁 |
| 锚点前无中枢 / 无前低前高端点 | 位置按「其他位置」处理 |

## 6. 与其他模块的依赖

| 模块 | 关系 |
|------|------|
| `chan-core` | 复用 `calcATR/calcMACD/buildZS/buildZSByUpper/findBuyPoints/findSellPoints/intervalSecOf` 及 §5 强分型的 `markWickBars/mergeBars/findFractals` |
| `chan-bi`（画笔） | **强制读取**本脚本落盘笔数据 |
| `chan-status`（状态） | **复制**其内部 `isRangeBound`（不 require、不修改，保持 chan-status 独立） |
| `mark-buy-sell`（买卖点） | 独立；买卖点 → 进场键的直映射（pointStrategyOf）在 mark-entry 实现 |
| `mark-sr-flip`（支阻位） | 独立（本 SKILL 不读取支阻位） |
| `mark-entry`（进出场） | **上游依赖反转**（2026-10-16）：mark-entry 不再读取 `plan_<品种>.json`，改为 require 本模块 `trendStateOf`（§5 顺势过滤）与 `computeRegime`（§2.2 震荡闸），买卖点候选自算 |

**运行依赖链**（2026-10-16 起简化；trading-plan 为可选独立工具，不在进出场前置链中）：

```
画笔（chan-bi）→ 支阻互换位（mark-sr-flip）→ 进出场（mark-entry）
（可选：交易计划 trading-plan —— 震荡/顺势判定查询与旧标记清理）
```
