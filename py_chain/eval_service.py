"""EVAL 评估：成笔用例快照 + 基线回归（设计见 SPEC_eval.md）。

用例 = 某时刻本地K线缓存（bars_all_tf.json 全周期）的内容寻址快照 + 当时画笔参数，
不可变；基线 = 多选用例的期望笔结果冻结（同批用例可建多个基线做改动前后 A/B）。
跑基线用当前逻辑（backtest.build_bis，与回测/全链路同口径）重算并与冻结期望逐笔比对，
口径与 align_check.py 一致（11 字段位置比较，严格相等才通过）。
"""

import copy
import gzip
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from . import chan_core, param_center
from .backtest import build_bis
from .mark_buy_sell import compute_all_marks

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "eval"
BARS_FILE = ROOT / "bars_all_tf.json"

# 与 align_check.py 相同的逐笔比对字段
FIELDS = ["type", "startIdx", "endIdx", "startTime", "endTime",
          "startPrice", "endPrice", "rawCount", "span", "gapLocked", "macdCross"]
MARK_FIELDS = {"rawCount", "span", "gapLocked", "macdCross"}  # 展示分组用（前端）
# 买卖点标记比对字段（mark_buy_sell.compute_period_marks 输出）
MARK_CMP_FIELDS = ["label", "time", "price", "rawTime", "rawPrice", "color"]
MARKS_KWARGS = {"nearAtrRatio", "keep", "class2ZsTol", "thirdZsTol"}  # compute_all_marks 可接受的 points 模块键
MIN_BARS = 50            # 建用例时所选周期（res=ALL 即全部默认周期）截断后的最少根数
MAX_DIFFS = 50           # 每周期差异明细上限
CONTEXT_PAD = 5          # 双侧笔对照窗口（首差异前后各N行）
PERIODS = ["D", "240", "60", "15", "3"]  # build_bis 默认周期（30S 不参与成笔回归）
_PERIOD_RANK = {"D": 0, "240": 1, "120": 2, "60": 3, "30": 4, "30S": 5, "15": 6, "5": 7, "3": 8, "1": 9}


def _marks_params(cfg):
    """过滤成 compute_all_marks 可接受的 points 模块参数。"""
    return {k: v for k, v in (cfg or {}).items() if k in MARKS_KWARGS}


def align_marks(a_list, b_list):
    """双侧买卖点对齐（仅展示用）：按 (rawTime, label) 排序后配对；单侧独有标注。"""
    a = sorted(a_list, key=lambda m: (m.get("rawTime") or 0, m.get("label") or ""))
    b = sorted(b_list, key=lambda m: (m.get("rawTime") or 0, m.get("label") or ""))
    rows, i, j = [], 0, 0
    while i < len(a) or j < len(b):
        x = a[i] if i < len(a) else None
        y = b[j] if j < len(b) else None
        ka = (x.get("rawTime") or 0, x.get("label") or "") if x else None
        kb = (y.get("rawTime") or 0, y.get("label") or "") if y else None
        if x and y and ka == kb:
            rows.append({"idxA": i, "idxB": j, "a": x, "b": y}); i += 1; j += 1
        elif y is None or (x and ka < kb):
            rows.append({"idxA": i, "a": x}); i += 1
        else:
            rows.append({"idxB": j, "b": y}); j += 1
    return rows


def compare_marks(expected, actual):
    """买卖点逐位 6 字段严格比较；差异键名沿用 bi（前端按节区分笔/点）。"""
    diffs, first = [], None
    for i in range(max(len(expected), len(actual))):
        e = expected[i] if i < len(expected) else None
        a = actual[i] if i < len(actual) else None
        found = False
        if e is None or a is None:
            diffs.append({"bi": i, "field": "(点)",
                          "expected": e and "有" or "(无)", "actual": a and "有" or "(无)"})
            found = True
        else:
            for f in MARK_CMP_FIELDS:
                if e.get(f) != a.get(f):
                    diffs.append({"bi": i, "field": f, "expected": e.get(f), "actual": a.get(f)})
                    found = True
        if found and first is None:
            first = i
    out = {"ok": not diffs, "nExpected": len(expected), "nActual": len(actual),
           "firstDiff": first, "nDiffs": len(diffs), "diffs": diffs[:MAX_DIFFS]}
    if diffs:
        rows = align_marks(expected, actual)
        center = 0
        for k, r in enumerate(rows):
            paired_diff = r.get("a") and r.get("b") and any(
                r["a"].get(f) != r["b"].get(f) for f in MARK_CMP_FIELDS)
            single = ("a" in r) != ("b" in r)
            hit = r.get("idxA") == first or r.get("idxB") == first
            if paired_diff or single or hit:
                center = k
                break
        out["context"] = {"rows": rows[max(0, center - CONTEXT_PAD): center + CONTEXT_PAD + 1]}
    else:
        out["context"] = None
    return out


