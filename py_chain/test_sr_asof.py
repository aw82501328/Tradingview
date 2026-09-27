# -*- coding: utf-8 -*-
"""「时点+回溯」（SR 控制台 as-of）单元测试（2026-09-26）

口径：
- 时点（cfg.from 语义重释）：空 = 当下（无 as_of_ts，不截断）；纯日期 →
  上海时间当日 00:00~23:59:59（as_of_ts = 上海日切 +86399，与页面 UTC+8 锚点同区）；
  带时分 → 上海时间精确截断并对齐 3 分钟格
- lookbackBars：密集区限窗 N（每周期各自计数，0/空=不限，缺省 300）。
  仅控制台路径经 build_chain_result(engine_extra={clusterLookbackBars}) 传入引擎；
  engine_kwargs_of 显式 16 键枚举永不映射 → 回测/live 语义逐位不变
- 取数：fetch_from_map 逐周期起点（max(N,500) 根×1.5 日历换算 + 2 天垫量）；
  ensure_symbol_data 新模式缓存存 [fetch_from, 最新]、输出截 <= as_of_ts，
  market 记录 fetchFrom 戳防「源已到头」时反复重拉

运行：python -m unittest py_chain.test_sr_asof -v
"""
import inspect
import types
import unittest
from unittest.mock import patch

from py_chain import sr_service, sr_tune
from py_chain.main import parse_from
from py_chain.sr_flip import compute_srflip
from py_chain.webapp import ControlApp, run_sr_compute


def bar(t, h, l, c):
    return {"time": t, "open": c, "high": h, "low": l, "close": c, "volume": 100}


def bi(type_, startTime, endTime, startPrice, endPrice):
    return {"type": type_, "startTime": startTime, "endTime": endTime,
            "startPrice": startPrice, "endPrice": endPrice,
            "span": abs(endPrice - startPrice)}


class TestFetchFromMap(unittest.TestCase):
    def test_per_period_spans_differ_and_floor_applies(self):
        n = max(100, sr_service.LOOKBACK_FLOOR_BARS)
        ft = sr_service.fetch_from_map(["D", "3"], as_of_ts=1_000_000_000, lookback_bars=100)
        self.assertEqual(ft["D"], 1_000_000_000 - int(n * 86400 * 1.5 + 2 * 86400))
        self.assertEqual(ft["3"], 1_000_000_000 - int(n * 180 * 1.5 + 2 * 86400))
        self.assertLess(ft["D"], ft["3"])  # 大周期回溯跨度更长 → 起点更早

    def test_empty_as_of_anchors_now(self):
        now = 1_700_000_000
        ft = sr_service.fetch_from_map(["60"], None, None, now_ts=now)
        self.assertEqual(ft["60"],
                         now - int(sr_service.LOOKBACK_FLOOR_BARS * 3600 * 1.5 + 2 * 86400))


class TestCoverageDict(unittest.TestCase):
    def test_int_from_ts_legacy_rules_unchanged(self):
        bars = {"60": [bar(t, 10, 9, 10) for t in range(100)]}
        self.assertEqual(sr_service.coverage(bars, ["60"], 5)["60"], "ok")
        self.assertEqual(sr_service.coverage(bars, ["60"], -1)["60"], "short")  # 跨度 < 7 天
        self.assertEqual(sr_service.coverage({}, ["60"], 5)["60"], "missing")

    def test_dict_from_ts_with_records(self):
        bars = {"60": [bar(t * 3600, 10, 9, 10) for t in range(10)]}
        ft = {"60": -100}
        # 首根(0) > 起点(-100)：无 records 且跨度 < 7 天 → short（需补拉）
        self.assertEqual(sr_service.coverage(bars, ["60"], ft)["60"], "short")
        # records 带 fetchFrom <= 起点 → partial（源深度已探明，不重拉）
        recs = {"60": {"fetchFrom": -200}}
        self.assertEqual(sr_service.coverage(bars, ["60"], ft, records=recs)["60"], "partial")
        # 旧缓存无 fetchFrom 戳 → 仍 short（首算重拉一次后打戳）
        self.assertEqual(sr_service.coverage(bars, ["60"], ft, records={"60": {}})["60"], "short")
        # 已覆盖 → ok
        self.assertEqual(sr_service.coverage(bars, ["60"], {"60": 0}, records=recs)["60"], "ok")


