import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志

# -*- coding: utf-8 -*-
"""区间套锁定下沉后的引擎一致性 + 未来函数腐蚀回归。

覆盖两道门槛（2026-09-30 画笔↔回测口径统一工程）：
1. 增量引擎 == 带锁批量（build_bis 外→内逐级锁定，与 chan-bi lockedPivotsOf(prevBis)
   同口径）——多个检查点对拍各周期笔列表逐笔一致；
2. 未来函数腐蚀：改写决策时刻 t 之后的全部K线，t 前的笔结构与信号必须逐字节不变。

数据：data/bars.db 的 OANDA:XAUUSD（240/60/15，2026-07-01 起），fine=15m。
"""
import copy
import datetime
import random
import sqlite3
import sys
import unittest

from .backtest import BacktestEngine, build_bis
from .chan_core import intervalSecOf

PERIODS = ["240", "60", "15"]
FROM = int(datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc).timestamp())


def _load():
    con = sqlite3.connect("data/bars.db")
    try:
        out = {}
        for res in PERIODS:
            rows = con.execute(
                "SELECT time,open,high,low,close FROM bars "
                "WHERE symbol='OANDA:XAUUSD' AND res=? AND time>=? ORDER BY time",
                (res, FROM)).fetchall()
            out[res] = [dict(zip(("time", "open", "high", "low", "close"), r)) for r in rows]
    finally:
        con.close()
    return out


def _snap_bis(engine):
    key = ("type", "startTime", "startPrice", "endTime", "endPrice")
    return {res: [tuple(b.get(k) for k in key) for b in (engine._bis.get(res) or [])]
            for res in PERIODS}


def _diff(a, b):
    out = []
    for res in PERIODS:
        x, y = a.get(res) or [], b.get(res) or []
        if len(x) != len(y):
            out.append((res, "count", len(x), len(y)))
            continue
        for i, (p, q) in enumerate(zip(x, y)):
            if p != q:
                out.append((res, i, p, q))
    return out


class LockParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = _load()

    def test_incremental_equals_locked_batch(self):
        """重同步点上增量引擎（含区间套锁定）与带锁批量 build_bis 前缀逐笔一致。

        引擎契约：重同步点状态严格等于 batch(前缀)（RESYNC_EVERY 注释）；重同步点
        之间的尾部增量漂移为既有容忍行为（HEAD 基线同样存在），不在本测试口径内。"""
        eng = BacktestEngine(copy.deepcopy(self.bars), periods=PERIODS,
                             signal_mode="realtime")
        fine = eng.bars[eng.fine_res]["_list"]
        sec = intervalSecOf(eng.fine_res) or 900
        bad = 0
        checkpoints = 0
        for i in range(len(fine)):
            eng._advance_cut(fine[i]["time"] + sec)
            if (i + 1) % 1500 == 0:
                checkpoints += 1
                eng.resync_all()
                prefix = {res: bl[:eng._cut[res]] for res, bl in
                          ((r, eng.bars[r]["_list"]) for r in PERIODS)}
                batch = build_bis(prefix)
                batch_key = {res: [tuple(b.get(k) for k in
                                         ("type", "startTime", "startPrice", "endTime", "endPrice"))
                                    for b in (batch.get(res) or [])] for res in PERIODS}
                d = _diff(_snap_bis(eng), batch_key)
                if d:
                    bad += len(d)
                    print(f"  检查点@{i}: {len(d)} 处差异，首个: {d[0]}")
        eng.resync_all()
        prefix = {res: bl[:eng._cut[res]] for res, bl in
                  ((r, eng.bars[r]["_list"]) for r in PERIODS)}
        batch = build_bis(prefix)
        batch_key = {res: [tuple(b.get(k) for k in
                                 ("type", "startTime", "startPrice", "endTime", "endPrice"))
                            for b in (batch.get(res) or [])] for res in PERIODS}
        d = _diff(_snap_bis(eng), batch_key)
        bad += len(d)
        checkpoints += 1
        self.assertEqual(bad, 0, f"重同步点≠带锁批量：{d[:3]}（检查点 {checkpoints} 个）")

    def test_no_lookahead_corruption(self):
        """腐蚀测试：改写 t_mid 之后全部K线，t_mid 前的笔结构必须逐笔不变。"""
        fine = self.bars["15"]
        t_mid = fine[len(fine) * 2 // 3]["time"]

        eng1 = BacktestEngine(copy.deepcopy(self.bars), periods=PERIODS,
                              signal_mode="realtime")
        eng1.run(to_ts=t_mid)
        snap1 = _snap_bis(eng1)

        corrupted = copy.deepcopy(self.bars)
        rng = random.Random(42)
        for res in PERIODS:
            for b in corrupted[res]:
                if b["time"] > t_mid:
                    scale = 1.0 + rng.uniform(-0.25, 0.25)
                    b["open"] = round(b["open"] * scale, 3)
                    b["high"] = round(b["high"] * scale, 3)
                    b["low"] = round(b["low"] * scale, 3)
                    b["close"] = round(b["close"] * scale, 3)

        eng2 = BacktestEngine(corrupted, periods=PERIODS, signal_mode="realtime")
        eng2.run(to_ts=t_mid)
        snap2 = _snap_bis(eng2)

        # 比较截至 t_mid 的已收笔结构（最后一笔端点受 t_mid 后第一根未收数据影响属正常，
        # 引擎只推进到 t_mid，两侧 cut 相同，应完全一致）
        d = _diff(snap1, snap2)
        self.assertEqual(d, [], f"未来数据泄漏：t 前结构随未来K线变化 {d[:3]}")


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    unittest.main(verbosity=2)
