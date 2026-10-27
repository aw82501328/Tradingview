import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")  # 引擎测试不落交易日志（e2e 例内单独开启）

# -*- coding: utf-8 -*-
"""长窗自动分段（2026-10-27）单测：三块——

1. _split_segments 纯函数：缺省不拆（旧方案重放忠实）/阈值/均分/UTC+8 日界对齐/
   seg_bounds 逐位重放/to_ts=None 用品种数据终点；
2. 引擎 carry 交接（合成随机游走，test_warmup_faststart 同源数据）：段2播种段1完整
   trades 后单号续接、跨段持仓同一笔（entryTime/方向/进场价不变）、尝试计数与
   当下背驰去重携带；
3. bt_batch.run_symbol 分段 e2e（真实 bars.db 有 OANDA:XAUUSD 才跑，否则跳过）：
   两段顺序跑→done 合并（steps=两段之和）、journal 每段独立文件 _sNofM 后缀、
   跨两段 fill 的 tradeNo 连续无重复。

分段语义（与单次整窗跑非逐位一致：每段结构从各自预热重建）见 webapp._split_segments
与 BacktestEngine.run carry 文档；性能动机=引擎单步耗时随窗口超线性上涨。
"""
import datetime
import json
import os
import unittest
from unittest import mock

from py_chain.backtest import BacktestEngine, merge_run_stats
from py_chain.test_warmup_faststart import PERIODS, SEC, gen_bars
from py_chain.webapp import BacktestWorker

TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def ts_utc8(y, mo, d):
    """北京时间 y-mo-d 00:00 的 Unix 秒（日界对齐断言的基准）。"""
    return int(datetime.datetime(y, mo, d, tzinfo=TZ8).timestamp())


def is_utc8_midnight(ts):
    return (ts + 8 * 3600) % 86400 == 0


