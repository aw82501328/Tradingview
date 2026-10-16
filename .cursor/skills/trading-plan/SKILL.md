---
name: trading-plan
description: Trading plan (交易计划) regime-only module — computes the 震荡(range) regime of a reference timeframe and the 顺势(trend-following) direction state (both off by default). Since 2026-10-16 the order-opening chain consumes chan-core buy/sell points directly (mark-entry); this module no longer produces per-period direction/strategy rows or draws plan text. Use when the user asks 交易计划, 震荡判定, 顺势方向.
disable-model-invocation: true
---

# 交易计划（regime-only）

> **2026-10-16 起本模块只保留两项判定**（开单链路改为 mark-entry 直接消费买卖点，
> 原「逐周期方向+策略」计划行与图上蓝色策略文字已取消）：
> 1. **震荡 regime**（`--range-res`，默认关闭）：参考周期判震荡 → 更低检测周期观望；
> 2. **顺势方向状态**（`--trend-res`，默认关闭）：参考周期方向，供 mark-entry 顺势过滤。

通过 CDP 连接 TradingView Desktop，读取 **`chan-bi`（画笔）SKILL 落盘的笔数据**，计算
参考周期的震荡 regime 与顺势方向并落盘；控制台输出摘要；同时清除图上遗留的旧
`CHAN_PLAN_*` 策略文字（不再绘制新文字）。

> **模块分类**：交易策略 · **缠论V1** 参数（`plan` 参数桶；分层见 `.cursor/skills/README.md`）。
>
> **算法来源**：缠论算法（买卖点识别、中枢）由 **`chan-core`**（`.cursor/skills/chan-core/scripts/chan_core.js`）提供（唯一算法源）；震荡判定 `isRangeBound` 复制自 **`chan-status`**（`.cursor/skills/chan-status/scripts/chan_status.js`，保持 chan-status 不被改动，复制并标注来源）。
>
> **强制依赖画笔数据**：本 SKILL **不计算笔**，**强制读取 `chan-bi` 画笔 SKILL 落盘的笔数据文件**（`.cursor/cache/bis_<品种>.json`）。文件不存在或品种不一致会**报错退出**，必须先对当前品种运行「画笔」（chan-bi）。

## 前置条件

1. TradingView Desktop 以调试模式启动：`TradingView.exe --remote-debugging-port=9222`
2. 已打开至少一张图表，且图表停留在**目标品种**上（脚本自动读取当前品种和周期）
3. chrome-remote-interface 已安装（在 `server-cdp/node_modules/`）
4. **已对该品种运行过「画笔」**（chan-bi SKILL），生成了 `.cursor/cache/bis_<品种>.json` 笔数据文件

## 使用方式

```bash
# 计算 regime/顺势（默认均关闭时只做清理与落盘）并清除旧计划文字
node .cursor/skills/trading-plan/scripts/trading_plan.js

# 开启震荡参考周期（4小时）：更低周期 regime 震荡即观望
node .cursor/skills/trading-plan/scripts/trading_plan.js --range-res=240

# 开启顺势参考周期（4小时）：输出参考周期方向状态
node .cursor/skills/trading-plan/scripts/trading_plan.js --trend-res=240

# 不动图（不清除旧标记）
node .cursor/skills/trading-plan/scripts/trading_plan.js --dry
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--range-res` | 震荡判定参考周期（`240`/`D`/`off`）；开启=参考周期 regime 震荡 → 更低检测周期观望（mark-entry 震荡闸消费）；关闭=无震荡闸直接走买卖点 | `off` |
| `--trend-res` | 顺势参考周期（`240`/`D`/`off`）；方向与信号相反时被 mark-entry 顺势过滤跳过 | `off` |
| `--periods=...` | 周期列表（逗号分隔，从大到小） | `D,240,60,15,3` |
| `--range-bar-n/--range-bi-n/--range-k-mult/--range-bi-mult/--range-break-mult/--range-bound-on/--range-zs-on` | 震荡判定阈值与两支开关；WEB 参数中心「交易计划」模块同名透传 | `40 / 4 / 5 / 7 / 1 / 开 / 开` |
| `--chan-cfg=<json>` | 缠论核心 CHAN_CFG 整体覆盖（WEB 参数中心透传，未知键闲置无害） | 不覆盖 |
| `--dry` | 不动图（不清除旧标记） | 关闭 |
| `--debug` | 打印调试信息 | 关闭 |

## 判定逻辑

### 1. 震荡 regime（`--range-res` 开启时；对齐 `py_chain/trading_plan.py compute_regime`）

对**参考周期**（4小时/日线）判震荡，A 或 B 任一命中即 regime 震荡：

