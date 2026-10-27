"""EVAL 评估：成笔用例快照 + 基线回归（设计见 SPEC_eval.md）。

用例 = 某时刻本地K线缓存（bars_all_tf.json 全周期）的内容寻址快照 + 当时画笔参数，
不可变；基线 = 多选用例的期望笔结果冻结（同批用例可建多个基线做改动前后 A/B）。
跑基线用当前逻辑（backtest.build_bis，与回测/全链路同口径）重算并与冻结期望逐笔比对，
口径与 align_check.py 一致（11 字段位置比较，严格相等才通过）。

盈利用例（kind=pnl）不另存 K 线：本地库不变，重算时按冻结窗口 load_store。
加入时冻住当时的引擎参数和进出场位置；合计由这些位置用 bt_runs.compute_summary 算出。
跑基线先重跑引擎得到新的进出场，再比合计。
"""

import copy
import gzip
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from . import chan_core, data_store, param_center
from .backtest import build_bis
from .bt_runs import compute_summary
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
MARKS_KWARGS = {"keep", "thirdZsTol"}  # compute_all_marks 可接受的 points 模块键（nearAtrRatio/class2ZsTol 已随规则v2/v3 移除停用）
MIN_BARS = 50            # 建用例时所选周期（res=ALL 即全部默认周期）截断后的最少根数
MAX_DIFFS = 50           # 每周期差异明细上限
CONTEXT_PAD = 5          # 双侧笔对照窗口（首差异前后各N行）
PERIODS = ["D", "240", "60", "15", "3"]  # build_bis 默认周期（30S 不参与成笔回归）
_PERIOD_RANK = {"D": 0, "240": 1, "120": 2, "60": 3, "30": 4, "30S": 5, "15": 6, "5": 7, "3": 8, "1": 9}


def _marks_params(cfg):
    """过滤成 compute_all_marks 可接受的 points 模块参数。"""
    return {k: v for k, v in (cfg or {}).items() if k in MARKS_KWARGS}


def _param_label(path):
    """参数路径换成参数中心里的中文名。对不上时保留路径。"""
    key = str(path or "").rsplit(".", 1)[-1]
    if path.startswith("chanCfg.") or path.startswith("chan_cfg."):
        prefer = ["chan", "points", "entry"]
    elif path.startswith("pointsCfg.") or ".marks." in path:
        prefer = ["points"]
    elif ".plan." in path:
        prefer = ["plan"]
    elif ".sr_kwargs." in path:
        prefer = []
    else:
        prefer = ["entry", "chan", "points", "plan", "fxma"]
    modules = param_center.PARAM_MODULES
    for mid in prefer + [m for m in modules if m not in prefer]:
        spec = modules.get(mid) or {}
        params = spec.get("params") or {}
        if key in params:
            return f"{spec.get('title') or mid} · {params[key][0]}"
    if ".sr_kwargs." in path:
        return f"支阻位 · {key}"
    return path or key


def _param_diffs(frozen, current, prefix):
    """逐项对比两份参数。字典下钻到叶子；列表和标量整段比较。

    一侧没有该键时 frozenMissing / currentMissing 为真。不用 null 表示缺失，
    因为参数值本身可以是 null。
    """
    if isinstance(frozen, dict) and isinstance(current, dict):
        diffs = []
        for k in dict.fromkeys(list(frozen) + list(current)):
            path = f"{prefix}.{k}" if prefix else str(k)
            in_f, in_c = k in frozen, k in current
            fv = frozen.get(k) if in_f else None
            cv = current.get(k) if in_c else None
            if in_f and in_c and isinstance(fv, dict) and isinstance(cv, dict):
                diffs.extend(_param_diffs(fv, cv, path))
                continue
            if (not in_f and isinstance(cv, dict)) or (not in_c and isinstance(fv, dict)):
                nested = _param_diffs(fv if in_f else {}, cv if in_c else {}, path)
                if nested:
                    diffs.extend(nested)
                    continue
            if (not in_f) or (not in_c) or fv != cv:
                diffs.append({
                    "path": path, "label": _param_label(path),
                    "frozen": fv, "current": cv,
                    "frozenMissing": not in_f, "currentMissing": not in_c,
                })
        return diffs
    if frozen != current:
        return [{
            "path": prefix, "label": _param_label(prefix),
            "frozen": frozen, "current": current,
            "frozenMissing": False, "currentMissing": False,
        }]
    return []


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


class DuplicateError(ValueError):
    """同一回测已在 EVAL 用例里。"""


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


# 盈利用例对照进出场时展示的字段（合计由整行 compute_summary，不只看这些）
# 后半段留给详情定位：品种、背驰周期、分批出场都要留在行快照里
_POS_VIEW = ("time", "direction", "periodX", "strategyKey", "status",
             "entryTime", "entryPrice", "exitTime", "exitPrice", "exitType", "pnl",
             "symbol", "markRes", "mode", "id", "lots", "price", "nearSr", "exits")
# 信号表浅蓝底与「同一条不重复加入」共用的行身份
_ROW_KEY_FIELDS = ("symbol", "time", "direction", "periodX", "strategyKey", "entryTime")


def _pos_key(row, with_entry=False):
    """信号身份：信号时间、方向、检测周期、策略名。

    单行对照 with_entry=True 再带进场时间。同一信号止损后再进场时，
    两笔信号时间相同，必须靠进场时间对上选中的那一笔。
    """
    key = (row.get("time"), row.get("direction"), row.get("periodX"), row.get("strategyKey"))
    if with_entry:
        return key + (row.get("entryTime"),)
    return key


