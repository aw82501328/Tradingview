# 实盘运行手册（EXNESS MT5 · live_trader）

配套规范：[spec/plans/SPEC_live_exness_mt5.md](../spec/plans/SPEC_live_exness_mt5.md)。
架构一句话：`live_trader.py` 常驻进程经 MetaTrader5 官方库（本机 IPC）驱动 MT5 终端；
引擎（backtest.py 零改动）为决策真相，券商侧每仓必带硬 SL，执行层镜像事件并对账收敛。

> **触价口径挂线（2026-10-21 `exitTouchOn`，缠论V1 默认开）**：价格线类出场（支阻止损/
> 保本/跟踪止盈/最大止损兜底/锁定盈利）引擎在触发根按线价即时成交；实盘把
> `backtest.next_touch_lines` 中**第一条会全平剩余手数的线**挂成券商 SL（`_eff_stop_of`
> 与回测触价段同一候选源；部分平仓线如跟踪止盈 25% 不挂，由引擎收盘事件后市价分批，
> deviation 记录）。锁定价随峰值每拍上抬 → `modify_sl` 跟随（reason=lock）。券商 SL
> 盘中先平时引擎终局/部分事件按 `broker_sl_first` 收口（跳过市价单、镜像行 closed）。
> 关闭 `exitTouchOn` 实盘退回旧口径（SL 对齐 stopRef/beStop 有效止损位）。

## 0. 前置准备（一次性）

1. **EXNESS 开户**：个人区开 Standard 模拟账户（MT5 / USD / 对冲模式）。记录登录号、
   服务器名（如 Exness-MT5Trial7）。实盘另开户，**先入最小额**。
2. **装 MT5 终端**并登录；确认 XAUUSD 出现在 Market Watch（注意 RAW 账户后缀
   XAUUSDm，需在配置 symbol 中对应）。
3. **终端设置**：Tools → Options → Charts：Max bars in history / in chart 均
   **Unlimited**；保存密码自动登录（无人值守重启自动连上）。
4. **Python**：`python -m pip install -r requirements.txt`（MetaTrader5 库仅 Windows；
   本机实测 3.13.8+numpy2.2 可用，代码已适配 numpy≥2 下标字段访问）。
5. **Windows**：电源计划禁睡眠；（服务器）任务计划设终端开机自启。
6. **配置**：复制 `py_chain/web/live_config.json` 为 `live_config.local.json`（已
   gitignore），填入 `account.login`（登录号）与 `account.server`（服务器名）。
   **密码不写任何配置文件**——只存终端凭据。
7. **强分型均线V1（fxma_v1）并行实盘（可选）**：复制 `py_chain/web/live_config.fxma.json`
   为 `live_config.fxma.local.json` 填账号（**magic 必须与缠论V1 不同**，模板已用
   20261101）；策略参数（进出场周期/类别/均线/止损止盈/默认手数）在参数中心
   「交易策略 · 强分型均线V1」按品种设置，live_config 只放账户/风控/运行参数。
   启动：`python -m py_chain.live_trader --config py_chain/web/live_config.fxma.local.json`
   （或 `--strategy fxma_v1` 覆盖默认配置的策略键）。**30S 周期实盘不支持**——
   entryRes 含 30S 时 load_config 拒启（MT5 行情由 M1 重采样构成）。账户级合计守卫
   （跨 magic）：`risk.max_account_total_volume` / `max_account_positions`，超限停新开仓。

## 1. 启动与验证（分阶段）

```powershell
# M1 环境 probe（账号守卫/spec/周末有无/维护窗/H4-D1 边界 → data/mt5_probe.json）
python -m py_chain.mt5_feed probe
# M2 数据对拍（重采样 vs bars.db OANDA → data/mt5_align_<date>.json，锚点物证）
python -m py_chain.mt5_align --days 14
# M2 深拉历史 M1 入库（EXNESS:XAUUSD，绝不写 OANDA:*）
python -m py_chain.mt5_feed history --days 365
# 单测（无需终端）
python -m unittest py_chain.test_mt5_feed py_chain.test_mt5_broker py_chain.test_live_trader
# shadow 模式启动（默认；只记录不下单，日志 data/live_trader.log）
python -m py_chain.live_trader
# 模拟盘真实下单（确认 shadow 链路无误后）
python -m py_chain.live_trader --no-shadow
# 单拍联调 / 审计
python -m py_chain.live_trader --once
python -m py_chain.live_trader --audit
```

