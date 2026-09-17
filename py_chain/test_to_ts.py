# -*- coding: utf-8 -*-
"""回测「结束日期」(to/to_ts) 单元测试（2026-09-17）

口径：结束日期含当日全天——to_ts = parse_from(to) + 86400 - 1（该日 23:59:59 UTC）；
截断在加载层完成（store/cache/live 三源一致），引擎拿到的是截断后数据 → 成交/信号/
未平仓 mark-to-market（_finish 按加载末根结算）全部止于结束日；旧方案库 cfg 无 to →
不设 to_ts → 行为与旧版逐笔一致（lead_days 兼容先例）。

覆盖：
  - normalize_cfg：合法 to → to_ts；空/缺省 → 不设 to_ts（旧方案兼容）；非法 → ValueError；
    to 早于 from → ValueError；to == from（单日窗口）→ 放行；
  - load_bars（cache 路径，不连 CDP）：to_ts 逐周期滤掉 time > to_ts 的K线；None 原样返回；
  - 截断数据端到端：加载层截断后跑引擎，无任何结束日之后的成交，lastTime <= to_ts。

运行：python -m unittest py_chain.test_to_ts -v
"""

import copy
import json
import os
import tempfile
import unittest

from py_chain import data_loader
from py_chain.backtest import BacktestEngine
from py_chain.main import parse_from
from py_chain.test_start_ts import bars_all

from . import webapp


class TestNormalizeToTs(unittest.TestCase):
    def test_valid_to(self):
        out = webapp.ControlApp.normalize_cfg(
            {"from": "2026-07-02", "to": "2026-08-31"}, "backtest")
        self.assertEqual(out["to_ts"], parse_from("2026-08-31") + 86400 - 1)  # 含当日全天
        self.assertEqual(out["from_ts"], parse_from("2026-07-02"))
        self.assertGreater(out["to_ts"], out["from_ts"])

    def test_missing_or_empty_to(self):
        # 旧方案库 cfg 无 to / 空字符串 → 不设 to_ts，行为与旧版一致
        for cfg in ({"from": "2026-07-02"}, {"from": "2026-07-02", "to": ""}):
            out = webapp.ControlApp.normalize_cfg(dict(cfg), "backtest")
            self.assertNotIn("to_ts", out)
            self.assertEqual(out["from_ts"], parse_from("2026-07-02"))

    def test_invalid_to(self):
        with self.assertRaises(ValueError):
            webapp.ControlApp.normalize_cfg({"to": "2026/08/31"}, "backtest")

    def test_to_before_from_rejected(self):
        with self.assertRaises(ValueError):
            webapp.ControlApp.normalize_cfg(
                {"from": "2026-07-02", "to": "2026-07-01"}, "backtest")

    def test_to_equal_from_single_day(self):
        # 同日 = 单日窗口（该日 00:00 ~ 23:59:59），合法
        out = webapp.ControlApp.normalize_cfg(
            {"from": "2026-07-02", "to": "2026-07-02"}, "backtest")
        self.assertEqual(out["to_ts"] - out["from_ts"], 86399)


class TestLoadBarsToTs(unittest.TestCase):
    def _tmp_cache(self, bars):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bars, f)
        self.addCleanup(os.unlink, path)
        return path

    def test_cache_trim(self):
        bars = {"3": [{"time": t} for t in (100, 200, 300, 400)],
                "D": [{"time": t} for t in (100, 400)]}
        path = self._tmp_cache(bars)
        out = data_loader.load_bars(use_cache=True, cache_file=path, to_ts=250)
        self.assertEqual([b["time"] for b in out["3"]], [100, 200])
        self.assertEqual([b["time"] for b in out["D"]], [100])
        # to_ts=None → 原样返回（到最新）
        out2 = data_loader.load_bars(use_cache=True, cache_file=path, to_ts=None)
        self.assertEqual([b["time"] for b in out2["3"]], [100, 200, 300, 400])


class TestBacktestEndsAtToTs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = bars_all()
        cls.fine = cls.bars["3"]

    def test_no_entries_after_to_ts(self):
        # BacktestWorker 语义：加载层截断后交给引擎——成交/lastTime 均止于 to_ts
        to_ts = self.fine[1500]["time"]
        bars = data_loader._trim_to_ts(copy.deepcopy(self.bars), to_ts)
        self.assertLessEqual(bars["3"][-1]["time"], to_ts)   # 加载末根 = 结束日末根
        res = BacktestEngine(bars, warmup_bars=60).run(log=lambda *a, **k: None)
        for tr in res["trades"]:
            self.assertLessEqual(tr["entryTime"], to_ts)     # 结束日之后无成交
        self.assertLessEqual(res["lastTime"], to_ts)         # mark-to-market 末根口径


if __name__ == "__main__":
    unittest.main()
