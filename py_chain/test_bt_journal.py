# -*- coding: utf-8 -*-
"""bt_journal/bt_query 单元测试：去重、状态变化检测、出场叙事、查询折叠、坏行容忍、
日志保留策略。

运行：python -m unittest py_chain.test_bt_journal -v
"""

import json
import os
import sys
import tempfile
import time
import unittest

from py_chain.bt_journal import (BtJournal, JOURNAL_DIR, cleanup_old, default_path,
                                 sanitize_symbol, gate_label)
from py_chain.backtest import advance_exit_decision, execute_pending_exit, close_trade


def _pos(**kw):
    """最小可推进的持仓 dict（advance_exit_decision 所需字段）。"""
    base = {
        "tradeNo": 1, "direction": "short", "strategyKey": "wait2Sell",
        "planDirection": "空头空", "markRes": "3", "periodX": "15",
        "signalTime": 1000, "entryTime": 1100, "entryPrice": 100.0,
        "stopRef": 103.0, "stopSource": "测试支阻位 103.00+止损滑点 3.00",
        "beStop": 97.0, "maxLoss": 110.0, "entryBarStart": None, "entryBarEnd": None,
        "entryBarExt": None, "slipStopEff": 3.0, "lots": 4, "mult": 1.0,
        "state": "open", "beDone": False, "halfDone": False, "exits": [],
    }
    base.update(kw)
    return base


def _bi(t0, t1, typ, p0, p1, forming=False):
    b = {"startTime": t0, "endTime": t1, "type": typ, "startPrice": p0, "endPrice": p1}
    if forming:
        b["_forming"] = True
    return b


class TestJournalWrite(unittest.TestCase):
    """写入侧：去重 / 状态变化检测 / 信号id回填。"""

    def setUp(self):
        # 本模块测的就是日志写入：临时摘掉引擎测试的防污染开关，tearDown 恢复
        self._env_prev = os.environ.pop("PY_CHAIN_BT_JOURNAL", None)
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.ndjson")

    def tearDown(self):
        if self._env_prev is not None:
            os.environ["PY_CHAIN_BT_JOURNAL"] = self._env_prev

    def _jr(self):
        return BtJournal(path=self.path, enabled=True, register=False)

    def test_reject_dedupe(self):
        jr = self._jr()
        for _ in range(5):
            jr.reject(100, "plan_watch", "60", None, None, planDir="观望")
        jr.reject(101, "plan_watch", "60", None, None, planDir="观望")  # t 变、ctx 同 → 不写
        jr.reject(102, "plan_watch", "60", None, None, planDir="多头多")  # ctx 变 → 再写
        jr.close()
        with open(self.path, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh]
        rej = [r for r in rows if r["ev"] == "reject"]
        self.assertEqual(len(rej), 2)
        self.assertEqual(rej[1]["ctx"]["planDir"], "多头多")

    def test_reject_volatile_keys_not_in_sig(self):
        jr = self._jr()
        for v in (1.0, 1.5, 2.0):
            jr.reject(100, "fx_ma_gap_fail", "3", 555, "fx2Buy",
                      diff=v, crossMinPts=2.0, volatile=("diff",))
        jr.close()
        with open(self.path, encoding="utf-8") as fh:
            rej = [json.loads(l) for l in fh if json.loads(l)["ev"] == "reject"]
        self.assertEqual(len(rej), 1)
        self.assertEqual(rej[0]["ctx"]["diff"], 1.0)  # 首次值

    def test_state_change_detection(self):
        jr = self._jr()
        s = {"planDir": "观望", "segStart": 1, "segType": "down",
             "segEnd": 100, "segEndPrice": 9.9}
        vol = ("segEnd", "segEndPrice")
        for t in (10, 11, 12):
            s2 = dict(s, segEnd=t, segEndPrice=t * 1.0)  # volatile 每拍变
            jr.state(t, "60", s2, volatile=vol)
        jr.state(13, "60", dict(s, segStart=2), volatile=vol)  # 标识变 → 写
        jr.close()
        with open(self.path, encoding="utf-8") as fh:
            st = [json.loads(l) for l in fh if json.loads(l)["ev"] == "state"]
        self.assertEqual(len(st), 2)

    def test_signal_id_backfill(self):
        jr = self._jr()
        s1 = {"time": 1, "periodX": "60", "strategyKey": "wait2Buy"}
        s2 = {"time": 2, "periodX": "15", "strategyKey": "wait1Sell"}
        jr.signal(s1)
        jr.signal(s2)
        jr.close()
        self.assertEqual((s1["_jid"], s2["_jid"]), (1, 2))

    def test_write_failure_silent(self):
        d = tempfile.mkdtemp()
        blocker = os.path.join(d, "blocker")
        with open(blocker, "w") as fh:   # 占位文件：makedirs(…/blocker) 必失败
            fh.write("x")
        jr = BtJournal(path=os.path.join(d, "blocker", "x.ndjson"), enabled=True,
                       register=False)
        self.assertFalse(jr.enabled)   # 打开失败 → 自动禁用，不抛
        jr.signal({"time": 1})          # 全部 no-op


