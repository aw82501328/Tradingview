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
- **笔结构对齐 JS 图表算法 + 区间套下沉判定（2026-09-08，实现 SPEC_divergence_chanset.md）**：
  - **块1 笔对齐**：`chan_core.py` 回填 JS chan-core 近几轮迭代规则——`markWickBars`
    （长上/下影压平 + `_topCand`/`_origLow` 旁路）、`_mergeStep` 旁路字段传播、`fractalAt`
    顶分型端点用 `_topCand` 影线价、`buildBi.fractalRangeClear` 双向版（起点侧按方向 2 根 +
    终点侧 3 根防反向吞没——曾致 15m 三段被吞成一笔、案例 A 的 15m 背驰永不成立）、
    `buildBi` 最小间隔脆弱笔例外（fragileMinimal）、`fixBiExtremes` 底端点含分型中心 +
    `_origLow` 恢复通道。引擎增量路径接入长影预处理（`_wick_process`：压平立即生效、
    `_topCand` 延后一根回标——与图表批量版「末根无 next」同口径）并补 `nearDouble`（≥60m）。
  - **对拍工具**：`python -m py_chain.align_check`（JS 侧 `.cursor/skills/chan-core/scripts/rebuild_bis.js`
    复用图表算法源重建）——bars_all_tf.json 6 周期（含 30S）逐笔 diff **0 差异**。
  - **增量=batch 一致性**：`python -m py_chain.engine_consistency`——引擎逐根推进状态在
    重同步点上严格等于 batch(前缀)；增量 wick 运行均值与全量均值的早期边界漂移由
    `_resync_bis`（每 `RESYNC_EVERY=1000` 根 fine bar 全量重同步）消除，`run()` 收尾
    最终重同步使末端状态严格等于 batch(全前缀)。
  - **块2 下沉判定**（`mark_entry.py`）：`sinkChainRealtime`/`sinkChainConfirm` 按规则 1
    逐级下沉（次级别 ≥3 笔且末段终点即 P 才下沉，链连续不可跳级，上限=检测周期 X）；
    `realtimeLowerDiverge`/`lowerDiverge` 只在停止级产候选；规则 2 参照 containment
    （参照跨所属上级笔 → 候选无效）。效果：案例 A（09-01 08:12）markRes=3 → 15；
    案例 B（09-04 15:48 同笔）不再出信号；markRes 分布从 3 一家独大回归多级别。
  - **残余差异（文档化）**：图表管线专属步骤（lockedPivots 上级锁定 / alignBiToUpper /
    ATR 幅度过滤 / calibrateBiTimes / 绘制窗口）未移植进引擎——引擎 vs 图表落盘
    端点吻合率 D 75%（4 笔小样本）/240 93%/60 93%/15 97%/3 90%，案例窗口的 15m/60m
    结构一致；如需 100% 对齐图表再做二期管线移植。
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
- 回测完成后回画**实际成交的进场箭头**：做多=红色向上箭头、做空=绿色向下箭头；出场阶梯（§2.1）已实现，一并回画**出场标记**（`marks.py`：终局 `xcross` / 平一半 `circle`，默认色 `DEFAULT_EXIT_COLOR = #FFEB3B`，与 JS 技能和 Web 控制台一致）。
- 依赖：`websocket-client`（CDP 通信）；其余为 Python 标准库。**尚未执行 pip 安装验证**。

## 2. 架构与数据流

```
CDP 取数(data_loader) → chan_core(mergeBars/buildBi/buildZS/MACD/背驰)
  → mark_buy_sell(买卖点) → sr_flip(支阻位) → trading_plan(交易计划)
  → mark_entry(进出场) → backtest(点状重放+持仓模拟) → tv_draw(CDP 回画进场箭头)
```

## 2.1 进出场规则（2026-09-10 整理，与 mark_entry.js 对齐）

> **范围声明**：本节描述 `py_chain` 引擎与 `mark-entry` JS 技能这一套进出场规则，二者是**同一套规则的两份实现**（逐项差异见 §2.1.5）。
> 仓库里另有 `vnpy/backtest.py`——更早的独立实现（**绿色共振进场 + 仅 ATR 止损出场**，无保本 / 半平 / 止盈阶梯 / 支阻位止损 / 兜底滑点），当前**不被任何代码或页面调用**，不在本节范围内。
> `chan_strategy/` 目录只有 `chan_core.py`（笔算法对拍工具），不含进出场逻辑。

### 2.1.1 总览

```
【进场】交易计划给策略 → 三个公共条件 → 策略专属条件 → 进场信号（画在背驰级别）
        → 下一根K线开盘价成交
【出场】持仓后每根收盘判定：保本 → 平一半 → 全平 → 止损 → 结算（× 手数）
```

### 2.1.2 进场

#### 2.1.2.1 进场状态从哪来

