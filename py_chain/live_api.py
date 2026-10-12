# -*- coding: utf-8 -*-
"""webapp 实盘 API（/api/live/overview + /live 页面 + POST /api/live/trading）。

GET 只读：仅查 bars.db 的 live_* 表，不触碰 ChartLock/三模式互斥，
不写任何状态，也不启动进程——打开页面不会自动开启实盘。

POST /api/live/trading（2026-09-29 策略交易开关）：写 live_state kv
（trading_enabled@<策略>）+ live_events 审计行，不改 live_config
（避免参数漂移守卫拒启）；live_trader 每拍重读该 kv 生效。
enabled=true 且进程不在线时，另起线程打开已保存账号的 MT5 终端并启动
live_trader（2026-10-12）。enabled=false 只停新开仓，不关终端、不杀进程。

策略感知（2026-09-29 模块分层）：kv 状态键带策略后缀（live_trader 写入侧同口径）。
overview 按 module_registry 逐策略聚合为列表（路径A 多进程，每策略一 tab；
无会话的策略也占位，保证 tab 列表稳定）。
"""

import os
import subprocess
import sys
import threading
import time

from . import live_store, module_registry

TRADING_KEY = "trading_enabled"
ONLINE_SEC = 90
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_lock = threading.Lock()
_jobs = {}   # strategy -> {phase, error, thread, pid, spawned_at}


def _heartbeat_online(sid):
    """心跳 90 秒内视为进程在线（与页面「在线」徽章同一口径）。"""
    hb = live_store.load_state(live_store.state_key("heartbeat", sid)) or {}
    return bool(hb) and (int(time.time()) - int(hb.get("ts") or 0)) < ONLINE_SEC


def _read_utf16(path):
    """MT5 的 origin.txt / common.ini 是 UTF-16。"""
    with open(path, encoding="utf-16") as f:
        return f.read()


def _ini_value(path, key):
    prefix = key + "="
    for line in _read_utf16(path).splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def terminal_exe_for(login):
    """定位已保存该账号的 terminal64.exe（common.ini 最新的一份）。"""
    base = os.path.join(os.environ["APPDATA"], "MetaQuotes", "Terminal")
    if not os.path.isdir(base):
        raise RuntimeError("未找到 MT5 终端数据目录")
    want = str(int(login))
    best = None
    for name in os.listdir(base):
        data = os.path.join(base, name)
        origin = os.path.join(data, "origin.txt")
        common = os.path.join(data, "config", "common.ini")
        if not (os.path.isfile(origin) and os.path.isfile(common)):
            continue
        if _ini_value(common, "Login") != want:
            continue
        install = _read_utf16(origin).strip().strip("\x00")
        exe = os.path.join(install, "terminal64.exe")
        if not os.path.isfile(exe):
            continue
        mtime = os.path.getmtime(common)
        if best is None or mtime > best[0]:
            best = (mtime, exe)
    if best is None:
        raise RuntimeError(f"未找到已保存账号 {want} 的 MT5 终端")
    return best[1]


def _ps(script):
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"PowerShell 失败 code={completed.returncode}")
    return completed.stdout or ""


def _terminal_running(exe):
    want = os.path.normcase(os.path.abspath(exe))
    out = _ps("Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" "
              "| ForEach-Object { $_.ExecutablePath }")
    for line in out.splitlines():
        path = line.strip()
        if path and os.path.normcase(os.path.abspath(path)) == want:
            return True
    return False


def _trader_running(sid):
    """该策略的 live_trader 是否已在跑（预热期间尚无心跳也算）。"""
    out = _ps("Get-CimInstance Win32_Process -Filter "
              "\"Name='python.exe' OR Name='pythonw.exe'\" "
              "| ForEach-Object { $_.CommandLine }")
    for line in out.splitlines():
        cmd = line.strip()
        if "py_chain.live_trader" not in cmd:
            continue
        is_fxma = "live_config.fxma" in cmd or "--strategy fxma_v1" in cmd
        if (sid == "fxma_v1" and is_fxma) or (sid != "fxma_v1" and not is_fxma):
            return True
    return False


