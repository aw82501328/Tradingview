---
name: compare-trends
description: Compare trend strength across multiple symbols on multiple timeframes using ADX, linear regression R², and directional consistency. Use when the user asks to compare trends, rank symbols by "trendiness", analyze which market is trending strongest, or compare 金/银/油/纳指/等品种。
disable-model-invocation: true
---

# 多品种多周期趋势对比

通过 CDP 连接 TradingView Desktop，逐品种逐周期获取日K线数据，
计算 ADX（趋势强度）、线性回归 R²（趋势质量）、方向一致性（趋势干净度），
按综合评分排名。

## 前置条件

1. TradingView Desktop 以调试模式启动：`TradingView.exe --remote-debugging-port=9222`
2. 已打开至少一张图表（任意品种均可）
3. chrome-remote-interface 已安装（在 `server-cdp/node_modules/`）

## 使用方式

### 默认（金/银/油/纳指，日线+4h+1h+15m）

```bash
node .cursor/skills/compare-trends/scripts/compare.js
```

### 自定义品种和周期

```bash
node .cursor/skills/compare-trends/scripts/compare.js \
  --symbols="BINANCE:BTCUSDT,OANDA:XAUUSD" \
  --timeframes="1D,240" \
  --lookback=20
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--symbols` | 品种代码，逗号分隔 | `OANDA:XAUUSD,OANDA:XAGUSD,TVC:USOIL,FX:NAS100` |
| `--timeframes` | 周期代码，逗号分隔 | `1D,240,60,15` |
| `--lookback` | 回归分析的最近K线根数 | `20` |
| `--labels` | 显示名称，逗号分隔（与 symbols 一一对应） | 取品种代码后缀 |

### 全市场 HTML 报告

一次性拉取商品/外汇/币圈三市场数据，生成深色主题 HTML 报告：

```bash
node .cursor/skills/compare-trends/scripts/full_report.js
```

报告包含每个市场的：
- **逐周期排名表**（日线/4H/1H/15M）
- **跨周期方向共振表**（全涨/全跌/混合）
- **自动结论**：最强最弱品种、共振分析、市场情绪、ADX峰值、操作建议

输出路径：`.cursor/skills/compare-trends/scripts/report.html`

## 评分方法论

每个品种 × 周期的综合评分由以下因子相乘得出：

| 因子 | 含义 | 权重 |
|------|------|------|
| 线性回归斜率（归一化） | 方向性强度：涨得快/跌得猛 → 高 | × 20 |
| R²（决定系数） | 趋势质量：价格走势越贴合直线 → 高 | × R² |
| ADX / 100 | 趋势强度：ADX > 25 才算有趋势 | × ADX% |
| 方向一致性 | 趋势干净度：同向K线占比越高 → 高 | × 占比 |
| 今日振幅 / ATR | 波动溢价：振幅越大 → 趋势越显著 | × min(振幅比/2, 2) |

**评分解读**：
- > 1.0：强趋势
- 0.3 – 1.0：中等趋势
- < 0.1：震荡/无趋势

## 输出格式

`compare.js` 输出两大部分：

1. **逐周期排名表**：每个周期一张表格，含 ADX、+DI/-DI、R²、斜率、一致性、波动率、综合评分，按评分降序。
2. **跨周期方向一致性**：横向对比各品种在四个周期的方向，标注共振情况（全涨/全跌/X涨Y跌）。

`full_report.js` 额外在每个市场底部生成自动化结论：
- **日线冠军**：评分最高的品种 + ADX/R²
- **共振分析**：四周期全涨/全跌品种 + 三涨一跌/三跌一涨品种
- **市场情绪**：全市场涨跌比 → 极度偏多/偏多/中性/偏空/极度偏空
- **ADX 峰值**：所有品种×周期中 ADX 最高的
- **操作建议**：顺势做多/做空的品种，或观望

## 常用品种代码参考

### 商品/股指
| 名称 | 代码 |
|------|------|
| 黄金 | `OANDA:XAUUSD` |
| 白银 | `OANDA:XAGUSD` |
| 原油 | `TVC:USOIL` |
| 纳指 | `FX:NAS100` |
| 标普500 | `FX:US500` |
| 道琼斯 | `FX:US30` |

### 主要外汇对
| 名称 | 代码 |
|------|------|
| 欧元/美元 | `OANDA:EURUSD` |
| 英镑/美元 | `OANDA:GBPUSD` |
| 美元/日元 | `OANDA:USDJPY` |
| 美元/瑞郎 | `OANDA:USDCHF` |
| 澳元/美元 | `OANDA:AUDUSD` |
| 纽元/美元 | `OANDA:NZDUSD` |
| 美元/加元 | `OANDA:USDCAD` |

### 数字货币
| 名称 | 代码 |
|------|------|
| 比特币 | `BINANCE:BTCUSDT` |
| 以太坊 | `BINANCE:ETHUSDT` |
| BNB | `BINANCE:BNBUSDT` |
| Solana | `BINANCE:SOLUSDT` |
| 瑞波 | `BINANCE:XRPUSDT` |
| 狗狗币 | `BINANCE:DOGEUSDT` |
| ADA | `BINANCE:ADAUSDT` |
| AVAX | `BINANCE:AVAXUSDT` |

### 美股
| 名称 | 代码 |
|------|------|
| 苹果 | `NASDAQ:AAPL` |
| 特斯拉 | `NASDAQ:TSLA` |

## 周期代码参考

| 周期 | 代码 |
|------|------|
| 15分钟 | `15` |
| 1小时 | `60` |
| 4小时 | `240` |
| 日线 | `1D` |
| 周线 | `1W` |

## 常见问题排查

| 问题 | 可能原因 | 检查命令 |
|------|----------|----------|
| ECONNREFUSED | TradingView 未以调试模式启动 | `netstat -ano \| findstr 9222` |
| 未找到页面 | 图表标签未打开 | 打开一张图表即可 |
| 数据不足 | 品种代码错误或周期无数据 | 先用 `tv_get_chart` 确认当前品种 |
| 模块找不到 | chrome-remote-interface 路径不对 | 确认 `server-cdp/node_modules/` 存在 |
