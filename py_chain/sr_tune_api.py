"""HTTP adapter kept separate from the existing SR control endpoints."""
from urllib.parse import parse_qs, urlparse
from . import sr_tune


def handle(handler, app, method, env):
    parsed = urlparse(handler.path)
    if not parsed.path.startswith("/api/sr/tune/"):
        return False
    manager = app.sr_tune
    parts = parsed.path[len("/api/sr/tune/"):].strip("/").split("/")
    query = parse_qs(parsed.query)
    value = lambda key, default="": query.get(key, [default])[0]
    release = None
    launched = False
    try:
        if method == "GET":
            if parts == ["samples"]:
                result = {"samples": manager.store.samples(value("symbol").upper(), value("period") or None)}
            elif parts == ["jobs"]:
                result = {"jobs": manager.jobs(value("symbol").upper() or None, value("period") or None)}
            elif len(parts) == 2 and parts[0] == "jobs":
                result = {"job": manager.public(manager.store.get("jobs", parts[1]))}
            else:
                handler._send_json({"ok": False, "error": "未知调参接口"}, 404)
                return True
            handler._send_json({"ok": True, **result})
            return True
        body = handler._read_body() if method == "POST" else {}
        if not isinstance(body, dict):
            raise ValueError("请求体须为对象")
        if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "validate":
            cfg = env["normalize_cfg"](body.get("cfg") or {})
            handler._send_json({"ok": True, "compatible": manager.compatible(parts[1], cfg)})
            return True
        if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "stop":
            handler._send_json({"ok": True, **manager.stop(parts[1])})
            return True
        # The same active-mode gate prevents backtest/live and chart operations
        # from starting during capture/search; the marks lock also covers CDP.
        acquired = env["acquire_active"]("sr-tune")
        if acquired is not True:
            raise sr_tune.Conflict(f"{acquired} 运行中，请等待或停止后重试")
        if not env["marks_lock"].acquire(blocking=False):
            env["release_active"]("sr-tune")
            raise sr_tune.Conflict("已有计算或绘图进行中")
        env["set_busy"]("tune")
        def release():
            env["set_busy"](None)
            env["marks_lock"].release()
            env["release_active"]("sr-tune")
        if method == "POST" and parts == ["samples"]:
            result = {"sample": sr_tune.save_sample(manager.store, body)}
        elif method == "DELETE" and len(parts) == 2 and parts[0] == "samples":
            sr_tune.delete_sample(manager.store, parts[1], int(value("version", "0")))
            result = {"deleted": parts[1]}
        elif method == "POST" and parts == ["snapshots"]:
            cfg = env["normalize_cfg"](body.get("cfg") or {})
            period = sr_tune.normalize_periods([body.get("period")])[0]
            cutoff = sr_tune.parse_time(body.get("cutoff"))
            result = {"job": manager.start_capture(cfg, period, cutoff,
                                                    body.get("refresh") is True, release)}
            launched = True
        elif method == "POST" and (parts == ["jobs"] or
             (len(parts) == 3 and parts[0] == "jobs" and parts[2] == "resume")):
            cfg = env["normalize_cfg"](body.get("cfg") or {})
            period = sr_tune.normalize_periods([body.get("period")])[0]
            result = {"job": manager.start_search(cfg, period, body.get("budgetSeconds", 300),
                                                  body.get("ranges"),
                                                  parts[1] if len(parts) == 3 else None, release)}
            launched = True
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "apply":
            cfg = env["normalize_cfg"](body.get("cfg") or {})
            result = {"cfg": manager.apply(parts[1], cfg)}
        else:
            handler._send_json({"ok": False, "error": "未知调参接口"}, 404)
            return True
        handler._send_json({"ok": True, **result}, 202 if launched else 200)
    except sr_tune.Conflict as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 409)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        handler._send_json({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        handler._send_json({"ok": False, "error": f"调参操作失败：{exc}"}, 500)
    finally:
        if release is not None and not launched:
            release()
    return True