def _spawn(command, cwd):
    """WMI 另起进程，webapp 退出后终端与 live_trader 仍保留。"""
    env = os.environ.copy()
    env["LIVE_SPAWN_CMD"] = command
    env["LIVE_SPAWN_CWD"] = cwd
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
         "-Arguments @{ CommandLine = $env:LIVE_SPAWN_CMD; "
         "CurrentDirectory = $env:LIVE_SPAWN_CWD }; "
         "if ($r.ReturnValue -ne 0) { exit $r.ReturnValue }; "
         "Write-Output $r.ProcessId"],
        cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"启动进程失败：{detail or completed.returncode}")
    pid = (completed.stdout or "").strip().splitlines()[-1].strip()
    return int(pid)


def _pid_alive(pid):
    if not pid:
        return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _log_tail():
    path = os.path.join(_REPO, "data", "live_trader.log")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4000))
        lines = [ln.strip() for ln in f.readlines() if ln.strip()]
    return lines[-1] if lines else ""


def _set_job(sid, **kw):
    with _lock:
        job = dict(_jobs.get(sid) or {})
        job.update(kw)
        _jobs[sid] = job


def _strategy_cfg(sid):
    """与 CLI 同一配置：缠论V1 走默认合并，强分型均线V1 走 fxma 本地文件。"""
    from .live_trader import DEFAULT_CONFIG_PATH, load_config
    if sid == "chan_v1":
        return load_config(), None
    if sid == "fxma_v1":
        path = os.path.join(os.path.dirname(DEFAULT_CONFIG_PATH),
                            "live_config.fxma.local.json")
        if not os.path.isfile(path):
            raise RuntimeError("未找到 py_chain/web/live_config.fxma.local.json")
        return load_config(path), path
    raise RuntimeError(f"{sid} 没有实盘启动配置")


def _assert_startable(cfg):
    """启动前拒启条件与 live_trader.start 一致：空账号、kill 文件、实盘双条件。"""
    from .live_trader import _abspath
    login = int(cfg["account"].get("login") or 0)
    if not login:
        raise RuntimeError("实盘配置账号为空，无法启动")
    kill = _abspath(cfg["run"]["kill_file"])
    if os.path.exists(kill):
        raise RuntimeError(f"检测到 kill 文件 {kill}，拒绝启动（删除后可重启）")
    if cfg["account"].get("require_mode") == "real":
        arm = _abspath(cfg["run"].get("arm_file") or "data/LIVE_ARMED")
        if not os.path.exists(arm):
            raise RuntimeError(f"实盘开闸双条件：require_mode=real 但 {arm} 不存在，拒启")
    return login


def _wait_login(exe, login, server, timeout=120):
    """等终端登录到配置账号。initialize 只探测，随即 shutdown，不占着连接。"""
    import MetaTrader5 as mt5
    deadline = time.time() + timeout
    last = "终端未响应"
    while time.time() < deadline:
        if mt5.initialize(exe):
            acc = mt5.account_info()
            mt5.shutdown()
            if acc is not None and int(acc.login) == int(login) and (
                    not server or server in (acc.server or "")):
                return
            if acc is not None:
                last = (f"终端账号 {acc.login} / {acc.server} "
                        f"与配置 {login} {server} 不符")
        else:
            last = str(mt5.last_error())
        time.sleep(2)
    raise RuntimeError(f"MT5 在 {timeout} 秒内未登录就绪：{last}")


def _audit_launch(sid, phase, text):
    session = live_store.load_state(live_store.state_key("session", sid)) or {}
    live_store.log_event(session.get("id") or f"web:{sid}", "live_launch",
                         {"strategy": sid, "phase": phase, "text": text})


def _do_launch(sid, cfg, cfg_path):
    login = int(cfg["account"]["login"])
    server = cfg["account"].get("server") or ""
    exe = terminal_exe_for(login)
    _set_job(sid, phase="terminal", error="")
    if not _terminal_running(exe):
        _spawn('"' + exe + '"', os.path.dirname(exe))
    appeared = time.time() + 30
    while not _terminal_running(exe):
        if time.time() > appeared:
            raise RuntimeError("MT5 终端进程未出现")
        time.sleep(1)
    _set_job(sid, phase="login")
    _wait_login(exe, login, server)
    if _trader_running(sid):
        _set_job(sid, phase="trader", spawned_at=time.time())
        return
    cmd = f'"{sys.executable}" -m py_chain.live_trader'
    if cfg_path:
        cmd += f' --config "{cfg_path}"'
    pid = _spawn(cmd, _REPO)
    _set_job(sid, phase="trader", pid=pid, spawned_at=time.time())
    _audit_launch(sid, "spawned", f"pid={pid}")
    deadline = time.time() + 20
    while time.time() < deadline:
        if not _pid_alive(pid):
            raise RuntimeError(_log_tail() or "实盘进程启动后立即退出")
        time.sleep(1)


