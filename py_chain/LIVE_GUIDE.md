# 实盘运行手册（EXNESS MT5 · live_trader）

配套规范：[spec/plans/SPEC_live_exness_mt5.md](../spec/plans/SPEC_live_exness_mt5.md)。
架构一句话：`live_trader.py` 常驻进程经 MetaTrader5 官方库（本机 IPC）驱动 MT5 终端；
引擎（backtest.py 零改动）为决策真相，券商侧每仓必带硬 SL，执行层镜像事件并对账收敛。

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
| 日亏熔断 | 当日 equity 回撤 ≥ `day_loss_halt_pct`(3%) | 停新开仓，存量管理到终局 |
| 点差门 | 点差 > `max_spread_entry`(0.5) | 该笔落 shadow（引擎照常），出场永不被拦 |
| 手数上限 | 单笔/总敞口/仓位数 | 拒新开仓（shadow） |
| 对账 | 每 `reconcile_min` 分钟 | 孤儿仓平掉；券商侧消失的仓脱钩；量不齐告警 |
| 实盘双条件 | `require_mode:"real"` + `data/LIVE_ARMED`（内容=确认日期） | 缺一拒启 |
| 参数漂移 | module_params.json / live 配置变更后重启 | 拒自动恢复，`--accept-param-drift` 显式放行 |

告警通道（可选 Telegram）：`notify.enabled=true` + `chat_id` + 环境变量
`LIVE_TG_TOKEN`（bot token）。事件全量落 bars.db `live_events`（审计主证据）。

## 4. 常见演练（模拟盘阶段至少各做一次）

- **杀进程**：任务管理器结束 python → 等自动重启 → 核对 `[live] 恢复会话`、
  `--audit` 全绿、MT5 终端持仓无变化。
- **断网 5 分钟**：恢复后看重连 + reconcile 日志；期间若券商 SL 打掉 → 该仓脱钩告警。
- **终端关闭重开**：live_trader 检测 IPC 断开自动重连（tick 异常捕获后继续轮询）。
- **跨周末**（EXNESS 周末有行情时）：feed 默认丢弃周末 M1（weekend=drop）。

## 5. 数据表（bars.db）

`live_state`（会话/游标/心跳）· `live_events`（事件流，保留 180 天）·
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
注册任务计划 → SSH 隧道访问 webapp 只读「实盘」页签（127.0.0.1:8001）。
上线顺序：VPS shadow 与本地对照 1 周 → 模拟盘 1 周 → 实盘小手数。

## 8. 已知边界

- 实盘周期表永不加 30S（M1 无法构成 30s 桶；要 30S 需 tick 数据另立项）。
- lots 为奇数时 TP2 半仓 < 最小手数：默认拒启，`risk.allow_odd_lots=true` 走全平降级。
- 若未来允许 240 作 markRes，backtest.py 的 barStart 模对齐会静默错位（现状
  markRes 恒 ∈ {3,15,60}，安全）。
- 风控上限按实例计数；多实例并行时账户总敞口=各实例上限之和，自行约束。