每个检测周期读取「交易计划」给出的策略，映射到 6 种进场策略；计划为震荡 / 数据不足 / 无匹配时，该周期不进场。

| 计划策略 | 策略标识 | 方向 |
|----------|----------|------|
| 等待反弹后做 2 卖 | `wait2Sell` | 做空 |
| 等待回调后做 2 买 | `wait2Buy` | 做多 |
| 等待高点附近的一卖 | `wait1Sell` | 做空 |
| 等待低点附近的一买 | `wait1Buy` | 做多 |
| 等待回调后的新买点 | `waitBuy` | 做多 |
| 等待反弹后的新卖点 | `waitSell` | 做空 |

映射函数 `entryStrategyOf`（`mark_entry.py` L290 / `mark_entry.js` L360）；计划策略本身由 `trading_plan.strategyOf` 产出。

#### 2.1.2.2 三个公共条件（必须同时满足）

1. **够笔** `lastBiOk`——检测周期最后一笔方向要「对着来」：做空要等一段反弹（up 笔），做多要等一段回调（down 笔）。
2. **以下级别出现背驰** `lowerDiverge` / `realtimeLowerDiverge`——在更低周期里找同向背驰点（做空找顶背驰、做多找底背驰），且该点必须通过**下沉链**校验（见 §2.1.2.3）。
3. **在支阻位附近** `nearSr`——背驰点价格与任一合并支阻位（`srflip.merged`，含密集区 / 黄金分割 / BOLL 三类）的价差 ≤ 检测周期 ATR × `NEAR_ATR`（默认 1.0，CLI `--near`）。

#### 2.1.2.3 背驰属于哪个级别（下沉链 / 区间套，2026-09-08 实现）

**要解决的问题**：检测周期（如 60 分钟）要求等一个「更低级别的背驰点」才进场。但低级别（如 3 分钟）上背驰点遍地都是——随便取一个 3 分钟微背驰，会画出一个和 60 分钟结构毫无关系的箭头。所以必须先回答：**这个背驰点，结构上究竟属于哪个级别？**

**做法**：从候选点 P 出发，看检测周期上「以 P 为终点的那一笔」——

- 这一笔的内部，在**次一级别**上能拆出 **≥3 笔**完整结构（段间不跨上级笔边界、含形成中延伸段计 1 笔），且最后一笔终点正好是 P → P 属于次一级别，**下沉**到该次级别，重复检查；
- 次一级别拆不出 3 笔（说明和上级是「同一笔」，内部没有独立结构）→ **停止下沉，就在这一级判定**。

**下沉停止的那一级 = 背驰级别 markRes**（箭头画在这个周期上）。只有「候选点自身的级别 == 它下沉停止的级别」才被保留（`sinkChainConfirm` / `sinkChainRealtime`）——3 分钟的微背驰因此不会冒充 15 分钟的结构。

**虚拟形成笔**：P 晚于检测周期末段端点且该周期无精确端点笔时（如近等双顶平台），以「末段端点 → P」的开放段作容器参与展开计数。

**两条约束**：

- **不可跳级**：60 → 15 → 3 必须逐级连续（`--with-30s` 时为 3 → 30S），中间缺一级链就断了；下沉上限 = 检测周期 X。
- **不可跨上级笔**：候选背驰段的**参照笔**必须与候选段同处其所属上级笔内部；参照笔起点早于该上级笔起点 → 候选无效（丢弃，不回退更早参照）。

**两个实例**（详见 `SPEC_divergence_chanset.md`）：

- 案例 A（2026-09-01 08:12）：60m 反弹笔内部在 15m 上是 5 笔结构 → 下沉到 15m；15m 末段内部在 3m 上只有 1 笔 → 停在 15m。**markRes=15**（箭头画在 15 分钟），而不是 3 分钟。
- 案例 B（2026-09-04 15:48）：15m 笔内部在 3m 上只有 1 笔 → 停在 15m；而 15m 末段没有创新高 → **不出信号**（而不是错报一个 3m 信号）。

#### 2.1.2.4 策略专属条件

> 在三个公共条件之外，每个策略各自还要看的东西（`strategyExtraOk`，`mark_entry.py` L463-487）。**「破前底 / 过前高」是同向笔比同向笔**：拿最近一笔 down（或 up）笔的终点，比它之前最近那笔**同向**笔的终点。

