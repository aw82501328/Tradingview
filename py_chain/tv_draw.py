# -*- coding: utf-8 -*-
"""
回画实际成交的进场箭头（Python 移植版，复用 server-cdp/src/server.js 的 createShape 方式）

通过 CDP 在 TradingView 桌面端当前图表上：
  - 清除上次回测画的箭头（文本前缀 BT·，只删自己创建的，不影响用户画的线）；
  - 回画本次回测「实际成交」的进场箭头：
      做多 = 红色向上箭头（arrow_up，文本 BUY + 成交价）
      做空 = 绿色向下箭头（arrow_down，文本 SELL + 成交价）
暂不标出场箭头（出场策略未实现）。

本模块不依赖任何 JS 服务端，直连 CDP 执行 createShape。
"""

import json

from .data_loader import CDPClient, CDPConfig

# 箭头颜色与文本前缀（BT· 前缀用于清旧标记，不影响用户自己的图形）
BUY_COLOR = "#F23645"      # 红
SELL_COLOR = "#089981"     # 绿
MARK_PREFIX = "BT·"
CHUNK = 50


def _clear_markers(c):
    """清除文本以 BT· 开头的 shape（上次回测画的箭头）。"""
    expr = (
        "(function(){ const chart = TradingViewApi.activeChart(); "
        "if (!chart) return -1; "
        "const series = chart.chartModel().mainSeries(); "
        "let removed = 0; "
        "try { "
        "  const ent = series.entities(); "
        "  const items = ent.items ? ent.items() : (ent._items || {}); "
        "  for (const k of Object.keys(items)) { "
        "    const e = items[k]; "
        "    if (e && e.type === 'shape' && e.entityData && "
        "        String(e.entityData.text || '').indexOf('" + MARK_PREFIX + "') === 0) { "
        "      try { series.removeEntity(k); removed++; } catch (err) {} "
        "    } "
        "  } "
        "} catch (err) { return -2; } "
        "return removed; })()"
    )
    return c.evaluate(expr)


def _draw_chunk(c, chunk):
    """一次 CDP 执行画出一批箭头。"""
    calls = []
    for t in chunk:
        shape = "arrow_up" if t["direction"] == "long" else "arrow_down"
        color = BUY_COLOR if t["direction"] == "long" else SELL_COLOR
        label = "BUY" if t["direction"] == "long" else "SELL"
        text = f"{MARK_PREFIX}{label} {t['entryPrice']:.2f}"
        calls.append(
            "await chart.createShape("
            f"{{ time: {t['entryTime']}, price: {t['entryPrice']} }}, "
            f"{{ shape: '{shape}', text: '{text}', lock: true, "
            f"color: '{color}', textColor: '{color}' }})"
        )
    expr = (
        "(async () => { const chart = TradingViewApi.activeChart(); "
        "if (!chart) return { error: 'no_chart' }; const ids = []; "
        + "; ".join(f"ids.push(await ({c}))" for c in calls) +
        "; return ids; })()"
    )
    return c.evaluate(expr)


def draw_trades(trades, cfg=None, clear_first=True, log=None):
    """把回测成交明细回画到图表。

    @param trades       回测结果中的 trades 列表（含 entryTime, entryPrice, direction）
    @param cfg          CDPConfig
    @param clear_first  画前先清除上次 BT· 箭头
    @returns { drawn, cleared, errors }
    """
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: None)
    if not trades:
        log("没有成交记录，跳过回画")
        return {"drawn": 0, "cleared": 0, "errors": 0}
    errors = 0
    drawn = 0
    with CDPClient(cfg, log=log) as c:
        cleared = 0
        if clear_first:
            try:
                cleared = int(_clear_markers(c) or 0)
                log(f"已清除上次回测箭头 {cleared} 个")
            except Exception as e:
                log(f"清除标记失败（忽略）：{e}")
        for i in range(0, len(trades), CHUNK):
            chunk = trades[i:i + CHUNK]
            try:
                r = _draw_chunk(c, chunk)
                n = len(r) if isinstance(r, list) else 0
                drawn += n
                log(f"已回画 {drawn}/{len(trades)} 个进场箭头")
            except Exception as e:
                errors += len(chunk)
                log(f"回画第 {i + 1}~{i + len(chunk)} 批失败：{e}")
    return {"drawn": drawn, "cleared": cleared, "errors": errors}


def draw_signals(signals_by_res, trades, cfg=None, clear_first=True, log=None):
    """按信号级别回画（兼容入口）：优先使用成交明细 trades 回画；signals 仅用于统计。"""
    return draw_trades(trades, cfg=cfg, clear_first=clear_first, log=log)
