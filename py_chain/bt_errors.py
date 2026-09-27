# -*- coding: utf-8 -*-
"""
全量回测典型案例存储（SQLite，页面显示名「典型案例」，原错误列表）：右击信号行
「复制并加入典型案例」的典型交易留档。

存储复用基础数据库 data/bars.db（data_store.DB_PATH），新增 bt_errors 一张表，
与 K 线表 / 历史方案表互不影响。每条案例 = 信号行整行 JSON 快照（row_json，与
bt_signals 及 /api/signals/locate 的 row 快照定位同口径，表格清空 / 服务重启后
仍可定位）+ 加入时的复制文本快照（copy_text）+ 处理状态（pending 待处理 /
done 已处理，前端滑动开关切换）。

去重键 source_key：live 来源（三模式控制台的信号表）用内容键
mode|symbol|time|periodX|direction|strategyKey（与 SignalLog._row_key 同口径）
——内存行 id 服务重启后从 1 重来，纯 id 键会误判重复；确定性回测重跑产生同一
信号视为同一行。run 来源（历史方案明细页）= run|<run_id>|<row.id>（run_id 全局
唯一，行内 id 在 run 内唯一且归因重算不改变）。

用法（入口是信号表 / 明细表右击菜单，见 bt_common.js attachSignalContextMenu）：
    GET    /api/bt/errors                 列表（倒序，含解析后的 row 快照）
    POST   /api/bt/errors                 加入（重复 source_key → 409）
    POST   /api/bt/errors/status          处理状态切换（body: id, status）
    DELETE /api/bt/errors?id=             删除
"""

import json
import os
import sqlite3
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

from . import data_store

# 错误表建在基础数据库里（同一 SQLite 文件、独立表；bars.db 已在 .gitignore）
DEFAULT_DB_PATH = data_store.DB_PATH

# 处理状态取值（前端滑动开关 pending=待处理 / done=已处理）
_STATUSES = ("pending", "done")

# 来源取值：live=三模式控制台信号表 / run=历史方案明细页
_SOURCE_TYPES = ("live", "run")


class _HttpError(Exception):
    """带 HTTP 状态码的业务错误（404/409 等）。"""

    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def derive_source_key(source_type, row, run_id=None):
    """典型案例条目去重键（纯函数，便于单测）。

    live：内容键（与 SignalLog._row_key 同口径）——服务重启后内存 id 从 1 重来，
          纯 id 键会误判；内容键跨会话稳定，确定性重跑的同一信号视为同一行。
    run：run_id 全局唯一 + 行内 id（run 内唯一且归因重算不改变）。
    @raises ValueError 快照缺关键字段（run 缺 run_id / 行 id；live 缺信号时间）
    """
    if source_type == "run":
        rid = run_id if isinstance(run_id, int) else None
        if isinstance(run_id, str) and run_id.strip():
            rid = run_id.strip()
        if not rid:
            raise ValueError("缺少方案 id（run_id）")
        row_id = row.get("id")
        if type(row_id) is not int or row_id <= 0:
            raise ValueError("记录快照缺少有效 id")
        return f"run|{rid}|{row_id}"
    if source_type == "live":
        if row.get("time") is None:
            raise ValueError("记录快照缺少信号时间")
        parts = [row.get(k) for k in
                 ("mode", "symbol", "time", "periodX", "direction", "strategyKey")]
        return "live|" + "|".join("" if p is None else str(p) for p in parts)
    raise ValueError(f"未知来源类型：{source_type}")