| 策略 | 方向 | 额外要求 ①（结构） | 额外要求 ②（动能） |
|------|------|--------------------|--------------------|
| `wait2Buy` 等待回调后做 2 买 | 做多 | **创新高** `brokePrevHigh`：最近完成的 up 笔终点突破更早那笔 up 笔终点 | **DIF 在 0 轴上方** `macdAboveZero`——上过 0 轴后回调没跌破 0 轴，多头动能还在 |
| `wait2Sell` 等待反弹后做 2 卖 | 做空 | **创新低** `brokePrevLow`：最近完成的 down 笔终点跌破更早那笔 down 笔终点 | **DIF 在 0 轴下方** `macdBelowZero`——下过 0 轴后反弹没能上 0 轴，空头动能还在 |
| `wait1Buy` 等待低点附近的一买 | 做多 | **创新低**（同上） | **离开中枢力度变弱** `zsExitWeak(...,"long")`：离开笔必须是 **down（向下离开）**，且比进入笔弱——与进入笔 `isBiDiverge`（绿柱面积 / DIF 低点 / 柱高任一更弱）**或** 幅度 `span` 小于进入笔。中枢取 `buildZSByUpper` 最后一个；**中枢尚未被离开 → 不成立** |
| `wait1Sell` 等待高点附近的一卖 | 做空 | **创新高**（同上） | **离开中枢力度变弱** `zsExitWeak(...,"short")`：离开笔必须是 **up（向上离开）**，其余同上 |
| `waitBuy` / `waitSell` 新买点 / 新卖点 | 多 / 空 | — | — **无额外要求**，只要三个公共条件满足 |

> 一句话记忆：**二买/二卖 = 创新高（低）+ MACD 还在正确一侧**；**一买/一卖 = 创新低（高）+ 离开中枢时力气变小了**（后者就是背驰的教科书定义）。

#### 2.1.2.5 进场标记画在哪

- 做多 = 红色向上箭头 `#F23645`；做空 = 绿色向下箭头 `#089981`。
- 画在**背驰级别 markRes** 周期上，不是检测周期。例：1 小时计划做多、15 分钟出底背驰且靠近支阻位 → 箭头画在 15 分钟。

#### 2.1.2.6 成交与同向互斥

- 信号一律在**下一根 K 线开盘价**成交（跳空自然体现）。
- 同时刻多周期共振（多个检测周期命中同一背驰点）→ 取**检测周期最大**的一条，其余不成交（按 `intervalSecOf` 比较）。
- 同方向已有未终局持仓 → 新信号**被过滤**（落盘记 `suppressed`，不画箭头）；**多空互不影响**，可同时持仓。
- 引擎另有两种成交口径见 §1「成交口径」：`fill_mode="anchor"`（默认，含未来函数）/ `"confirm"`（无未来函数）；`signal_mode="realtime"`（默认，当下制）/ `"confirm"`（确认制）。

### 2.1.3 出场

#### 2.1.3.1 出场阶梯总览

持仓后**每根 K 线收盘**判定一次；**同一根 K 线内按固定顺序只看第一个命中的事件**：

> **保本 → 平一半 → 全平 → 止损**

所有成交型出场统一在**触发 K 线的下一根开盘价**成交；触发 K 线若已是最后一根则未成交（持仓保持 `open`，按最新收盘价 mark-to-market 收尾）。

| 顺序 | 事件 | 触发条件 | 动作 |
|------|------|----------|------|
| 1 | **保本** `breakeven` | 背驰周期出现进场后第一笔**有利方向**的完成笔（做空→down / 做多→up，`find_bi_event`） | 止损位从「止损参考位」上移到**保本位 beStop**（不动仓位，仅状态迁移） |
| 2 | **平一半** `half` | **仅顺势**：检测周期出现首个有利方向、**合并后 ≥5 根 K** 且有成笔预期的形成段（`forming_seg_ready`） | 平掉一半手数（余额 lots/2）；剩余半仓止损移到 beStop |
| 3 | **全平** `close` | **顺势**：检测周期有利方向笔**破前高 / 破前低**（`find_bi_event(..., break_prev=True)`）；**逆势**：达到上述「形成段 ≥5 根 K」条件 | 全部平掉（终局） |
| 4 | **止损** `stopSr` / `stopBe` | K 线**盘中**触及止损位（做空 `high > 位` / 做多 `low < 位`） | 全部平掉（终局） |

> **「合并后 ≥5 根 K」是什么意思**：用缠论 K 线包含合并后的结果计数——检测周期当前形成段自段起点块算起，**合并后满 5 块**即视为「具备成笔预期」，可提前减半或离场（`EXIT_MIN_MERGED=5`，与 `chan_core` 成笔判定 `isValid` 的 `gap>=4` 同一口径）。末笔延伸创新极值 → 锚点右移、计数归零。

#### 2.1.3.2 止损位怎么定（`stop_ref_of`，永远存在）

- 做空：进场价**上方**最近的支阻位 **+ 止损滑点**（默认 3）
- 做多：进场价**下方**最近的支阻位 **− 止损滑点**
- 进场信号自带的近支阻位 `nearSr` 若已在正确一侧 → 直接沿用；否则从支阻位列表重选正确侧最近位（进场判定逻辑不变，仅供出场止损参考）
- **没有正确一侧的支阻位 → 兜底止损 = 进场价 ± 兜底滑点**（默认 10）
- 结论：**止损位永远不为空**，不存在「不设止损」的仓位

