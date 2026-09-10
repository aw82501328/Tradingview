"""Serialized WEB analysis jobs. No scheduler starts until the user enables it."""
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid

from .data_loader import CDPClient, CDPConfig
from . import sr_service, sr_draw
from .monitor import replay_started

ROOT = Path(__file__).resolve().parent.parent
ORDER = ["bi", "points", "zs", "sr", "plan", "entry"]
DEPENDENCIES = {"bi": [], "points": ["bi"], "zs": ["bi"], "sr": ["bi"],
                "plan": ["bi"], "entry": ["bi", "sr", "plan"]}
SCRIPTS = {"bi": ("chan-bi", "chan_bi"), "points": ("mark-buy-sell", "mark_buy_sell"),
           "zs": ("chan-zs", "chan_zs"), "plan": ("trading-plan", "trading_plan"),
           "entry": ("mark-entry", "mark_entry")}
PREFIX = {"bi": "bis", "zs": "zs", "sr": "srflip", "plan": "plan", "entry": "entry"}
PERIODS = ["D", "240", "60", "15", "3"]


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def dependency_order(step):
    if step == "all":
        return ORDER[:]
    if step not in DEPENDENCIES:
        raise ValueError("未知分析模块")
    found = set()
    def visit(key):
        for dep in DEPENDENCIES[key]:
            visit(dep)
        found.add(key)
    visit(step)
    return [key for key in ORDER if key in found]


def symbol_key(symbol):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", symbol)


def list_targets(port=9222):
    with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json", timeout=3) as response:
        pages = json.load(response)
    result = []
    for page in pages:
        if page.get("type") != "page" or "tradingview.com/chart/" not in page.get("url", ""):
            continue
        try:
            with CDPClient(CDPConfig(port=port, target_id=page["id"]), log=lambda *_: None) as c:
                value = c.evaluate("({symbol:TradingViewApi.activeChart().symbol(),resolution:String(TradingViewApi.activeChart().resolution())})")
                value["replay"] = replay_started(c)
            result.append({"id": page["id"], "title": page.get("title"), "url": page["url"], **value})
        except Exception as exc:
            result.append({"id": page["id"], "title": page.get("title"), "error": str(exc)})
    return result


