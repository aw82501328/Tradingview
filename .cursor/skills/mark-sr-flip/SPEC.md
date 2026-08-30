# 支阻位标记功能规格（mark-sr-flip）

> 文档用途：作为 mark-sr-flip 脚本的**规格说明（spec）**，描述输入、处理规则、判定逻辑、输出与边界情况，使实现与使用有唯一共识。
> 对应脚本：`.cursor/skills/mark-sr-flip/scripts/mark_sr_flip.js`（2026-08-29 评分改造后版本）。

## 1. 输入

| 项 | 来源 | 说明 |
|----|------|------|
| 笔数据 | `.cursor/cache/bis_<品种>.json`（chan-bi 画笔落盘） | 各周期笔列表，含 `startPrice`/`endPrice`/`startTime`/`endTime`/`type`；缺文件或品种不符则报错退出 |
| K 线 | 从 TradingView 图表实时读取（`--from` 起 + 30 根缓冲） | 用于 ATR 计算、突破判定、经过 K 线数统计 |
| 当前价 | 最小有数据周期最后一根 K 线收盘价 | 优先级：3 > 15 > 60 > 240 > D |
| 参数 | 命令行参数 | `--from` 必填；其余可选 |

### 参数表

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--from=YYYY-MM-DD` | 无（必填） | 起始日期，与画笔 chan-bi 一致 |
| `--periods` | `D,240,60,15,3` | 要标记的周期列表 |
| `--cluster` | `0.5` | 强支阻位聚类容差（×ATR） |
| `--merge` | `0.5` | 跨周期合并容差（×最小周期ATR） |
| `--recent-cluster` | `1.0` | 近期极值位聚类容差（×ATR） |
| `--min-touch` | 按级别（D/240/60=4，15=3，3=8） | 强支阻位最少触及次数；显式指定则全局覆盖 |
| `--max-dist` | `3.0` | 选取距离上限（×本级别ATR） |
| `--max-per-period` | `50` | 每周期候选数量上限（超出按强度评分降序截断；仅约束标记/落盘数据层） |
| `--dry` | 关闭 | 只计算不绘图 |
| `--debug` | 关闭 | 打印调试信息 |

## 2. 候选识别（每个周期独立）

### 2.1 强支阻互换位

1. 从笔列表提取 swing 高低转折点（`extractSwingPoints`）：首尾相连，每笔终点即一次转折，另补第一笔起点；
2. 按价格聚类，相邻价差 ≤ `cluster × ATR` 的点并入同一价位簇（`clusterPoints`）；
3. 簇触及次数 `< minTouch` 则丢弃；
4. 互换判定（`detectFlip`）：
   - **情况 1**：簇内首尾触及角色相反（先高后低 → R2S，先低后高 → S2R），直接判定角色已反转，`breakTime = lastTouch`；
   - **情况 2**：角色未反转，按主导角色（高点≥低点 → 阻力；否则支撑），在末触之后找收盘价有效穿越（`bar.close > price + tol` → R2S；`bar.close < price - tol` → S2R），找到即判定。

### 2.2 近期极值位（`recent`）

- 取最近 `RECENT_BI_COUNT`（默认 20）根笔的 swing 端点（`extractRecentExtremes`）；
- 聚类容差放宽为 `recent-cluster × ATR`（默认 1.0），把同一天密集高低点聚成一条「近期价位区」；
- **不要求触及次数**；簇内同时有高、低点按首尾判定 R2S/S2R，否则记为纯阻力 RES / 纯支撑 SUP；
- 作用：弥补强支阻互换位在最新价格行为上的滞后（如 3 分钟 8-29 高开后 4467 一线形成的阻力）。

### 2.3 候选数量上限（数据层截断）

每周期识别出的全部候选（强支阻互换位 + 近期极值位）最多保留 `--max-per-period`（默认 `50`）个：

- 超出上限时按「强度评分降序」截断，保留 Top N——保留市场测试/停留最多（最重要）的位置，
  丢弃最弱候选（如 3 分钟识别出的海量低分噪音位）；
- 评分沿用 `flipScore`，以该周期全部候选为归一化集（与选取阶段一致）；
- **只约束「标记/落盘」的数据层**（`periods`/`merged`），**不影响默认绘制层**（每个级别仍上下各 1 个）；
- 跨周期合并后的 `merged` 不设上限（各周期最多 50，合并后保留全部，供 `mark-entry` 读取判断靠近支阻位）。

## 3. 强度评分

每个候选（含合并后）计算强度评分，用于「上下各 1 个」的选取：

```
score = 0.6 × norm(touchCount) + 0.4 × norm(barsPassed)
```

- `touchCount`（权重 60%）：价位被 swing 端点命中的次数；
- `barsPassed`（权重 40%）：价位带 `price ± 聚类容差` 被多少根 K 线覆盖/穿越（含影线，`low ≤ price + tol && high ≥ price - tol`）；
- `norm()` = 同一级别候选集内 min-max 归一化，消除量纲差异；
- 权重常量：`TOUCH_WEIGHT = 0.6`、`BARS_WEIGHT = 0.4`。

## 4. 跨周期合并

- 合并容差：`merge × 最小有数据周期 ATR`（默认 `0.5 × minAtr`）；
- 价差 ≤ 容差的候选合并：
  - 价格按触及次数加权平均；
  - `touchCount`、`barsPassed` 累加；
  - `sources` 记录来源周期；
  - `firstTouch` 取更早、`breakTime` 取更晚；
  - 类型冲突时以触及次数更多者为准；
- `level`（主要来源级别）= 来源中**最大的级别**（大级别支阻位更重要，决定颜色与可见范围，不被小级别「淹没」）。

## 5. 选取与绘制

### 5.1 选取（`pickByLevel`）

每级别只保留「当前价格上方 1 个 + 下方 1 个」（`SIDE_COUNT = 1`）：

1. **距离范围限制**：仅保留距当前价 ≤ `max-dist × 本级别ATR`（默认 `3.0 × ATR`）的候选，避免远古强位（触及/经过K线多但离现价远）挤掉当前价附近支阻位；
2. **同侧选评分最高**：同一侧仍多个候选时，按 `score` 降序取前 1 个（而非纯距离最近）。

### 5.2 绘制

- 颜色按 `level` 映射（与 chan-bi 笔颜色一致）：

  | 级别 | 颜色 |
  |------|------|
  | 日线 D | 红 `#F23645` |
  | 4小时 240 | 蓝 `#2962FF` |
  | 1小时 60 | 黄 `#FFD700` |
  | 15分钟 15 | 紫 `#8A2BE2` |
  | 3分钟 3 | 青蓝 `#00BCD4` |