class TestNormalizeAsOf(unittest.TestCase):
    def _cfg(self, _from=None, **kw):
        base = {"symbol": "OANDA:XAUUSD", "periods": ["D", "60"], "srTypes": ["cluster"]}
        if _from is not None:
            base["from"] = _from
        base.update(kw)
        return ControlApp.normalize_sr_cfg(base)

    def test_empty_from_means_now(self):
        cfg = self._cfg(_from="")
        self.assertNotIn("as_of_ts", cfg)
        self.assertNotIn("from_ts", cfg)

    def test_from_date_is_inclusive_end_of_day(self):
        cfg = self._cfg(_from="2026-07-02")
        # 上海 2026-07-02 00:00 = UTC 前一日 16:00；截止 = 当日 23:59:59
        start = parse_from("2026-07-02") - 8 * 3600
        self.assertEqual(cfg["from_ts"], start)
        self.assertEqual(cfg["as_of_ts"], start + 86400 - 1)

    def test_invalid_and_future_date_rejected(self):
        with self.assertRaises(ValueError):
            self._cfg(_from="not-a-date")
        with self.assertRaises(ValueError):
            self._cfg(_from="2100-01-01")

    def test_lookback_bars_default_zero_and_validation(self):
        self.assertEqual(self._cfg()["lookbackBars"], sr_service.LOOKBACK_BARS_DEFAULT)
        self.assertEqual(self._cfg(lookbackBars="")["lookbackBars"], 0)
        self.assertEqual(self._cfg(lookbackBars=None)["lookbackBars"], 0)
        self.assertEqual(self._cfg(lookbackBars="120")["lookbackBars"], 120)
        with self.assertRaises(ValueError):
            self._cfg(lookbackBars="abc")
        with self.assertRaises(ValueError):
            self._cfg(lookbackBars=5001)
        with self.assertRaises(ValueError):
            self._cfg(lookbackBars=-1)


def _asof_fixture():
    """早密集簇（≈130，t10-80）+ 过渡下跌笔 + 晚密集簇（≈110，t190-260）。
    笔严格交替（up/down 起止连续），每簇 8 个 swing 且首 L 末 H → detectFlip
    直接判 S2R。"""
    early = [bi("up" if i % 2 == 0 else "down", 10 + 10 * i, 20 + 10 * i,
                129.8 if i % 2 == 0 else 130.2, 130.2 if i % 2 == 0 else 129.8)
             for i in range(7)]
    late = [bi("up" if i % 2 == 0 else "down", 190 + 10 * i, 200 + 10 * i,
               109.8 if i % 2 == 0 else 110.2, 110.2 if i % 2 == 0 else 109.8)
            for i in range(7)]
    trans = bi("down", 80, 190, 130.2, 109.8)
    # early 末笔 up(t70-80)，trans down，late 首笔 up(t190-200)：交替成立
    return ({"3": early + [trans] + late},
            {"3": [bar(t, 121, 119, 120) for t in range(1, 301)]})


class TestClusterLookbackWindow(unittest.TestCase):
    """clusterLookbackBars 限窗只影响密集区；fib/BOLL/现价逐位不变；work_cache 不串键。"""

    def setUp(self):
        self.bis, self.bars = _asof_fixture()

    def _run(self, **kw):
        # clusterParts 只留 flip：recent 子类按「最近 N 根笔」取点，计数随笔数变化，
        # 与限窗断言纠缠；flip 子类的时间窗语义即本测试对象
        return compute_srflip(self.bis, self.bars, ["3"],
                              srTypes=("cluster", "fib", "boll"),
                              clusterParts=("flip",),
                              periodAtrsIn={"3": 1}, minTouchsIn={"3": 4}, **kw)

    @staticmethod
    def _clusters(out):
        return sorted(round(f["price"], 6) for f in out["merged"]
                      if not f.get("fib") and not f.get("boll") and not f.get("manual"))

    @staticmethod
    def _fibs(out):
        return sorted((f["type"], round(f["price"], 9)) for f in out["merged"] if f.get("fib"))

    @staticmethod
    def _bolls(out):
        return sorted((f["type"], round(f["price"], 9)) for f in out["merged"] if f.get("boll"))

    def test_window_drops_old_cluster_keeps_fib_boll_current(self):
        full = self._run()
        lim = self._run(clusterLookbackBars=120)  # cBars ≈ t181-300：早簇(≤80)出窗
        self.assertEqual(len(self._clusters(full)), 2)   # ≈130 与 ≈110 两簇
        self.assertEqual(len(self._clusters(lim)), 1)   # 早簇消失、晚簇保留
        self.assertAlmostEqual(self._clusters(lim)[0], 110.0, places=6)
        # fib/BOLL/现价：全窗取当下，与不限窗逐位一致
        self.assertEqual(self._fibs(full), self._fibs(lim))
        self.assertEqual(self._bolls(full), self._bolls(lim))
        self.assertEqual(full["currentPrice"], lim["currentPrice"])

    def test_zero_or_none_means_unlimited(self):
        base = self._clusters(self._run())
        self.assertEqual(self._clusters(self._run(clusterLookbackBars=0)), base)
        self.assertEqual(self._clusters(self._run(clusterLookbackBars=None)), base)

    def test_work_cache_not_polluted_across_windows(self):
        wc = {}
        full = self._run(work_cache=wc)
        lim = self._run(clusterLookbackBars=120, work_cache=wc)  # 同 cache 不同 N
        self.assertEqual(len(self._clusters(full)), 2)
        self.assertEqual(len(self._clusters(lim)), 1)


