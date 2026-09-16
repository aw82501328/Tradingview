# -*- coding: utf-8 -*-
"""
全量回测历史方案存储（SQLite）：参数 + 信号快照 + 固化汇总，供明细查询与多方案对比。

存储复用基础数据库 data/bars.db（data_store.DB_PATH），新增 bt_runs / bt_signals
两张表，与 K 线表互不影响（data_store 的按品种删除只动 bars/stores）。

保存语义：手动保存"最近一次完成的全量回测"。参数取 worker.cfg（ModeWorker.start
赋值、done 后仍在）；信号行按 _row_base 过滤"本次运行新增行"——/api/backtest/start
不清信号表，连跑多次行会混叠，行 id 单调递增（clear 也不复用）保证 cfg 与行严格
配对。汇总额在保存时由 compute_summary 固化，口径逐字段复刻前端 renderSummary
（web/index.html），保证当前表格 / 明细 / 对比三处数字一致；另含 equity 资金曲线点列
（compute_equity，与前端 equityPoints 同口径），以及 duration_sec 墙钟运行秒数
（ModeWorker 启动→结束，供列表/对比/明细展示），供对比小图与运行时长直接使用。

用法（入口是 WEB 全量回测页的"保存方案"按钮）：
    GET    /api/bt/runs                 列表（轻量，不含信号行）
    GET    /api/bt/runs/detail?id=      明细（含全部信号行）
    POST   /api/bt/runs/save            保存当前（最近一次完成的）回测
    POST   /api/bt/runs/rename          改名
    DELETE /api/bt/runs?id=             删除
"""

import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

from . import data_store

# 方案表建在基础数据库里（同一 SQLite 文件、独立表；bars.db 已在 .gitignore）
DEFAULT_DB_PATH = data_store.DB_PATH

# 信号行四种状态（SignalLog 的 status 取值），对比矩阵的计数口径
_STATUSES = ("信号", "持仓中", "已平仓", "同向过滤")


