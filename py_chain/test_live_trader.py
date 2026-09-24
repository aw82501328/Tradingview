# -*- coding: utf-8 -*-
"""live_trader 测试：纯函数（时段窗/临时SL/对账判定）+ ReplayFeed 全链路集成。

核心不变量（restart 不变性）：任意拍中断 → 重启重放恢复 → 最终 trades 与连续运行
逐字段一致、动作序列一致。窗口 OANDA:XAUUSD 2026-06-01~06-15（3 笔成交，含
wait2Buy/stopBe/waitSell/close，覆盖进场/SL对齐/保本改单/终局平仓）。

运行：py -3.12 -m unittest py_chain.test_live_trader（无需 MT5 终端，约 2~3 分钟）。
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from . import live_store
from .backtest import DEFAULT_PERIODS
from .live_trader import (LiveTrader, ReplayFeed, deep_merge, in_block_windows,
                          load_config, provisional_sl, reconcile_plan)
from .mt5_broker import MockBroker

T0 = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
T1 = int(datetime(2026, 6, 15, tzinfo=timezone.utc).timestamp())


def _ts(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


class TestPure(unittest.TestCase):

    def test_deep_merge(self):
        got = deep_merge({"a": {"x": 1, "y": 2}, "b": 1},
                         {"a": {"y": 3, "z": 4}, "c": 5})
        self.assertEqual(got, {"a": {"x": 1, "y": 3, "z": 4}, "b": 1, "c": 5})

    def test_in_block_windows(self):
        w = [["21:55", "22:15"]]
        self.assertTrue(in_block_windows(_ts("2026-09-23 22:00"), w))
        self.assertFalse(in_block_windows(_ts("2026-09-23 20:00"), w))
        cross = [["23:50", "00:10"]]                      # 跨午夜
        self.assertTrue(in_block_windows(_ts("2026-09-23 23:55"), cross))
        self.assertTrue(in_block_windows(_ts("2026-09-23 00:05"), cross))
        self.assertFalse(in_block_windows(_ts("2026-09-23 12:00"), cross))

    def test_provisional_sl(self):
        # long：近支阻-3 更远 → 抬到 bid−最大止损；错误侧 / 无近支阻同为最大止损价
        self.assertEqual(provisional_sl("long", 4000.0, 4000.2, 3980.0), 3990.0)
        self.assertEqual(provisional_sl("long", 4000.0, 4000.2, 4005.0), 3990.0)
        self.assertEqual(provisional_sl("long", 4000.0, 4000.2, None), 3990.0)
        # short：近支阻+3 更远 → 压到 ask+最大止损；错误侧同为最大止损价
        self.assertEqual(provisional_sl("short", 4000.0, 4000.2, 4020.0), 4010.2)
        self.assertEqual(provisional_sl("short", 4000.0, 4000.2, 3995.0), 4010.2)


class TestReconcilePlan(unittest.TestCase):
    """对账判定穷举：量不齐/补平/脱钩/孤儿/缺行。"""

    def _row(self, no, ticket=111, vol=0.02, shadow=0, state="open", half=0):
        return {"engine_trade_no": no, "position_ticket": ticket, "shadow": shadow,
                "state": state, "volume_left": vol if not half else vol / 2,
                "volume_open": vol, "half_done": half, "direction": "long"}

    def _pos(self, ticket, vol=0.02):
        return {"ticket": ticket, "volume": vol, "type": 0, "sl": 0.0,
                "price_open": 0.0, "magic": 1, "comment": "", "time": 0, "profit": 0.0}

    def test_all_consistent(self):
        rows = [self._row(1)]
        pos = {111: self._pos(111)}
        eng = {1: object()}
        self.assertEqual(reconcile_plan(rows, pos, eng), [])

    def test_volume_mismatch_alerts(self):
        rows = [self._row(1, vol=0.02)]
        pos = {111: self._pos(111, vol=0.01)}        # 券商量 < 镜像
        a = reconcile_plan(rows, pos, {1: object()})
        self.assertEqual([x["action"] for x in a], ["alert_mismatch"])

    def test_engine_closed_broker_open_catchup(self):
        rows = [self._row(1)]                         # 镜像还 open
        pos = {111: self._pos(111)}
        a = reconcile_plan(rows, pos, engine_open={})  # 引擎已无此仓
        self.assertEqual([x["action"] for x in a], ["catchup_close"])

    def test_broker_gone_engine_open_detaches(self):
        rows = [self._row(1)]
        a = reconcile_plan(rows, {}, {1: object()})   # 券商侧仓消失
        self.assertEqual([x["action"] for x in a], ["detach"])

    def test_orphan_closed(self):
        a = reconcile_plan([], {999: self._pos(999)}, {})
        self.assertEqual([x["action"] for x in a], ["orphan_close"])
        a2 = reconcile_plan([], {999: self._pos(999)}, {}, orphan_policy="alert")
        self.assertEqual([x2["action"] for x2 in a2], ["alert_mismatch"])

    def test_engine_open_without_row_alerts(self):
        a = reconcile_plan([], {}, {7: object()})
        self.assertEqual([x["action"] for x in a], ["alert_missing_row"])

    def test_shadow_row_ignored(self):
        rows = [self._row(1, shadow=1)]
        self.assertEqual(reconcile_plan(rows, {111: self._pos(111)}, {}), [])


class _QuoteFollowBroker(MockBroker):
    """报价跟随回放行情的 mock（确定性：由引擎最新 3m 收盘价驱动）。"""

    def set_market(self, mid):
        self.set_quote(round(mid - 0.10, 2), round(mid + 0.10, 2))


class LiveIntegrationBase(unittest.TestCase):
    """ReplayFeed + MockBroker + LiveTrader 全链路（每用例独立临时 live 库）。"""

    HOUR = 3600

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "data", "bars.db")):
            raise unittest.SkipTest("无 bars.db")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="live_test_")
        os.environ["LIVE_DB_PATH"] = os.path.join(self.tmp, "live.db")
        live_store.ensure_tables()
        self.broker = _QuoteFollowBroker(fill_policy=lambda d, ref: ref)

    def tearDown(self):
        os.environ.pop("LIVE_DB_PATH", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self):
        return load_config(None, {
            "symbol": "XAUUSD", "db_symbol": "OANDA:XAUUSD", "lots": 2,
            "risk": {"max_spread_entry": 5.0, "entry_session_block_srv": [],
                     "max_positions": 4, "max_total_open_volume": 0.2},
            "run": {"shadow": False, "poll_sec": 0, "warmup_days": 30,
                    "reconcile_min": 60, "param_drift": "refuse",
                    "kill_file": os.path.join(self.tmp, "KILL"),
                    "arm_file": os.path.join(self.tmp, "ARM")},
            "notify": {"enabled": False},
        })

    def _trader(self, cursor=None):
        feed = ReplayFeed("OANDA:XAUUSD", DEFAULT_PERIODS,
                          cursor if cursor is not None else T0, T1)
        t = LiveTrader(self._cfg(), self.broker, feed, log=lambda *a: None)
        t.pre_tick_hook = lambda: self._follow_quote(t)
        return t, feed

    @staticmethod
    def _follow_quote(trader):
        bars = trader.engine.bars.get("3", {}).get("_list") or []
        if bars:
            trader.broker.set_market(bars[-1]["close"])

    def _run(self, trader, feed):
        n = 0
        while not feed.done():
            trader.tick()
            n += 1
        return n

    @staticmethod
    def _inv_row(r):
        """参与不变量对比的字段：引擎决策侧强一致（entry/exit 类型与时刻/引擎盈亏/
        状态机标记/手数）。exit_price 为券商成交价——宕机窗口内终局的仓按设计在重启
        时以真实市价补平（restore_catchup），与连续运行的成交时刻不同 → 豁免对比。"""
        return {k: r.get(k) for k in (
            "engine_trade_no", "direction", "entry_time", "entry_price",
            "state", "exit_type", "exit_time", "engine_pnl",
            "be_done", "half_done", "shadow", "volume_open", "volume_left")}

    def _orders_summary(self, session):
        out = {}
        for o in live_store.orders_of(session):
            if o["engine_trade_no"] is None:
                out.setdefault("_none", []).append(o["action"])
            else:
                out.setdefault(o["engine_trade_no"], []).append(o["action"])
        return out


class TestActionSequence(LiveIntegrationBase):
    """引擎事件 ↔ 券商动作 1:1：进场单/SL对齐/保本改单/终局平仓。"""

    def test_full_window_actions(self):
        trader, feed = self._trader()
        trader.start(once=True)          # 守卫+预热+首轮
        n = self._run(trader, feed)
        session = trader.session
        trades = live_store.all_trades(session)
        self.assertGreaterEqual(len(trades), 3, f"窗口应≥3笔（实际{len(trades)}）")
        orders = self._orders_summary(session)
        for t in trades:
            no = t["engine_trade_no"]
            if t["shadow"]:
                continue
            acts = orders.get(no, [])
            self.assertIn("entry", acts, f"trade#{no} 缺进场单")
            if t["state"] == "closed" and t["position_ticket"]:
                self.assertTrue(any(a in ("full_close", "half_close", "undo_entry")
                                    for a in acts),
                                f"trade#{no} closed 但无平仓单（动作={acts}）")
        # trade#1 stopBe 出场 → 必经历 be 改单（beStop 生效）
        t1 = next(t for t in trades if t["engine_trade_no"] == 1)
        self.assertEqual(t1["exit_type"], "stopBe")
        self.assertEqual(t1["be_done"], 1)
        self.assertIn("sl_modify", orders.get(1, []))
        # 无孤儿仓收尾
        self.assertEqual(self.broker.positions(), [])
        # 审计（fill 事件 ↔ 镜像行 1:1）
        from .live_trader import audit
        rep = audit(session, log=lambda *a: None)
        self.assertTrue(rep["ok"], rep["mismatches"])


class TestRestartInvariance(LiveIntegrationBase):
    """核心不变量：任意拍中断 → 重放恢复 → 最终 trades/动作序列与连续运行一致。"""

    def test_restart_mid_run(self):
        # 基准：连续运行（独立临时库），记录总拍数
        base_rows, base_orders, n_total = self._run_continuous()
        # 中断重跑：同一 broker（模拟终端持久），crash_after 拍后丢弃对象（进程被杀）
        os.environ["LIVE_DB_PATH"] = os.path.join(self.tmp, "live3.db")
        live_store.ensure_tables()
        crash_after = max(3, n_total // 2)
        trader2, feed2 = self._trader()
        trader2.start(once=True)
        for _ in range(crash_after):
            trader2.tick()
        self.assertGreater(feed2.cursor, T0)           # 已推进若干拍
        trader3, feed3 = self._trader(cursor=feed2.cursor)
        trader3.start(once=True)                       # 恢复：重放+挂接+对账
        while not feed3.done():
            trader3.tick()
        got_rows = [self._inv_row(r) for r in live_store.all_trades(trader3.session)]
        got_orders = self._orders_summary(trader3.session)
        self.assertEqual(got_rows, base_rows)
        self.assertEqual({k: v for k, v in got_orders.items() if k != "_none"},
                         {k: v for k, v in base_orders.items() if k != "_none"})
        self.assertEqual(self.broker.positions(), [])

    def _run_continuous(self):
        """连续基准：独立临时库跑全窗，返回（不变量行/动作摘要/总拍数）。"""
        os.environ["LIVE_DB_PATH"] = os.path.join(self.tmp, "live_base.db")
        live_store.ensure_tables()
        trader, feed = self._trader()
        trader.start(once=True)
        n = self._run(trader, feed)
        rows = [self._inv_row(r) for r in live_store.all_trades(trader.session)]
        return rows, self._orders_summary(trader.session), n


class TestGateShadow(LiveIntegrationBase):
    """风控门只挡镜像侧：shadow 行=引擎有仓券商无仓，事件不丢。"""

    def test_shadow_mode_blocks_entries(self):
        cfg = self._cfg()
        cfg["run"]["shadow"] = True
        feed = ReplayFeed("OANDA:XAUUSD", DEFAULT_PERIODS, T0, T1)
        t = LiveTrader(cfg, self.broker, feed, log=lambda *a: None)
        t.pre_tick_hook = lambda: self._follow_quote(t)
        t.start(once=True)
        self._run(t, feed)
        trades = live_store.all_trades(t.session)
        self.assertGreaterEqual(len(trades), 3)
        self.assertTrue(all(r["shadow"] == 1 for r in trades), "shadow 模式全部落 shadow")
        self.assertEqual(self.broker.positions(), [])   # 一张真单都没有


if __name__ == "__main__":
    unittest.main()
