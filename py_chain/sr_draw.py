# -*- coding: utf-8 -*-
"""
支阻位调试模块 CDP 绘制（sr.html「画图 / 清除本模块线」）

主线（drawnByPeriod 每显示周期 ≤2×sideCount 条）= 无限水平线（horizontal_line，
单锚点 time=breakTime / price=代表价），统一灰（默认 #787B86 可调）+ 中文来源标注
（title 带 SRT· 前缀、text 同串，TV 不支持 text 时退化为 title 悬停可见）；
调试叠加 RAW· = 合并前原始候选线（sr_service.raw_pool_lines 生成：级别 ≥ 本图的
全部来源原始候选、按来源家族配色、距现价 ≤ maxDistAtr×来源ATR），与灰线同框对照
「合并前/合并后」；一条灰线 = 附近若干 RAW 线被并成的结果。

周期可见性走「仅本显示周期」（镜像 JS srVisibilitySingle：D→days 1-1、
240→hours 4-4、60→hours 1-1、15→minutes 15-15、3→minutes 3-3、W→weeks 1-1），
创建后 applyIV 逐字段 setValue 权威路径 + overrides 双保险（与 marks.py 同款）。

前缀互不干扰：SRT·/RAW· 不含 SR_（JS skill 支阻线）/ML·（进出场标记）/BT·/RT·
前缀 —— JS skill 重跑、「删除标记」按钮都不误删本模块线，本模块清除也不动它们；
index.html 的 clear_all_marks 扩展后会把 SRT·/RAW· 一并清掉（同属系统标记）。

骨架逐段复刻 marks.py（draw_sr_marks/_clear_sr_marks/clear_all_marks 房规）：
切周期 → _ensure_hist_loaded → 分块画 → 批内 _purge_broken_marks → 恢复原周期。
"""

import json
import time

from .data_loader import CDPClient, CDPConfig
from .marks import _ensure_hist_loaded, _purge_broken_marks, _scroll_realtime
from .monitor import RES_WAIT
from .sr_flip import LEVEL_ORDER

# 主线（合并后按周期选取的位置线）
SRT_PREFIX = "SRT·"
SRT_IDS_KEY = "sr_test_ids"
# 调试叠加（合并前原始成员线）
RAW_PREFIX = "RAW·"
RAW_IDS_KEY = "sr_raw_ids"

CHUNK = 50
DEFAULT_SR_COLOR = "#787B86"
RAW_KIND_COLORS = {"cluster": "#F0B90B", "fib": "#5B8DEF", "boll": "#26A69A"}
KIND_NAMES = {"cluster": "密集区", "fib": "黄金分割", "boll": "BOLL"}

# TV resolution() 可能回别名（日线 1D/周线 1W 等），比较前归一化
_RES_ALIAS = {"1W": "W", "1D": "D", "4H": "240", "1H": "60", "W": "W", "D": "D"}


def norm_res(res):
    return _RES_ALIAS.get(str(res).upper(), str(res))


def _ensure_res_norm(c, res):
    """切周期（别名归一化后比较，避免 TV 回 1D/1W 时反复切换刷新）。返回是否切换。"""
    cur = norm_res(c.evaluate("String(TradingViewApi.activeChart().resolution());"))
    res = norm_res(res)
    if cur == res:
        return False
    c.evaluate(f"TradingViewApi.activeChart().setResolution({json.dumps(res)});")
    return True


def _iv_single_cfg(res):
    """「仅本显示周期可见」的全字段 intervalsVisibilities 配置（镜像 JS srVisibilitySingle）。
    返回 JS 对象字面量；无法识别周期返回 None（不限制）。"""
    def lit(cfg):
        return "{ " + ", ".join(f"{k}: {v}" for k, v in cfg.items()) + " }"

    base = {
        "ticks": "false",
        "seconds": "false", "secondsFrom": 1, "secondsTo": 59,
        "minutes": "false", "minutesFrom": 1, "minutesTo": 59,
        "hours": "false", "hoursFrom": 1, "hoursTo": 24,
        "days": "false", "daysFrom": 1, "daysTo": 366,
        "weeks": "false", "weeksFrom": 1, "weeksTo": 52,
        "months": "false", "monthsFrom": 1, "monthsTo": 12,
    }
    r = norm_res(res)
    if r == "W":
        return lit(dict(base, weeks="true", weeksFrom=1, weeksTo=1))
    if r == "D":
        return lit(dict(base, days="true", daysFrom=1, daysTo=1))
    if r == "240":
        return lit(dict(base, hours="true", hoursFrom=4, hoursTo=4))
    if r == "60":
        return lit(dict(base, hours="true", hoursFrom=1, hoursTo=1))
    if r.isdigit():
        minutes = int(r)
        if minutes < 60:
            return lit(dict(base, minutes="true", minutesFrom=minutes, minutesTo=minutes))
        h = minutes // 60
        if h == 1 or h == 4:
            return lit(dict(base, hours="true", hoursFrom=h, hoursTo=h))
    return None


