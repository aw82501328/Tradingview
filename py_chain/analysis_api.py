"""Small HTTP adapter for the analysis workbench; paths and scripts are allowlisted."""
import json
from urllib.parse import urlparse, parse_qs

from .analysis_service import ROOT, SCRIPTS, list_targets


def handle(handler, app, method):
    parsed = urlparse(handler.path)
    if not parsed.path.startswith("/api/analysis/"):
        return False
    action = parsed.path.removeprefix("/api/analysis/")
    manager = app.analysis
    try:
        if method == "GET":
            if action == "state":
                result = manager.snapshot()
            elif action == "targets":
                result = {"targets": list_targets(manager.cfg.get("port", 9222))}
            elif action == "result":
                result = manager.result(parse_qs(parsed.query).get("module", [""])[0])
            elif action == "docs":
                key = parse_qs(parsed.query).get("module", [""])[0]
                skill = "mark-sr-flip" if key == "sr" else SCRIPTS.get(key, (None,))[0]
                if not skill:
                    raise ValueError("未知说明模块")
                path = ROOT / ".cursor" / "skills" / skill / ("SPEC.md" if key == "entry" else "SKILL.md")
                # The files are read on demand, avoiding a second stale copy of rules.
                result = {"title": skill, "text": path.read_text(encoding="utf-8"),
                          "note": "支阻技能说明是旧JS口径；本工作台使用WEB参数与计算服务。" if key == "sr" else ""}
            else:
                handler._send_json({"ok": False, "error": "未知分析接口"}, 404)
                return True
        else:
            body = handler._read_body()
            if not isinstance(body, dict):
                raise ValueError("请求体须为对象")
            if action == "config":
                result = manager.configure(body.get("cfg") or {})
            elif action == "start":
                result = manager.start(body.get("module", "all"))
                if not result.get("ok"):
                    handler._send_json(result, 409)
                    return True
            elif action == "auto":
                if not isinstance(body.get("enabled"), bool):
                    raise ValueError("enabled须为布尔值")
                result = manager.enable_auto(body["enabled"])
            elif action == "stop":
                result = manager.stop()
            else:
                handler._send_json({"ok": False, "error": "未知分析接口"}, 404)
                return True
        handler._send_json({"ok": True, **result})
    except (ValueError, TypeError, KeyError) as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 503)
    return True
