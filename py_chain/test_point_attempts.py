# -*- coding: utf-8 -*-
"""同一个买卖点最大尝试次数（CHAN_CFG.pointMaxAttempts，2026-10-25 参数中心 entry 桶）
单元测试。

口径（用户拍板 2026-10-25）：
  - 点身份 = (检测周期 periodX, 策略键 strategyKey(类别+强弱), 锚点时间)——锚点被
    新点替换/笔延伸出新锚点时间 = 新点 = 新预算；
  - 一次尝试 = 实际开仓成交（backtest._fill_pending 建仓递增；被同向互斥压掉
    未成交的信号不消耗次数）；
  - 计数终生保留：终局重置（sinkFallbackRearm/_rearm_fired）只清段去重、不清
    尝试计数（否则与无限重进的旧口径等价）；
  - 0=不限（旧口径 A/B 关闭开关）；默认 2=初始一次+重试一次；
  - 检查闸在锚点窗口之后：窗外锚照旧报 anchor_bar_window（过期故事），窗内且
    次数用满才报 point_attempt_max（预算故事）；
  - 仅当下模式（evaluateRealtimeEntries）；确认制与 JS 标记层不适用。

覆盖：
  - 当下制 evaluateRealtimeEntries：计数达上限拒绝 point_attempt_max（ctx 带
    pointType/anchorTime/attempts/max）、未达放行、0=不限、attempts=None 不检查
    （直调旧调用方零变化）、窗外锚优先报窗口闸（检查点在窗口之后）、
    信号携带 anchorTime
  - 引擎成交侧 _fill_pending：建仓计数 +1、同点第二单计数到 2、不同锚点各自
    独立预算、互斥压掉不计数、交易字典携带 anchorTime
  - _rearm_fired：清段去重不清尝试计数
  - _collect_realtime 接线：引擎把 self._rt_attempts 传给 evaluateRealtimeEntries
  - 参数中心注册见 test_param_center.TestPointMaxAttemptsParams

运行：python -m unittest py_chain.test_point_attempts -v
"""

import os as _os
_os.environ.setdefault("PY_CHAIN_BT_JOURNAL", "0")

import unittest
from unittest import mock

import py_chain.mark_entry as me
from py_chain.backtest import BacktestEngine


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice,
            "span": abs(endPrice - startPrice)}


# 60m K线时间轴（3600s 一根）：bar idx = t // 3600
TIMES60 = list(range(0, 40000, 3600))

# 卖侧 fixture（同 test_point_classes/test_anchor_bar_window）：末笔 up 终点
# 10800@108.0 挂强 2卖 → 策略键 2Sell
BIS60_SELL = [
    bi("up", 0, 3600, 100, 110),
    bi("down", 3600, 7200, 110, 105),
    bi("up", 7200, 10800, 105, 108),
]
PT_2SELL = {"type": "2卖", "time": 10800, "price": 108.0, "strong": True}

# 尝试计数键：("60", "2Sell", 锚点时间 10800)
KEY = ("60", "2Sell", 10800)


def _pma(value):
    """临时覆盖 CHAN_CFG.pointMaxAttempts 的上下文管理器；None=保持现状。"""
    saved = me.CHAN_CFG.get("pointMaxAttempts")

    class _Ctx(object):
        def __enter__(self):
            if value is not None:
                me.CHAN_CFG["pointMaxAttempts"] = value
            return self

        def __exit__(self, *a):
            if saved is None:
                me.CHAN_CFG.pop("pointMaxAttempts", None)
            else:
                me.CHAN_CFG["pointMaxAttempts"] = saved
            return False
    return _Ctx()


