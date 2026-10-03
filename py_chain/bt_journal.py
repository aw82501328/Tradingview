# -*- coding: utf-8 -*-
"""全量回测交易日志（NDJSON）：回测运行时把逐笔决策上下文落盘，供事后零重算快查。

设计（SPEC_bt_journal.md，2026-09-30）：回测引擎（缠论 BacktestEngine / 强分型均线
FxMaEngine）在事件产生处顺手写一行 JSON——信号注记、成交止损位推导、出场触发叙事、
同向互斥过滤、候选被各闸门拒绝的原因、各周期状态变化。聊天侧用 bt_query 直接读
文件回答「为什么这笔开单 / 为什么在这里止损 / 为什么某时间没开单」，不再重放复现。

零开销口径：事件级（信号/成交/出场稀有）+ 状态变化级（前后快照比较后才写）+
拒绝去重（(周期, 策略, 段起点, 闸门) 首次 + ctx 变化才写）。写失败静默吞掉，
日志永不阻断回测；journal=False 可整体关闭。

文件：data/journal/bt_<strategy>_<symbol>_<yyyymmdd_HHMMSSmmm>.ndjson；
latest.json 记录 {strategy|symbol → path} 供查询定位。自动清理：每次新建日志时
删除超过 RETENTION_DAYS 天的旧日志，同一 (strategy, symbol) 组内无论如何保留
最近 RETENTION_MIN_RUNS 个。bt_runs SQLite 摘要不受影响。

本模块只依赖标准库（不 import 引擎/缠论核心），labels 单一来源供 mark_entry/
backtest/fx_ma（写侧）与 bt_query（读侧）共用。
"""

import json
import os
import re
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_DIR = os.path.join(REPO_ROOT, "data", "journal")
LATEST_JSON = os.path.join(JOURNAL_DIR, "latest.json")
RETENTION_DAYS = 30
RETENTION_MIN_RUNS = 10
FLUSH_EVERY = 500

# ---- 中文标签单一来源（写侧组句 / 读侧渲染共用） ----
DIR_LABELS = {"long": "做多", "short": "做空"}
EXIT_LABELS = {
    "breakeven": "TP1 保本", "half": "TP2 平一半", "close": "TP3 全平",
    "stopSr": "支阻位止损", "stopBe": "保本止损",
    "stop": "固定止损", "takeProfit": "固定止盈",
}
FILL_MODE_LABELS = {
    "anchor": "锚点当拍成交", "confirm": "确认成交（下一开盘）",
    "confirm-stale-anchor": "旧锚点回落确认成交", "nextOpen": "下一开盘成交",
}
# 闸门代码 → 中文（拒绝行渲染；code 可为「no_diverge 细分原因」）
GATE_LABELS = {
    "res_ge_trend": "检测周期≥趋势参考周期（结构性剔除）",
    "plan_watch": "计划观望（无进场策略）",
    "no_strategy_map": "计划策略无进场映射",
    "trend_filter": "逆顺势参考周期方向（顺势过滤）",
    "no_bis": "该周期无笔",
    "forming_wrong_type": "形成段方向不匹配（回调/反弹方向不符）",
    "expect_min_bars": "预期够笔段长不足",
    "counter_move_fail": "逆势段不合格（回调/反弹未确认）",
    "min_bars": "段长不足够笔门槛",
    "fired_dedup": "该段已发过信号（去重）",
    "strategy_extra": "策略专属条件未过",
    "cand_fired": "该形成段已发过（候选级去重）",
    "near_sr_fail": "不在支阻位附近",
    "eval_reason": "确认制条件未过",
    # realtimeLowerDiverge / _evalRealtimeNode 内部原因（gate=no_diverge 的细分）
    "sink_chain_short": "下沉链不通（停止级=检测周期自身，无严格更低级别背驰）",
    "few_bis": "该级笔数<2（无参照笔）",
    "wrong_type": "下沉节点段方向不匹配",
    "seg_short": "下沉节点段长不足够笔门槛",
    "no_refer": "无合格参照笔（或参照跨出上级笔）",
    "no_new_extreme": "未创新极值（且不在近等容差带内）",
    "macd_no_diverge": "MACD 对比不背驰",
    "near_equal_strict_fail": "近等带内护栏未过（三判据 AND）",
    "diverge_confirm_wait": "分型确认未到（divergeConfirm）",
    "macd_shrink_gate": "进场MACD柱缩闸未过",
    # fx_ma 闸门
    "fx_class_not_selected": "最新买卖点不属所选类别",
    "fx_point_fired": "该买卖点已触发/已作废",
    "fx_point_invalidated": "反向点已出现，点失效作废",
    "fx_point_expired": "点超有效期作废",
    "fx_point_drift_fail": "点超价距（等待价格回到范围内）",
    "fx_no_strong_fx": "点后未出现强分型",
    "fx_ma_not_ready": "均线未满周期",
    "fx_ma_gap_fail": "均线分离不足",
    "fx_ma_stand_not_ready": "站线均线未满周期",
    "fx_ma_stand_fail": "收盘未站上/站下站线均线",
    "fx_fib_no_ref": "无前一同侧买卖点（黄金分割无锚点）",
    "fx_fib_swing_fail": "黄金分割摆动段退化（前点后无有效极值）",
    "fx_fib_not_near": "不在黄金分割档位附近",
    "fx_upper_no_bi": "上级周期无笔（同向无法判定）",
    "fx_upper_dir_fail": "上级周期当前笔方向相反",
}