**上线顺序（不可跳步）**：shadow ≥2 交易日（`--audit` 全绿）→ 模拟盘真实下单 ≥1 完整
生命周期 → （切实盘时）实盘账号 shadow 1 周 → 实盘小手数。

## 2. 无人值守（任务计划：开机自启 + 崩溃自动重启）

以管理员 PowerShell 注册（路径按实际仓库位置调整）：

```powershell
schtasks /Create /TN "live_trader" /RL HIGHEST /SC ONSTART /RU <用户名> ^
  /TR "python -m py_chain.live_trader --no-shadow" /F
# 崩溃自动重启：任务计划 GUI → live_trader → 设置 → "如果任务失败，按以下频率重新启动
# 每 1 分钟，尝试 999 次"；或导出 XML 后改 <RestartOnFailure>。
```

重启即确定性重放恢复（10 天窗口 <2s），对账收敛后自动继续；重启日志看
`data/live_trader.log` 的 `[live] 恢复会话` / `[live] 挂接 trade#` 行。

## 3. 风控与急停

| 机制 | 触发 | 行为 |
|---|---|---|
| kill 文件 | `data/LIVE_KILL` 存在 | 拒新开仓并退出，存量保留（券商 SL 兜底）；`--kill-close` 全平后退出 |
| 策略开关 | /live 页「开启/关闭交易」按钮（写 kv `trading_enabled@<策略>`，缺键=开启） | 停新开仓（新信号转 shadow），存量管理到终局；进程离线时记录、恢复后 ≤1 个 poll 周期生效 |
| 日亏熔断 | 当日 equity 回撤 ≥ `day_loss_halt_pct`(3%) | 停新开仓，存量管理到终局 |
| 点差门 | 点差 > `max_spread_entry`(0.5) | 该笔落 shadow（引擎照常），出场永不被拦 |
| 手数上限 | 单笔/总敞口/仓位数 | 拒新开仓（shadow） |
| 对账 | 每 `reconcile_min` 分钟 | 孤儿仓默认只告警不平（`orphan_policy=close` 才平）；券商侧消失的仓脱钩；量不齐告警 |
| 实盘双条件 | `require_mode:"real"` + `data/LIVE_ARMED`（内容=确认日期） | 缺一拒启 |
| 参数漂移 | 引擎消费的有效参数 / live 配置 / 引擎逻辑版本变化后重启（2026-10-20 起语义指纹） | 券商无本 magic 持仓 → 自动放行重建（记日志含差异）；有持仓 → 拒启并列差异与票号，`--accept-param-drift` 显式放行（旧仓脱离引擎管理） |

告警通道（可选 Telegram）：`notify.enabled=true` + `chat_id` + 环境变量
`LIVE_TG_TOKEN`（bot token）。事件全量落 bars.db `live_events`（审计主证据）。

策略开关（2026-09-29）：`POST /api/live/trading`（body `{strategy, enabled}`）
仅写 live_state kv + `trading_disabled/enabled` 审计事件，不触碰 live_trader
进程与 live_config（无参数漂移拒启风险）；live_trader 每拍重读生效，shadow 模式
下 gate 原因仍报 "shadow模式"（更强的全进程事实）。多策略时 /live 页按 TAB 分策略展示。