- **A：横盘整理**（复制自 chan-status `isRangeBound`，增加一条突破跳过）：最近 40 根K线 `maxHigh-minLow ≤ 5×ATR`，且「最近 40 根K线时间范围内」最近 4 笔端点极差 ≤ 7×ATR、涨跌交替（无连续 3 笔同向）；**若最后一笔终点相对窗口区间的另一端明显偏移（> 1×ATR），视为突破盘整，跳过 A 判定**；
- **B：中枢内**：中枢构建在**上一级别同一笔**内（`buildZSByUpper`）；取上一级别最后一笔对应段内最后一个**未离开**（`exitTime === null`）的中枢，当前最新价位于 `[zd, zg]` 内。

regime 震荡 → 更低检测周期全部观望（`range_watch`）；参考周期笔 <2 → 同样观望（`insufficient`）；参考周期及以上不作检测周期（`res_ge_range`，只作锚）。**关闭（默认）→ 无震荡闸**，各周期直接走买卖点。

### 2. 顺势方向判定（`--trend-res` 开启时；规则与 2026-09-15 确认口径不变）

本模块导出 `trendDirection` / `trendStateOf` / `strongFractalAfter` / `phaseDirection` / `TREND_RES` / `computeRegime`（Python 端 `py_chain/trading_plan.py` 同构），供 mark-entry 进出场**顺势过滤**消费：

- 方向 = 参考周期**最近买卖点**：1买/1卖须端点后出现**强分型**（右肩收盘穿左肩开盘，只比实体、不含影线）才确立；非1类点出现即确立；
- 确立后参考周期**收盘价**破该点端点价 → 反向延续并**闩锁**到下一个买卖点（价格收回不翻回）；
- 相位树（`trendRebound` 开，默认开）：锚点确立后按「形成段方向 → 够笔 → 位置（中枢边界/前低前高）→ 角度强弱」分相位，可返回观望（双向放行）；
- 无买卖点 / 1类点强分型未出现 → 回退最近一笔方向；笔数据不足 → 不过滤；
- 完整规则见本 SKILL 的 `SPEC.md`；WEB 参数页「交易计划」页签展示同一份规则文本。

### 3. 策略行已取消（2026-10-16）

原「按最近笔端点买卖点三档路由出 12 种策略文案」（`predictPlan`/`strategyOf`/`classifySecond`）
已删除——进出场（mark-entry）直接读 chan-core 买卖点（点类型+强弱 → 进场键，
`pointStrategyOf`），不再读取 `plan_<品种>.json`。买卖点→策略键映射见 mark-entry SKILL。

## 结果落盘（regime-only，仅供查询/审计）

每次运行（含 `--dry`）都会把结果写入 **`.cursor/cache/plan_<品种>.json`**：

```json
{
  "symbol": "OANDA:XAUUSD",
  "generatedAt": "...",
  "rangeRes": "240",
  "regime": { "range": true, "insufficient": false, "resName": "4小时", "reason": "4小时：…判定为震荡整理" },
  "trendRes": "",
  "trend": null
}
```

> **`mark-entry` 不再读取该文件**（2026-10-16 起自算买卖点与 regime）；进出场运行前
> 无需先跑本脚本。运行顺序简化为：**画笔（chan-bi）→ 支阻互换位（mark-sr-flip）→ 进出场（mark-entry）**。

## 常见问题排查

| 问题 | 可能原因 | 检查命令 |
|------|----------|----------|
| ECONNREFUSED | TradingView 未以调试模式启动 | `netstat -ano \| findstr 9222` |
| 未找到页面 | 图表标签未打开 | 打开一张图表即可 |
| **报错「未找到 XX 的笔数据文件」** | 未先运行「画笔」SKILL（chan-bi），或缓存被清空 | 先对当前品种运行画笔（生成 `.cursor/cache/bis_<品种>.json`），再运行本脚本 |
| regime 判「笔不足·观望」 | 参考周期笔数 <2 | 用画笔 SKILL 重新画该周期 |
| 图上残留蓝色计划文字 | 旧版本遗留 | 再运行一次本脚本（非 `--dry`）即清除 |

## 注意事项

- 脚本自动读取**当前图表**的品种和周期，切换品种后需重新运行。
- **必须先对该品种运行「画笔」**（chan-bi SKILL）生成笔数据文件，否则报错退出。
- 本 SKILL **不画笔、不标记历史买卖点、不判断状态预判、不产策略行**（画笔用 `chan-bi`，历史买卖点用 `mark-buy-sell`，状态用 `chan-status`，策略映射在 `mark-entry`）。
- **笔结构为快照**：`bis` 来自 chan-bi 最近一次运行的缓存文件；在关键行情节点前建议先运行「画笔」刷新缓存。

## 预期笔与够笔运行笔（2026-09-17 更新）

下跌够笔且普通底分型右肩收盘后，预期上涨段立即参与下级买卖点、中枢归属和方向判断；卖侧镜像。合并后从起点块起满5块（含起点）才通过够笔门槛；进场预期够笔同样使用合并K线。活动端点不锚定已确认一类点，原始画笔缓存与结构视图分开。Python与JS共用同构 `buildStructureContext` / `mergedSegmentCount` 规则，详见 [统一规范](../../../../spec/plans/SPEC_structure_context.md)。