#### 2.1.3.3 保本位 beStop 怎么定

= **进场成交那根 K 线的极值 ± 保本滑点**（做空：最高价 + 3；做多：最低价 − 3）。

保本事件触发（或先发生了平一半）后，止损位即换成 beStop；此后被打到记为**保本止损 `stopBe`**。
run() 批量路径成交 bar 当拍未收盘，存在 ≤1 根 fine bar 的微前视；step_to 实时路径无前视——研究口径可接受。

#### 2.1.3.4 顺势 / 逆势

- **顺势** = 计划方向 ∈ {多头多, 空头空}（计划结构方向 = 操作方向，`TREND_PLAN_DIRS`）
- **逆势** = {多头空, 空头多}
- 计划方向缺失时按 `strategyKey` 兜底：`wait2Buy`/`waitBuy`/`wait2Sell`/`waitSell` → 顺势，`wait1Buy`/`wait1Sell` → 逆势（`trend_following_of`）
- 顺势：阶梯完整——保本 → 半平 → 破前高/前低全平
- 逆势：**没有半平**——「形成段 ≥5 根 K」一到就**全平**快速离场

#### 2.1.3.5 结算：手数与盈亏

- 手数 `lots` 默认 **4**，参数化：CLI `--lots` / Web 回测界面 / `run_backtest`。
- 平仓盈亏 =（0.5 × 半平价 + 0.5 × 终局价 − 进场价）× 方向(±1) × 手数；未触发半平则 =（终局价 − 进场价）× 方向 × 手数（`close_trade`）。
- 未平仓按最新收盘价 mark-to-market × 手数；已 half 的按 0.5 × half 价 + 0.5 × 最新收盘加权。

### 2.1.4 参数表

| 类别 | 参数 | 默认 | 含义 |
|------|------|------|------|
| 进场 | `--near` / `NEAR_ATR` | 1.0 | 靠近支阻位阈值（× 检测周期 ATR） |
| 进场 | `--lots` | 4 | 每笔进场手数 |
| 出场 | `--slip-stop` | 3.0 | 止损位滑点（支阻位外侧偏移，绝对价格） |
| 出场 | `--slip-fallback` | 10.0 | 兜底止损滑点（无正确侧支阻位时） |
| 出场 | `--slip-be` | 3.0 | 保本滑点（beStop = 进场K线极值 ± 该值） |
| 出场 | `EXIT_MIN_MERGED` | 5 | 形成段成笔预期门槛（合并后 K 线块数） |
| 进场（当下制） | `REALTIME_MIN_BARS` | 5 | 检测周期形成中回调段的够笔门槛（段长 ≥5 根 K） |

### 2.1.5 附注：全仓库的实现分布 / 为什么会有「口径差异」

#### 2.1.5.1 三套实现，谁是谁

| # | 位置 | 进场 | 出场 | 谁在调用 |
|---|------|------|------|----------|
| 1 | `py_chain/mark_entry.py` + `backtest.py` | 6 策略映射 + 三公共条件 + 专属条件；`signal_mode` 分确认制 / 当下制 | 出场阶梯（保本 → 半平 → 全平 → 止损）；`advance_exit_decision` 是**回测 / 回放 / 实时监控三模式的唯一实现源** | WEB「回测与监控」（`/modes.html`）、CLI `py_chain.main` |
| 2 | `.cursor/skills/mark-entry/scripts/mark_entry.js` | 第 1 套规则的**平行 JS 实现** | 阶梯的**平行 JS 实现**（`simulatePosition`） | WEB「分析工作台」的「标记进出场」模块（`analysis_service.py:26`） |
| 3 | `vnpy/backtest.py` | **绿色共振**：周期 T 的 1买/1卖 与紧邻上级的 1/2/3 类买卖点同点位才开仓 | **仅 ATR 止损**（开仓极值 ± 1.0×ATR）+ 反向信号平仓/反手 | **无**——更早的独立实现，当前无人调用，**不在本节范围** |

#### 2.1.5.2 工作台（JS）vs 回测/监控（Python）：逐项差异

**根本原因：时间视角不同**

| | 工作台 · JS 技能 `mark_entry.js` | 回测/监控 · Python 引擎 `backtest.py` |
|---|---|---|
| 回答的问题 | 「这一段走完之后回头看，信号/出场该在哪」 | 「站在当下这根 K 线收盘时，我能知道什么」 |
| 笔数据来源 | 读 `bis_<品种>.json`——画笔跑完的**最终快照**，末笔已延伸到最新极值 | 引擎内部**增量重建**的当下状态，只用 `endTime ≤ 决策时刻` 的已确认笔 |
| 后果 | 全 hindsight（事后视角） | 无未来窥视 |

**进场侧差异**

