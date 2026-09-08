---
name: mark-sr-flip
description: Mark support/resistance flip levels (支阻互换位) on the TradingView Desktop chart via CDP using gray horizontal lines. Use when the user asks to 标记支阻互换位, 标记支撑阻力, 画支撑阻力线, or mark key S/R flip zones on the chart.
disable-model-invocation: true
---

# 支阻互换位标记

通过 CDP 连接 TradingView Desktop，读取 **`chan-bi`（画笔）SKILL 落盘的笔数据**，识别各周期重要的**支阻位**（密集区 + 黄金分割 + BOLL，`--sr-types` 可分别开关，默认 `cluster,boll`），并在图上标记。

> **算法来源**：密集区（强支阻互换位/近期极值位）是独立技术分析概念（非缠论算法），识别逻辑在本脚本 `scripts/mark_sr_flip.js` 内实现；黄金分割位基于**非一类买卖点**的参照笔回撤，需现算买卖点（复用 `chan-core` 的 `findBuyPoints`/`findSellPoints`/`calcMACD`）；BOLL 布林带取每周期最后一根已收盘K线的 26 周期 SMA ± 2σ；另复用 `calcATR` 等工具函数。
>
> **强制依赖画笔数据**：本 SKILL **不计算笔**，**强制读取 `chan-bi` 画笔 SKILL 落盘的笔数据文件**（`.cursor/cache/bis_<品种>.json`）。如果文件不存在、或文件品种与当前图表品种不一致，脚本会**报错退出**，必须先对当前品种运行「画笔」（chan-bi）后再标记。

## 支阻位定义

支阻位来自三类来源（`--sr-types=cluster,boll` 可分别开关，`cluster`/`fib`/`boll` 任意组合）：

### 密集区（cluster，价格聚类）

**1. 强支阻互换位**（反复测试 + 角色互换）：
1. **反复测试**：该价位被价格多次测试（≥ `--min-touch` 次）形成支撑或阻力；
2. **突破**：之后价格突破该价位；
3. **角色互换**：
   - **阻力转支撑（R2S）**：曾是阻力（多次受阻回落），向上突破后变为支撑；
   - **支撑转阻力（S2R）**：曾是支撑（多次受撑反弹），向下跌破后变为阻力。

识别逻辑（`detectFlip`）：
- 若价位簇首尾触及角色相反（先高后低 → R2S，先低后高 → S2R），直接判定角色已反转；
- 若角色未反转（全部高点或全部低点），用 K 线收盘价判断突破（阻力向上突破 → R2S，支撑向下跌破 → S2R）。

**2. 近期极值位**（`recent`，新增）：
- 取最近 `RECENT_BI_COUNT`（默认 20）根笔的 swing 端点（高低点）作为候选，
  弥补强支阻互换位在最新价格行为上的滞后——刚形成的阻力/支撑（如 3分钟 8-29 高开后
  在 4467 一线形成的阻力）触及次数少、不满足 minTouch，但却是当前最直接的价位参考；
- 聚类后不要求触及次数，簇内同时有高、低点则按首尾判定 R2S/S2R，否则记为
  纯阻力 RES / 纯支撑 SUP；
- 聚类容差用更宽的 `--recent-cluster × ATR`（默认 1.0），把同一天密集高低点
  聚成一条「近期价位区」（如 4460~4467 高点群聚成 4464 一线）。

### 黄金分割（fib，非一类买卖点参照笔回撤）

对每个周期、每个方向（买/卖）**最新的非一类买卖点**（2买/类2买/3买、2卖/类2卖/3卖；
买卖点在本脚本内现算 `chan-core findBuyPoints/findSellPoints`，不落盘），取其回调笔
**紧邻前方的顺势笔**为参照笔，画经典回撤分割位：

- **买点**：参照笔 = 前方上涨笔（低 L→高 H），分割位 = `H - r×(H-L)`（回撤**支撑**）；
- **卖点**：参照笔 = 前方下跌笔（高 H→低 L），分割位 = `L + r×(H-L)`（反弹**阻力**）；
- 比率 `r` 默认 `0.382/0.5/0.618`（`--fib-levels` 可配）。

**口径要点**：

