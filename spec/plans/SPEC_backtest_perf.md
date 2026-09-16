# 回测性能优化：链路函数内部热点逐位等价优化（SPEC_backtest_perf）

> 状态：第一批见 §4；第二批见 §7；第三批见 §8；第四批见 §9；第五批按周期缓存+BiInc 回溯收紧见 §10。
> 日期：2026-09-08（§9–§10 更新于 2026-09-16）
> 范围：`py_chain/chan_core.py`、`py_chain/sr_flip.py`、`py_chain/backtest.py`、`py_chain/bi_inc.py`、`py_chain/mark_entry.py`、`py_chain/trading_plan.py`（全部为「输出逐位不变」的纯性能优化）；`py_chain/SPEC.md` §5.1/§5.2 为同步记录。

## 1. 背景与问题定位

Web 控制台全量回测（XAUUSD，起始 2026-07-02，3m 时间轴约 2 万根）实测约 **20 分钟**，而
py_chain/SPEC 记录的 9-05 基准是 11,475 根 = 46s——相差 5~10 倍。定位过程与结论：

### 1.1 缩放曲线（实测，读缓存、纯计算、无 profile）

| 窗口 | 3m 根数 | 耗时 | 每步 |
|---|---|---|---|
| 3 天 | 459 | 0.4s | 0.9ms |
| 7 天 | 2,295 | 8.5s（优化前） | 3.8ms |
| 21 天 | 6,885 | 191s | 28ms |
| 35 天 | 11,475 | 256.3s | ~22ms |
| 2 个月 | 20,756 | ≈20.7 分钟 | 60.1ms |

**每步成本随历史长度超线性增长（总量近 O(n²)）**——增量引擎只增量到「何时重算」（链路短路），
没增量到「重算多少」：链路重算在趋势段接近逐根触发（最后一笔延伸每根推进），而每次重算内部
仍是全量扫描，扫描成本随窗口长度线性膨胀。

### 1.2 函数级热点（35 天窗口 cProfile，442s profiled / 256s raw）

| 占比 | 函数 | 调用量 | 问题 |
|---|---|---|---|
| ~19% | `sr_flip.countBarsPassing` | 40 万次 | 每个支阻位对全周期K线逐根数「经过根数」 |
| ~45%(cum) | `findSellPoints`+`findBuyPoints` | 4.3 万次 | compute_srflip（fib 分割位）与 compute_plan 每次重算都全量重跑；内部 2买/2卖 区间套是「上级笔×全部笔」双层循环 |
| ~12% | `isSameAsUpperBi`+`_findIndex` | 330万/100万次 | 被 find* 的循环放大；`_findIndex` 等值线性查找 |
| ~11% | `biMacdMetrics`+`hasMacdCrossBetween` | 350万/110万次 | 每次背驰判断从头线性扫 MACD 数组（数组本身按时间有序） |

注：P1（MACD 二分化）在 21 天窗口实测 3.2×，但**全量尺度几乎无感**（20.7 分钟 → 20.7 分钟）
——短窗口时 MACD 扫描占比高，全量时其余 O(n²) 项（countBarsPassing/find* 双层循环）主导。
教训：**优化前必须在与目标场景同尺度的窗口上 profile**。

## 2. 已实施优化（全部「输出逐位不变」）

### P1 `biMacdMetrics` / `hasMacdCrossBetween` 二分定位窗口（chan_core.py）

- MACD 数组按时间升序（calcMACD 与 MacdAccumulator 输出均为追加式有序），
  `bisect_left/right` 定位 `[t0,t1]` **闭区间**窗口后只扫窗口内条目，替代从头线性扫描；
- 窗口语义逐条对齐旧实现（`< t0` continue / `> t1` break）：`bisect_left(t0)` 与
  `bisect_right(t1)` 恰好重现含端点的窗口；空窗口返回 None/False 不变。

### Fix 1 `countBarsPassing` 向量化（sr_flip.py）

- 语义：价位带 `[price-tol, price+tol]` 被 `low <= hiP and high >= loP` 的K线覆盖计数——
  是**价格重叠计数，不能按时间二分**；
- numpy 可用时（可选依赖，缺失自动回退纯循环）：`count_nonzero((lows <= hiP) & (highs >= loP))`，
  IEEE 比较语义与逐根循环完全一致；