class TestFibAnchorWindow(unittest.TestCase):
    """fib 锚点限窗（fib 只当下）：锚点须形成于最近 N 根内，窗外方向不出 fib。
    复刻 2026-09-26 用户案例：时点=9-2、D 买向锚点停在 2024-12（窗外）→ 买向 3 条应消失。"""

    def setUp(self):
        # 交替笔 t=0..900：down 笔终于 t=100（2买锚点），末笔 up 终于 t=900（2卖锚点，
        # 也是最后一根笔 → 买向无形成中回调笔，pending 不触发）
        self.bis = {"3": [
            bi("up", 0, 50, 100, 115), bi("down", 50, 100, 115, 100),      # 2买 @ t=100
            bi("up", 100, 200, 100, 116), bi("down", 200, 300, 116, 101),
            bi("up", 300, 400, 101, 117), bi("down", 400, 500, 117, 102),
            bi("up", 500, 600, 102, 118), bi("down", 600, 700, 118, 103),
            bi("up", 700, 800, 103, 119), bi("down", 800, 850, 119, 104),
            bi("up", 850, 900, 104, 120),                                  # 2卖 @ t=900（末笔）
        ]}
        self.bars = {"3": [bar(t, 121, 99, 110) for t in range(1, 1001)]}
        self.buy = [{"type": "2买", "time": 100, "price": 100}]
        self.sell = [{"type": "2卖", "time": 900, "price": 120}]

    def _fibs(self, **kw):
        with patch("py_chain.sr_flip.findBuyPoints", return_value=list(self.buy)), \
             patch("py_chain.sr_flip.findSellPoints", return_value=list(self.sell)):
            out = compute_srflip(self.bis, self.bars, ["3"], srTypes=("fib", "boll"),
                                 fibLevels=[0.382, 0.5, 0.618],
                                 periodAtrsIn={"3": 1}, **kw)
        return out, [f for f in out["merged"] if f.get("fib")]

    def test_anchor_outside_window_drops_that_side(self):
        out_full, fibs_full = self._fibs()
        self.assertEqual(len(fibs_full), 6)                       # 不限窗：买向3+卖向3
        out_lim, fibs_lim = self._fibs(fibLookbackBars=200)       # 窗≈t801..1000
        self.assertEqual(len(fibs_lim), 3)                        # 买向锚点 t=100 出窗 → 只剩卖向
        self.assertTrue(all(f["type"] == "RES" for f in fibs_lim))
        self.assertTrue(all(not f.get("pending") for f in fibs_lim))
        # BOLL 天然当下：不受 fib 限窗影响
        boll_full = sorted(round(f["price"], 6) for f in out_full["merged"] if f.get("boll"))
        boll_lim = sorted(round(f["price"], 6) for f in out_lim["merged"] if f.get("boll"))
        self.assertEqual(boll_full, boll_lim)

    def test_zero_or_none_means_unlimited_and_cache_safe(self):
        base = [f["price"] for f in self._fibs()[1]]
        self.assertEqual(len(self._fibs(fibLookbackBars=0)[1]), len(base))
        wc = {}
        fibs_a = self._fibs(fibLookbackBars=200, work_cache=wc)[1]
        fibs_b = self._fibs(work_cache=wc)[1]                     # 同 cache 不同 N 不串
        self.assertEqual(len(fibs_a), 3)
        self.assertEqual(len(fibs_b), 6)

    def test_last_stroke_extending_uses_final_bi(self):
        # 延伸中（末根K线=极值K线，gap=0）→ 参照=末笔 up 104→120 → SUP 3 条
        bars = {"3": [bar(t, 121, 99, 110) for t in range(1, 901)]}   # 末根 t=900=末笔终点
        with patch("py_chain.sr_flip.findBuyPoints", return_value=list(self.buy)), \
             patch("py_chain.sr_flip.findSellPoints", return_value=list(self.sell)):
            out = compute_srflip(self.bis, bars, ["3"], srTypes=("fib",),
                                 fibLevels=[0.382, 0.5, 0.618],
                                 periodAtrsIn={"3": 1}, fibLastStroke=True)
        fibs = [f for f in out["merged"] if f.get("fib")]
        self.assertEqual(len(fibs), 3)
        self.assertTrue(all(f["type"] == "SUP" for f in fibs))
        self.assertEqual(sorted(round(f["price"], 6) for f in fibs),
                         sorted([round(120 - r * 16, 6) for r in (0.382, 0.5, 0.618)]))
        self.assertTrue(all(f["fromPoint"]["type"] == "最近段" for f in fibs))

    def test_last_stroke_retracing_uses_second_last_bi(self):
        # 回撤中（极值K线距末根 > 一根间距）→ 参照=bis[-2]（与回撤同向的推进笔）
        # 本夹具 bis[-1]=up(850,900,104,120)、bis[-2]=down(800,850,119,104)；
        # bars 到 t=1000 → gap=100 > spacing=1 → 参照 down 119→104 → RES 3 条
        fibs = self._fibs(fibLastStroke=True)[1]
        self.assertEqual(len(fibs), 3)
        self.assertTrue(all(f["type"] == "RES" for f in fibs))
        self.assertEqual(sorted(round(f["price"], 6) for f in fibs),
                         sorted([round(104 + r * 15, 6) for r in (0.382, 0.5, 0.618)]))
        # 与白名单点无关
        with patch("py_chain.sr_flip.findBuyPoints", return_value=[]), \
             patch("py_chain.sr_flip.findSellPoints", return_value=[]):
            out = compute_srflip(self.bis, self.bars, ["3"], srTypes=("fib",),
                                 fibLevels=[0.382, 0.5, 0.618],
                                 periodAtrsIn={"3": 1}, fibLastStroke=True)
        self.assertEqual([f["price"] for f in out["merged"]],
                         [f["price"] for f in fibs])
        # 默认 False = 白名单口径（双侧 6 条）；cache 不同口径不串
        self.assertEqual(len(self._fibs()[1]), 6)
        wc = {}
        self.assertEqual(len(self._fibs(fibLastStroke=True, work_cache=wc)[1]), 3)
        self.assertEqual(len(self._fibs(work_cache=wc)[1]), 6)

    def test_last_stroke_user_case_240_res_4320(self):
        # 复刻 2026-09-26 用户案例：末笔=反弹 up 4244.3→4315.8，现价回撤中
        # → 参照=倒数第二笔 down 4397→4244.3 → RES 0.5 = 4320.65
        bis = {"240": [
            bi("up", 0, 50, 4100, 4200),
            bi("down", 50, 100, 4200, 4150),
            bi("up", 100, 150, 4150, 4397.0),
            bi("down", 150, 200, 4397.0, 4244.3),
            bi("up", 200, 250, 4244.3, 4315.8),     # 末笔=反弹（被回撤中）
        ]}
        bars = {"240": [bar(t, 4330, 4240, 4285) for t in range(1, 300)]}  # 尾部回撤
        with patch("py_chain.sr_flip.findBuyPoints", return_value=[]), \
             patch("py_chain.sr_flip.findSellPoints", return_value=[]):
            out = compute_srflip(bis, bars, ["240"], srTypes=("fib",),
                                 fibLevels=[0.382, 0.5, 0.618],
                                 periodAtrsIn={"240": 1}, fibLastStroke=True)
        fibs = [f for f in out["merged"] if f.get("fib")]
        self.assertEqual(len(fibs), 3)
        self.assertTrue(all(f["type"] == "RES" for f in fibs))
        rb = fibs[0]["referBi"]
        self.assertEqual(rb["startPrice"], 4397.0)
        self.assertEqual(rb["endPrice"], 4244.3)
        mid = [f for f in fibs if abs(f["ratio"] - 0.5) < 1e-9][0]
        self.assertAlmostEqual(mid["price"], 4320.65, places=2)   # 用户预期数字

    def test_last_stroke_dirty_alternation_falls_back(self):
        # 脏数据：bis[-2] 与末笔同向 → 回退末笔本身
        bis = {"3": [
            bi("down", 0, 50, 120, 100),
            bi("up", 50, 100, 100, 110),
            bi("up", 100, 900, 110, 120),           # 同向脏数据（末笔 up 110→120）
        ]}
        bars = {"3": [bar(t, 121, 99, 110) for t in range(1, 1001)]}
        with patch("py_chain.sr_flip.findBuyPoints", return_value=[]), \
             patch("py_chain.sr_flip.findSellPoints", return_value=[]):
            out = compute_srflip(bis, bars, ["3"], srTypes=("fib",),
                                 fibLevels=[0.5], periodAtrsIn={"3": 1},
                                 fibLastStroke=True)
        fibs = [f for f in out["merged"] if f.get("fib")]
        self.assertEqual(len(fibs), 1)
        self.assertEqual(fibs[0]["type"], "SUP")    # 末笔 up 本身
        self.assertAlmostEqual(fibs[0]["price"], 120 - 0.5 * 10, places=9)


