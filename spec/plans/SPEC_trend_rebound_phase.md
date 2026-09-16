# 参考周期「反弹相位」判定：1卖后够笔三岔与观望态（SPEC_trend_rebound_phase）

> 状态：**待执行**（规则已与用户逐条确认 2026-09-16；代码未动）
> 日期：2026-09-16
> 范围：`py_chain/trading_plan.py`、`py_chain/param_center.py`、`py_chain/backtest.py`、
> `py_chain/mark_entry.py`、`py_chain/main.py`、`py_chain/web/params.html`、
> `py_chain/test_trend_filter.py`、`py_chain/test_param_center.py`、
> `py_chain/SPEC.md` §2.1.2.1、`.cursor/skills/trading-plan/SPEC.md` §5。
> 仅 Python 链路（回测/信号表/参数中心）；JS 技能端不同步（后续任务）。

## 1. 背景

`trading_plan.trend_direction` 现口径：4小时 1卖 确立空头后、破坏闩锁触发前一直锁定空
（如 8-25 1卖@4697 后连跌 300 点仍显示「空（4小时1卖）」）。新逻辑：1卖后下跌一旦够笔，
方向三岔——近结构位→看反弹（多）、其他→观望；反弹段够笔后按 DIF 分「2卖预期（空）/观望」。
默认开启、参数可配。1买镜像。

## 2. 规则（用户确认版，优先级从上到下命中即停）

### 2.1 总表（参考周期默认 4 小时）

| 序 | 条件 | 方向 | 来源 |
|---|---|---|---|
| ① | 笔数据 < 2 根 | 无方向（不过滤） | 现有 |
| ② | 最近 60 笔内无买卖点 | 末笔方向（末笔向上→多 / 向下→空） | 现有 |
| ③ | 最近点=1买/1卖，强分型未出现 | 回退末笔方向 | 现有 |
| ④ | 非一类买卖点（2/类2/3类），出现即确立 | 买类→多、卖类→空（如 空（4小时2卖）） | 现有 |
| ⑤ | 破坏闩锁：确立后任一根参考周期K收盘破锚点价 | 反向（下跌/上涨延续），闩到下个点 | 现有 |
| ⑥ | 1类锚点+强分型已过+闩锁未触发 | 相位表 ↓ | 新增 |

### 2.2 相位表⑥（trendRebound 开，默认）

锚点=**1卖**（形成段=延伸中的末笔）：

| 形成段 | 条件 | 方向 |
|---|---|---|
| 向下 | 下跌不够笔 | 空（4小时1卖进行中） |
| 向下 | 够笔 + 极值近锚点前买点端点(≤reboundNearAtr×ATR) | **多（4小时1卖后反弹预期）** |
| 向下 | 够笔 + 不近结构位 | **观望（4小时1卖后方向不明）** → 双向不滤 |
| 向上 | 下跌未开始（段起点≤锚点） | 空（4小时1卖进行中） |
| 向上 | 反弹段不够笔 | **多（4小时1卖后反弹）** |
| 向上 | 反弹段够笔 + 最新K DIF≤0 | **空（4小时2卖预期）** |
| 向上 | 反弹段够笔 + DIF>0 | **观望（4小时1卖后方向不明）** → 双向不滤 |

锚点=**1买**（镜像）：

| 形成段 | 条件 | 方向 |
|---|---|---|
| 向上 | 上涨不够笔 | 多（4小时1买进行中） |
| 向上 | 够笔 + 近卖点端点 | 空（4小时1买后回调预期） |
| 向上 | 够笔 + 不近 | 观望（4小时1买后方向不明） |
| 向下 | 回调段不够笔 | 空（4小时1买后回调） |
| 向下 | 回调段够笔 + DIF≥0 | 多（4小时2买预期） |
| 向下 | 回调段够笔 + DIF<0 | 观望（4小时1买后方向不明） |

收尾规则：

- 出现更新的买卖点 → 回到总表重新锚定
- trendRebound 关闭 → ⑥ 退回现行为：空（4小时1卖）/ 多（4小时1买），锁定到闩锁或新点
- 观望态：进场双向放行，信号行仍标注「（4小时1卖后方向不明）」式注记

### 2.3 术语口径

- **够笔** = 形成段起点以来 ≥ `CHAN_CFG.expectBiMinBars`（默认 5）根本级K，或锚点后已有确认的同向笔（多腿下跌覆盖「从高点一笔下跌已够笔」）
- **结构位** = 锚点前所有买/卖点端点价（不限 3买——笔端点即关键转折点）；容差 = reboundNearAtr × 参考周期当拍 ATR
- **0轴** = 最新参考周期K 的 DIF 符号（与 mark_entry.macdAboveZero/macdBelowZero 同口径）
- **观望** = dir None + reason 保留 → 进场双向不滤，信号仍附 trendReason 注记
- 用户已移除「内部背驰」条件（2026-09-16 更新；原设计含 realtimeLowerDiverge 低级别探测，不再需要）