def _launch_worker(sid, cfg, cfg_path):
    try:
        _do_launch(sid, cfg, cfg_path)
    except Exception as exc:
        _set_job(sid, phase="error", error=str(exc))
        _audit_launch(sid, "error", str(exc))


def begin_launch(strategy_id):
    """开启交易时拉起终端与 live_trader。已在线或已在启动则不重复拉起。"""
    sid = module_registry.normalize_strategy(strategy_id)
    with _lock:
        job = _jobs.get(sid)
        if job and job.get("thread") and job["thread"].is_alive():
            return {"starting": True,
                    "launch_phase": job.get("phase") or "terminal",
                    "launch_error": ""}
    if _heartbeat_online(sid):
        return {"starting": False, "launch_phase": "", "launch_error": ""}
    if _trader_running(sid):
        _set_job(sid, phase="trader", error="", thread=None, spawned_at=time.time())
        return {"starting": True, "launch_phase": "trader", "launch_error": ""}
    cfg, cfg_path = _strategy_cfg(sid)
    _assert_startable(cfg)
    thread = threading.Thread(target=_launch_worker, args=(sid, cfg, cfg_path),
                              daemon=True)
    _set_job(sid, phase="terminal", error="", thread=thread, pid=None, spawned_at=0)
    thread.start()
    return {"starting": True, "launch_phase": "terminal", "launch_error": ""}


def _attach_launch(sid, out):
    """把启动进度贴进快照。在线后清空进度；进程秒退则把日志末行当作错误。"""
    job = _jobs.get(sid) or {}
    phase = job.get("phase") or ""
    err = job.get("error") or ""
    if out["online"]:
        phase, err = "", ""
    elif (phase == "trader" and job.get("pid") and job.get("spawned_at")
          and time.time() - job["spawned_at"] > 20
          and not _pid_alive(job["pid"])):
        phase = "error"
        err = err or _log_tail() or "实盘进程已退出"
    out["launch_phase"] = phase
    out["launch_error"] = err
    return out


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
        "online": _heartbeat_online(sid),
        "session": session, "heartbeat": hb, "engine_cursor": cursor,
        "trading_enabled": _trading_enabled(sid),
        "open_trades": [], "closed_trades": [], "orders": [], "events": [],
        "pnl_summary": None,
    }
    sess_id = session.get("id")
    if not sess_id:
        return _attach_launch(sid, out)
    trades = live_store.all_trades(sess_id)
    out["open_trades"] = [t for t in trades if t["state"] not in ("closed", "detached")]
    closed = [t for t in trades if t["state"] == "closed"]
    out["closed_trades"] = closed[-50:]
    out["pnl_summary"] = _pnl_summary(closed)
    out["orders"] = list(reversed(live_store.orders_of(sess_id)[-100:]))
    out["events"] = list(reversed(live_store.recent_events(sess_id, n=80)))
    return _attach_launch(sid, out)


def overview():
    """多策略实盘状态快照列表（live_trader 可能不在运行：heartbeat 超时即离线）。"""
    live_store.ensure_tables()   # webapp 可能先于 live_trader 首次运行启动（幂等）
    return {"ok": True,
            "strategies": [_strategy_snapshot(sid)
                           for sid in module_registry.strategy_ids()]}


def set_trading(strategy_id, enabled):
    """写页面交易开关 kv + 审计事件（进程离线时也照记，恢复后生效）。"""
    sid = module_registry.normalize_strategy(strategy_id)   # 未知 → ValueError → 400
    live_store.ensure_tables()
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
            try:
                handler._send_json(overview())
            except Exception as exc:   # 不让请求线程异常以空响应断连
                handler._send_json({"ok": False, "error": str(exc)}, 503)
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
            if body["enabled"]:
                result.update(begin_launch(result["strategy"]))
        except (ValueError, TypeError, RuntimeError) as exc:
            handler._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            handler._send_json({"ok": False, "error": str(exc)}, 503)
        else:
            handler._send_json({"ok": True, **result})
        return True
    return False