| # | 差异点 | 工作台 · JS | 回测/监控 · Python |
|---|--------|-------------|---------------------|
| 1 | **信号模式**（影响最大） | 只有**确认制**——仅在笔结构变化时收集，取**已完成**的低级别背驰笔（`mark_entry.js` 内无 realtime 实现） | 默认**当下制** `signal_mode="realtime"`——每根 3 分钟收盘评估，取低级别**形成中段**（已延伸到当下极值）的背驰，**不等反向笔确认**；可用 `signal_mode="confirm"` 回到确认制 |
| 2 | **够笔门槛** | 只看最后一笔方向是否匹配 | 当下制另要求检测周期形成中回调段**段长 ≥5 根 K**（`REALTIME_MIN_BARS`），否则单笔回调场景永不触发 |

> 实测影响（见 §1）：同一区间，确认制 64 信号 / 延迟 avg 41.5 分钟；当下制 24 信号 / 延迟 avg 7.9 分钟，24 个共同信号，确认制多出的 40 个是「混合时点」信号。
> 所以**工作台看到的信号通常比回测/监控更晚、更多**。

**成交侧差异**

| # | 差异点 | 工作台 · JS | 回测/监控 · Python |
|---|--------|-------------|---------------------|
| 3 | **进场成交口径** | 不模拟进场成交价，只用信号价 `sig.price` 画箭头 | 默认 `fill_mode="anchor"`——背驰锚点之后第 1 根 fine K 线开盘成交。**含未来函数**（锚点当拍不可知），§1 有专门警示；`fill_mode="confirm"` 为无未来函数的原口径 |

**出场侧差异**

| # | 差异点 | 工作台 · JS | 回测/监控 · Python |
|---|--------|-------------|---------------------|
| 4 | **beStop 取哪根 bar 的极值** | `sig.time`（**信号时刻**）对应 markRes bar 的极值，向前找 `time <= entryT` 最近一根；取不到则回退 `进场价 ± slipBe`（`mark_entry.js:757-765`） | **实际成交**那根 fine bar 的极值。信号时刻 ≠ 成交时刻，两边锚定不同的 bar |
| 5 | **形成段 ≥5 块计数锚点** | `favSeg5Time`——`mergeStep` 回放记录每块**诞生时间**（`mergedBornTimes`），触发 = 段内第 5 块诞生那根 bar | `forming_seg_ready`——当下没有「诞生时间」历史，用**末笔延伸终点**在合并块数组里二分定位往后数。对「段起点在哪」定义不同 → 同一段可能差一块 |
| 6 | **未成笔的形成段** | hindsight：形成段达 5 块后若最终被笔结构吸收（未成笔）→ **不触发** | 当下：达 5 块即已触发。两侧代码注释均记为「已知近似差异」 |
| 7 | **出场成交价的单调性保护** | `nextOpen` 以 `lastFill`（最近成交时间）为下界，保证成交时序不回退（`mark_entry.js:771-776`） | 逐拍执行，天然单调，无此语义 |

**更底层：笔结构本身也有差异**

引擎未移植图表管线的专属步骤（`lockedPivots` 上级锁定 / `alignBiToUpper` / ATR 幅度过滤 / `calibrateBiTimes` / 绘制窗口），笔端点吻合率：**D 75% / 240 93% / 60 93% / 15 97% / 3 90%**（见 §1）。工作台读的是过完整图表管线的 `bis_<品种>.json`。

**为什么这是设计取舍而不是 bug**

- JS 侧的任务是**把已走完的行情画到图上**，用最终结构最自然，也方便人工复核；
- 引擎侧的任务是**不偷看未来**——含未来函数的回测会得出无法在实盘复现的漂亮结果（这正是 `fill_mode=anchor` 必须加警示的原因），实时监控更是只能用当下数据。

因此：**两者不能混用**，工作台图上的箭头与回测/监控的信号不保证一一对应，属研究口径差。

### 2.1.6 引擎实现索引（五档事件表与回调）

引擎（`BacktestEngine.run` / `step_to(execute=True)` 三模式同一套 `advance_exit_decision`/`execute_pending_exit`）
在成交后对每个持仓按时间顺序增量推进出场，**同拍顺序：保本 → 平一半 → 全平 → 止损（同拍只挂一个）**：

| 事件 | 触发条件 | 动作 |
|------|----------|------|
| 止损 `stopSr` | fine K线**盘中**破坏止损位（`stop_ref_of`：正确侧最近支阻位 ± 止损滑点；**无正确侧位兜底 = 进场价 ± 兜底滑点**，止损位永不为 None；跳空按开盘价成交） | 全平终局 |
| 保本 `breakeven` | 背驰周期（markRes）**够笔**：进场后首笔有利方向笔（short→down/long→up）完成（`find_bi_event`） | 止损位上移至**保本止损位 beStop** |
| 平一半 `half` | **仅顺势**（plan.direction ∈ {多头多, 空头空}，`trend_following_of`）：检测周期首个有利方向、**合并后 ≥5 根K且有成笔预期**的形成段（`forming_seg_ready`，引擎增量 `_merged_times` 计数；不要求保本先触发） | 平一半（剩余 lots/2），剩余半仓止损移至 beStop |
| 全平 `close` | **顺势**：检测周期**有利方向笔破前高/前低**（breakPrev）；**逆势**（多头空/空头多）：首个有利方向形成段（合并后 ≥5 根K成笔预期） | 全平终局 |
| 保本止损 `stopBe` | 保本（TP1 或 half）后盘中破坏 **beStop** | 全平终局 |

