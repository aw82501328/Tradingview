# -*- coding: utf-8 -*-
"""MT5 重采样 vs bars.db OANDA 对拍（M2 数据层锚点物证）。

对比同窗口 15/240/D：时间戳匹配率、OHLC 差分布（中位/p95/max）、缺口归因、
DST 两规则（us/eu）240 边界匹配率、按小时点差分布。
报告打印 + JSON 落盘 data/mt5_align_<date>.json。

运行前提：MT5 终端已登录 EXNESS。用法：
    py -3.12 -m py_chain.mt5_align --days 14
"""

import json
import os
import statistics
import time
from datetime import datetime, timezone

from .mt5_feed import (MT5Feed, ny_day_start_utc, offset_at, resample,
                       session_keep, srv_to_utc)
from . import data_store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MATCH_ANCHOR = {"15": 0.99, "240": 0.995, "D": 0.995}   # 时间戳匹配率锚点（初始值）


def _pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    k = min(int(len(vals) * p / 100), len(vals) - 1)
    return vals[k]


def _ohlc_diff_stats(pairs, field):
    """pairs=[(oanda_bar, exness_bar)] 同时间戳对；返回单字段差分布。"""
    ds = [abs(a[field] - b[field]) for a, b in pairs]
    return {"n": len(ds), "median": round(statistics.median(ds), 3) if ds else None,
            "p95": round(_pct(ds, 95), 3), "max": round(max(ds), 3) if ds else None}


def compare_window(days=14, symbol="XAUUSD", log=print):
    """拉 MT5 M1 重采样 vs bars.db OANDA，产出对拍报告 dict。"""
    feed = MT5Feed(symbol=symbol)
    feed.connect()
    try:
        now = int(time.time())
        t0 = now - days * 86400
        # 时区自检：请求窗口首末差应≈窗口长（否则 copy_rates_range 参数口径有误）
        raw = feed.m1_range(t0, now, with_spread=True)
        feed.log(f"M1 拉取 {len(raw)} 根（{datetime.fromtimestamp(raw[0]['time'], timezone.utc)}"
                 f" ~ {datetime.fromtimestamp(raw[-1]['time'], timezone.utc)}）")
        span = raw[-1]["time"] - raw[0]["time"]
        tz_selfcheck = {"req_span_sec": now - t0, "got_span_sec": span,
                        "ok": abs((now - t0) - span) < 3 * 86400}

        kept = [b for b in raw if session_keep(b["time"], "drop")]
        exn = {res: {b["time"]: b for b in resample(kept, res, weekend="keep")}
               for res in ("15", "240", "D")}
        ond = {res: {b["time"]: b for b in data_store.load_store(
            "OANDA:XAUUSD", [res], from_ts=t0, to_ts=now)[res]}
            for res in ("15", "240", "D")}

        report = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "window_days": days, "symbol": symbol, "tz_selfcheck": tz_selfcheck,
                  "res": {}}

        for res in ("15", "240", "D"):
            e, o = exn[res], ond[res]
            common = sorted(set(e) & set(o))
            pairs = [(o[t], e[t]) for t in common]
            only_exn = [t for t in sorted(set(e) - set(o))]
            only_ond = [t for t in sorted(set(o) - set(e))]
            # 缺口归因：weekend/维护窗/假日早收/流动性
            def attr(ts_list):
                tags = {"weekend": 0, "maintenance": 0, "other": 0}
                for t in ts_list:
                    if not session_keep(t, "drop"):
                        tags["weekend"] += 1
                    elif t < ny_day_start_utc(t) + 3600:
                        tags["maintenance"] += 1
                    else:
                        tags["other"] += 1
                return tags
            match_rate = len(common) / max(len(o), 1)
            report["res"][res] = {
                "oanda_count": len(o), "exness_count": len(e), "common": len(common),
                "match_rate": round(match_rate, 4),
                "match_anchor": MATCH_ANCHOR[res], "match_ok": match_rate >= MATCH_ANCHOR[res],
                "ohlc_diff": {f: _ohlc_diff_stats(pairs, f)
                              for f in ("open", "high", "low", "close")},
                "exness_only": {"count": len(only_exn), "attr": attr(only_exn),
                                "sample": only_exn[:10]},
                "oanda_only": {"count": len(only_ond), "attr": attr(only_ond),
                               "sample": only_ond[:10]},
            }

        # DST 规则判定：UTC M1 反推回服务器时间，再分别按 us/eu 正转重采样 240，
        # 与 OANDA 240 边界匹配率高者为应选规则（仅历史段受规则影响；近3天走动态offset）
        rules = {}
        rates = feed.m1_range(now - 14 * 86400, now)
        for rule in ("us", "eu"):
            m1_r = []
            for b in rates:
                srv = b["time"] + offset_at(b["time"], feed.rule)   # 反推服务器时间
                m1_r.append({"time": srv_to_utc(srv, rule), "open": b["open"],
                             "high": b["high"], "low": b["low"], "close": b["close"]})
            m1_r = [b for b in m1_r if session_keep(b["time"], "drop")]
            alt240 = {b["time"] for b in resample(m1_r, "240", weekend="keep")}
            o240 = {b["time"] for b in data_store.load_store(
                "OANDA:XAUUSD", ["240"], from_ts=now - 14 * 86400, to_ts=now)["240"]}
            rules[rule] = round(len(alt240 & o240) / max(len(o240), 1), 4)
        report["dst_rule_match_240"] = rules
        report["dst_rule_pick"] = max(rules, key=rules.get)

        # 点差按服务器小时分布（M1 spread 字段，点×point）
        si = feed.spec()
        by_hour = {}
        for b in raw:
            if "spread" not in b:
                continue
            h = datetime.fromtimestamp(b["time"], timezone.utc).hour
            by_hour.setdefault(h, []).append(b["spread"] * si["point"])
        report["spread_by_utc_hour"] = {
            h: {"n": len(v), "median": round(statistics.median(v), 3),
                "p95": round(_pct(v, 95), 3)}
            for h, v in sorted(by_hour.items())}
        return report
    finally:
        feed.close()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="MT5 vs OANDA 对拍")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--out", default=None, help="报告路径（默认 data/mt5_align_<date>.json）")
    args = ap.parse_args(argv)
    report = compare_window(days=args.days, symbol=args.symbol)
    out = args.out or os.path.join(
        REPO_ROOT, "data",
        f"mt5_align_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("spread_by_utc_hour",)}, ensure_ascii=False, indent=2))
    print(f"\n报告已落盘：{out}")


if __name__ == "__main__":
    main()
