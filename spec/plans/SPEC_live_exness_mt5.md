# 实盘接入 EXNESS MT5（全自动交易）

状态：M0–M4 代码完成（2026-09-23）；**M1+M2 真机验证完成（2026-09-24，Exness-MT5Trial5 模拟户 277967333 / XAUUSDm）**——probe/对拍/深拉/shadow 全链路跑通，真机暴露并修复 4 处集成缺陷（见「真机实测修正」节）。待 M5 模拟盘实单（需终端开启 AutoTrading）。算法引擎改动 = `fill_at_open_bar` 开关（默认关闭）+ `fine_res` 显式覆盖参数（默认 None 不改变回测行为）。

## 目标与边界

- 缠论算法全自动交易 EXNESS MT5 的 XAUUSD；先模拟盘跑通，再切实盘；未来部署 Windows 服务器。
- 单实例 XAUUSD 起步；多策略=多进程+独立 magic（见「多策略扩展」）。
- **总原则：引擎=决策真相（只信 bar），券商 SL=生存保底，镜像执行层=尽力执行+对账收敛。**

## 连接机制（信号→交易客户端）

不存在「把信号发送给客户端」环节——Python 进程本身就是交易者，直接驱动 MT5 终端：

```
live_trader.py（Python 常驻进程：算法+引擎+风控）
   │  ① 行情 mt5.copy_rates_from_pos("XAUUSD", M1, …)
   │  ② 下单 mt5.order_send({action: DEAL, type: BUY/SELL, volume, sl, deviation, magic, comment})
   │  ③ 持仓 positions_get / 改SL TRADE_ACTION_SLTP / 平仓
   ↕  MetaTrader5 官方 Python 库——本机 IPC，非网络 API（仅 Windows）
MT5 终端（本机常驻、自动登录 EXNESS 账号）↕ EXNESS 服务器
```

非 EA/MQL、非 Telegram/文件桥、非 EXNESS REST API。终端必须保持运行且已登录。

## 设计依据（已实测核实）

| 事实 | 影响 |
|---|---|
| bars.db 240/D 时间戳=纽约17:00锚定（240 落 UTC 01/05/09/13/17/21 夏令时，`%14400≠0`；D=NY 日界随 DST 漂移）；3/15/60=UTC 整数倍对齐 | M1 重采样：3/15/60 epoch 分箱；240/D 按 `zoneinfo("America/New_York")` 17:00 日界分箱；**禁用 MT5 自带 H4/D1**（EET 服务器日界在美欧 DST 相位差窗口偏离 NY17:00 一小时） |
| OANDA 日维护窗 21:00-21:59 UTC 无 bar；周六全天无 bar | feed 会话过滤丢维护窗+周末 M1（EXNESS 24/7，算法口径无周末结构），`weekend=drop` 默认开 |
| `step_to(execute=True)` 返回 `{"signals","fills","exits","suppressed"}`，exits 只含终局出场；TP1 保本/TP2 半仓是 open_pos dict 原地状态变化（beDone/halfDone/exits/stopRef） | 执行器持 fill dict 引用，每拍 diff 持仓快照镜像 be/half/止损外推 |
| `_fill_pending` 成交价=fine 次根开盘；stopRef/beStop 成交拍冻结（stopRef 可随 entryBarExt 外推放松） | 信号拍即市价单（≈次根开盘），SL 用信号拍 stop_ref_of 值，成交拍对齐差异则改单 |
| 引擎纯确定性状态机（bars+params+T_start→状态）；7 年全量重放 ~130s | **重启恢复=重放式**（execute=False 预热到 T_start→execute=True 重放到 now 抑制下单→按 live_orders 挂接 ticket→对账），不序列化引擎内部状态 |
| 1 回测手=0.01 标准手（mark_entry.py Exness 口径）；lots 偶数是 TP2 半仓（≥0.01）硬前提 | 推荐 lots=2（0.02 手）；lots=1 需 half→全平降级（显式接受） |
| webapp 全局互斥（_active_lock+ChartLock 独占 TD 图表） | 实盘=独立进程 `python -m py_chain.live_trader`；webapp 只加只读页签 |

## 真机实测修正（2026-09-24，M1+M2 验证所得）

