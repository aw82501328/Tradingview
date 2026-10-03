# -*- coding: utf-8 -*-
"""EVAL 评估子API（/api/eval/*）：用例/基线/评估任务的前端入口。

GET  bars-info / cases / baselines / run-state / bl-bis?id=（基线冻结笔明细）
POST create-case / delete-case / create-baseline / refresh-baseline /
     rename-baseline / delete-baseline / run
"""

from urllib.parse import urlparse, parse_qs

from .eval_service import BusyError


def _require_id(body):
    cid = body.get("id")
    if not isinstance(cid, str) or not cid:
        raise ValueError("id 不能为空")
    return cid


def handle(handler, app, method):
    parsed = urlparse(handler.path)
    if not parsed.path.startswith("/api/eval/"):
        return False
    action = parsed.path.removeprefix("/api/eval/")
    mgr = app.eval
    symbol = (app.analysis.cfg or {}).get("symbol")
    try:
        if method == "GET":
            if action == "bars-info":
                result = {"symbol": symbol, **mgr.bars_info()}
            elif action == "cases":
                result = {"cases": mgr.list_cases()}
            elif action == "baselines":
                result = {"baselines": mgr.list_baselines()}
            elif action == "run-state":
                result = mgr.job_snapshot()
            elif action == "bl-bis":
                bid = (parse_qs(parsed.query).get("id") or [""])[0]
                if not bid:
                    raise ValueError("id 不能为空")
                result = mgr.baseline_bis(bid)
            else:
                handler._send_json({"ok": False, "error": "未知评估接口"}, 404)
                return True
        else:
            body = handler._read_body()
            if not isinstance(body, dict):
                raise ValueError("请求体须为对象")
            if action == "create-case":
                result = mgr.create_case(body, symbol=symbol) or {}
            elif action == "delete-case":
                result = mgr.delete_case(_require_id(body)) or {}
            elif action == "create-baseline":
                result = mgr.start_freeze(body) or {}
            elif action == "refresh-baseline":
                result = mgr.start_freeze({"id": _require_id(body)}) or {}
            elif action == "rename-baseline":
                result = mgr.rename_baseline(body) or {}
            elif action == "delete-baseline":
                result = mgr.delete_baseline(_require_id(body)) or {}
            elif action == "run":
                ids = body.get("baselineIds")
                if ids is not None and not isinstance(ids, list):
                    raise ValueError("baselineIds 须为数组")
                result = mgr.start_run(ids or []) or {}
            else:
                handler._send_json({"ok": False, "error": "未知评估接口"}, 404)
                return True
        handler._send_json({"ok": True, **result})
    except BusyError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 409)
    except (ValueError, TypeError, KeyError) as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 503)
    return True