class TestNormalizeMinuteAsOf(unittest.TestCase):
    """时点分钟精度：带时分按上海时间解析并向下对齐 3 分钟格；纯日期=上海当日结束。"""

    def _cfg(self, _from):
        return ControlApp.normalize_sr_cfg(
            {"symbol": "OANDA:XAUUSD", "periods": ["D", "60"], "srTypes": ["cluster"],
             "from": _from})

    def test_minute_input_shanghai_with_3min_snap(self):
        # 2026-09-02 00:00 UTC = 1788307200；上海 14:31 = UTC 06:31 → 对齐 3 分钟 → 06:30
        self.assertEqual(self._cfg("2026-09-02 14:31")["as_of_ts"], 1788330600)
        self.assertEqual(self._cfg("2026-09-02 14:30")["as_of_ts"], 1788330600)  # 已在格上
        self.assertEqual(self._cfg("2026-09-02T14:31")["as_of_ts"], 1788330600)  # ISO T 分隔
        cfg = self._cfg("2026-09-02 14:31")
        self.assertEqual(cfg["from_ts"], 1788330600)              # 兼容键同步

    def test_date_only_is_shanghai_end_of_day(self):
        cfg = self._cfg("2026-07-02")
        start = parse_from("2026-07-02") - 8 * 3600
        self.assertEqual(cfg["as_of_ts"], start + 86400 - 1)
        self.assertEqual(cfg["from_ts"], start)

    def test_invalid_minute_input_rejected(self):
        with self.assertRaises(ValueError):
            self._cfg("2026-09-02 25:00")
        with self.assertRaises(ValueError):
            self._cfg("not-a-date 14:30")

    def test_empty_means_now(self):
        cfg = self._cfg("")
        self.assertNotIn("as_of_ts", cfg)


