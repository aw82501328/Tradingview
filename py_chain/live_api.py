# -*- coding: utf-8 -*-
"""webapp 实盘只读 API（/api/live/overview + /live 页面）。

只读：仅查 bars.db 的 live_* 表与 stores 元数据，不触碰 ChartLock/三模式互斥，
不写任何状态——webapp 重启不影响 live_trader 进程。
"""

import time

from . import live_store


def overview():
    """实盘状态快照（live_trader 可能不在运行：heartbeat 超时即离线）。"""
    session = live_store.load_state("session") or {}
    hb = live_store.load_state("heartbeat") or {}
    cursor = live_store.load_state("engine_cursor") or {}
    sid = session.get("id")
    online = bool(hb) and (int(time.time()) - int(hb.get("ts") or 0)) < 90
    out = {
        "ok": True, "online": online, "session": session, "heartbeat": hb,
        "engine_cursor": cursor,
        "open_trades": [], "closed_trades": [], "orders": [], "events": [],
    }
    if not sid:
        return out
    trades = live_store.all_trades(sid)
    out["open_trades"] = [t for t in trades if t["state"] not in ("closed", "detached")]
    out["closed_trades"] = [t for t in trades if t["state"] == "closed"][-50:]
    out["orders"] = list(reversed(live_store.orders_of(sid)[-100:]))
    out["events"] = list(reversed(live_store.recent_events(sid, n=80)))
    return out


def handle(handler, app, method):
    """webapp 路由挂载点（GET /live、/api/live/overview）。"""
    if method != "GET":
        return False
    path = handler.path.split("?", 1)[0]
    if path == "/api/live/overview":
        handler._send_json(overview())
        return True
    if path in ("/live", "/live.html"):
        handler._serve_file("live.html", "text/html; charset=utf-8")
        return True
    return False
