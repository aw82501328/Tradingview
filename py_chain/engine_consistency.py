# -*- coding: utf-8 -*-
"""增量引擎 vs batch 前缀等价检查（SPEC_divergence_chanset.md 回归 5.5）

正确的一致性定义：引擎逐根推进到时刻 t 后的 self._bis == build_bars(bars[:cut(t)])——
「引擎在 t 的状态」应恰等于「用 t 之前的数据全量重建」（无未来函数 + 增量无漂移）。
（与「用全历史 batch 重建」比较无意义：markWickBars 的稳定 ATR 随窗口增长变化，
全历史窗口不是图表当刻的口径；前缀等价才是与「图表当时所见」一致的语义。）

用法：python -m py_chain.engine_consistency [bars.json] [--every N]
"""

import argparse
import sys

from .backtest import BacktestEngine, build_bis
from .chan_core import fmtT
from .data_loader import load_bars

PERIODS = ["D", "240", "60", "15", "3"]


def compare_bis(a, b):
    diffs = []
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x is None or y is None:
            diffs.append((i, "count", len(a), len(b)))
            continue
        for f in ("type", "startTime", "endTime", "startPrice", "endPrice"):
            if x.get(f) != y.get(f):
                diffs.append((i, f, x.get(f), y.get(f)))
    return diffs


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=2000, help="每 N 根 fine bar 做一次前缀对拍")
    args = ap.parse_args(argv)

    bars = load_bars(periods=PERIODS, from_ts=0, use_cache=True)
    eng = BacktestEngine(bars, periods=PERIODS, signal_mode="realtime")
    fine = eng.bars[eng.fine_res]["_list"]
    fine_sec = 180

    total_bad = 0
    checkpoints = 0
    def checkpoint(idx):
        nonlocal total_bad, checkpoints
        checkpoints += 1
        prefix = {res: bl[: eng._cut[res]] for res, bl in
                  ((r, eng.bars[r]["_list"]) for r in PERIODS)}
        batch = build_bis(prefix)
        bad = 0
        for res in PERIODS:
            d = compare_bis(eng._bis.get(res) or [], batch.get(res) or [])
            if d:
                bad += len(d)
                print(f"  [{res}] {len(d)} 处差异，首个：{d[0]}")
        status = "OK" if bad == 0 else f"DIFF {bad}"
        print(f"检查点 #{idx} @ {fmtT(fine[idx]['time'])}（cut 3m={eng._cut['3']}）→ {status}")
        total_bad += bad

    for i in range(len(fine)):
        eng._advance_cut(fine[i]["time"] + fine_sec)
        if (i + 1) % args.every == 0:
            checkpoint(i)
    # 最终强制重同步后对拍（与 run() 收尾一致：最终状态严格等于 batch(全前缀)）
    eng.resync_all()
    checkpoint(len(fine) - 1)
    print(f"\n检查点 {checkpoints} 个，合计差异 {total_bad}（{'✓ 增量=前缀batch' if total_bad == 0 else '✗ 不一致'}）")
    return 0 if total_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
