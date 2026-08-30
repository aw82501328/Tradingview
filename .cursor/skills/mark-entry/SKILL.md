---
name: mark-entry
description: Mark entry signals (进出场) on the TradingView Desktop chart via CDP using yellow arrows. Use when price approaches a support/resistance flip level AND a Chanlun divergence (背驰) appears on any timeframe (3m/15m/1h/4h). Long = up arrow, short = down arrow.
disable-model-invocation: true
---

# 进出场标记

通过 CDP 连接 TradingView Desktop，当**价格靠近支阻位**、且任一周期（3分钟/15分钟/1小时/4小时）出现**缠论背驰**时，在该周期标记**进场黄色箭头**：

- **底背驰**（下跌笔创新低 + MACD 背驰）→ **做多** → 向上箭头（`arrow_up`）
- **顶背驰**（上涨笔创新高 + MACD 背驰）→ **做空** → 向下箭头（`arrow_down`）

> **算法来源**：背驰判定复用 `chan-core` 的 `isBiDiverge`（唯一算法源），本脚本只负责取数据、组合「背驰 + 靠近支阻位」双条件、绘制箭头与落盘。
>
> **强制依赖两个前置数据**：
> 1. **画笔**（chan-bi）落盘的笔数据 `.cursor/cache/bis_<品种>.json`；
> 2. **支阻互换位**（mark-sr-flip）落盘的支阻位数据 `.cursor/cache/srflip_<品种>.json`（读取其 `merged` 合并后支阻位）。
>
> 任一数据文件缺失、或品种不匹配，脚本会**报错退出**，必须先依次运行「画笔」→「支阻互换位」→ 本脚本。

## 前置条件

1. TradingView Desktop 以调试模式启动：`TradingView.exe --remote-debugging-port=9222`
2. 已打开至少一张图表，且图表停留在**要标记的品种**上（脚本自动读取当前品种和周期）
3. chrome-remote-interface 已安装（在 `server-cdp/node_modules/`）
4. **已运行「画笔」**（chan-bi），生成笔数据文件
5. **已运行「支阻互换位」**（mark-sr-flip），生成支阻位数据文件

## 使用方式

```bash
# 在图表上标记进出场（默认 4小时/1小时/15分钟/3分钟）
node .cursor/skills/mark-entry/scripts/mark_entry.js --from=2026-06-30

# 只计算并打印，不绘图（先验证再标）
node .cursor/skills/mark-entry/scripts/mark_entry.js --dry --from=2026-06-30

# 调整靠近支阻位阈值（×当前周期ATR）
node .cursor/skills/mark-entry/scripts/mark_entry.js --from=2026-06-30 --near=1.0
```

> `--from` 起始日期应与画笔/支阻位时一致。

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--from=YYYY-MM-DD` | **必填**：起始日期，与画笔/支阻位一致 | 无（缺少时报错退出） |
| `--periods=...` | 检测周期列表（逗号分隔，从大到小） | `240,60,15,3` |
| `--near=K` | 靠近支阻位阈值（×当前周期ATR） | `1.0` |
| `--dry` | 只计算不绘图 | 关闭 |
| `--debug` | 打印背驰、ATR 等调试信息 | 关闭 |

## 起始日期规则（每次标记必做，且必须输入日期）

**用户说「标记进出场」时，必须先询问「起始日期」（格式 `YYYY-MM-DD`，与画笔/支阻位一致），得到日期后再执行标记。不得在未获得日期的情况下直接运行脚本。**

```
① 询问：本次标记进出场从哪个日期开始？（格式 YYYY-MM-DD，与画笔/支阻位一致）
② 用户给出日期 → 运行 node mark_entry.js --from=YYYY-MM-DD
③ 用户未给出明确日期 → 再次询问，直到获得日期后才执行
```

## 信号判定逻辑

### 背驰识别（复用 chan-core.isBiDiverge）

遍历各周期笔，参照 `findBuyPoints`/`findSellPoints` 的候选逻辑（但不做区间套/锚定）：

- **做多（底背驰）**：下跌笔**创新低** + MACD 背驰（绿柱面积变小 **或** DIF 低点抬高 **或** 绿柱最大高度变小）；
- **做空（顶背驰）**：上涨笔**创新高** + MACD 背驰（红柱面积变小 **或** DIF 高点变低 **或** 红柱最大高度变小）；
- **参照笔** = 向前最近同向笔（跳过幅度 < 当前笔 50% 的次级别回调）。

### 靠近支阻位判定

背驰点极值价与任一支阻位（`merged` 列表）的价差 ≤ `--near × 当前周期ATR`（默认 `1.0×ATR`）→ 视为「靠近」。

> 不同周期阈值自适应：4小时 ATR 大 → 阈值宽；3分钟 ATR 小 → 阈值严（需更精确贴近支阻位）。

## 显示规则

- 做多 = **向上黄色箭头**（`arrow_up`），做空 = **向下黄色箭头**（`arrow_down`），颜色 `#FFD700`；
- **只在该周期显示**：每个周期的箭头通过 `intervalsVisibilities` 只在本周期显示（如 1小时的箭头只在 1小时图可见，切到其他周期自动隐藏）；
- title 打上源周期标签 `ENTRY_<周期>`，再次标记时按标签只清除本周期旧箭头。

## 落盘

每次标记（含 `--dry`）都会把各周期信号写入 **`.cursor/cache/entry_<品种>.json`**：
字段：`time` 背驰时间、`price` 背驰点价格、`direction`（`long` 做多 / `short` 做空）、`nearSr` 靠近的支阻位价格。

## 常见问题排查

| 问题 | 可能原因 | 检查命令 |
|------|----------|----------|
| ECONNREFUSED | TradingView 未以调试模式启动 | `netstat -ano \| findstr 9222` |
| 未找到页面 | 图表标签未打开 | 打开一张图表即可 |
| **报错「未找到笔数据文件」** | 未先运行「画笔」SKILL（chan-bi） | 先画笔，再运行本脚本 |
| **报错「未找到支阻位数据文件」** | 未先运行「支阻互换位」SKILL（mark-sr-flip） | 先标记支阻位，再运行本脚本 |
| 某周期无信号 | 该周期无背驰，或背驰点未靠近支阻位 | 用 `--debug` 查看背驰数与靠近判定 |
| 信号太多 | 靠近阈值太宽 | 调小 `--near`（如 0.5） |
| **切到小周期后早期箭头错位/漂移** | TradingView 小周期默认只加载最近若干根K线，早期箭头被吸附到数据边缘 | 切到该周期后**滚动到图表最左**加载完整历史，箭头即恢复正确位置 |

## 注意事项

- 脚本自动读取**当前图表**的品种和周期，切换品种后需重新运行。
- **必须先依次运行「画笔」→「支阻互换位」**，本脚本才能标记；否则报错退出。
- 本 SKILL 只负责进出场箭头标记，**不画笔、不清除笔/中枢/买卖点/支阻位**。
- 信号是「背驰 + 靠近支阻位」双条件共振，缺一不可；只有背驰但远离支阻位、或靠近支阻位但无背驰，都不标记。
- **箭头锚点用K线索引存储**：TradingView 切周期时会重置为「默认加载最近K线」，小周期（3/15分钟）默认范围短，早期箭头切周期后可能漂移；这是平台限制，滚动到图表最左加载完整历史即可恢复（画笔的笔同样存在此现象，但箭头是单点所以更明显）。
