# py_chain Python 化回测链路 SPEC（进展记录）

> 文档用途：记录 `py_chain/` 包的移植进展、当前卡点与下一步任务，供暂停后续跑。
> 对应计划：`C:\Users\Administrator\.cursor\plans\python_回测链路_7a661d98.plan.md`（只读，不要编辑）。
> 中文注释、UTF-8 编码（用户规则）。

## 1. 目标与边界

- 把「画笔 → 标记买卖点 → 支阻位 → 交易计划 → 进出场」整条链路移植为纯 Python 包 `py_chain/`，不改动现有 `vnpy/` 与 `.cursor/skills/` 下 JS 实现（规则：不修改已完成功能）。
- 数据源：TradingView 桌面端 CDP（用户已确认）；回测：全链路逐根 K 线点状重放（用户已确认），用 `mark-entry` 的 6 种进场策略。
- 回测完成后只回画**实际成交的进场箭头**：做多=红色向上箭头、做空=绿色向下箭头；暂不实现出场策略，故无出场箭头。
- 依赖：`websocket-client`（CDP 通信）；其余为 Python 标准库。**尚未执行 pip 安装验证**。

## 2. 架构与数据流

```
CDP 取数(data_loader) → chan_core(mergeBars/buildBi/buildZS/MACD/背驰)
  → mark_buy_sell(买卖点) → sr_flip(支阻位) → trading_plan(交易计划)
  → mark_entry(进出场) → backtest(点状重放+持仓模拟) → tv_draw(CDP 回画进场箭头)
```

## 3. 文件清单与状态（截至 2026-09-01）

| 文件 | 状态 | 说明 |
|------|------|------|
| `py_chain/chan_core.py` | ✅ 已完成 | 以 `vnpy/chan_core.py` 为基线补齐 `fixBiExtremes`/`buildZS`/`buildZSByUpper`；另**新增增量原语**：`_mergeStep`、`fractalAt`、`updateFractalsTail`、`MacdAccumulator`、`AtrAccumulator`（供增量回测）。 |
| `py_chain/mark_buy_sell.py` | ✅ 已完成 | 移植 JS `mark_buy_sell.js`；`compute_all_marks` 支持可选 `periodMacd`/`periodAtr` 预计算参数。 |
| `py_chain/sr_flip.py` | ✅ 已完成 | 移植 JS `mark_sr_flip.js`；`compute_srflip` 支持可选 `periodAtrsIn`。 |
| `py_chain/trading_plan.py` | ✅ 已完成 | 移植 JS `trading_plan.js`（含复制自 chan-status 的 `isRangeBound`）；`compute_plan` 支持 `periodMacd`/`periodAtr`。 |
| `py_chain/mark_entry.py` | ✅ 已完成 | 移植 JS `mark_entry.js`（6 种策略 + `findDivergePoints`/`evaluateEntry`）；`compute_entries` 支持 `periodMacd`/`periodAtr`。 |
| `py_chain/data_loader.py` | ✅ 已完成 | `CDPClient`（HTTP 找 target + websocket `Runtime.evaluate`）、`fetch_bars`/`load_bars`/`load_cached`、`align_periods`；与 `load_all_tf.js` 对齐。 |
| `py_chain/backtest.py` | ✅ 已完成 | 增量点状回测引擎 `BacktestEngine` + `run_backtest` + `build_bis` + `summarize`；`_append_bars` 用 `extendLastBiFrom` 增量延伸 + 笔结构变化才重算链路（短路）。 |
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
- 交易计划取最近 60 笔；震荡判定 A（isRangeBound）与 B（未离开中枢且当前价在 ZD~ZG 内）优先于趋势策略。
