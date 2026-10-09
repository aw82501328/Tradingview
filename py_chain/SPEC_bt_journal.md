# SPEC：全量回测交易日志（bt_journal，2026-09-30 已实现）

> 状态：**已实现**（取代 SPEC_signals_explain.md 的方案草案——该草案只覆盖"为什么开单/
> 为什么止损"，本实现补齐"为什么某时间没开单"，且行号/出场口径已按 2026-09 重构后重写）。

## 目的

用户在全量回测后常问三类问题：**为什么这笔交易会开单 / 为什么在这里止损 / 为什么
某某时间没开单**。此前原因从不落盘，每次回答都要全量重放复现（分钟级）。现在回测
运行时把决策上下文顺手写进 NDJSON 日志，事后用 `bt_query` 直接读文件回答，零重算、
零 CDP、<0.2s。

## 架构

- **`py_chain/bt_journal.py`**（纯标准库）：`BtJournal` 写入器 + 中文标签单一来源
  （DIR/EXIT/FILL_MODE/GATE_LABELS）+ 保留策略（`cleanup_old`）+ `latest.json` 指针。
- **`py_chain/bt_query.py`**：查询 CLI（读日志渲染中文叙事）。
- 引擎接线：`BacktestEngine.run(journal=...)`（缠论V1）与 `FxMaEngine.run(journal=...)`
  （强分型均线V1）——Web 回测卡 / CLI / bt_batch 多品种子进程三入口全覆盖；
  `step_to` 实时路径不写日志（`self._journal` 仅 run() 期间挂载）。
- 零开销口径（性能实测见下）：**事件级**（信号/成交/出场/被滤稀有）+ **状态变化级**
  （前后快照比较，`volatile` 键不参与判定）+ **拒绝去重**（(周期,策略,段起点,闸门)
  首次 + ctx 变化才写；`volatile` 键如均线差值不参与判定）。写失败静默，永不阻断回测。

## 行格式（一行一 JSON，`ev` 区分）

| ev | 时机 | 关键字段 |
|---|---|---|
| header | run 开始 | cfg 快照（周期/口径/滑点/lots/策略参数） |
| state | 周期状态变化 | t, period, 计划方向/策略/原因, 趋势参考, 形成段快照 |
| reject | 闸门拒绝（去重） | t, period, gate, segStart, strategyKey, ctx(原始数字) |
| signal | 信号产生点快照 | id, signalNote(中文), nearSr, segStart, flags |
| fill | 下一开盘成交 | tradeNo, journalId, entryWhy(止损位推导叙事), stopSource |
| exit | 出场事件 | tradeNo, type, why(触发细节+成交价) |
| trade_end | 终局/期末 | exitType, pnl, exitWhy；state=open 为 mark-to-market |
| suppressed | 同向互斥被滤 | why(被哪笔持仓挡住) |
| footer | run 结束 | stats, wall, rows |

闸门代码→中文见 `bt_journal.GATE_LABELS`（15 个进出场闸门 + 背驰下沉内部原因细分 +
fxma 专属闸门；`fx_prov_invalidated`=pred2 预判点消失——收复前低/前高、结构重算或真点
接管，ctx 含 provTime/provPrice/dSegStart，2026-10-09）。**拒绝行存结构化数字，中文渲染
统一在 bt_query**（写侧零格式化开销）。fxma state 行的买卖点类型带「预判」前缀
（如「预判2卖」）= pred2 提前入列的未确认点承担尾点（判定/去重仍用原标签）。

## 文件与保留

- `data/journal/bt_<strategy>_<symbol>_<yyyymmdd_HHMMSSmmm>.ndjson`（.gitignore 已忽略）
- `data/journal/latest.json`：`{strategy|symbol → path}` 指针，供查询定位
- **自动清理**（每次新建日志时）：删除 >30 天的旧日志；同 (strategy, symbol) 组内
  无论如何保留最近 10 个。bt_runs SQLite 摘要不受影响（永久）。
- bt_runs 保存的方案快照随带 `cfg["journal"]` 路径（webapp 回填）。

## bt_query 用法

```
python -m py_chain.bt_query --list                       # 交易总表+统计（缺省动作）
python -m py_chain.bt_query --trade 3                    # 第3笔：信号→进场→逐出场→终局
python -m py_chain.bt_query --signal 5                   # 信号id=5（含被滤/未成交）
python -m py_chain.bt_query --time "9-20 02:00"          # 该时刻前后事件时间线
python -m py_chain.bt_query --why-not "9-20 02:00" --period 60   # 为何没开单
python -m py_chain.bt_query --symbol OANDA:XAUUSD --list # 按品种定位最新日志
python -m py_chain.bt_query --json ...                   # 机器可读
```

`--why-not` 回答路径：①前后24h是否其实有信号/被同向过滤（常见误会）；②各周期当时
状态行（计划观望/趋势过滤/形成段）；③该周期 t 前最后出现的各闸门拒绝原因（含数字：
距支阻位 X>near Y、MACD 不背驰、段长不足等）。

## 关键实现点（防坑）

- **signalNote 必须在信号产生点快照**（evaluateRealtimeEntries/compute_entries 内），
  不能到成交拍再读 `self._plan`——缓存计划可能已被后续结构重算，与信号判定时不一致。
- 出场**决策与成交隔一拍**：`pos["pendingWhy"]` 跨拍携带（pendingExit 守卫保证不覆盖），
  execute_pending_exit 拼"下一开盘 … 成交"后落事件；breakeven 当拍即落。
- `stopSource` 随止损位演变更新：进场时（近支阻沿用/正确侧重选/最大止损兜底）→
  进场K线极值外推 → 保本位 beStop（stopBe 时）。
- fx_ma 均线差值等每拍变化的数值用 `volatile=()` 排除去重判定，防退化成逐 bar 写。
- 信号→成交关联：`jr.signal()` 回填 `sig["_jid"]`，成交行经 `journalId` 引用；
  同一信号 dict 对象流经 pending→fill，天然对齐。
- `journal=False` 完全关闭（零对象创建）；`None` 默认开；实例注入供测试。

## 验证（2026-09-30 实测）

1. `test_bt_journal`：12 用例（去重/状态变化/出场叙事/查询折叠/坏行容忍/保留策略）。
2. 既有套件回归：test_exit_rules + test_fx_ma（73）+ test_start_ts/range_gate/bt_runs
   （32）+ test_engine_lock_parity（2）全绿。
3. 无漂移：XAUUSD 同配置双跑 journal 关/开（两次不同窗口），stats/trades/signals
   指纹全等（12信号/8成交/4被滤/8平仓 与 2/2/0/2 两轮）。
4. 性能：成对交替基准（off/on ×6 对，中位）**开销 +0.9%**（cProfile 实测日志代码
   占总耗时 ~1.5%，状态行采样每 3 拍后更低）。注意：本机偶发负载尖峰会使单对计时
   偏差达 ±20~45%，判定开销须用成对中位，不可信单对数字。