class TestEngineSync(unittest.TestCase):
    """2026-09-26 引擎同步（用户拍板）：engine_kwargs_of 映射 lookbackBars→
    clusterLookbackBars、fibLastStroke 恒 True——回测/分析/控制台同口径。"""

    def test_engine_kwargs_maps_lookback_and_laststroke(self):
        cfg = ControlApp.normalize_sr_cfg(
            {"symbol": "OANDA:XAUUSD", "periods": ["D", "60"], "srTypes": ["cluster"],
             "lookbackBars": 300})
        kw = sr_service.engine_kwargs_of(cfg)
        self.assertEqual(kw["clusterLookbackBars"], 300)
        self.assertIs(kw["fibLastStroke"], True)
        self.assertTrue(set(kw) <= set(inspect.signature(compute_srflip).parameters))

    def test_missing_lookback_means_unlimited(self):
        kw = sr_service.engine_kwargs_of({"srTypes": ["cluster"]})
        self.assertEqual(kw["clusterLookbackBars"], 0)   # 0=不限=全前缀

    def test_build_chain_result_windows_without_extra(self):
        # 分析页路径（不传 engine_extra）：cfg 带 lookbackBars → 密集区限窗同样生效
        bis, bars = _asof_fixture()
        cfg = {"periods": ["3"], "srTypes": ["cluster"], "lookbackBars": 120,
               "clusterParts": ["flip"], "minTouchs": {"3": 4}}
        result, _ = sr_service.build_chain_result(bars, cfg,
                                                  log=lambda *a, **k: None, bis_by_period=bis)
        got = sorted(round(f["price"], 6) for f in result["merged"] if not f.get("manual"))
        self.assertEqual(len(got), 1)   # 早簇（≈130）出窗，只剩晚簇（≈110）
        self.assertAlmostEqual(got[0], 110.0, places=6)