class TestExitWhy(unittest.TestCase):
    """出场叙事：breakeven/half/close/stopSr 全链 why 字段。"""

    def test_breakeven_event_has_why(self):
        pos = _pos()
        mark_bis = [_bi(900, 1000, "down", 110, 105), _bi(1050, 1200, "down", 104, 98.5)]
        bar = {"time": 1200, "open": 99, "high": 101, "low": 97.5, "close": 99}
        advance_exit_decision(pos, 1203, bar, mark_bis, [], None)
        ev = pos["exits"][-1]
        self.assertEqual(ev["type"], "breakeven")
        self.assertIn("TP1 保本", ev["why"])
        self.assertIn("beStop 97.00", ev["why"])

    def test_half_pending_why_and_exec(self):
        pos = _pos()
        px_bis = [_bi(900, 1000, "down", 110, 105), _bi(1010, 1105, "down", 104, 99)]
        bar = {"time": 1200, "open": 99, "high": 101, "low": 97.5, "close": 99}
        typ = advance_exit_decision(pos, 1203, bar, [], px_bis, None)
        self.assertEqual(typ, "half")
        self.assertIn("TP2 半平", pos["pendingWhy"])
        tr = execute_pending_exit(pos, {"time": 1203, "open": 99.0})
        self.assertIsNone(tr)   # half 不终局
        ev = [e for e in pos["exits"] if e["type"] == "half"][0]
        self.assertIn("下一开盘", ev["why"])
        self.assertTrue(pos["beDone"])

    def test_stop_pending_why_and_close(self):
        pos = _pos(stopRef=100.0, beDone=False)
        bar = {"time": 1200, "open": 100.5, "high": 100.5, "low": 99, "close": 100.2}
        typ = advance_exit_decision(pos, 1203, bar, [], [], None)
        self.assertEqual(typ, "stopSr")
        tr = execute_pending_exit(pos, {"time": 1203, "open": 100.6})
        self.assertEqual(tr["exitType"], "stopSr")
        self.assertIn("冲上止损位 100.00", tr["exitWhy"])
        self.assertEqual(tr["exitWhy"].count("下一开盘"), 1)  # 成交拍拼接不重复

    def test_close_tp3a_with_ref_endpoint(self):
        from py_chain.mark_entry import find_bi_event
        # 空单 TP3a：有利方向=down 笔破前一同向（down）笔端点；末笔为延伸中的 up，
        # lastBiOk=False 不触发 TP2 半平，走 TP3a 分支
        bis = [_bi(900, 1000, "down", 110, 100), _bi(1000, 1100, "up", 100, 106),
               _bi(1100, 1250, "down", 106, 99),
               _bi(1250, 1300, "up", 99, 100.5, forming=True)]
        ev = find_bi_event(bis, 1000, "down", break_prev=True)
        self.assertEqual(ev["refPrice"], 100)
        self.assertEqual(ev["refTime"], 1000)
        pos = _pos(stopRef=90.0)  # 止损放远，走 TP3a 分支
        bar = {"time": 1300, "open": 100, "high": 101, "low": 99, "close": 100}
        typ = advance_exit_decision(pos, 1303, bar, [], bis, None)
        self.assertEqual(typ, "close")
        self.assertIn("破前一同向笔端点 100.00", pos["pendingWhy"])