- **每方向只取最新一个非一类点**，最新点匹配不到参照笔则该方向跳过（不回退更早点）；
- **预期回退（pending）**：某方向无已形成非一类点时，用「形成中的回调笔 + 其前方顺势笔」生成**预期黄金分割位**（等待2卖/2买 的预期形成区）——卖向需末笔为形成中上涨笔且现高点 < 前方下跌笔起点（次高点结构未破坏），买向对称；候选带 `pending: true`；预期结构被行情否定时下次重算自动消失；
- **豁免每周期上限截断与评分**（fib 由结构点派生，评分对它无意义，且每周期上界 2×比率数）；
- **参与统一合并池**（见下「统一管线」）：纯单来源 fib 独立线保留 `fib: true`、`ratio`、`fromPoint`、`referBi` 标记；与其它来源并簇的混合线删除这些标记，统一按「位置线」口径；
- 候选带 `fib: true`、`ratio`、`fromPoint`（派生它的买卖点/预期标记）、`referBi`（参照笔）字段。

### BOLL 布林带（boll，末根已收盘K线 ± 2σ）

每周期取**最后一根已收盘K线**的布林带上/中/下轨作为支阻位：

- **上轨 = 阻力 `RES`、下轨 = 支撑 `SUP`、中轨按现价侧**（现价 ≥ 中轨 → 支撑，否则 → 阻力）；
- 口径：剔除取数最后一根（形成中）K线后取末 `--boll-length`（默认 26）根收盘价的 SMA ± `--boll-mult`（默认 2）× **总体标准差（÷N）**，与 TradingView 布林带同口径；
- bars < 26 的周期无布林位（正常降级）；带宽值随每次重跑的最新已收盘K线刷新；
- 不区分买卖点、无 pending 概念；候选带 `boll: "upper"|"mid"|"lower"` 标记。

### 统一管线（三类同池合并）

三类候选（密集区截断后 + fib + boll）全部进**同一个** `mergeFlipsAcrossPeriods` 池，同一规则：

- 价差 ≤ `--merge × 最小周期ATR` 并成一条**位置线**：价格按触及次数加权平均、`sources` 记录全部来源周期、`level` = 最大来源周期、类型冲突按 touchCount；
- **多来源混合线**（如 cluster+fib、cluster+boll 并簇）删除 `fib/pending/boll` 等具体来源标记，统一按「位置线」口径（来源标注文字为 `位置线`）；
- **纯单来源 fib/boll 独立线保留标记**（打印/标注区分，如 `黄金分割0.5+1小时`、`BOLL上轨+4小时`）；
- 密集区按评分截断（`--max-per-period`），fib/boll 豁免（评分语义不适用）。

## 前置条件

1. TradingView Desktop 以调试模式启动：`TradingView.exe --remote-debugging-port=9222`
2. 已打开至少一张图表，且图表停留在**要标记的品种**上（脚本自动读取当前品种和周期）
3. chrome-remote-interface 已安装（在 `server-cdp/node_modules/`）
4. **已对该品种运行过「画笔」**（chan-bi SKILL），生成了 `.cursor/cache/bis_<品种>.json` 笔数据文件

## 使用方式

```bash
# 在图表上标记支阻位（默认 日线/4小时/1小时/15分钟/3分钟 全部级别，密集区+BOLL）
node .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js --from=2026-06-30

# 只计算并打印，不绘图（先验证再标）
node .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js --dry --from=2026-06-30

# 调整聚类阈值（×ATR）、跨周期合并阈值与最少触及次数
node .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js --from=2026-06-30 --cluster=0.5 --merge=0.5 --min-touch=4

# 只标记密集区（行为与黄金分割/BOLL 加入前一致）
node .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js --from=2026-06-30 --sr-types=cluster

# 恢复旧黄金分割行为：密集区 + 黄金分割，且自定义比率
node .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js --from=2026-06-30 --sr-types=cluster,fib --fib-levels=0.382,0.618

# 只标记 BOLL，且自定义周期与倍数
node .cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js --from=2026-06-30 --sr-types=boll --boll-length=20 --boll-mult=2
```