1. **服务器时区=UTC+0 固定（非 EET）**：tick 与最新 M1 时间戳实测 server epoch≈UTC；
   `rule="utc"`（恒 0 偏移、无 DST）改为默认，us/eu EET 规则留作他服务器备用；
   mt5_align `dst_rule_pick="utc"` 确认。禁用 MT5 自带 H4/D1 结论不变（其日界
   =UTC 0:00 ≠ NY17:00）。
2. **MetaTrader5 Python 包（5.0.6180）无 TimeTradeServer/TimeGMT**（MQL5 专属）：
   动态偏移改由纯函数 `server_offset_from_tick`（mt5_feed）——最近 tick 服务器
   时间戳 vs UTC、整点量化、±14h 合法域、停盘残 tick 保持上次值；broker.server_now
   同源。原「TimeTradeServer-TimeGMT」表述全部作废。
3. **numpy≥2 的 np.void 不再支持属性式字段访问**（本机 3.13.8+numpy 2.2.3）：
   copy_rates_*/positions_get/history_deals_get 元素一律 `r["time"]` 下标访问。
4. **fine_res 自动探测误选 240**：OANDA 补深 240/D(265d) ≫ 3/15/60(101d) 时，
   「span≥90%·max_span」把时间轴选成 240（shadow 实测 fine_last 卡 240 形成桶）。
   修复=BacktestEngine 新增 `fine_res` 显式覆盖参数，live_trader 固定传 "3"；
   回测不传、行为不变。
5. **服务器 M1 深度上限 ~101 天**（Trial5）：history() 深度不足时自动 OANDA 补深
   240/D（幂等；seam 记录 live_state key=`exness_seam`）。
6. **M2 对拍实测**（14 天窗）：时间戳匹配 15m/240/D=100%（达标）；OHLC 差 15m
   中位 0.09~0.20 / p95 0.30~0.43 ——**锚点按实测修订为 中位≤0.25 / p95≤0.50**
   （原 0.05/0.30 作废；两源微结构差异固有）；单 bar 离群可至 ~5.8（15m 极值）、
   D 级 ~67（9/22 Exness 独有深低点，闪崩类）；点差按小时中位 0.24~0.26
   （max_spread_entry=0.5 维持）。

## 引擎改动：fill_at_open_bar + fine_res 覆盖（2026-09-23/24）

**发现**：`_step_execute` 成交槽条件 `i+1 < end_cut` 要求成交 bar 已进入已收前缀，即单次 step_to 调用需跨 ≥2 根新收盘 bar 才能冲销 pending——批量 run() 天然满足；但实时每拍仅推进 1 根（实测逐拍推进 fills 恒为 0，LiveMonitor/ReplayMonitor 历史上也从未逐拍成交过，实时模式一直只是信号模式）。

**改动**：`BacktestEngine(fill_at_open_bar=False)` 新增默认关闭的开关；开启时成交槽额外允许 `i+1 == end_cut 且 i+1 < len(fine)`（下一根 bar 以进行中形态存在，开盘价已知）。信息论上无未来函数：判定时刻 t_dec 恰为次根开盘瞬间，其开盘价在该时刻已知。**默认关闭 → run()/LiveMonitor/全部回测锚点位级不变**；仅 live_trader 构建引擎时开启。

**配套**（live_trader 侧）：① 数据源须供 fine 周期进行中 bar（MT5Feed 形成桶天然有；ReplayFeed 合成平开盘桶，收盘后真实值经 override 修正）；② beStop 收盘校正——同拍成交时引擎按 开盘价±保本滑点 冻结 beStop，进场 bar 收盘后由 live_trader 按真实极值±同滑量重算（写入引擎 trade dict，与批量口径等价；重放路径的成交 bar 为已收真实值、天然无需校正）；③ 重启恢复的大批量重放里成交走 `i+1 < end_cut` 原路径（预载已收 bar），最新一根 pending 若未冲销，下拍经开关补齐，成交 bar 与连续运行一致。

## 模块职责（全部新文件，中文注释；`import MetaTrader5` 仅限 mt5_feed/mt5_broker）