class TestQueryFold(unittest.TestCase):
    """查询侧：折叠渲染 + 坏行容忍。"""

    def setUp(self):
        self._env_prev = os.environ.pop("PY_CHAIN_BT_JOURNAL", None)

    def tearDown(self):
        if self._env_prev is not None:
            os.environ["PY_CHAIN_BT_JOURNAL"] = self._env_prev

    def _write(self):
        jr = BtJournal(path=os.path.join(self.dir, "q.ndjson"), enabled=True,
                       register=False)
        jr.header({"periods": ["60", "15", "3"], "fillMode": "confirm", "lots": 4})
        s = {"time": 1700000000, "periodX": "60", "markRes": "15", "direction": "short",
             "strategyKey": "wait2Sell", "price": 100.0, "nearSr": 100.5,
             "segStart": 1699990000, "signalNote": "等待反弹后做2卖｜近支阻位 100.50"}
        jr.signal(s)
        jr.fill({"tradeNo": 1, "journalId": 1, "entryTime": 1700000100,
                 "entryPrice": 100.2, "direction": "short", "strategyKey": "wait2Sell",
                 "periodX": "60", "markRes": "15", "fillMode": "confirm",
                 "signalTime": 1700000000, "signalPrice": 100.0,
                 "stopRef": 103.0, "beStop": 97.0, "maxLoss": 110.0,
                 "entryWhy": "做空｜等待反弹后做2卖｜…｜止损位 103.00"})
        jr.exit_event(1, {"type": "stopSr", "time": 1700000200, "price": 103.0,
                          "why": "stopSr 触发：当根最高 103.20 冲上止损位"})
        jr.trade_end({"tradeNo": 1, "state": "closed", "exitType": "stopSr",
                      "exitTime": 1700000200, "exitPrice": 103.0, "pnl": -11.2,
                      "exitWhy": "stopSr 触发：…", "entryTime": 1700000100,
                      "entryPrice": 100.2, "direction": "short"})
        jr.reject(1699999000, "near_sr_fail", "60", 1699990000, "wait2Sell",
                  price=100.0, srPrice=111.0, dist=11.0, near=10.0, markRes="15")
        jr.state(1699999000, "60", {"planDir": "空头空", "planStrategy": "等待反弹后做2卖",
                                    "planReason": "测试", "trendDir": "short",
                                    "trendReason": "", "segStart": 1699990000,
                                    "segType": "up", "segForming": True,
                                    "segEnd": 1699999000, "segEndPrice": 100.0})
        jr.suppressed(1700000300, {"strategyKey": "wait2Buy", "periodX": "15",
                                   "time": 1700000300, "price": 99.0,
                                   "direction": "long"},
                      why="同向互斥：已有做空持仓单 #1")
        jr.footer({"steps": 10, "signals": 1, "executed": 1}, {})
        jr.close()
        # 追加半行（模拟断电）
        with open(jr.path, "a", encoding="utf-8") as fh:
            fh.write('{"ev":"reject","t":1')
        return jr.path

    def test_fold_and_render(self):
        from py_chain import bt_query
        self.dir = tempfile.mkdtemp()
        j = bt_query.Journal(self._write())
        self.assertEqual(len(j.signals), 1)
        self.assertEqual(len(j.fills), 1)
        self.assertEqual(len(j.rejects), 1)   # 坏行被容忍
        r = j.state_at("60", 1700000000)
        self.assertEqual(r["planDir"], "空头空")
        line = bt_query.fmt_reject(j.rejects[0])
        self.assertIn("不在支阻位附近", line)
        self.assertIn("near 10", line)
        # 时刻时间线渲染
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bt_query.show_time(j, 1700000000, 1.0)
        out = buf.getvalue()
        self.assertIn("信号 id=1", out)
        self.assertIn("空头空", out)


class TestRetention(unittest.TestCase):
    """保留策略：>30 天删除，但组内最新 10 个保留。"""

    def test_cleanup(self):
        d = tempfile.mkdtemp()
        now = time.time()
        for i in range(14):
            p = os.path.join(d, f"bt_chan_XAUUSD_20260101_000000{i:03d}.ndjson")
            with open(p, "w") as fh:
                fh.write("{}")
            os.utime(p, (now - 40 * 86400, now - 40 * 86400))   # 全部 40 天前
        keep_new = os.path.join(d, "bt_chan_XAUUSD_20260901_000000001.ndjson")
        with open(keep_new, "w") as fh:
            fh.write("{}")
        os.utime(keep_new, (now - 1 * 86400, now - 1 * 86400))
        old_module = JOURNAL_DIR
        import py_chain.bt_journal as bj
        bj.JOURNAL_DIR = d
        try:
            cleanup_old(keep_for=("chan", "XAUUSD"))
        finally:
            bj.JOURNAL_DIR = old_module
        left = sorted(os.listdir(d))
        # 组内共保留最近 10 个（1 新 + 9 个最旧批中的较新者），40 天前的其余删除
        self.assertEqual(len(left), 10)
        self.assertIn(os.path.basename(keep_new), left)

    def test_sanitize(self):
        self.assertEqual(sanitize_symbol("OANDA:XAUUSD"), "OANDA-XAUUSD")
        self.assertEqual(gate_label("near_sr_fail"), "不在支阻位附近")
        self.assertEqual(gate_label("unknown_gate"), "unknown_gate")


if __name__ == "__main__":
    unittest.main()