def _eval_row_key(row):
    """一条信号在 EVAL 里的身份。与前端 evalRowKey 同口径。"""
    return "|".join("" if row.get(k) is None else str(row.get(k)) for k in _ROW_KEY_FIELDS)


def _pos_view(row):
    return {k: row.get(k) for k in _POS_VIEW}


def _subset_rows(expected, actual):
    """从重跑结果里按单行身份（含进场时间）取出被选中的行，顺序跟期望侧一致。对不上的不补。"""
    used = set()
    buckets = {}
    for i, row in enumerate(actual or []):
        buckets.setdefault(_pos_key(row, with_entry=True), []).append(i)
    out = []
    for exp in expected or []:
        for i in buckets.get(_pos_key(exp, with_entry=True), []):
            if i not in used:
                used.add(i)
                out.append(actual[i])
                break
    return out


_PERIOD_SEC = {"D": 86400, "240": 14400, "120": 7200, "60": 3600, "30": 1800,
               "15": 900, "5": 300, "3": 180, "1": 60, "30S": 30}


def _sig_brief(s):
    """同向过滤回调里只留身份字段。信号对象随后还会被改，这里先拷出来。"""
    return {k: s.get(k) for k in ("time", "direction", "periodX", "strategyKey", "entryTime")}


def _scoped_miss_note(base, matched, rows, suppressed):
    """选中行在重算里对不上时的说明。全部对上返回空串。

    对不上表示没开单。运行结果把重算合计记 0，与保本平仓（行还在、盈亏为 0）靠这句说明分开。
    """
    got = {_pos_key(r, True) for r in matched or []}
    notes = []
    for exp in base or []:
        if _pos_key(exp, True) in got:
            continue
        key = _pos_key(exp, False)
        filtered = any(_pos_key(s, False) == key for s in suppressed or [])
        sec = _PERIOD_SEC.get(str(exp.get("periodX")), 0)
        blocker = None
        for r in rows or []:
            if r.get("time") != exp.get("time") or r.get("direction") != exp.get("direction"):
                continue
            if _PERIOD_SEC.get(str(r.get("periodX")), 0) <= sec:
                continue
            blocker = r
            break
        if filtered:
            text = "这行在重算里被同向过滤，没有成交"
            if blocker is not None:
                pnl = blocker.get("pnl")
                pnl_txt = (f"{pnl:+.2f}" if isinstance(pnl, (int, float))
                           and not isinstance(pnl, bool) else "—")
                text += (f"，同时刻 {blocker.get('periodX')} {blocker.get('strategyKey')}"
                         f" 已成交（{pnl_txt}）")
            text += "。没开单。"
        else:
            text = "重算结果里没有这行。没开单。"
        notes.append(text)
    return "".join(notes)


def _row_case_name(symbol, row):
    """单行用例名：品种、信号时间、方向、检测周期、策略、该笔盈亏。"""
    sym = str(symbol or "").split(":")[-1] or (symbol or "")
    side = {"long": "多", "short": "空"}.get(row.get("direction"), "")
    when = ""
    ts = row.get("time")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        when = chan_core.fmtT(ts)
    pnl = row.get("pnl")
    pnl_txt = (f"{pnl:+.2f}" if isinstance(pnl, (int, float)) and not isinstance(pnl, bool)
               else "—")
    return " ".join(x for x in (
        sym, when, side, str(row.get("periodX") or ""), str(row.get("strategyKey") or ""), pnl_txt,
    ) if x)


def align_positions(expected, actual, extras=True, with_entry=False):
    """按信号时间、方向、检测周期、策略对齐两侧进出场。未配上的单侧保留。

    extras=False 时不把重跑里多出来的其他行塞进对照（单行用例只看选中的那几条）。
    with_entry=True 时进场时间也入键，单行对照不会配上同一信号上更早的另一笔。
    """
    used = set()
    buckets = {}
    for i, row in enumerate(actual or []):
        buckets.setdefault(_pos_key(row, with_entry=with_entry), []).append(i)
    out = []
    for exp in expected or []:
        hit = None
        for i in buckets.get(_pos_key(exp, with_entry=with_entry), []):
            if i not in used:
                hit = i
                used.add(i)
                break
        out.append({"a": _pos_view(exp), "b": _pos_view(actual[hit]) if hit is not None else None})
    if extras:
        for i, row in enumerate(actual or []):
            if i not in used:
                out.append({"a": None, "b": _pos_view(row)})
    return out