1. **`py_chain/mt5_feed.py`**（数据层）：`MT5Feed`——connect/probe（账号+spec+周末/维护窗实测→`data/mt5_probe.json`）；`srv_to_utc(ts, rule)` 分段纯函数（近 3 天动态 offset=TimeTradeServer−TimeGMT，历史按美/欧 DST 规则表，对拍实测定 rule）；`resample(m1,res)` 纯函数（空箱不出 bar；进行中末箱照常输出）；`history(min_depth_days)` 分块深拉→`data_store.upsert_bars("EXNESS:XAUUSD")`；`tail(n_m1)` 轮询。**绝不把 MT5 bar 写进 OANDA:XAUUSD**；M1 深度不足用 OANDA 补深 D/240 并记录 seam。
2. **`py_chain/mt5_broker.py`**（执行层）：`MT5Broker`——connect 账号守卫（login/server/trade_mode/margin_mode 必须 hedging，netting 拒启）；spec 探测（digits/point/volume_min/step/stops_level/freeze_level/filling 位掩码，不硬编码）；`market_order`（filling 旋转 FOK→IOC，10030 换档；成交以 history_deals 实际量价回填）；`modify_sl`（stops_level clamp）；`close_position`（可部分平）；`deals_since`；normalize_price/volume。`MockBroker` 同接口（滑点函数/inject_bar 判 SL 触发/fail_queue 注入 retcode）。
3. **`py_chain/live_store.py`**（持久层）：bars.db 新 4 表读写 + busy_timeout=10000 连接（与 webapp 跨进程共存，不开 WAL）。
4. **`py_chain/live_trader.py`**（编排层）：`LiveTrader`——start 守卫序列（kill_file→账号守卫→feed→参数中心+apply_cfg→config_hash 漂移拒启）→ preload → 重放式恢复 → reconcile → run_forever；tick（feed.tail→append_bars→step_to→_handle_fill 门控下单→_diff_open_trades 镜像→checkpoint）；`reconcile_plan()` 纯函数产出动作表。`ReplayFeed`（bars.db 回放，测试/演练）。CLI：`[--shadow] [--once] [--audit] [--config]`。
5. **`py_chain/mt5_align.py`**（对拍脚本）：重采样 vs bars.db OANDA 同窗口——时间戳匹配率、OHLC 差分布、缺口归因、DST 两规则匹配率、点差按小时分布→`data/mt5_align_<date>.json`。
6. 其余：`requirements.txt`、`py_chain/web/live_config.json`（模板）、`py_chain/LIVE_GUIDE.md`、测试 `test_mt5_feed/test_mt5_broker/test_live_trader`。
7. 可选存量小改：webapp.py `_engine_module_params`（8 行纯映射）上移 `param_center.engine_module_params`，webapp 改调用。

**关键规则——风控门只挡镜像侧，永不挡 step_to**：门控跳过下单时 trade 标记 `shadow=1`（引擎继续管理生命周期，对账豁免）。挡引擎会使其看不到止损 bar，而券商 SL 照打，制造更危险偏差。出场（平仓/改单）永不被点差门拦。

## bars.db 新表

```sql
live_state(key PK, value JSON, updated_at)                -- 游标/day_guard/session+config_hash/heartbeat
live_events(id, ts_utc, session, kind, symbol, payload)   -- append-only 事件流（审计主证据；保留180天）
live_orders(id, ts_utc, session, action, engine_trade_no, direction, volume, price, sl,
            request_ticket, position_ticket, ok, retcode, retcomment, deal_price, deal_volume,
            shadow, raw)                                  -- 每次下单/改单/平仓一行，含失败
live_trades(id, session, engine_trade_no UNIQUE(session,tno), direction, entry_time, entry_price,
            position_ticket, volume_open, volume_left, be_done, half_done, engine_stop, broker_sl,
            shadow, state, exit_type/exit_time/exit_price, engine_pnl, broker_pnl, deviation,
            engine_json, updated_at)                      -- 引擎 trade 镜像+MT5 挂接（对账核心）
```

行情复用 bars 表新 symbol `EXNESS:XAUUSD`。

## 风控默认值（live_config.json）

lots=2 · max_volume_per_order=0.02 · max_total_open_volume=0.06 · max_positions=2 · day_loss_halt_pct=3%（UTC 日起始 equity 回撤停新开仓，存量管理到终局）· max_spread_entry=$0.50（仅进场门，实测后校准）· entry_session_block_srv=[["21:55","22:15"]]（rollover 占位，实测校准）· weekend=drop · deviation_pts=50 · order/modify_retry=3 · kill_file=data/LIVE_KILL（停新仓；--kill-close 全平）· shadow=true 默认 · poll_sec=10 · reconcile_min=5 · param_drift=refuse。