class TestBollIncludeLast(unittest.TestCase):
    """BOLL 末根口径（2026-09-27）：调参页含末根（engine_extra 通道，与 TV 当前 bar 同拍）；
    engine_kwargs_of 永不映射 → 回测/分析/CLI 机制上拿不到，默认 False=已收盘锚点不动。"""

    def test_engine_kwargs_never_maps_boll_include_last(self):
        # 手写预设/品种桶带杂键也惰性：engine_kwargs_of 显式枚举不含该键
        kw = sr_service.engine_kwargs_of({"srTypes": ["cluster"],
                                          "bollIncludeLast": True})
        self.assertNotIn("bollIncludeLast", kw)

    def test_compute_srflip_default_off(self):
        # 引擎默认 = 已收盘口径（回测历史锚点）；形参存在且缺省 False
        self.assertIs(inspect.signature(compute_srflip)
                      .parameters["bollIncludeLast"].default, False)

    def test_run_sr_compute_routes_console_only_extra(self):
        # run_sr_compute（SR 调参页 /api/sr/compute）经 engine_extra 传 bollIncludeLast=True
        app = types.SimpleNamespace(
            sr={}, broadcaster=types.SimpleNamespace(emit=lambda *a, **k: None))
        got = {}

        def fake_build(bars, cfg, log=None, **kw):
            got.update(kw)
            return ({"currentPrice": 100, "merged": [], "drawnByPeriod": {}},
                    {"per_level_atr": {}})

        cfg = ControlApp.normalize_sr_cfg(
            {"symbols": "OANDA:XAUUSD", "from": "2026-07-01",
             "periods": ["D"], "srTypes": ["cluster"]})
        with patch("py_chain.sr_service.ensure_data",
                   side_effect=lambda *a, **k: {"D": [{"time": 1}]}), \
             patch("py_chain.sr_service.build_chain_result", side_effect=fake_build):
            run_sr_compute(app, cfg, "auto")
        self.assertEqual(got.get("engine_extra"), {"bollIncludeLast": True})

    def test_build_chain_result_extra_changes_boll_only(self):
        # engine_extra 只改 BOLL：同夹具（末根 close 可区分）两口径 BOLL 价不同、
        # 密集区/fib 逐位不变
        cfg = {"periods": ["3"], "srTypes": ["cluster", "fib", "boll"],
               "clusterParts": ["flip"], "minTouchs": {"3": 4}}

        def run(extra):
            bis, bars = _asof_fixture()
            bars["3"][-1] = bar(300, 999, 999, 999)   # 末根 close 设计成可区分值
            result, _ = sr_service.build_chain_result(
                bars, cfg, log=lambda *a, **k: None,
                bis_by_period=bis, engine_extra=extra)
            return result

        a, b = run(None), run({"bollIncludeLast": True})
        a_boll = sorted(round(f["price"], 6) for f in a["merged"] if f.get("boll"))
        b_boll = sorted(round(f["price"], 6) for f in b["merged"] if f.get("boll"))
        # 默认：bars[:-1] 全是 120 → σ=0 三轨重合；含末根：999 拉开轨道
        self.assertEqual(a_boll, [120.0, 120.0, 120.0])
        self.assertNotEqual(a_boll, b_boll)
        a_other = [round(f["price"], 9) for f in a["merged"] if not f.get("boll")]
        b_other = [round(f["price"], 9) for f in b["merged"] if not f.get("boll")]
        self.assertEqual(a_other, b_other)


class _MemStore:
    """sr_tune.Store 最小替身：get 缺键抛 ValueError（与真 Store 行为一致）。"""

    def __init__(self):
        self.d = {}

    def get(self, ns, key):
        if key not in self.d:
            raise ValueError("missing")
        return self.d[key]

    def put(self, ns, key, val):
        self.d[key] = val