- **beStop**（引擎侧实现细节）：规则见 §2.1.3.3；`run()` 批量路径成交 bar 当拍未收盘（≤1 根 fine bar 微前视），`step_to` 实时路径无前视。
- **手数 lots**（引擎侧实现细节）：规则见 §2.1.3.5；参数化路径 CLI `--lots` / Web 回测界面 / `run_backtest`。
- **同向持仓互斥**（引擎侧实现细节）：同方向持仓未终局时新信号不成交（`stats["suppressed"]` + `on_suppressed` 回调，Web 行状态=同向过滤）；**多空互不影响**（各方向独立状态机）；同时刻同向共振按检测周期从大到小取一条。
- **回测新增回调**：`on_exit(tr)`（持仓终局）、`on_suppressed(s)`（互斥过滤）。
- **与 JS 的口径差**：见 §2.1.5.2（七条逐项差异表），此处不再重复。
- **Web 控制台**：信号行状态流转 `信号→持仓中→已平仓`（或 `同向过滤`），含出场时间/出场价/出场类型/盈亏/手数列；「标记进出场」按钮画箭头 + 黄色出场标记（`marks.py`：终局 `xcross` / 平一半 `circle`，默认色 `DEFAULT_EXIT_COLOR = #FFEB3B`）。
  - 注：`marks.py` 的模块 docstring（L11、L57）仍写着「统一灰 `#787B86`」，与常量和实际行为不符，属陈旧注释（本次未改代码）。

## 2.2 支阻位三类来源（2026-09-08 新增 BOLL 并统一合并管线，与 mark_sr_flip.js 对齐）

支阻位（`sr_flip.py` `compute_srflip`）分三大类，`srTypes` 参数分别开关（默认 `cluster,boll`，fib 默认关）：

- **密集区（cluster）**：强支阻互换位 + 近期极值位（原逻辑不变），走评分截断/跨周期合并/选取管线；
- **黄金分割（fib，默认关）**：对每周期每方向**最新的非一类买卖点**（2买/类2买/3买、2卖/类2卖/3卖，
  现算 `findBuyPoints`/`findSellPoints`），取其回调笔**紧邻前方的顺势笔**为参照笔画经典回撤
  分割位（买 `H-r×(H-L)` 支撑 / 卖 `L+r×(H-L)` 阻力，`fibLevels` 默认 0.382/0.5/0.618）；
  **预期回退（pending）**：该方向无已形成点时，用「形成中回调笔 + 其前方
  顺势笔」生成预期位（等待2卖/2买 的预期形成区；卖向需形成笔现高点 < 前方下跌笔起点，
  买向对称；次高点结构被否定自动失效），带 `pending:True` 同样进 merged
  （实盘口径：预期位置 + 够笔/小级别背驰确认；增量重放中随结构演变消失/重生，无未来函数）；
- **BOLL 布林带（boll，默认开）**：每周期取**最后一根已收盘K线**的布林带上/中/下轨
  （剔除取数末根形成中K线后，末 `bollLength`（默认 26）根收盘价的 SMA ± `bollMult`（默认 2）×
  **总体标准差（÷N）**，与 TradingView 同口径）：上轨=阻力 `RES`、下轨=支撑 `SUP`、
  中轨按现价侧（现价 ≥ 中轨 → 支撑，否则阻力）；bars < 26 的周期无布林位（正常降级）。

**统一合并管线（2026-09-08）**：三类候选（密集区截断后 + fib + boll）进**同一个**
`mergeFlipsAcrossPeriods` 池（不再有 fib「并行双轨」）。合并时维护每条 `srcType` 集合：
多来源混合线置 `srcType="mixed"` 并**删除** `fib/pending/ratio/fromPoint/referBi/boll` 标记
（统一按「位置线」口径）；纯单来源 fib/boll 独立线保留标记。显示模型改为「每周期图 ≤
2×`sideCount`（默认上下各 2 → ≤4 条）、高级别线继承到低周期图、仅该周期可见」，
`pickNearestForDisplay` 按与现价的价差就近选取（距离上限 ≤ `maxDistAtr` × 线自身级别 ATR），
输出 `drawnByPeriod`（替代旧的 `drawn`/`drawnFib`）。

