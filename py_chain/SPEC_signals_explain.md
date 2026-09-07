# SPEC：单笔信号"进出场原因"快查优化（方案存档，暂未实现）

> 状态：**方案草案，仅存档，未执行**。实现前需按「验证」节做改动前回归基线。

## Context（为什么慢）

用户在三模式 Web 控制台（`py_chain/webapp.py`，回测/回放/实时共用）跑完回测后，在聊天里问"某一行为什么进场/出场"，回答很慢（秒~分钟级）。根因（已探明）：

1. **原因从不落盘**：`SignalLog`（webapp.py:82-211）每行只存结构化摘要（strategyKey/markRes/nearSr/fillMode/exitType/exits[]），信号细节（背驰点、计划文本、策略标签）在 `append_signal` 白名单（webapp.py:100-121）被丢弃；出场事件只有 `{type,time,price}` 枚举，无触发叙事。
2. 于是任何一次"解释某行"都退化为**全量重算**（2.4MB bars 缓存 + 5 周期画笔 + 计划 + 进出场，秒级）或 **CDP 驱动 TradingView**（分钟级）。
3. 而所有叙事所需数据在事件产生处（信号收集、成交、出场判定/成交）**全部在作用域内**——是"懒得记"而非"算不出来"。

**方案方向：在产生处顺手把中文原因记下来（零重算、纯增量键），SignalLog 每次变更镜像一条 NDJSON 到磁盘，聊天侧用新脚本读文件直接渲染。** 主链路语义零改动，前端/JS/marks.py/vnpy 均不感知。范围：**仅主方案**（不含页面"为什么"列、不含 live/replay 顺序修复）。

## 数据流事实（改前确认）

- 两条信号路径：确认制 `compute_entries`（mark_entry.py:518-607，sig 构造 596-605）与当下制 `evaluateRealtimeEntries`（mark_entry.py:406-505，out.append 494-504，多 `realtime`/`segStart`，无 `color`）。
- 两条路径作用域内都有 `plan`（每周期 `{direction, strategy, reason, pointDesc}`，trading_plan.py:321-326），`plan.reason` 已是现成中文（"找到最近买卖点 1买 @ …"）。
- `entryStrategyOf`（mark_entry.py:84-97）返回的 strategy dict 已含 `label`（"等待反弹后做2卖"等 6 条）。
- 出场：`advance_exit_decision`（backtest.py:58-99）收盘判定（breakeven 当拍落事件 84 行；half/close/stopSr/stopBe 只挂 `pendingExit`），`execute_pending_exit`（102-115）下一根开盘成交并落事件（112 行），`close_trade`（118-131）终局。**决策与成交隔一拍**——用 `pos["pendingWhy"]` 跨拍携带（`pendingExit` 守卫 73 行保证不被并发覆盖）。
- 触发细节在决策点可取：tp1/tp2/tp3 笔事件（78-80 行）、触发 bar 高低价与 stop 价（92-98 行）；`stop` 变量在 beDone 后即 entryPrice。唯缺 TP3"破前同向笔参照端点价"——给 `find_bi_event`（mark_entry.py:641-673）break_prev 命中处返回值**增量加 `refTime/refPrice`**（现有调用者只按 time/price 取值，零风险）。
- 三模式唯一汇流点 = SignalLog 的 5 个方法（append_signal/fill_trade/fill_exit/fill_suppressed/clear），全在既有 `self.lock` 内 → 镜像写在这里即全覆盖。

## 改动清单

### 1. `py_chain/labels.py`（新建，纯常量、零 import）
中文标签单一来源：`STRATEGY_LABELS`（6 键，值同 entryStrategyOf label）、`EXIT_LABELS`（stopSr=支阻位止损/stopBe=保本止损/close=全平/half=平一半/breakeven=保本）、`FILL_MODE_LABELS`（anchor=锚点当拍成交/confirm=确认成交/confirm-stale-anchor=确认·旧锚点回落）、`DIR_LABELS`。随后消除既有重复：backtest.py:856、webapp.py:303 的出场名映射改用它；mark_entry 的 entryStrategyOf 映射值引用它。

### 2. `py_chain/mark_entry.py`
- 顶部 import `fmtT`（chan_core）。
- 新增模块级 `compose_signal_note(strategy, plan, markRes, t, price, nearSr, segStart=None)`：
  文案形如 `{label}｜检测周期 {X} 计划：{plan.reason}｜背驰 {markRes} @ {fmtT(t)} {price:.2f}｜近支阻位 {nearSr:.2f}`（当下制可追加 `形成段自 {fmtT(segStart)}`）。
- 两条路径的信号 dict 各加 2 个增量键：`strategyLabel: strategy["label"]`、`signalNote`（确认制 596-605；当下制 494-504）。
- `find_bi_event`：break_prev 命中返回 dict 加 `refTime/refPrice`。

### 3. `py_chain/backtest.py`
- `advance_exit_decision`：
  - breakeven 事件（84 行）直接带 `why`：`TP1 保本：{markRes} 首笔有利{方向}笔 {fmtT(tp1.time)} @ {tp1.price:.2f} 完成，止损位上移至进场价`。
  - half/close/stop 三处置 `pos["pendingWhy"] = {"rule", "note"}`：half=`TP2 半平触发：{periodX} 有利笔 @ {tp2.time} 完成（保本已生效）`；close=`TP3 全平触发：{periodX} 不利笔 {fmtT(tp3.time)} @ {price} 破前一同向笔端点 {refPrice}`；stopSr/stopBe=`止损/保本止损触发：{fmtT(bar.time)} bar {low/high} {extreme:.2f} {跌破/升破} 止损/保本位 {stop:.2f}`。