class SplitSegmentsTests(unittest.TestCase):
    """_split_segments：分段计算与重放忠实。from 统一取 2026-01-01（+8 日界）。"""

    FROM = ts_utc8(2026, 1, 1)
    LEAD = 60

    def segs(self, cfg, from_ts=None, to_ts=None, lead_days=None):
        return BacktestWorker._split_segments(
            cfg, "OANDA:XAUUSD", from_ts if from_ts is not None else self.FROM,
            to_ts, self.LEAD if lead_days is None else lead_days)

    def test_default_off_replay_fidelity(self):
        # 缺省不拆：旧方案/旧客户端 cfg 无 auto_split 键 → 永远整窗（重放忠实）
        self.assertIsNone(self.segs({"from": "2026-01-01"},
                                    to_ts=self.FROM + 200 * 86400))

    def test_explicit_off(self):
        self.assertIsNone(self.segs(
            {"auto_split": False}, to_ts=self.FROM + 200 * 86400))

    def test_lead_zero_legacy_not_split(self):
        # lead_days=0（warmup_bars 旧口径）不拆：分段依赖「预热提前」装载语义
        self.assertIsNone(self.segs(
            {"auto_split": True}, to_ts=self.FROM + 200 * 86400, lead_days=0))

    def test_window_at_or_below_threshold(self):
        # ≤90 天不拆（含恰好 90 天）
        self.assertIsNone(self.segs(
            {"auto_split": True}, to_ts=self.FROM + 60 * 86400))
        self.assertIsNone(self.segs(
            {"auto_split": True}, to_ts=self.FROM + 90 * 86400))

    def test_split_182d_four_segments(self):
        to = self.FROM + 182 * 86400
        cfg = {"auto_split": True}
        segs = self.segs(cfg, to_ts=to)
        self.assertIsNotNone(segs)
        self.assertEqual(len(segs), 4)               # ceil(182/60)=4
        self.assertEqual(segs[0]["start_ts"], self.FROM)
        self.assertEqual(segs[-1]["to_ts"], to)
        prev = self.FROM
        for s in segs:
            self.assertTrue(s["start_ts"] < s["to_ts"])
            self.assertTrue(is_utc8_midnight(s["start_ts"]))   # 段界对齐 +8 日界
            self.assertEqual(s["data_from_ts"], s["start_ts"] - self.LEAD * 86400)
            self.assertLessEqual(s["to_ts"] - s["start_ts"], 61 * 86400)  # 对齐余量 ≤1 天
            self.assertGreaterEqual(s["start_ts"], prev)
            prev = s["start_ts"]
        # cfg 落 seg_bounds（bt_runs 快照/重放），与返回段一一对应
        self.assertEqual([[s["start_ts"], s["to_ts"]] for s in segs], cfg["seg_bounds"])

    def test_split_100d_two_segments(self):
        segs = self.segs({"auto_split": True}, to_ts=self.FROM + 100 * 86400)
        self.assertEqual(len(segs), 2)               # ceil(100/60)=2

    def test_seg_bounds_verbatim_replay(self):
        # 快照重放：按存档段界逐位重放，不重算（窗口再小/阈值再变都不影响）
        f1, t1 = self.FROM + 10 * 86400, self.FROM + 12 * 86400
        f2, t2 = self.FROM + 12 * 86400, self.FROM + 15 * 86400
        segs = self.segs({"auto_split": True, "seg_bounds": [[f1, t1], [f2, t2]]})
        self.assertEqual([(s["start_ts"], s["to_ts"]) for s in segs],
                         [(f1, t1), (f2, t2)])

    def test_auto_split_off_wins_over_stale_bounds(self):
        # 显式关闭优先于残留快照段界
        self.assertIsNone(self.segs(
            {"auto_split": False,
             "seg_bounds": [[self.FROM, self.FROM + 86400]]}))

    def test_to_none_uses_store_min_last(self):
        # to_ts=None（到最新）：窗口终点=需装载周期最晚时间的交集（最细周期截断口径）
        store = [{"symbol": "OANDA:XAUUSD", "periods": {
            "3": {"last": self.FROM + 200 * 86400},
            "15": {"last": self.FROM + 202 * 86400},
            "60": {"last": self.FROM + 205 * 86400},
            "240": {"last": self.FROM + 206 * 86400},
            "D": {"last": self.FROM + 206 * 86400}}}]
        with mock.patch("py_chain.data_store.list_stores", return_value=store):
            segs = self.segs({"auto_split": True}, to_ts=None,
                             lead_days=self.LEAD)
        self.assertIsNotNone(segs)
        self.assertEqual(segs[-1]["to_ts"], self.FROM + 200 * 86400)  # min(last)
        # 数据终点不足 90 天时不拆
        store2 = [{"symbol": "OANDA:XAUUSD", "periods": {
            r: {"last": self.FROM + 80 * 86400} for r in PERIODS}}]
        with mock.patch("py_chain.data_store.list_stores", return_value=store2):
            self.assertIsNone(self.segs({"auto_split": True}, to_ts=None))

    def test_no_store_info_no_split(self):
        with mock.patch("py_chain.data_store.list_stores", return_value=[]):
            self.assertIsNone(self.segs({"auto_split": True}, to_ts=None))