def _clear_both(c):
    """一条表达式清掉本模块全部线：SRT· + RAW·（两 ids 键精删 + 两前缀兜底）。
    结构复刻 marks.clear_all_marks 模板。返回删除数量。"""
    expr = (
        "(async () => { "
        "const chart = TradingViewApi.activeChart(); "
        "if (!chart) return -1; "
        "const cm = chart.chartModel(); "
        "const PREFIXES = " + json.dumps([SRT_PREFIX, RAW_PREFIX]) + "; "
        "const IDS_KEYS = " + json.dumps([SRT_IDS_KEY, RAW_IDS_KEY]) + "; "
        "let removed = 0; "
        "for (const key of IDS_KEYS) { "
        "  let ids = []; "
        "  try { ids = JSON.parse(localStorage.getItem(key) || '[]'); } catch (e) {} "
        "  for (const id of ids) { "
        "    try { "
        "      const ds = cm.dataSourceForId(id); "
        "      if (ds) { cm.removeSource(ds); removed++; } "
        "    } catch (err) {} "
        "  } "
        "  try { localStorage.setItem(key, '[]'); } catch (e) {} "
        "} "
        "const readPrefix = (id) => { "
        "  try { "
        "    const sh = chart.getShapeById(id); "
        "    const props = sh && sh._source && sh._source._properties; "
        "    if (!props) return ''; "
        "    if (props.text && props.text._value) return String(props.text._value); "
        "    if (props.title && props.title._value) return String(props.title._value); "
        "    return ''; "
        "  } catch (e) { return ''; } "
        "}; "
        "try { "
        "  for (const s of chart.getAllShapes()) { "
        "    const p = readPrefix(s.id); "
        "    if (PREFIXES.some(pre => p.startsWith(pre))) { "
        "      try { chart.removeEntity(s.id); removed++; } catch (e) {} "
        "    } "
        "  } "
        "} catch (e) {} "
        "return removed; })()"
    )
    return c.evaluate(expr)