- `execute_pending_exit`：112 行事件 append 带 `why = note + f"；下一开盘 {fmtT(exec_time)} @ {exec_price:.2f} 成交"`，随后清掉 `pos["pendingWhy"]`。
- `close_trade`：`pos["exitWhy"] = pos["exits"][-1].get("why")`。
- `_fill_pending`：trade dict（753-771）加 `strategyLabel/signalNote/entryWhy`（entryWhy 由小助手组：`做{多/空} {label}：信号 {fmtT} @ price；口径 {FILL_MODE_LABELS}，{fmtT entryTime} @ entryPrice 成交；止损参考位 {stopRef 或 "无（仅三档止盈）"}`）；suppressed 分支（727-734）在 `on_suppressed(s)` 前给 `s["suppressedWhy"] = f"同向{d}互斥：已有持仓单 #{open_pos[d]['tradeNo']}…，本信号不成交"`。
- `summarize`（856 行）出场名映射换 `labels.EXIT_LABELS`。

### 4. `py_chain/webapp.py` — SignalLog 镜像
- `__init__`：`self._mirror_path`（默认 `os.path.join(os.path.dirname(__file__), "signals.ndjson")`，可注入供测试）并**截断文件**。
- 新增 `_mirror(op, row)`：持锁内追加一行 `json.dumps({"op":op,"row":row}, ensure_ascii=False, default=str)`；异常静默（落盘是增强不阻断主流程）。
- `append_signal` 白名单加 `strategyLabel/signalNote` → `_mirror("append", row)`；`fill_trade` 行加 `entryWhy`（无行回退新建分支补 strategyLabel/signalNote）→ `_mirror("update", row)`；`fill_exit` 行加 `exitWhy` → mirror；`fill_suppressed` 行加 `suppressedWhy` → mirror；`clear()` 截断 mirror。
- `ModeWorker._on_exit`（303 行）出场名映射换 labels。
- **生命周期语义（与现状内存一致）**：webapp 重启或点"清空"→ 内存与镜像文件同时清空、id 从 1 重来；多轮回测行追加共存。

### 5. `py_chain/explain_signal.py`（新建，快问快答入口）
- 仅 import `labels` + `chan_core.fmtT`（stdlib；不 import backtest/webapp，保启动快）。
- CLI：`python -m py_chain.explain_signal [--id N | --latest | 模糊时间串如 "9-5 02:00" | --list] [--json]`。
- 读 `signals.ndjson` 折叠 `{op,row}` 按 id（后写覆盖；末行损坏容忍忽略）。按 status 渲染中文段落：
  - 表头：行号/模式/方向/周期/策略 label/背驰级别/信号时间·价/近支阻/状态；
  - `信号`/`同向过滤` → signalNote + suppressedWhy（旧数据行缺 label 时用 STRATEGY_LABELS 按 key 兜底反查）；
  - `持仓中`/`已平仓` → entryWhy，逐条出场事件 `- {fmtT} {EXIT_LABELS[e.type]} @ 价：{e.why}`，终局附 exitWhy + pnl。
- 文件缺失/过旧 → 提示"先跑 webapp 回测（表格行号即 id），或 curl http://127.0.0.1:8000/api/signals"。
- 性能目标 <0.2s（3MB 级全读折叠 <50ms，实测验证）。

### 6. `CLAUDE.md`（新建于仓库根，~15 行）
指引：用户问某行/某单为什么进出场 → 先 `python -m py_chain.explain_signal --id <行号>`（读 NDJSON 镜像，零重算零 CDP）；文件缺失提示先跑回测；**不要**为此重跑全链路或启动 CDP。

## 关键实现注意（核查结论）

- **signalNote 必须在信号产生处快照**，不要到 `_fill_pending` 再读 `self._plan`——缓存计划可能已被后续结构变化重算，与信号判定所用不一致。
- 决策点无 exec_bar（成交盘信息），成交点无触发详情——`pendingWhy` 是唯一跨拍中间态，作用域由 pendingExit 守卫保护。
- 数据末端"止损触发但无下一根 bar"→ 持仓保持 open 无事件，v1 不处理（不镜像 pendingWhy），接受。
- 30S 级别 markRes 文案分钟粒度可读性、fmtT 本地时区口径，实现时抽查观感即可。
- 新增键全为增量，JS 端 `marks.py`/`index.html`/SSE 消费者只用白名单键，无需改动。

## 验证（实现时执行）

1. **语法**：`python -m py_compile py_chain/labels.py py_chain/mark_entry.py py_chain/backtest.py py_chain/webapp.py py_chain/explain_signal.py`。
2. **回归无漂移**：改前先跑一次缓存回测（`python -m py_chain.main --use-cache --no-draw` 或 webapp use_cache）记录 stats（信号/成交/出场类型/盈亏）→ 改后同配置再跑 → 数字一致。
3. **主链路冒烟**：`python -m py_chain.webapp --port 8000` → use_cache 回测跑完 → 页面行数与折叠 NDJSON 行数一致；抽查已平仓行 exits[] 每事件带 `why`、被过滤行带 `suppressedWhy`。
4. **explain 输出**：`--latest`、`--id`（分别命中 全平/保本止损/支阻位止损/同向过滤/持仓中 各一行）核对叙事时间价与事件一致；`--json` 结构完好。
5. **性能**：循环 20 次 explain 计时 <0.2s。
6. **生命周期**：重启 webapp → ndjson 清空、id 重排；页面"清空"→ 文件截断。
