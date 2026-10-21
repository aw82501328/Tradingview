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
from pathlib import Path
from unittest import mock

from . import data_store, live_store
from .backtest import DEFAULT_PERIODS
from .live_trader import (LiveTrader, ReplayFeed, deep_merge, in_block_windows,
                          load_config, provisional_sl, reconcile_plan)
from .mt5_broker import MockBroker

T0 = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
T1 = int(datetime(2026, 6, 15, tzinfo=timezone.utc).timestamp())


def _ts(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


class TestPure(unittest.TestCase):

    def test_load_config_strategy(self):
        """strategy 键（模块分层 2026-09-29）：默认 chan_v1；CLI/配置覆盖；未知拒绝启动。"""
        self.assertEqual(load_config(None).get("strategy"), "chan_v1")
        cfg = load_config(None, {"strategy": ""})
        self.assertEqual(cfg["strategy"], "chan_v1")
        with self.assertRaises(ValueError):
            load_config(None, {"strategy": "ema_v9"})
        # 显式配置文件覆盖（模拟 live_config.local.json 的 strategy 键）
        import json as _json
        import tempfile as _tf
        with _tf.TemporaryDirectory() as d:
            p = Path(d) / "live_config.json"
            p.write_text(_json.dumps({"strategy": "chan_v1", "symbol": "XAUUSD"}),
                         encoding="utf-8")
            self.assertEqual(load_config(str(p))["strategy"], "chan_v1")

    def test_state_key_strategy_suffix(self):
        """kv 状态键带策略后缀（多策略并存=路径A 多进程互不覆盖）。"""
        from . import live_store
        self.assertEqual(live_store.state_key("session", "chan_v1"), "session@chan_v1")
        self.assertEqual(live_store.state_key("heartbeat"), "heartbeat")  # 无策略语境保持原键

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
        # 券商止损一律是最大止损兜底，近支阻再近也不收紧
        self.assertEqual(provisional_sl("long", 4000.0, 4000.2, 3980.0), 3990.0)
        self.assertEqual(provisional_sl("long", 4000.0, 4000.2, 3997.0), 3990.0)
        self.assertEqual(provisional_sl("long", 4000.0, 4000.2, 4005.0), 3990.0)
        self.assertEqual(provisional_sl("long", 4000.0, 4000.2, None), 3990.0)
        self.assertEqual(provisional_sl("short", 4000.0, 4000.2, 4020.0), 4010.2)
        self.assertEqual(provisional_sl("short", 4000.0, 4000.2, 4005.0), 4010.2)
        self.assertEqual(provisional_sl("short", 4000.0, 4000.2, 3995.0), 4010.2)

    def test_eff_stop_touch_first_full_line(self):
        """触价口径挂线（2026-10-21 exitTouchOn）：券商SL=next_touch_lines 第一条
        会全平剩余的线——跟踪部分线(25%×4=1手)不挂、武装后的锁定价(全平)挂；
        剩余 1 手时跟踪线即全平线（trade#3 场景：券商SL在 4459.15 与引擎同线同价）；
        无全平线/无 exitCfg（fxma）→ 回落旧口径有效止损位。"""
        from .mark_entry import EXIT_MODE_DEFAULTS
        ec = dict(EXIT_MODE_DEFAULTS, exitTrailOn=True, exitTrailPct=25.0,
                  exitLockOn=True, exitLockPts=100.0)
        t = {"direction": "long", "entryPrice": 4305.12, "lots": 4, "lotsLeft": 3,
             "beDone": True, "beStop": 4305.12, "stopRef": 4459.15,
             "trailRaised": True, "maxFavPx": 4510.93, "maxLoss": 4295.12,
             "stopSrFired": False, "stopBeFired": False, "trailFired": False,
             "exitCfg": ec}
        # 候选序：跟踪线 4459.15（部分）→ 锁定价 4408.02（全平）→ SL 挂后者
        self.assertAlmostEqual(LiveTrader._eff_stop_of(t), 4408.025)
        # 剩余 1 手：跟踪线 25%×4=1=全平 → SL 挂跟踪线
        t["lotsLeft"] = 1
        self.assertEqual(LiveTrader._eff_stop_of(t), 4459.15)
        # 锁定未武装（峰值 4330 盈利 25 < 100）且跟踪为部分线 → 无全平线，
        # 回落旧口径（beStop 与上移后 stopRef 更紧者）
        t["lotsLeft"] = 3
        t["maxFavPx"] = 4330.0
        self.assertEqual(LiveTrader._eff_stop_of(t), 4459.15)
        # 无 exitTouchOn 的 cfg（如 fxma/旧持仓快照）→ 旧口径：beStop 与上移后
        # stopRef 的更紧者
        t["exitCfg"] = {"exitTrailOn": True}
        self.assertEqual(LiveTrader._eff_stop_of(t), 4459.15)


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
        self.assertEqual([x["action"] for x in a], ["alert_mismatch"])
        a2 = reconcile_plan([], {999: self._pos(999)}, {}, orphan_policy="close")
        self.assertEqual([x2["action"] for x2 in a2], ["orphan_close"])

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
            # 隔离 repo 的 live_config.local.json（真实账户 require_mode=real
            # 会触发 ARM 开闸双条件拒启，与本套用例无关）
            "account": {"require_mode": "demo"},
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


class TestTouchBrokerFirst(LiveIntegrationBase):
    """触价口径「券商SL先平」（2026-10-21）：SL 挂的=引擎全平线，盘中触价成交
    先于引擎收盘判定——终局/部分事件到达时券商仓已无 → 跳过市价单、按
    broker_sl_first 收口，镜像行 closed。轻量夹具直调 _on_exit/_partial_close。"""

    def test_exit_and_partial_broker_first(self):
        trader, _ = self._trader()
        trader.session = "test-touch-broker-first"
        live_store.upsert_trade({
            "session": trader.session, "engine_trade_no": 5,
            "position_ticket": 700123, "state": "open", "shadow": 0,
            "direction": "long", "volume_open": 0.04, "volume_left": 0.04,
            "entry_price": 4305.12})
        # 券商侧仓已被 SL 打掉（positions_store 空 → volume_left=0）
        t = {"tradeNo": 5, "direction": "long", "exitType": "trailStop",
             "exitTime": 1788525000, "exitPrice": 4459.15, "pnl": 154.03,
             "state": "closed"}
        trader._open[5] = t
        trader._on_exit(t)
        row = live_store.get_trade(trader.session, 5)
        self.assertEqual(row["state"], "closed")
        self.assertEqual(row["exit_type"], "trailStop")
        self.assertEqual(row["exit_price"], 4459.15)
        orders = live_store.orders_of(trader.session)
        full = [o for o in orders if o["action"] == "full_close"]
        self.assertTrue(full, "应有一条 full_close 审计行")
        self.assertIn("broker_sl_first", full[0]["retcomment"])
        # 部分平仓事件在券商先平后到达 → 只记账不再下单
        row2 = live_store.upsert_trade({
            "session": trader.session, "engine_trade_no": 6,
            "position_ticket": 700124, "state": "open", "shadow": 0,
            "direction": "long", "volume_open": 0.04, "volume_left": 0.04})
        trader._partial_close(trader.session and 6, {"tradeNo": 6, "direction": "long"},
                              live_store.get_trade(trader.session, 6),
                              {"type": "trailStop", "lots": 1, "price": 4459.15})
        self.assertEqual(live_store.get_trade(trader.session, 6)["state"], "closed")


class TestActionSequence(LiveIntegrationBase):
    """引擎事件 ↔ 券商动作 1:1：进场单/SL对齐/保本改单/终局平仓。"""

    def test_full_window_actions(self):
        # 本用例的硬编码期望（trade#1 stopBe/be_done/sl_modify）锚定旧成交口径的
        # 既定序列：触价口径（exitTouchOn，2026-10-21 默认开）下出场提前一根使
        # 同向互斥解锁时点移动、窗口序列重排（trade#1 记 stopSr）。本用例验证的是
        # live 镜像机制（事件↔订单 1:1/审计/无孤儿），不是成交口径——钉旧口径跑；
        # 触价口径的 live 行为由 TestPure.test_eff_stop_touch_first_full_line 与
        # TestTouchBrokerFirst 覆盖，TestRestartInvariance 全套在默认触价口径下通过。
        from . import param_center as _pc
        _emp = _pc.engine_module_params
        with mock.patch("py_chain.param_center.engine_module_params",
                        side_effect=lambda pm: dict(_emp(pm), exitTouchOn=False)):
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
    """影子单已取消：配置里的 shadow 不再落影子仓，进场按实单走。"""

    def test_shadow_config_does_not_write_shadow_rows(self):
        cfg = self._cfg()
        cfg["run"]["shadow"] = True
        feed = ReplayFeed("OANDA:XAUUSD", DEFAULT_PERIODS, T0, T1)
        t = LiveTrader(cfg, self.broker, feed, log=lambda *a: None)
        t.pre_tick_hook = lambda: self._follow_quote(t)
        t.start(once=True)
        self._run(t, feed)
        trades = live_store.all_trades(t.session)
        self.assertTrue(all(r["shadow"] == 0 for r in trades), "不再写影子仓")
        self.assertTrue(all(o["shadow"] == 0 for o in live_store.orders_of(t.session)))


class TestTradingToggle(LiveIntegrationBase):
    """页面策略交易开关（kv trading_enabled@chan_v1）：缺键=开启；关闭只挡新开仓。"""

    KEY = live_store.state_key("trading_enabled", "chan_v1")

    def test_refresh_kv_semantics(self):
        t, _ = self._trader()
        self.assertTrue(t._trading_enabled)              # 初始缺省开启
        t._refresh_trading_enabled()
        self.assertTrue(t._trading_enabled)              # 缺键=开启（向后兼容）
        live_store.save_state(self.KEY, {"enabled": True})
        t._refresh_trading_enabled()
        self.assertTrue(t._trading_enabled)
        live_store.save_state(self.KEY, {"enabled": False})
        t._refresh_trading_enabled()
        self.assertFalse(t._trading_enabled)
        live_store.delete_state(self.KEY)
        t._refresh_trading_enabled()
        self.assertTrue(t._trading_enabled)

    def test_entry_gate_returns_disabled_reason(self):
        t, _ = self._trader()
        t.session = "S_gate_test"
        live_store.save_state(self.KEY, {"enabled": False})
        t._refresh_trading_enabled()
        self.assertEqual(t._entry_gate("long"), (False, "策略交易已关闭(页面开关)"))
        live_store.delete_state(self.KEY)
        t._refresh_trading_enabled()
        self.assertEqual(t._entry_gate("long"), (True, ""))   # 其余门全过（点差0.2<5）

    def test_disabled_kv_blocks_entries(self):
        live_store.save_state(self.KEY, {"enabled": False})
        trader, feed = self._trader()                     # cfg 默认 shadow=False
        trader.start(once=True)                           # start 即读 kv 生效
        self._run(trader, feed)
        trades = live_store.all_trades(trader.session)
        self.assertEqual(trades, [])
        self.assertEqual(self.broker.positions(), [])     # 一张真单都没有
        self.assertFalse(any(o["shadow"] for o in live_store.orders_of(trader.session)))
        evs = live_store.recent_events(trader.session, n=500)
        self.assertTrue(any(e["kind"] == "alert"
                            and isinstance(e["payload"], dict)
                            and e["payload"].get("kind") == "gate_blocked"
                            for e in evs))

    def test_tick_rereads_toggle_mid_run(self):
        trader, feed = self._trader()
        trader.start(once=True)
        while not feed.done():
            trader.tick()
            filled = any(not r["shadow"] for r in live_store.all_trades(trader.session))
            inflight = [p for p in trader._pending if p.get("status") == "ordered"]
            if filled and not inflight:
                break
        cut = feed.cursor
        before = len(live_store.all_trades(trader.session))
        live_store.save_state(self.KEY, {"enabled": False})
        self._run(trader, feed)
        trades = live_store.all_trades(trader.session)
        self.assertTrue(any(not r["shadow"] and r["entry_time"] <= cut for r in trades),
                        "关闭前应有真实成交")
        self.assertEqual(len(trades), before, "关闭后不再记新仓，也不记影子仓")
        self.assertTrue(all(r["shadow"] == 0 for r in trades))

    def test_heartbeat_carries_trading_enabled(self):
        live_store.save_state(self.KEY, {"enabled": False})
        trader, feed = self._trader()
        trader.start(once=True)
        self._run(trader, feed)
        hb = live_store.load_state(live_store.state_key("heartbeat", "chan_v1"))
        self.assertIsNotNone(hb)
        self.assertIs(hb.get("trading_enabled"), False)


class _GapFeed(ReplayFeed):
    """MT5Feed 停机缺口形态（bars 库隔离）：库内只有落后末根、feed 内存持全量；
    current_time=墙钟语义（恒为 to_ts）；fill_gap 把 (from_ts,to_ts] 各周期写回库。"""

    def __init__(self, db_symbol, periods, bars, cursor, to_ts,
                 step_sec=180, tail_n=60):
        self.db_symbol = db_symbol
        self.periods = tuple(periods)
        self.fine_res = "3"
        self.cursor = int(cursor)
        self.to_ts = int(to_ts)
        self.step = int(step_sec)
        self.tail_n = int(tail_n)
        self._bars = bars
        self._times = {res: [b["time"] for b in bs] for res, bs in bars.items()}
        self.fill_calls = []

    def current_time(self):
        return self.to_ts

    def fill_gap(self, from_ts, to_ts, fine_res):
        n = 0
        for res in self.periods:
            fresh = [b for b in self._bars[res]
                     if int(from_ts) < b["time"] <= int(to_ts)]
            data_store.upsert_bars(self.db_symbol, res, fresh)
            if res == fine_res:
                n = len(fresh)
        self.fill_calls.append((int(from_ts), int(to_ts)))
        return n


class TestFreshSessionGap(LiveIntegrationBase):
    """2026-10-14 事故回归：新会话启动时库内末根远落后墙钟（停机缺口），缺口K
    必须先补库、按历史预热忽略——不得经首拍 tail 进入 execute 重放真实下单
    （当日实盘：库停 29h，首拍 12 笔实单）。"""

    def test_stale_db_fresh_start_ignores_gap_signals(self):
        t_mid = _ts("2026-06-02 00:00")     # 库内末根（停机点）
        full = data_store.load_store("OANDA:XAUUSD", DEFAULT_PERIODS,
                                     T0 - 40 * 86400, T1)
        logs = []
        with mock.patch.object(data_store, "DB_PATH",
                               os.path.join(self.tmp, "bars_stale.db")):
            data_store.upsert_bars("OANDA:XAUUSD", "3", [])       # 建表
            for res, bs in full.items():   # 库只到 t_mid：模拟停机期间无写入
                data_store.upsert_bars("OANDA:XAUUSD", res,
                                       [b for b in bs if b["time"] <= t_mid])
            feed = _GapFeed("OANDA:XAUUSD", DEFAULT_PERIODS, full, t_mid, T1)
            trader = LiveTrader(self._cfg(), self.broker, feed,
                                log=lambda *a: logs.append(" ".join(map(str, a))))
            trader.pre_tick_hook = lambda: self._follow_quote(trader)
            trader.start(once=True)
            while not feed.done():
                trader.tick()
        # ① 缺口必须被补库（不补则引擎冷在 t_mid，缺口K经首拍 tail 变实时信号）
        self.assertTrue(feed.fill_calls, "新会话启动未补停机缺口")
        self.assertIn("行情缺口", "".join(logs))
        # ② 全窗口信号只允许作为历史被引擎构造/预热消化：无实盘订单/持仓
        #    （引擎构造即消费已载入K线，预热 init 可为空——断言只看实盘侧零动作）
        self.assertEqual(live_store.orders_of(trader.session), [])
        self.assertEqual(self.broker.positions(), [])


if __name__ == "__main__":
    unittest.main()