class TestEnsureSymbolDataAsOf(unittest.TestCase):
    """新模式的取数编排：db 优先补深 / CDP 兜底 / 有界窗口 / 宽容跳过。
    query_bars 统一 patch（_db_bars 内部吞掉一切异常，空结果=继续走 CDP）。"""

    NO_DB = {"total": 0, "rows": []}

    def test_new_mode_truncates_passes_dict_from_and_stamps(self):
        bars_all = [bar(t, 10, 9, 10) for t in range(0, 3000, 60)]  # 50 根：0..2940
        store = _MemStore()
        ft = {"60": -1000}
        with patch.object(sr_tune, "Store", return_value=store), \
             patch("py_chain.data_store.query_bars", return_value=self.NO_DB), \
             patch.object(sr_service.data_loader, "fetch_bars",
                          return_value={"60": bars_all}) as fetch:
            out = sr_service.ensure_symbol_data(["60"], None, "TEST:FUT",
                                                lambda *a, **k: None,
                                                fetch_froms=ft, as_of_ts=1500)
            self.assertTrue(all(b["time"] <= 1500 for b in out["60"]))   # 截 <= as_of
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.kwargs["from_ts"], {"60": -1000})  # 逐周期 dict
            rec = store.d[sr_tune.digest({"symbol": "TEST:FUT", "period": "60"})]
            self.assertEqual(rec["fetchFrom"], 0)   # 诚实戳=实际持有最早K线
            # 二次：源深度已探明（fetchFrom <= 起点）→ partial，不重拉
            sr_service.ensure_symbol_data(["60"], None, "TEST:FUT",
                                          lambda *a, **k: None,
                                          fetch_froms=ft, as_of_ts=1500)
            self.assertEqual(fetch.call_count, 1)

    def test_empty_as_of_keeps_latest(self):
        bars_all = [bar(t, 10, 9, 10) for t in range(0, 3000, 60)]
        store = _MemStore()
        with patch.object(sr_tune, "Store", return_value=store), \
             patch("py_chain.data_store.query_bars", return_value=self.NO_DB), \
             patch.object(sr_service.data_loader, "fetch_bars",
                          return_value={"60": bars_all}):
            out = sr_service.ensure_symbol_data(["60"], None, "TEST:FUT",
                                                lambda *a, **k: None,
                                                fetch_froms={"60": -1000}, as_of_ts=None)
            self.assertEqual(out["60"][-1]["time"], 2940)  # 不截断 → 最新

    def test_db_covers_window_skips_cdp(self):
        db_rows = [bar(t, 12, 11, 11) for t in range(0, 2400, 60)]
        store = _MemStore()
        with patch.object(sr_tune, "Store", return_value=store), \
             patch("py_chain.data_store.query_bars",
                   return_value={"total": len(db_rows), "rows": db_rows}), \
             patch.object(sr_service.data_loader, "fetch_bars") as fetch:
            out = sr_service.ensure_symbol_data(["60"], None, "TEST:FUT",
                                                lambda *a, **k: None,
                                                fetch_froms={"60": -100}, as_of_ts=1000)
            fetch.assert_not_called()                      # db 覆盖回溯窗+时点 → 免 CDP
            self.assertTrue(all(b["time"] <= 1000 for b in out["60"]))
            rec = store.d[sr_tune.digest({"symbol": "TEST:FUT", "period": "60"})]
            self.assertEqual(rec["fetchFrom"], 0)          # 诚实戳=实际首根
            # 二次同参：缓存已覆盖 → db/CDP 都不再访问
            sr_service.ensure_symbol_data(["60"], None, "TEST:FUT",
                                          lambda *a, **k: None,
                                          fetch_froms={"60": -100}, as_of_ts=1000)
            fetch.assert_not_called()

    def test_bounded_window_even_when_cache_is_deeper(self):
        # 缓存深于回溯起点（历史深拉残留）→ 输出仍从回溯起点截起，不随缓存膨胀。
        # 用 3 分钟周期（intervalSecOf=180）：lo = 3000 - 2×180 = 2640，网格上首根 2700
        deep = [bar(t, 10, 9, 10) for t in range(0, 6000, 180)]
        store = _MemStore()
        store.d[sr_tune.digest({"symbol": "TEST:FUT", "period": "3"})] = \
            {"symbol": "TEST:FUT", "period": "3", "bars": deep, "fetchFrom": 0}
        with patch.object(sr_tune, "Store", return_value=store), \
             patch.object(sr_service.data_loader, "fetch_bars") as fetch:
            out = sr_service.ensure_symbol_data(["3"], None, "TEST:FUT",
                                                lambda *a, **k: None,
                                                fetch_froms={"3": 3000}, as_of_ts=5000)
            fetch.assert_not_called()                      # 覆盖（首根 0 <= 3000）
            self.assertEqual(out["3"][0]["time"], 2700)    # 有下界（≥ 2640 的首根）
            self.assertEqual(out["3"][-1]["time"], 4860)

    def test_period_without_data_is_skipped_not_fatal(self):
        bars_all = [bar(t, 10, 9, 10) for t in range(0, 3000, 60)]
        late_only = [bar(t, 10, 9, 10) for t in range(2400, 3000, 60)]  # 全部晚于 as_of
        store = _MemStore()
        with patch.object(sr_tune, "Store", return_value=store), \
             patch("py_chain.data_store.query_bars", return_value=self.NO_DB), \
             patch.object(sr_service.data_loader, "fetch_bars",
                          side_effect=lambda **kw: {"60": bars_all, "3": late_only}):
            out = sr_service.ensure_symbol_data(["60", "3"], None, "TEST:FUT",
                                                lambda *a, **k: None,
                                                fetch_froms={"60": -1000, "3": -1000},
                                                as_of_ts=1500)
            self.assertIn("60", out)          # 可得周期照算
            self.assertNotIn("3", out)        # 时点前无数据 → 跳过，不整品种失败
        # 全部周期不可得 → 报错（有明确指引）
        with patch.object(sr_tune, "Store", return_value=_MemStore()), \
             patch("py_chain.data_store.query_bars", return_value=self.NO_DB), \
             patch.object(sr_service.data_loader, "fetch_bars",
                          return_value={"60": late_only, "3": late_only}):
            with self.assertRaises(RuntimeError):
                sr_service.ensure_symbol_data(["60", "3"], None, "TEST:FUT",
                                              lambda *a, **k: None,
                                              fetch_froms={"60": -1000, "3": -1000},
                                              as_of_ts=1500)

    def test_dishonest_fetchfrom_stamp_still_tries_db(self):
        """2026-09-26 事故回归：CDP 只给到浅处时旧版把 fetchFrom 戳成请求深度，
        coverage 误判「已探明」→ 拦住 db 补深 → 3m 被整周期跳过。
        现口径：是否补深只看缓存首根 vs 回溯起点（与戳无关），db 每次都可复询。
        时间用天级刻度（起点容差 HEAD_TOL_SEC=4 天，浅缓存首根须晚于起点 >4 天）。"""
        DAY = 86400
        shallow = [bar(t, 10, 9, 10) for t in range(10 * DAY, 30 * DAY, 3600)]  # CDP 残留浅缓存
        db_rows = [bar(t, 12, 11, 11) for t in range(0, 30 * DAY, 3600)]        # db 有深数据
        store = _MemStore()
        store.d[sr_tune.digest({"symbol": "TEST:FUT", "period": "60"})] = {
            "symbol": "TEST:FUT", "period": "60", "bars": shallow,
            "fetchFrom": -1000}   # 旧版不诚实戳：声称探到 -1000，实际首根 10 天后
        with patch.object(sr_tune, "Store", return_value=store), \
             patch("py_chain.data_store.query_bars",
                   return_value={"total": len(db_rows), "rows": db_rows}), \
             patch.object(sr_service.data_loader, "fetch_bars") as fetch:
            out = sr_service.ensure_symbol_data(["60"], None, "TEST:FUT",
                                                lambda *a, **k: None,
                                                fetch_froms={"60": 0}, as_of_ts=20 * DAY)
            fetch.assert_not_called()          # db 覆盖 → 免 CDP
            self.assertTrue(out["60"][0]["time"] <= 20 * DAY)
            rec = store.d[sr_tune.digest({"symbol": "TEST:FUT", "period": "60"})]
            self.assertEqual(rec["fetchFrom"], 0)           # 诚实戳=实际首根

    def test_cdp_probed_stamp_prevents_repeated_deep_pull(self):
        """CDP 到头后不重拉：cdpProbedTo 戳生效，第二次同参计算不再发起 CDP。"""
        DAY = 86400
        shallow = [bar(t, 10, 9, 10) for t in range(5 * DAY, 15 * DAY, 3600)]  # 小时K浅缓存
        store = _MemStore()
        with patch.object(sr_tune, "Store", return_value=store), \
             patch("py_chain.data_store.query_bars", return_value=self.NO_DB), \
             patch.object(sr_service.data_loader, "fetch_bars",
                          return_value={"60": shallow}) as fetch:
            sr_service.ensure_symbol_data(["60"], None, "TEST:FUT",
                                          lambda *a, **k: None,
                                          fetch_froms={"60": 0}, as_of_ts=10 * DAY)
            self.assertEqual(fetch.call_count, 1)
            rec = store.d[sr_tune.digest({"symbol": "TEST:FUT", "period": "60"})]
            self.assertEqual(rec["cdpProbedTo"], 0)             # 探测戳
            self.assertEqual(rec["fetchFrom"], 5 * DAY)         # 诚实戳
            sr_service.ensure_symbol_data(["60"], None, "TEST:FUT",
                                          lambda *a, **k: None,
                                          fetch_froms={"60": 0}, as_of_ts=10 * DAY)
            self.assertEqual(fetch.call_count, 1)               # 不重拉


if __name__ == "__main__":
    unittest.main()