## 3. 实现设计（待执行）

### 3.1 trading_plan.py（核心）

- 新常量（`TREND_RES = "240"` L365 后）：`TREND_REBOUND = True`、`REBOUND_NEAR_ATR = 1.5`。
- 新私有函数 `_rebound_phase(name, p, t_, bis, bars, macdArr, pts, rb, tCut=None)`（插
  strong_fractal_after L394 后），返回 `(dir, reason)`，dir 可为 None（观望）。要点：
  - 结构位池 `keyPts = [q["price"] for q in pts if q["type"] in 对侧类型 and q["time"] < p["time"]]`（严格早于锚点，排除锚点自身）
  - 够笔计数：`times = [b["time"] for b in bars]` 纯 bisect（`bisect_left(seg.startTime)` 到 `bisect_right(now)`，与 mark_entry._barsSince 同口径；不用 bisect key= 避免 Python 版本依赖），或 `any(b.type=="down" and b.startTime>=p.time for b in bis[:-1])`
  - 近结构位：`any(abs(seg.endPrice - e) <= near_atr * atr)`，atr 取 `rb["atr"]` 或 `calcATR(bars, 14)` 兜底
  - DIF 分叉：`macdArr[-1]["dif"]` 符号内联（不 import mark_entry）
  - seg 向上且 `seg.startTime <= p["time"]`（1卖恰在当前形成段端点、下跌未开始）→ 视同「1卖进行中」
- `trend_direction`（L397）签名追加 `rebound=None`（前 5 个位置参数不动——test_trend_filter L198 断言 positional[3]）；在闩锁循环（L446-453）之后、L454 确立返回之前插入：`rebound` 提供且 `rebound.get("enabled", True)` 且 `t_ in ("1买","1卖")` → `return _rebound_phase(...)`。docstring 补规则 7（观望语义：dir=None 消费方不过滤）。
- `trend_state_of`（L457）签名追加 `periodAtr=None, cfg=None`。cfg 含 trendRebound（默认 TREND_REBOUND）时组装
  `rebound = {"enabled": True, "near_atr": float(cfg.get("reboundNearAtr", REBOUND_NEAR_ATR)), "min_bars": int(CHAN_CFG.get("expectBiMinBars", 5) or 5), "atr": (periodAtr or {}).get(tr) 或 calcATR(bars,14) 兜底}`，传给 trend_direction。无 div_probe / periodData / mark_entry 依赖。

### 3.2 param_center.py

- `PARAM_MODULES["plan"]["params"]`（L88 trendRes 后）：
  - `"trendRebound": ("反弹相位判定", "1卖/1买确立后按够笔+近结构位判反弹/回调相位与观望态（闩锁优先；仅回测/监控引擎，JS技能无此参数）", None, None)`
  - `"reboundNearAtr": ("近结构位容差(×ATR)", "形成段极值与锚点前买/卖点端点价差 ≤ 该值×参考周期ATR 视为接近（调大等效关闭）", 0.0, 50.0)`
- `defaults_of("plan")`（L112）并入两常量——**与 specs 同一提交**（test_schema_matches_defaults_keys 键集护栏）。持久化 module_params.json 只存 override、bt_runs 历史方案零迁移。

### 3.3 backtest.py

- L60 import 追加 `TREND_REBOUND, REBOUND_NEAR_ATR`。
- `__init__` L291 `self.trend_res` 后：`self.rebound_cfg = {两键从 plan_mp.pop（缺省回退常量）}`——与 trendRes 同模式 pop，不混入震荡阈值 plan_cfg。
- `_rebuild_chain` L1014：`trend_state_of(periodBis, barsByPeriod, self.trend_res, periodMacd=periodMacd, periodAtr=periodAtr, cfg=self.rebound_cfg)`（periodAtr 同拍局部变量已有；外围 try/except 已有）。

### 3.4 mark_entry.py

- `compute_entries`（L986）签名追加 `trend_cfg=None`；L1048-1050 自算路径传 `periodAtr=periodAtr, cfg=trend_cfg`（periodAtr 形参作用域内已有）。
- **两处信号附字段条件放宽**（观望态也要带注记）：evaluateRealtimeEntries L969 与 compute_entries 对应处（~L1098）`if trend_dir:` → `if trend_dir or trend_reason:`。方向门控 `if trend_dir and ...` 不变（dir None 自动双向放行）。evaluateRealtimeEntries 其余不动（trend_state 一律引擎传入）。

### 3.5 main.py

`build_full_chain` L89-90：pop trendRes 后 `trend_cfg = {k: plan_mp.pop(k) for k in ("trendRebound","reboundNearAtr") if k in plan_mp}`，L94 compute_entries 追加 `trend_cfg=trend_cfg`（缺省 → 模块常量默认全开）。

### 3.6 不动清单

webapp.py（`_engine_module_params` 的 `"plan": pm["plan"]` 整体传入，新键自动带上）、analysis_service.py（--trend-res 是 JS 支线）、monitor.py、bt_runs.py、前端 index.html（trendReason 显示逻辑通用）。

