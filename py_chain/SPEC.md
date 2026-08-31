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
| `py_chain/backtest.py` | ⚠️ 基本完成，**有性能卡点** | 增量点状回测引擎 `BacktestEngine` + `run_backtest` + `build_bis` + `summarize`。 |
| `py_chain/tv_draw.py` | ✅ 已完成 | `draw_trades` 通过 CDP `createShape` 回画进场箭头（多红空绿、文本 `BT·BUY/SELL + 价`），画前清除旧 `BT·` 标记。 |
| `py_chain/main.py` | ✅ 已完成 | CLI：`--symbol/--periods/--from/--port/--use-cache/--warmup/--no-draw/--no-marks`；串起取数→全链路→回测→回画→统计。 |
| `py_chain/__init__.py` | ✅ 已完成 | 包初始化。 |

## 4. 已验证情况（合成数据）

- `python -c "import py_chain.*"` 全部导入成功。
- 合成数据冒烟：`build_bis` 正常产出各周期笔；`marks`/`srflip`/`plan` 全链路跑通；`run_backtest` 跑通（合成数据无进场信号属正常）。
- `evaluateEntry` 成功路径单测通过（构造 3m 底背驰 + 15m 过前高回调 + 支阻位附近 → `ok:true`，markRes=3，颜色=红 `#F23645`；空头对称路径也通过，绿 `#089981`）。
- 真实缓存 `bars_all_tf.json`（3m 16265 根）上：`build_bis` 0.41s、`srflip` 0.03s、`plan` 0.03s、`entries` 0.04s——全量单次链路很快。

## 5. 当前卡点（必须解决，暂停在此）

**增量引擎 `_append_bars` 中每根K线调用全量 `extendLastBi(bisArr, bars)`（O(n) 从头扫描 bars 找 `startIdx`），16k 根 3m K线回测变成 O(n²)，导致 CLI 卡死（5 分钟未完成）。**

相关位置：
- `py_chain/backtest.py` `_append_bars`（约 L98）：`self._bis[res] = extendLastBi(self._bis[res], sl)` 每步全量扫描。
- `py_chain/chan_core.py` `extendLastBi`（约 L830）：内部 `for i, k in enumerate(bars)` 从头找 `startTime`。

**下一步方案（下次续跑第一步）**：
1. 给 `extendLastBi` 增加 O(1) 增量入口：`extendLastBiFrom(bisArr, bars, startIdx)`——从已记录的 `last.startIdx`（或记录 `lastStartIdxCache`）开始只扫描新到K线；引擎缓存每周期最后笔的 `startIdx` 位置。
   - 注意 `fixBiExtremes` 会改写 `startIdx/endIdx`，增量延伸必须在 fix 之后做；只需在引擎里维护 `last_start_idx_by_res`，增量扫描窗口 = `[该周期最后笔 startIdx, 新 bar 数)`。
   - 或更简单：维护 `self._lastBiStartIdx[res]`，新到K线只扫 `tail = sl[lastStartIdx:]`，其中 `lastStartIdx` 通过二分（`bisect_left` on `_times`，时间 `last.startTime`）得到 O(log n)。
2. 修复后重新跑校验脚本确认增量笔与全量重建**完全一致**（已有脚本 `C:\Users\Administrator\AppData\Local\Temp\py_chain_incr_check.py`，验证 `norm(bis)` 全等）。
3. 然后用真实 `bars_all_tf.json` 跑 `python -m py_chain.main --use-cache --no-draw --no-marks --from 2026-07-02`，确认在可接受时间内（目标 < 2 分钟）完成。
4. 可选：若仍慢，可加「分型未变化则跳过笔重建」+「bis 未变化则跳过整个链路」的短路（当前已按分型变化重建笔，但链路每步都算；可再加 `_bis_changed` 判断只在新笔确认时算 `mark_entry`，非确认步骤仅推进 bars 不产信号——注意与「信号在新笔端点确认时出现」语义一致，需与 JS 行为核对）。

## 6. 下一步任务清单（待办）

- [ ] **P0 修复增量延伸性能**：`extendLastBi` O(1) 化（见上）。
- [ ] P0 用校验脚本确认增量笔 == 全量笔（`py_chain_incr_check.py`）。
- [ ] P0 真实数据 CLI 回测跑通（`--use-cache --no-draw`），记录耗时与信号统计。
- [ ] P1 `pip install websocket-client`，真实 TradingView 桌面端联调 `data_loader` 取数与 `tv_draw` 回画。
- [ ] P1 用当前缓存数据核对 Python 与 JS 在同样数据上的输出一致性（笔数、买卖点、计划、进场信号）。
- [ ] P2 `--with-marks` 选项验证买卖点阶段（默认关闭以提速）。

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
- 交易计划取最近 60 笔；震荡判定 A（isRangeBound）与 B（未离开中枢且当前价在 ZD~ZG 内）优先于趋势策略。
