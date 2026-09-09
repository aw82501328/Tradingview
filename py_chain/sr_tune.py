"""Persistent human-labelled SR calibration. No trading or drawing side effects.

Snapshots contain only closed bars. Search uses the regular SR generator and a
rectangular assignment solver; no price labels are injected into the engine.
"""
import copy
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone, timedelta

from . import data_loader
from .backtest import build_bis
from .chan_core import calcATR, intervalSecOf
from .sr_flip import (cluster_candidates, CLUSTER_ATR, RECENT_CLUSTER_ATR,
                      RECENT_BI_COUNT, minTouchFor, capPerPeriod)
from .sr_service import normalize_periods

VERSION = 1
DATA_DIR = Path(__file__).resolve().parents[1] / ".sr-tune"
PARAMS = ("minTouch", "clusterAtr", "recentClusterAtr", "recentBiCount")
DEFAULT_RANGES = {"minTouch": [1, 20], "clusterAtr": [.01, 5.0],
                  "recentClusterAtr": [.05, 5.0], "recentBiCount": [1, 200]}
INTEGER_PARAMS = {"minTouch", "recentBiCount"}
SHANGHAI = timezone(timedelta(hours=8))


class Conflict(ValueError):
    pass


class Cancelled(Exception):
    pass


class BudgetExpired(Exception):
    pass


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def number(value, name, *, positive=False, integer=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} 须为数字")
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} 须为数字") from None
    if not math.isfinite(n) or (positive and n <= 0) or (integer and n != int(n)):
        raise ValueError(f"{name} 须为{'正' if positive else '有限'}{'整数' if integer else '数字'}")
    return int(n) if integer else n