def _run_rt(attempts, point_max=None, tCut=11000):
    """当下制（evaluateRealtimeEntries）：mock 屏蔽背驰/支阻细节，只驱动尝试闸。
    返回 (信号列表, 拒绝记录列表)。"""
    periodBis = {"240": BIS60_SELL, "60": BIS60_SELL, "15": BIS60_SELL, "3": BIS60_SELL}
    points = {"60": {"buy": [], "sell": [dict(PT_2SELL)]}}
    rejects = []
    with _pma(point_max), \
            mock.patch.object(me, "realtimeLowerDiverge",
                              return_value=[{"res": "15", "segStart": 7200,
                                             "point": {"time": 11000, "price": 105.0}}]), \
            mock.patch.object(me, "counterMoveQualifies", return_value=True), \
            mock.patch.object(me, "mergedSegmentCount", return_value=99), \
            mock.patch.object(me, "strategyExtraOk", return_value=None), \
            mock.patch.object(me, "near_zone",
                              return_value={"sr": {"price": 105.0}}):
        sigs = me.evaluateRealtimeEntries(
            periodBis, {}, {}, points, [{"price": 105.0}],
            ["240", "60", "15", "3"], trend_res="",
            tCut=tCut, fired=set(), attempts=attempts,
            periodTimes={"60": TIMES60, "15": TIMES60, "3": TIMES60},
            on_reject=lambda gate, period, seg, skey, ctx, t:
                rejects.append({"gate": gate, "period": period, "skey": skey, "ctx": ctx}))
    return sigs, rejects


class TestRealtimeAttemptGate(unittest.TestCase):
    """当下制：计数达上限的锚点本拍拒绝 point_attempt_max，不再布防触发。"""

    def test_at_cap_rejects_with_ctx(self):
        sigs, rejects = _run_rt({KEY: 2})   # 默认 cap=2，已成交 2 次
        self.assertEqual(sigs, [])
        hit = [r for r in rejects if r["gate"] == "point_attempt_max"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["skey"], "2Sell")
        self.assertEqual(hit[0]["ctx"]["pointType"], "2卖")
        self.assertEqual(hit[0]["ctx"]["anchorTime"], 10800)
        self.assertEqual(hit[0]["ctx"]["attempts"], 2)
        self.assertEqual(hit[0]["ctx"]["max"], 2)

    def test_below_cap_fires_with_anchor_time(self):
        # 已成交 1 次 < cap 2 → 第二次（重试）照常放行；信号携带锚点时间
        sigs, rejects = _run_rt({KEY: 1})
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["strategyKey"], "2Sell")
        self.assertEqual(sigs[0]["anchorTime"], 10800)
        self.assertFalse([r for r in rejects if r["gate"] == "point_attempt_max"])

    def test_no_attempts_no_gate(self):
        # 未成交过（空计数）→ 初始尝试照常
        sigs, _ = _run_rt({})
        self.assertEqual(len(sigs), 1)

    def test_zero_means_unlimited(self):
        # 0=不限（旧口径关闭开关）：预置 5 次仍放行
        sigs, _ = _run_rt({KEY: 5}, point_max=0)
        self.assertEqual(len(sigs), 1)

    def test_none_disables_gate(self):
        # attempts=None（直调缺省）= 不启用检查：旧调用方零变化
        sigs, _ = _run_rt(None)
        self.assertEqual(len(sigs), 1)

    def test_out_of_window_reports_window_gate_first(self):
        # 检查点在锚点窗口之后：窗外锚（tCut=25000，锚窗只到 18000）即使次数已满
        # 也报 anchor_bar_window（过期故事），不误报预算闸
        sigs, rejects = _run_rt({KEY: 2}, tCut=25000)
        self.assertEqual(sigs, [])
        self.assertFalse([r for r in rejects if r["gate"] == "point_attempt_max"])
        self.assertTrue([r for r in rejects if r["gate"] == "anchor_bar_window"])

    def test_new_anchor_gets_fresh_budget(self):
        # 锚点时间不同 = 不同点：旧点用满不影响新点（14400 的新 2卖锚照常放行）
        sigs, _ = _run_rt({("60", "2Sell", 14400): 2})
        self.assertEqual(len(sigs), 1)