def _draw_chunk(c, items, ids_key, iv_lit):
    """一次 CDP 画一批无限水平线（同属一个显示周期 L），ids 累积到 ids_key。
    @param items [{time, price, title, color}]；IV 字面量 iv_lit（本 L 单周期可见）
    @returns {'ids': [...], 'skipped': n} 或 {'error': ...}
    """
    expr = (
        "(async () => { "
        "const chart = TradingViewApi.activeChart(); "
        "if (!chart) return { error: 'no_chart' }; "
        "const ITEMS = " + json.dumps(items) + "; "
        "const IV_CFG = " + (iv_lit or "null") + "; "
        "const applyIV = (id) => { "
        "  if (!IV_CFG) return; "
        "  try { "
        "    const iv = chart.getShapeById(id)._source._properties.intervalsVisibilities; "
        "    iv.ticks.setValue(IV_CFG.ticks); "
        "    iv.seconds.setValue(IV_CFG.seconds); "
        "    iv.secondsFrom.setValue(IV_CFG.secondsFrom); "
        "    iv.secondsTo.setValue(IV_CFG.secondsTo); "
        "    iv.minutes.setValue(IV_CFG.minutes); "
        "    iv.minutesFrom.setValue(IV_CFG.minutesFrom); "
        "    iv.minutesTo.setValue(IV_CFG.minutesTo); "
        "    iv.hours.setValue(IV_CFG.hours); "
        "    iv.hoursFrom.setValue(IV_CFG.hoursFrom); "
        "    iv.hoursTo.setValue(IV_CFG.hoursTo); "
        "    iv.days.setValue(IV_CFG.days); "
        "    iv.daysFrom.setValue(IV_CFG.daysFrom); "
        "    iv.daysTo.setValue(IV_CFG.daysTo); "
        "    iv.weeks.setValue(IV_CFG.weeks); "
        "    iv.weeksFrom.setValue(IV_CFG.weeksFrom); "
        "    iv.weeksTo.setValue(IV_CFG.weeksTo); "
        "    iv.months.setValue(IV_CFG.months); "
        "    iv.monthsFrom.setValue(IV_CFG.monthsFrom); "
        "    iv.monthsTo.setValue(IV_CFG.monthsTo); "
        "    iv.ranges.setValue(false); "
        "  } catch (e) {} "
        "}; "
        "const ids = []; let skipped = 0; "
        "for (const s of ITEMS) { "
        "  try { "
        "    const v = await chart.createMultipointShape( "
        "      [{ time: s.time, price: s.price }], "
        "      { shape: 'horizontal_line', lock: false, "
        "        overrides: { linecolor: s.color, linewidth: 1, linestyle: 0, "
        "                    title: s.title, text: s.no_text ? undefined : (s.text || s.title) } }); "
        "    if (v) { ids.push(v); applyIV(v); } else { skipped++; } "
        "  } catch (e) { skipped++; } "
        "} "
        "try { const old = JSON.parse(localStorage.getItem('" + ids_key + "') || '[]'); "
        "localStorage.setItem('" + ids_key + "', JSON.stringify(old.concat(ids))); } catch (e) {} "
        "return { ids: ids, skipped: skipped }; })()"
    )
    return c.evaluate(expr)


def _draw_group(c, items, ids_key, res, iv_lit, log, tag, cfg):
    """在一个显示周期 L 上分块画完一组线（主线或 RAW），返回 {drawn, skipped, errors}。"""
    drawn = skipped = errors = 0
    total = len(items)
    for i in range(0, total, CHUNK):
        chunk = items[i:i + CHUNK]
        batch_ids = []
        try:
            r = _draw_chunk(c, chunk, ids_key, iv_lit)
            if isinstance(r, dict):
                batch_ids = list(r.get("ids") or [])
                skipped += int(r.get("skipped") or 0)
        except Exception as e:
            # 批量失败拆单重试一轮（createMultipointShape 偶发内部错误）
            log(f"  {res}: {tag}第 {i + 1}~{i + len(chunk)} 批失败（{e}），拆单重试...")
            for it in chunk:
                try:
                    r = _draw_chunk(c, [it], ids_key, iv_lit)
                    if isinstance(r, dict):
                        batch_ids += list(r.get("ids") or [])
                    else:
                        skipped += 1
                except Exception as e2:
                    errors += 1
                    log(f"  {res}: 单条仍失败（{e2}）：{it.get('title')}")
        try:
            n_broken = int(_purge_broken_marks(c, batch_ids) or 0)
            if n_broken:
                log(f"  {res}: 清理锚点异常半成品 {n_broken} 条")
        except Exception:
            n_broken = 0
        drawn += max(0, len(batch_ids) - n_broken)
    return {"drawn": drawn, "skipped": skipped, "errors": errors}


