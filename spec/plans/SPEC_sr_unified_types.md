# 支阻位体系改造方案：三类型可配置 + 统一管线 + 按周期显示（SPEC_sr_unified_types）

> 状态：**待确认**（用户确认后实施；实施完成后本文件移入 spec/results 并补充实测结果）
> 日期：2026-09-08
> 范围：`.cursor/skills/mark-sr-flip/`（JS 实盘标记）与 `py_chain/sr_flip.py`（Python 回测）双端同步；下游 `mark-entry` 只读 `merged.price`，消费口径不变。

## 背景

支阻位原为「密集区」（强支阻互换位 + 近期极值位）单来源，后新增黄金分割（fib，非一类买卖点参照笔回撤 + pending 预期回退）走「并行双轨」（豁免截断、不合并、全部独立绘制、橙色虚线）。本轮按用户规则统一与扩展：

1. 三类来源（密集区 / 黄金分割 / BOLL）全部可配置，默认密集区 + BOLL（fib 保留默认关）；
2. BOLL 与黄金分割合并逻辑一致——三类同池合并，都属于「位置线」；
3. 显示模型改为「按周期」：每个周期图最多 4 条（可配置），候选池含该级别及以上级别的继承线；
4. 所有线统一灰色 + 来源标注文字。

## 一、三类来源（`--sr-types` 可配置，默认 `cluster,boll`）

| 类型 | 开关名 | 默认 | 生成逻辑 | 每周期数量 |
|------|--------|------|----------|-----------|
| 密集区 | `cluster` | ✅ 开 | 强支阻互换位（触及 ≥ minTouch 次 + 角色互换 R2S/S2R）+ 近期极值位（最近 20 笔端点、宽容差聚类、无触及要求）——**原逻辑不变** | 不定（当前 3m ≈17 条） |
| 黄金分割 | `fib` | ❌ 关 | 每方向**最新非一类买卖点**× 0.382/0.5/0.618 参照笔回撤；无已形成点时 pending 预期回退（逻辑保留，仅默认关闭） | ≤6 |
| BOLL 布林带 | `boll` | ✅ 开 | **每周期最后一根已收盘K线**的布林带上/中/下轨：26 周期 SMA ± 2σ（`--boll-length/--boll-mult` 可调），**不含形成中当根**；不区分买卖点、无 pending 概念 | 3 |

BOLL 细则：

- 上轨 = 阻力 `RES`、下轨 = 支撑 `SUP`、**中轨按现价侧**（现价 ≥ 中轨 → 支撑，否则 → 阻力）；
- 已收盘口径：剔除取数最后一根（形成中）K线后取末 `length` 根收盘价，总体标准差（与 TradingView 布林带同口径）；
- bars < 26 的周期无布林位（正常降级，如当前日线窗口 13 根）；
- 带宽值随每次重跑的最新已收盘K线刷新。

## 二、统一管线（三类同池同规则）

1. **每周期截断**：密集区按评分截到 50 条/周期（`--max-per-period`，原样）；**fib 与 boll 豁免截断**（fib touchCount=1 评分必垫底、boll 为当前带宽值，评分语义均不适用；豁免保证进入 merged 供 mark-entry 使用。当前默认参数下密集区 3m ≈17 条 < 50，截断本就不触发，豁免是 min-touch 调低时的保险）；
2. **跨周期合并（同一池、同一规则）**：三类候选全部进 `mergeFlipsAcrossPeriods`——价差 ≤ `0.5×最小周期ATR` 并成一条**位置线**：价格按触及次数加权平均、`sources` 记录全部来源周期、`level` = 最大来源周期、类型冲突按 touchCount；**多来源混合线删掉 fib/pending/boll 标记**（统一按位置线口径），纯单来源 fib/boll 独立线保留标记（打印区分）。

## 三、显示规则（全新模型）

- **每周期图最多 4 条线**（`--side-count=2` 每侧条数，总 = 2×side-count）：对每个显示周期 L，候选池 = **该级别及以上全部级别的合并线**（高级别线继承到低周期图，如 3m 图的候选池含 3/15/60/240/D 全部位置线），取「距现价最近的上方 2 条 + 下方 2 条」；每条线仍受 `≤3×线自身级别ATR` 距离上限（`--max-dist`）约束；允许上下不对称；
  - 实现方式：每个显示周期各画自己的 ≤4 条，线实例可见性 = 仅该周期（同一高级别线可被多个周期各自选中、互不可见不重叠）；
