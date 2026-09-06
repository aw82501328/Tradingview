# py_chain Python 化回测链路 SPEC（进展记录）

> 文档用途：记录 `py_chain/` 包的移植进展、当前卡点与下一步任务，供暂停后续跑。
> 对应计划：`C:\Users\Administrator\.cursor\plans\python_回测链路_7a661d98.plan.md`（只读，不要编辑）。
> 中文注释、UTF-8 编码（用户规则）。

## 1. 目标与边界

- 把「画笔 → 标记买卖点 → 支阻位 → 交易计划 → 进出场」整条链路移植为纯 Python 包 `py_chain/`，不改动现有 `vnpy/` 与 `.cursor/skills/` 下 JS 实现（规则：不修改已完成功能）。
- 数据源：TradingView 桌面端 CDP（用户已确认）；回测：全链路逐根 K 线点状重放（用户已确认），用 `mark-entry` 的 6 种进场策略。
- **成交口径（`fill_mode`，默认 `anchor`，用户选定）**：信号在「背驰锚点时间之后的第 1 根
  fine 周期K线」开盘成交——消除背驰端点确认+检测周期条件确认的两层延迟（曾出现锚点
  19:46:30 → 确认成交 20:30:30 的 44 分钟差）。**警示**：锚点在其当拍不可知（需后续反向笔
  确认），该口径含未来函数、回测结果偏理想化，实盘/实时监控（step_to）不可复现该成交价
  （监控维持确认成交）。`fill_mode="confirm"` 为原无未来函数口径（收集拍下一根开盘）。
  **旧锚点保护**：anchor 模式下锚点距收集时刻 > 检测周期 1 根 bar 长度（「校验失败回退
  次新」选中的过时旧点）→ 该笔回落 confirm 口径，成交表「口径」列显示「确认(旧锚)」。
- **信号模式（`signal_mode`，默认 `realtime`，2026-09-05 新增）**：
  - `"realtime"`（当下背驰）：每根 fine（3分钟）收盘评估——低级别**形成中段**（引擎增量
    状态的最后一笔，已由 `extendLastBiFrom` 延伸到当下极值）创新低/新高 + 当拍 MACD
    对比弱于参照笔（`isBiDiverge`：绿柱面积/DIF低点/柱高，OR）即出信号，**不等反向笔确认**
    （用户原则：交易是基于当下的预测，背驰判断本身不需要确认）。三条件分层：① 够笔在
    **检测周期**（形成中回调段方向匹配 + 段长 ≥5 根K线，`REALTIME_MIN_BARS`，否则单笔回调
    场景永不触发）；② 当下背驰在**次级别/次次级别**（`realtimeLowerDiverge`，参照笔=向前
    最近同向已完成笔，保留 50% span 过滤）；③ 支阻位附近（形成中段当前极值价）。策略专属
    条件与确认制共用（`strategyExtraOk`）。去重按 `(periodX, strategyKey, markRes, 段起点)`
    ——每个形成段只发一次，段延伸不重发。①③所需的交易计划/支阻位仍在笔结构变化时重算
    并缓存（控性能：全区间 11475 根实时评估无耗时回退）。当下信号一律 **confirm 口径成交**
    （收集拍=信号拍，无未来函数，anchor 对其无意义）。
  - `"confirm"`（确认制）：仅在笔结构变化时收集，回头找已完成低级别背驰笔（原行为）。
    实测含**混合时点评估**信号：旧背驰点（如 8-13 06:27）+ 当下条件（08:15 MACD 已收回
    0 轴）组合入场——当下制按一致性原则跳过这类信号。
- **已收盘 bar 切片（2026-09-05 修复，两种模式共同生效）**：`_advance_cut` 只让
  「收盘时刻 ≤ 决策时刻」的 bar 进入切片（`bar.time + intervalSec(res) <= t`）。旧规则
  `bar.time <= t` 让 15m/60m/240/D 的 bar 在开盘时刻即以完整 OHLC 进入，对决策构成
  未来窥视（60m 最多提前 57 分钟——8-21 16:00 的中枢曾因 16:00-17:00 bar 的未来高点
  4585.13 提前成立、信号延后触发）。`run()` 决策时刻 = 本根收盘瞬间（`fine[i+1].time`），
  成交用下一根开盘价（同时刻）。副效应：结构重建次数减少，全区间回测 343s → 46s。