def draw_sr_lines(main_by_period=None, raw_by_period=None, cfg=None, clear_first=True,
                  color=DEFAULT_SR_COLOR, draw_text=True, log=None):
    """把调试页的支阻线画到 TradingView 图（按显示周期 L 分组、单周期可见）。

    @param main_by_period { L: [ {time, price, label} ] } 主线（drawnByPeriod）
    @param raw_by_period  { L: [ {time, price, kind, type, source} ] } 调试 RAW 成员线（可选）
    @param clear_first    画前清掉本模块上次 SRT·/RAW· 全部线
    @param color          主线颜色（默认统一灰 #787B86）
    @param draw_text      是否写 text（关闭时仅 title，清除仍可命中）
    @returns { drawn, raw_drawn, cleared, errors, skipped }
    """
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: None)
    main_by_period = main_by_period or {}
    raw_by_period = raw_by_period or {}
    if not main_by_period and not raw_by_period:
        log("没有可画的支阻线（先计算）")
        return {"drawn": 0, "raw_drawn": 0, "cleared": 0, "errors": 0, "skipped": 0}

    def order(L):
        try:
            return LEVEL_ORDER.index(norm_res(L))
        except ValueError:
            return 99

    levels = sorted(set(main_by_period) | set(raw_by_period), key=order)
    drawn = raw_drawn = cleared = errors = skipped = 0
    with CDPClient(cfg, log=log) as c:
        try:
            display_res = str(c.evaluate("String(TradingViewApi.activeChart().resolution());"))
        except Exception:
            display_res = None
        try:
            if clear_first:
                try:
                    cleared = int(_clear_both(c) or 0)
                    log(f"已清除上次本模块支阻线 {cleared} 条")
                except Exception as e:
                    log(f"清除本模块支阻线失败（忽略）：{e}")
            for L in levels:
                no_text = not draw_text
                items = []
                for f in main_by_period.get(L, []):
                    label = str(f.get("label") or f.get("level") or L)
                    price = float(f["price"])
                    text = f"{SRT_PREFIX}{label} {price:.2f}"
                    items.append({"time": int(f.get("time") or f.get("breakTime") or 0),
                                  "price": price,
                                  "color": color,
                                  "title": text, "text": text, "no_text": no_text})
                raw_items = []
                for m in raw_by_period.get(L, []):
                    kind = str(m.get("kind") or "cluster")
                    kind_name = KIND_NAMES.get(kind, kind)
                    src = str(m.get("source") or "")
                    typ = str(m.get("type") or "")
                    price = float(m["price"])
                    t = int(m.get("time") or m.get("breakTime") or 0)
                    text = f"{RAW_PREFIX}{kind_name}{src}({typ}) {price:.2f}"
                    raw_items.append({"time": t, "price": price,
                                     "color": RAW_KIND_COLORS.get(kind, DEFAULT_SR_COLOR),
                                     "title": text, "text": text, "no_text": no_text})
                n_item = len(items) + len(raw_items)
                if not n_item:
                    continue
                iv_lit = _iv_single_cfg(L)
                try:
                    if _ensure_res_norm(c, L):
                        time.sleep(RES_WAIT)
                    min_ts = min((it["time"] for it in (items + raw_items) if it["time"]), default=0)
                    if min_ts and not _ensure_hist_loaded(c, norm_res(L), min_ts, cfg, log):
                        errors += n_item
                        log(f"  {L}: 历史未覆盖最早锚点，跳过该组 {n_item} 条（避免坏线）")
                        continue
                except Exception as e:
                    errors += n_item
                    log(f"切换到 {L} 周期失败：{e}")
                    continue
                if items:
                    r = _draw_group(c, items, SRT_IDS_KEY, L, iv_lit, log, "支阻线", cfg)
                    drawn += r["drawn"]; skipped += r["skipped"]; errors += r["errors"]
                    log(f"  {L}: 支阻线已画 {drawn} 条")
                if raw_items:
                    r = _draw_group(c, raw_items, RAW_IDS_KEY, L, iv_lit, log, "成员线", cfg)
                    raw_drawn += r["drawn"]; skipped += r["skipped"]; errors += r["errors"]
                    log(f"  {L}: 成员线(RAW) 已画 {raw_drawn} 条")
        finally:
            if display_res:
                try:
                    if _ensure_res_norm(c, display_res):
                        time.sleep(RES_WAIT)
                    log(f"已恢复图表周期：{display_res}")
                except Exception:
                    pass
            try:
                _scroll_realtime(c)
            except Exception:
                pass
    log(f"支阻位绘制完成：主线 {drawn} 条，成员线 {raw_drawn} 条，清除 {cleared} 条，"
        f"失败 {errors} 条" + (f"，跳过 {skipped} 条" if skipped else ""))
    return {"drawn": drawn, "raw_drawn": raw_drawn, "cleared": cleared,
            "errors": errors, "skipped": skipped}


def clear_sr_test_marks(cfg=None, log=None):
    """清除本模块全部线（SRT· + RAW·）。不影响 JS skill 的 SR_ 线、ML· 等其它标记。"""
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: None)
    with CDPClient(cfg, log=log) as c:
        removed = int(_clear_both(c) or 0)
    log(f"已清除本模块支阻线 {removed} 条")
    return removed