- **所有线统一灰色 `#787B86` 实线**；**每条线带来源标注**：`BOLL上轨+4小时` / `密集区+15分钟` / `黄金分割0.5+1小时` / `预期2卖+240`——标注写入 shape 的 title 与 text（title 必定生效；text 若 TV 该线形不支持则退化为仅 title/悬停可见，实画时验证）；
- 清线前缀统一 `SR_`（一次清除全部旧 SR_FLIP / SR_FIB / 新线）；
- 落盘 `srflip_<品种>.json`：`periods`（各周期原始候选：密集区截断后 + fib + boll）、`merged`（统一合并结果——mark-entry 仍只读 `price`，nearSr/止损参考自动覆盖三类）、`drawnByPeriod`（各显示周期选中的 ≤4 条，含来源标注）；meta 增 `srTypes / fibLevels / bollCfg:{length,mult} / sideCount`。

## 四、参数表（改造后）

| 参数 | 默认 | 说明 |
|------|------|------|
| `--sr-types` | `cluster,boll` | 类型开关（`cluster`/`fib`/`boll` 任意组合，全关报错退出） |
| `--boll-length` | `26` | BOLL SMA 周期（已收盘K线口径） |
| `--boll-mult` | `2` | BOLL 标准差倍数 |
| `--side-count` | `2` | 每周期图每侧条数（2 → 每图最多 4 条） |
| `--fib-levels` | `0.382,0.5,0.618` | 黄金分割比率（仅 fib 开启时生效） |
| `--from / --periods / --cluster / --merge / --recent-cluster / --min-touch / --max-dist / --max-per-period / --dry / --debug` | 原值 | 不变 |

## 五、实施清单（确认后执行）

- **JS `mark_sr_flip.js`**：参数区（三类型合法名、BOLL_LENGTH/BOLL_MULT、SIDE_COUNT=`--side-count` 默认 2、删 FIB_COLOR）；新增 `calcBOLL(bars, len, mult)`（剔除末根形成中K线、总体σ）与 `buildBollCandidates`；main 循环 boll 块；统一合并池 + 多来源标记清理；**选取重写为按显示周期**（`pickNearestForDisplay(merged, displayPeriods, currentPrice, sideCount, maxDistAtr, periodAtrs)`：池 = level≥L，就近上下各 N，新函数替代 pickByLevel 的调用位）；绘制统一灰线 + title/text 来源标注 + 可见性仅该周期 + 清线前缀 `SR_`；落盘/头部注释/module.exports 同步；
- **JS 测试**：`calcBOLL`（常数列三轨合一、手工数列、bars 不足→null）、`buildBollCandidates`（三轨类型/中轨按现价侧/字段）、`pickNearestForDisplay`（继承池/就近上下各 N/距离上限/不对称）、typeNameOf(boll) 标签；**fib/pending 用例零改动**（fib 逻辑未动的回归证明）；
- **py `sr_flip.py`**（编辑前重读，保留外部 numpy 向量化与出场状态机改动）：常量/函数镜像；`compute_srflip` 签名增 `bollLength/bollMult`，统一池 + 标记清理 + 新选取（`drawn` 改为 `drawnByPeriod` 口径，py 不绘图仅数据对齐）；`backtest.py`（先重读）与 `main.py` 透传 `boll_length/boll_mult`（CLI `--boll-length/--boll-mult`）；
- **py 测试**：镜像用例 + `compute_srflip` 集成（≥28 根夹具分离三轨、`periodAtrsIn` 控合并容差，断言 boll 进 merged、`drawnByPeriod` 键、无 `drawnFib`）；
- **文档**：mark-sr-flip SPEC/SKILL（三类来源/新显示模型/灰线标注/参数表/边界）、py_chain SPEC §2.2、mark-entry SPEC 支阻位数据行（merged 三类来源一句）。

## 六、验证

1. 单测：JS `node --test mark_sr_flip.test.js` / py `python -m unittest py_chain.test_sr_flip` 全绿（fib 用例不动）；
2. 真实数据（bars_all_tf.json）：boll 三轨与手算一致（末根已收盘口径）；fib 27 条保留进池；JS↔py 同 bis/bars 输出逐条一致（$TEMP 对照脚本）；
3. 实盘重标 `--from=2026-08-20`：切各周期确认**每图 ≤4 条灰线**、高级别线可继承到低周期图（如 3m 图可见 240 来源线）、来源标注可见（或 title 可见）、旧 SR_FLIP/SR_FIB 全清、无重复；
4. 回测冒烟跑通（boll 进 merged 无异常）；`--sr-types=cluster` 可复现纯密集区旧行为。