class CarryContinuityTests(unittest.TestCase):
    """引擎 carry 交接（合成随机游走，固定种子确定性复现）。

    数据集校准（base=5）：唯一一笔 #1 空单在 3m 序号 3425 进场（4489.21）、
    stopRef=4495.73；段界取 3426 = 进场后 1 根 → 交接时未平仓，段2 内由携带的
    止损线触价平仓（与连续整窗跑同价同损），段2 另开 #2/#3/#4 续号。"""

    OFF = {"D": 0, "240": 1, "60": 2, "15": 3, "3": 4}
    BASE = 5
    N3 = 6000
    K0 = 800        # 段1 交易起点（预热 ~800 根）
    K1 = 3426       # 段界（#1 空单进场后 1 根 → 交接时持仓中）

    @classmethod
    def setUpClass(cls):
        total = cls.N3 * SEC["3"]
        cls.bars = {res: gen_bars(res, total // SEC[res],
                                  seed=cls.BASE + cls.OFF[res]) for res in PERIODS}
        cls.fine = cls.bars["3"]

    def _slice(self, lo, hi):
        return {res: [b for b in bl if lo <= b["time"] <= hi]
                for res, bl in self.bars.items()}

    def _run(self, bars, start_ts, carry=None):
        eng = BacktestEngine(bars, warmup_bars=60, signal_mode="realtime")
        res = eng.run(start_ts=start_ts, journal=False, carry=carry)
        return eng, res

    def test_open_position_carried_and_managed_in_seg2(self):
        t0 = self.fine[self.K0]["time"]
        t1 = self.fine[self.K1]["time"]
        t2 = self.fine[-1]["time"] + SEC["3"]
        _, r1 = self._run(self._slice(self.fine[0]["time"] - 1, t1 - 1), t0)
        trades1 = r1["trades"]
        # 段1：恰好一笔、交接时未平仓（带止损线）
        self.assertEqual(len(trades1), 1)
        hand = trades1[0]
        self.assertNotEqual(hand.get("state"), "closed")
        self.assertIsNotNone(hand.get("stopRef"))
        self.assertEqual(r1["stats"]["closed"], 0)
        cs = r1["carryState"]
        self.assertEqual(sorted(cs),
                         ["rtAttempts", "rtAttemptsSeg", "rtFired", "trades"])
        # 段2：装载=段界前推同量预热；carry 交接后持仓被继续管理
        lead = self.K0 * SEC["3"]
        _, r2 = self._run(self._slice(t1 - lead, t2), t1, carry=cs)
        trades2 = r2["trades"]
        # 单号 1..N 连续（交接全部进场 + 新单续号）
        self.assertEqual([t["tradeNo"] for t in trades2],
                         list(range(1, len(trades2) + 1)))
        # 交接单：身份不变，且在段2 内被携带的止损线平仓（连续整窗跑同 pnl）
        t = trades2[0]
        self.assertEqual((t["tradeNo"], t["direction"], t["entryTime"],
                          t["entryPrice"], t["lots"]),
                         (hand["tradeNo"], hand["direction"], hand["entryTime"],
                          hand["entryPrice"], hand["lots"]))
        self.assertEqual(t["state"], "closed")
        self.assertTrue(str(t.get("exitType", "")).startswith("stop"))
        self.assertAlmostEqual(t["pnl"], -26.08, places=1)
        # 段2 平仓计数=自身新单平仓+交接单平仓；新单 tradeNo ≥ 2
        new = trades2[1:]
        self.assertTrue(all(t["tradeNo"] >= 2 for t in new))
        self.assertEqual(r2["stats"]["closed"], len(new) + 1)
        # 合并口径：closed 之和 = 总笔数（全部终局）
        merged = merge_run_stats(merge_run_stats(None, r1["stats"]), r2["stats"])
        self.assertEqual(merged["closed"], len(trades2))
        self.assertEqual(merged["steps"],
                         r1["stats"]["steps"] + r2["stats"]["steps"])
        self.assertEqual(merged["executed"],
                         r1["stats"]["executed"] + r2["stats"]["executed"])
        # 尝试计数/段级计数：携带键保留（值只增不减——rearm 终局重置不清计数）；
        # rtFired 去重集除外：交接单在段2 终局时 rearm 会按语义移除其键
        cs2 = r2["carryState"]
        for key in ("rtAttempts", "rtAttemptsSeg"):
            for k, v in cs[key].items():
                self.assertGreaterEqual(cs2[key].get(k, 0), v)

    def test_no_carry_independent_numbering(self):
        # 无 carry：段2 独立从 #1 编号（连续性必须显式传 carry 才成立）
        t0 = self.fine[self.K0]["time"]
        t1 = self.fine[self.K1]["time"]
        t2 = self.fine[-1]["time"] + SEC["3"]
        _, r2 = self._run(self._slice(t1 - self.K0 * SEC["3"], t2), t1)
        nos = [t["tradeNo"] for t in r2["trades"]]
        self.assertEqual(nos, list(range(1, len(nos) + 1)))


def _store_has(symbol="OANDA:XAUUSD"):
    import sqlite3
    if not os.path.exists("data/bars.db"):
        return False
    try:
        con = sqlite3.connect("data/bars.db")
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM bars WHERE symbol=? AND res='3'", (symbol,)
            ).fetchone()[0]
        finally:
            con.close()
        return n > 5000
    except Exception:
        return False


