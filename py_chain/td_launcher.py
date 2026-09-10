"""One serialized, explicit TD launch operation shared by all browser tabs."""
import json
from pathlib import Path
import subprocess
import threading
import uuid

ROOT = Path(__file__).resolve().parent.parent


def run_launcher(allow_restart):
    command = ["node", str(ROOT / ".cursor/skills/open-tradingview/scripts/open_tradingview.js"), "--json"]
    if allow_restart:
        command.append("--allow-restart")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", timeout=240,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        data = json.loads(result.stdout.strip())
        if data.get("state") not in ("ready", "needs_confirmation", "error"):
            raise ValueError("invalid result")
        return data
    except (ValueError, AttributeError):
        raise RuntimeError("TD启动脚本失败：" + (result.stderr.strip()[-1000:] or "无有效结果"))


class TDLauncher:
    def __init__(self, acquire, release, runner=run_launcher):
        self.acquire, self.release, self.runner = acquire, release, runner
        self.lock = threading.RLock()
        self.state = {"state": "idle"}
        self.thread = None

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, allow_restart=False):
        if type(allow_restart) is not bool:
            raise ValueError("allowRestart须为布尔值")
        with self.lock:
            if self.state["state"] == "starting":
                return {"ok": True, **self.state}
            owner = self.acquire("td-launch")
            if owner is not True:
                return {"ok": False, "error": f"等待 {owner} 任务结束后再启动TD"}
            self.state = {"state": "starting", "id": uuid.uuid4().hex, "message": "正在启动TD…"}
            try:
                self.thread = threading.Thread(target=self._run, args=(allow_restart,), daemon=True)
                self.thread.start()
            except Exception:
                self.state = {"state": "error", "message": "无法创建TD启动任务"}
                self.release("td-launch")
                raise
            return {"ok": True, **self.state}

    def _run(self, allow_restart):
        try:
            result = self.runner(allow_restart)
        except subprocess.TimeoutExpired:
            result = {"state": "error", "message": "启动TD超时，请检查TD后重试"}
        except Exception as exc:
            result = {"state": "error", "message": str(exc)}
        finally:
            self.release("td-launch")
        with self.lock:
            self.state = {"id": self.state["id"], **result}
