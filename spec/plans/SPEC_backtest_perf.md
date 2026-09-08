# 回测性能优化：链路函数内部热点逐位等价优化（SPEC_backtest_perf）

> 状态：**第一批已实施并验证**（2026-09-07~09-08，详见 §4 实测结果）；§5 路线图为待实施项。
> 日期：2026-09-08
> 范围：`py_chain/chan_core.py`、`py_chain/sr_flip.py`（全部为「输出逐位不变」的纯性能优化，不改任何计算口径/阈值/行为）；`py_chain/SPEC.md` §5.1/§5.2 为同步记录。

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