class BusyError(RuntimeError):
    """已有评估任务在运行。"""


def _new_id(prefix):
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def align_bis(a_list, b_list):
    """双侧笔对齐（仅展示用）：起止时间相同配对；单侧独有标注；其余按时间重叠配对。"""
    rows, i, j = [], 0, 0
    while i < len(a_list) or j < len(b_list):
        a = a_list[i] if i < len(a_list) else None
        b = b_list[j] if j < len(b_list) else None
        if a and b and a.get("startTime") == b.get("startTime") and a.get("endTime") == b.get("endTime"):
            rows.append({"idxA": i, "idxB": j, "a": a, "b": b}); i += 1; j += 1
        elif a and (not b or a["endTime"] <= b["startTime"]):
            rows.append({"idxA": i, "a": a}); i += 1
        elif b and (not a or b["endTime"] <= a["startTime"]):
            rows.append({"idxB": j, "b": b}); j += 1
        else:
            rows.append({"idxA": i, "idxB": j, "a": a, "b": b}); i += 1; j += 1
    return rows


def compare_bis(expected, actual):
    """逐笔 11 字段位置比较（严格相等）；返回差异明细 + 首差异附近的双侧对照窗口。"""
    diffs, first = [], None
    for i in range(max(len(expected), len(actual))):
        e = expected[i] if i < len(expected) else None
        a = actual[i] if i < len(actual) else None
        found = False
        if e is None or a is None:
            diffs.append({"bi": i, "field": "(笔)",
                          "expected": e and "有" or "(无)", "actual": a and "有" or "(无)"})
            found = True
        else:
            for f in FIELDS:
                if e.get(f) != a.get(f):
                    diffs.append({"bi": i, "field": f, "expected": e.get(f), "actual": a.get(f)})
                    found = True
        if found and first is None:
            first = i
    out = {"ok": not diffs, "nExpected": len(expected), "nActual": len(actual),
           "firstDiff": first, "nDiffs": len(diffs), "diffs": diffs[:MAX_DIFFS]}
    if diffs:
        rows = align_bis(expected, actual)
        center = 0
        for k, r in enumerate(rows):
            paired_diff = r.get("a") and r.get("b") and any(
                r["a"].get(f) != r["b"].get(f) for f in FIELDS)
            single = ("a" in r) != ("b" in r)
            hit = r.get("idxA") == first or r.get("idxB") == first
            if paired_diff or single or hit:
                center = k
                break
        out["context"] = {"rows": rows[max(0, center - CONTEXT_PAD): center + CONTEXT_PAD + 1]}
    else:
        out["context"] = None
    return out


