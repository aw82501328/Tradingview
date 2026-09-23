# -*- coding: utf-8 -*-
"""
历史K线存储（SQLite）：按品种合并累积，供回测离线取数

两条取数路径：
  1. 实时图表：复用 data_loader.fetch_bars（cache=False，绝不写 bars_all_tf.json），
     深度受数据源限制（60/15/3 约10个月、240 约2-3年、D 数年、30S 约一周，
     30S 更早历史由 auto 模式回放补深覆盖，实测 2025-01 仍可回放）；
  2. 回放深拉：TradingView Bar Replay 定位到目标日期后，回放态图表的
     m_bars._items 即为该日期之前的历史K线（可拿到实时态拿不到的多年前小周期）。
     逐块往回推进（scrollToFirstBar 翻页优先、selectDate 跳跃兜底），
     每块读完立即入库（断点可续，停止/失败后已拉部分保留）。

存储：单文件 <仓库根>/data/bars.db，表结构见 _ensure_schema。
冲突（同 symbol+res+time）一律"新拉取者胜"——顺带修正旧存储里最后一根未收盘K线。

用法（CLI 手动拉取，主要入口是 WEB 基础数据页）：
    python -m py_chain.data_store --symbol OANDA:XAUUSD --from 2026-01-02
"""

import argparse
import os
import sqlite3
import statistics
import threading
import time

from .chan_core import fmtT, intervalSecOf
from .data_loader import (
    CDPClient, CDPConfig, _read_bars, _scroll_to_first_bar, _set_resolution,
    fetch_bars,
)
from .main import parse_from
from .monitor import (
    replay_check, replay_current_date, replay_select_date, replay_show_toolbar,
    replay_started, replay_stop,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "data", "bars.db")

# 所有 DB 读写都过这把锁（读快，无感）；连接每次独立创建、用完即关，避免跨线程复用
_store_lock = threading.Lock()

# 回放深拉等待参数：进回放/跳跃后等数据就绪、scrollToFirstBar 后等分批加载
REPLAY_READY_TIMEOUT = 30.0
REPLAY_READY_POLL = 0.5
SCROLL_WAIT = 12.0
# 回放深拉安全上限（轮数），防止异常情况下死循环
MAX_ROUNDS = 5000
# 月度根数低于该周期月度中位数的 30% 标记为稀疏月（相对密度，避开交易日历推导）
SPARSE_MONTH_RATIO = 0.3
# 缺段阈值（回放补拉判定与汇总页缺口展示共用口径）：≤5 天的正常周末+假期间隔
# 既不触发补拉也不计入缺口列表（7 年深库周末间隔约 400 个/周期，混入会淹没真缺段；
# 误报段进回放后发现无进展即停，代价小）。边界口径：
#   - 接缝不漏：段取 [a+1, b-1]（a=洞前最后一根、b=洞后第一根），回放定位 until 时光标
#     落在 ≤until 的最后一根K线上、该块必读到光标处；翻页停止条件 chunk_first <= fill_from
#     实际会覆盖到已存的 a 那根（接缝重叠一根，upsert 幂等）
#   - 残余风险一：≤5 天的真缺口不补拉也不展示（明细查询仍可逐根看到，口径统一但不自动处理）
#   - 残余风险二：no-progress 误判早停会留新洞——断点续拉下次重查缺段自动重试（最终一致）
FILL_GAP_SEC = 5 * 86400