- **lows/highs 数组按 bars 列表对象字典缓存**（持强引用防 id 复用 + 长度校验失效 + 条目>16 清空）。
  踩过的坑：单槽缓存在多周期交替调用下每次都 miss（等于没缓存），必须按对象各自缓存。

### Fix 3 二分 key 回调消除（chan_core.py）

- P1 初版 `bisect(..., key=_macdTime)` 在 350 万次调用下 key 回调本身成为热点（1.5 亿次调用）；
- 改为按 macdArr 对象缓存**平行时间列表**（append-only，长度校验失效），plain bisect 无 key。

### Fix 4 `findBuyPoints` / `findSellPoints` 内部三处（chan_core.py）

1. 2买/2卖 区间套：预计算 down/up 笔端点数组（bis 本身有序），按上级笔时间段
   `bisect` 取窗替代「上级笔 × 全部笔」双层循环——选出的集合与顺序和逐根过滤完全一致；
2. `_findIndex` 等值线性查找 → `endTime → 首次出现下标` 字典（保持首个匹配语义）；
3. `isSameAsUpperBi` 的上级笔按类型分组传入（该函数内部本来就跳过异类型笔）。

**不能做的「共享」**：compute_srflip（fib）与 compute_plan 的 find* 输入不同（前者全量 bis、
后者截断 [-60:]），共享结果会改变计划输出——只能各自内部提速。

## 3. 验证协议（每项优化三道关，全部通过）

1. **单元级新旧对照**：从 `git show HEAD` 取旧实现同进程加载，真实数据 + 随机窗口直接比对
   （countBarsPassing 800 组含多周期交替、biMacdMetrics 2000 组含缓存路径与追加失效、
   find* 12 组多窗口/多 frac，含空窗/单根/越界边界）；
2. **全链路 A/B**：同缓存数据、固定随机环境，改前/改后 stats + 全部成交明细（含出场事件）
   JSON 逐位比对一致；
3. **全量一致**：2 个月全量信号 90 / 成交 65 与优化前完全相同。

## 4. 实测结果（XAUUSD，anchor/realtime，无 marks）

| 窗口 | 优化前 | 优化后 | 提速 |
|---|---|---|---|
| 7 天 | 12.4s | 5.9s | 2.1× |
| 21 天 | 283s | 88.4s | 3.2× |
| 35 天 | 256.3s | **163.2s** | 1.57× |
| 2 个月全量 | 20.7 分钟 | **12.0 分钟** | **1.7×** |

每步 60.1ms → 34.7ms。短窗口（实时监控/回放的每步延迟）收益更直接。

## 5. 未实施路线图（按预期收益排序，风险递增；全部需过 §3 验证协议）

1. **`_rebuild_chain` 切片拷贝消除**（backtest.py）：每次重算 `bars[res][:_cut]` 整体拷贝
   ~3.5 万元素 × 近逐根重算 → 改传 (list, cut) 视图语义或仅对变化周期重切；
2. **`buildBi` 尾部增量重建**：分型一变即从零重建全部笔（O(n²) 主项）。**一致性风险最高**
   （须与 JS 图表算法逐位对齐，且与引擎现有 `_resync_bis` 重同步机制协同设计），
   建议单独开 SPEC、确认笔前缀冻结语义后实施；
3. **`detectFlip` 突破扫描二分**（sr_flip.py）：从头扫 bars 找 lastTouch 之后的突破 →
   bisect 定位起点再扫；
4. **realtime 模式 `zsExitWeak` 结果缓存**（mark_entry.py）：`buildZSByUpper` 每根
   O(B_下级×B_上级)，按周期缓存、笔变失效（不改 [-60:] 截断口径）。

## 6. 测量方法备忘（复现要点）

- **`--use-cache` 忽略 `--from`**：`load_cached()` 返回全量缓存，CLI 的 from 过滤只作用于
  CDP 取数——离线测小窗口必须在内存里自行过滤（`load_cached()` 后按时间裁剪再建引擎）；
- 计时基准跑与 profile 跑分开（cProfile 约 1.7× 开销）；多进程并行跑会互相抢 CPU，计时失真；
- bars_all_tf.json 被 TV 历史深度上限约束（3m ≈ 2 万根），2 个月「全量」实际从 7-6 起。

