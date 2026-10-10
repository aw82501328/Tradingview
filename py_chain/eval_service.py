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
_POS_VIEW = ("time", "direction", "periodX", "strategyKey", "status",
             "entryTime", "entryPrice", "exitTime", "exitPrice", "exitType", "pnl")


def _pos_key(row):
    return (row.get("time"), row.get("direction"), row.get("periodX"), row.get("strategyKey"))


def _pos_view(row):
    return {k: row.get(k) for k in _POS_VIEW}


def align_positions(expected, actual):
    """按信号时间、方向、检测周期、策略对齐两侧进出场。未配上的单侧保留。"""
    used = set()
    buckets = {}
    for i, row in enumerate(actual or []):
        buckets.setdefault(_pos_key(row), []).append(i)
    out = []
    for exp in expected or []:
        hit = None
        for i in buckets.get(_pos_key(exp), []):
            if i not in used:
                hit = i
                used.add(i)
                break
        out.append({"a": _pos_view(exp), "b": _pos_view(actual[hit]) if hit is not None else None})
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
        """列表不带进出场明细和引擎参数，只留合计与窗口。"""
        rp = c.get("replay") or {}
        return {
            "id": c.get("id"), "name": c.get("name"), "kind": "pnl",
            "symbol": c.get("symbol"), "strategy": c.get("strategy"),
            "source": c.get("source"), "runId": c.get("runId"),
            "sourceKey": c.get("sourceKey"),
            "expectedTotal": c.get("expectedTotal"),
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
            raise ValueError("用例被基线引用，请先删除或重建基线：" + "、".join(refs))
        (self.case_dir / f"{case_id}.json").unlink()

    # ---------------- 盈利用例（回测合计） ----------------

    def create_pnl_case(self, payload, app):
        """从一次回测收成盈利用例。K 线不另存，参数和进出场在此刻冻住。"""
        run_id = payload.get("runId")
        source = payload.get("source")
        if run_id:
            run = app.bt_runs.detail(run_id)
            if not run:
                raise ValueError(f"方案不存在：{run_id}")
            cfg = dict(run.get("cfg") or {})
            rows = list(run.get("signals") or [])
            source = "run"
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
            source_key = self._live_source_key(worker, rows)
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
        if any(c.get("sourceKey") == source_key for c in self._read_cases()):
            raise DuplicateError("该回测已在 EVAL 案例")
        replay = self._freeze_replay(cfg)
        total = compute_summary(rows)["total"]
        name = (payload.get("name") or "").strip()
        if not name:
            if default_name:
                name = default_name
            else:
                name = f"{symbol} {cfg.get('from') or ''} 合计{total:+.2f}".strip()
        case = {
            "id": _new_id("c"), "name": name, "kind": "pnl",
            "source": source, "runId": run_id if source == "run" else None,
            "sourceKey": source_key, "symbol": symbol, "strategy": strategy,
            "btCfg": _json_roundtrip(cfg), "replay": replay,
            "positions": rows, "expectedTotal": total,
            "createdAt": time.time(),
        }
        _write_json(self.case_dir / f"{case['id']}.json", case)
        return {"case": self._pnl_case_public(case)}

    def pnl_flags(self, app, strategy):
        """已加入的方案 id，以及当前策略这次回测是否已经收成用例。"""
        cases = self._read_cases()
        run_ids = [c["runId"] for c in cases if c.get("kind") == "pnl" and c.get("runId")]
        live, min_id = False, 0
        workers = getattr(app, "bt_workers", None) or {}
        worker = workers.get(strategy) if strategy else None
        if worker is not None and worker.state in ("done", "stopped") and worker.cfg:
            rows = app.signals.snapshot("backtest", worker._row_base, strategy=worker.strategy)
            if rows:
                key = self._live_source_key(worker, rows)
                live = any(c.get("sourceKey") == key for c in cases)
                min_id = worker._row_base
        return {"runIds": run_ids, "live": live, "minId": min_id}

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
                periods = list(cfg.get("periods") or DEFAULT_PERIODS)
                kw = BacktestWorker._engine_kwargs_of(cfg, periods)
                chan_cfg = param_center.chan_cfg_effective(symbol)
            replay = {
                "strategy": strategy, "symbol": symbol,
                "chan_cfg": chan_cfg, "engine_kwargs": kw,
                "periods": periods, "data_from_ts": data_from_ts,
                "to_ts": to_ts, "start_ts": start_ts,
                "from": cfg.get("from") or "",
            }
            return _json_roundtrip(replay)
        finally:
            chan_core.apply_cfg(saved)

    def _replay_positions(self, replay):
        """用冻结参数重跑引擎，得到与信号表相同口径的进出场行。K 线按窗口从本地库读。"""
        from . import engine_dispatch
        from .webapp import SignalLog
        saved = dict(chan_core.CHAN_CFG)
        try:
            chan_core.apply_cfg(replay.get("chan_cfg") or {})
            bars = data_store.load_store(
                replay["symbol"], periods=replay["periods"],
                from_ts=replay["data_from_ts"], to_ts=replay.get("to_ts"))
            kw = dict(replay["engine_kwargs"])
            engine_cls = engine_dispatch.engine_class_of(replay.get("strategy"))
            engine = engine_cls(bars, **kw)
            log = SignalLog()
            mode, strategy, symbol = "eval", replay.get("strategy"), replay["symbol"]
            result = engine.run(
                start_ts=replay.get("start_ts"),
                log=lambda *a, **k: None,
                on_signal=lambda s: log.append_signal(mode, s, symbol=symbol, strategy=strategy),
                on_trade=lambda tr: log.fill_trade(mode, tr, symbol=symbol, strategy=strategy),
                on_exit=lambda tr: log.fill_exit(mode, tr, symbol=symbol, strategy=strategy),
                on_suppressed=lambda s: log.fill_suppressed(mode, s, symbol=symbol, strategy=strategy),
                journal=False,
            )
            for tr in result.get("trades") or []:
                if tr.get("state") == "closed":
                    continue
                log.fill_trade(mode, tr, symbol=symbol, strategy=strategy)
            return log.list(mode=mode, strategy=strategy)
        finally:
            chan_core.apply_cfg(saved)

    def _pnl_drift(self, case):
        """当前参数中心与冻住的引擎参数不一致则标漂移。重算仍用冻住的那份。"""
        current = self._freeze_replay(case.get("btCfg") or {})
        frozen = case.get("replay") or {}
        return (current.get("chan_cfg") != frozen.get("chan_cfg")
                or current.get("engine_kwargs") != frozen.get("engine_kwargs"))

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
            out = []
            for cid in bl.get("caseIds", []):
                c = names.get(cid)
                if not c:
                    continue
                exp = (bl.get("expected") or {}).get(cid) or {}
                out.append({"caseId": cid, "caseName": c.get("name"),
                            "symbol": c.get("symbol"), "kind": "pnl",
                            "total": exp.get("total"), "frozenAt": exp.get("frozenAt"),
                            "positions": exp.get("positions") or []})
            return {"name": bl["name"], "kind": "pnl", "updatedAt": bl.get("updatedAt"),
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
                    if case.get("kind") == "pnl":
                        self._handle_pnl(job, idx, case, computed)
                        continue
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

    def _handle_pnl(self, job, idx, case, computed):
        """盈利用例：建立基线只拷贝已冻住的进出场；更新期望和跑基线才重跑引擎。"""
        self._set_item(job, idx, res="盈利", caseName=case["name"], kind="pnl")
        if not case.get("replay"):
            raise ValueError("盈利用例缺少冻结参数")
        drift = self._pnl_drift(case)
        if job["mode"] == "freeze" and not job.get("baselineId"):
            positions = case.get("positions") or []
            total = case.get("expectedTotal")
            self._set_item(job, idx, state="done", ok=True, drift=drift, details={
                "kind": "pnl", "expectedTotal": total, "actualTotal": total, "delta": 0})
            computed[case["id"]] = {"case": case, "kind": "pnl",
                                    "positions": positions, "total": total}
            return
        rows = self._replay_positions(case["replay"])
        total = compute_summary(rows)["total"]
        if job["mode"] == "run":
            bl = self._baseline(job["items"][idx]["baselineId"])
            exp = (bl.get("expected") or {}).get(case["id"]) or {}
            exp_total = exp.get("total")
            ok = exp_total == total
            delta = None if exp_total is None else round(total - exp_total, 2)
            details = {"kind": "pnl", "expectedTotal": exp_total,
                       "actualTotal": total, "delta": delta}
            if not ok:
                details["positions"] = align_positions(exp.get("positions") or [], rows)
            self._set_item(job, idx, state="done", ok=ok, drift=drift, details=details)
        else:
            self._set_item(job, idx, state="done", ok=True, drift=drift, details={
                "kind": "pnl", "expectedTotal": total, "actualTotal": total, "delta": 0})
        computed[case["id"]] = {"case": case, "kind": "pnl", "positions": rows, "total": total}

    def _write_freeze(self, job, computed):
        """冻结期望并落盘基线（新建或覆盖更新）：笔 + 买卖点标记。

        盈利用例改为冻住进出场和由它们算出的合计，不写笔。
        """
        now = time.time()
        expected = {}
        pnl = False
        for item in job["items"]:
            c = computed[item["caseId"]]
            if c.get("kind") == "pnl":
                pnl = True
                expected[item["caseId"]] = {
                    "total": c["total"], "positions": c["positions"], "frozenAt": now,
                }
                continue
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