- 可见范围：某级别的位置只显示在该级别及以下级别（`intervalsVisibilities` 连续范围）：
  - D → 全可见；240 → 4h 及以下；60 → 1h 及以下；15 → 15m 及以下；3 → 仅 3m；
- 形状：`horizontal_line`，title = `SR_FLIP_<级别>`（如 `SR_FLIP_60`）；
- 绘制前先清除所有 title 前缀为 `SR_FLIP` 的旧横线；
- 绘制后切回原周期。

## 6. 输出

1. **图上绘制**：按上述规则的 `horizontal_line`；
2. **缓存落盘** `.cursor/cache/srflip_<品种>.json`：
   - `periods`：各周期原始候选（含 `price`/`type`/`breakTime`/`touchCount`/`barsPassed`/`firstTouch`/`lastTouch`/`recent`；**每周期最多 `maxPerPeriod` 个**）；
   - `merged`：跨周期合并结果（含 `sources`/`level`）；
   - `drawn`：最终绘制列表（含 `level`/`score`）；
   - 元信息：`from`/`currentPrice`/`minTouch`/`sideCount`/`recentBiCount`/`scoreWeights`/`maxDistAtr`/`maxPerPeriod` 等。

## 7. 边界情况

| 场景 | 处理 |
|------|------|
| 笔数据文件不存在或品种不符 | 报错退出，提示先运行「画笔」 |
| 某周期笔数 < 3 | 跳过该周期 |
| K 线读取失败 | 跳过该周期 |
| 数据未覆盖 `--from` 起始日期 | 自动滚动加载完整历史（`scrollToFirstBar`）重试 |
| 当前价未知 | 不按级别筛选，全部合并结果绘制 |
| 某级别某侧无候选 | 该侧不画（允许上下不对称） |
| 近期极值位与强支阻位同价位 | 跨周期/同级别合并时按价格并簇 |

## 8. 与其他模块的依赖

| 模块 | 关系 |
|------|------|
| `chan-bi`（画笔） | **强制依赖**：读取其落盘笔数据；无笔数据不运行 |
| `chan-core` | 仅复用 `calcATR` 等工具函数 |
| `mark-entry`（进出场） | **读取本脚本落盘 `srflip_<品种>.json` 的 `merged` 字段**判断「靠近支阻位」；本脚本字段变更需保持 `merged` 结构兼容 |