class TestFillPendingAttempts(unittest.TestCase):
    """成交侧：建仓计数 +1（互斥压掉不计数）；计数随引擎跨拍持有。"""

    def _engine(self):
        bars = {"3": [{"time": i * 180, "open": 100, "high": 101, "low": 99, "close": 100}
                      for i in range(6)]}
        return BacktestEngine(bars, periods=["3"])

    @staticmethod
    def _sig(direction="short", anchor_time=10800, seg=7200):
        return {"periodX": "60", "markRes": "15", "time": 11000, "price": 105.0,
                "direction": direction, "strategyKey": "2Sell",
                "strategyLabel": "等待回调后做2卖", "signalNote": "unit",
                "nearSr": 105.0, "pointType": "2卖", "pointStrong": True,
                "anchorTime": anchor_time, "realtime": True, "segStart": seg}

    def _stats(self):
        return {"executed": 0, "suppressed": 0}

    def test_fill_increments_per_anchor(self):
        eng = self._engine()
        trades, stats = [], self._stats()
        eng._fill_pending(trades, [self._sig()], 100.5, 900, stats, open_pos={})
        self.assertEqual(len(trades), 1)
        self.assertEqual(eng._rt_attempts[KEY], 1)
        self.assertEqual(trades[0]["anchorTime"], 10800)   # 交易字典携带锚点时间
        # 同点第二单（终局重置 rearm 后再进，新段）：计数到 2（=默认上限）
        eng._fill_pending(trades, [self._sig(seg=7400)], 100.5, 1080, stats, open_pos={})
        self.assertEqual(len(trades), 2)
        self.assertEqual(eng._rt_attempts[KEY], 2)

    def test_mutex_suppressed_not_counted(self):
        # 被同向互斥压掉未成交的信号不消耗次数（口径：成交才算一次）
        eng = self._engine()
        trades, stats = [], self._stats()
        blk = {"tradeNo": 9, "entryTime": 700, "entryPrice": 100.0, "strategyKey": "Buy"}
        eng._fill_pending(trades, [self._sig()], 100.5, 900, stats,
                          open_pos={"short": blk})
        self.assertEqual(trades, [])
        self.assertEqual(stats["suppressed"], 1)
        self.assertEqual(eng._rt_attempts, {})

    def test_different_anchor_separate_budget(self):
        eng = self._engine()
        trades, stats = [], self._stats()
        eng._fill_pending(trades, [self._sig()], 100.5, 900, stats, open_pos={})
        eng._fill_pending(trades, [self._sig(anchor_time=14400)], 100.5, 1080,
                          stats, open_pos={})
        self.assertEqual(eng._rt_attempts[KEY], 1)
        self.assertEqual(eng._rt_attempts[("60", "2Sell", 14400)], 1)


class TestRearmKeepsAttempts(unittest.TestCase):
    """终局重置只清段去重，不清尝试计数（同一点终生上限，重置不回血）。"""

    def test_rearm_clears_fired_not_attempts(self):
        bars = {"3": [{"time": i * 180, "open": 100, "high": 101, "low": 99, "close": 100}
                      for i in range(6)]}
        eng = BacktestEngine(bars, periods=["3"])
        eng._rt_fired = {("60", "2Sell", "15", 7200)}
        eng._rt_attempts = {KEY: 2}
        eng._rearm_fired({"periodX": "60", "strategyKey": "2Sell", "markRes": "15"})
        self.assertEqual(eng._rt_fired, set())                    # 段去重已重置（可再进）
        self.assertEqual(eng._rt_attempts, {KEY: 2})              # 尝试计数不回血


class TestCollectRealtimeWiring(unittest.TestCase):
    """接线：引擎把 self._rt_attempts 传给 evaluateRealtimeEntries（同一 dict 对象，
    成交侧递增 ↔ 评估侧检查闭环）。"""

    def test_collect_realtime_passes_attempts(self):
        bars = {"3": [{"time": i * 180, "open": 100, "high": 101, "low": 99, "close": 100}
                      for i in range(6)]}
        eng = BacktestEngine(bars, periods=["3"])
        stats = {"signals": 0, "long": 0, "short": 0, "markRes": {}, "strategyKeys": {}}
        with mock.patch.object(me, "evaluateRealtimeEntries", return_value=[]) as m:
            eng._collect_realtime({}, stats, 900)
        self.assertIs(m.call_args.kwargs.get("attempts"), eng._rt_attempts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