## 4. 参数页展示（用户指定）

`py_chain/web/params.html`：

- `MODULE_RULES.plan`（L72-84）追加 `tables` 字段（HTML 字符串），内容 = §2 整合总表（①-⑥）+ 相位表（1卖 7 行 / 1买镜像 7 行）+ 收尾规则 + 术语注（形成段/够笔/结构位/0轴 各一行）；既有 items 保留并补一条相位摘要、方向列文案示例更新为新 reason。
- `renderModule`（L156-163）扩展：`rules.tables` 存在时拼在 `</ol>` 之后。
- style 块补 `.prules table` 最小样式（border-collapse、th/td 边框内边距、th 底色，与页面风格一致）。
- plan noteLine（L154）补「反弹相位仅回测/监控引擎（JS 技能无此参数）」。

## 5. 测试（待执行）

既有用例排查结论：**全部不变**（直调 trend_direction 未传 rebound=None 即关闭；2/3类锚不跑相位；闩锁优先；TestTrendStateOf positional 断言不受追加 kwarg 影响）。

- **test_trend_filter.py** 新增：
  - `TestReboundPhaseSellAnchor`（1卖@111 锚、2买@90 端点、atr=10 容差15，mock 点）12 条：seg down 不够笔→("short","4小时1卖进行中")；够笔+近→("long","4小时1卖后反弹预期")；够笔+不近（near_atr 调小）→(None,"4小时1卖后方向不明")；锚点后已有确认下跌笔→也判够笔；无前买点端点→观望；seg up 起点在锚点前→1卖进行中；反弹段不够笔→("long","4小时1卖后反弹")；反弹够笔 dif≤0→("short","4小时2卖预期")；dif>0→(None,"4小时1卖后方向不明")；闩锁优先（close>111）→("long","4小时上涨延续")；enabled=False→("short","4小时1卖")（旧串）；rebound=None→同前。
  - `TestReboundPhaseBuyAnchor` 镜像 6 条（1买进行中/回调预期/观望/回调/2买预期 dif≥0/观望 dif<0）。
  - `TestTrendStateOfRebound`：cfg trendRebound=False→mock 断言 rebound is None；缺省→ctx 字段（near_atr=1.5/min_bars=CHAN_CFG.expectBiMinBars/atr=periodAtr 传入值）。
  - `TestEntryFiltering` 补 1 条：trend_state={"dir":None,"reason":"4小时1卖后方向不明"} → 双向信号都放行且带 trendReason 注记。
  - 头部 docstring 补覆盖点。
- **test_param_center.py**：`test_rebound_params`（defaults 与常量一致、bool/float 类型、normalize 越界/非 bool raise、update→effective→reset 往返）。
- 运行（仓库根，逐模块）：`python -u -m unittest py_chain.test_trend_filter py_chain.test_param_center -v`；回归 `test_mark_entry_sink test_divergence_fallback test_near_double test_start_ts test_backtest_perf test_exit_rules`；全套 discover 兜底。

## 6. 文档同步（待执行）

- `py_chain/SPEC.md` §2.1.2.1 L125：段末追加相位摘要（两分支三岔/镜像/观望=不过滤但附注记/优先级/参数默认/仅 Python 引擎）。
- `.cursor/skills/trading-plan/SPEC.md` §5：§5.1 加规则 7；§5.2 补 reason 新字符串（1卖进行中/1卖后反弹预期/1卖后反弹/2卖预期/方向不明）；§5.3 边界表加行（观望不过滤/下跌未开始视同进行中/非1类不判相位）+「JS 端暂不同步」声明。
- `web/params.html` 见 §4。

## 7. 风险与已知简化（SPEC 注明）

- 近结构位容差用当拍 ATR（非锚点时点 ATR）；够笔为预期口径（不等分型确认，与当下制一致）。
- 反弹确认后 bis[-1] 已翻下时回到下跌分支重判（reason 变化，方向语义一致）。
- 观望态：`signalDirectionName` 将显示「多/空（{周期}1卖后方向不明）」（信号本身方向+趋势注记）；方向过滤键取基名 多/空 不受影响。
- 默认开启会改变真实回测在 1类锚点场景的信号（特性目的）；bt_runs 历史方案不重跑不受影响。

## 8. 验证（待执行）

1. 单测全绿（§5 命令）。
2. 端到端只读复现（bars_all_tf.json + build_bis 截断至 2026-09-03 10:39 决策拍，`trend_state_of(..., cfg=默认)`）：若 4395 与锚点前某买点端点价差 ≤1.5×ATR → 多（4小时1卖后反弹预期），否则 → 观望（4小时1卖后方向不明）；截断到更早拍（下跌头几根）→ 空（4小时1卖进行中）。
3. Web 冒烟：参数页出现 2 个新参数、相位表格渲染、保存/重置往返；跑一轮回测，方向列出现新文案；观望态双向信号都出现。
