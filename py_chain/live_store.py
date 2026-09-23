# -*- coding: utf-8 -*-
"""实盘持久层：bars.db 新 4 表（live_state / live_events / live_orders / live_trades）。

- 与 webapp 跨进程写同一 SQLite：连接一律 busy_timeout=10000（默认 journal，不开 WAL，
  避免动 webapp 现有读路径）。
- live_events 为 append-only 审计主证据，按保留策略定期清理（默认 180 天）。
- 引擎 trade 镜像（live_trades）带 engine_json 全量快照，每次状态迁移整行更新。
"""

import json
import os
import sqlite3
import time

from .data_store import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS live_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc  INTEGER NOT NULL,
    session TEXT NOT NULL,
    kind    TEXT NOT NULL,
    symbol  TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_events_ts ON live_events(ts_utc);

CREATE TABLE IF NOT EXISTS live_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          INTEGER NOT NULL,
    session         TEXT NOT NULL,
    action          TEXT NOT NULL,
    engine_trade_no INTEGER,
    direction       TEXT,
    volume          REAL,
    price           REAL,
    sl              REAL,
    request_ticket  INTEGER,
    position_ticket INTEGER,
    ok              INTEGER,
    retcode         INTEGER,
    retcomment      TEXT,
    deal_price      REAL,
    deal_volume     REAL,
    shadow          INTEGER DEFAULT 0,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_orders_tno ON live_orders(session, engine_trade_no);

