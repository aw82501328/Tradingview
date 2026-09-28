"""登录后拉起 WEB 控制台，供 Windows 任务计划调用。

用 WMI 另起进程，任务本身马上结束。页面内「重启 WEB 服务」替换进程时，
不会被任务计划在结束时一并关掉。
"""
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 8000
LOG = ROOT / ".cache" / "webapp-service.log"


def _log(msg):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def _listening():
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        return sock.connect_ex((HOST, PORT)) == 0
    finally:
        sock.close()


def main():
    if _listening():
        _log(f"已在监听 {HOST}:{PORT}，跳过启动")
        return 0
    command = f'"{sys.executable}" -m py_chain.webapp --host {HOST} --port {PORT}'
    env = os.environ.copy()
    env["WEBAPP_CMD"] = command
    env["WEBAPP_CWD"] = str(ROOT)
    completed = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            "-Arguments @{ CommandLine = $env:WEBAPP_CMD; CurrentDirectory = $env:WEBAPP_CWD }; "
            "if ($r.ReturnValue -ne 0) { exit $r.ReturnValue }; Write-Output $r.ProcessId",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        _log(f"启动失败 code={completed.returncode} {detail}")
        return completed.returncode or 1
    pid = (completed.stdout or "").strip()
    _log(f"已启动 pid={pid} http://{HOST}:{PORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
