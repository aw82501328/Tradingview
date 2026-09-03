# -*- coding: utf-8 -*-
"""
信号列表进场点标记（Web 控制台专用）

把「进场信号记录」表格里的全部信号，按各自背驰周期（markRes）画到
TradingView 图表对应周期K线上（箭头锚点为背驰点 time，已对齐 markRes K线起点）：
  - 做多 = 向上箭头（arrow_up，文本 ML·BUY + 价）
  - 做空 = 向下箭头（arrow_down，文本 ML·SELL + 价）
颜色可由调用方指定（默认买红 #F23645、卖绿 #089981）。

与回测 BT·（tv_draw）、实时 RT·（monitor）标记隔离：
使用独立前缀 ML· 与独立 localStorage 键 mark_list_ids，
删除时只删自己创建的标记，不影响用户图形与其它标记。
"""

import json
import time

from .chan_core import intervalSecOf, fmtT
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


def _interval_visibility_js(res):
    """生成「箭头只在背驰周期显示」的 intervalsVisibilities JS 对象字面量。

    TradingView 图形可通过 intervalsVisibilities 控制其显示的周期范围，
    每一类周期（seconds/minutes/hours/days/weeks/months）独立开关并可用
    from/to 限定精确范围。这里把箭头限制为只在信号自己的背驰周期
    （markRes）上显示，避免背驰点在更细周期（如 3m）上命中、却跑到
    检测周期（如 60m）等高周期图上叠加箭头。

    返回 JS 对象字面量字符串；无法识别周期时返回 None（保持默认不限制）。
    """
    res = str(res or "").strip()
    if not res:
        return None
    up = res.upper()
    if up in ("D", "1D"):
        return ("{ seconds: false, minutes: false, hours: false, "
                "days: true, weeks: false, months: false }")
    if up in ("W", "1W", "M", "1M"):
        # 周/月线暂无对应范围字段约定，保持默认不限
        return None
    if res.isdigit():
        minutes = int(res)
    else:
        sec = intervalSecOf(res) or 0
        minutes = sec // 60 if sec else 0
    if minutes <= 0:
        return None
    if minutes < 60:
        # 分钟级周期：仅显示在该分钟图表上（如 3m/5m/15m/30m）
        return ("{ seconds: false, minutes: true, "
                f"minutesFrom: {minutes}, minutesTo: {minutes}, "
                "hours: false, days: false, weeks: false, months: false }")
    if minutes < 1440:
        # 小时级周期（TV 将 60m/240m 归入 hours 类）：仅显示在该小时图表
        h = minutes // 60
        return ("{ seconds: false, minutes: false, hours: true, "
                f"hoursFrom: {h}, hoursTo: {h}, "
                "days: false, weeks: false, months: false }")
    # 日线及以上：仅日线级显示
    return ("{ seconds: false, minutes: false, hours: false, "
            "days: true, weeks: false, months: false }")



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
        # 箭头只在背驰周期（markRes）显示；无法识别周期时保持默认不限
        iv = _interval_visibility_js(s.get("markRes"))
        iv_part = f", intervalsVisibilities: {iv}" if iv else ""
        calls.append(
            "await chart.createShape("
            f"{{ time: {s['time']}, price: {s['price']} }}, "
            f"{{ shape: '{shape}', text: '{text}', lock: false, "
            f"color: '{color}', textColor: '{color}', "
            # arrow_up/arrow_down 工具的箭头图标颜色是独立字段 arrowColor，
            # 不走顶层 color/textColor（否则恒为默认黄 #FFEB3B），必须用 overrides 指定
            f"overrides: {{ arrowColor: '{color}'{iv_part} }} }})"
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


def _dedup_rows(rows):
    """画图前去重，避免同一根K线上出现多个重叠箭头。

    信号重复的两个来源：
      1) 同一进场点（同一背驰点 time/price）会被多个检测周期（15m/60m/...）
         各自命中 → 同 (time, direction) 只保留一个箭头；
      2) 同一背驰周期在同一根K线内多次触发（箭头按 markRes 画图，time 已
         对齐 markRes K线起点）→ 同 (markRes, K线起点, direction) 只保留
         一个箭头；不同背驰K线的信号（如同一检测周期内 8:12 与 8:21）都保留。

    按 time 升序、先到先得保留第一条（不做成交/统计，仅影响标记显示）。
    """
def _read_loaded_range(c):
    """读取当前周期数据源已加载K线范围 {first, last}（UTC秒）；无K线返回 None。"""
    return c.evaluate(
        "(function(){ const c=TradingViewApi.activeChart(); "
        "const it=c.chartModel().mainSeries().data().m_bars._items; "
        "if(!it || !it.length) return null; "
        "return { first: it[0].value[0], last: it[it.length-1].value[0] }; })()"
    )


def _scroll_first_bar(c):
    """滚动到第一根K线，触发该周期历史数据加载。"""
    c.evaluate(
        "(function(){ const c=TradingViewApi.activeChart(); "
        "const w=c._chartWidget||(c.chartModel&&c.chartModel()._chartWidget); "
        "const ts=w&&w.model?w.model().timeScale():c.chartModel().timeScale(); "
        "ts.scrollToFirstBar(); return 'ok'; })()"
    )


def _scroll_realtime(c):
    """滚动回最新K线（画图时切周期/滚动历史后会停在远古位置，画完恢复视图用）。"""
    c.evaluate(
        "(function(){ const c=TradingViewApi.activeChart(); "
        "const w=c._chartWidget||(c.chartModel&&c.chartModel()._chartWidget); "
        "const ts=w&&w.model?w.model().timeScale():c.chartModel().timeScale(); "
        "ts.scrollToRealtime(); return 'ok'; })()"
    )


def _ensure_hist_loaded(c, res, min_ts, cfg=None, log=None):
    """画图前确保当前 res 周期数据源已加载到 min_ts 对应的历史K线。

    TradingView 切到某个周期后，数据源只预加载可见窗口附近的K线（约几百根）；
    若信号时间点早于已加载范围，createShape 会把箭头吸附到当前可见的K线上，
    造成同一根K线堆叠多个箭头（如远古背驰信号被画到最近可见bar）。
    这里在画图前先滚动到第一根K线触发历史加载，轮询直到数据源覆盖目标时间。

    @returns True 数据源已覆盖目标时间；False 尽力仍未覆盖（照常画，可能仍吸附）
    """
    log = log or (lambda *a, **k: None)
    cfg = cfg or CDPConfig()
    # 切周期后数据源可能尚未填充K线，先等到读得到范围
    rng = None
    t0 = time.time()
    while time.time() - t0 < max(cfg.res_wait, 4.0):
        rng = _read_loaded_range(c)
        if rng:
            break
        time.sleep(0.5)
    if not rng:
        log(f"  {res}: 数据源暂未就绪，无法确认历史加载范围（直接开始标记）")
        return False
    if min_ts >= rng["first"]:
        return True
    log(f"  {res}: 信号最早 {fmtT(min_ts)} 早于已加载范围起点 {fmtT(rng['first'])}，"
        f"先滚动加载 {res} 历史...")
    _scroll_first_bar(c)
    deadline = time.time() + max(cfg.scroll_wait, 8.0)
    while time.time() < deadline:
        time.sleep(1.5)
        rng = _read_loaded_range(c)
        if rng and min_ts >= rng["first"]:
            log(f"  {res}: 历史已覆盖到 {fmtT(rng['first'])}，开始标记")
            return True
    rng = _read_loaded_range(c)
    log(f"  {res}: 滚动加载后仅覆盖到 {fmtT(rng['first'] if rng else 0)}，"
        f"未完全覆盖目标（尽力而为）")
    return rng is not None


def _dedup_rows(rows):
    """画图前去重，避免同一根K线上出现多个重叠箭头。

    信号重复的两个来源：
      1) 同一进场点（同一背驰点 time/price）会被多个检测周期（15m/60m/...）
         各自命中 → 同 (time, direction) 只保留一个箭头；
      2) 同一背驰周期在同一根K线内多次触发（箭头按 markRes 画图，time 已
         对齐 markRes K线起点）→ 同 (markRes, K线起点, direction) 只保留
         一个箭头；不同背驰K线的信号（如同一检测周期内 8:12 与 8:21）都保留。

    按 time 升序、先到先得保留第一条（不做成交/统计，仅影响标记显示）。
    """
    deduped = []
    seen_time_dir = set()   # (time, direction)：跨检测周期的同一信号
    seen_bar = set()        # (markRes, K线起点, direction)：同一根K线同方向
    for s in sorted(rows, key=lambda x: x.get("time") or 0):
        t = s.get("time") or 0
        d = s.get("direction")
        if (t, d) in seen_time_dir:
            continue
        res = str(s.get("markRes") or "")
        bar_sec = intervalSecOf(res) or 0
        bar_start = (t // bar_sec) * bar_sec if bar_sec else t
        if (res, bar_start, d) in seen_bar:
            continue
        seen_time_dir.add((t, d))
        seen_bar.add((res, bar_start, d))
        deduped.append(s)
    return deduped


def draw_signal_marks(rows, cfg=None, clear_first=True, colors=None, log=None):
    """把信号列表全部进场点按背驰周期画到图表。

    @param rows         信号行列表，每行含 time/price/direction/periodX/markRes
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

    # 画图前去重：同一进场点被多周期命中 / 同一根背驰K线同方向多次触发只画一个箭头
    n_raw = len(rows)
    rows = _dedup_rows(rows)
    if len(rows) < n_raw:
        log(f"去重：{n_raw} 条信号合并为 {len(rows)} 条（同一进场点/同一背驰K线只画一个箭头）")

    # 按背驰周期分组（无 markRes 的直接跳过并在日志提示）
    by_res = {}
    skipped = 0
    for s in rows:
        res = s.get("markRes")
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
                log(f"标记背驰 {res} 周期：{len(chunk_list)} 个进场点")
                try:
                    if _ensure_res(c, res):
                        time.sleep(RES_WAIT)
                    # 画图前先确保该周期历史已加载到本组最早信号，避免远古信号
                    # 因数据源未加载对应K线而被 createShape 吸附到可见bar（重复箭头）
                    min_ts = chunk_list[0].get("time") or 0
                    _ensure_hist_loaded(c, res, min_ts, cfg, log)
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
            try:
                _scroll_realtime(c)
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