class _HttpError(Exception):
    """带 HTTP 状态码的业务错误（404/409 等）。"""

    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def _num(v):
    """可转浮点则返回 float，否则 None（与前端 numeric 对齐）。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _exit_pnl_events(row):
    """按 exitDisplayRows 口径拆分出场盈亏事件：半平一段 + 终局/浮盈一段。

    返回 [{t, pnl}, ...]；无有效 pnl 的事件跳过。时间戳原样保留（秒或毫秒均可）。
    """
    total = _num(row.get("pnl"))
    lots = _num(row.get("lots"))
    final_t = row.get("exitTime")
    final_pnl = total
    events = []
    half = next((e for e in (row.get("exits") or []) if e.get("type") == "half"), None)
    if half:
        half_lots = None if lots is None else lots / 2
        entry = _num(row.get("entryPrice"))
        price = _num(half.get("price"))
        d = 1 if row.get("direction") == "long" else -1 if row.get("direction") == "short" else None
        half_pnl = None
        if entry is not None and price is not None and half_lots is not None and d is not None:
            half_pnl = (price - entry) * d * half_lots
        if half_pnl is not None and half.get("time") is not None:
            events.append({"t": half["time"], "pnl": half_pnl})
        if total is not None and half_pnl is not None:
            final_pnl = total - half_pnl
    if final_pnl is not None and final_t is not None:
        events.append({"t": final_t, "pnl": final_pnl})
    elif final_pnl is not None and final_t is None and row.get("status") == "持仓中":
        # 持仓浮盈无出场时间：留给 compute_equity 在末尾补点
        events.append({"t": None, "pnl": final_pnl})
    return events


def compute_equity(rows):
    """资金曲线：从 0 起步，按出场事件时间累加已实现盈亏（含半平拆分）。

    口径与前端 equityPoints / 汇总条「合计」一致：pnl 为 None 的行不参与；
    持仓浮盈（无 exitTime）排在时间序列末尾，使终点等于 realized+floating。
    返回 [{t, v}, ...]；无事件时返回 []。起点补 (首事件时间, 0)。
    """
    timed, floating = [], []
    for r in rows:
        if r.get("pnl") is None and r.get("status") not in ("已平仓", "持仓中"):
            continue
        if r.get("status") not in ("已平仓", "持仓中"):
            continue
        if r.get("pnl") is None:
            continue
        for ev in _exit_pnl_events(r):
            if ev["pnl"] is None:
                continue
            if ev["t"] is None:
                floating.append(ev)
            else:
                timed.append(ev)
    timed.sort(key=lambda e: e["t"])
    events = timed + floating
    if not events:
        return []
    # 浮盈补时间：取末笔有时事件之后 +1，全是浮盈则用 0
    last_t = timed[-1]["t"] if timed else 0
    out = []
    cum = 0.0
    first_t = timed[0]["t"] if timed else last_t
    out.append({"t": first_t, "v": 0.0})
    for i, ev in enumerate(events):
        t = ev["t"] if ev["t"] is not None else (last_t + 1 if timed else 0)
        # 多笔浮盈共用同一补时：依次 +1 避免重叠覆盖
        if ev["t"] is None:
            t = last_t + 1 + sum(1 for e in events[:i] if e["t"] is None)
        cum += ev["pnl"]
        out.append({"t": t, "v": round(cum, 2)})
    return out


def compute_summary(rows):
    """按前端 renderSummary 的口径聚合信号行（web/index.html 盈亏汇总条）。

    pnl 为 None 的行（信号/同向过滤/未回填）不参与任何盈亏合计；
    pnl=0 的保本单不计胜负、不进盈亏均值；avg_loss==0 时 payoff_ratio 存
    None（避免 JSON 出 Infinity），前端按 win 数显示 ∞ / —。
    额外写入 equity（compute_equity），供历史方案对比小图直接使用。
    """
    closed = [r for r in rows if r.get("status") == "已平仓" and r.get("pnl") is not None]
    open_pos = [r for r in rows if r.get("status") == "持仓中" and r.get("pnl") is not None]
    win = [r for r in closed if r["pnl"] > 0]
    lose = [r for r in closed if r["pnl"] < 0]
    realized = sum(r["pnl"] for r in closed)
    floating = sum(r["pnl"] for r in open_pos)
    n_win, n_lose = len(win), len(lose)
    # 已平仓平均盈利 / 平均亏损绝对值；保本单和持仓浮盈不参与
    avg_win = sum(r["pnl"] for r in win) / n_win if n_win else 0.0
    avg_loss = -sum(r["pnl"] for r in lose) / n_lose if n_lose else 0.0
    exits = {"stopBe": 0, "stopSr": 0, "close": 0, "half": 0}
    counts = {s: 0 for s in _STATUSES}
    for r in rows:
        if r.get("exitType") in exits:
            exits[r["exitType"]] += 1
        exits["half"] += sum(1 for e in (r.get("exits") or []) if e.get("type") == "half")
        if r.get("status") in counts:
            counts[r["status"]] += 1
    return {
        "closed": len(closed), "win": n_win, "lose": n_lose,
        "win_rate": round(100.0 * n_win / (n_win + n_lose)) if (n_win + n_lose) else None,
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "payoff_ratio": round(avg_win / avg_loss, 2) if avg_loss else None,
        "realized": round(realized, 2), "floating": round(floating, 2),
        "total": round(realized + floating, 2),
        "exits": exits,
        "rows_total": len(rows),
        "cnt_signal": counts["信号"], "cnt_open": counts["持仓中"],
        "cnt_closed": counts["已平仓"], "cnt_filtered": counts["同向过滤"],
        "equity": compute_equity(rows),
    }


def build_cfg_summary(cfg):
    """列表/对比表头用的参数摘要：品种 / 周期（+ 连接）/ 起始日期。"""
    periods = cfg.get("periods") or []
    if isinstance(periods, str):
        periods = [p.strip() for p in periods.split(",") if p.strip()]
    return {
        "symbol": cfg.get("symbol"),
        "periods": "+".join(str(p) for p in periods),
        "from": cfg.get("from"),
    }


def default_name(cfg):
    """默认方案名：品种 + 起始日期 + 保存时刻（月日-时分）。"""
    symbol = str(cfg.get("symbol") or "?")
    from_s = str(cfg.get("from") or "").strip()
    return f"{symbol} {from_s}·{time.strftime('%m%d-%H%M')}".strip()


class BtRunStore:
    """bt_runs / bt_signals 读写（线程安全：实例锁串行 + 短连接用完即关，仿 data_store）。"""

    _META_COLS = ("id, name, saved_at, v, mode, worker_state, "
                  "cfg, cfg_summary, summary, signal_count")

    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = db_path
        self.lock = threading.Lock()

    def _connect(self):
        """打开短连接并确保表结构存在（幂等，模式同 data_store._connect）。"""
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        self._ensure_schema(conn)
        return conn

    @staticmethod
    def _ensure_schema(conn):
        # 信号行存整行 JSON：明细页总是整方案加载展示，且 SignalLog 行结构仍在
        # 演进（近期新增 fallback/nearEqual/expectBi），拆列会引入迁移负担。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bt_runs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                saved_at INTEGER NOT NULL,
                v INTEGER NOT NULL DEFAULT 1,
                mode TEXT NOT NULL DEFAULT 'backtest',
                worker_state TEXT,
                cfg TEXT NOT NULL,
                cfg_summary TEXT NOT NULL,
                summary TEXT NOT NULL,
                signal_count INTEGER NOT NULL)""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bt_signals (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                row_json TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)) WITHOUT ROWID""")

    @staticmethod
    def _meta_from_row(row):
        """bt_runs 查询行 → meta 字典（JSON 列反序列化；损坏抛 ValueError）。"""
        return {
            "id": row[0], "name": row[1], "saved_at": row[2], "v": row[3],
            "mode": row[4], "worker_state": row[5],
            "cfg": json.loads(row[6]),
            "cfg_summary": json.loads(row[7]),
            "summary": json.loads(row[8]),
            "signal_count": row[9],
        }

    def save(self, name, cfg, rows, worker_state=None, mode="backtest",
             duration_sec=None):
        """保存一条方案（bt_runs 一行 + bt_signals N 行，事务原子）。

        duration_sec：墙钟运行秒数，写入 summary（旧方案无此字段）。
        @returns 完整 meta（含 cfg / cfg_summary / summary）
        @raises sqlite3.IntegrityError 方案名重名（UNIQUE 约束，调用方转 409）
        """
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        cfg = dict(cfg or {})
        summary = compute_summary(rows)
        if duration_sec is not None:
            summary["duration_sec"] = float(duration_sec)
        meta = {
            "id": run_id, "name": name, "saved_at": int(time.time()), "v": 1,
            "mode": mode, "worker_state": worker_state,
            "cfg": cfg,
            "cfg_summary": build_cfg_summary(cfg),
            "summary": summary,
            "signal_count": len(rows),
        }
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        f"INSERT INTO bt_runs({self._META_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id, name, meta["saved_at"], 1, mode, worker_state,
                         _dumps(cfg), _dumps(meta["cfg_summary"]),
                         _dumps(meta["summary"]), len(rows)))
                    conn.executemany(
                        "INSERT INTO bt_signals(run_id, seq, row_json) VALUES(?,?,?)",
                        [(run_id, i, _dumps(r)) for i, r in enumerate(rows)])
            finally:
                conn.close()
        return meta

    def list(self):
        """全部方案元数据（不含信号行），按保存时间降序；单条损坏跳过并记 stderr。"""
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"SELECT {self._META_COLS} FROM bt_runs "
                    "ORDER BY saved_at DESC, id DESC")
                out = []
                for row in cur:
                    try:
                        out.append(self._meta_from_row(row))
                    except (ValueError, TypeError):
                        print(f"[bt_runs] 方案 {row[0]} 数据损坏，已跳过",
                              file=sys.stderr)
                return out
            finally:
                conn.close()

    def get(self, run_id):
        """单个方案明细（meta + signals 行原样，保序）；未知 id 返回 None。"""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT {self._META_COLS} FROM bt_runs WHERE id=?",
                    (run_id,)).fetchone()
                if row is None:
                    return None
                meta = self._meta_from_row(row)
                meta["signals"] = [json.loads(r[0]) for r in conn.execute(
                    "SELECT row_json FROM bt_signals WHERE run_id=? ORDER BY seq",
                    (run_id,))]
                return meta
            finally:
                conn.close()

    def rename(self, run_id, name):
        """改名；返回更新后的 meta，未知 id 返回 None，重名抛 IntegrityError。"""
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE bt_runs SET name=? WHERE id=?", (name, run_id))
                    if cur.rowcount == 0:
                        return None
                return self._meta_from_row(conn.execute(
                    f"SELECT {self._META_COLS} FROM bt_runs WHERE id=?",
                    (run_id,)).fetchone())
            finally:
                conn.close()

    def delete(self, run_id):
        """删除方案（bt_signals + bt_runs，事务）；返回是否删除了记录。"""
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute("DELETE FROM bt_signals WHERE run_id=?", (run_id,))
                    cur = conn.execute("DELETE FROM bt_runs WHERE id=?", (run_id,))
                    deleted = cur.rowcount > 0
                return deleted
            finally:
                conn.close()


