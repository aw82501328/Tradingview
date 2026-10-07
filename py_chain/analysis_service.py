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
from . import sr_service, sr_draw, param_center, module_registry
from .monitor import replay_started

ROOT = Path(__file__).resolve().parent.parent
# 流程顺序与依赖来自模块分层注册表。工作台「更新全部」(step=all) 只跑
# 基础组件 01/02/03（bi/zs/points）；单模块仍按依赖补齐。
ORDER = module_registry.order_of()
DEPENDENCIES = module_registry.dependencies_of()
SCRIPTS = {"bi": ("chan-bi", "chan_bi"), "points": ("mark-buy-sell", "mark_buy_sell"),
           "zs": ("chan-zs", "chan_zs"), "plan": ("trading-plan", "trading_plan"),
           "entry": ("mark-entry", "mark_entry"),
           "fxma_entry": ("fxma-entry", "fxma_entry")}
PREFIX = {"bi": "bis", "zs": "zs", "sr": "srflip", "plan": "plan", "entry": "entry",
          "fxma_entry": "fxma"}
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


def dependency_order(step, strategy=None):
    order = module_registry.order_of(strategy)
    deps = module_registry.dependencies_of(strategy)
    # 更新全部：只跑基础结构 01 画笔、02 画中枢、03 标记买卖点
    if step == "all":
        return list(module_registry.BASE_STAGES)
    if step not in deps:
        raise ValueError("未知分析模块")
    found = set()
    def visit(key):
        for dep in deps[key]:
            visit(dep)
        found.add(key)
    visit(step)
    return [key for key in order if key in found]


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
                    "with30s": False, "keep": 10, "strategy": module_registry.DEFAULT_STRATEGY,
                    "near": 10.0, "slip_stop": 3.0, "slip_fallback": 10.0, "slip_be": 3.0,
                    "slip_stop_atr_k": 0.0, "slip_fallback_atr_k": 0.0, "slip_be_atr_k": 0.0}
        self.job = None
        self.last_success = None
        self.waiting = None
        try:
            saved = json.loads((self.storage / "state.json").read_text(encoding="utf-8"))
            self.cfg.update(saved.get("cfg", {}))
            # 旧存量 state.json 无 strategy 键 → 补默认策略（缠论V1），行为不变
            self.cfg["strategy"] = module_registry.normalize_strategy(self.cfg.get("strategy"))
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
        # 交易策略（注册表校验）：空 → 默认策略；未知 → ValueError（走 400 报错路径）
        candidate["strategy"] = module_registry.normalize_strategy(candidate.get("strategy"))
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
        # keep/near/slip_*：参数配置页统一管理（参数中心为唯一编辑入口），每次 configure
        # 强制刷新为参数中心当前值 → state.json/job cfg 快照随之更新，参数变化后旧结果
        # 走现有 resultsStale 机制标记"本轮未更新"。
        pm = param_center.effective_all(candidate["symbol"])
        candidate["keep"] = int(pm["points"]["keep"])
        candidate["near"] = float(pm["entry"]["near"])
        if not 1 <= candidate["keep"] <= 100 or not math.isfinite(candidate["near"]) or candidate["near"] <= 0:
            raise ValueError("标记数量须为1至100；近支阻阈值须大于0")
        # 进出场支阻使用参数（绝对价差；entry 子进程 --slip-* 透传，与回测界面同名）
        for k in ("slip_stop", "slip_fallback", "slip_be"):
            candidate[k] = float(pm["entry"][k])
            if not math.isfinite(candidate[k]) or candidate[k] <= 0:
                raise ValueError("滑点参数须大于0")
        # 滑点 ATR 系数（2026-09-19）：有效滑点 = 固定值 + 系数×ATR(14,背驰周期)；允许 0（=关闭）
        for k in ("slip_stop_atr_k", "slip_fallback_atr_k", "slip_be_atr_k"):
            candidate[k] = float(pm["entry"][k])
            if not math.isfinite(candidate[k]) or candidate[k] < 0:
                raise ValueError("滑点ATR系数须不小于0")
        candidate["with30s"] = candidate.get("with30s") is True
        # 支阻：服务端按品种从参数中心填入（不再依赖客户端传入整份预设）
        sr = copy.deepcopy(param_center.effective_sr(candidate["symbol"]))
        # 丢掉可能残留的多选 symbols，避免 normalize 把 symbol 盖成 symbols[0]
        sr.pop("symbols", None)
        sr.update(symbol=candidate["symbol"], **{"from": candidate["from"]})
        sr = self.normalize_sr(sr)
        if any(p not in PERIODS for p in sr["periods"]):
            raise ValueError("完整分析支持D/240/60/15/3，请在参数配置中调整该品种支阻周期（勿含周线）")
        candidate["sr"] = sr
        # JSON serialization also rejects non-finite nested values.
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
        with self.lock:
            if self.thread and self.thread.is_alive():
                return {"ok": False, "error": "分析任务正在运行"}
            self.configure(self.cfg)
            # "all" = 01/02/03 基础组件；单模块按依赖补齐（未知模块 ValueError）
            stages = dependency_order(step, self.cfg.get("strategy"))
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
                            "stages": {key: {"state": "pending" if key in stages else "stale"}
                                       for key in module_registry.order_of(self.cfg.get("strategy"))}}
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

    def _prefetch_ref_bars(self, cfg, log):
        """画笔前置：3m 校准基准深历史缺段时用回放深拉补库（与 WEB 基础数据页同路径）。

        15m 笔的端点校准依赖 3m K线；TV 图表 3m 深度仅约 2 个月，更早历史由
        chan_bi.js 从 bars.db（回放深拉库）拼接。库缺段时在此自动补拉——会切图表
        周期并进出回放，必须在画笔子进程启动前串行完成（子进程运行期禁止回放态）。
        预取失败不阻断画笔（退化为图表深度内校准，由脚本如实提示）。
        """
        try:
            from . import data_store
            from .main import parse_from
            from_ts = parse_from(cfg.get("from") or "")
            if not from_ts:
                return
            win15 = int(param_center.chan_cfg_effective(cfg["symbol"]).get("windowDays15") or 0)
            need = max(from_ts, int(time.time() - win15 * 86400)) if win15 > 0 else from_ts
            segs = data_store.missing_segments(cfg["symbol"], "3", need)
            if not segs:
                return
            gaps = "、".join(
                f"{dt.datetime.fromtimestamp(s, dt.timezone.utc):%m-%d}~"
                f"{dt.datetime.fromtimestamp(e, dt.timezone.utc):%m-%d}"
                for s, e in segs[:5])
            log(f"3m 校准基准缺 {len(segs)} 段（{gaps}{'…' if len(segs) > 5 else ''}），回放深拉补库（与基础数据页同路径）…")
            data_store.fetch_and_store(cfg["symbol"], ["3"], need, mode="auto", log=log)
            left = data_store.missing_segments(cfg["symbol"], "3", need)
            log("3m 校准基准补库完成" if not left
                else f"3m 校准基准补库后仍有 {len(left)} 段缺口（数据源可能到头），画笔继续")
        except Exception as exc:
            log(f"3m 校准基准预取失败（{exc}），继续画笔（更早端点将不做低级别校准）")

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
        if stage == "bi":
            self._prefetch_ref_bars(cfg, log)
        skill, script = SCRIPTS[stage]
        report = folder / (stage + "_report.json")
        env = {**os.environ, "CHAN_TARGET": cfg["targetId"], "CHAN_SYMBOL": cfg["symbol"],
               "CHAN_PORT": str(cfg["port"]), "CHAN_REPORT": str(report), "CHAN_CACHE_DIR": str(folder)}
        command = [shutil.which("node") or "node", "--require", str(ROOT / "py_chain" / "analysis_bridge.cjs"),
                   str(ROOT / ".cursor" / "skills" / skill / "scripts" / (script + ".js")), "--from=" + cfg["from"]]
        # 参数中心（参数配置页）：--chan-cfg 透传拼合 CHAN_CFG（画笔/买卖点/进出场；
        # JS 子进程无法共享本进程全局 override，未知键在 JS 侧闲置不报错）；各模块专属参数按 stage 追加
        pm = param_center.effective_all(cfg["symbol"])
        command.append("--chan-cfg=" + json.dumps(param_center.chan_cfg_effective(cfg["symbol"]),
                                                  separators=(",", ":")))
        if cfg["with30s"] and stage in ("bi", "entry", "fxma_entry"):
            command.append("--with-30s")
        if stage == "points":
            command.append("--keep=" + str(cfg["keep"]))
            command.append("--nearp=" + str(pm["points"]["nearAtrRatio"]))
            command.append("--class2-zs-tol=" + str(pm["points"].get("class2ZsTol", 0)))
            command.append("--third-zs-tol=" + str(pm["points"].get("thirdZsTol", 0)))
        if stage == "zs":
            zs_periods, zs_keep = param_center.zs_draw_spec(pm["zs"])
            # 空列表也传参：JS 清旧中枢并落盘空结果（全部关闭时图上不残留）
            command.append("--zs-periods=" + (",".join(zs_periods) if zs_periods else ""))
            command.append("--zs-keep=" + ",".join(f"{r}:{n}" for r, n in zs_keep.items()))
        if stage == "plan":
            command.append("--range-bar-n=" + str(pm["plan"]["rangeBarN"]))
            command.append("--range-bi-n=" + str(pm["plan"]["rangeBiN"]))
            command.append("--range-k-mult=" + str(pm["plan"]["rangeKMult"]))
            command.append("--range-bi-mult=" + str(pm["plan"]["rangeBiMult"]))
            command.append("--range-break-mult=" + str(pm["plan"]["rangeBreakMult"]))
            # 两类震荡开关：True→1 / False→0（JS 侧 "0" 为关，缺省开）
            command.append("--range-bound-on=" + ("1" if pm["plan"].get("rangeBoundOn", True) else "0"))
            command.append("--range-zs-on=" + ("1" if pm["plan"].get("rangeZsOn", True) else "0"))
            # 2买/2卖 中间档容差 + 3类点强档开关（trading_plan.js RANGE_CFG 同名解析）
            command.append("--prev-high-near-pts=" + str(pm["plan"].get("prevHighNearPts", 5.0)))
            command.append("--second-near-pts=" + str(pm["plan"].get("secondNearPts", 5.0)))
            command.append("--third-strong-trend=" + ("1" if pm["plan"].get("thirdStrongTrend", False) else "0"))
        if stage == "entry":
            command.append("--near=" + str(cfg["near"]))
            command.append("--slip-stop=" + str(cfg.get("slip_stop", 3.0)))
            command.append("--slip-fallback=" + str(cfg.get("slip_fallback", 10.0)))
            command.append("--slip-be=" + str(cfg.get("slip_be", 3.0)))
            # 滑点 ATR 系数（有效滑点 = 固定值 + 系数×ATR(14,背驰周期)；0=关闭）
            command.append("--slip-stop-atr-k=" + str(cfg.get("slip_stop_atr_k", 0.0)))
            command.append("--slip-fallback-atr-k=" + str(cfg.get("slip_fallback_atr_k", 0.0)))
            command.append("--slip-be-atr-k=" + str(cfg.get("slip_be_atr_k", 0.0)))
            command.append("--exit-min-merged=" + str(pm["entry"]["exit_min_merged"]))
            command.append("--zs-weak-ratio=" + str(pm["entry"]["zs_exit_weak_ratio"]))
            # 顺势参考周期（交易计划模块；"" = 关闭）——低周期只做参考周期方向的单边
            command.append("--trend-res=" + str(pm["plan"].get("trendRes", "")))
            command.append("--trend-rebound=" + ("1" if pm["plan"].get("trendRebound", True) else "0"))
            command.append("--rebound-near-pts=" + str(pm["plan"].get("reboundNearPts", 5)))
            command.append("--rebound-angle-ref=" + str(pm["plan"].get("reboundAngleRef", 5)))
        if stage == "fxma_entry":
            # 强分型均线V1：策略参数全部来自参数中心 fxma 品种桶（工作台无策略专属数字）
            fx = pm["fxma"]
            command.append("--entry-res=" + str(fx.get("entryRes", "3,15,60")))
            command.append("--point-classes=" + str(fx.get("pointClasses", "1,2,2x,3,3x")))
            # 条件开关：True→1 / False→0（JS 侧 "0" 为关，缺省开）
            command.append("--ma-on=" + ("1" if fx.get("maOn", True) else "0"))
            command.append("--ma-type=" + str(fx.get("maType", "SMA")))
            command.append("--ma-fast-1=" + str(fx.get("maFast1", 8)))
            command.append("--ma-slow-1=" + str(fx.get("maSlow1", 20)))
            command.append("--ma-fast-2=" + str(fx.get("maFast2", 5)))
            command.append("--ma-slow-2=" + str(fx.get("maSlow2", 8)))
            command.append("--cross-min-pts=" + str(fx.get("crossMinPts", 2.0)))
            command.append("--ma-stand-on=" + ("1" if fx.get("maStandOn", True) else "0"))
            command.append("--ma-stand-1=" + str(fx.get("maStand1", 5)))
            command.append("--ma-stand-2=" + str(fx.get("maStand2", 5)))
            # 新条件开关（JS 侧 "1"=开、缺省关；显式传 0 亦为关）
            command.append("--fib-near-on=" + ("1" if fx.get("fibNearOn", False) else "0"))
            command.append("--fib-levels=" + str(fx.get("fibLevels", "0.382,0.5,0.618")))
            command.append("--fib-near-pts=" + str(fx.get("fibNearPts", 5.0)))
            command.append("--upper-dir-on=" + ("1" if fx.get("upperDirOn", False) else "0"))
            command.append("--strong-fx-on=" + ("1" if fx.get("strongFxOn", True) else "0"))
            command.append("--strong-fx-min-pts=" + str(fx.get("strongFxMinPts", 0.0)))
            command.append("--point-valid-bars=" + str(fx.get("pointValidBars", 0)))
            command.append("--point-valid-pts=" + str(fx.get("pointValidPts", 0)))
            command.append("--stop-pts=" + str(fx.get("stopPts", 10.0)))
            command.append("--tp-pts=" + str(fx.get("tpPts", 30.0)))
            command.append("--tp-mode=" + str(fx.get("tpMode", "points")))
            command.append("--tp-near-pts=" + str(fx.get("tpNearPts", 0.0)))
            command.append("--tp-trail-slip-pts=" + str(fx.get("tpTrailSlipPts", 1.0)))
            command.append("--same-bar-priority=" + str(fx.get("sameBarPriority", "stop")))
            command.append("--mutex-scope=" + str(fx.get("mutexScope", "global")))
            command.append("--lots=" + str(fx.get("lots", 4)))
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