## 7. 第二批：历史回测专用加速（2026-09-09）

### 7.1 当前基线与扩大采样

本批正式等价验证以 `f2962fe`（出场阶梯重构后）为基线，使用当前默认 `cluster+boll`（fib 关闭）、
`anchor/realtime`、无 marks。当前出场规则已经变化，不能沿用 §4 的历史成交数量。
初次采样缓存各周期根数：D=49、240=297、60=1136、15=4541、3=21854；Python 3.13.15、NumPy 2.5.2。
缓存 SHA256：`c7c8f26c87a53b86980d6631b20c9b35cf5b0ab0d0fc0ca62f8ca776bbc4ab21`。

在重同步边界恢复前缀后，各推进 1500 根，包含一次周期重同步；纯计时与 profile 分开、串行运行。
采样包含状态推进、链路重算和 realtime 信号收集，不含此前信号历史、成交管理和取数。

| 起点（已有3m根数） | 1500根耗时 | 每根耗时 | 链路重算次数 |
|---|---|---|---|
| 2000 | 3.48s | 2.32ms | 810 |
| 10000 | 15.22s | 10.15ms | 823 |
| 20000 | 35.42s | 23.61ms | 803 |

后段耗时约为前段的 10.2 倍，重算次数接近；主要是每次计算成本随历史增长。
后段 profile 累计占比：成笔与极值修正约40%、确认式进场约18%、detectFlip约16%、
经过根数统计约10.5%（其中价格数组准备约90%）。这些比例用于定位，不与无profile计时混用。

### 7.2 已实施

1. 仅 `run()` 的 realtime 分支跳过 `compute_entries`，继续由 `_collect_realtime` 产信号。
   `_rebuild_chain` 默认仍计算确认式进场，保留 confirm、step_to、实时监控与回放路径。
2. `detectFlip` 接受可选时间索引，以 `bisect_right(lastTouch, hi=len(bars))` 定位扫描起点。
   保持严格晚于最后触及、首个有效突破的语义；二分和扫描均不越过可见前缀。
3. `run()` 每周期预建一次 lows/highs，仅向支阻函数传 `[:cut]` 视图，cluster/fib 共同使用。
   数组为本次运行局部变量，不复用于实时追加/覆盖/回退；无 NumPy 时仍走原逐根循环。

没有修改成笔、阈值、浮点求和顺序、重同步频率或历史窗口长度。
未实现切片视图、中枢缓存和增量成笔；后续需根据新热点另行评估。

### 7.3 验证

- 现有133项单元测试与3项临时边界测试通过：含重复时间、未来索引边界、512根向量化阈值、
  NaN比较、NumPy缺失回退，以及默认调用保留确认式进场行为。
- 同一后段1500根 A/B：34.45s → 19.02s（1.81×，耗时下降44.8%）；
  信号、统计、最终笔、支阻、计划与重算次数序列化摘要相同。
- 临时验证脚本按项目约定保存在系统 TEMP；报告写入 `.cache/backtest-perf/`，不修改输入缓存。
- 正式全量采用独立固定快照（原共享缓存会被其他操作更新）：来自 `f2962fe:bars_all_tf.json`，
  SHA256=`d8833b13359ca8499007858e02cd214ee8cd1359d130a0ed430234a39518b856`。
  本次使用默认五周期 D/240/60/15/3，根数48/291/1113/4449/21394（快照中的30S未启用）。
- **全量单次串行 A/B：227.24s → 145.69s，1.56×，耗时下降35.9%。**
  预热60根后21334步，62信号、50成交；完整结果、信号/成交/出场/过滤回调序列与最终笔状态
  JSON摘要完全相同：`824176d4bda9e9de5fcb23b899e01017a379a4f58040ad2dc2c92f43b653d930`。
  计时包含预热与收尾、不含取数；报告为 `.cache/backtest-perf/full-frozen.json`。
- 短窗口额外验证 confirm/anchor + cluster/fib/boll + marks，以及 realtime/confirm + fib/boll + marks，
  完整业务结果和事件序列均一致。报告为 `.cache/backtest-perf/compatibility.json`。
- 上述为本机本次观测值，不与§4旧版12分钟基准直接比较；成笔全量重建仍存在，尚未消除历史增长带来的成本增加。