class AnalysisManager:
    def __init__(self, emit, acquire, release, marks_lock, normalize_sr, publish_sr,
                 storage=None, stage_runner=None, target_reader=None):
        self.emit, self.acquire, self.release = emit, acquire, release
        self.marks_lock, self.normalize_sr, self.publish_sr = marks_lock, normalize_sr, publish_sr
        self.storage = Path(storage or ROOT / ".cache" / "analysis")
        self.stage_runner = stage_runner or self._execute_stage
        self.target_reader = target_reader or self._target
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.shutdown = threading.Event()
        self.scheduler = None
        self.thread = None
        self.auto = False
        self.next_at = None
        self.cfg = {"from": "2026-06-30", "intervalMinutes": 5, "port": 9222,
                    "with30s": False, "keep": 10, "near": 1.0}
        self.job = None
        self.last_success = None
        self.waiting = None
        try:
            saved = json.loads((self.storage / "state.json").read_text(encoding="utf-8"))
            self.cfg.update(saved.get("cfg", {}))
            self.last_success = saved.get("lastSuccess")
            self.job = saved.get("job")
            if self.job and self.job.get("state") in ("running", "stopping"):
                self.job["state"] = "interrupted"
                self.job["error"] = "服务重启中断了任务，请重新更新；自动模式已关闭"
            if self.job and self.job.get("state") in ("error", "interrupted", "stopped"):
                for stage in self.job.get("stages", {}).values():
                    if stage.get("state") in ("pending", "running"):
                        stage["state"] = "blocked"
        except (OSError, ValueError):
            pass

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({"cfg": self.cfg, "auto": self.auto, "nextAt": self.next_at,
                                  "waiting": self.waiting, "job": self.job,
                                  "resultsStale": bool(self.job and self.job.get("cfg") != self.cfg),
                                  "lastSuccess": self.last_success})

    def _save(self):
        atomic_json(self.storage / "state.json", self.snapshot())

    def _changed(self):
        self._save()
        self.emit("analysis", self.snapshot())

    def configure(self, cfg):
        if not isinstance(cfg, dict):
            raise ValueError("配置须为对象")
        candidate = {**self.cfg, **copy.deepcopy(cfg)}
        date = dt.date.fromisoformat(str(candidate.get("from", "")))
        candidate["from"] = date.isoformat()
        interval = float(candidate.get("intervalMinutes", 5))
        if not math.isfinite(interval) or not 1 <= interval <= 1440:
            raise ValueError("自动间隔须为1至1440分钟")
        candidate["intervalMinutes"] = interval
        candidate["port"] = int(candidate.get("port", 9222))
        if not 1 <= candidate["port"] <= 65535:
            raise ValueError("CDP端口不合法")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(candidate.get("targetId", ""))):
            raise ValueError("请选择目标TradingView图表")
        if not re.fullmatch(r"[A-Za-z0-9_:.!/-]{1,100}", str(candidate.get("symbol", ""))):
            raise ValueError("缺少或无效品种")
        candidate["keep"] = int(candidate.get("keep", 10))
        candidate["near"] = float(candidate.get("near", 1))
        if not 1 <= candidate["keep"] <= 100 or not math.isfinite(candidate["near"]) or candidate["near"] <= 0:
            raise ValueError("标记数量须为1至100；近支阻阈值须大于0")
        candidate["with30s"] = candidate.get("with30s") is True
        sr = copy.deepcopy(candidate.get("sr") or {"srTypes": ["cluster", "boll"]})
        sr.update(symbol=candidate["symbol"], **{"from": candidate["from"]})
        sr = self.normalize_sr(sr)
        if any(p not in PERIODS for p in sr["periods"]):
            raise ValueError("完整分析支持D/240/60/15/3，请选择不含周线的支阻预设")
        candidate["sr"] = sr
        # JSON serialization also rejects non-finite nested preset values.
        json.dumps(candidate, allow_nan=False)
        with self.lock:
            self.cfg = candidate
            if self.auto:
                self.next_at = time.time() + interval * 60
            self._changed()
        return self.snapshot()

    def enable_auto(self, enabled):
        with self.lock:
            if enabled:
                self.configure(self.cfg)
            self.auto = bool(enabled)
            self.waiting = None
            self.next_at = time.time() if enabled else None
            if enabled and self.scheduler is None:
                self.scheduler = threading.Thread(target=self._schedule_loop, daemon=True, name="analysis-scheduler")
                self.scheduler.start()
            self._changed()
        return self.snapshot()

    def _schedule_loop(self):
        while not self.shutdown.wait(1):
            self.poll()

    def poll(self):
        with self.lock:
            if not self.auto or self.next_at is None or time.time() < self.next_at:
                return
            if self.thread and self.thread.is_alive():
                return  # one due timestamp, no queue of missed intervals
        result = self.start("all", automatic=True)
        if not result.get("ok"):
            with self.lock:
                self.waiting = result.get("error")
                self.next_at = time.time() + 10
                self._changed()

    def start(self, step="all", automatic=False):
        stages = dependency_order(step)
        with self.lock:
            if self.thread and self.thread.is_alive():
                return {"ok": False, "error": "分析任务正在运行"}
            self.configure(self.cfg)
            holder = self.acquire("analysis")
            if holder is not True:
                return {"ok": False, "error": f"等待 {holder} 任务结束"}
            if not self.marks_lock.acquire(blocking=False):
                self.release("analysis")
                return {"ok": False, "error": "等待图表操作结束"}
            try:
                ident = uuid.uuid4().hex
                self.stop_event.clear()
                self.waiting = None
                self.job = {"id": ident, "state": "running", "step": step, "startedAt": time.time(),
                            "cfg": copy.deepcopy(self.cfg), "error": None, "logs": [],
                            "stages": {key: {"state": "pending" if key in stages else "stale"} for key in ORDER}}
                self.next_at = None
                self._changed()
                self.thread = threading.Thread(target=self._run, args=(copy.deepcopy(self.job), stages),
                                               daemon=True, name="analysis-" + ident[:8])
                self.thread.start()
            except Exception:
                self.marks_lock.release()
                self.release("analysis")
                raise
        return {"ok": True, "jobId": ident}

    def stop(self):
        with self.lock:
            self.auto = False
            self.next_at = None
            self.waiting = None
            self.stop_event.set()
            if self.thread and self.thread.is_alive():
                self.job["state"] = "stopping"
            self._changed()
        return self.snapshot()

    def _log(self, msg):
        with self.lock:
            self.job["logs"] = (self.job["logs"] + [str(msg)])[-120:]
        self.emit("analysis_log", {"jobId": self.job["id"], "message": str(msg)})

    def _target(self, cfg, restore=None):
        config = CDPConfig(port=cfg["port"], target_id=cfg["targetId"], expected_symbol=cfg["symbol"])
        with CDPClient(config, log=lambda *_: None) as c:
            if restore:
                c.evaluate("TradingViewApi.activeChart().setResolution(" + json.dumps(restore) + ");")
            elif replay_started(c):
                raise RuntimeError("目标图表处于历史回放，请先退出TradingView回放，再更新当前行情")
            return c.evaluate("String(TradingViewApi.activeChart().resolution())")

    def _run(self, job, stages):
        original = None
        folder = self.storage / "runs" / job["id"]
        current = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            original = self.target_reader(job["cfg"])
            for current in stages:
                if self.stop_event.is_set():
                    break
                self.target_reader(job["cfg"])
                with self.lock:
                    self.job["stages"][current] = {"state": "running", "startedAt": time.time()}
                    self._changed()
                result = self.stage_runner(current, job["cfg"], folder, self._log)
                result["inputRunId"] = job["id"]
                snapshot_file = folder / f"bis_{symbol_key(job['cfg']['symbol'])}.json"
                if snapshot_file.exists():
                    snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
                    result["dataEnd"] = {p: bars[-1]["time"] for p, bars in snapshot.get("bars", {}).items() if bars}
                self.target_reader(job["cfg"])
                with self.lock:
                    self.job["stages"][current].update(state="success", finishedAt=time.time(), result=result)
                    self._changed()
            if self.stop_event.is_set():
                with self.lock:
                    self.job["state"] = "stopped"
            else:
                self._promote(folder, job, stages)
                with self.lock:
                    self.job["state"] = "success"
                    if job["step"] == "all":
                        self.last_success = {"id": job["id"], "at": time.time(), "symbol": job["cfg"]["symbol"]}
        except Exception as exc:
            with self.lock:
                self.job.update(state="error", error=str(exc))
                if current:
                    self.job["stages"][current].update(state="error", error=str(exc))
                # Fail closed: restarting automatic updates requires an explicit action.
                self.auto = False
                self.next_at = None
            self._log("分析失败：" + str(exc))
        finally:
            if original:
                try:
                    self.target_reader(job["cfg"], restore=original)
                except Exception as exc:
                    self._log("原周期恢复失败：" + str(exc))
                    with self.lock:
                        self.job.update(state="error", error="原周期恢复失败：" + str(exc))
                        self.auto = False
            self.marks_lock.release()
            self.release("analysis")
            with self.lock:
                self.job["finishedAt"] = time.time()
                for stage in self.job["stages"].values():
                    if stage["state"] == "pending":
                        stage["state"] = "blocked" if self.job["state"] == "error" else "stopped"
                if self.auto:
                    self.next_at = time.time() + self.cfg["intervalMinutes"] * 60
                self._changed()

    def _execute_stage(self, stage, cfg, folder, log):
        key = symbol_key(cfg["symbol"])
        if stage == "sr":
            payload = json.loads((folder / f"bis_{key}.json").read_text(encoding="utf-8"))
            sr_cfg = cfg["sr"]
            result, meta = sr_service.build_chain_result(payload["bars"], sr_cfg, log,
                                                        bis_by_period=payload["periods"])
            draw = sr_draw.draw_sr_lines(
                main_by_period=sr_service.main_lines(result),
                raw_by_period=sr_service.raw_pool_lines(result, sr_cfg.get("maxDistAtr", 3)) if sr_cfg.get("draw_raw") else None,
                cfg=CDPConfig(port=cfg["port"], target_id=cfg["targetId"], expected_symbol=cfg["symbol"]),
                color=sr_cfg.get("color", "#787B86"), draw_text=sr_cfg.get("draw_text", True), log=log)
            if draw["errors"] or draw["skipped"]:
                raise RuntimeError(f"支阻位绘图不完整：{draw}")
            atomic_json(folder / f"srflip_{key}.json", {"symbol": cfg["symbol"],
                        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(), **result})
            self.publish_sr(sr_cfg, result, meta)
            return {"count": len(result["merged"]), "drawing": draw, "meta": meta}
        skill, script = SCRIPTS[stage]
        report = folder / (stage + "_report.json")
        env = {**os.environ, "CHAN_TARGET": cfg["targetId"], "CHAN_SYMBOL": cfg["symbol"],
               "CHAN_PORT": str(cfg["port"]), "CHAN_REPORT": str(report), "CHAN_CACHE_DIR": str(folder)}
        command = [shutil.which("node") or "node", "--require", str(ROOT / "py_chain" / "analysis_bridge.cjs"),
                   str(ROOT / ".cursor" / "skills" / skill / "scripts" / (script + ".js")), "--from=" + cfg["from"]]
        if cfg["with30s"] and stage in ("bi", "entry"):
            command.append("--with-30s")
        if stage == "points":
            command.append("--keep=" + str(cfg["keep"]))
        if stage == "entry":
            command.append("--near=" + str(cfg["near"]))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        # Output is drained in a separate reader so a hung CDP cannot defeat timeout.
        with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace", creationflags=flags) as proc:
            def read_output():
                with (folder / (stage + ".log")).open("w", encoding="utf-8") as output:
                    for line in proc.stdout:
                        output.write(line)
                        log(line.rstrip())
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            try:
                code = proc.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                raise RuntimeError("模块执行超过30分钟，已终止；图表可能部分更新")
            finally:
                reader.join(timeout=10)
        if code or not report.exists():
            raise RuntimeError(f"{skill} 执行失败（退出码 {code}），请查看日志")
        status = json.loads(report.read_text(encoding="utf-8"))
        if not status["ok"]:
            raise RuntimeError("；".join(status["errors"])[-2000:])
        if stage == "points":
            return {"message": "买卖点脚本执行完成；详见运行日志", "report": status}
        data = json.loads((folder / f"{PREFIX[stage]}_{key}.json").read_text(encoding="utf-8"))
        if data.get("symbol") != cfg["symbol"]:
            raise RuntimeError("模块结果品种不一致")
        periods = data.get("periods") or {}
        if stage == "bi" and any(p not in periods or not data.get("bars", {}).get(p) for p in PERIODS):
            raise RuntimeError("画笔没有覆盖全部必需周期，后续步骤已停止")
        counts = {p: len(v) if isinstance(v, list) else 1 for p, v in periods.items()}
        return {"counts": counts, "generatedAt": data.get("generatedAt"),
                "dataEnd": {p: b[-1]["time"] for p, b in data.get("bars", {}).items() if b}}

    def _promote(self, folder, job, stages):
        key = symbol_key(job["cfg"]["symbol"])
        previous = folder / "previous"
        previous.mkdir(exist_ok=True)
        published = []
        try:
            self._publish_files(folder, job, stages, key, previous, published)
        except Exception:
            for dst, existed in reversed(published):
                if existed:
                    os.replace(previous / dst.name, dst)
                else:
                    dst.unlink(missing_ok=True)
            raise

    def _publish_files(self, folder, job, stages, key, previous, published):
        for stage in stages:
            if stage not in PREFIX:
                continue
            src = folder / f"{PREFIX[stage]}_{key}.json"
            if not src.exists():
                continue
            data = json.loads(src.read_text(encoding="utf-8"))
            data["analysisRunId"] = job["id"]
            data["analysisConfig"] = job["cfg"]
            data.pop("bars", None)  # Full inputs remain in the run folder.
            dst = ROOT / ".cursor" / "cache" / src.name
            existed = dst.exists()
            if existed:
                shutil.copy2(dst, previous / dst.name)
            published.append((dst, existed))
            atomic_json(dst, data)

    def result(self, stage):
        if stage not in PREFIX:
            return {"message": "此模块结果请查看图表与运行记录"}
        with self.lock:
            job = copy.deepcopy(self.job)
        if not job or job["stages"][stage].get("state") != "success":
            return {"stale": True, "message": "当前任务尚无此模块的成功结果"}
        path = self.storage / "runs" / job["id"] / f"{PREFIX[stage]}_{symbol_key(job['cfg']['symbol'])}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("bars", None)
        return {"jobId": job["id"], "partial": job["state"] != "success", "data": data}

    def close(self):
        self.shutdown.set()
        self.stop_event.set()
        self.auto = False
