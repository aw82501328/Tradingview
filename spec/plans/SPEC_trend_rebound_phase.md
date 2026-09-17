# 参考周期方向判定相位树：1卖/2\3卖后分相位与观望态（SPEC_trend_rebound_phase v2）

> 状态：**已执行**（2026-09-17；用户图片决策树 + 判据口径逐条确认，当日落地）
> 日期：2026-09-17（v1 2026-09-16 待执行稿已被本版**整体取代**——v1 的「近对侧买点
> 端点(×ATR) + DIF 0轴」判据作废，git 史留底）
> 范围：`py_chain/trading_plan.py`、`py_chain/param_center.py`、`py_chain/backtest.py`、
> `py_chain/mark_entry.py`、`py_chain/main.py`、`py_chain/web/params.html`（逻辑图解表2/2a/2b）、
> `py_chain/test_trend_filter.py`、`py_chain/test_param_center.py`、
> `py_chain/SPEC.md` §2.1.2.1、`.cursor/skills/trading-plan/SPEC.md` §5。
> Python与JS方向相位及预期笔规则现已同步，补充规范见 [预期笔与运行笔](SPEC_structure_context.md)。

## 1. 背景

`trend_direction` 旧口径：4小时 1卖/2\3卖 确立空头后、破坏闩锁触发前一直锁死「空」
（如 8-25 1卖@4697 后连跌 300 点仍显示「空（4小时1卖）」，多单全被拦）。
新逻辑（用户图片决策树）：锚点确立后按「形成段方向 → 够笔 → 位置（中枢上/下沿、
前低/前高） → 角度强弱」分相位，引入**观望态**（dir=None 带 reason，双向放行带注记）。
**非1类锚点（2\3卖后）也走相位表**（改动旧「出现即确立」行为）。1买/2\3买完全镜像。

## 2. 规则（图片决策树；优先级从上到下命中即停）

### 2.1 总表（参考周期默认 4 小时；①~⑤ 全部沿用现有）

| 序 | 条件 | 方向 | 来源 |
|---|---|---|---|
| ① | 笔数据 < 2 根 | 无方向（不过滤） | 现有 |
| ② | 最近 60 笔内无买卖点 | 末笔方向（末笔向上→多 / 向下→空） | 现有 |
| ③ | 最近点=1买/1卖，强分型未出现 | 回退末笔方向 | 现有 |
| ④ | 破坏闩锁：确立后任一根参考周期K收盘破锚点价 | 反向（下跌/上涨延续），闩到下个点——**优先于相位表** | 现有 |
| ⑤ | 1类锚点+强分型已过+闩锁未触发 | 表2a 相位 | 2026-09-17 |
| ⑥ | 非一类锚点（2/类2/3/类3）+闩锁未触发 | 表2b 相位（改动旧「出现即确立」） | 2026-09-17 |

### 2.2 表2a：1卖后相位（1买完全镜像——多空互换、反弹↔回调、前低↔前高）

| 形成段 | 条件 | (dir, reason) |
|---|---|---|
| 向下 | 下跌不够笔 | ("short", "{名}1卖进行中") |
| 向下 | 够笔+近中枢上沿或下沿+角度强 | (None, "{名}1卖近中枢观望") |
| 向下 | 够笔+近中枢上沿或下沿+角度弱 | ("long", "{名}1卖转多预期") |
| 向下 | 够笔+其他位置+角度强 | ("short", "{名}1卖进行中") |
| 向下 | 够笔+其他位置+角度弱 | (None, "{名}1卖后方向不明") |
| 向上 | 反弹不够笔 | ("long", "{名}1卖反弹") |
| 向上 | 反弹够笔+力度强 | ("long", "{名}1卖强反") |
| 向上 | 反弹够笔+力度弱 | ("short", "{名}2卖预期") |
| 向上 | 段起点≤锚点（下跌未开始） | ("short", "{名}1卖进行中") |

1买镜像：进行中/近中枢观望/转空预期/方向不明/回调/强回/2买预期；中枢上/下沿结论相同
（图片上沿、下沿两支完全一致，实现合并为「近中枢边界」）。

### 2.3 表2b：2\3卖后相位（2\3买完全镜像）

| 条件 | (dir, reason) |
|---|---|
| 前低附近 | (None, "{名}2/3卖前低附近") |
| 其他位置+下跌不够笔 | ("short", "{名}2/3卖进行中") |
| 其他位置+够笔+角度弱 | (None, "{名}2/3卖后方向不明") |
| 其他位置+够笔+角度强 | ("short", "{名}2/3卖进行中") |
| 形成段向上（反弹中、未涨破卖点价） | (None, "{名}2/3卖反弹中")——图未覆盖，默认观望（用户 2026-09-17 确认；闩锁仍在上：收盘涨破卖点价即转 多·上涨延续） |

2\3买镜像：前高附近/进行中/方向不明/回调中。

### 2.4 术语口径（用户 2026-09-17 确认）