- **buildZSByUpper 最后一段开放段（2026-09-05）**：最后一个上级笔的 endTime 视为 +∞
  （`open_last=True` 默认）——形成中的下级笔归属于形成中的上级笔，使 zsExitWeak
  （出中枢力度变弱）在够笔当下即可评估（曾因上级形成笔终点只随上级 bar 收盘延伸，
  下级最新笔被切段规则丢弃，导致 8-21 15:48 信号延后 15 分钟触发）。
- **fine_res 覆盖感知选择（2026-09-05 修复）**：回测时间轴 = 按周期间隔从细到粗第一个
  「数据跨度 ≥ 全部周期最大跨度 × 0.9」的周期。30S 历史深度有限（实测仅约 6 小时），
  开启 `--with-30s` 后若仍以 30S 为时间轴，整个回测坍缩到 30S 数据窗口（曾致 from=8-20
  的回测信号归零）；修复后时间轴仍为 3 分钟，30S 只在自身覆盖内作低级别背驰候选
  （评估/成交粒度 = 3 分钟收盘；实测仅增不减：24 → 26 信号，新增 2 个 30S 级别）。
  若回测区间就在 30S 覆盖内（如只测最近几小时），30S 仍会成为时间轴保持细粒度。
- **30S 取数滚动扩充（2026-09-05，用户确定的简单策略）**：15m 及以上全量、3m 保持全量
  （~2 万根 ≈15s 非瓶颈，且为回测时间轴）、**30S 滚动加载近一周**——秒级分支原有界滚动
  （最多 3 轮 scrollToFirstBar × scroll_wait，覆盖 7 天或首根不再推进即停）。实测 TV 30S
  数据源深度 ≥10 天、图表缓冲可装 2.2 万根，保留最近 7 天 ≈ 13,751 根（此前默认缓冲仅
  821 根 ≈ 6.8 小时，致 30S 背驰级别几乎无效）。深度不足一周时日志警告。分钟级全量补滚
  加固：单次滚动后首根仍晚于 from_ts（TV 分批推进停在半路，实测短 1~4 天；3m 受图表
  缓冲 ~2.2 万根上限约束）时再滚 1~2 轮。实测 from=7-2 开 30S：110 信号 = 3×93 + 15×10
  + 60×1 + **30S×6**（30S 只增不减，成交延迟 1~2 分钟）。
- **修复后全区间双模式实测（8-3~9-5，11475 根，fill_mode=confirm）**：确认制 64 信号
  65% 胜率、延迟 avg41.5/max8.8h；当下制 24 信号 66% 胜率、延迟 **avg7.9/max0.4h**；
  24 个共同信号，确认制多出的 40 个为混合时点信号；两模式各 46s。
- 回测完成后只回画**实际成交的进场箭头**：做多=红色向上箭头、做空=绿色向下箭头；暂不实现出场策略，故无出场箭头。
- 依赖：`websocket-client`（CDP 通信）；其余为 Python 标准库。**尚未执行 pip 安装验证**。

## 2. 架构与数据流

```
CDP 取数(data_loader) → chan_core(mergeBars/buildBi/buildZS/MACD/背驰)
  → mark_buy_sell(买卖点) → sr_flip(支阻位) → trading_plan(交易计划)
  → mark_entry(进出场) → backtest(点状重放+持仓模拟) → tv_draw(CDP 回画进场箭头)
```

## 2.1 出场规则与同向持仓互斥（2026-09-05 新增，与 mark_entry.js 对齐）

引擎（`BacktestEngine.run`，全量回测模式）在成交后对每个持仓按时间顺序增量推进出场；
`step_to`（回放/实时）只收集信号不成交，无出场状态机。规则：

| 事件 | 触发条件 | 动作 |
|------|----------|------|
| 止损 `stopSr` | fine K线**盘中**破坏止损参考位（`stop_ref_of` 方向感知选位：short 上方最近 / long 下方最近支阻位；跳空按开盘价成交） | 全平终局 |
| 保本 `breakeven` | 背驰周期（markRes）**够笔**：进场后首笔有利方向笔（short→down/long→up）完成（`find_bi_event`） | 止损位上移至**进场价**（实际成交价） |
| 平一半 `half` | 检测周期（periodX）**够笔**（需保本已触发） | 平一半（0.5 手） |
| 全平 `close` | 检测周期**破前底/过前高**：进场后开始的不利方向笔端点破前一同向笔端点 | 全平终局 |
| 保本止损 `stopBe` | 保本后盘中触及进场价 | 全平终局 |