## 8. 第三批：成笔内部等价优化（2026-09-09）

### 8.1 定位与实现

第二批完成后，在同一固定快照第19000根起采样1500根：profile约35.39s，
其中成笔与极值修正约22.47s（63%），跳空检查187万次、约5.58s。

- **单次成笔内复用跳空判定**：首次需要时，根据相邻合并K线的 rawHigh/rawLow 与当前ATR阈值，
  建立跳空次数前缀；原 `[aIdx,bIdx)` 扫描改为前缀差查询。保留两个方向的 `>=` 判定，
  不用浮点前缀和，不跨调用缓存，ATR变化、包含合并修改及重同步后都会重新计算。
- **MACD成笔条件短路**：只有合并索引间隔恰为3时才调用变色检测；只有真正进入MACD成笔分支时
  才计算原始K线根数。最终成笔条件、端点标记与回溯规则不变。
- 没有实现笔前缀冻结或尾部增量重建；历史增长导致的全量成笔成本仍存在。

### 8.2 验证与收益

- 与修改前核心逐项对照344组：真实六周期、多窗口、随机跳空、四种跳空阈值、锁定端点、
  近等双顶/底开关。输出及输入分型上的标记副作用均完全相同。
- 另验18组精确阈值/上下跳空/ATR变化边界，及133项现有单元测试，全部通过。
- 同轮串行后段1500根：17.41s → 15.95s（耗时下降8.4%），信号、统计、笔、支阻、计划摘要一致；
  `.cache/backtest-perf/third-sample.json` 保存结果。
- 固定21394根快照全量：**126.60s**；上一轮145.69s，进一步下降13.1%；相比最初227.24s累计下降44.3%。
  全量数字是各轮单次计时，不是多次中位数。
- 62信号、50成交、12过滤；完整结果、全部回调事件和最终笔状态摘要仍为
  `824176d4bda9e9de5fcb23b899e01017a379a4f58040ad2dc2c92f43b653d930`。
  数据SHA256与§7正式全量一致，报告为 `.cache/backtest-perf/third-full.json`。

## 9. 第四批：链路重算减负 + 全周期 BiInc（2026-09-16）

### 9.1 定位（优化前）

当前缓存（D/240/60/15/3 = 73/441/1690/6758/20605；3m 跨度短于其它周期，
fine_res 实际为 15m）。dump_baseline realtime 全量 **98.5s**（7 信号/7 成交）。

后段 1500 根 profile：_advance_cut/buildBi 约 60s，_rebuild_chain 约 37s；
biStep 454 万次。

### 9.2 已实施（输出逐位不变）

1. **_prefix_bars**：cut 增长时原地 extend，消除 _rebuild_chain 每次 bars[:cut] 整表拷贝。
2. **countBarsPassing 前缀累加**：同 (bars, price, tol) 在 cut 只增时 = 旧计数 + 新增段。
3. **zsExitWeak/buildZSByUpper 笔快照缓存**：末笔延伸即失效。
4. **BiIncBuilder 全周期启用**（原仅 30S）：
   - nearDouble / lowerContext 与批量 buildBi 同参；
   - 跳空差值表用当前 ATR 判定；任一已固化对布尔翻转 → 阶段二从 0 重放；
   - lowerContext 按内容指纹比较（避免切片新对象误触发全量重建）。

### 9.3 验证与收益

- realtime 全量 A/B：summary/stats/signals/trades JSON **逐位一致**
  （摘要哈希 signals=`fa51b1eae5e5e97b` trades=`3526d967844d13a5`）；
  耗时 **98.5s → 81.4s（约 1.21×，下降 17%）**。
- confirm 在优化后自洽（32 信号/30 成交，前后两次 85s 级一致）。
- 单测：test_sr_flip / test_chan_core_rules / test_near_double /
  test_exit_rules / test_mark_entry_sink / test_bt_runs 全部通过。
- engine_consistency 中间检查点差异与改前同量级（wick/增量漂移既有现象），
  末端重同步点 OK；未引入新回归。
- 后段 1500 根 profile：99.8s → 82.9s；biStep 454 万 → 268 万；
  _advance_cut 61.7s → 42.7s。链路重算（支阻/计划）仍是剩余主项。

