# -*- coding: utf-8 -*-
"""参数中心 HTTP 适配器：GET /api/params、POST /api/params/{module}[/reset]。

chan（缠论核心）模块的保存/恢复会全局变更 CHAN_CFG——任一计算任务运行中返回 409，
防止单次计算中途混用两套参数（points/entry/plan 在每次任务启动时读取，无此约束）。
"""

from urllib.parse import urlparse

from . import param_center


def handle(handler, app, method, busy=None):
    parsed = urlparse(handler.path)
    if parsed.path != "/api/params" and not parsed.path.startswith("/api/params/"):
        return False
    parts = [p for p in parsed.path.split("/") if p]
    rest = parts[2:]
    module = rest[0] if rest else ""
    action = rest[1] if len(rest) > 1 else ""
    try:
        if method == "GET":
            if rest:
                handler._send_json({"ok": False, "error": "未知参数接口"}, 404)
                return True
            result = {"modules": param_center.snapshot()}
        elif method == "POST":
            if module not in param_center.PARAM_MODULES:
                handler._send_json({"ok": False, "error": "未知参数模块"}, 404)
                return True
            if module == "chan" and busy and busy():
                handler._send_json({"ok": False, "error":
                                    "有任务运行中，无法修改缠论核心参数，请停止任务后再试"}, 409)
                return True
            if action == "reset":
                effective = param_center.reset(module)
                result = {"effective": effective, "reset": True}
            elif action == "":
                body = handler._read_body()
                if not isinstance(body, dict) or not isinstance(body.get("cfg"), dict):
                    raise ValueError("请求体须为 {cfg: {...}}")
                effective = param_center.update(module, body["cfg"])
                result = {"effective": effective, "applied": True}
            else:
                handler._send_json({"ok": False, "error": "未知参数接口"}, 404)
                return True
        else:
            handler._send_json({"ok": False, "error": "不支持的方法"}, 405)
            return True
        handler._send_json({"ok": True, **result})
    except (ValueError, TypeError, KeyError) as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 503)
    return True