- **同向持仓互斥**：同方向持仓未终局时新信号不成交（`stats["suppressed"]` + `on_suppressed` 回调，Web 行状态=同向过滤）；**多空互不影响**（各方向独立状态机）；同时刻同向共振按检测周期从大到小取一条。
- **盈亏口径**：已平仓 = 0.5×TP2 价 + 0.5×终局价（未到 TP2 则全量终局价）；未平仓 mark-to-market。`summarize` 拆「已平仓盈亏/未平仓浮盈」+ 出场类型统计。
- **回测新增回调**：`on_exit(tr)`（持仓终局）、`on_suppressed(s)`（互斥过滤）。
- **与 JS 的口径差**：JS（mark_entry.js `simulatePosition`）用最终笔快照全 hindsight 模拟、保本价=信号价（背驰点价）；引擎用当前快照（笔确认有时延、无未来窥视）、保本价=实际成交价——同一信号出场时序在笔重构处可能略有差异，属研究口径差。
- **Web 控制台**：信号行状态流转 `信号→持仓中→已平仓`（或 `同向过滤`），新增出场时间/出场价/出场类型/盈亏列；「标记进出场」按钮画箭头 + 灰色出场标记（`marks.py`：终局 xcross / 平一半 circle，默认 `#787B86`）。

## 3. 文件清单与状态（截至 2026-09-01）

| 文件 | 状态 | 说明 |
|------|------|------|
| `py_chain/chan_core.py` | ✅ 已完成 | 以 `vnpy/chan_core.py` 为基线补齐 `fixBiExtremes`/`buildZS`/`buildZSByUpper`；另**新增增量原语**：`_mergeStep`、`fractalAt`、`updateFractalsTail`、`MacdAccumulator`、`AtrAccumulator`（供增量回测）。 |
| `py_chain/mark_buy_sell.py` | ✅ 已完成 | 移植 JS `mark_buy_sell.js`；`compute_all_marks` 支持可选 `periodMacd`/`periodAtr` 预计算参数。 |
| `py_chain/sr_flip.py` | ✅ 已完成 | 移植 JS `mark_sr_flip.js`；`compute_srflip` 支持可选 `periodAtrsIn`。 |
| `py_chain/trading_plan.py` | ✅ 已完成 | 移植 JS `trading_plan.js`（含复制自 chan-status 的 `isRangeBound`）；`compute_plan` 支持 `periodMacd`/`periodAtr`。 |
| `py_chain/mark_entry.py` | ✅ 已完成 | 移植 JS `mark_entry.js`（6 种策略 + `findDivergePoints`/`evaluateEntry`）；`compute_entries` 支持 `periodMacd`/`periodAtr`；**出场纯函数** `stop_ref_of`（方向感知止损参考位）/`find_bi_event`（够笔/破高低点事件源，与 JS `stopRefOf`/`findBiEvent` 对齐）。 |
| `py_chain/data_loader.py` | ✅ 已完成 | `CDPClient`（HTTP 找 target + websocket `Runtime.evaluate`）、`fetch_bars`/`load_bars`/`load_cached`、`align_periods`；与 `load_all_tf.js` 对齐。 |
| `py_chain/backtest.py` | ✅ 已完成 | 增量点状回测引擎 `BacktestEngine` + `run_backtest` + `build_bis` + `summarize`；`_append_bars` 用 `extendLastBiFrom` 增量延伸 + 笔结构变化才重算链路（短路）；**出场状态机**（`_advance_exit`/`_close_pos`：止损+三档止盈+同向互斥，见 §2.1）。 |
| `py_chain/tv_draw.py` | ✅ 已完成 | `draw_trades` 通过 CDP `createShape` 回画进场箭头（多红空绿、文本 `BT·BUY/SELL + 价`），画前清除旧 `BT·` 标记。 |
| `py_chain/main.py` | ✅ 已完成 | CLI：`--symbol/--periods/--from/--port/--use-cache/--warmup/--no-draw/--no-marks`；串起取数→全链路→回测→回画→统计。 |
| `py_chain/__init__.py` | ✅ 已完成 | 包初始化。 |

## 4. 已验证情况（截至 2026-09-01）