- 透传链路：`BacktestEngine`/`run_backtest` 新增 `sr_types`/`fib_levels`/`boll_length`/`boll_mult` 参数
  （`_rebuild_chain` 同时传 `periodMacdIn` 复用增量 MACD 缓存）；`main.py` 新增
  `--sr-types`/`--fib-levels`/`--boll-length`/`--boll-mult` CLI 参数（全链路摘要与回测两段同口径透传）。
- **回测口径影响**：三类候选进入 `merged` 后 `nearSr`/`stop_ref_of` 命中集变大，部分信号的
  近支阻/止损参考位会落在 fib/boll 位上（预期行为）；`--sr-types=cluster` 可复现纯密集区旧结果，
  `--sr-types=cluster,fib` 可复现旧黄金分割行为。
- py 侧不做 fromTs 过滤（窗口由调用方决定，与 JS「买卖点在 from 过滤笔上算」的最终
  最新点结果一致）。

## 3. 文件清单与状态（截至 2026-09-01）

| 文件 | 状态 | 说明 |
|------|------|------|
| `py_chain/chan_core.py` | ✅ 已完成 | 以 `vnpy/chan_core.py` 为基线补齐 `fixBiExtremes`/`buildZS`/`buildZSByUpper`；另**新增增量原语**：`_mergeStep`、`fractalAt`、`updateFractalsTail`、`MacdAccumulator`、`AtrAccumulator`（供增量回测）。 |
| `py_chain/mark_buy_sell.py` | ✅ 已完成 | 移植 JS `mark_buy_sell.js`；`compute_all_marks` 支持可选 `periodMacd`/`periodAtr` 预计算参数。 |
| `py_chain/sr_flip.py` | ✅ 已完成 | 移植 JS `mark_sr_flip.js`；`compute_srflip` 支持可选 `periodAtrsIn`/`periodMacdIn`；**2026-09-08 支持三类来源统一管线**：密集区（cluster）+ 黄金分割（fib，非一类买卖点参照笔回撤）+ BOLL（boll，末根已收盘K线 ±2σ），`srTypes`/`fibLevels`/`bollLength`/`bollMult` 参数，见 §2.2。 |
| `py_chain/trading_plan.py` | ✅ 已完成 | 移植 JS `trading_plan.js`（含复制自 chan-status 的 `isRangeBound`）；`compute_plan` 支持 `periodMacd`/`periodAtr`。 |
| `py_chain/mark_entry.py` | ✅ 已完成 | 移植 JS `mark_entry.js`（6 种策略 + `findDivergePoints`/`evaluateEntry`）；`compute_entries` 支持 `periodMacd`/`periodAtr`；**出场纯函数** `stop_ref_of`（方向感知止损参考位）/`find_bi_event`（够笔/破高低点事件源，与 JS `stopRefOf`/`findBiEvent` 对齐）。 |
| `py_chain/data_loader.py` | ✅ 已完成 | `CDPClient`（HTTP 找 target + websocket `Runtime.evaluate`）、`fetch_bars`/`load_bars`/`load_cached`、`align_periods`；与 `load_all_tf.js` 对齐。 |
| `py_chain/backtest.py` | ✅ 已完成 | 增量点状回测引擎 `BacktestEngine` + `run_backtest` + `build_bis` + `summarize`；`_append_bars` 用 `extendLastBiFrom` 增量延伸 + 笔结构变化才重算链路（短路）；**出场状态机**（`_advance_exit`/`_close_pos`：止损+三档止盈+同向互斥，见 §2.1）。 |
| `py_chain/tv_draw.py` | ✅ 已完成 | `draw_trades` 通过 CDP `createShape` 回画进场箭头（多红空绿、文本 `BT·BUY/SELL + 价`），画前清除旧 `BT·` 标记。 |
| `py_chain/main.py` | ✅ 已完成 | CLI：`--symbol/--periods/--from/--port/--use-cache/--warmup/--no-draw/--no-marks/--sr-types/--fib-levels/--boll-length/--boll-mult`；串起取数→全链路→回测→回画→统计。 |
| `py_chain/test_sr_flip.py` | ✅ 已完成 | sr_flip 三类来源（密集区/黄金分割/BOLL）纯函数 + `compute_srflip` 集成单测（unittest，`python -m unittest py_chain.test_sr_flip -v`）。 |
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

### 5.1 链路函数内部热点优化（2026-09-07，输出逐位不变）

长窗口实测缩放超线性（每步成本随历史长度增长，2 个月 20,756 根 3m ≈ 20.7 分钟），35 天窗口
cProfile 归因后做四项「逐位等价」优化（每项均有新旧直接对照 + 全链路 A/B 验证，结果完全一致）：

1. `chan_core.biMacdMetrics` / `hasMacdCrossBetween`：MACD 数组按时间升序，bisect 定位 `[t0,t1]`
   闭区间窗口后只扫窗口（替代从头线性扫描）；时间列表按数组对象缓存（append-only，长度校验失效），
   消除 bisect key 回调的百万级开销。
