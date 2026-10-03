# SPEC · EVAL 评估（成笔用例与基线回归）

> 2026-10-01 实施。WEB 工作台「EVAL评估」页：按周期快照成笔用例、多选建立基线、
> 画笔逻辑调整后跑基线回归。页面 `py_chain/web/eval.html`（主导航 index 4，iframe
> `/eval.html?embed=1`），后端 `py_chain/eval_api.py`（`/api/eval/*`）+
> `py_chain/eval_service.py`（`EvalManager`）。

## 目标与口径

- **回归对象**：`backtest.build_bis(bars_by_period)`（全周期流水线：markWickBars →
  mergeBars → findFractals → ATR/MACD → buildBi（上级 lockedPivots / 下级 15m 上下文 /
  nearDouble）→ fixBiExtremes → extendLastBi），与回测/全链路同口径。只评估 Python
  实现；py↔JS 对拍仍走 `align_check.py`。
- **比对口径**：与 align_check 相同的 11 字段逐笔位置比较
  （type/startIdx/endIdx/startTime/endTime/startPrice/endPrice/rawCount/span/gapLocked/
  macdCross），严格相等才通过。展示上分「结构差异」与「标记差异」
  （rawCount/span/gapLocked/macdCross）两组；双侧笔对照窗口按起止时间对齐
  （单侧行 = 基线独有 / 当前新增），窗口为首差异前后各 5 行。

## 数据布局（`data/eval/`）

- `snapshots/<hash16>.json.gz` —— K线快照 `{res: [bars]}`（gzip）。内容寻址去重：
  同一批 bars_all_tf 数据建多个用例只存一份。删除用例不删快照（可能共享）。
- `cases/<caseId>.json` —— 用例（纯输入、不可变）：`{id, name, symbol, res
  (3/15/60/240/D/ALL), fromTs, toTs, snapshot, chanCfg, pointsCfg, barCounts, createdAt}`。
- `baselines/<baselineId>.json` —— 基线：`{id, name, note, caseIds, expected:
  {caseId: {bis: {res: [bi]}, marks: {res: [mark]}, frozenAt}}, createdAt/updatedAt, lastRun:
  {at, pass, total, cases}}`。期望（笔 + 买卖点标记）冻结在基线内 → 同批用例可建多个基线做改动前后
  A/B 对比；差异明细不落盘（重跑即得）。2026-10-03 前的旧基线无 marks → 运行时只比笔，
  UI 标「—·旧」，对其「更新期望」即补上。

## 语义要点

- **用例创建**：一键快照根目录 `bars_all_tf.json`（「基础数据」页拉取的当前图表缓存）；
  可选 `fromTs`/`toTs` 截断各周期到区间（按K线开盘时刻计，边界含当根；须 toTs>fromTs；
  **所选周期**（res=ALL 即全部 5 个）截断后每周期 ≥50 根否则拒绝，未选周期不设下限——
  build_bis 对 <6 根的周期自动跳过、上级锁随之缺省，同快照下仍逐位可复现；截断后前段
  笔与全量图不同属正常——基线只要求对同一份快照可复现）。symbol 取当前分析配置
  `app.analysis.cfg.symbol`；`chanCfg` 快照 = 创建时 `param_center.chan_cfg_effective(symbol)`。
- **建立基线 / 更新期望**：对选用例用**当时**的逻辑重算并冻结 expected（后台任务，
  同一时刻仅一个任务，忙时 409）。「更新期望」仅在逻辑有意变更后使用。
- **跑基线**：逐用例解压快照 → 按用例 chanCfg 应用（若与当前有效参数不同则标记
  `参数漂移`，UI 黄色徽标）→ build_bis → compute_all_marks（pointsCfg 口径，漂移同语义）
  → 与冻结期望比对（笔 11 字段 + 买卖点 6 字段：label/time/price/rawTime/rawPrice/color）；
  任务结束后恢复任务前的进程内画笔参数（`chan_core.apply_cfg`，try/finally）。
  笔与买卖点全过才算通过；旧基线无 marks 时只比笔。
- **res=ALL**：期望与比对覆盖全部 5 个周期（30S 不参与）。
- **删除保护**：用例被基线引用时禁止删除（提示基线名）；删除基线不影响用例/快照。
- **lastRun 摘要**写回基线文件（仅通过数/逐用例布尔，不存差异明细）。
- **冻结明细视图**：基线列表「明细」按钮 / 冻结完成的「查看冻结明细」→ `bl-bis` 渲染
  逐笔 11 字段表 + 逐周期买卖点表（随时可看）。差异展开顶部「差异笔清单 / 差异点清单」
  按号聚合字段差异（#12：type、startTime…），直接回答"哪几笔/哪几个点有问题"；
  超过 MAX_DIFFS=50 条截断并标注。

## API（`/api/eval/*`）

- GET `bars-info`（缓存概况+当前品种）、`cases`、`baselines`、`run-state`（任务快照）、
  `bl-bis?id=`（基线冻结笔明细：逐用例各周期完整冻结笔，前端「明细」视图）
- POST `create-case {name,res,fromTs?,toTs?}`、`delete-case {id}`、
  `create-baseline {name,caseIds}`、`refresh-baseline {id}`（重冻期望）、
  `rename-baseline {id,name,note?}`、`delete-baseline {id}`、
  `run {baselineIds}`（空=全部）

## 与既有基线工具的关系

| 工具 | 覆盖 |
| --- | --- |
| EVAL 评估（本页） | 画笔（bi）结构回归，WEB 可视化，参数口径锁定 |
| `align_check.py` | py↔JS 双实现逐笔对拍 |
| `engine_consistency.py` | 增量引擎 = 前缀批量一致性 |
| `dump_baseline.py`（.baseline/.after） | 回测统计/交易留档 |

## 测试

`python -m unittest py_chain.test_eval -v`：快照去重/截断校验、冻结→运行全通过、
篡改期望（字段/缺笔）差异定位、参数漂移与恢复、基线引用保护、比对函数单测。
