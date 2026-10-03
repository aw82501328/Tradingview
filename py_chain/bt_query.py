# -*- coding: utf-8 -*-
"""回测交易日志快查 CLI：直接读 data/journal/*.ndjson，零重算零 CDP。

用法（python -m py_chain.bt_query ...）：
  --list                        交易总表 + 统计（缺省动作）
  --trade N                     第 N 笔成交：信号→进场（止损位推导）→逐出场事件→终局
  --signal N                    信号 id=N（含未成交/被同向过滤的信号）
  --time "9-5 02:00"            该时刻前后事件时间线（--window 小时数，默认 6）
  --why-not "9-5 02:00" [--period 60]   为什么该时刻没开单：各周期当时状态 + 被拒记录
  --file PATH / --symbol SYM    指定日志（缺省 latest.json 定位最新；symbol 精确匹配组）
  --json                        机器可读输出（折叠后的完整结构）
  --header                      只看运行配置

时间串支持：M-D / M-D HH:MM / YYYY-M-D[ HH:MM] / 纯秒级时间戳（无年份时按
header.startTs→当前年份推断）。日志文件缺失/被清理时提示（bt_runs SQLite 摘要仍在）。
"""

import argparse
import json
import os
import re
import sys
import time as _time

from .bt_journal import (JOURNAL_DIR, latest_path, gate_label,
                         DIR_LABELS, EXIT_LABELS, FILL_MODE_LABELS)

try:
    from .chan_core import fmtT
except Exception:  # 独立可跑（无缠论核心环境时兜底）
    def fmtT(ts):
        return _time.strftime("%m-%d %H:%M", _time.localtime(ts)) if ts else "?"