> `--from` 起始日期应与画笔时一致（脚本读取该日期之后的笔数据来计算）。

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--from=YYYY-MM-DD` | **必填**：起始日期，与画笔 chan-bi 一致 | 无（缺少时报错退出） |
| `--periods=...` | 要标记的周期列表（逗号分隔） | `D,240,60,15,3` |
| `--sr-types=...` | 支阻位类型开关（逗号分隔，可选 `cluster`/`fib`/`boll`，全关报错退出） | `cluster,boll` |
| `--fib-levels=...` | 黄金分割比率（逗号分隔，须在 (0,1) 内；仅 fib 开启时生效） | `0.382,0.5,0.618` |
| `--boll-length=N` | BOLL SMA 周期（已收盘K线口径） | `26` |
| `--boll-mult=K` | BOLL 标准差倍数 | `2` |
| `--side-count=N` | 每周期图每侧条数（2 → 每图最多 4 条） | `2` |
| `--cluster=K` | 价位聚类阈值（×ATR，相近价位合并为同一支阻位；仅密集区） | `0.5` |
| `--merge=K` | 跨周期合并阈值（×最小周期ATR，三类同池合并） | `0.5` |
| `--recent-cluster=K` | 近期极值位聚类阈值（×ATR，同一天密集高低点聚成一条） | `1.0` |
| `--min-touch=N` | 最少触及次数（仅强支阻互换位用；可选，不传则按级别：D/240/60=4，15=3，3=8） | 按级别 |
| `--max-dist=K` | 选取时距离上限（×线自身级别ATR） | `3.0` |
| `--max-per-period=N` | 每周期候选数量上限（超出按强度评分降序截断；仅密集区，fib/boll 豁免） | `50` |
| `--dry` | 只计算不绘图 | 关闭 |
| `--debug` | 打印聚类、ATR 等调试信息 | 关闭 |

## 起始日期规则（每次标记必做，且必须输入日期）

**用户说「标记支阻互换位」时，必须先询问「起始日期」（格式 `YYYY-MM-DD`，与画笔一致），得到日期后再执行标记。不得在未获得日期的情况下直接运行脚本。**

```
① 询问：本次标记支阻互换位从哪个日期开始？（格式 YYYY-MM-DD，与画笔一致）
② 用户给出日期 → 运行 node mark_sr_flip.js --from=YYYY-MM-DD
③ 用户未给出明确日期 → 再次询问，直到获得日期后才执行
```

## 支阻位强度评分

识别出的每个支阻位（含跨周期合并后）都会计算一个**强度评分**，用于「上下各 1 个」的选取：

```
score = 0.6 × norm(触及次数) + 0.4 × norm(经过K线数量)
```

- **触及次数**（`touchCount`，权重 60%）：该价位被 swing 端点（笔的转折点）命中的次数；
- **经过 K 线数量**（`barsPassed`，权重 40%）：该价位带 `price ± 聚类容差` 被多少根 K 线覆盖/穿越（含影线，`low ≤ price+tol && high ≥ price-tol`），衡量价格在该价位停留/穿越的时长；
- 两者在**同一级别候选集内 min-max 归一化**到 [0,1] 后再加权，消除量纲差异。

跨周期合并时 `touchCount` 与 `barsPassed` 均累加（同一价位带的强度合并）。

## 显示规则

- **每周期图最多 2×`--side-count` 条线**（默认 `--side-count=2` → 每图最多 4 条）：
  对每个显示周期 L，候选池 = **该级别及以上级别的合并线**（高级别线继承到低周期图，如 3m 图候选池含 3/15/60/240/D 全部位置线），取「距现价最近的上方 sideCount 条 + 下方 sideCount 条」；
- **距离上限**：每条线仍受 `≤ --max-dist × 线自身级别ATR`（默认 `3.0×`）约束，允许上下不对称（一侧不足 N 条时不补）；
- **每条线只在其显示周期可见**：高级别线继承到低周期图时为各周期生成独立线实例（`intervalsVisibilities` 仅该周期），互不重叠；
- **所有线统一灰色 `#787B86` 实线**，来源用 title/text 标注区分：
  - title = `SR_<来源类型>+<周期中文名>`（如 `SR_BOLL上轨+4小时` / `SR_密集区+15分钟` / `SR_黄金分割0.5+1小时` / `SR_预期2卖+240` / `SR_位置线+60`）；
  - text = 来源标注（悬停可见；TV 若该线形不支持 text 则退化为仅 title）；