# ============================================================
# HTTP 适配（仿 analysis_api.handle：路径前缀命中即处理并返回 True）
# ============================================================
def _save_current(app, body):
    """保存最近一次完成的全量回测：worker.cfg + 本次运行新增的信号行快照。"""
    worker = app.workers["backtest"]
    if worker.state in ("running", "paused"):
        raise _HttpError("回测进行中，请等其完成或停止后再保存", 409)
    if not worker.cfg:
        raise ValueError("服务启动后尚未运行过回测，没有可保存的参数")
    rows = app.signals.snapshot("backtest", worker._row_base)
    if not rows:
        raise ValueError("当前没有可保存的本轮回测信号记录（表格已清空或未产生信号）")
    name = str(body.get("name") or "").strip() or default_name(worker.cfg)
    name = name[:100]
    try:
        meta = app.bt_runs.save(
            name, worker.cfg, rows, worker_state=worker.state,
            duration_sec=worker.duration_sec)
    except sqlite3.IntegrityError:
        raise _HttpError(f"方案名已存在：{name}", 409)
    app.broadcaster.emit("log", {"mode": "backtest",
                                 "msg": f"已保存回测方案：{name}（{len(rows)} 条信号）"})
    return {"run": meta}


def _rename(app, body):
    run_id = str(body.get("id") or "")
    name = str(body.get("name") or "").strip()[:100]
    if not run_id:
        raise ValueError("缺少方案 id")
    if not name:
        raise ValueError("方案名不能为空")
    try:
        meta = app.bt_runs.rename(run_id, name)
    except sqlite3.IntegrityError:
        raise _HttpError(f"方案名已存在：{name}", 409)
    if meta is None:
        raise _HttpError("方案不存在", 404)
    return {"run": meta}


