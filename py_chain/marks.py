# -*- coding: utf-8 -*-
"""
信号列表进场点标记（Web 控制台专用）

把「进场信号记录」表格里的全部信号，按各自检测周期（periodX）画到
TradingView 图表对应周期K线上：
  - 做多 = 向上箭头（arrow_up，文本 ML·BUY + 价）
  - 做空 = 向下箭头（arrow_down，文本 ML·SELL + 价）
颜色可由调用方指定（默认买红 #F23645、卖绿 #089981）。

与回测 BT·（tv_draw）、实时 RT·（monitor）标记隔离：
使用独立前缀 ML· 与独立 localStorage 键 mark_list_ids，
删除时只删自己创建的标记，不影响用户图形与其它标记。
"""

import json
import time

from .data_loader import CDPClient, CDPConfig
from .monitor import _ensure_res, RES_WAIT

# 箭头前缀与 localStorage 键（与 tv_draw / monitor 隔离）
MARK_PREFIX = "ML·"
IDS_KEY = "mark_list_ids"
CHUNK = 50

# 默认颜色：做多红、做空绿（与 tv_draw / monitor 约定一致）
DEFAULT_BUY_COLOR = "#F23645"
DEFAULT_SELL_COLOR = "#089981"


def _colors(colors=None):
    """规范化颜色配置：返回 {'buy': '#...', 'sell': '#...'}。"""
    colors = colors or {}
    return {
        "buy": colors.get("buy") or DEFAULT_BUY_COLOR,
        "sell": colors.get("sell") or DEFAULT_SELL_COLOR,
    }


def _clear_marks(c):
    """清除本模块画的 ML· 标记：读取 localStorage 记录的 shape id 逐个删除。

    返回删除数量；localStorage 为空或 id 已失效（图表切换/手动删除）时返回 0，
    不误删用户图形与 BT·/RT· 标记。
    """
    expr = (
        "(async () => { "
        "const chart = TradingViewApi.activeChart(); "
        "if (!chart) return -1; "
        "const cm = chart.chartModel(); "
        "let ids = []; "
        "try { ids = JSON.parse(localStorage.getItem('" + IDS_KEY + "') || '[]'); } catch (e) {} "
        "let removed = 0; "
        "for (const id of ids) { "
        "  try { "
        "    const ds = cm.dataSourceForId(id); "
        "    if (ds) { cm.removeSource(ds); removed++; } "
        "  } catch (err) {} "
        "} "
        "try { localStorage.setItem('" + IDS_KEY + "', '[]'); } catch (e) {} "
        "return removed; })()"
    )
    return c.evaluate(expr)


def _draw_chunk(c, chunk, colors):
    """一次 CDP 执行画出一批 ML· 箭头，并把新 shape id 累积记录到 localStorage。

    @param colors  {'buy': '#..', 'sell': '#..'} 做多/做空颜色
    @returns 新画的 shape id 列表
    """
    calls = []
    for s in chunk:
        shape = "arrow_up" if s["direction"] == "long" else "arrow_down"
        color = colors["buy"] if s["direction"] == "long" else colors["sell"]
        label = "BUY" if s["direction"] == "long" else "SELL"
        text = f"{MARK_PREFIX}{label} {s['price']:.2f}"
        calls.append(
            "await chart.createShape("
            f"{{ time: {s['time']}, price: {s['price']} }}, "
            f"{{ shape: '{shape}', text: '{text}', lock: true, "
            f"color: '{color}', textColor: '{color}' }})"
        )
    expr = (
        "(async () => { const chart = TradingViewApi.activeChart(); "
        "if (!chart) return { error: 'no_chart' }; const ids = []; "
        + "; ".join(f"ids.push(await ({c}))" for c in calls) +
        "; "
        "try { const old = JSON.parse(localStorage.getItem('" + IDS_KEY + "') || '[]'); "
        "localStorage.setItem('" + IDS_KEY + "', JSON.stringify(old.concat(ids))); } catch (e) {} "
        "; return ids; })()"
    )
    return c.evaluate(expr)


def draw_signal_marks(rows, cfg=None, clear_first=True, colors=None, log=None):
    """把信号列表全部进场点按对应周期画到图表。

    @param rows         信号行列表，每行含 time/price/direction/periodX
    @param cfg          CDPConfig
    @param clear_first  画前先清除上次 ML· 标记
    @param colors       {'buy': '#..', 'sell': '#..'} 做多/做空颜色（可选）
    @param log          日志回调（默认 print）
    @returns { drawn, cleared, errors, skipped }
    """
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: None)
    colors = _colors(colors)
    if not rows:
        log("信号列表为空，无可标记的进场点")
        return {"drawn": 0, "cleared": 0, "errors": 0, "skipped": 0}

    # 按检测周期分组（无 periodX 的归入 unknown 组，直接跳过并在日志提示）
    by_res = {}
    skipped = 0
    for s in rows:
        res = s.get("periodX")
        if not res:
            skipped += 1
            continue
        by_res.setdefault(str(res), []).append(s)
    for res in by_res:
        by_res[res].sort(key=lambda s: s.get("time") or 0)

    errors = 0
    drawn = 0
    cleared = 0
    with CDPClient(cfg, log=log) as c:
        # 记录初始周期，标记期间逐周期切换，结束恢复
        try:
            display_res = str(c.evaluate("String(TradingViewApi.activeChart().resolution());"))
        except Exception:
            display_res = None
        try:
            if clear_first:
                try:
                    cleared = int(_clear_marks(c) or 0)
                    log(f"已清除上次 ML· 标记 {cleared} 个")
                except Exception as e:
                    log(f"清除 ML· 标记失败（忽略）：{e}")
            for res in sorted(by_res, key=lambda r: int(r) if str(r).isdigit() else 0):
                chunk_list = by_res[res]
                log(f"标记 {res} 周期：{len(chunk_list)} 个进场点")
                try:
                    if _ensure_res(c, res):
                        time.sleep(RES_WAIT)
                except Exception as e:
                    errors += len(chunk_list)
                    log(f"切换到 {res} 周期失败：{e}")
                    continue
                for i in range(0, len(chunk_list), CHUNK):
                    chunk = chunk_list[i:i + CHUNK]
                    try:
                        r = _draw_chunk(c, chunk, colors)
                        n = len(r) if isinstance(r, list) else 0
                        drawn += n
                        log(f"  {res}: 已画 {drawn}/{sum(len(v) for v in by_res.values())} 个进场箭头")
                    except Exception as e:
                        errors += len(chunk)
                        log(f"  {res}: 第 {i + 1}~{i + len(chunk)} 批画标记失败：{e}")
        finally:
            if display_res:
                try:
                    if _ensure_res(c, display_res):
                        time.sleep(RES_WAIT)
                    log(f"已恢复图表周期：{display_res}")
                except Exception:
                    pass
    log(f"标记完成：共画 {drawn} 个，清除 {cleared} 个，失败 {errors} 个"
        + (f"，跳过无周期信号 {skipped} 个" if skipped else ""))
    return {"drawn": drawn, "cleared": cleared, "errors": errors, "skipped": skipped}


def clear_signal_marks(cfg=None, log=None):
    """清除所有 ML· 标记（只删自己创建的，不影响用户图形与 BT·/RT· 标记）。

    @returns 删除数量
    """
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: None)
    with CDPClient(cfg, log=log) as c:
        removed = int(_clear_marks(c) or 0)
    log(f"已清除 ML· 标记 {removed} 个")
    return removed