def _pos_fingerprint(rows):
    keys = [_pos_key(r) + (r.get("entryTime"), r.get("exitTime"), r.get("pnl"), r.get("status"))
            for r in rows or []]
    raw = json.dumps(keys, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _json_ready(obj):
    """tuple 收成 list，其余保持原样，便于整份参数落成 JSON。"""
    if isinstance(obj, tuple):
        return [_json_ready(x) for x in obj]
    if isinstance(obj, list):
        return [_json_ready(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _json_ready(v) for k, v in obj.items()}
    return obj


def _json_roundtrip(obj):
    """冻进用例的参数必须是纯 JSON。有不能序列化的值就直接失败。"""
    return json.loads(json.dumps(_json_ready(obj), ensure_ascii=False))


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
            if c.get("kind") == "pnl":
                out.append(self._pnl_case_public(c))
            else:
                c["snapshotOk"] = (self.snap_dir / f"{c.get('snapshot')}.json.gz").exists()
                out.append(c)
        out.sort(key=lambda c: c.get("createdAt") or 0, reverse=True)
        return out

    def _pnl_case_public(self, c):
        """列表带进出场（详情里展开），不带引擎参数。"""
        rp = c.get("replay") or {}
        return {
            "id": c.get("id"), "name": c.get("name"), "kind": "pnl",
            "symbol": c.get("symbol"), "strategy": c.get("strategy"),
            "source": c.get("source"), "runId": c.get("runId"),
            "sourceKey": c.get("sourceKey"), "scope": c.get("scope") or "run",
            "expectedTotal": c.get("expectedTotal"),
            "positions": c.get("positions") or [],
            "from": rp.get("from"), "toTs": rp.get("to_ts"),
            "dataFromTs": rp.get("data_from_ts"),
            "createdAt": c.get("createdAt"),
            "snapshotOk": True,
        }

    def _case(self, case_id):
        p = self.case_dir / f"{case_id}.json"
        if not p.exists():
            raise ValueError(f"用例不存在：{case_id}")
        return _read_json(p)

    def delete_case(self, case_id):
        self._case(case_id)
        refs = [b["name"] for b in self.list_baselines() if case_id in b.get("caseIds", [])]
        if refs:
            raise ValueError("用例被基线引用，请先从基线移出该用例：" + "、".join(refs))
        (self.case_dir / f"{case_id}.json").unlink()

    # ---------------- 盈利用例（回测合计） ----------------

    def create_pnl_case(self, payload, app):
        """从一次回测收成盈利用例。K 线不另存，参数和进出场在此刻冻住。

        仅 runId：历史方案整包（scope=run）。
        runId 且带 signalId：历史方案明细里的那一行（scope=rows）。
        source=live：当前信号表里的那一行（scope=rows）。
        """
        run_id = payload.get("runId")
        source = payload.get("source")
        row_only = False
        if run_id:
            run = app.bt_runs.get(run_id)
            if not run:
                raise ValueError(f"方案不存在：{run_id}")
            cfg = dict(run.get("cfg") or {})
            rows = list(run.get("signals") or [])
            source = "run"
            if "signalId" in payload:
                row_only = True
                signal_id = payload.get("signalId")
                if type(signal_id) is not int or signal_id <= 0:
                    raise ValueError("请在方案明细右击要加入的那一行")
                picked = [r for r in rows if r.get("id") == signal_id]
                if not picked:
                    raise ValueError("找不到这条信号记录")
                row = dict(picked[0])
                if row.get("symbol"):
                    cfg["symbol"] = row["symbol"]
                elif cfg.get("symbol"):
                    row["symbol"] = cfg["symbol"]
                # 明细只收这一行：按该行品种重放，不再按批量整包拒绝
                cfg.pop("symbols", None)
                rows = [row]
                source_key = self._cfg_row_source_key(cfg, row)
                default_name = None
            else:
                source_key = f"run|{run_id}"
                default_name = run.get("name") or run_id
        elif source == "live":
            worker = self._live_worker(app, payload.get("strategy"))
            if worker.state in ("running", "paused"):
                raise ValueError("回测尚未结束，不能加入 EVAL")
            if worker.state not in ("done", "stopped") or not worker.cfg:
                raise ValueError("没有已结束的回测")
            cfg = dict(worker.cfg)
            rows = app.signals.snapshot("backtest", worker._row_base, strategy=worker.strategy)
            if not rows:
                raise ValueError("当前没有这次回测的信号记录（表格已清空或未产生信号）")
            signal_id = payload.get("signalId")
            if type(signal_id) is not int or signal_id <= 0:
                raise ValueError("请在信号表右击要加入的那一行")
            rows = [r for r in rows if r.get("id") == signal_id]
            if not rows:
                raise ValueError("找不到这条信号记录")
            source_key = self._row_source_key(worker, rows[0])
            default_name = None
        else:
            raise ValueError("须指定 runId，或 source=live")
        self._reject_batch(cfg)
        symbol = cfg.get("symbol")
        if not symbol:
            raise ValueError("回测配置缺少品种")
        if cfg.get("from_ts") is None:
            raise ValueError("回测配置缺少起始时间")
        from . import module_registry
        strategy = module_registry.normalize_strategy(cfg.get("strategy"))
        cfg["strategy"] = strategy
        cfg["symbol"] = symbol
        scope = "rows" if source == "live" or row_only else "run"
        if any(c.get("sourceKey") == source_key for c in self._read_cases()):
            raise DuplicateError("该记录已在 EVAL 案例" if scope == "rows" else "该回测已在 EVAL 案例")
        replay = self._freeze_replay(cfg)
        total = compute_summary(rows)["total"]
        name = (payload.get("name") or "").strip()
        if not name:
            if scope == "rows":
                name = _row_case_name(symbol, rows[0])
            elif default_name:
                name = default_name
            else:
                name = f"{symbol} {cfg.get('from') or ''} 合计{total:+.2f}".strip()
        case = {
            "id": _new_id("c"), "name": name, "kind": "pnl",
            "source": source, "runId": run_id if source == "run" else None,
            "sourceKey": source_key, "scope": scope,
            "symbol": symbol, "strategy": strategy,
            "btCfg": _json_roundtrip(cfg), "replay": replay,
            "positions": rows, "expectedTotal": total,
            "createdAt": time.time(),
        }
        _write_json(self.case_dir / f"{case['id']}.json", case)
        return {"case": self._pnl_case_public(case)}

    def pnl_flags(self, app, strategy):
        """已加入的历史方案 id，以及信号表里按行加入的身份键。

        整包方案（scope=run）只染历史方案那一行，不把信号表整表染蓝。
        明细里按行加入的用例带 runId，但不把方案列表整行染蓝。
        """
        cases = self._read_cases()
        run_ids = [c["runId"] for c in cases
                   if c.get("kind") == "pnl" and c.get("runId")
                   and (c.get("scope") or "run") != "rows"]
        sid = None
        if strategy:
            from . import module_registry
            try:
                sid = module_registry.normalize_strategy(strategy)
            except ValueError:
                sid = strategy
        keys = []
        for c in cases:
            if c.get("kind") != "pnl" or c.get("scope") != "rows":
                continue
            if sid and c.get("strategy") != sid:
                continue
            for row in c.get("positions") or []:
                keys.append(_eval_row_key(row))
        return {"runIds": run_ids, "rowKeys": keys}

    def _read_cases(self):
        out = []
        for p in self.case_dir.glob("c_*.json"):
            try:
                out.append(_read_json(p))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def _live_worker(self, app, strategy):
        from . import module_registry
        sid = module_registry.normalize_strategy(strategy)
        worker = (getattr(app, "bt_workers", None) or {}).get(sid)
        if worker is None:
            raise ValueError(f"策略 {sid} 没有回测 Worker")
        return worker

    def _live_source_key(self, worker, rows):
        cfg = worker.cfg or {}
        return "|".join([
            "live", str(worker.strategy or ""), str(cfg.get("symbol") or ""),
            str(cfg.get("from_ts") or ""), str(cfg.get("to_ts") or ""),
            _pos_fingerprint(rows),
        ])

    def _cfg_row_source_key(self, cfg, row, strategy=None):
        """同一回测窗口里的同一条信号只建一次。

        当前信号表与历史方案明细共用此键：同一窗口、同一行不会各建一条。
        strategy 缺省时按 cfg 规范化；当前信号表传入 Worker 上的策略 id。
        """
        if strategy is None:
            from . import module_registry
            try:
                strategy = module_registry.normalize_strategy(cfg.get("strategy"))
            except ValueError:
                strategy = str(cfg.get("strategy") or "")
        return "|".join([
            "row", str(strategy or ""), str(cfg.get("symbol") or ""),
            str(cfg.get("from_ts") or ""), str(cfg.get("to_ts") or ""),
            _eval_row_key(row),
        ])

    def _row_source_key(self, worker, row):
        """同一回测窗口里的同一条信号只建一次。"""
        cfg = worker.cfg or {}
        return self._cfg_row_source_key(cfg, row, strategy=str(worker.strategy or ""))

    def _reject_batch(self, cfg):
        syms = [s for s in (cfg.get("symbols") or []) if s]
        if len(syms) > 1:
            raise ValueError("多品种批量回测不能整包加入，请按单品种回测后再加入")

    def _freeze_replay(self, cfg):
        """按与回测 Worker 相同的取参口径，把此刻的引擎参数冻成可 JSON 的 replay。

        K 线不在这里读取。会暂时 apply_cfg，结束时恢复调用前的画笔参数。
        """
        from . import engine_dispatch, mark_entry, module_registry
        from .data_loader import DEFAULT_PERIODS
        from .webapp import BacktestWorker
        saved = dict(chan_core.CHAN_CFG)
        try:
            cfg = dict(cfg)
            strategy = module_registry.normalize_strategy(cfg.get("strategy"))
            symbol = cfg.get("symbol")
            engine_id = module_registry.STRATEGIES[strategy]["engine"]
            lead_days = int(cfg.get("lead_days") or 0)
            from_ts = int(cfg.get("from_ts") or 0)
            data_from_ts = max(0, from_ts - lead_days * 86400)
            start_ts = from_ts if lead_days > 0 else None
            to_ts = int(cfg.get("to_ts") or 0) or None
            if engine_id == "fx_ma":
                pm = param_center.effective_all(symbol)
                periods = list(engine_dispatch.fxma_load_periods(pm["fxma"]["entryRes"]))
                lots = cfg.get("lots")
                if lots is None:
                    lots = pm["fxma"].get("lots") or mark_entry.DEFAULT_LOTS
                kw = param_center.fxma_engine_kwargs(
                    pm, lots_override=lots,
                    contract_mult=mark_entry.contract_mult_of(symbol))
                chan_cfg = param_center.chan_cfg_effective(symbol)
            else:
                # 双周期链（2026-10-23）：显式 periods（历史方案）优先单口径；链式方案
                # 取链任务列表（重放引擎按第一份任务跑——eval 比对以单链口径冻结，
                # 双链并行的逐链统计走 Web 回测日志/journal）
                explicit = [p for p in (cfg.get("periods") or []) if str(p).strip()]
                if explicit:
                    periods = list(explicit)
                    kw = BacktestWorker._engine_kwargs_of(cfg, periods)
                else:
                    tasks = BacktestWorker._chain_tasks(cfg, symbol)
                    periods = list(tasks[0]["periods"])
                    kw = dict(tasks[0]["engine_kwargs"])
                chan_cfg = param_center.chan_cfg_effective(symbol)
            # 长窗自动分段（2026-10-27）：分段方案的冻结重放必须同段界逐段跑（段间
            # carry 交接持仓），否则整窗单引擎重放与原跑结果失真；_split_segments
            # 对带 seg_bounds 的 cfg 逐位重放，冻结/重放两侧同源
            segments = []
            if engine_id != "fx_ma":
                try:
                    segments = BacktestWorker._split_segments(
                        cfg, symbol, from_ts, to_ts, lead_days,
                        periods_needed=sorted(set(periods))) or []
                except Exception:
                    segments = []
            replay = {
                "strategy": strategy, "symbol": symbol,
                "chan_cfg": chan_cfg, "engine_kwargs": kw,
                "periods": periods, "data_from_ts": data_from_ts,
                "to_ts": to_ts, "start_ts": start_ts,
                "from": cfg.get("from") or "",
                "segments": segments,
            }
            return _json_roundtrip(replay)
        finally:
            chan_core.apply_cfg(saved)

    def _replay_positions(self, replay, job=None):
        """用冻结参数重跑引擎，得到与信号表相同口径的进出场行。K 线按窗口从本地库读。"""
        from . import engine_dispatch
        from .webapp import SignalLog
        saved = dict(chan_core.CHAN_CFG)
        try:
            chan_core.apply_cfg(replay.get("chan_cfg") or {})
            self._note_progress(job, 0, 0, "正在读取K线")
            # 长窗自动分段（2026-10-27）：分段冻结重放逐段跑（与 bt_batch 同口径），
            # 段间 carry 交接持仓；SignalLog/过滤行跨段累计（循环外只建一次）；
            # 单段（segments 空）= 原整窗路径不变
            segments = replay.get("segments") or [None]
            carry = None
            trades_all = []
            log = SignalLog()
            mode, strategy, symbol = "eval", replay.get("strategy"), replay["symbol"]
            suppressed = []

            def _on_suppressed(s):
                log.fill_suppressed(mode, s, symbol=symbol, strategy=strategy)
                suppressed.append(_sig_brief(s))

            def _on_progress(i, n):
                self._note_progress(job, i, n, "回测进度")

            for seg in segments:
                seg = seg or {}
                bars = data_store.load_store(
                    replay["symbol"], periods=replay["periods"],
                    from_ts=seg.get("data_from_ts", replay["data_from_ts"]),
                    to_ts=seg.get("to_ts", replay.get("to_ts")))
                kw = dict(replay["engine_kwargs"])
                engine_cls = engine_dispatch.engine_class_of(replay.get("strategy"))
                engine = engine_cls(bars, **kw)
                result = engine.run(
                    start_ts=seg.get("start_ts", replay.get("start_ts")),
                    log=lambda *a, **k: None,
                    on_signal=lambda s: log.append_signal(mode, s, symbol=symbol, strategy=strategy),
                    on_trade=lambda tr: log.fill_trade(mode, tr, symbol=symbol, strategy=strategy),
                    on_exit=lambda tr: log.fill_exit(mode, tr, symbol=symbol, strategy=strategy),
                    on_suppressed=_on_suppressed,
                    on_progress=_on_progress,
                    journal=False,
                    carry=carry,
                )
                carry = result.get("carryState")
                trades_all = result.get("trades") or []
            for tr in trades_all:
                if tr.get("state") == "closed":
                    continue
                log.fill_trade(mode, tr, symbol=symbol, strategy=strategy)
            return log.list(mode=mode, strategy=strategy), suppressed
        finally:
            chan_core.apply_cfg(saved)

    def _bound_replay(self, case, baseline):
        """这条基线实际使用的引擎参数。

        强制更新后参数写在基线期望里；没写过则仍用加入用例时冻在用例上的 replay。
        """
        if baseline:
            exp = (baseline.get("expected") or {}).get(case.get("id")) or {}
            if exp.get("replay"):
                return exp["replay"]
        return case.get("replay") or {}

    def _pnl_param_diffs(self, case, baseline=None):
        """当前参数中心与这条基线冻住的引擎参数逐项对比。同时返回两侧快照。"""
        bound = self._bound_replay(case, baseline)
        current = self._freeze_replay(case.get("btCfg") or {})
        diffs = _param_diffs(bound.get("chan_cfg") or {}, current.get("chan_cfg") or {}, "chan_cfg")
        diffs += _param_diffs(bound.get("engine_kwargs") or {}, current.get("engine_kwargs") or {},
                              "engine_kwargs")
        return diffs, current, bound

    def _bi_bound_params(self, case, baseline):
        """成笔用例这条基线实际使用的画笔 / 买卖点参数。"""
        exp = {}
        if baseline:
            exp = (baseline.get("expected") or {}).get(case.get("id")) or {}
        chan = exp["chanCfg"] if isinstance(exp.get("chanCfg"), dict) else (case.get("chanCfg") or {})
        pts = exp["pointsCfg"] if exp.get("pointsCfg") is not None else case.get("pointsCfg")
        return chan, pts

    def _bi_param_diffs(self, case, baseline=None):
        """当前画笔 / 买卖点参数与这条基线冻住的逐项对比。"""
        symbol = case.get("symbol")
        bound_chan, bound_pts = self._bi_bound_params(case, baseline)
        current_chan = param_center.chan_cfg_effective(symbol)
        current_pts = param_center.effective("points", symbol) or {}
        diffs = _param_diffs(bound_chan, current_chan, "chanCfg")
        if bound_pts is not None:
            diffs += _param_diffs(_marks_params(bound_pts), _marks_params(current_pts), "pointsCfg")
        return diffs, current_chan, current_pts, bound_chan, bound_pts

    def param_drift(self, baseline_id):
        """更新期望前的参数差异。不重跑引擎。"""
        bl = self._baseline(baseline_id)
        out = []
        for cid in bl.get("caseIds") or []:
            case = self._case(cid)
            if case.get("kind") == "pnl":
                diffs, _, _ = self._pnl_param_diffs(case, bl)
            else:
                diffs, _, _, _, _ = self._bi_param_diffs(case, bl)
            if diffs:
                out.append({"caseId": cid, "caseName": case.get("name"),
                            "kind": "pnl" if case.get("kind") == "pnl" else "bi",
                            "diffs": diffs})
        return {"cases": out}

    def _pnl_drift(self, case, baseline=None):
        """当前参数中心与这条基线冻住的引擎参数不一致则标漂移。"""
        diffs, _, _ = self._pnl_param_diffs(case, baseline)
        return bool(diffs)

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
                bl = _read_json(p)
            except (OSError, json.JSONDecodeError):
                continue
            if bl.get("kind") == "pnl":
                exp = {}
                for cid, entry in (bl.get("expected") or {}).items():
                    entry = entry or {}
                    exp[cid] = {"total": entry.get("total"), "frozenAt": entry.get("frozenAt")}
                bl = {**bl, "expected": exp}
            # 列表只要通过数。对照进出场只在明细接口返回
            lr = bl.get("lastRun")
            if isinstance(lr, dict) and "details" in lr:
                bl = {**bl, "lastRun": {k: v for k, v in lr.items() if k != "details"}}
            out.append(bl)
        out.sort(key=lambda b: b.get("createdAt") or 0, reverse=True)
        return out

    def _baseline(self, baseline_id):
        p = self.bl_dir / f"{baseline_id}.json"
        if not p.exists():
            raise ValueError(f"基线不存在：{baseline_id}")
        return _read_json(p)

    def baseline_bis(self, baseline_id):
        """基线冻结明细：逐用例返回各周期完整冻结笔（前端「明细」视图）。

        盈利用例返回冻住的进出场和合计，不走成笔表。
        """
        bl = self._baseline(baseline_id)
        if bl.get("kind") == "pnl":
            names = {c["id"]: c for c in self._read_cases()}
            run_details = (bl.get("lastRun") or {}).get("details") or {}
            out = []
            for cid in bl.get("caseIds", []):
                c = names.get(cid)
                if not c:
                    continue
                exp = (bl.get("expected") or {}).get(cid) or {}
                current = run_details.get(cid) if isinstance(run_details, dict) else None
                if not isinstance(current, dict) or "actualTotal" not in current:
                    current = None
                out.append({"caseId": cid, "caseName": c.get("name"),
                            "symbol": c.get("symbol"), "kind": "pnl",
                            "total": exp.get("total"), "frozenAt": exp.get("frozenAt"),
                            "positions": exp.get("positions") or [],
                            "current": current})
            return {"name": bl["name"], "kind": "pnl", "updatedAt": bl.get("updatedAt"),
                    "lastRunAt": (bl.get("lastRun") or {}).get("at") if run_details else None,
                    "cases": out}
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

    def remove_baseline_case(self, baseline_id, case_id):
        """从已建基线移出一条用例。用例文件和其他基线不动。

        同步去掉冻结期望和最近一次运行摘要，通过数按剩下的用例重算。
        不改 updatedAt，列表上的期望冻结时间仍是上次冻结时刻。
        """
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("caseId 不能为空")
        bl = self._baseline(baseline_id)
        ids = list(bl.get("caseIds") or [])
        if case_id not in ids:
            raise ValueError("该用例不在这条基线里")
        with self._job_lock:
            with self._state_lock:
                if self._job and self._job.get("state") == "running":
                    raise BusyError("已有评估任务在运行，请稍候")
        ids.remove(case_id)
        bl["caseIds"] = ids
        expected = dict(bl.get("expected") or {})
        expected.pop(case_id, None)
        bl["expected"] = expected
        lr = bl.get("lastRun")
        if isinstance(lr, dict):
            cases = dict(lr.get("cases") or {})
            cases.pop(case_id, None)
            lr["cases"] = cases
            lr["total"] = len(cases)
            lr["pass"] = sum(1 for v in cases.values() if v)
            details = lr.get("details")
            if isinstance(details, dict):
                details.pop(case_id, None)
                if not details:
                    lr.pop("details", None)
            bl["lastRun"] = lr
        _write_json(self.bl_dir / f"{bl['id']}.json", bl)
        return {"baselineId": bl["id"], "caseIds": ids}

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
            case_by_id = {c["id"]: c for c in self.list_cases()}
            for cid in bl.get("caseIds", []):
                c = case_by_id.get(cid) or {}
                items.append({"baselineId": bid, "baselineName": bl["name"],
                              "caseId": cid, "res": c.get("res"),
                              "kind": "pnl" if c.get("kind") == "pnl" else "bi",
                              "state": "pending",
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
        kinds = {"pnl" if cases[cid].get("kind") == "pnl" else "bi" for cid in case_ids}
        if len(kinds) > 1:
            raise ValueError("成笔用例和盈利用例不能放进同一条基线")
        items = []
        for cid in case_ids:
            c = cases[cid]
            items.append({"baselineId": baseline_id or "(新建)", "baselineName": name,
                          "caseId": cid, "caseName": c["name"], "res": c.get("res"),
                          "kind": "pnl" if c.get("kind") == "pnl" else "bi",
                          "state": "pending", "ok": None, "drift": False,
                          "driftDiffs": [], "error": None, "details": None})
        adopt = bool(payload.get("adoptParams"))
        if adopt and not baseline_id:
            raise ValueError("只有更新已有基线时可以按当前参数强制更新")
        return self._spawn("freeze", items, freeze_name=name, freeze_note=note,
                           baseline_id=baseline_id, adopt_params=adopt)

    def _spawn(self, mode, items, freeze_name=None, freeze_note=None, baseline_id=None,
               adopt_params=False):
        job = {"id": _new_id("j"), "state": "running", "mode": mode,
               "items": items, "startedAt": time.time(),
               "finishedAt": None, "error": None,
               "freezeName": freeze_name, "baselineId": baseline_id,
               "adoptParams": bool(adopt_params)}
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

    def _note_progress(self, job, done, total, label):
        """把当前重放进度写进任务快照，供页面进度条轮询。

        未跨过 1% 且距上次不足 0.4 秒则跳过，避免每根 K 线都抢状态锁。
        """
        if job is None:
            return
        now = time.time()
        done = int(done or 0)
        total = int(total or 0)
        with self._state_lock:
            prev = job.get("progress")
            if isinstance(prev, dict) and prev.get("total") == total and total > 0 and done < total:
                advanced = done - int(prev.get("done") or 0)
                if advanced * 100 < total and now - float(prev.get("at") or 0) < 0.4:
                    return
            job["progress"] = {"done": done, "total": total, "label": label, "at": now}

    def _worker(self, job):
        try:
            saved_cfg = dict(chan_core.CHAN_CFG)  # 任务结束恢复进程内画笔参数
            computed = {}
            # 同一套冻结参数的盈利用例共用一次引擎重跑
            replay_cache = {}
            for idx, item in enumerate(list(job["items"])):
                self._set_item(job, idx, state="running")
                try:
                    case = self._case(item["caseId"])
                    if case.get("kind") == "pnl":
                        self._handle_pnl(job, idx, case, computed, replay_cache)
                        continue
                    self._set_item(job, idx, res=case["res"],
                                   caseName=case["name"])
                    bars = self._load_snapshot(case["snapshot"])
                    bid = job.get("baselineId") or item.get("baselineId")
                    baseline = self._baseline(bid) if bid and bid != "(新建)" else None
                    diffs, current_chan, current_pts, bound_chan, bound_pts = (
                        self._bi_param_diffs(case, baseline))
                    # 强制更新：按当前参数重算，并把这套参数冻进这条基线
                    adopt = (bool(job.get("adoptParams")) and job["mode"] == "freeze"
                             and baseline is not None)
                    if adopt:
                        chan_core.apply_cfg(current_chan)
                        pts_cfg = current_pts
                        drift, drift_diffs = False, []
                    else:
                        # 旧用例无 pointsCfg → 用当前有效参数（不标漂移）；有则按冻住的那份
                        pts_cfg = current_pts if bound_pts is None else bound_pts
                        chan_core.apply_cfg(bound_chan)
                        drift, drift_diffs = bool(diffs), diffs
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
                    self._set_item(job, idx, state="done", ok=ok, drift=drift,
                                   driftDiffs=drift_diffs, details=details)
                    computed[case["id"]] = {"case": case, "bis": bis_all, "marks": marks_all}
                    if adopt:
                        computed[case["id"]]["adoptChan"] = current_chan
                        computed[case["id"]]["adoptPoints"] = current_pts
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

    def _replay_group_key(self, replay):
        """同一套冻结参数共用一次重跑。展示用的 from 不参与分组。"""
        fields = ("strategy", "symbol", "chan_cfg", "engine_kwargs",
                  "periods", "data_from_ts", "to_ts", "start_ts")
        payload = {k: (replay or {}).get(k) for k in fields}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    def _cached_replay(self, replay, replay_cache, job=None):
        """按冻结参数取重跑结果。同组后续用例直接用已有进出场行。"""
        key = self._replay_group_key(replay)
        if key not in replay_cache:
            replay_cache[key] = self._replay_positions(replay, job)
        return replay_cache[key]

    def _handle_pnl(self, job, idx, case, computed, replay_cache):
        """盈利用例：建立基线只拷贝已冻住的进出场；更新期望和跑基线才重跑引擎。

        默认用加入用例时的 replay。这条基线若已强制更新过，用基线里另冻的那份。
        更新期望且用户确认强制更新时，改用当前参数，并把这份参数写进基线。
        """
        self._set_item(job, idx, res="盈利", caseName=case["name"], kind="pnl")
        if not case.get("replay"):
            raise ValueError("盈利用例缺少冻结参数")
        bid = job.get("baselineId") or job["items"][idx].get("baselineId")
        baseline = self._baseline(bid) if bid and bid != "(新建)" else None
        diffs, current, bound = self._pnl_param_diffs(case, baseline)
        adopt = (bool(job.get("adoptParams")) and job["mode"] == "freeze"
                 and baseline is not None)
        replay = current if adopt else bound
        drift = False if adopt else bool(diffs)
        drift_diffs = [] if adopt else diffs
        if job["mode"] == "freeze" and not job.get("baselineId"):
            positions = case.get("positions") or []
            total = case.get("expectedTotal")
            self._set_item(job, idx, state="done", ok=True, drift=drift,
                           driftDiffs=drift_diffs, details={
                "kind": "pnl", "expectedTotal": total, "actualTotal": total, "delta": 0})
            computed[case["id"]] = {"case": case, "kind": "pnl",
                                    "positions": positions, "total": total}
            return
        rows, suppressed = self._cached_replay(replay, replay_cache, job)
        # 按行加入的用例只比选中行；整包方案仍比这次回测的合计
        scoped = case.get("scope") == "rows"
        if job["mode"] == "run":
            bl = self._baseline(job["items"][idx]["baselineId"])
            exp = (bl.get("expected") or {}).get(case["id"]) or {}
            exp_positions = exp.get("positions") or []
            if scoped and not exp_positions:
                exp_positions = case.get("positions") or []
            if scoped:
                matched = _subset_rows(exp_positions, rows)
                miss = _scoped_miss_note(exp_positions, matched, rows, suppressed)
                # 没开单：合计记 0，不再沿用冻结盈利。行还在则照旧加总（含盈亏刚好为 0 的保本）
                total = 0 if miss else compute_summary(matched)["total"]
            else:
                miss = ""
                matched = rows
                total = compute_summary(rows)["total"]
            exp_total = exp.get("total")
            if scoped and not exp.get("positions"):
                exp_total = case.get("expectedTotal")
            ok = (not miss) and exp_total == total
            delta = None if exp_total is None or total is None else round(total - exp_total, 2)
            details = {"kind": "pnl", "expectedTotal": exp_total,
                       "actualTotal": total, "delta": delta,
                       "positions": align_positions(
                           exp_positions, matched, extras=not scoped, with_entry=scoped)}
            if miss:
                details["unmatched"] = miss
            self._set_item(job, idx, state="done", ok=ok, drift=drift,
                           driftDiffs=drift_diffs, details=details)
            positions_out = rows
        else:
            if scoped:
                bl = self._baseline(job["items"][idx]["baselineId"])
                exp = (bl.get("expected") or {}).get(case["id"]) or {}
                base = exp.get("positions") or case.get("positions") or []
                kept_total = exp.get("total") if exp.get("positions") else case.get("expectedTotal")
                positions_out = _subset_rows(base, rows)
                miss = _scoped_miss_note(base, positions_out, rows, suppressed)
            else:
                positions_out = rows
                miss = ""
                kept_total = None
            if miss:
                positions_out = base
                total = kept_total
                self._set_item(job, idx, state="done", ok=False, drift=drift,
                               driftDiffs=drift_diffs, details={
                    "kind": "pnl", "expectedTotal": kept_total, "actualTotal": None,
                    "delta": None, "unmatched": miss})
            else:
                total = compute_summary(positions_out)["total"]
                self._set_item(job, idx, state="done", ok=True, drift=drift,
                               driftDiffs=drift_diffs, details={
                    "kind": "pnl", "expectedTotal": total, "actualTotal": total, "delta": 0})
        computed[case["id"]] = {"case": case, "kind": "pnl",
                                "positions": positions_out, "total": total,
                                "adoptReplay": replay if adopt else None}

    def _write_freeze(self, job, computed):
        """冻结期望并落盘基线（新建或覆盖更新）：笔 + 买卖点标记。

        盈利用例改为冻住进出场和由它们算出的合计，不写笔。
        """
        now = time.time()
        expected = {}
        pnl = False
        old_expected = {}
        if job.get("baselineId"):
            old_expected = (self._baseline(job["baselineId"]).get("expected") or {})
        for item in job["items"]:
            c = computed[item["caseId"]]
            prev = old_expected.get(item["caseId"]) or {}
            if c.get("kind") == "pnl":
                pnl = True
                entry = {
                    "total": c["total"], "positions": c["positions"], "frozenAt": now,
                }
                # 强制更新把当前参数冻进这条基线；普通更新保留已经冻过的那份
                if c.get("adoptReplay"):
                    entry["replay"] = c["adoptReplay"]
                elif prev.get("replay"):
                    entry["replay"] = prev["replay"]
                expected[item["caseId"]] = entry
                continue
            res_list = PERIODS if c["case"]["res"] == "ALL" else [c["case"]["res"]]
            entry = {
                "bis": {res: c["bis"].get(res) or [] for res in res_list},
                "marks": {res: c["marks"].get(res) or [] for res in res_list},
                "frozenAt": now,
            }
            if c.get("adoptChan") is not None:
                entry["chanCfg"] = c["adoptChan"]
                entry["pointsCfg"] = c.get("adoptPoints") or {}
            else:
                if isinstance(prev.get("chanCfg"), dict):
                    entry["chanCfg"] = prev["chanCfg"]
                if prev.get("pointsCfg") is not None:
                    entry["pointsCfg"] = prev["pointsCfg"]
            expected[item["caseId"]] = entry
        if job["baselineId"]:
            bl = self._baseline(job["baselineId"])
            bl["expected"] = expected
            bl["updatedAt"] = now
            if pnl:
                bl["kind"] = "pnl"
        else:
            bl = {"id": _new_id("b"), "name": job["freezeName"], "note": "",
                  "caseIds": [i["caseId"] for i in job["items"]],
                  "expected": expected, "createdAt": now, "updatedAt": now,
                  "lastRun": None}
            if pnl:
                bl["kind"] = "pnl"
        _write_json(self.bl_dir / f"{bl['id']}.json", bl)
        with self._state_lock:
            job["baselineId"] = bl["id"]

    def _record_last_run(self, job):
        """把运行摘要写回各基线文件。盈利用例另存冻结与重算对照，供明细并排显示。"""
        by_bl = {}
        for item in job["items"]:
            d = by_bl.setdefault(item["baselineId"], {"cases": {}, "details": {}})
            d["cases"][item["caseId"]] = bool(item["ok"])
            det = item.get("details") if item.get("kind") == "pnl" else None
            if isinstance(det, dict) and det.get("kind") == "pnl":
                d["details"][item["caseId"]] = {
                    "expectedTotal": det.get("expectedTotal"),
                    "actualTotal": det.get("actualTotal"),
                    "delta": det.get("delta"),
                    "unmatched": det.get("unmatched") or "",
                    "positions": det.get("positions") or [],
                }
        for bid, d in by_bl.items():
            try:
                bl = self._baseline(bid)
            except ValueError:
                continue
            total = len(d["cases"])
            last = {"at": time.time(),
                    "pass": sum(1 for v in d["cases"].values() if v),
                    "total": total, "cases": d["cases"]}
            if d["details"]:
                last["details"] = d["details"]
            bl["lastRun"] = last
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
                diffs, current_chan, current_pts, bound_chan, bound_pts = (
                    self._bi_param_diffs(case, bl))
                drift = bool(diffs)
                pts_cfg = current_pts if bound_pts is None else bound_pts
                chan_core.apply_cfg(bound_chan)
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