def handle(handler, app, method):
    """处理 /api/bt/runs 前缀的请求；命中返回 True（webapp 三个 do_* 顶部挂载）。"""
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/bt/runs":
        action = ""
    elif path.startswith("/api/bt/runs/"):
        action = path[len("/api/bt/runs/"):].strip("/")
    else:
        return False
    try:
        if method == "GET":
            if action == "":
                result = {"runs": app.bt_runs.list()}
            elif action == "detail":
                run_id = parse_qs(parsed.query).get("id", [""])[0]
                run = app.bt_runs.get(run_id)
                if run is None:
                    raise _HttpError("方案不存在", 404)
                result = {"run": run}
            else:
                raise _HttpError("未知回测方案接口", 404)
        elif method == "POST":
            body = handler._read_body()
            if not isinstance(body, dict):
                raise ValueError("请求体须为对象")
            if action == "save":
                result = _save_current(app, body)
            elif action == "rename":
                result = _rename(app, body)
            else:
                raise _HttpError("未知回测方案接口", 404)
        elif method == "DELETE":
            if action:
                raise _HttpError("未知回测方案接口", 404)
            run_id = parse_qs(parsed.query).get("id", [""])[0]
            if not app.bt_runs.delete(run_id):
                raise _HttpError("方案不存在", 404)
            result = {"removed": run_id}
        else:
            return False
        handler._send_json({"ok": True, **result})
    except _HttpError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, exc.code)
    except (ValueError, TypeError) as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 503)
    return True