def parse_time(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return number(value, "时间戳", positive=True, integer=True)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int((dt if dt.tzinfo else dt.replace(tzinfo=SHANGHAI)).timestamp())
    except (ValueError, TypeError, OverflowError):
        raise ValueError("时间格式无效，请使用日期或上海时间 YYYY-MM-DDTHH:MM") from None


def price_list(values):
    if isinstance(values, str):
        values = [p for p in re.split(r"[\s,，;；]+", values.strip()) if p]
    if not isinstance(values, list) or not 1 <= len(values) <= 500:
        raise ValueError("每组样本需包含 1–500 个价位")
    return sorted(set(number(p, "价位") for p in values))


def normalize_overrides(raw):
    if not isinstance(raw, dict):
        raise ValueError("clusterParamsByPeriod 须为周期配置对象")
    out = {}
    for key, values in raw.items():
        period = normalize_periods([key])[0]
        if not isinstance(values, dict):
            raise ValueError(f"{period} 的周期参数须为对象")
        out[period] = {}
        for name in PARAMS[1:]:
            if name in values:
                out[period][name] = number(values[name], name, positive=True,
                                           integer=name in INTEGER_PARAMS)
    return out


def effective_params(cfg, period):
    specific = cfg.get("clusterParamsByPeriod", {}).get(period, {})
    return {"minTouch": int(cfg.get("minTouchs", {}).get(period, minTouchFor(period))),
            "clusterAtr": float(specific.get("clusterAtr", cfg.get("clusterAtr", CLUSTER_ATR))),
            "recentClusterAtr": float(specific.get("recentClusterAtr", cfg.get("recentClusterAtr", RECENT_CLUSTER_ATR))),
            "recentBiCount": int(specific.get("recentBiCount", cfg.get("recentBiCount", RECENT_BI_COUNT)))}


def normalize_ranges(raw=None):
    ranges = copy.deepcopy(DEFAULT_RANGES)
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("搜索范围须为对象")
    for name, bounds in (raw or {}).items():
        if name not in PARAMS or not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError("搜索范围须为参数的 [最小值, 最大值]")
        lo, hi = [number(v, name, positive=True, integer=name in INTEGER_PARAMS) for v in bounds]
        if lo > hi or hi > (1000 if name in INTEGER_PARAMS else 50):
            raise ValueError(f"{name} 范围无效或过大")
        if name not in INTEGER_PARAMS and (lo < .01 or round(lo, 2) != lo or round(hi, 2) != hi):
            raise ValueError("ATR 容差搜索边界须 >= 0.01，最多两位小数")
        ranges[name] = [lo, hi]
    return ranges


class Store:
    """JSON records + content-addressed gzip snapshots; atomic, thread-safe writes."""
    def __init__(self, root=DATA_DIR):
        self.root = Path(root)
        self.lock = threading.RLock()

    def _path(self, kind, ident):
        if kind not in ("samples", "snapshots", "jobs", "market") or not re.fullmatch(r"[a-f0-9]{32,64}", ident):
            raise ValueError("无效记录标识")
        return self.root / kind / (ident + (".json.gz" if kind in ("snapshots", "market") else ".json"))

    def put(self, kind, ident, obj):
        with self.lock:
            path = self._path(kind, ident)
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if path.suffix == ".gz":
                raw = gzip.compress(raw)
            temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with open(temp, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)

    def get(self, kind, ident):
        with self.lock:
            path = self._path(kind, ident)
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                raise ValueError("记录不存在") from None
            if path.suffix == ".gz":
                raw = gzip.decompress(raw)
            return json.loads(raw)

    def list(self, kind):
        with self.lock:
            records = [self.get(kind, p.stem) for p in (self.root / kind).glob("*.json")]
            return sorted(records, key=lambda r: r.get("createdAt", 0), reverse=True)

    def samples(self, symbol, period=None, enabled_only=False):
        return [s for s in self.list("samples") if s["symbol"] == symbol and not s.get("deleted")
                and (period is None or s["period"] == period) and (not enabled_only or s["enabled"])]


def closed_snapshot(bars, symbol, period, from_ts, cutoff, now=None):
    """Cut first, then rebuild. Never use future bars to construct historic strokes."""
    now = int(time.time()) if now is None else now
    if not symbol or cutoff > now or cutoff <= from_ts:
        raise ValueError("品种不能为空，截止时间须晚于起始日期且不能在未来")
    interval = intervalSecOf(period)
    cleaned = {}
    for raw in bars:
        t = number(raw.get("time"), "K线时间", positive=True, integer=True)
        if from_ts <= t and t + interval <= cutoff:
            b = {k: number(raw.get(k), f"K线 {k}") for k in ("open", "high", "low", "close")}
            b["time"] = t
            if b["high"] < max(b["low"], b["open"], b["close"]) or b["low"] > min(b["open"], b["close"]):
                raise ValueError("K线 OHLC 数据无效")
            cleaned[t] = b
    closed = sorted(cleaned.values(), key=lambda b: b["time"])
    if len(closed) < 14:
        raise ValueError("该时间窗口的已收盘K线不足 14 根，请扩大范围")
    atr = calcATR(closed, 14)
    bis = build_bis({period: closed}, [period]).get(period, [])
    if len(bis) < 3 or not math.isfinite(atr) or atr <= 0:
        raise ValueError("快照不足 3 笔或 ATR 无效，无法校准；请扩大时间范围")
    snap = {"schemaVersion": VERSION, "symbol": symbol, "period": period,
            "from": from_ts, "cutoff": cutoff, "actualFrom": closed[0]["time"],
            "actualTo": closed[-1]["time"] + interval, "bars": closed,
            "barCount": len(closed), "biCount": len(bis), "atr": atr,
            "partial": closed[0]["time"] > from_ts + interval,
            "closeRule": "bar.time + intervalSec <= cutoff"}
    snap["id"] = digest(snap)
    return snap


def snapshot_meta(snap):
    return {k: v for k, v in snap.items() if k != "bars"}


def capture_snapshot(store, cfg, period, cutoff, log=None, refresh=False):
    """Only use verified, symbol-labelled data. Legacy shared cache is never read."""
    symbol = cfg["symbol"].strip().upper()
    from_ts = parse_time(cfg["from"])
    cutoff = parse_time(cutoff)
    if cutoff > time.time() or cutoff <= from_ts:
        raise ValueError("截止时间须晚于起始日期且不能在未来")
    key = digest({"symbol": symbol, "period": period})
    try:
        cached = store.get("market", key)
    except ValueError:
        cached = {}
    bars = cached.get("bars", [])
    suitable = (cached.get("symbol") == symbol and cached.get("period") == period
                and bars and bars[0]["time"] <= from_ts + intervalSecOf(period)
                and bars[-1]["time"] + intervalSecOf(period) >= cutoff)
    if refresh or not suitable:
        fetched = data_loader.fetch_bars(cfg=data_loader.CDPConfig(periods=[period]),
                                         from_ts=from_ts, cache=False, symbol=symbol,
                                         log=log, verify_symbol=True)
        bars = fetched.get(period, [])
        if not bars:
            raise ValueError("未能取得该品种和周期的数据")
        bars = data_loader._dedup_sorted(bars)
        store.put("market", key, {"symbol": symbol, "period": period, "bars": bars,
                                  "fetchedAt": int(time.time())})
    snap = closed_snapshot(bars, symbol, period, from_ts, cutoff)
    store.put("snapshots", snap["id"], snap)
    bis = build_bis({period: snap["bars"]}, [period])[period]
    candidates = cluster_candidates(bis, snap["bars"], snap["atr"],
                                    **effective_params(cfg, period),
                                    clusterParts=cfg.get("clusterParts", ["flip", "recent"]))
    return {"snapshot": snapshot_meta(snap), "candidates": candidates,
            "suggestedTolerance": max(round(.1 * snap["atr"], 6), .000001)}


def save_sample(store, body):
    with store.lock:
        if body.get("id"):
            sample = store.get("samples", body["id"])
            if sample.get("deleted") or body.get("version") != sample["version"]:
                raise Conflict("样本已变更，请刷新后重试")
            if "snapshotId" in body and body["snapshotId"] != sample["snapshotId"]:
                raise ValueError("既有样本的行情不可修改，请新建快照")
            sample = dict(sample)
        else:
            snap = store.get("snapshots", str(body.get("snapshotId", "")))
            if snap["partial"] and body.get("acceptPartial") is not True:
                raise ValueError("起始历史覆盖不足，请核对实际时间范围并确认使用")
            sample = {"id": uuid.uuid4().hex, "snapshotId": snap["id"],
                      "symbol": snap["symbol"], "period": snap["period"],
                      "snapshot": snapshot_meta(snap), "version": 0,
                      "createdAt": time.time(), "enabled": True}
        if "prices" in body:
            sample["prices"] = price_list(body["prices"])
        if "tolerance" in body:
            sample["tolerance"] = number(body["tolerance"], "价格误差", positive=True)
        if "prices" not in sample or "tolerance" not in sample:
            raise ValueError("缺少人工价位或允许误差")
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise ValueError("enabled 须为布尔值")
            sample["enabled"] = body["enabled"]
        sample["name"] = str(body.get("name", sample.get("name", "")))[:80]
        sample["version"] += 1
        sample["updatedAt"] = time.time()
        store.put("samples", sample["id"], sample)
        return sample


def delete_sample(store, ident, version):
    with store.lock:
        sample = store.get("samples", ident)
        if sample["version"] != version:
            raise Conflict("样本已变更，请刷新后重试")
        sample.update(deleted=True, version=sample["version"] + 1, updatedAt=time.time())
        store.put("samples", ident, sample)


def assignment(costs, check=lambda: None):
    """Hungarian rectangular assignment, rows <= columns; deterministic tie order."""
    n, m = len(costs), len(costs[0]) if costs else 0
    u, v, p, way = [0.] * (n + 1), [0.] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        check()
        p[0], j0 = i, 0
        minv, used = [float("inf")] * (m + 1), [False] * (m + 1)
        while True:
            check()
            used[j0] = True
            i0, delta, j1 = p[j0], float("inf"), 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = costs[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if not j0:
                break
    out = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            out[p[j] - 1] = j - 1
    return out


def match_prices(targets, candidates, tolerance, check=lambda: None):
    targets = sorted(set(targets))
    # flip and recent at exactly the same price represent one position, not two.
    unique = {}
    for c in candidates:
        unique.setdefault(c["price"], c)
    cands = [unique[k] for k in sorted(unique)]
    def cost(t, c):
        d = abs(t - c["price"]) / tolerance
        return (.7 if d > 1 + 1e-10 else 0) + .1 * min(d, 3)
    # Far candidates cannot improve on an unmatched target; prune them before assignment.
    relevant = [c for c in cands if any(abs(c["price"] - t) < 3 * tolerance for t in targets)]
    costs = [[cost(t, c) for c in relevant] + [1.] * len(targets) for t in targets]
    choices = assignment(costs, check)
    rows, loss, misses, errors = [], 0., 0, []
    for i, t in enumerate(targets):
        j = choices[i]
        c = relevant[j] if j < len(relevant) else None
        delta = abs(t - c["price"]) if c else None
        hit = c is not None and delta <= tolerance * (1 + 1e-10)
        misses += not hit
        loss += costs[i][j]
        if delta is not None:
            errors.append(delta)
        rows.append({"target": t, "price": c["price"] if c else None, "delta": delta,
                     "matched": hit, "source": ("recent" if c.get("recent") else "flip") if c else None,
                     "type": c.get("type") if c else None})
    count = len(targets)
    return {"loss": loss / count if count else 1., "matchRate": 1 - misses / count if count else 0,
            "matchedCount": count - misses, "targetCount": count, "candidateCount": len(cands),
            "meanError": sum(errors) / len(errors) if errors else None,
            "maxError": max(errors) if errors else None, "unmatchedCount": misses,
            "matches": rows,
            "extraPenalty": .01 * max(0, len(cands) - (count - misses)) / max(1, len(cands) + count)}


class Evaluator:
    def __init__(self, store, samples, parts, check=lambda: None):
        self.samples, self.parts, self.check = samples, tuple(parts), check
        self.data, self.cache = {}, OrderedDict()
        for sample in samples:
            check()
            snap = store.get("snapshots", sample["snapshotId"])
            if snap["id"] != digest({k: v for k, v in snap.items() if k != "id"}):
                raise ValueError("快照校验失败，数据可能被修改")
            bis = build_bis({sample["period"]: snap["bars"]}, [sample["period"]])[sample["period"]]
            self.data[sample["id"]] = (snap, bis)

    def candidates(self, sample, params, strength=False):
        snap, bis = self.data[sample["id"]]
        if strength:
            return cluster_candidates(bis, snap["bars"], snap["atr"], **params, clusterParts=self.parts)
        result = []
        for part in self.parts:
            pkey = (params["clusterAtr"],) if part == "flip" else (params["recentClusterAtr"], params["recentBiCount"])
            key = (sample["snapshotId"], part, pkey)
            if key not in self.cache:
                self.check()
                kwargs = dict(params, minTouch=1)
                self.cache[key] = cluster_candidates(bis, snap["bars"], snap["atr"], **kwargs,
                                                     clusterParts=(part,), with_strength=False)
                if len(self.cache) > 2048:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(key)
            result.extend(c for c in self.cache[key] if part == "recent" or c["touchCount"] >= params["minTouch"])
        return result

    def evaluate(self, params, details=False, cfg=None):
        reports = []
        for sample in self.samples:
            self.check()
            report = match_prices(sample["prices"], self.candidates(sample, params), sample["tolerance"], self.check)
            if details:
                report.update(sampleId=sample["id"], version=sample["version"],
                              cutoff=sample["snapshot"]["cutoff"], tolerance=sample["tolerance"])
                full = self.candidates(sample, params, strength=True)
                cfg = cfg or {}
                capped = capPerPeriod({sample["period"]: full}, int(cfg.get("maxPerPeriod", 50)),
                                      float(cfg.get("touchWeight", .6)), float(cfg.get("barsWeight", .4)))
                kept = {c["price"] for c in capped.get(sample["period"], [])}
                for row in report["matches"]:
                    row["keptAfterCap"] = row["price"] in kept if row["price"] is not None else False
            else:
                report.pop("matches")
            reports.append(report)
        losses = [r["loss"] for r in reports]
        score = .8 * sum(losses) / len(losses) + .2 * max(losses)
        score += sum(r["extraPenalty"] for r in reports) / len(reports)
        metrics = {"score": score, "meanMatchRate": sum(r["matchRate"] for r in reports) / len(reports),
                   "worstMatchRate": min(r["matchRate"] for r in reports),
                   "targetCount": sum(r["targetCount"] for r in reports), "sampleCount": len(reports)}
        return metrics, reports


def signature(samples, cfg, period, ranges):
    # Display-only options and unrelated cycles can change without invalidating a job.
    keys = ("symbol", "from", "srTypes", "clusterParts", "maxPerPeriod", "touchWeight",
            "barsWeight", "mergeAtr", "maxDistAtr", "sideCount", "fibLevels", "bollLength", "bollMult")
    return digest({"version": VERSION, "samples": sorted((s["id"], s["version"]) for s in samples),
                   "cfg": {k: cfg.get(k) for k in keys}, "params": effective_params(cfg, period),
                   "period": period, "ranges": ranges})


def next_params(state, ranges, parts):
    """Random exploration interleaved with multi-start coordinate refinement.

    Each proposal is reproducible from the persisted cursor; no wall-time seed.
    """
    cursor = state["cursor"]
    state["cursor"] += 1
    rng = random.Random(1729 + cursor)
    active = (["minTouch", "clusterAtr"] if "flip" in parts else []) + (
        ["recentClusterAtr", "recentBiCount"] if "recent" in parts else [])
    params = dict(state["baselineParams"])
    if cursor >= 128 and cursor % 5:
        top = state["top"]
        params.update(top[(cursor // 5) % len(top)]["params"])
        name = active[(cursor // 2) % len(active)]
        scale = (cursor // (2 * len(active))) % 3
        step = ([10, 2, 1] if name == "recentBiCount" else [1, 1, 1] if name == "minTouch" else [.2, .05, .01])[scale]
        params[name] += step * (1 if cursor % 2 else -1)
    else:
        for name in active:
            lo, hi = ranges[name]
            params[name] = rng.randint(int(lo), int(hi)) if name in INTEGER_PARAMS else rng.randint(round(lo * 100), round(hi * 100)) / 100
    for name in active:
        lo, hi = ranges[name]
        params[name] = min(hi, max(lo, params[name]))
        params[name] = int(params[name]) if name in INTEGER_PARAMS else round(params[name], 2)
    return params


def param_distance(params, baseline):
    return sum(abs(params[k] - baseline[k]) / max(1, abs(baseline[k])) for k in PARAMS)


class TuneManager:
    def __init__(self, store=None, emit=None):
        self.store = store or Store()
        self.emit = emit or (lambda *_: None)
        self.lock = threading.RLock()
        self.active = None
        self.stop_event = threading.Event()
        self.thread = None
        # A server restart never silently launches or claims to still run old work.
        for job in self.store.list("jobs"):
            if job["status"] in ("running", "stopping"):
                job["status"] = "interrupted"
                self.store.put("jobs", job["id"], job)

    def public(self, job):
        return copy.deepcopy({k: v for k, v in job.items() if k not in ("search", "cfg")})

    def jobs(self, symbol=None, period=None):
        return [self.public(j) for j in self.store.list("jobs")
                if (not symbol or j.get("symbol") == symbol) and (not period or j.get("period") == period)][:30]

    def stop(self, ident):
        with self.lock:
            if self.active != ident:
                raise Conflict("任务当前未运行")
            self.stop_event.set()
            return {"stopping": True}

    def _checkpoint(self, job):
        self.store.put("jobs", job["id"], job)
        self.emit("sr_tune", {"jobId": job["id"], "status": job["status"],
                              "evaluations": job.get("evaluations", 0), "progress": job.get("progress", 0)})

    def _launch(self, job, worker, release):
        with self.lock:
            if self.active:
                raise Conflict("已有调参任务运行中")
            self.active = job["id"]
            self.stop_event.clear()
            try:
                self._checkpoint(job)
            except Exception:
                self.active = None
                raise
            def run():
                try:
                    worker(job)
                except Cancelled:
                    job["status"] = "stopped"
                except Exception as exc:
                    job["status"], job["error"] = "failed", str(exc)
                finally:
                    job["updatedAt"] = time.time()
                    try:
                        self._checkpoint(job)
                    finally:
                        with self.lock:
                            self.active = None
                        release()
            self.thread = threading.Thread(target=run, daemon=True, name="sr-tune")
            try:
                self.thread.start()
            except Exception:
                self.active = None
                raise
        return self.public(job)

    def start_capture(self, cfg, period, cutoff, refresh=False, release=lambda: None):
        job = {"id": uuid.uuid4().hex, "kind": "snapshot", "status": "running",
               "symbol": cfg["symbol"].upper(), "period": period, "createdAt": time.time()}
        def worker(j):
            def log(msg):
                self.emit("log", {"mode": "sr", "msg": str(msg)})
                if self.stop_event.is_set():
                    raise Cancelled()
            j["preview"] = capture_snapshot(self.store, cfg, period, cutoff, log, refresh)
            if self.stop_event.is_set():
                raise Cancelled()
            j["status"] = "completed"
        return self._launch(job, worker, release)

    def start_search(self, cfg, period, budget=300, ranges=None, resume=None, release=lambda: None):
        budget = number(budget, "搜索预算", positive=True)
        if budget > 600:
            raise ValueError("单次搜索预算不能超过 600 秒")
        ranges = normalize_ranges(ranges)
        if "cluster" not in cfg.get("srTypes", []) or not cfg.get("clusterParts"):
            raise ValueError("请开启密集区及至少一种子类型")
        samples = self.store.samples(cfg["symbol"].upper(), period, True)
        if not samples:
            raise ValueError("该品种和周期没有启用的人工样本")
        sig = signature(samples, cfg, period, ranges)
        if resume:
            job = self.store.get("jobs", resume)
            if job["kind"] != "search" or job["signature"] != sig:
                raise Conflict("样本、参数或搜索范围已变更，请重新开始优化")
            if job["status"] in ("running", "stopping"):
                raise Conflict("任务仍在运行")
            job["status"] = "running"
            job.pop("error", None)
        else:
            baseline = effective_params(cfg, period)
            job = {"id": uuid.uuid4().hex, "kind": "search", "status": "running",
                   "symbol": cfg["symbol"].upper(), "period": period, "createdAt": time.time(),
                   "signature": sig, "ranges": ranges, "cfg": copy.deepcopy(cfg),
                   "search": {"cursor": 0, "seen": [], "baselineParams": baseline, "top": []},
                   "evaluations": 0, "elapsedSeconds": 0, "sampleVersions": {s["id"]: s["version"] for s in samples}}
        job["budgetSeconds"], job["progress"] = budget, 0
        def worker(j):
            started, previous = time.monotonic(), j.get("elapsedSeconds", 0)
            deadline = None
            def check():
                if self.stop_event.is_set():
                    raise Cancelled()
                if deadline is not None and time.monotonic() >= deadline:
                    raise BudgetExpired()
            evaluator = Evaluator(self.store, samples, cfg["clusterParts"], check)
            state = j["search"]
            if not state["top"]:
                metrics, reports = evaluator.evaluate(state["baselineParams"], True, cfg)
                j["baseline"] = {"params": state["baselineParams"], "metrics": metrics, "samples": reports}
                j["best"] = copy.deepcopy(j["baseline"])
                state["top"] = [{"params": state["baselineParams"], "score": metrics["score"]}]
            seen = set(state["seen"])
            last_save = time.monotonic()
            deadline = started + budget
            try:
                while time.monotonic() - started < budget:
                    check()
                    proposal = next_params(state, ranges, cfg["clusterParts"])
                    key = json.dumps(proposal, sort_keys=True)
                    if key in seen:
                        # Fixed or exhausted ranges should not spin on a CPU indefinitely.
                        if state["cursor"] - j.get("lastUniqueCursor", 0) > 3000:
                            j["exhausted"] = True
                            break
                        continue
                    rank = lambda item: (round(item["score"], 12), param_distance(item["params"], state["baselineParams"]))
                    pending_best = None
                    try:
                        metrics, _ = evaluator.evaluate(proposal)
                        candidate = {"params": proposal, "score": metrics["score"]}
                        if rank(candidate) < rank(state["top"][0]):
                            detailed, reports = evaluator.evaluate(proposal, True, cfg)
                            pending_best = {"params": proposal, "metrics": detailed, "samples": reports}
                    except (Cancelled, BudgetExpired):
                        # Resume must retry a partially evaluated proposal, not skip it.
                        state["cursor"] -= 1
                        raise
                    seen.add(key)
                    j["lastUniqueCursor"] = state["cursor"]
                    j["evaluations"] += 1
                    if pending_best is not None:
                        j["best"] = pending_best
                    state["top"] = sorted(state["top"] + [candidate], key=rank)[:8]
                    if time.monotonic() - last_save >= 1:
                        state["seen"] = sorted(seen)
                        j["elapsedSeconds"] = previous + time.monotonic() - started
                        j["progress"] = min(99, int((time.monotonic() - started) / budget * 100))
                        self._checkpoint(j)
                        last_save = time.monotonic()
                j["status"], j["progress"] = "completed", 100
            except BudgetExpired:
                j["status"], j["progress"] = "completed", 100
            finally:
                state["seen"] = sorted(seen)
                j["elapsedSeconds"] = previous + time.monotonic() - started
        return self._launch(job, worker, release)

    def apply(self, ident, cfg):
        with self.lock, self.store.lock:
            job = self.store.get("jobs", ident)
            if job["kind"] != "search" or "best" not in job or job["status"] in ("running", "stopping"):
                raise Conflict("任务尚未产生可应用的结果")
            samples = self.store.samples(cfg["symbol"].upper(), job["period"], True)
            if signature(samples, cfg, job["period"], job["ranges"]) != job["signature"]:
                raise Conflict("样本或当前参数已变更，请重新优化后应用")
            out = copy.deepcopy(cfg)
            p = job["period"]
            params = job["best"]["params"]
            out.setdefault("minTouchs", {})[p] = params["minTouch"]
            out.setdefault("clusterParamsByPeriod", {})[p] = {k: params[k] for k in PARAMS[1:]}
            if p not in out["periods"]:
                out["periods"] = normalize_periods(out["periods"] + [p])
            return out

    def compatible(self, ident, cfg):
        with self.store.lock:
            job = self.store.get("jobs", ident)
            if job["kind"] != "search":
                return False
            samples = self.store.samples(cfg["symbol"].upper(), job["period"], True)
            return signature(samples, cfg, job["period"], job["ranges"]) == job["signature"]
