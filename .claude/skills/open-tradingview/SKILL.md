---
name: open-tradingview
description: Open or launch the local TradingView Desktop app with the CDP debug port (9222) so the agent can connect and control the chart. Use when the user says 打开交易软件, 打开TD, 打开TradingView, 启动TradingView, 打开交易终端, or whenever the TradingView app needs to be started (e.g. CDP connection refused).
---

# 打开 TradingView Desktop

自动检测并启动本地 TradingView Desktop（带 CDP 调试端口 9222），
确保后续 CDP 脚本（画笔、回测、趋势对比等）可以连接。

## Claude Code 调用方式

本技能已移植到 Claude Code（与 Cursor 版共用 `.cursor/skills/open-tradingview/scripts/open_tradingview.js`）：

- **斜杠命令**：对话框输入 `/open-tradingview` 直接调用；
- **意图触发**：用户说「打开交易软件 / 打开TD / 打开TradingView / 启动TradingView / 打开交易终端」，或任何 CDP 脚本报 `ECONNREFUSED`（9222 未监听）需要先拉起 TradingView 时；
- **加载后**：Claude 先检查 9222 端口（已监听则告知用户直接可用、不重复启动），未监听则用 Bash 运行 `node .cursor/skills/open-tradingview/scripts/open_tradingview.js`，并按下方「输出说明」汇报。

## 功能

1. **端口检查**：若 9222 已监听（TradingView 已在运行），直接返回，不重复启动
2. **自动查找**：在 `C:\Program Files\WindowsApps` 中按通配符查找
   `TradingView.Desktop_*\TradingView.exe`（兼容不同版本号）
3. **调试模式启动**：以 `--remote-debugging-port=9222` 参数启动
4. **等待就绪**：轮询端口最多 30 秒，确认 CDP 可用后返回

## 使用方式

```bash
node .cursor/skills/open-tradingview/scripts/open_tradingview.js
```

## 输出说明

| 输出 | 含义 |
|------|------|
| `TradingView 已在运行（端口 9222 已监听）` | 无需启动，直接可用 |
| `启动 TradingView: <路径>` + `SUCCESS` | 本次新启动，端口就绪 |
| `ERROR: 未找到 TradingView.exe` | 未安装或路径异常 |
| `ERROR: 等待端口 9222 超时` | 启动失败，可手动打开后重试 |

## 触发场景

- 用户说「打开交易软件 / 打开TD / 打开TradingView / 启动TradingView」
- CDP 连接报 `ECONNREFUSED`，需要先拉起 TradingView 再执行后续操作

## 前置条件

- TradingView Desktop 已通过 MSIX 安装（WindowsApps 目录存在该应用）
- 无需手动指定端口，脚本固定使用 9222