2. `sr_flip.countBarsPassing`：numpy 可用时向量化比较（`low <= hiP & high >= loP` 计数，语义与
   逐根循环一致），lows/highs 数组按 bars 对象字典缓存（多周期交替必须按对象各自缓存）；
   无 numpy 回退纯循环。numpy 为可选依赖。
3. `chan_core.findBuyPoints` / `findSellPoints` 内部：2买/2卖 区间套的「上级笔 × 全部笔」双层
   循环改为 down/up 笔端点数组 + bisect 时间取窗（集合与顺序不变）；`_findIndex` 等值线性查找
   改 endTime→首次下标字典；`isSameAsUpperBi` 上级笔按类型分组传入（函数本就跳过异类型）。
4. 实测（XAUUSD，anchor/realtime，无 marks）：
   - 7 天（2295 根）：12.4s → 5.9s；21 天（6885 根）：283s → 88.4s（含 1+2）
   - 35 天（11475 根）：256.3s → 163.2s（1+2+3，累计 1.57×）
   - 2 个月全量（20756 根）：1242.9s → **718.2s（12.0 分钟，约 1.7×）**，每步 60.1ms → 34.7ms；
     信号 90 / 成交 65 与优化前全量完全一致
   - 仍有余量（见 §5.2）

### 5.2 尚未做的进一步提速项（按预期收益排序，风险递增）

- `_rebuild_chain` 每次 `bars[res][:_cut]` 整体切片拷贝（~3.5 万元素 × 近逐根重算）→ 传视图/复用切片；
- `buildBi` 分型一变即全量重建（O(n²) 主项）→ 确认笔前缀冻结、仅尾部重建（与 JS 一致性风险最高，需单独严格 A/B）；
- `detectFlip` 的历史回测时间二分已于2026-09-09实施；其他调用无索引时保留原扫描语义。
- realtime 模式 `strategyExtraOk` 的 `buildZSByUpper` 每根 O(B_l×B_u) → 按周期缓存、笔变失效。

2026-09-09第二批：`run()` 的 realtime 模式跳过未消费的确认式进场计算，
按运行复用各周期价格数组并以 `[:cut]` 限制可见数据，突破扫描按时间索引定位。
后段1500根采样34.45s → 19.02s（1.81×），结果摘要一致。
以f2962fe为基线、固定五周期21394根3m数据的完整回测：227.24s → 145.69s（1.56×）；
62信号、50成交，完整业务结果、事件序列和最终笔状态摘要一致。
当前默认支阻为cluster+boll且出场规则已更新，旧版90信号/65成交不作本批验收基线。
完整说明与验证记录见 `spec/plans/SPEC_backtest_perf.md` §7。

第三批成笔内部等价优化：单次buildBi按当前ATR建立跳空次数前缀，区间判定复用；
仅在间隔恰为3时检查MACD变色，命中成笔后才计算原始K线数量。
344组新旧成笔对照、18组阈值边界和133项单元测试通过；同一21394根全量耗时降至126.60s，
比第二批145.69s进一步减少13.1%，完整结果摘要保持一致。详见性能规格§8。

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
python -m py_chain.main --use-cache --no-draw --sr-types=cluster ...    # 复现纯密集区旧回测
python -m py_chain.main --use-cache --no-draw --sr-types=cluster,fib ... # 复现旧黄金分割行为
python -m py_chain.main --use-cache --no-draw --sr-types=boll --boll-length=20 --boll-mult=2  # 只标 BOLL
python -m unittest py_chain.test_sr_flip -v                           # sr_flip 单测
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


## 一小时近等端点补充确认（2026-09-10）

原阈值 max(0.3×ATR, 0.001×价格) 保持。仅60分钟价差超过原阈值但不超过1.5倍时，可由15分钟双动能确认：后段同色柱峰值与同侧DIF极值绝对值均不超过前段50%，两段柱峰值均非零，双底DIF均负、双顶均正。保留平台间隔、原阈值反向波动、锁定端点和单次替换保护；数据不足不走补充分支，不改变买卖点创新极值背驰定义。

JS/Python的`buildBi`增加可选末参`lowerContext`；用`makeBiLowerContext(res,bars,cutoff,macd)`准备15分钟数据和MACD索引，缺省参数保持旧行为。新增配置`nearDoubleLowerRelax=1.5`、`nearDoubleLowerRatio=0.5`。

画笔构建60分钟前需要完整15分钟历史（最近30天仅限显示）；回测、回放和监控仅使用当前决策时刻已收盘数据，先更新低周期。目标小时K线为8月7日08:00、4229.875，15分钟精确极值为08:45；固定样本仅目标相邻两笔变化，实际全量影响需回归检查。

完整条件、接口、保护和测试见[现行补充规范](../spec/plans/SPEC_near_double_lower_confirmation.md)。早期章节中原阈值的说明描述基础分支，与本补充分支共同适用。