- **形成段** = 当前结构上下文末段，含普通分型确认后立即参与的预期段。
- **够笔** = 从起点所在合并块起（含）达到 `CHAN_CFG.expectBiMinBars`，默认5块；不再使用原始K线数或历史多腿代替当前段计数。
- **角度强/弱**（下跌角度与反弹/回调力度同一口径）= 当前笔平均每根点数
  `span/根数 > reboundAngleRef`（45° 基准，点/根，固定点数不乘 ATR）为强。
- **近中枢边界** = 形成段极值（末笔端点价）距「锚点前最近已形成中枢」的 ZG/ZD
  ≤ `reboundNearPts`（绝对点数）；中枢取法复用 trading_plan 震荡判定同款
  （有上级笔 `buildZSByUpper(bis, upperBis, barSec)` 否则 `buildZS(bis, barSec)`，
  取最后一个 startTime ≤ 锚点时间者）；无中枢 → 其他位置。
- **前低/前高**（2/3类锚点）= 锚点前最近一个 down/up 笔端点价，极值距其
  ≤ `reboundNearPts` 为附近；无 → 其他位置。
- **观望** = dir None + reason 保留 → 进场双向放行，信号仍附 trendReason 注记
  （mark_entry 附字段条件已放宽为 `if trend_dir or trend_reason:`）。

## 3. 实现落点（已执行）

- `trading_plan.py`：常量 `TREND_REBOUND=True`、`REBOUND_NEAR_PTS=5.0`、
  `REBOUND_ANGLE_REF=5.0`；`_seg_bars_since` + `_phase_direction`
  （卖点侧树+买点镜像，返回三态）；`trend_direction(..., rebound=None)`（闩锁循环后、
  确立返回前插入，前 5 个位置参数不动）；`trend_state_of(..., cfg=None)` 组装
  rebound dict（min_bars 取 CHAN_CFG.expectBiMinBars），work_cache key 并入相位三参数。
- `param_center.py` plan 模块：`trendRebound`（开关）、`reboundNearPts`(0.0~1000.0)、
  `reboundAngleRef`(0.1~100.0)；`defaults_of("plan")` 并入常量。
- `backtest.py`：`__init__` 与 trendRes 同模式 pop 三键 → `self.rebound_cfg`；
  `_rebuild_chain` 的 `trend_state_of(..., cfg=self.rebound_cfg)`。
- `mark_entry.py`：`compute_entries(..., trend_cfg=None)`（自算 trend_state 路径传 cfg）；
  两处信号附注放宽 `if trend_dir or trend_reason:`（evaluateRealtimeEntries ~L973 与
  compute_entries ~L1102）；方向门控不变（dir=None 自动双向放行）。
- `main.py`：`build_full_chain` pop 三键 → `trend_cfg` 透传。
- `web/params.html`：逻辑图解表2 规则/总表更新 + 新增表2a/2b 相位表 + 判据大白话注；
  plan noteLine 补参数说明。

## 4. 参数（参数中心 → 交易计划）

| 参数 | 默认 | 说明 |
|---|---|---|
| trendRebound | True | 方向相位判定开关（False = 旧行为：确立锁到闩锁/新点） |
| reboundNearPts | 5.0 | 「附近」容差（绝对点数）：距中枢 ZG/ZD 或前低/前高 |
| reboundAngleRef | 5.0 | 角度 45° 基准（点/根）：当前笔平均每根点数 > 该值 = 强（XAUUSD 4h 量级，跨品种需调） |

## 5. 测试（已执行）

`py_chain/test_trend_filter.py`：`TestPhaseSell1Anchor`（9 叶+闩锁优先+关退旧行为）、
`TestPhaseSell23Anchor`（前低/角度/反弹中/类2卖3卖同树/闩锁）、`TestPhaseBuyMirror`
（镜像抽查）、`TestTrendStateOfReboundCfg`（cfg 组装/关闭/默认/缓存参数敏感）、
`TestEntryFiltering.test_wait_state_passes_with_note`（观望态双向放行带注记）。
`py_chain/test_param_center.py`：corrupt 回退默认键集补三新键。

## 6. 风险与已知简化

- 45° 角度的数学映射依赖 `reboundAngleRef` 点/根基准（固定点数，不随波动率自适应）；
  近位容差同为固定点数——跨品种需调参。
- 近中枢用当拍中枢（buildZS 现算，取锚点前最近一个）；够笔为预期口径（不等分型确认）。
- 反弹确认后 bis[-1] 翻下 → 回到下跌分支重判（reason 变化，方向语义一致）。
- 观望态：`signalDirectionName` 显示「多/空（{周期}1卖后方向不明）」式注记（信号自身
  方向+趋势注记）；方向筛选按基名 多/空 匹配不受影响。
- 默认开启改变 1类/非1类锚点场景的真实回测信号（特性目的）；bt_runs 历史方案不受影响。
- JS（工作台分析/技能）方向口径自此与 Python 分叉，文档已声明。