**多策略扩展（预留）**：每策略/品种一个进程+独立 config（参数快照复用 bt_runs 方案库机制）；magic 按实例分配（20260923+序号）持仓隔离；feed/broker/store/对账/风控底座与策略无关。边界：风控按实例计数，账户级聚合敞口用「各实例上限之和」约束。

## 稳定·可靠·安全

- **稳定**：心跳（live_state 每拍更新）+ Windows 任务计划开机自启/崩溃自动重启；重启即重放恢复（10 天 <2s）；IPC 断开指数退避重连、重连先 reconcile；行情 staleness>300s 告警+停新开仓。
- **可靠五层**：①引擎确定性重放 ②风控熔断（只挡镜像侧）③每笔仓单必带券商硬 SL ④每 5 分钟 reconcile（孤儿仓 close_all、缺仓 shadow、宕机期 SL 打掉用 deals_since adopt）⑤告警即时外推。
- **安全**：密码只存终端凭据，永不进仓库/代码/日志；实盘真实配置走 `live_config.local.json`（.gitignore）；**实盘开闸双条件** `require_mode:"real"` + `data/LIVE_ARMED` 文件（内容=确认日期）缺一拒启；账号四查守卫；magic 只认自己的仓；webapp 页签只读绑 127.0.0.1；远程告警（Telegram Bot 等，M5 启用，仅通知不承载指令）。
- **服务器部署（M7）**：不拆库（算法同源，改参即生效；轻量靠进程隔离——live_trader 不含 webapp/CDP/前端）；Windows Server VPS（2C/4G，伦敦低延迟）；Linux 后路=mt5_broker 窄接口换 REST 实现；上线顺序 VPS shadow 1 周→模拟盘 1 周→实盘小手数。

## 阶段与验收锚点

| 阶段 | 验收锚点 |
|---|---|
| M0 SPEC+配置模板 | 本规范+SPEC.md 章节+live_config 模板+.gitignore |
| M1 环境+probe | `data/mt5_probe.json`：demo/hedging/spec/周末有无 M1/H4-D1 边界 vs NY17:00（**✓ 2026-09-24**：demo/hedging 通过、volume_min=0.01、filling=FOK+IOC、周六 0 根 M1；注意 terminal.trade_allowed=false——实单前须开终端 AutoTrading） |
| M2 数据层+对拍 | 15m 共同时段时间戳匹配≥99%（**实测 100% ✓ 2026-09-24**）、OHLC 中位差≤0.25/p95≤0.50（实测 0.14/0.43 ✓，锚点已按实测修订）；重采样 240/D 匹配 100% ✓；DST 规则判定 ✓（pick=utc）；点差分布表 ✓（中位 0.24~0.26） |
| M3 执行层 | 失败注入矩阵（10030 旋转/10016 clamp/部分成交/SL 盘中触发）全绿；demo 只读烟测 |
| M4 编排+shadow | **restart 不变性**（任意拍 kill→重放→trades 逐字段一致）；`--audit` 事件:动作=1:1；kill/参数漂移演练 |
| M5 模拟盘实单 | ≥1 笔完整生命周期；live_orders 与终端历史逐笔一致；演练（kill/断网/终端重开/跨周末）reconcile 收敛；告警送达 |
| M6 页签+文档 | webapp 只读页签零写操作；切实盘 checklist（LIVE_ARMED 双条件→实盘 shadow 1 周→复核→开闸） |
| M7 服务器部署 | VPS shadow 与本地对照 1 周→模拟盘 1 周→实盘小手数 |

## 测试组织

`import MetaTrader5` 只在 mt5_feed/mt5_broker 顶部；业务逻辑全纯函数/接口方法，MockBroker/ReplayFeed 注入，CI 无需终端。L1 纯函数单测（时区/重采样/门控/对账，**直接用 bars.db 真实 OANDA 序列作期望值**）；L2 MockBroker 失败矩阵；L3 集成（ReplayFeed 已知交易窗口；restart 不变性为核心不变量）；L4 真机（probe/烟测/shadow/实单）。

## 用户侧前置（M1）

Exness 开 Standard 模拟户（MT5/USD/对冲）→ 装 MT5 终端登录、XAUUSD 进 Market Watch（注意 XAUUSDm 等后缀变体）→ Max bars in history/chart 改 Unlimited → 保存密码自动登录 → Windows 禁睡眠。切实盘另开户+KYC+先入最小额。