- **每周期标记上限 50 个**：密集区识别结果（强支阻位 + 近期极值位）每周期最多保留 50 个（`--max-per-period` 可调），
  超出按强度评分降序截断，完整落盘供数据使用；**fib/boll 豁免截断**；
- **清线前缀统一 `SR_`**：再次标记时按前缀 `SR_` 一次清除全部旧横线（兼容历史 `SR_FLIP`/`SR_FIB`）。

## 跨周期合并

不同级别/不同来源可能在同一价位各自识别出位置线（如 1小时 4440.88 与 3分钟 4443.45 实为同一阻力位），
为避免图上画出多条近乎重叠的线，识别完成后会对**三类候选统一**做一次**跨周期合并**：

- 按价格排序，价差 ≤ `--merge × 最小周期ATR`（默认 `0.5 × 最小周期ATR`）的位置线合并为一条；
- 合并后价格按触及次数加权平均、触及次数累加、`sources` 记录来源周期（如 `60+3`）、
  互换时间取更晚者、类型冲突时以触及次数更多者为准；
- **主要来源级别（`level`）= 来源中最大的级别**：大级别识别的支阻位是更长期的价位，
  优先保留其归属，不被触及次数更多的小级别「淹没」；
- **多来源混合线删 fib/pending/boll 标记**（统一按「位置线」口径），纯单来源 fib/boll 独立线保留标记。

## 落盘

每次标记（含 `--dry`）都会把识别结果写入 **`.cursor/cache/srflip_<品种>.json`**：

- `periods`：各周期的原始候选列表（字段：`price` 代表价、`type` 互换类型、`breakTime` 互换时间、`touchCount` 触及次数、`barsPassed` 经过K线数量、`firstTouch`/`lastTouch` 首末触及时间、`recent` 是否近期极值位；**密集区截断后 + fib + boll**，fib 候选额外含 `fib: true`、`ratio`、`fromPoint`、`referBi`，boll 候选含 `boll: "upper"|"mid"|"lower"`）；
- `merged`：三类统一跨周期合并后的完整位置线列表（额外含 `sources` 来源周期、`level` 主要来源级别、`srcType` 来源类型）；下游 `mark-entry` **仍只读取 `price`**（判定「靠近支阻位」与止损参考位），三类来源自动覆盖；
- `drawnByPeriod`：各显示周期选中的 ≤2×`side-count` 条线（含 `label` 来源标注，如 `BOLL上轨+4小时`）；
- 元信息含 `srTypes`（本次开关的类型）、`fibLevels`（本次使用的比率）、`bollCfg:{length,mult}`（BOLL 参数）、`sideCount`。

## 常见问题排查

| 问题 | 可能原因 | 检查命令 |
|------|----------|----------|
| ECONNREFUSED | TradingView 未以调试模式启动 | `netstat -ano \| findstr 9222` |
| 未找到页面 | 图表标签未打开 | 打开一张图表即可 |
| **报错「未找到 XX 的笔数据文件」** | 未先运行「画笔」SKILL（chan-bi） | 先对当前品种运行画笔，再运行本脚本 |
| 某周期无互换位 | 该周期笔数不足（如日线/4小时笔少），或聚类阈值不合适 | 用更早的 `--from` 重新画笔，或调整 `--cluster` |
| 相近价位画了多条线 | 跨周期合并阈值太小 | 调大 `--merge`（如 1.0）让相近支阻位合并 |

## 注意事项

- 脚本自动读取**当前图表**的品种和周期，切换品种后需重新运行。
- **必须先对该品种运行「画笔」**（chan-bi SKILL）生成笔数据文件，本脚本才能标记；否则报错退出。
- 本 SKILL 只负责支阻互换位标记，**不画笔、不清除笔、不清除中枢/买卖点**。
- **数据充分性**：支阻互换位识别需要足够的笔数据（笔端点即 swing 转折点）。若起始日期太近导致某周期笔数不足、识别不出互换位（尤其日线/4小时），建议用更早的 `--from` 重新画笔后再标记。