class BtErrorStore:
    """bt_errors 读写（线程安全：实例锁串行 + 短连接用完即关，仿 BtRunStore）。"""

    _COLS = ("id, created_at, status, source_type, source_key, "
             "symbol, signal_time, row_json, copy_text")

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
        # 行存整行 JSON 快照（同 bt_signals 口径，行结构仍在演进不拆列）；
        # UNIQUE(source_key) 数据库层去重并兼作查询索引；AUTOINCREMENT 保证
        # 删除后 id 不复用（前端缓存失效安全）。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bt_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','done')),
                source_type TEXT NOT NULL CHECK (source_type IN ('live','run')),
                source_key TEXT NOT NULL UNIQUE,
                symbol TEXT,
                signal_time INTEGER,
                row_json TEXT NOT NULL,
                copy_text TEXT NOT NULL DEFAULT '')""")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_bt_errors_status
                ON bt_errors(status, created_at DESC)""")

    @staticmethod
    def _entry_from_row(row):
        """查询行 → 条目字典（row_json 反序列化进 row；损坏抛 ValueError）。"""
        return {
            "id": row[0], "created_at": row[1], "status": row[2],
            "source_type": row[3], "source_key": row[4],
            "symbol": row[5], "signal_time": row[6],
            "row": json.loads(row[7]),
            "copy_text": row[8],
        }

    def add(self, source_type, source_key, row, copy_text=""):
        """加入一条错误（status 默认 pending）。

        symbol / signal_time 从行快照提取冗余存列，列表页不解析 JSON 即可展示。
        @returns 新条目 dict（含 id）
        @raises sqlite3.IntegrityError source_key 重复（UNIQUE，调用方转 409）
        """
        entry = {
            "id": None, "created_at": int(time.time()), "status": "pending",
            "source_type": source_type, "source_key": source_key,
            "symbol": row.get("symbol"), "signal_time": row.get("time"),
            "row": row, "copy_text": str(copy_text or ""),
        }
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        f"INSERT INTO bt_errors({self._COLS}) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (None, entry["created_at"], entry["status"],
                         source_type, source_key, entry["symbol"],
                         entry["signal_time"],
                         _dumps(row), entry["copy_text"]))
                    entry["id"] = cur.lastrowid
            finally:
                conn.close()
        return entry

    def list(self):
        """全部典型案例条目（含 row 快照），按加入时间倒序；单条损坏跳过并记 stderr。"""
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"SELECT {self._COLS} FROM bt_errors "
                    "ORDER BY created_at DESC, id DESC")
                out = []
                for row in cur:
                    try:
                        out.append(self._entry_from_row(row))
                    except (ValueError, TypeError):
                        print(f"[bt_errors] 条目 {row[0]} 数据损坏，已跳过",
                              file=sys.stderr)
                return out
            finally:
                conn.close()

    def set_status(self, entry_id, status):
        """切换处理状态；返回更新后的条目，未知 id 返回 None。"""
        if status not in _STATUSES:
            raise ValueError(f"无效状态：{status}")
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE bt_errors SET status=? WHERE id=?",
                        (status, entry_id))
                    if cur.rowcount == 0:
                        return None
                row = conn.execute(
                    f"SELECT {self._COLS} FROM bt_errors WHERE id=?",
                    (entry_id,)).fetchone()
                return None if row is None else self._entry_from_row(row)
            finally:
                conn.close()

    def delete(self, entry_id):
        """删除一条错误；返回是否删除了记录。"""
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "DELETE FROM bt_errors WHERE id=?", (entry_id,))
                    deleted = cur.rowcount > 0
                return deleted
            finally:
                conn.close()


# ============================================================
# HTTP 适配（仿 bt_runs.handle：路径前缀命中即处理并返回 True）
# ============================================================
def _add(app, body):
    """加入典型案例：body.row 为信号行整行快照，copy_text 为加入时复制文本。"""
    source_type = body.get("source_type")
    if source_type not in _SOURCE_TYPES:
        raise ValueError("source_type 须为 live 或 run")
    row = body.get("row")
    if not isinstance(row, dict):
        raise ValueError("缺少记录快照 row")
    run_id = body.get("run_id")
    if run_id is not None and not isinstance(run_id, (int, str)):
        raise ValueError("run_id 类型无效")
    source_key = derive_source_key(source_type, row, run_id)
    try:
        entry = app.bt_errors.add(
            source_type, source_key, row, copy_text=str(body.get("copy_text") or ""))
    except sqlite3.IntegrityError:
        raise _HttpError("该记录已在典型案例，未重复加入", 409) from None
    return {"entry": entry}


def _set_status(app, body):
    entry_id = body.get("id")
    status = body.get("status")
    if type(entry_id) is not int or entry_id <= 0:
        raise ValueError("id 须为正整数")
    if status not in _STATUSES:
        raise ValueError("status 须为 pending 或 done")
    entry = app.bt_errors.set_status(entry_id, status)
    if entry is None:
        raise _HttpError("典型案例条目不存在", 404)
    return {"entry": entry}


def handle(handler, app, method):
    """处理 /api/bt/errors 前缀的请求；命中返回 True（webapp 三个 do_* 顶部挂载）。"""
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/bt/errors":
        action = ""
    elif path.startswith("/api/bt/errors/"):
        action = path[len("/api/bt/errors/"):].strip("/")
    else:
        return False
    try:
        if method == "GET":
            if action == "":
                result = {"errors": app.bt_errors.list()}
            else:
                raise _HttpError("未知典型案例接口", 404)
        elif method == "POST":
            body = handler._read_body()
            if not isinstance(body, dict):
                raise ValueError("请求体须为对象")
            if action == "":
                result = _add(app, body)
            elif action == "status":
                result = _set_status(app, body)
            else:
                raise _HttpError("未知典型案例接口", 404)
        elif method == "DELETE":
            if action:
                raise _HttpError("未知典型案例接口", 404)
            raw = parse_qs(parsed.query).get("id", [""])[0]
            try:
                entry_id = int(raw)
            except ValueError:
                raise ValueError("id 须为整数") from None
            if not app.bt_errors.delete(entry_id):
                raise _HttpError("典型案例条目不存在", 404)
            result = {"removed": entry_id}
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
