# 技能模块分层索引（2026-09-29）

全仓库技能按**两大类 + 工具**分层。分层口径的唯一来源是 `py_chain/module_registry.py`（注册表），
工作台流程（`py_chain/web/analysis-catalog.json`）、参数页签（`py_chain/param_center.py` + `web/params.html`）、
回测/实盘入口校验（`webapp.normalize_cfg` / `live_trader.load_config`）均从该口径派生。

## 一、基础公用组件（所有交易策略共用）

结构计算与标注，不含任何「何时交易」的决策逻辑：

| 技能 | 职责 | 脚本 |
| --- | --- | --- |
| chan-core | 缠论算法核心库（**唯一算法源**：包含处理/分型/笔/中枢/MACD背驰/买卖点识别，纯函数） | `chan-core/scripts/chan_core.js` |
| chan-bi | 画笔（取K线 → 调 chan-core → 绘制 + 落盘 `bis_<品种>.json`） | `chan-bi/scripts/chan_bi.js` |
| chan-zs | 画中枢（读笔数据 → 绘制中枢矩形） | `chan-zs/scripts/chan_zs.js` |
| mark-buy-sell | 标记买卖点（1/2/3类，读笔数据） | `mark-buy-sell/scripts/mark_buy_sell.js` |

Python 侧对应：`py_chain/chan_core.py`（算法唯一源）。

## 二、交易策略（按策略成组，组内 = 支阻 + 计划 + 进场三步）

### 缠论V1（id=`chan_v1`，当前唯一策略）

| 技能 | 职责 | 脚本 |
| --- | --- | --- |
| mark-sr-flip | 标记支阻互换位（落盘 `srflip_<品种>.json`；2026-09-29 起归属策略组——支阻参数按策略经「方案列表单选生效」选定） | `mark-sr-flip/scripts/mark_sr_flip.js` |
| trading-plan | 交易计划（震荡/趋势分类 → 策略文案，落盘 `plan_<品种>.json`） | `trading-plan/scripts/trading_plan.js` |
| mark-entry | 标记进出场（读计划 + 笔 + 支阻 → 校验10种进场条件 → 出场阶梯） | `mark-entry/scripts/mark_entry.js` |

Python 侧对应：`py_chain/sr_flip.py` / `sr_service.py`（支阻）、`py_chain/trading_plan.py` +
`py_chain/mark_entry.py`（回测/实盘引擎链路，由 `BacktestEngine` 统一驱动，`live_trader` 复用同一引擎）。

## 三、工具（不属于分层）

| 技能 | 职责 |
| --- | --- |
| open-tradingview | 以 9222 调试端口启动 TradingView Desktop |
| chan-status | 各周期结构状态描述与买卖点预判（消费基础组件产物） |
| futures-backtest | EMA 交叉趋势策略回测（独立回测工具，**未纳入**策略注册表） |

## 新增交易策略接入清单（五步）

以「缠论V2」为例（详见 `py_chain/SPEC.md` 模块分层架构章节）：

1. **实现策略**：Python 引擎模块（实现 `step_to(t)` 驱动接口：推进到时刻 t → 返回 fills/exits/suppressed
   事件——满足此接口即可接入回测/实盘全链路）＋ 工作台 JS 技能脚本（计划与进场两步，复用 chan-core 基础算法）。
2. **注册**：`py_chain/module_registry.py` 的 `STRATEGIES` 加条目（title/stages/dependencies/param_modules/engine）。
3. **工作台**：`py_chain/web/analysis-catalog.json` 加模块条目（`category:"strategy"` + `strategyId`），
   `py_chain/analysis_service.py` 的 `SCRIPTS` 加技能→脚本映射——策略下拉与流程分组**自动生效**。
4. **参数**：`py_chain/param_center.py` `PARAM_MODULES` 加参数模块（`category:"strategy"`），
   `web/params.html` `TAB_GROUPS` 策略组追加页签——参数页**自动生效**。
5. **回测/实盘**：`webapp.normalize_cfg` / `live_trader.load_config` 已按注册表校验策略 id；
   第二策略到来时在 Worker 构建处与 `live_trader._build_engine` 按 `STRATEGIES[engine]` 分发引擎。

**实盘多策略并存 = 路径A 多进程**：每策略一个 `live_trader` 实例（独立 live_config + **不同 magic**）；
MT5 按 magic 隔离持仓，live_trades/live_orders/live_events 已按 session 隔离，kv 状态键已带策略后缀
（`live_store.state_key`）。待第二策略接入时补：/live 只读页多会话聚合、账户级风控分额/合计守卫。