CREATE TABLE IF NOT EXISTS live_trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session          TEXT NOT NULL,
    engine_trade_no  INTEGER NOT NULL,
    direction        TEXT,
    entry_time       INTEGER,
    entry_signal_time INTEGER,
    entry_price      REAL,
    position_ticket  INTEGER,
    volume_open      REAL,
    volume_left      REAL,
    be_done          INTEGER DEFAULT 0,
    half_done        INTEGER DEFAULT 0,
    engine_stop      REAL,
    broker_sl        REAL,
    shadow           INTEGER DEFAULT 0,
    state            TEXT DEFAULT 'open',
    exit_type        TEXT,
    exit_time        INTEGER,
    exit_price       REAL,
    engine_pnl       REAL,
    broker_pnl       REAL,
    deviation        REAL,
    engine_json      TEXT,
    updated_at       INTEGER,
    UNIQUE(session, engine_trade_no)
);
"""


def _db_path():
    """bars.db 路径；测试用 LIVE_DB_PATH 环境变量隔离到临时库。"""
    return os.environ.get("LIVE_DB_PATH") or DB_PATH


def _connect():
    """跨进程共存连接：busy_timeout=10s（与 webapp 同库不同进程写）。"""
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def ensure_tables():
    conn = _connect()
    try:
        with conn:
            conn.executescript(_SCHEMA)
    finally:
        conn.close()


def _now():
    return int(time.time())


# -- live_state（KV） --------------------------------------------------------

def save_state(key, obj):
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO live_state(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, json.dumps(obj, ensure_ascii=False), _now()))
    finally:
        conn.close()


def load_state(key, default=None):
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM live_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
    finally:
        conn.close()


def delete_state(key):
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM live_state WHERE key=?", (key,))
    finally:
        conn.close()


# -- live_events（append-only） ----------------------------------------------

def log_event(session, kind, payload, symbol=None, ts_utc=None):
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO live_events(ts_utc, session, kind, symbol, payload) "
                "VALUES(?,?,?,?,?)",
                (ts_utc or _now(), session, kind, symbol,
                 json.dumps(payload, ensure_ascii=False, default=str)))
    finally:
        conn.close()


def recent_events(session=None, n=100, kinds=None):
    conn = _connect()
    try:
        sql = "SELECT id, ts_utc, session, kind, symbol, payload FROM live_events"
        conds, args = [], []
        if session:
            conds.append("session=?")
            args.append(session)
        if kinds:
            conds.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            args.extend(kinds)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(n))
        rows = conn.execute(sql, args).fetchall()
        out = []
        for r in reversed(rows):
            e = {"id": r[0], "ts_utc": r[1], "session": r[2], "kind": r[3], "symbol": r[4]}
            try:
                e["payload"] = json.loads(r[5])
            except Exception:
                e["payload"] = r[5]
            out.append(e)
        return out
    finally:
        conn.close()


def prune_events(keep_days=180):
    """按保留策略清理过期事件（默认 180 天）。返回删除行数。"""
    conn = _connect()
    try:
        with conn:
            n = conn.execute("DELETE FROM live_events WHERE ts_utc < ?",
                             (_now() - int(keep_days) * 86400,)).rowcount
        return n
    finally:
        conn.close()


# -- live_orders（动作日志） ---------------------------------------------------

def log_order(session, action, ts_utc=None, engine_trade_no=None, direction=None,
              volume=None, price=None, sl=None, request_ticket=None,
              position_ticket=None, ok=None, retcode=None, retcomment=None,
              deal_price=None, deal_volume=None, shadow=0, raw=None):
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO live_orders(ts_utc, session, action, engine_trade_no, "
                "direction, volume, price, sl, request_ticket, position_ticket, ok, "
                "retcode, retcomment, deal_price, deal_volume, shadow, raw) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts_utc or _now(), session, action, engine_trade_no, direction,
                 volume, price, sl, request_ticket, position_ticket,
                 None if ok is None else int(ok), retcode, retcomment,
                 deal_price, deal_volume, int(shadow or 0),
                 json.dumps(raw, ensure_ascii=False, default=str) if raw else None))
            return cur.lastrowid
    finally:
        conn.close()


def update_order_trade_no(order_id, engine_trade_no):
    """成交拍回填：信号拍下的进场单（当时 tradeNo 未知）挂接到引擎 trade。"""
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE live_orders SET engine_trade_no=? WHERE id=?",
                         (engine_trade_no, order_id))
    finally:
        conn.close()


# -- live_trades（引擎 trade 镜像 + 券商挂接） ---------------------------------

TRADE_FIELDS = ("session", "engine_trade_no", "direction", "entry_time",
                "entry_signal_time", "entry_price", "position_ticket", "volume_open",
                "volume_left", "be_done", "half_done", "engine_stop", "broker_sl",
                "shadow", "state", "exit_type", "exit_time", "exit_price",
                "engine_pnl", "broker_pnl", "deviation", "engine_json")


def upsert_trade(row):
    """整行 upsert（UNIQUE(session, engine_trade_no)）。row 需含 TRADE_FIELDS 子集；
    已存在的缺失字段保留原值（部分更新语义）。返回最新行。"""
    conn = _connect()
    try:
        with conn:
            existing = conn.execute(
                "SELECT * FROM live_trades WHERE session=? AND engine_trade_no=?",
                (row["session"], row["engine_trade_no"])).fetchone()
            cols = [c[1] for c in conn.execute("PRAGMA table_info(live_trades)")]
            if existing:
                merged = dict(zip(cols, existing))
                merged.update({k: v for k, v in row.items() if k in cols})
                merged["updated_at"] = _now()
                conn.execute(
                    "UPDATE live_trades SET " + ",".join(f"{c}=?" for c in cols[1:]) +
                    " WHERE id=?",
                    [merged[c] for c in cols[1:]] + [merged["id"]])
            else:
                merged = {c: row.get(c) for c in cols}
                merged["session"] = row["session"]
                merged["engine_trade_no"] = row["engine_trade_no"]
                merged["updated_at"] = _now()
                conn.execute(
                    "INSERT INTO live_trades(" + ",".join(cols[1:]) + ") VALUES(" +
                    ",".join("?" * (len(cols) - 1)) + ")",
                    [merged[c] for c in cols[1:]])
            cur = conn.execute(
                "SELECT * FROM live_trades WHERE session=? AND engine_trade_no=?",
                (row["session"], row["engine_trade_no"])).fetchone()
            return dict(zip(cols, cur))
    finally:
        conn.close()


def get_trade(session, engine_trade_no):
    conn = _connect()
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(live_trades)")]
        cur = conn.execute(
            "SELECT * FROM live_trades WHERE session=? AND engine_trade_no=?",
            (session, engine_trade_no)).fetchone()
        return dict(zip(cols, cur)) if cur else None
    finally:
        conn.close()


def open_trades(session):
    """未终局镜像行（state NOT IN closed/detached）。"""
    conn = _connect()
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(live_trades)")]
        rows = conn.execute(
            "SELECT * FROM live_trades WHERE session=? AND state NOT IN "
            "('closed','detached') ORDER BY id", (session,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def all_trades(session):
    conn = _connect()
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(live_trades)")]
        rows = conn.execute(
            "SELECT * FROM live_trades WHERE session=? ORDER BY id", (session,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def orders_of(session, engine_trade_no=None):
    conn = _connect()
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(live_orders)")]
        if engine_trade_no is None:
            rows = conn.execute(
                "SELECT * FROM live_orders WHERE session=? ORDER BY id",
                (session,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM live_orders WHERE session=? AND engine_trade_no=? "
                "ORDER BY id", (session, engine_trade_no)).fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            if d.get("raw"):
                try:
                    d["raw"] = json.loads(d["raw"])
                except Exception:
                    pass
            out.append(d)
        return out
    finally:
        conn.close()
