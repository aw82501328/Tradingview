# -*- coding: utf-8 -*-
"""
回归基线留档：在缓存数据上跑双模式（realtime/confirm）全量回测，保存 stats + 信号 + 成交。

用法：
    python -m py_chain.dump_baseline                # 输出到 py_chain/.baseline/
    python -m py_chain.dump_baseline --out-dir py_chain/.after --from 2026-07-02

SPEC_divergence_chanset.md 要求：改动前必须留档（回归基线），改动后重跑对比。
"""

import argparse
import calendar
import datetime
import json
import os
import sys
import time


def main(argv=None):
    ap = argparse.ArgumentParser(description="双模式回测基线留档")
    ap.add_argument("--from", dest="from_date", default="2026-07-02", help="起始日期 YYYY-MM-DD（UTC）")
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(__file__), ".baseline"))
    ap.add_argument("--warmup", type=int, default=60)
    ap.add_argument("--signal-modes", default="realtime,confirm")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    from .data_loader import load_bars
    from .backtest import run_backtest, summarize, DEFAULT_PERIODS
    from .chan_core import fmtT

    y, m, d = (int(x) for x in args.from_date.split("-"))
    from_ts = calendar.timegm(datetime.datetime(y, m, d).timetuple())
    bars = load_bars(periods=DEFAULT_PERIODS, from_ts=from_ts, use_cache=True)
    for res in DEFAULT_PERIODS:
        n = len(bars.get(res, []))
        if n:
            print(f"  {res:>4}: {n} 根（{fmtT(bars[res][0]['time'])} ~ {fmtT(bars[res][-1]['time'])}）")

    os.makedirs(args.out_dir, exist_ok=True)
    for mode in [m_.strip() for m_ in args.signal_modes.split(",") if m_.strip()]:
        t0 = time.time()
        result = run_backtest(bars, periods=DEFAULT_PERIODS, warmup_bars=args.warmup,
                              with_marks=False, signal_mode=mode, log=lambda *a: None)
        cost = time.time() - t0
        signals_flat = [
            dict(s, markRes=markRes)
            for markRes, sigs in result["signals"].items()
            for s in sigs
        ]
        signals_flat.sort(key=lambda s: (s.get("time") or 0,))
        out = {
            "signalMode": mode,
            "fromDate": args.from_date,
            "elapsedSec": round(cost, 1),
            "summary": summarize(result),
            "stats": result["stats"],
            "signals": signals_flat,
            "trades": result["trades"],
            "lastTime": result["lastTime"],
        }
        path = os.path.join(args.out_dir, f"{mode}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, default=str)
        s = out["summary"]
        print(f"[{mode}] 信号 {s['信号数']} 成交 {s['成交数']} 已平仓 {s['已平仓数']} "
              f"已实现盈亏 {s['已平仓盈亏']} （{cost:.0f}s）→ {path}")
        print(f"  markRes 分布: {s['按背驰级别分布']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