def parse_time(s, year_hint=None):
    """'9-5'/'9-5 02:00'/'2026-9-5 02:00'/epoch → 秒级时间戳；解析失败返回 None。"""
    s = str(s).strip()
    if re.fullmatch(r"\d{9,}", s):
        return int(s)
    m = re.fullmatch(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if not m:
        return None
    y = int(m.group(1)) if m.group(1) else (year_hint or _time.localtime().tm_year)
    import datetime
    try:
        dt = datetime.datetime(y, int(m.group(2)), int(m.group(3)),
                               int(m.group(4) or 0), int(m.group(5) or 0))
    except ValueError:
        return None
    return int(dt.timestamp())


class Journal:
    """NDJSON 日志折叠读取（末行损坏容忍）。"""

    def __init__(self, path):
        self.path = path
        self.rows = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.rows.append(json.loads(line))
                except Exception:
                    continue  # 坏行（如中途断电的半行）忽略
        self.header = next((r for r in self.rows if r.get("ev") == "header"), {})
        self.footer = next((r for r in reversed(self.rows) if r.get("ev") == "footer"), None)
        self.signals = {r["id"]: r for r in self.rows if r.get("ev") == "signal"}
        self.fills = {r["tradeNo"]: r for r in self.rows if r.get("ev") == "fill"}
        self.ends = {r["tradeNo"]: r for r in self.rows if r.get("ev") == "trade_end"}
        self.exits = {}
        for r in self.rows:
            if r.get("ev") == "exit":
                self.exits.setdefault(r.get("tradeNo"), []).append(r)
        self.states = {}   # period -> [rows]（升序）
        for r in self.rows:
            if r.get("ev") == "state":
                self.states.setdefault(r.get("period"), []).append(r)
        self.rejects = [r for r in self.rows if r.get("ev") == "reject"]
        self.suppressed = [r for r in self.rows if r.get("ev") == "suppressed"]

    def state_at(self, period, t):
        """period 在 t（含）之前最后一条状态行（无则 None）。"""
        best = None
        for r in self.states.get(period) or []:
            if (r.get("t") or 0) <= t:
                best = r
            else:
                break
        return best

    def year_hint(self):
        cfg = self.header.get("cfg") or {}
        for k in ("startTs", "toTs"):
            if cfg.get(k):
                return _time.localtime(cfg[k]).tm_year
        return None


def resolve_file(path=None, symbol=None, strategy=None):
    if path:
        return path if os.path.exists(path) else None
    p = latest_path(symbol=symbol, strategy=strategy) if (symbol or strategy) else latest_path()
    return p


# ---------------- 渲染 ----------------

def _f2(v):
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def fmt_reject(r):
    """拒绝行 → 中文一行（闸门标签 + ctx 数字叙事）。"""
    ctx = r.get("ctx") or {}
    g = r.get("gate")
    seg = r.get("segStart")
    head = f"[{r.get('period')}] {gate_label(g)}"
    if seg:
        head += f"（段自 {fmtT(seg)}）"
    if r.get("strategyKey"):
        head += f" <{r['strategyKey']}>"
    detail = {
        "near_sr_fail": lambda c: (f"背驰点价 {_f2(c.get('price'))}，最近支阻位 "
                                   f"{_f2(c.get('srPrice'))} 距离 {c.get('dist')} > "
                                   f"near {c.get('near')}（markRes={c.get('markRes')}）"),
        "min_bars": lambda c: f"形成段合并 {c.get('count')} 根 < 够笔门槛 {c.get('need')} 根",
        "expect_min_bars": lambda c: f"预期够笔段合并 {c.get('count')} 根 < 门槛 {c.get('need')} 根",
        "strategy_extra": lambda c: str(c.get("reason") or ""),
        "plan_watch": lambda c: (f"计划 {c.get('planDir')}（{c.get('planStrategy')}）："
                                 f"{c.get('reason') or ''}"),
        "no_strategy_map": lambda c: f"计划策略「{c.get('planStrategy')}」无进场映射",
        "trend_filter": lambda c: (f"顺势参考周期方向 {c.get('trendDir')}，本策略需 "
                                   f"{c.get('need')}（{c.get('trendReason') or ''}）"),
        "forming_wrong_type": lambda c: (f"形成段为 {c.get('lastType')}，需 {c.get('want')}"
                                         "（方向不符）"),
        "no_new_extreme": lambda c: (f"{c.get('res')} 段极值 {_f2(c.get('extreme'))} 未破参照端点 "
                                     f"{_f2(c.get('refExtreme'))}（近等容差 {c.get('tol')}；"
                                     f"参照 {fmtT(c.get('refTime')) if c.get('refTime') else '?'}）"),
        "macd_no_diverge": lambda c: f"{c.get('res')} MACD 对比不背驰（动能未衰竭）",
        "macd_shrink_gate": lambda c: (f"最近两根柱 |macd| {c.get('prev')}→{c.get('last')}"
                                       " 未收缩"),
        "diverge_confirm_wait": lambda c: f"分型确认时刻 {fmtT(c.get('confirmAt'))} 未到",
        "seg_short": lambda c: f"{c.get('res')} 下沉段合并 {c.get('count')} 根 < {c.get('need')} 根",
        "fx_ma_gap_fail": lambda c: (f"均线分离 {c.get('diff')} < {c.get('crossMinPts')} 点"
                                     f"（{c.get('ptType')}）"),
        "fx_ma_stand_not_ready": lambda c: f"站线均线（{c.get('maKind')}类）未满周期（{c.get('ptType')}）",
        "fx_ma_stand_fail": lambda c: (f"收盘 {c.get('close')} 未站上/站下 "
                                       f"MA{c.get('maP')} {c.get('ma')}（{c.get('ptType')}）"),
        "fx_no_strong_fx": lambda c: f"{c.get('ptType')} 之后未出现强分型（{c.get('kind')}）",
        "fx_class_not_selected": lambda c: (f"最新点 {c.get('ptType')} 不属所选类别 "
                                            f"{c.get('classes')}"),
        "fx_point_invalidated": lambda c: (f"{c.get('ptType')} 被反向点 {c.get('oppType')}"
                                           f"（{fmtT(c.get('oppTime')) if c.get('oppTime') else '?'}）作废"),
        "fx_fib_no_ref": lambda c: f"{c.get('ptType')} 无前一同侧买卖点（摆动段无锚点）",
        "fx_fib_swing_fail": lambda c: (f"摆动段退化：前点 {c.get('refPrice')}，极值 "
                                        f"{c.get('ext')}（{c.get('ptType')}）"),
        "fx_fib_not_near": lambda c: (f"{c.get('ptType')} 价 {c.get('ptPrice')} 距最近档位 "
                                      f"{c.get('ratio')}（{c.get('level')}，摆动 {c.get('refPrice')}"
                                      f"→{c.get('ext')}）{c.get('gap')} > 容差 {c.get('tol')} 点"),
        "fx_upper_no_bi": lambda c: f"上级周期 {c.get('upper')} 无笔（{c.get('ptType')}）",
        "fx_upper_dir_fail": lambda c: (f"上级 {c.get('upper')} 当前笔 {c.get('upperDir')}，"
                                        f"与信号方向相反（{c.get('ptType')}）"),
    }.get(g)
    body = f"：{detail(ctx)}" if detail else (f"：{ctx}" if ctx else "")
    return f"{fmtT(r.get('t'))} {head}{body}"


def fmt_state(r):
    p = r.get("period")
    if "planDir" in r or "planStrategy" in r:   # 缠论引擎
        seg = (f"形成段 {r.get('segType')}{'(延伸中)' if r.get('segForming') else ''}"
               f" 自 {fmtT(r.get('segStart'))} → {_f2(r.get('segEndPrice'))}"
               if r.get("segStart") else "无笔")
        tr = f"，趋势参考 {r.get('trendDir')}" if r.get("trendDir") else ""
        pl = (f"计划 {r.get('planDir')}/{r.get('planStrategy')}"
              if r.get("planStrategy") else f"计划 {r.get('planDir') or '无'}")
        reason = f"（{r.get('planReason')}）" if r.get("planReason") else ""
        return f"[{p}] {pl}{reason}{tr}；{seg}"
    # fx_ma 引擎
    pts = []
    if r.get("buyPtType"):
        pts.append(f"买点 {r['buyPtType']} @ {fmtT(r['buyPtTime'])}")
    if r.get("sellPtType"):
        pts.append(f"卖点 {r['sellPtType']} @ {fmtT(r['sellPtTime'])}")
    ma = (f"MA1 {_f2(r.get('ma1Fast'))}/{_f2(r.get('ma1Slow'))}"
          f" MA2 {_f2(r.get('ma2Fast'))}/{_f2(r.get('ma2Slow'))}")
    stand = (f" 站MA {_f2(r.get('maStand1'))}/{_f2(r.get('maStand2'))}"
             if r.get("maStand1") is not None or r.get("maStand2") is not None else "")
    upper = (f" 上级{ {'up': '↑', 'down': '↓'}.get(r.get('upperDir'), r.get('upperDir')) }"
             if r.get("upperDir") else "")
    return f"[{p}] {'；'.join(pts) or '暂无买卖点'}；{ma}{stand}{upper}"


def show_header(j):
    h = j.header
    cfg = h.get("cfg") or {}
    res_txt = cfg.get("periods") or cfg.get("entryRes")
    print(f"策略 {h.get('strategy')}｜品种 {h.get('symbol')}｜周期 {res_txt}"
          f"｜fine {cfg.get('fineRes') or cfg.get('fine_res')}")
    if cfg.get("engine") == "fx_ma" or h.get("strategy") == "fxma":
        print(f"entryRes={cfg.get('entryRes')} 类别={cfg.get('pointClasses')} "
              f"{cfg.get('maType')} {cfg.get('ma1')}/{cfg.get('ma2')} "
              f"分离≥{cfg.get('crossMinPts')} 止损{cfg.get('stopPts')}/止盈{cfg.get('tpPts')}")
    else:
        print(f"成交口径 {cfg.get('fillMode')}｜信号模式 {cfg.get('signalMode')}｜"
              f"lots={cfg.get('lots')} near={cfg.get('near')} "
              f"止损滑点={cfg.get('slipStop')} 保本滑点={cfg.get('slipBe')}")
    if j.footer:
        st = j.footer.get("stats") or {}
        print(f"结果：{st.get('steps')} 步，信号 {st.get('signals')}，成交 {st.get('executed')}，"
              f"同向过滤 {st.get('suppressed')}，已平仓 {st.get('closed')}，"
              f"止损 {st.get('stopped', 0)}（耗时 {j.footer.get('wall')}s，"
              f"日志 {j.footer.get('rows')} 行）")


def show_list(j):
    show_header(j)
    print()
    print(f"{'单':>3} {'向':<4} {'进场':<12} {'进场价':>9} {'周期':>4} {'策略':<14} "
          f"{'出场':<10} {'盈亏':>10}")
    for no in sorted(j.fills):
        f, e = j.fills[no], j.ends.get(no) or {}
        print(f"{no:>3} {DIR_LABELS.get(f.get('direction'), '?'):<4} "
              f"{fmtT(f.get('entryTime')):<12} {_f2(f.get('entryPrice')):>9} "
              f"{str(f.get('periodX')):>4} {str(f.get('strategyKey')):<14} "
              f"{EXIT_LABELS.get(e.get('exitType'), e.get('exitType') or ('持仓中' if e.get('state') == 'open' else '?')):<10} "
              f"{_f2(e.get('pnl')):>10}")
    n_sig = len(j.signals)
    n_sup = len(j.suppressed)
    print(f"\n信号 {n_sig} 条（成交 {len(j.fills)}，同向过滤 {n_sup}，未成交 "
          f"{max(0, n_sig - len(j.fills) - n_sup)}）；拒绝记录 {len(j.rejects)} 条。")
    print("提示：--trade N 看单笔叙事；--signal N 看信号；--why-not \"9-5 02:00\" 查为何没开单。")


def show_trade(j, no):
    f = j.fills.get(no)
    if f is None:
        print(f"没有第 {no} 笔成交（--list 查看全部）")
        return
    sig = j.signals.get(f.get("id")) or {}
    e = j.ends.get(no) or {}
    print(f"—— 第 {no} 笔（{'持仓中' if e.get('state') == 'open' else '已平仓'}）——")
    if sig.get("note"):
        print(f"信号：{sig['note']}")
    if f.get("why"):
        print(f"进场：{f['why']}")
    for ev in j.exits.get(no) or []:
        w = f"：{ev['why']}" if ev.get("why") else ""
        print(f"出场 {fmtT(ev.get('t'))} {EXIT_LABELS.get(ev.get('type'), ev.get('type'))} "
              f"@ {_f2(ev.get('price'))}{w}")
    if e:
        if e.get("why"):
            print(f"终局：{e['why']}")
        print(f"结果：{EXIT_LABELS.get(e.get('exitType'), e.get('exitType') or '-')} "
              f"@ {_f2(e.get('price'))}，盈亏 {_f2(e.get('pnl'))}")


def show_signal(j, sid):
    s = j.signals.get(sid)
    if s is None:
        print(f"没有 id={sid} 的信号（--list 查看）")
        return
    print(f"—— 信号 id={sid} ——")
    if s.get("note"):
        print(f"注记：{s['note']}")
    print(f"{DIR_LABELS.get(s.get('direction'), '?')} {s.get('strategyKey')}｜"
          f"检测周期 {s.get('periodX')}｜背驰级别 {s.get('markRes')}｜"
          f"信号 {fmtT(s.get('t'))} @ {_f2(s.get('price'))}"
          + (f"｜近支阻位 {_f2(s.get('nearSr'))}" if s.get("nearSr") else ""))
    flags = [k for k in ("fallback", "nearEqual", "expectBi") if s.get(k)]
    if flags:
        print(f"标记：{'、'.join(flags)}")
    # 后续去向：成交 / 被滤
    for no, f in sorted(j.fills.items()):
        if f.get("id") == sid:
            print(f"→ 已成交为第 {no} 笔（--trade {no} 看完整叙事）")
            return
    for sup in j.suppressed:
        if sup.get("id") == sid:
            print(f"→ 被同向过滤：{sup.get('why')}")
            return
    print("→ 未成交（信号拍之后无下一根K线，或成交前被同向互斥）")


def show_time(j, t, window_h):
    lo, hi = t - window_h * 3600, t + window_h * 3600
    print(f"—— {fmtT(t)} 前后事件时间线（±{window_h}h）——")
    evs = []
    for r in j.rows:
        et = r.get("t")
        if et is None or not (lo <= et <= hi):
            continue
        k = r.get("ev")
        if k == "signal":
            evs.append((et, f"信号 id={r['id']} {DIR_LABELS.get(r.get('direction'))} "
                            f"{r.get('strategyKey')} @ {_f2(r.get('price'))}"))
        elif k == "fill":
            evs.append((et, f"成交 第{r.get('tradeNo')}笔 进场 {_f2(r.get('entryPrice'))}"))
        elif k == "exit":
            evs.append((et, f"出场 第{r.get('tradeNo')}笔 "
                            f"{EXIT_LABELS.get(r.get('type'), r.get('type'))} @ {_f2(r.get('price'))}"))
        elif k == "trade_end":
            evs.append((et, f"终局 第{r.get('tradeNo')}笔 {r.get('exitType') or '持仓中'} "
                            f"pnl {_f2(r.get('pnl'))}"))
        elif k == "suppressed":
            evs.append((et, f"同向过滤 {r.get('strategyKey')}：{r.get('why')}"))
        elif k == "reject":
            evs.append((et, fmt_reject(r)))
    if not evs:
        print("（该窗口无信号/成交/出场/拒绝事件——扩大 --window 或查 --why-not）")
    for et, txt in sorted(evs):
        print(f"{fmtT(et)} {txt}")
    print("\n当时各周期状态：")
    for p in sorted(j.states):
        r = j.state_at(p, t)
        if r:
            print(f"  {fmtT(r.get('t'))} {fmt_state(r)}")


def show_why_not(j, t, period=None):
    print(f"—— 为什么 {fmtT(t)} 没开单 ——")
    periods = [period] if period else sorted(j.states)
    # 1) 该时刻前后其实有没有信号/被滤（常见误会：有信号但同向持仓挡住）
    near = [r for r in j.rows if r.get("ev") in ("signal", "suppressed")
            and r.get("t") is not None and abs(r["t"] - t) <= 24 * 3600]
    for r in sorted(near, key=lambda x: x["t"]):
        if r["ev"] == "signal":
            print(f"！前后24h其实有信号：id={r['id']} {DIR_LABELS.get(r.get('direction'))} "
                  f"{r.get('strategyKey')} @ {fmtT(r['t'])}（--signal {r['id']}）")
        else:
            print(f"！前后24h有信号被同向过滤：{r.get('strategyKey')} @ {fmtT(r.get('t'))}："
                  f"{r.get('why')}")
    # 2) 各周期当时门控状态 + 之前最后的拒绝原因（按闸门取最近一条）
    for p in periods:
        r = j.state_at(p, t)
        if r:
            print(f"\n[{p}]（状态 @ {fmtT(r.get('t'))}）{fmt_state(r)}")
        rjs = [x for x in j.rejects
               if x.get("period") == p and (x.get("t") or 0) <= t]
        if not rjs:
            print("  （该周期无拒绝记录——候选可能从未到达闸门，或已发过信号）")
            continue
        # 每个闸门取 t 前最后一条（去重日志的「最近一次出现」），按时间倒序展示
        by_gate = {}
        for x in rjs:
            by_gate[(x.get("gate"), x.get("segStart"), x.get("strategyKey"))] = x
        last = sorted(by_gate.values(), key=lambda x: -(x.get("t") or 0))[:6]
        for x in last:
            age = t - (x.get("t") or 0)
            age_txt = f"{age/3600:.1f}h 前" if age < 48 * 3600 else f"{age/86400:.1f} 天前"
            print(f"  · {fmt_reject(x)}〔{age_txt}〕")


def main(argv=None):
    ap = argparse.ArgumentParser(description="回测交易日志快查（零重算）",
                                 prog="python -m py_chain.bt_query")
    ap.add_argument("--file", help="日志文件路径（缺省 latest.json 定位最新）")
    ap.add_argument("--symbol", help="按品种定位最新日志（如 OANDA:XAUUSD）")
    ap.add_argument("--strategy", help="配合 --symbol 精确定位（chan/fxma）")
    ap.add_argument("--list", action="store_true", help="交易总表 + 统计（缺省动作）")
    ap.add_argument("--trade", type=int, help="第 N 笔成交完整叙事")
    ap.add_argument("--signal", type=int, help="信号 id=N（含未成交/被滤）")
    ap.add_argument("--time", dest="at_time", help="时刻（9-5 / 9-5 02:00 / 2026-9-5 02:00）")
    ap.add_argument("--window", type=float, default=6.0, help="--time 的前后窗口小时数")
    ap.add_argument("--why-not", dest="why_not", help="为什么该时刻没开单")
    ap.add_argument("--period", help="--why-not 限定周期（如 60）")
    ap.add_argument("--header", action="store_true", help="只看运行配置")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    path = resolve_file(args.file, args.symbol, args.strategy)
    if not path:
        print(f"未找到交易日志（data/journal/ 下无文件或 latest.json 失效）。\n"
              f"先跑一次全量回测（Web 回测卡 / python -m py_chain.main）自动生成；\n"
              f"旧日志可能已被自动清理（保留 30 天 + 每品种至少 10 次），"
              f"bt_runs SQLite 摘要仍可查。当前目录：{JOURNAL_DIR}")
        return 1
    j = Journal(path)
    if not j.rows:
        print(f"日志为空：{path}")
        return 1

    if args.json:
        print(json.dumps({
            "path": path, "header": j.header, "footer": j.footer,
            "signals": list(j.signals.values()), "fills": list(j.fills.values()),
            "exits": j.exits, "ends": list(j.ends.values()),
            "states": j.states, "rejects": j.rejects,
            "suppressed": j.suppressed,
        }, ensure_ascii=False, indent=1, default=str))
        return 0

    print(f"日志：{path}")
    if args.header:
        show_header(j)
    elif args.trade is not None:
        show_trade(j, args.trade)
    elif args.signal is not None:
        show_signal(j, args.signal)
    elif args.at_time:
        t = parse_time(args.at_time, j.year_hint())
        if t is None:
            print(f"时间串无法解析：{args.at_time}")
            return 1
        show_time(j, t, args.window)
    elif args.why_not:
        t = parse_time(args.why_not, j.year_hint())
        if t is None:
            print(f"时间串无法解析：{args.why_not}")
            return 1
        show_why_not(j, t, args.period)
    else:
        show_list(j)
    return 0


if __name__ == "__main__":
    sys.exit(main())