def _connect():
    """打开短连接并确保表结构存在。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT NOT NULL, res TEXT NOT NULL, time INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            PRIMARY KEY (symbol, res, time)) WITHOUT ROWID""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stores (
            symbol TEXT PRIMARY KEY, updated_at INTEGER)""")
    # v1：周期级更新时间 store_res。此前只有品种级 stores.updated_at，任何周期
    # 入库都会刷新它、而汇总页把它显示在每个周期行里，单周期更新会"带亮"全品种
    # 行。旧库一次性回填（各周期先取品种级时间，之后各自独立），user_version 防重跑
    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS store_res (
                    symbol TEXT NOT NULL, res TEXT NOT NULL, updated_at INTEGER,
                    PRIMARY KEY (symbol, res)) WITHOUT ROWID""")
            conn.execute("""
                INSERT OR IGNORE INTO store_res(symbol, res, updated_at)
                SELECT src.symbol, src.res, stores.updated_at
                FROM (SELECT DISTINCT symbol, res FROM bars) src
                JOIN stores ON stores.symbol = src.symbol""")
            conn.execute("PRAGMA user_version = 1")


def _touch_store(conn, symbol, res):
    """刷新品种级与周期级更新时间（页面「最近更新」按周期展示）。"""
    now = int(time.time())
    conn.execute(
        "INSERT INTO stores(symbol, updated_at) VALUES(?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET updated_at=excluded.updated_at",
        (symbol, now))
    conn.execute(
        "INSERT INTO store_res(symbol, res, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(symbol, res) DO UPDATE SET updated_at=excluded.updated_at",
        (symbol, res, now))


def upsert_bars(symbol, res, bars):
    """按主键 upsert（新值覆盖），返回写入行数（插入+更新）。"""
    rows = [(symbol, res, b["time"], b["open"], b["high"], b["low"], b["close"])
            for b in bars]
    if not rows:
        return 0
    with _store_lock:
        conn = _connect()
        try:
            with conn:
                before = conn.total_changes
                conn.executemany(
                    "INSERT INTO bars(symbol, res, time, open, high, low, close) "
                    "VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(symbol, res, time) DO UPDATE SET "
                    "open=excluded.open, high=excluded.high, "
                    "low=excluded.low, close=excluded.close",
                    rows)
                n = conn.total_changes - before
                _touch_store(conn, symbol, res)
            return n
        finally:
            conn.close()


def list_stores():
    """列出所有已存储品种：[{symbol, updated_at, periods: {res: {count, first,
    last, updated_at}}}].（symbol.updated_at 为品种级最近写入，periods 内为各周期自身。）"""
    with _store_lock:
        conn = _connect()
        try:
            out = {}
            for sym, updated_at in conn.execute(
                    "SELECT symbol, updated_at FROM stores ORDER BY symbol"):
                out[sym] = {"symbol": sym, "updated_at": updated_at, "periods": {}}
            touch = {(sym, res): t for sym, res, t in conn.execute(
                "SELECT symbol, res, updated_at FROM store_res")}
            for sym, res, n, first, last in conn.execute(
                    "SELECT symbol, res, COUNT(*), MIN(time), MAX(time) "
                    "FROM bars GROUP BY symbol, res"):
                if sym not in out:  # stores 行缺失时兜底（理论上不该发生）
                    out[sym] = {"symbol": sym, "updated_at": None, "periods": {}}
                out[sym]["periods"][res] = {
                    "count": n, "first": first, "last": last,
                    "updated_at": touch.get((sym, res))}
            return list(out.values())
        finally:
            conn.close()


def _resolve_symbol(conn, symbol):
    """精确匹配优先，其次大小写不敏感兜底；返回存储中的实际 symbol 或 None。"""
    row = conn.execute(
        "SELECT symbol FROM stores WHERE symbol = ?", (symbol,)).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT symbol FROM stores WHERE symbol = ? COLLATE NOCASE",
        (symbol,)).fetchone()
    return row[0] if row else None


def load_store(symbol, periods=None, from_ts=0, to_ts=None):
    """回测取数入口：按周期+时间范围组装 {res: [bars]}（时间升序）。

    缺品种或某周期在窗口内无数据时抛 ValueError（信息列出缺什么）。
    """
    periods = list(periods) if periods else None
    with _store_lock:
        conn = _connect()
        try:
            actual = _resolve_symbol(conn, symbol)
            if not actual:
                raise ValueError(
                    f"本地存储没有品种 {symbol}，请先在基础数据页拉取入库")
            out = {}
            missing = []
            for res in (periods or [r for (r,) in conn.execute(
                    "SELECT DISTINCT res FROM bars WHERE symbol=?", (actual,))]):
                q = ("SELECT time, open, high, low, close FROM bars "
                     "WHERE symbol=? AND res=? AND time>=?")
                args = [actual, res, int(from_ts or 0)]
                if to_ts:
                    q += " AND time<=?"
                    args.append(int(to_ts))
                q += " ORDER BY time"
                bars = [{"time": t, "open": o, "high": h, "low": lo, "close": cl}
                        for t, o, h, lo, cl in conn.execute(q, args)]
                if not bars:
                    missing.append(res)
                    continue
                out[res] = bars
            if missing:
                raise ValueError(
                    f"本地存储 {actual} 缺少周期数据：{','.join(missing)}"
                    f"（窗口内无K线），请先在基础数据页补拉")
            return out
        finally:
            conn.close()


def delete_store(symbol):
    """删除品种全部数据；返回是否删除了内容。"""
    with _store_lock:
        conn = _connect()
        try:
            actual = _resolve_symbol(conn, symbol)
            if not actual:
                return False
            with conn:
                conn.execute("DELETE FROM bars WHERE symbol=?", (actual,))
                conn.execute("DELETE FROM store_res WHERE symbol=?", (actual,))
                conn.execute("DELETE FROM stores WHERE symbol=?", (actual,))
            return True
        finally:
            conn.close()


def delete_res(symbol, res):
    """删除某品种单个周期的数据；返回是否删除了内容。

    stores 行与该周期的 store_res 时间戳一并清理（其余周期不动）；
    若删除后该品种已无任何K线，顺带清掉 stores 行。
    """
    with _store_lock:
        conn = _connect()
        try:
            actual = _resolve_symbol(conn, symbol)
            if not actual:
                return False
            with conn:
                cur = conn.execute(
                    "DELETE FROM bars WHERE symbol=? AND res=?", (actual, res))
                deleted = cur.rowcount > 0
                conn.execute(
                    "DELETE FROM store_res WHERE symbol=? AND res=?",
                    (actual, res))
                if deleted and not conn.execute(
                        "SELECT 1 FROM bars WHERE symbol=? LIMIT 1", (actual,)
                ).fetchone():
                    conn.execute("DELETE FROM stores WHERE symbol=?", (actual,))
            return deleted
        finally:
            conn.close()


def window_stats(symbol, res, from_ts, to_ts):
    """某周期在 [from_ts, to_ts] 窗口内的 {count, first, last}（无数据 count=0）。"""
    with _store_lock:
        conn = _connect()
        try:
            actual = _resolve_symbol(conn, symbol)
            if not actual:
                return {"count": 0, "first": None, "last": None}
            q = ("SELECT COUNT(*), MIN(time), MAX(time) FROM bars "
                 "WHERE symbol=? AND res=? AND time>=? AND time<=?")
            n, first, last = conn.execute(
                q, (actual, res, int(from_ts or 0), int(to_ts))).fetchone()
            return {"count": n or 0, "first": first, "last": last}
        finally:
            conn.close()


def earliest_stored(symbol, res):
    """该周期已存储的最早时间戳（无数据返回 None）——回放补拉的续拉起点。"""
    with _store_lock:
        conn = _connect()
        try:
            actual = _resolve_symbol(conn, symbol)
            if not actual:
                return None
            row = conn.execute(
                "SELECT MIN(time) FROM bars WHERE symbol=? AND res=?",
                (actual, res)).fetchone()
            return row[0]
        finally:
            conn.close()


def stats():
    """全库汇总统计：概览 + 每品种×周期的月度覆盖与缺口分析（回测前看清数据是否够用）。

    @returns {"total_bars", "symbol_count",
              "symbols": [{"symbol", "updated_at", "periods": {
                  res: {"count","first","last","updated_at","span_days",
                        "months": [{"ym","count","sparse"}...],
                        "gaps": [{"from","to","days"}...]}}}]}
              gaps 只收相邻间隔 > FILL_GAP_SEC（5 天）的真缺段；周末/假日休市不计入
    """
    with _store_lock:
        conn = _connect()
        try:
            symbols = []
            total_bars = 0
            for sym, updated_at in conn.execute(
                    "SELECT symbol, updated_at FROM stores ORDER BY symbol"):
                periods = {}
                touch = {res: t for res, t in conn.execute(
                    "SELECT res, updated_at FROM store_res WHERE symbol=?",
                    (sym,))}
                for res, n, first, last in conn.execute(
                        "SELECT res, COUNT(*), MIN(time), MAX(time) FROM bars "
                        "WHERE symbol=? GROUP BY res", (sym,)):
                    total_bars += n
                    months = [{"ym": ym, "count": c} for ym, c in conn.execute(
                        "SELECT strftime('%Y-%m', time, 'unixepoch'), COUNT(*) "
                        "FROM bars WHERE symbol=? AND res=? "
                        "GROUP BY 1 ORDER BY 1", (sym, res))]
                    med = statistics.median([m["count"] for m in months]) if months else 0
                    for m in months:
                        m["sparse"] = bool(med and m["count"] < med * SPARSE_MONTH_RATIO)
                    times = [t for (t,) in conn.execute(
                        "SELECT time FROM bars WHERE symbol=? AND res=? ORDER BY time",
                        (sym, res))]
                    gaps = [{"from": a, "to": b, "days": round((b - a) / 86400.0, 1)}
                            for a, b in zip(times, times[1:])
                            if b - a > FILL_GAP_SEC]
                    periods[res] = {
                        "count": n, "first": first, "last": last,
                        "updated_at": touch.get(res),
                        "span_days": round((last - first) / 86400.0, 1)
                        if first and last and last > first else 0.0,
                        "months": months, "gaps": gaps}
                symbols.append({"symbol": sym, "updated_at": updated_at,
                                "periods": periods})
            return {"total_bars": total_bars, "symbol_count": len(symbols),
                    "symbols": symbols}
        finally:
            conn.close()


def query_bars(symbol, res, from_ts=0, to_ts=None, offset=0, limit=100, order="asc"):
    """明细查询：按周期+窗口分页读取（time 升/降序）。

    @returns {"total": 窗口内总数, "rows": [{time,open,high,low,close}...]}
    @raises ValueError 缺品种或窗口内无数据
    """
    with _store_lock:
        conn = _connect()
        try:
            actual = _resolve_symbol(conn, symbol)
            if not actual:
                raise ValueError(
                    f"本地存储没有品种 {symbol}，请先在基础数据页拉取入库")
            where = "symbol=? AND res=? AND time>=?"
            args = [actual, res, int(from_ts or 0)]
            if to_ts:
                where += " AND time<=?"
                args.append(int(to_ts))
            total = conn.execute(
                f"SELECT COUNT(*) FROM bars WHERE {where}", args).fetchone()[0]
            if not total:
                raise ValueError(f"本地存储 {actual} 周期 {res} 在窗口内无数据")
            direction = "DESC" if order == "desc" else "ASC"
            rows = [{"time": t, "open": o, "high": h, "low": lo, "close": cl}
                    for t, o, h, lo, cl in conn.execute(
                        f"SELECT time, open, high, low, close FROM bars "
                        f"WHERE {where} ORDER BY time {direction} LIMIT ? OFFSET ?",
                        args + [int(limit), int(offset)])]
            return {"total": total, "rows": rows}
        finally:
            conn.close()


# ============================================================
# 回放深拉
# ============================================================
# 实测结论（2026-09-12 桌面端探针，OANDA:XAUUSD）：
#   - 回放数据地板：selectFirstAvailableDate → 2006-03-20（3m 可回放约 20 年）；
#   - selectDate 定位后默认加载约 300 根（3m 约 1 天），scrollToFirstBar 一次
#     扩展到约 20000 根（约 2 个月）——每轮"跳跃+翻页"可推进约 2 个月；
#   - 回放内再次 selectDate 的生效前提：当前数据已沉降（未沉降时请求被无视），
#     跳跃后同样默认 ~300 根，需再 scrollToFirstBar 扩展；
#   - 地板之前无任何K线（m_bars 读不到），作为自然终止条件。


def _settle(c, tag, want_before=None, timeout=REPLAY_READY_TIMEOUT, log=None):
    """等回放数据沉降：started + currentDate 就绪 + bars 已反映光标且连续两次读数一致。

    @param want_before  要求光标退到该时刻之前（跳跃生效判据；None 只求数据就绪）
    @returns (pos, snap) snap=(first, last, total)；超时返回 None
    """
    log = log or (lambda *a, **k: None)
    deadline = time.time() + timeout
    stable = []
    pos = None
    snap = None
    while time.time() < deadline:
        pos = replay_current_date(c) if replay_started(c) else None
        d = _read_bars(c)
        bars = (d or {}).get("bars") or []
        ok_pos = pos is not None and (want_before is None or pos < want_before)
        snap = (bars[0]["time"], bars[-1]["time"], d["total"]) if bars else None
        if ok_pos and snap and snap[1] <= pos + 180:
            stable.append(snap)
            if len(stable) >= 2 and stable[-1] == stable[-2]:
                return pos, snap
        else:
            stable = []
        time.sleep(REPLAY_READY_POLL)
    log(f"[{tag}] 等待回放数据沉降超时（pos={pos} snap={snap}）")
    return None


def fetch_replay_deep(c, symbol, res, from_ts, until_ts, log=None, progress=None,
                      stop_evt=None):
    """单周期回放深拉：定位 until_ts → 翻页/跳跃逐块往回推进到 from_ts，每块立即入库。

    @param c        已连接的 CDPClient（调用方负责 setSymbol/恢复周期）
    @param until_ts 回放定位点（秒，通常=结束日期或已存储最早时间-1）
    @returns {"rounds": n, "written": n}；中途停止返回已入库部分
    """
    log = log or (lambda *a, **k: None)
    progress = progress or (lambda *a, **k: None)
    _set_resolution(c, res)
    time.sleep(4.0)
    replay_check(c, log=log)
    span = max(1, until_ts - from_ts)

    def _read_store():
        """读当前已加载bars（截到 until_ts）入库，返回首根时间（无数据 None）。"""
        d = _read_bars(c)
        bars = [b for b in ((d or {}).get("bars") or []) if b["time"] <= until_ts]
        if not bars:
            return None
        n = upsert_bars(symbol, res, bars)
        _read_store.written += n
        first = bars[0]["time"]
        remain = max(0, first - from_ts)
        pct = max(0, min(100, int(100 * (1 - remain / span))))
        progress(f"{res} 已到 {fmtT(first)} · 累计写入 {_read_store.written} 行 · "
                 f"剩余 {remain/86400:.1f} 天", pct=pct)
        return first
    _read_store.written = 0

    in_replay = [False]
    try:
        replay_show_toolbar(c)
        r = replay_select_date(c, until_ts * 1000, log=log)
        if r and r.get("error"):
            log(f"{res}：进入回放失败（{r['error']}），跳过该周期")
            return {"rounds": 0, "written": 0}
        in_replay[0] = True
        got = _settle(c, res, timeout=REPLAY_READY_TIMEOUT, log=log)
        if not got:
            log(f"{res}：回放定位后数据未就绪（可能已到数据地板），跳过")
            return {"rounds": 0, "written": 0}
        first = _read_store()
        if first is None:
            return {"rounds": 0, "written": 0}
        rounds = 0
        no_progress = 0
        while first > from_ts and rounds < MAX_ROUNDS:
            if stop_evt is not None and stop_evt.is_set():
                log(f"{res}：收到停止请求，已入库部分保留（断点续拉）")
                break
            rounds += 1
            # 1) 翻页扩展：scrollToFirstBar 一次可扩到 ~2 万根（约2个月）
            _scroll_to_first_bar(c)
            time.sleep(SCROLL_WAIT)
            _settle(c, f"{res}+scroll", timeout=REPLAY_READY_TIMEOUT, log=log)
            first2 = _read_store()
            if first2 is not None and first2 < first:
                first, no_progress = first2, 0
                continue
            # 2) 翻页无进展：selectDate 跳到当前首根之前（须已沉降，否则被无视）
            r = replay_select_date(c, (first - 1) * 1000, log=log)
            if r and r.get("error"):
                log(f"{res}：selectDate 跳跃失败（{r['error']}），数据可能到头")
                break
            got = _settle(c, f"{res}+jump", want_before=first, timeout=30.0, log=log)
            if got:
                first3 = _read_store()
                if first3 is not None and first3 < first:
                    first, no_progress = first3, 0
                    continue
            no_progress += 1
            if no_progress >= 2:
                log(f"{res}：无法继续向前推进（当前最早 {fmtT(first)}），"
                    f"视为数据源到头（3m 地板约 2006-03）")
                break
        return {"rounds": rounds, "written": _read_store.written}
    finally:
        if in_replay[0]:
            replay_stop(c)
            deadline = time.time() + 5.0
            while time.time() < deadline and replay_started(c):
                time.sleep(0.5)


def _window_times(symbol, res, from_ts, to_ts):
    """窗口内已存时间轴（升序 int 列表）。"""
    with _store_lock:
        conn = _connect()
        try:
            actual = _resolve_symbol(conn, symbol)
            if not actual:
                return []
            return [t for (t,) in conn.execute(
                "SELECT time FROM bars WHERE symbol=? AND res=? AND time>=? AND time<=?"
                " ORDER BY time", (actual, res, int(from_ts or 0), int(to_ts)))]
        finally:
            conn.close()


def missing_segments(symbol, res, from_ts, to_ts=None):
    """分段补缺：按已存时间轴生成缺段列表 [(fill_from, fill_until), ...]。

    前缺（首根距窗口起点 > FILL_GAP_SEC）、中洞（相邻已存间隔 > FILL_GAP_SEC）、
    尾缺（仅 to_ts 给定且已过去、距末根 > FILL_GAP_SEC；to_ts=None 时实时阶段已拉到
    最新，不算尾缺）。仅日内周期参与（D/W 靠实时深度）。空列表=覆盖充足。
    """
    sec = intervalSecOf(res)
    if not sec or sec >= 86400:
        return []
    bound = int(to_ts) if to_ts else int(time.time())
    if bound <= int(from_ts or 0):
        return []
    times = _window_times(symbol, res, from_ts, bound)
    if not times:
        return [(int(from_ts or 0), bound)]
    segs = []
    first, last = times[0], times[-1]
    if first - from_ts > FILL_GAP_SEC:
        segs.append((from_ts, first - 1))
    for a, b in zip(times, times[1:]):
        if b - a > FILL_GAP_SEC:
            segs.append((a + 1, b - 1))
    if (to_ts and to_ts <= time.time() - FILL_GAP_SEC
            and to_ts - last > FILL_GAP_SEC):
        segs.append((last + 1, to_ts))
    return segs


def _replay_fill(symbol, periods, from_ts, to_ts, log, progress, stop_evt):
    """auto 模式的回放补拉阶段：逐周期生成分段并逐段回放深拉，最后恢复原周期。

    实时阶段的数据此刻已入库，缺段完全按 DB 时间轴判定（不再依赖 live_data）。
    """
    todo = [(res, segs) for res in periods
            if (segs := missing_segments(symbol, res, from_ts, to_ts))]
    if not todo:
        log("各周期覆盖充足，无需回放补拉")
        return {}
    for res, segs in todo:
        log(f"{res} 缺 {len(segs)} 段：" + " ".join(
            f"[{fmtT(f)}~{fmtT(u)}]" for f, u in segs))
    results = {}
    cfg = CDPConfig(expected_symbol=symbol)
    with CDPClient(cfg, log=log) as c:
        # 记录原周期，结束时恢复（回放深拉会反复切周期/进出回放）
        try:
            orig_res = str(c.evaluate(
                "String(TradingViewApi.activeChart().resolution())"))
        except Exception:
            orig_res = None
        try:
            for res, segs in todo:
                total = {"segments": len(segs), "rounds": 0, "written": 0}
                results[res] = total
                for i, (fill_from, fill_until) in enumerate(segs):
                    if stop_evt is not None and stop_evt.is_set():
                        break
                    log(f"{res} 补拉段 {i + 1}/{len(segs)}："
                        f"{fmtT(fill_from)} ~ {fmtT(fill_until)}")
                    r = fetch_replay_deep(c, symbol, res, fill_from, fill_until,
                                          log=log, progress=progress,
                                          stop_evt=stop_evt)
                    total["rounds"] += r["rounds"]
                    total["written"] += r["written"]
        finally:
            if orig_res:
                try:
                    _set_resolution(c, orig_res)
                except Exception:
                    pass
    return results


# ============================================================
# 统一入口
# ============================================================

def fetch_and_store(symbol, periods, from_ts, to_ts=None, mode="auto",
                    log=None, progress=None, stop_evt=None):
    """拉取并入库：实时路径（必跑）+ auto 模式下覆盖不足的回放补拉。

    @param mode  "auto"=实时+回放补深；"live"=仅实时图表
    @returns {"symbol", "live": {res: n}, "replay": {res: {rounds, written}}}
    """
    log = log or (lambda *a, **k: print(*a))
    progress = progress or (lambda *a, **k: None)
    periods = list(periods)
    log(f"开始拉取：{symbol} periods={periods} 窗口 "
        f"[{fmtT(from_ts)} ~ {fmtT(to_ts) if to_ts else '最新'}] 模式={mode}")
    data = fetch_bars(cfg=CDPConfig(periods=periods), from_ts=from_ts,
                      cache=False, symbol=symbol, log=log, verify_symbol=True)
    summary = {"symbol": symbol, "live": {}, "replay": {}}
    for res in periods:
        bars = data.get(res) or []
        if to_ts:
            bars = [b for b in bars if b["time"] <= to_ts]
        n = upsert_bars(symbol, res, bars)
        summary["live"][res] = n
        if bars:
            log(f"入库 {res}：写入 {n} 行（{fmtT(bars[0]['time'])} ~ "
                f"{fmtT(bars[-1]['time'])}）")
        else:
            log(f"入库 {res}：本轮无数据")
    progress("实时拉取完成", pct=100 if mode == "live" else None)
    if mode == "auto" and stop_evt is not None and stop_evt.is_set():
        log("停止请求：跳过回放补拉（已入库部分保留）")
        return summary
    if mode == "auto":
        summary["replay"] = _replay_fill(symbol, periods, from_ts, to_ts,
                                         log, progress, stop_evt) or {}
    log(f"拉取完成：{symbol}（live={summary['live']} replay={summary['replay']}）")
    return summary


def main(argv=None):
    """CLI 手动拉取（主要入口是 WEB 基础数据页）。"""
    ap = argparse.ArgumentParser(description="历史K线存储：拉取入 SQLite")
    ap.add_argument("--symbol", default="OANDA:XAUUSD")
    ap.add_argument("--periods", default="D,240,60,15,3")
    ap.add_argument("--from", dest="from_date", default="2026-01-02")
    ap.add_argument("--to", dest="to_date", default=None)
    ap.add_argument("--mode", choices=["auto", "live"], default="auto")
    args = ap.parse_args(argv)
    from_ts = parse_from(args.from_date)
    to_ts = parse_from(args.to_date) if args.to_date else None
    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    fetch_and_store(args.symbol, periods, from_ts, to_ts=to_ts, mode=args.mode)
    for s in list_stores():
        if s["symbol"].upper() == args.symbol.upper():
            for res, st in s["periods"].items():
                print(f"{res}: {st['count']} 根 "
                      f"({fmtT(st['first'])} ~ {fmtT(st['last'])})")


if __name__ == "__main__":
    main()