class EvalManager:
    """用例/基线存储 + 后台评估任务（同一时刻只允许一个任务）。"""

    def __init__(self, data_dir=None, bars_file=None):
        self.data_dir = Path(data_dir or DATA_DIR)
        self.bars_file = Path(bars_file or BARS_FILE)
        self.snap_dir = self.data_dir / "snapshots"
        self.case_dir = self.data_dir / "cases"
        self.bl_dir = self.data_dir / "baselines"
        for d in (self.snap_dir, self.case_dir, self.bl_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._job_lock = threading.Lock()   # 同一时刻仅一个任务
        self._state_lock = threading.Lock()
        self._job = None

    # ---------------- 基础信息 ----------------

    def bars_info(self):
        """当前本地K线缓存概况（建用例表单显示）。"""
        if not self.bars_file.exists():
            return {"available": False, "path": str(self.bars_file)}
        try:
            data = _read_json(self.bars_file)
        except (OSError, json.JSONDecodeError) as exc:
            return {"available": False, "path": str(self.bars_file), "error": str(exc)}
        periods = {}
        for res in sorted(data, key=lambda r: (_PERIOD_RANK.get(r, 99), r)):
            bars = data[res] or []
            if not bars:
                continue
            periods[res] = {"count": len(bars), "first": bars[0]["time"], "last": bars[-1]["time"]}
        return {"available": True, "path": str(self.bars_file), "periods": periods}

    # ---------------- 用例 ----------------

    def create_case(self, payload, symbol=None):
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("用例名称不能为空")
        res = str(payload.get("res") or "").strip()
        from_ts = payload.get("fromTs")
        to_ts = payload.get("toTs")
        if from_ts is not None and not isinstance(from_ts, (int, float)):
            raise ValueError("fromTs 须为时间戳")
        if to_ts is not None and not isinstance(to_ts, (int, float)):
            raise ValueError("toTs 须为时间戳")
        if from_ts and to_ts and to_ts <= from_ts:
            raise ValueError("结束时间须晚于起始时间")
        if not self.bars_file.exists():
            raise ValueError(f"本地K线缓存不存在（{self.bars_file.name}），请先在「基础数据」页拉取")
        data = _read_json(self.bars_file)
        avail = {r for r, v in data.items() if v}
        if res == "ALL":
            pass
        elif res in PERIODS and res in avail:
            pass
        else:
            raise ValueError("周期无效或缓存中无该周期K线")
        if from_ts or to_ts:
            data = {r: [b for b in bars if (not from_ts or b["time"] >= from_ts)
                        and (not to_ts or b["time"] <= to_ts)]  # toTs 按K线开盘时刻计（含）
                        for r, bars in data.items()}
        # 只校验所选周期（评估/比对仅覆盖它）；未选周期不设下限——build_bis 对
        # <6 根的周期自动跳过、上级锁随之缺省，同快照下仍逐位可复现
        check_periods = PERIODS if res == "ALL" else [res]
        short = [r for r in check_periods if len(data.get(r) or []) < MIN_BARS]
        if short:
            detail = "、".join(f"{r}仅{len(data.get(r) or [])}根" for r in short)
            raise ValueError(f"截断后K线不足（所选周期每周期至少{MIN_BARS}根）：{detail}")
        payload_json = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(payload_json).hexdigest()[:16]
        snap_file = self.snap_dir / f"{digest}.json.gz"
        if not snap_file.exists():
            with gzip.open(snap_file, "wt", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        case = {
            "id": _new_id("c"), "name": name, "symbol": symbol, "res": res,
            "fromTs": from_ts, "toTs": to_ts, "snapshot": digest,
            "chanCfg": param_center.chan_cfg_effective(symbol),
            "pointsCfg": param_center.effective("points", symbol) or {},
            "barCounts": {r: len(v) for r, v in sorted(
                data.items(), key=lambda kv: _PERIOD_RANK.get(kv[0], 99)) if v},
            "createdAt": time.time(),
        }
        _write_json(self.case_dir / f"{case['id']}.json", case)
        return {"case": case}

    def list_cases(self):
        out = []
        for p in self.case_dir.glob("c_*.json"):
            try:
                c = _read_json(p)
            except (OSError, json.JSONDecodeError):
                continue
            c["snapshotOk"] = (self.snap_dir / f"{c.get('snapshot')}.json.gz").exists()
            out.append(c)
        out.sort(key=lambda c: c.get("createdAt") or 0, reverse=True)
        return out

    def _case(self, case_id):
        p = self.case_dir / f"{case_id}.json"
        if not p.exists():
            raise ValueError(f"用例不存在：{case_id}")
        return _read_json(p)

    def delete_case(self, case_id):
        self._case(case_id)
        refs = [b["name"] for b in self.list_baselines() if case_id in b.get("caseIds", [])]
        if refs:
            raise ValueError("用例被基线引用，请先删除或重建基线：" + "、".join(refs))
        (self.case_dir / f"{case_id}.json").unlink()

    def _load_snapshot(self, digest):
        path = self.snap_dir / f"{digest}.json.gz"
        if not path.exists():
            raise ValueError(f"K线快照缺失：{digest}")
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)

    # ---------------- 基线 ----------------

    def list_baselines(self):
        out = []
        for p in self.bl_dir.glob("b_*.json"):
            try:
                out.append(_read_json(p))
            except (OSError, json.JSONDecodeError):
                continue
        out.sort(key=lambda b: b.get("createdAt") or 0, reverse=True)
        return out

    def _baseline(self, baseline_id):
        p = self.bl_dir / f"{baseline_id}.json"
        if not p.exists():
            raise ValueError(f"基线不存在：{baseline_id}")
        return _read_json(p)

    def baseline_bis(self, baseline_id):
        """基线冻结明细：逐用例返回各周期完整冻结笔（前端「明细」视图）。"""
        bl = self._baseline(baseline_id)
        cases = {c["id"]: c for c in self.list_cases()}
        out = []
        for cid in bl.get("caseIds", []):
            c = cases.get(cid)
            if not c:
                continue
            exp = (bl.get("expected") or {}).get(cid) or {}
            out.append({"caseId": cid, "caseName": c["name"], "res": c["res"],
                        "symbol": c.get("symbol"), "fromTs": c.get("fromTs"),
                        "toTs": c.get("toTs"), "frozenAt": exp.get("frozenAt"),
                        "bis": exp.get("bis") or {}, "marks": exp.get("marks") or {}})
        return {"name": bl["name"], "updatedAt": bl.get("updatedAt"),
                "cases": out}

    def delete_baseline(self, baseline_id):
        self._baseline(baseline_id)
        (self.bl_dir / f"{baseline_id}.json").unlink()

    def rename_baseline(self, payload):
        bl = self._baseline(payload.get("id"))
        name = (payload.get("name") or bl["name"]).strip()
        if not name:
            raise ValueError("基线名称不能为空")
        note = payload.get("note", bl.get("note", ""))
        bl.update({"name": name, "note": note, "updatedAt": time.time()})
        _write_json(self.bl_dir / f"{bl['id']}.json", bl)
        return {"baseline": bl}

    # ---------------- 评估任务 ----------------

    def job_snapshot(self):
        with self._state_lock:
            return copy.deepcopy(self._job) if self._job else {"state": "idle"}

    def start_run(self, baseline_ids):
        """跑基线：当前逻辑重算并与冻结期望比对。baseline_ids 为空 = 全部基线。"""
        all_bl = self.list_baselines()
        if not baseline_ids:
            baseline_ids = [b["id"] for b in all_bl]
        if not baseline_ids:
            raise ValueError("没有可运行的基线，请先建立基线")
        by_id = {b["id"]: b for b in all_bl}
        items = []
        for bid in baseline_ids:
            bl = by_id.get(bid)
            if not bl:
                raise ValueError(f"基线不存在：{bid}")
            for cid in bl.get("caseIds", []):
                items.append({"baselineId": bid, "baselineName": bl["name"],
                              "caseId": cid, "res": None, "state": "pending",
                              "ok": None, "drift": False, "error": None, "details": None})
        if not items:
            raise ValueError(f"基线没有用例：{baseline_ids}")
        return self._spawn("run", items)

    def start_freeze(self, payload):
        """建立/更新基线：对选用例跑当前逻辑并冻结期望（异步任务）。"""
        baseline_id = payload.get("id")
        if baseline_id:
            bl = self._baseline(baseline_id)
            name, note, case_ids = bl["name"], bl.get("note", ""), bl["caseIds"]
            if not case_ids:
                raise ValueError("基线没有用例")
        else:
            name = (payload.get("name") or "").strip()
            note = payload.get("note", "")
            case_ids = payload.get("caseIds") or []
            if not name:
                raise ValueError("基线名称不能为空")
            if not case_ids or not isinstance(case_ids, list):
                raise ValueError("请先勾选至少一个用例")
        cases = {c["id"]: c for c in self.list_cases()}
        missing = [cid for cid in case_ids if cid not in cases]
        if missing:
            raise ValueError("选用例不存在：" + "、".join(missing))
        items = []
        for cid in case_ids:
            c = cases[cid]
            items.append({"baselineId": baseline_id or "(新建)", "baselineName": name,
                          "caseId": cid, "caseName": c["name"], "res": c["res"],
                          "state": "pending", "ok": None, "drift": False,
                          "error": None, "details": None})
        return self._spawn("freeze", items, freeze_name=name, freeze_note=note,
                           baseline_id=baseline_id)

    def _spawn(self, mode, items, freeze_name=None, freeze_note=None, baseline_id=None):
        job = {"id": _new_id("j"), "state": "running", "mode": mode,
               "items": items, "startedAt": time.time(),
               "finishedAt": None, "error": None,
               "freezeName": freeze_name, "baselineId": baseline_id}
        with self._job_lock:
            with self._state_lock:
                if self._job and self._job.get("state") == "running":
                    raise BusyError("已有评估任务在运行，请稍候")
                self._job = job
        threading.Thread(target=self._worker, args=(job,), daemon=True).start()
        return {"jobId": job["id"]}

    def _set_item(self, job, idx, **kv):
        with self._state_lock:
            job["items"][idx] = {**job["items"][idx], **kv}

    def _worker(self, job):
        try:
            saved_cfg = dict(chan_core.CHAN_CFG)  # 任务结束恢复进程内画笔参数
            computed = {}
            for idx, item in enumerate(list(job["items"])):
                self._set_item(job, idx, state="running")
                try:
                    case = self._case(item["caseId"])
                    self._set_item(job, idx, res=case["res"],
                                   caseName=case["name"])
                    bars = self._load_snapshot(case["snapshot"])
                    symbol = case.get("symbol")
                    drift = case.get("chanCfg") != param_center.chan_cfg_effective(symbol)
                    # 旧用例无 pointsCfg → 用当前有效参数（不标漂移）；有则比对是否漂移
                    pts_cfg = case.get("pointsCfg")
                    if pts_cfg is None:
                        pts_cfg = param_center.effective("points", symbol) or {}
                    else:
                        drift = drift or _marks_params(pts_cfg) != _marks_params(
                            param_center.effective("points", symbol))
                    chan_core.apply_cfg(case.get("chanCfg") or {})  # 用例参数口径
                    bis_all = build_bis(bars)
                    marks_all = compute_all_marks(bis_all, bars, PERIODS,
                                                  fromTs=None, **_marks_params(pts_cfg))
                    res_list = PERIODS if case["res"] == "ALL" else [case["res"]]
                    if job["mode"] == "run":
                        bl = self._baseline(item["baselineId"])
                        exp_entry = (bl.get("expected") or {}).get(case["id"], {})
                        exp_bis = exp_entry.get("bis") or {}
                        exp_marks = exp_entry.get("marks")  # None = 旧基线未冻结买卖点
                        details = {}
                        for res in res_list:
                            det = compare_bis(exp_bis.get(res) or [], bis_all.get(res) or [])
                            det["marks"] = (None if exp_marks is None else
                                            compare_marks(exp_marks.get(res) or [],
                                                          marks_all.get(res) or []))
                            det["ok"] = det["ok"] and (det["marks"] is None or det["marks"]["ok"])
                            details[res] = det
                        ok = all(d["ok"] for d in details.values())
                    else:
                        details = {res: {"nActual": len(bis_all.get(res) or []),
                                         "nMarks": len(marks_all.get(res) or [])}
                                   for res in res_list}
                        ok = True
                    self._set_item(job, idx, state="done", ok=ok, drift=drift, details=details)
                    computed[case["id"]] = {"case": case, "bis": bis_all, "marks": marks_all}
                except Exception as exc:  # 单用例失败不影响其余用例
                    self._set_item(job, idx, state="error", ok=False, error=str(exc))
            if job["mode"] == "freeze" and all(i["state"] == "done" for i in job["items"]):
                self._write_freeze(job, computed)
            elif job["mode"] == "run":
                self._record_last_run(job)
        except Exception as exc:
            with self._state_lock:
                job["error"] = str(exc)
        finally:
            chan_core.apply_cfg(saved_cfg)
            with self._state_lock:
                job["state"] = "error" if job.get("error") else "done"
                job["finishedAt"] = time.time()
                job["durationMs"] = round((job["finishedAt"] - job["startedAt"]) * 1000)

    def _write_freeze(self, job, computed):
        """冻结期望并落盘基线（新建或覆盖更新）：笔 + 买卖点标记。"""
        now = time.time()
        expected = {}
        for item in job["items"]:
            c = computed[item["caseId"]]
            res_list = PERIODS if c["case"]["res"] == "ALL" else [c["case"]["res"]]
            expected[item["caseId"]] = {
                "bis": {res: c["bis"].get(res) or [] for res in res_list},
                "marks": {res: c["marks"].get(res) or [] for res in res_list},
                "frozenAt": now,
            }
        if job["baselineId"]:
            bl = self._baseline(job["baselineId"])
            bl["expected"] = expected
            bl["updatedAt"] = now
        else:
            bl = {"id": _new_id("b"), "name": job["freezeName"], "note": "",
                  "caseIds": [i["caseId"] for i in job["items"]],
                  "expected": expected, "createdAt": now, "updatedAt": now,
                  "lastRun": None}
        _write_json(self.bl_dir / f"{bl['id']}.json", bl)
        with self._state_lock:
            job["baselineId"] = bl["id"]

    def _record_last_run(self, job):
        """把运行摘要写回各基线文件（仅摘要，差异明细不落盘）。"""
        by_bl = {}
        for item in job["items"]:
            d = by_bl.setdefault(item["baselineId"], {"cases": {}})
            d["cases"][item["caseId"]] = bool(item["ok"])
        for bid, d in by_bl.items():
            try:
                bl = self._baseline(bid)
            except ValueError:
                continue
            total = len(d["cases"])
            bl["lastRun"] = {"at": time.time(),
                             "pass": sum(1 for v in d["cases"].values() if v),
                             "total": total, "cases": d["cases"]}
            _write_json(self.bl_dir / f"{bid}.json", bl)

    def run_compare(self, baseline_id):
        """同步跑单个基线并返回逐周期比对结果（测试用；生产走 start_run 任务）。"""
        bl = self._baseline(baseline_id)
        saved_cfg = dict(chan_core.CHAN_CFG)
        results = {}
        try:
            for cid in bl["caseIds"]:
                case = self._case(cid)
                bars = self._load_snapshot(case["snapshot"])
                symbol = case.get("symbol")
                drift = case.get("chanCfg") != param_center.chan_cfg_effective(symbol)
                pts_cfg = case.get("pointsCfg")
                if pts_cfg is None:
                    pts_cfg = param_center.effective("points", symbol) or {}
                else:
                    drift = drift or _marks_params(pts_cfg) != _marks_params(
                        param_center.effective("points", symbol))
                chan_core.apply_cfg(case.get("chanCfg") or {})
                bis_all = build_bis(bars)
                marks_all = compute_all_marks(bis_all, bars, PERIODS,
                                              fromTs=None, **_marks_params(pts_cfg))
                exp_entry = (bl.get("expected") or {}).get(cid, {})
                exp_bis = exp_entry.get("bis") or {}
                exp_marks = exp_entry.get("marks")
                res_list = PERIODS if case["res"] == "ALL" else [case["res"]]
                details = {}
                for res in res_list:
                    det = compare_bis(exp_bis.get(res) or [], bis_all.get(res) or [])
                    det["marks"] = (None if exp_marks is None else
                                    compare_marks(exp_marks.get(res) or [],
                                                  marks_all.get(res) or []))
                    det["ok"] = det["ok"] and (det["marks"] is None or det["marks"]["ok"])
                    details[res] = det
                results[cid] = {"drift": drift, "details": details}
            return results
        finally:
            chan_core.apply_cfg(saved_cfg)
