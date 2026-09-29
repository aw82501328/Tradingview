# -*- coding: utf-8 -*-
"""webapp 实盘 API（/api/live/overview + /live 页面 + POST /api/live/trading）。

GET 只读：仅查 bars.db 的 live_* 表，不触碰 ChartLock/三模式互斥，
不写任何状态——webapp 重启不影响 live_trader 进程。

POST /api/live/trading（2026-09-29 策略交易开关）：仅写 live_state kv
（trading_enabled@<策略>）+ live_events 审计行，同样不触碰 live_trader 进程
与 live_config（避免参数漂移守卫拒启）；live_trader 每拍重读该 kv 生效。

策略感知（2026-09-29 模块分层）：kv 状态键带策略后缀（live_trader 写入侧同口径）。
overview 按 module_registry 逐策略聚合为列表（路径A 多进程，每策略一 tab；
无会话的策略也占位，保证 tab 列表稳定）。
"""

import time

from . import live_store, module_registry

TRADING_KEY = "trading_enabled"


def _trading_enabled(sid):
    """页面交易开关 kv（缺键=开启，向后兼容）。"""
    kv = live_store.load_state(live_store.state_key(TRADING_KEY, sid))
    return True if not kv else bool(kv.get("enabled", True))


def _pnl_summary(closed):
    """已平仓盈亏汇总：合计（引擎/券商口径）、胜率（券商优先回退引擎）、平均偏差。"""
    engine_total = sum(t.get("engine_pnl") or 0.0 for t in closed)
    broker_vals = [t.get("broker_pnl") for t in closed if t.get("broker_pnl") is not None]
    pnls = [(t["broker_pnl"] if t.get("broker_pnl") is not None else t.get("engine_pnl"))
            for t in closed]
    pnls = [p for p in pnls if p is not None]
    devs = [t.get("deviation") for t in closed if t.get("deviation") is not None]
    return {
        "closed": len(closed),
        "engine_pnl": round(engine_total, 2),
        "broker_pnl": round(sum(broker_vals), 2) if broker_vals else None,
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else None,
        "avg_deviation": round(sum(devs) / len(devs), 2) if devs else None,
    }


def _strategy_snapshot(sid):
    """单策略快照：无会话也占位（offline 空表），保证 tab 列表稳定。"""
    spec = module_registry.STRATEGIES.get(sid) or {}
    session = live_store.load_state(live_store.state_key("session", sid)) or {}
    hb = live_store.load_state(live_store.state_key("heartbeat", sid)) or {}
    cursor = live_store.load_state(live_store.state_key("engine_cursor", sid)) or {}
    out = {
        "ok": True, "strategy": sid,
        "strategy_title": spec.get("title", sid),
        "online": bool(hb) and (int(time.time()) - int(hb.get("ts") or 0)) < 90,
        "session": session, "heartbeat": hb, "engine_cursor": cursor,
        "trading_enabled": _trading_enabled(sid),
        "open_trades": [], "closed_trades": [], "orders": [], "events": [],
        "pnl_summary": None,
    }
    sess_id = session.get("id")
    if not sess_id:
        return out
    trades = live_store.all_trades(sess_id)
    out["open_trades"] = [t for t in trades if t["state"] not in ("closed", "detached")]
    closed = [t for t in trades if t["state"] == "closed"]
    out["closed_trades"] = closed[-50:]
    out["pnl_summary"] = _pnl_summary(closed)
    out["orders"] = list(reversed(live_store.orders_of(sess_id)[-100:]))
    out["events"] = list(reversed(live_store.recent_events(sess_id, n=80)))
    return out


def overview():
    """多策略实盘状态快照列表（live_trader 可能不在运行：heartbeat 超时即离线）。"""
    return {"ok": True,
            "strategies": [_strategy_snapshot(sid)
                           for sid in module_registry.strategy_ids()]}


def set_trading(strategy_id, enabled):
    """写页面交易开关 kv + 审计事件（进程离线时也照记，恢复后生效）。"""
    sid = module_registry.normalize_strategy(strategy_id)   # 未知 → ValueError → 400
    live_store.save_state(live_store.state_key(TRADING_KEY, sid),
                          {"enabled": bool(enabled)})
    session = live_store.load_state(live_store.state_key("session", sid)) or {}
    live_store.log_event(session.get("id") or f"web:{sid}",
                         "trading_enabled" if enabled else "trading_disabled",
                         {"strategy": sid, "enabled": bool(enabled)})
    return {"strategy": sid, "enabled": bool(enabled)}


def handle(handler, app, method):
    """webapp 路由挂载点（GET /live、/api/live/overview；POST /api/live/trading）。"""
    path = handler.path.split("?", 1)[0]
    if method == "GET":
        if path == "/api/live/overview":
            handler._send_json(overview())
            return True
        if path in ("/live", "/live.html"):
            handler._serve_file("live.html", "text/html; charset=utf-8")
            return True
        return False
    if method == "POST" and path == "/api/live/trading":
        try:
            body = handler._read_body()
            if not isinstance(body, dict):
                raise ValueError("请求体须为对象")
            if not isinstance(body.get("enabled"), bool):
                raise ValueError("enabled 须为布尔值")
            result = set_trading(body.get("strategy"), body["enabled"])
        except (ValueError, TypeError) as exc:
            handler._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            handler._send_json({"ok": False, "error": str(exc)}, 503)
        else:
            handler._send_json({"ok": True, **result})
        return True
    return False