def gate_label(code):
    """闸门代码 → 中文（未知代码原样返回，前向兼容旧日志）。"""
    return GATE_LABELS.get(code, code)


def sanitize_symbol(symbol):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(symbol or "NA"))


def default_path(strategy="chan", symbol=None):
    now = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + f"{int(now % 1 * 1000):03d}"
    return os.path.join(JOURNAL_DIR, f"bt_{strategy}_{sanitize_symbol(symbol)}_{stamp}.ndjson")


def _parse_name(path):
    """文件名 → (strategy, symbol) 或 None（不匹配本命名约定）。"""
    m = re.match(r"bt_(.+?)_(.+?)_\d{8}_\d{9}\.ndjson$", os.path.basename(path))
    return (m.group(1), m.group(2)) if m else None


def cleanup_old(keep_for=None):
    """保留策略：同 (strategy, symbol) 组内保留最近 RETENTION_MIN_RUNS 个；再删掉
    修改时间超过 RETENTION_DAYS 天的其余文件。keep_for=(strategy, symbol) 时只清
    该组（本次新建日志的组）。latest.json 同步剔除失效条目。"""
    try:
        files = [f for f in os.listdir(JOURNAL_DIR) if f.endswith(".ndjson")]
    except OSError:
        return
    groups = {}
    for f in files:
        g = _parse_name(f)
        if g is None:
            continue
        p = os.path.join(JOURNAL_DIR, f)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        groups.setdefault(g, []).append((mt, p))
    cutoff = time.time() - RETENTION_DAYS * 86400
    for g, items in groups.items():
        if keep_for is not None and g != keep_for:
            continue
        items.sort(reverse=True)  # 新→旧
        for rank, (mt, p) in enumerate(items):
            if rank < RETENTION_MIN_RUNS:
                continue
            if mt < cutoff:
                try:
                    os.remove(p)
                except OSError:
                    pass