**参数漂移守卫（2026-10-20 语义指纹）**：指纹 = 该策略实际消费的参数中心有效参数
（基础 chan/zs/points + 策略 param_modules，chan_v1 另含 sr/plan/entry、fxma_v1 含
fxma；sr 走 effective_sr）+ 解析后 live 配置 + 引擎逻辑版本（live_trader
`ENGINE_LOGIC_VERSION`，决策级引擎代码变更须 bump）。参数中心任何保存造成的
savedAt/格式/无关模块桶重写**不再算漂移**。守卫保护对象=重放与券商持仓的一致性：
券商侧无本 magic 持仓时**自动放行重建**（日志 `[live] 参数漂移但券商无本 magic
持仓，自动放行重建会话`，附差异明细）；有持仓才拒启，错误信息列出 `模块.键: 旧→新`
差异（截断 12 条）、持仓票号与处置路径（先在 MT5 平旧仓或 `--kill-close`，再
`--accept-param-drift` 放行重建）。session 存档带 `config_fp` 明细供差异比对
（旧存档无此键 → 显示「指纹口径变更」，无持仓时同样自动放行）。页面改手数走 kv
不动 live_config，不触发本守卫。entry 桶 2026-10-24 新增 `pointClasses`（买卖点
类别白名单，默认全选=引擎行为不变、`ENGINE_LOGIC_VERSION` 不 bump）入指纹——部署后
首次重启指纹即变属预期（无持仓自动放行；有持仓须一次 `--accept-param-drift`）。

## 4. 常见演练（模拟盘阶段至少各做一次）

- **杀进程**：任务管理器结束 python → 等自动重启 → 核对 `[live] 恢复会话`、
  `--audit` 全绿、MT5 终端持仓无变化。
- **断网 5 分钟**：恢复后看重连 + reconcile 日志；期间若券商 SL 打掉 → 该仓脱钩告警。
- **终端关闭重开**：live_trader 检测 IPC 断开自动重连（tick 异常捕获后继续轮询）。
- **跨周末**（EXNESS 周末有行情时）：feed 默认丢弃周末 M1（weekend=drop）。

## 5. 数据表（bars.db）

`live_state`（会话/游标/心跳/页面交易开关 `trading_enabled@<策略>`）·
`live_events`（事件流，保留 180 天）·
`live_orders`（每次下单/改单/平仓一行，含失败与 shadow）·
`live_trades`（引擎 trade 镜像 + MT5 ticket 挂接，对账核心）。行情复用 `bars` 表
symbol=`EXNESS:XAUUSD`。

## 6. 切实盘 checklist

1. `live_config.local.json`：`account` 换实盘登录号/服务器，`require_mode:"real"`。
2. 创建 `data/LIVE_ARMED`（内容写确认日期，如 2026-10-01）。
3. 实盘先 `--shadow` 跑 1 周，与模拟盘对照信号一致。
4. 复核 `lots`（默认 2=0.02 手）、`max_total_open_volume`、`day_loss_halt_pct`。
5. `--no-shadow` 开闸；首周每天核对 MT5 终端历史 vs `live_orders` 逐笔一致。

## 7. 服务器部署（M7）

Windows Server VPS（2C/4G/60G，伦敦机房低延迟）：装 Python 3.12 + MT5 终端（自动
登录）+ git clone 本仓库 + `pip install -r requirements.txt` →
`python -m py_chain.mt5_feed history --days 365`（重建 EXNESS:XAUUSD 数据）→
注册任务计划 → SSH 隧道访问 webapp「实盘」页签（127.0.0.1:8001，多策略 TAB +
策略交易开关）。
上线顺序：VPS shadow 与本地对照 1 周 → 模拟盘 1 周 → 实盘小手数。

## 8. 已知边界

- 实盘周期表永不加 30S（M1 无法构成 30s 桶；要 30S 需 tick 数据另立项）。
- lots 按出场方式比例换算的最小分批 < 最小手数（2026-10-08 起比例可配，如够笔止盈 50%
  × 奇数 lots）：默认拒启，`risk.allow_odd_lots=true` 走全平降级；缠论V1 守卫按参数中心
  生效配置的启用方式最小比例推导，全关止损类时启动告警。
- 若未来允许 240 作 markRes，backtest.py 的 barStart 模对齐会静默错位（现状
  markRes 恒 ∈ {3,15,60}，安全）。