报告目录：`.cache/backtest-perf/before|after|after2|profile-*.txt`。

## 10. 第五批：按周期缓存 + BiInc 回溯收紧（2026-09-16）

### 10.1 已实施

1. **compute_srflip / compute_plan 按周期 work_cache**：cut/ATR/笔指纹未变的高周期直接复用 cluster/fib 与计划行；BOLL 仍每拍重算（依赖现价）。
2. **evaluateRealtimeEntries**：同一 (periodX, strategyKey, segStart) 已在 fired 中则跳过背驰链重算。
3. **BiIncBuilder**：ATR 跳空翻转只回溯到首个受影响 seq；lowerContext 指纹去掉 cutoff，长度变化只尾部重放。

### 10.2 验证

- realtime 相对最初基线：98.5s → **67.5s（1.46×）**，signals/trades JSON 逐位一致。
- confirm：32 信号/30 成交与上一批优化结果一致（约 75s）。
- 单测 test_sr_flip / test_near_double / test_mark_entry_sink 通过。

### 10.3 收尾修复（同日，两处正确性隐患）

1. **`_resync_bis` 清空 `_chain_work_cache`**：批量重建可能修正中段笔而 `_bis_fingerprint`
   （len+末笔端点）不变，sr/plan 按周期复用会拿到漂移期旧结果，破坏「重同步点输出严格
   等于 batch(前缀)」的既有保证。每 200 根清一次，代价可忽略；`_rewind_res`（实时整周期
   重放）经 `_resync_bis` 同样覆盖。
2. **`_invalidate_prefix`**：实时 bar 被覆盖（`bl[-1]=dict(b)` 整槽替换）或 `_rewind_res`
   重放后，bars 前缀缓存仍持旧 dict 引用且 cut 数值不变——新增失效方法（列表与计数器
   同时重置）并在 `append_bars` override 分支与 `_rewind_res` 开头调用。

验证：新增 `py_chain/test_backtest_perf.py`（重同步清缓存并可重填 / 覆盖与重放后前缀
反映新值）；用户真实场景全量 A/B（store 源 + lead_days=60 + ZZ+HJ 预设 fib 开、
33430 根 3m、13892 步）：stats/signals/trades/lastTime/最终笔数 **逐位一致**
（`.cache/backtest-perf/batch6-ab/prefix|postfix-lead60.json`）；逐模块单测
（test_sr_flip/test_chan_core_rules/test_near_double/test_exit_rules/
test_mark_entry_sink/test_start_ts/test_divergence_fallback）全部通过。

### 10.4 用户真实场景基线（第六批定位用，同日）

Web 方案库最近方案实测（OANDA:XAUUSD、store 源、from=2026-08-02、lead_days=60、
ZZ+HJ 预设=cluster+fib、realtime）：**810.5~928.9s（≈14~15.5 分钟）**，其中取数仅
0.06s、预热 27~34s、交易段 783~895s（13892 步 × ~60ms/步）——纯计算瓶颈。
与 §9/§10 的 67.5s 实测不可比：该口径 fine=15m（仅 ~6700 步）且 fib 关闭，
比用户场景（fine=3m、33430 根、fib 开）轻一个量级——这是「修了仍慢」的根因。

## 11. 第六批：用户场景口径（fib 开、fine=3m）定位与优化（2026-09-16）

### 11.1 定位（末 3000 根 cProfile，`batch6-profile.txt`）

rebuilds=1643/3000 根（~55% 步触发链路重算）；cumulative 上 `compute_srflip`
占 86%，其中 `findSellPoints` 265s + `findBuyPoints` 120s；内部 `isSameAsUpperBi`
336 万次调用（tottime 93.7s），其时间容差比较引发 **9.02 亿次 `abs`**（66.2s）。
fine/15m 周期末笔每根延伸 → 笔指纹每拍变 → §10 的按周期 work_cache 每拍 miss →
fib 每次重算全量扫描。注意：profile 占比对调用密集型代码有放大（cProfile 逐调用
插桩），原始耗时占比低于此——全量实测收益（§11.3）小于 profile 暗示的比例。

### 11.2 已实施（输出逐位不变）