@unittest.skipUnless(_store_has(), "本地库无 OANDA:XAUUSD 3m 数据（跳过 e2e）")
class BtBatchSegmentsE2ETests(unittest.TestCase):
    """bt_batch.run_symbol 两段 e2e：journal 命名/合并统计/跨段单号连续。"""

    SYMBOL = "OANDA:XAUUSD"

    @classmethod
    def setUpClass(cls):
        from py_chain import bt_batch
        cls.bt_batch = bt_batch
        f1 = ts_utc8(2026, 9, 24)
        t1 = ts_utc8(2026, 9, 27) - 1
        f2 = ts_utc8(2026, 9, 27)
        t2 = ts_utc8(2026, 9, 30) - 1
        lead = 60 * 86400
        kw = {"periods": ["D", "240", "60", "15", "3"], "warmup_bars": 60,
              "lots": 1, "res_chain": ["D", "240", "60", "15", "3"],
              "open_res": {"15", "3", "60"}}
        cls.child_cfg = {
            "chan_cfg": {},
            "chains": [{"id": "A",
                        "periods": ["D", "240", "60", "15", "3"],
                        "engine_kwargs": dict(kw)}],
            "data_from_ts": f1 - lead, "to_ts": t2, "start_ts": f1,
            "segments": [
                {"data_from_ts": f1 - lead, "to_ts": t1, "start_ts": f1},
                {"data_from_ts": f2 - lead, "to_ts": t2, "start_ts": f2},
            ],
        }
        cls.bounds = (f1, t1, f2, t2)

    def test_two_segments_merged_done_and_journals(self):
        import multiprocessing as mp
        import threading
        old = os.environ.get("PY_CHAIN_BT_JOURNAL")
        os.environ["PY_CHAIN_BT_JOURNAL"] = "1"   # 本例验证 journal 段后缀命名
        q = mp.get_context("spawn").Queue()
        # run_symbol 必须在旁路线程跑：主线程持续排水，否则消息积压撑满管道缓冲会死锁
        th = threading.Thread(
            target=self.bt_batch.run_symbol,
            args=(self.child_cfg, self.SYMBOL, q, mp.Event(), mp.Event()))
        th.start()
        msgs = []
        try:
            while True:
                m = q.get(timeout=300)
                msgs.append(m)
                if m.get("kind") in ("done", "error"):
                    break
            th.join(timeout=10)
        finally:
            if old is None:
                os.environ.pop("PY_CHAIN_BT_JOURNAL", None)
            else:
                os.environ["PY_CHAIN_BT_JOURNAL"] = old
        self.assertEqual(msgs[-1].get("kind"), "done",
                         f"e2e 未正常 done：{msgs[-1]}")
        done = msgs[-1]
        # journal：两段独立文件，命名带段后缀（同 wall-clock 启动防撞名）
        paths = (done.get("journal") or "").split(";")
        self.assertEqual(len(paths), 2)
        self.assertIn("chanA_s1of2", os.path.basename(paths[0]))
        self.assertIn("chanA_s2of2", os.path.basename(paths[1]))
        footers = []
        try:
            for p in paths:
                self.assertTrue(os.path.exists(p))
                with open(p, "r", encoding="utf-8") as fh:
                    rows = [json.loads(x) for x in fh if x.strip()]
                fs = [r for r in rows if r.get("ev") == "footer"]
                self.assertEqual(len(fs), 1)
                footers.append(fs[0])
            # done 合并 = 两段 footer 统计之和
            st = done.get("stats") or {}
            self.assertEqual(st["steps"], sum(f["stats"]["steps"] for f in footers))
            self.assertEqual(st["signals"], sum(f["stats"]["signals"] for f in footers))
            self.assertEqual(st["executed"], sum(f["stats"]["executed"] for f in footers))
            # 跨段单号连续：两段 fill 的 tradeNo 合并后 = 1..N 无重复无跳号
            fills = []
            for p in paths:
                with open(p, "r", encoding="utf-8") as fh:
                    fills += [json.loads(x) for x in fh if x.strip()
                              and json.loads(x).get("ev") == "fill"]
            nos = sorted(f.get("tradeNo") for f in fills)
            self.assertEqual(nos, list(range(1, len(nos) + 1)))
        finally:
            for p in paths:
                try:
                    os.remove(p)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