- `python -c "import py_chain.*"` 全部导入成功。
- 合成数据冒烟：`build_bis` 正常产出各周期笔；`marks`/`srflip`/`plan` 全链路跑通；`run_backtest` 跑通（合成数据无进场信号属正常）。
- `evaluateEntry` 成功路径单测通过（构造 3m 底背驰 + 15m 过前高回调 + 支阻位附近 → `ok:true`，markRes=3，颜色=红 `#F23645`；空头对称路径也通过，绿 `#089981`）。
- 真实缓存 `bars_all_tf.json`（3m 16265 根）上：`build_bis` 0.41s、`srflip` 0.03s、`plan` 0.03s、`entries` 0.04s——全量单次链路很快。
- **增量笔 == 全量笔校验通过**：截断到 3m 前 3000 根，逐根推进每 500 根采样，78 个 (时刻×周期) 样本 `norm(bis)` 全等（临时脚本 `%TEMP%\py_chain_incr_check.py`）。
- **Python 与 JS 输出一致性核对通过**（同一份 `bars_all_tf.json`，node 直接加载 `.cursor/skills/chan-core/scripts/chan_core.js` 离线跑）：
  - 笔构建（mergeBars/findFractals/buildBi/fixBiExtremes/extendLastBi）：D/240/60/15/3 五周期笔数与逐笔完全一致；
  - 买卖点（findBuyPoints/findSellPoints，含上级笔区间套）：各周期 1/2/3 类买卖点完全一致。
  - 期间修复两处移植偏差：① `buildBi` 间隔不足回溯条件由 `gapPrevLast<=12 or gapPrevK>=4` 改为与 JS 一致的 `!prevLastValidBi`（prev→last 构成有效笔则前顶/前底作废保护）；② `isBiDiverge`/`biMacdMetrics` 补齐 JS 的第三个条件「绿柱/红柱最大高度变小（greenMax/redMax）」。
- 小窗口完整 CLI 流程（3m 前 3000 根，--no-marks）：全链路 0.1s、回测 2940 步约 23s、37 信号/37 成交；`--with-marks` 时买卖点正常输出（60周期3个/15周期7个/3周期9个），信号统计与不开 marks 一致。
- **出场状态机真实数据验证（2026-09-05）**：`bars_all_tf.json`（3m 5469 根，当下背驰模式）46s 跑完——24 信号、20 成交、4 同向过滤、19 已平仓（保本止损 9 + 支阻位止损 10）+ 1 仍持仓、平一半 3 次；已平仓盈亏 10.69 / 未平仓浮盈 53.61；8-31 多单（XAUUSD 顶背驰空）出场链路与 JS 手工模拟一致（3m 够笔保本 → 回抽进场价保本止损出局，pnl=0）。
- **批量标记显示异常修复（2026-09-05）**：`_ensure_hist_loaded` 旧实现 15s deadline 远不够 TV 分批加载深历史（3m 数月深度需 1-3 分钟）→ 加载不到位时 `createShape` 拿到数据范围外的时间 → **shape 锚点丢失（getPoints 空、图上不可见/错位、永久损坏）**。修复：对齐 mark-entry `ensureBarsCover` 策略（长轮询 + 每 15 次重触发 scrollToFirstBar + 连续 30 次无进展兜底），加载失败**跳过该组不画坏的**；批量 `createShape` 偶发 `Value is undefined` → 拆单重试一轮；收尾自动清理「ML· 前缀但锚点为空」的半成品孤儿。验证：27 信号全画出、各自 markRes 周期下锚点全部正常（注意：shape 的 getPoints 只在其可见周期下非空，跨周期检查会得到假阴性空锚点）。
- `websocket-client` 1.9.0 已安装并可导入（`pip install websocket-client`）。
- **TradingView 桌面端 CDP 实时联调通过（2026-09-01）**：
  - `open-tradingview` 脚本以 `--remote-debugging-port=9222` 拉起桌面端；
  - 修复 WS 握手 403：`CDPClient.connect` 加 `suppress_origin=True`（Chromium 新版拒绝非白名单 origin）；
  - `data_loader` 真实取数成功：XAUUSD 5 周期（D 22/240 129/60 491/15 1961/3 9781 根），首尾时间价格正确；
  - 全量回测跑通：9721 步、59 信号/59 成交、约 8 分钟（联调用 CDP 取数缓存，未追求速度）；
  - `tv_draw` 回画 59 个进场箭头全部成功（失败 0）；
  - **修复清除旧标记**：新版桌面端 `mainSeries().entities()` 已移除（返回 -2）；改为 `createShape` 记录 id 到 localStorage（`bt_arrow_ids`）→ `chartModel().dataSourceForId(id)` + `removeSource(ds)` 按 id 精准删除（已验证 清除→画→清除 循环干净，不影响用户图形）。

## 5. 性能卡点（已于 2026-09-01 解决）

原卡点：增量引擎 `_append_bars` 每根K线调用全量 `extendLastBi(bisArr, bars)`（O(n) 从头扫描 bars 找 `startIdx`），16k 根 3m K线回测变 O(n²)，CLI 卡死（5 分钟未完成）。