- 风控上限按实例计数；多实例并行时账户总敞口=各实例上限之和，自行约束。

## 双周期链实盘（2026-10-23）

缠论V1 支持两条周期链各自独立实盘进程并行（链A=30S-3-15-60-240 在役；链B=
1-5-30-240）：

- **链A（现状不变）**：`live_config.json`（magic 20260923、data/LIVE_KILL、
  data/LIVE_ARMED、kv 键 chan_v1@*）；周期由参数中心·周期链 链A 勾选决定——
  勾 30S 拒启（MT5 由 M1 重采样无 30 秒行情；30S 仅回测）；fine 恒 3m。
- **链B（新）**：配置 `web/live_config.chain_b.json`（真实账号放
  `live_config.chain_b.local.json`，与 fxma 同款约定）——magic **20261023**、
  kill/arm 文件 `data/LIVE_KILL_B`/`data/LIVE_ARMED_B`、kv 键 chan_v1:B@*。
  启动前置：①参数中心·周期链 链B 总开关开；②本地库已有 1/5/30 周期数据
  （首启无数据会按 warmup_days 从 MT5 深拉）。fine=1m，行情由 M1 重采样出
  1/5/30（BIN_SEC 扩展；"1"=M1 直通）。
- **页面**：实盘页出现第三 TAB「缠论V1·链B」（虚拟槽位 chan_v1_b）——状态/手数/
  交易开关/持仓全部独立；进程检测按 config 文件名/live_config.chain_b 识别。
- **CLI**：`python -m py_chain.live_trader --config py_chain/web/live_config.chain_b.json`
  （或任意配置里 `"chain": "B"`；`--chain B` 可临时覆盖）。
- **指纹与漂移**：chain 模块属 chan_v1 param_modules → 两链进程的语义指纹都含整份
  链配置——改链B 勾选会同时变更链A 进程指纹（保守口径：有持仓拒启，需各自
  --accept-param-drift 放行）。ENGINE_LOGIC_VERSION=20261025-point-max-attempts
  （2026-10-25：同一点最大尝试次数 pointMaxAttempts 默认 2——同一买卖点（同检测
  周期+类别+锚点时间）终生最多实际开仓 2 次，成交建仓才计数（被同向互斥压掉
  未成交的信号不消耗），用满即拒 point_attempt_max；终局重置只清段去重不清
  尝试计数；0=不限（旧口径）。同日稍早：吸收成笔即标 absorbMarkFast + 合成K
  参与笔/点重算 synthIntrabarPoints（20261025-absorb-synth-points）、近支阻按
  方向取侧 srSideGateOn 默认开（20261025-sr-side-gate）——fib/BOLL 位按信号方向
  收窄，做多只认 SUP（上涨笔回撤支撑/布林下轨与下侧中轨）、做空只认 RES
  （下跌笔反弹阻力/布林上轨与上侧中轨），近支阻闸门与止损重选同池；
  密集区/人工位不过滤。同日晚：当前生效配置固化为代码默认值（7 键：windowDays15/
  divergeConfirm/pointClasses/keep/near/exitStopBeMode/chainB）——XAUUSD 有效参数
  逐位不变 → 语义指纹不变、ENGINE_LOGIC_VERSION 不 bump、无需 --accept-param-drift
  （其余四品种默认变化无实盘会话）。
  前版 2026-10-24：3/4类族拆强弱——点后同向腿背驰=弱；白名单 pointClasses 9→13 项，
  全部策略键去 wait 前缀、弱 3/4 锚点分流 3BuyBear/3SellBear/BuyBear/SellBear；
  实盘信号 strategyKey 全量换名，重启即按新口径，有持仓须一次 --accept-param-drift）。
- **风控**：每链实例上限独立计数；账户级跨 magic 守卫（positions(magic=0) 合计
  max_account_total_volume/max_account_positions）覆盖双链合计敞口。
- **默认口径变化**：链A 240 默认不勾（不在 240 开单，检测=60/15）——与 2026-10-22
  前的实盘信号集不同，重启即按新口径（有持仓须 --accept-param-drift）。