1. **`isSameAsUpperBi` 时间带二分**（chan_core.py）：命中须 |ΔstartTime| ≤ tEps
   （本级 1 根 bar），同型上级笔 startTime 严格递增 → 带外必不匹配；带内按原列表
   顺序扫描，首个通过全部条件者与原全量线性扫描完全一致。startTime 平行列按
   upperBis 对象缓存（持强引用防 id 复用 + 长度/末元素校验，Fix 3 同款）。
2. **`trend_state_of` 挂 work_cache**（trading_plan.py + backtest.py）：参考周期/
   上级笔指纹与 bars/MACD 长度未变时复用（两次参考周期收盘间全命中）；重同步清空
   已由 §10.3 覆盖。
3. **BOLL/人工位尾切片**（sr_flip.py）：`calcBOLL`/`buildBollCandidates`/
   `buildManualCandidates` 免 `bars[:-1]` O(n) 整表拷贝，只取末 length+1 根
   （同元素同序求和，浮点逐位不变；length<1 保留原路径防 -0 切片语义漂移）。

### 11.3 试作回退：resync×BiInc「相等即换绑」（adopt）——不成立

曾实施：`_resync_bis` 批量重建后若 (fractals, merged, bis) 与增量旧值深度相等则
`BiIncBuilder.adopt_if_equal` 换绑续用（免下次分型变化全量重建）。全量 A/B 出现
**1 个多出的信号**（15m waitSell 9-9 15:45；`final_bis_len` 仍一致——末端重同步
收敛掩盖了中途偏离）。根因：**输出相等不足以证明增量构建器内部栈状态（heads/seq）
与批量重建一致**——engine_consistency 既有的中途漂移正是靠每 200 根的重同步归零，
adopt 在输出碰巧相同时跳过归零，漂移跨重同步点存活导致后续 bis 偏离。合成数据
无漂移，白盒等价测试未能捕获。已整体回退；命中率探针（D 111/111、240 141/151、
60 51/165、15 79/167、3 2/167）显示收益本就集中于低成本周期，弃之不可惜。

### 11.4 验证与收益

- 第一道关：`isSameAsUpperBi` 新旧 3 万组随机对照（含容差带边界/重复起点/空表，
  命中 11895）；calcBOLL/buildBollCandidates/buildManualCandidates 4000 组行为
  一致（含 45 组双方同崩病态组合）；trend_state_of 引擎推进 86 拍三路一致
  （旧实现/新无缓存/新带缓存，缓存命中 75）。
- 全量 A/B（§10.4 场景）：**stats/signals/trades/lastTime/最终笔数逐位一致**
  （`batch6-ab/batch6b-lead60.json` vs `prefix-lead60.json`）；
  耗时 810.5s（基线，同码复跑 863.1s，单次计时离散约 ±6%）→ **768.2s（~8-11%）**。
  注：预热 42.2s 异于常值属机器噪声。
- 优化后短窗 profile（`batch6-profile-after.txt`，末 1500 根、更深位置）：
  `isSameAsUpperBi` tottime 93.7s→2.0s（单次 28µs→1.2µs，~23×）、`abs` 9.02 亿→
  2.12 亿次；profile 段每根 148.0→117.6ms（-21%）。
- 单测：test_chan_core_rules / test_sr_flip / test_near_double / test_marks /
  test_same_bi_mark / test_start_ts / test_backtest_perf / test_divergence_fallback
  全部通过。

### 11.5 未实施路线图（第六批后剩余热点，按 profile 数据排序）

1. `findSellPoints` 自身循环体（优化后 tottime 仍 ~45ms/bar·profiled）：一卖/二卖
   候选对全笔线性遍历，可按上级笔时间段 bisect 取窗（Fix 4 已做 2 买侧）；
2. `biMacdMetrics`/`isBiDiverge`（23+26s·profiled）：背驰判定的 MACD 窗口查询
   量 inherent（第三批已二分化），需按 (bi对) 记忆化或增量维护才能再降；
3. `anchorFirstSell`（18.5s·profiled）：上级笔端点线性扫，右锚点沿笔列表单调 →
   可二分；`_findIndex` 类2买/类2卖去重（11.8s·profiled）可换 (type,time) 集合；
4. 结构性：fib 对 fine 周期每拍全量重算（末笔延伸即指纹变）——如需再量级提速，
   须做 find* 的笔前缀冻结/尾部增量（一致性风险最高，须单独 SPEC）。