**已解决方式**（与方案一致并加强）：
1. `py_chain/chan_core.py` 新增增量入口 `extendLastBiFrom(bisArr, bars, startIdx, endIdx)`：只扫描 `bars[startIdx:endIdx]`，不再从头遍历；`extendLastBi` 保留原逻辑，内部改用二分定位起点后复用。
2. `py_chain/backtest.py` `_append_bars` 用 `bisect.bisect_left(self._times[res], last.startTime)` 定位最后笔起点（O(log n)），传 `endIdx=self._cut[res]` 避免整段切片复制；并记录延伸端点变化作为「笔结构变化」信号。
3. 链路短路：`_advance_cut` 返回是否有周期笔结构变化（新分型或延伸推进），`run()` 仅在变化时重算链路（支阻位→计划→进出场），未变化时仅推进 bars 不产信号（与「信号在新笔端点确认时出现」语义一致）。
4. 实测：3000 根小窗口回测约 22-24s（优化前 47.8s）；全量 16265 根未跑完（按用户要求先验证小部分）。

剩余注意：全量 16265 根预计仍需数分钟（链路重算频率较高），若后续需要进一步提速，可对链路函数内部做增量（当前仅跳过未变化步骤）。

## 6. 下一步任务清单（待办）

- [x] **P0 修复增量延伸性能**：`extendLastBiFrom` 增量入口 + bisect 定位 + 链路短路（已解决，见上）。
- [x] P0 用校验脚本确认增量笔 == 全量笔（`py_chain_incr_check.py`，3000 根窗口全等）。
- [ ] P0 全量数据 CLI 回测跑通并记录耗时（用户要求先跑小部分；联调时全量 9781 根已跑通约 8 分钟，正式全量待后续）。
- [x] P1 `pip install websocket-client`（1.9.0 已装）。
- [x] P1 真实 TradingView 桌面端联调 `data_loader` 取数与 `tv_draw` 回画（取数成功、回画 59 箭头成功、清除逻辑修复）。
- [x] P1 用当前缓存数据核对 Python 与 JS 输出一致性（笔构建、买卖点完全一致；计划/进场信号为 JS 图表脚本，无法离线核对，已通过回测输出与全链路验证）。
- [x] P2 `--with-marks` 选项验证买卖点阶段（小窗口输出正常，性能略增：3000 根约 36s vs 无 marks 23s）。

## 7. 运行方式（下次续跑）

```bash
cd E:\AI_Projects\TRADINGVIEW
python -m py_chain.main --use-cache --no-draw --no-marks --from 2026-07-02 --warmup 60
python -m py_chain.main --symbol OANDA:XAUUSD --periods D,240,60,15,3 --from 2026-07-02
```

## 8. 注意事项

- 全部注释中文、UTF-8 编码。
- 修改某个函数前先理解原 JS/旧实现逻辑，在原来基础上增量修改（保留原逻辑）。
- 不写专门测试脚本（用户规则）；临时校验脚本放系统 TEMP，不属于项目文件。
- 检测周期与 JS 一致：`mark_entry` 用 `240,60,15,3`（不含 D）；D 仅作 240 的上一级别笔。
- **30 秒级别（`--with-30s`，可选，JS/Python 双端同名开关）**：periods 追加 `30S`，使 3 分钟状态的
  进场「以下级别背驰」可落到 30S（箭头画在 30S 级别）。数据特性：TV 30s 历史深度有限（数周），
  `data_loader.fetch_bars` 对秒级周期不滚全量历史、只保留最近 3 天；回放/实时监控的最细周期
  （tick 驱动）随 periods 自动变为 30S（步进 30s，刷新频率 ×6，抖动大时可在 webapp 加节流）。
  Web 控制台各模式卡片有「30秒级别」下拉开关。关闭后不传 30S 即回到现状。
- **以下级别背驰的候选回退（JS/Python 双端一致）**：`lowerDiverge` 收集所有更低周期的同向
  背驰点并按时间降序返回候选列表；`evaluateEntry` 依次做「支阻位附近」校验，失败回退次新点。
  背景：30S 背驰点高频出现会按「时间最新」抢占 3/15 分钟点，而 30S 微观极值价常远离支阻位，
  无回退时信号彻底消失（旧点被抢占丢弃、新点校验被拒，两边都不出箭头）。
- 交易计划取最近 60 笔；震荡判定 A（isRangeBound）与 B（未离开中枢且当前价在 ZD~ZG 内）优先于趋势策略。