def _update_latest(strategy, symbol, path):
    try:
        state = {}
        if os.path.exists(LATEST_JSON):
            with open(LATEST_JSON, "r", encoding="utf-8") as fh:
                state = json.load(fh) if os.path.getsize(LATEST_JSON) else {}
        state = {k: v for k, v in state.items()
                 if isinstance(v, dict) and os.path.exists(v.get("path") or "")}
        state[f"{strategy}|{sanitize_symbol(symbol)}"] = {
            "path": path, "time": time.time(),
        }
        os.makedirs(JOURNAL_DIR, exist_ok=True)
        with open(LATEST_JSON, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
    except Exception:
        pass  # 指针文件失败不影响回测


def latest_path(symbol=None, strategy=None):
    """latest.json → 最新日志路径。优先 (strategy, symbol) 精确匹配；只给 symbol 时
    在该品种各组（chan/fxma/…）中取最新；都不命中再回退全局最新（无则 None）。"""
    try:
        with open(LATEST_JSON, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        return None
    if strategy is not None and symbol is not None:
        hit = state.get(f"{strategy}|{sanitize_symbol(symbol)}")
        if hit and os.path.exists(hit.get("path") or ""):
            return hit["path"]
    best = None
    if symbol is not None:
        suf = f"|{sanitize_symbol(symbol)}"
        for k, v in state.items():
            p = (v or {}).get("path")
            if str(k).endswith(suf) and p and os.path.exists(p) \
                    and (best is None or v.get("time", 0) > best[1]):
                best = (p, v.get("time", 0))
    if best is None:
        for v in state.values():
            p = (v or {}).get("path")
            if p and os.path.exists(p) and (best is None or v.get("time", 0) > best[1]):
                best = (p, v.get("time", 0))
    return best[0] if best else None


class BtJournal:
    """回测交易日志写入器（零开销口径：缓冲写 + 去重 + 状态变化检测）。

    引擎在事件产生处调用对应方法；所有写路径 try/except 静默——落盘是增强，
    绝不阻断回测主流程。行格式：一行一个 JSON 对象，ev 字段区分类型
    （header/state/reject/signal/fill/exit/trade_end/suppressed/footer）。
    """

    def __init__(self, path=None, strategy="chan", symbol=None, enabled=True,
                 register=True):
        self.enabled = bool(enabled)
        self.strategy = strategy
        self.symbol = symbol
        self.path = None
        self._fh = None
        self._n = 0
        self._seq = 0                # 信号自增 id
        self._t0 = time.time()
        self._state_keys = {}        # period -> 上次状态标识 tuple
        self._reject_sigs = {}       # (period,key,segStart,gate) -> 上次 ctx 签名
        # 测试污染防护：环境变量 PY_CHAIN_BT_JOURNAL=0 → 默认创建一律禁用
        # （各引擎测试模块在 import 时设置，避免 unittest 每次跑都往 data/journal 落文件）
        if enabled and os.environ.get("PY_CHAIN_BT_JOURNAL") == "0":
            enabled = False
            self.enabled = False
        if not self.enabled:
            return
        try:
            if register:
                cleanup_old(keep_for=(strategy, sanitize_symbol(symbol)))
            self.path = path or default_path(strategy, symbol)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._fh = open(self.path, "w", encoding="utf-8")
            if register:
                _update_latest(strategy, symbol, self.path)
        except Exception:
            self.enabled = False
            self._fh = None

    # ---------------- 基础写 ----------------

    def _write(self, row):
        if not self.enabled or self._fh is None:
            return
        try:
            self._fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            self._n += 1
            if self._n % FLUSH_EVERY == 0:
                self._fh.flush()
        except Exception:
            pass

    def close(self):
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---------------- 行类型 ----------------

    def header(self, cfg):
        self._write({"ev": "header", "ts": time.time(), "strategy": self.strategy,
                     "symbol": self.symbol, "cfg": cfg})
        # header 立即落盘：运行中的回测也能马上看到文件非空与实际配置
        try:
            if self._fh is not None:
                self._fh.flush()
        except Exception:
            pass

    def state(self, t, period, s, volatile=()):
        """周期紧凑状态，仅标识字段变化时落盘（volatile 列出的键每拍都变、只随行
        携带不参与变化判定——如形成段端点/均线值，参与判定会退化成逐 bar 写）。"""
        key = tuple(sorted((k, v) for k, v in s.items() if k not in volatile))
        if self._state_keys.get(period) == key:
            return
        self._state_keys[period] = key
        self._write({"ev": "state", "t": t, "period": period, **s})

    def reject(self, t, gate, period, seg_start=None, strategy_key=None, volatile=(), **ctx):
        """候选被闸门拒绝（去重：同键首次 + ctx 变化才写；ctx 为结构化数字，
        中文渲染在 bt_query）。volatile 列出的 ctx 键不参与变化判定（如均线差值
        每拍都变的近似数——只随首次记录，避免退化成逐 bar 写）。"""
        key = (period, strategy_key, seg_start, gate)
        try:
            sig = json.dumps({k: v for k, v in ctx.items() if k not in volatile},
                             sort_keys=True, default=str)
        except Exception:
            sig = str(ctx)
        if self._reject_sigs.get(key) == sig:
            return
        self._reject_sigs[key] = sig
        self._write({"ev": "reject", "t": t, "period": period, "gate": gate,
                     "segStart": seg_start, "strategyKey": strategy_key, "ctx": ctx})

    def signal(self, sig):
        """信号行（含产生点快照的 signalNote 中文叙事）；回填 sig["_jid"]=自增 id。"""
        self._seq += 1
        sig["_jid"] = self._seq
        self._write({"ev": "signal", "id": self._seq, "t": sig.get("time"),
                     "periodX": sig.get("periodX"), "markRes": sig.get("markRes"),
                     "direction": sig.get("direction"),
                     "strategyKey": sig.get("strategyKey"),
                     "strategyLabel": sig.get("strategyLabel"),
                     "price": sig.get("price"), "nearSr": sig.get("nearSr"),
                     "segStart": sig.get("segStart"),
                     "planDirection": sig.get("planDirection"),
                     "trendDirection": sig.get("trendDirection"),
                     "trendReason": sig.get("trendReason"),
                     "note": sig.get("signalNote") or sig.get("reason"),
                     "fallback": sig.get("fallback", False),
                     "nearEqual": sig.get("nearEqual", False),
                     "expectBi": sig.get("expectBi", False)})
        return self._seq

    def fill(self, trade):
        """成交行（entryWhy 含止损位推导叙事；journalId 关联信号行）。"""
        self._write({"ev": "fill", "id": trade.get("journalId"),
                     "tradeNo": trade.get("tradeNo"), "t": trade.get("entryTime"),
                     "periodX": trade.get("periodX"), "markRes": trade.get("markRes"),
                     "direction": trade.get("direction"),
                     "strategyKey": trade.get("strategyKey"),
                     "signalTime": trade.get("signalTime"),
                     "signalPrice": trade.get("signalPrice"),
                     "entryTime": trade.get("entryTime"),
                     "entryPrice": trade.get("entryPrice"),
                     "fillMode": trade.get("fillMode"),
                     "nearSr": trade.get("nearSr"),
                     "stopRef": trade.get("stopRef"), "beStop": trade.get("beStop"),
                     "maxLoss": trade.get("maxLoss"),
                     "stopSource": trade.get("stopSource"),
                     "tpRef": trade.get("tpRef"),
                     "lots": trade.get("lots"), "mult": trade.get("mult"),
                     "why": trade.get("entryWhy")})

    def exit_event(self, trade_no, ev, journal_id=None):
        """单个出场事件行（breakeven/half/close/stopSr/stopBe/stop/takeProfit）。"""
        self._write({"ev": "exit", "id": journal_id, "tradeNo": trade_no,
                     "type": ev.get("type"), "t": ev.get("time"),
                     "price": ev.get("price"), "why": ev.get("why")})

    def trade_end(self, trade):
        """终局行（state=closed 终局出场+pnl / state=open 期末 mark-to-market）。"""
        self._write({"ev": "trade_end", "id": trade.get("journalId"),
                     "tradeNo": trade.get("tradeNo"), "state": trade.get("state"),
                     "exitType": trade.get("exitType"), "t": trade.get("exitTime"),
                     "price": trade.get("exitPrice"), "pnl": trade.get("pnl"),
                     "why": trade.get("exitWhy"),
                     "entryTime": trade.get("entryTime"),
                     "entryPrice": trade.get("entryPrice"),
                     "direction": trade.get("direction")})

    def suppressed(self, t, sig, why=None):
        """同向互斥被滤行。"""
        self._write({"ev": "suppressed", "t": t, "periodX": sig.get("periodX"),
                     "markRes": sig.get("markRes"), "direction": sig.get("direction"),
                     "strategyKey": sig.get("strategyKey"),
                     "signalTime": sig.get("time"), "price": sig.get("price"),
                     "id": sig.get("_jid"), "why": why})

    def footer(self, stats, extra=None):
        self._write({"ev": "footer", "ts": time.time(),
                     "wall": round(time.time() - self._t0, 3),
                     "rows": self._n, "stats": stats, "extra": extra or {}})
