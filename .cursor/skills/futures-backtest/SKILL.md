---
name: futures-backtest
description: Backtest EMA cross trend-following strategies on futures/forex/crypto/index symbols by pulling full history from TradingView Desktop via CDP. Use when the user wants to backtest a strategy, evaluate buy/sell signals against a moving average, or measure win rate / drawdown / profit factor on 纳指/期货/外汇/币圈 品种.
disable-model-invocation: true
---

# 期货策略回测

通过 CDP 连接 TradingView Desktop，拉取**完整历史K线**（突破默认 300 根限制），
对策略做历史回测，输出收益率、胜率、盈亏比、最大回撤等统计。

## 前置条件

1. TradingView Desktop 以调试模式启动：`TradingView.exe --remote-debugging-port=9222`
2. 已打开至少一张图表，且图表停留在**要回测的品种**上（脚本自动读取当前品种）
3. chrome-remote-interface 已安装（在 `server-cdp/node_modules/`）

## 使用方式

```bash
# 日线近三年（最常用）
node .cursor/skills/futures-backtest/scripts/ema_cross_backtest.js 1D 20 3

# 15分钟全部可用数据
node .cursor/skills/futures-backtest/scripts/ema_cross_backtest.js 15 20 0

# 4小时近两年
node .cursor/skills/futures-backtest/scripts/ema_cross_backtest.js 240 20 2

# 自定义 EMA 周期（如 EMA50）
node .cursor/skills/futures-backtest/scripts/ema_cross_backtest.js 1D 50 3
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| 参数1 | 周期代码：`15`/`60`/`240`/`1D`/`1W` | `1D` |
| 参数2 | EMA 周期 | `20` |
| 参数3 | 回测年数（`0`=用全部可用数据） | `0` |

## 策略逻辑（EMA 交叉，单均线趋势跟踪）

- **多头**：价格**上穿** EMA → 开多；价格**下穿** EMA → 平多
- **空头**：价格**下穿** EMA → 开空；价格**上穿** EMA → 平空

等价于：价格在 EMA 上方持多、下方持空，**永远持仓**。
信号以收盘价成交，满仓单品种，未含手续费/点差/滑点。

## 关键技术点：加载完整历史

TradingView 桌面端默认只加载 **300 根K线**，直接读 `m_bars._items` 只能拿到约 14 个月日线。

**突破方法**：调用内部 API 滚动到最早时间，触发后台逐步加载全部历史：

```javascript
TradingViewApi.activeChart()._chartWidget.model().timeScale().scrollToFirstBar()
```

之后轮询 `m_bars._items.length` 直到稳定，即可拿到完整历史（日线约 9000+ 根，可回测几十年）。

## 数据源限制

| 周期 | 历史深度 | 能否覆盖三年 |
|------|----------|-------------|
| **日线 / 周线** | 9000+ 根（数十年） | ✅ |
| **4小时** | 约 2-3 年 | ⚠️ 边缘 |
| **1小时 / 15分钟** | 约 10 个月 | ❌ |

**结论**：FX/差价合约品种的小周期（≤1小时）只保留约 10 个月历史，
"近三年"回测只能用日线或 4 小时及以上周期。

## 回测指标解读

| 指标 | 含义 |
|------|------|
| 总收益率 | 期末资金 / 初始资金 - 1 |
| 最大回撤 | 从历史峰值到谷底的最大跌幅 |
| 胜率 | 盈利交易占比（趋势策略通常 < 40%） |
| 盈亏比 | 平均盈利 / 平均亏损（> 1 才有正期望） |
| 利润因子 | 总盈利 / 总亏损（> 1 才盈利） |

趋势跟踪策略的典型特征：**低胜率 + 高盈亏比**，靠少数大趋势覆盖震荡期连续小亏。

## 常见问题排查

| 问题 | 可能原因 | 检查命令 |
|------|----------|----------|
| ECONNREFUSED | TradingView 未以调试模式启动 | `netstat -ano \| findstr 9222` |
| 未找到页面 | 图表标签未打开 | 打开一张图表即可 |
| 数据不足 | 小周期历史深度不够，或品种无数据 | 换日线周期，或确认当前品种 |
| 模块找不到 | chrome-remote-interface 路径不对 | 确认 `server-cdp/node_modules/` 存在 |
