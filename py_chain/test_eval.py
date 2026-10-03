# -*- coding: utf-8 -*-
"""eval_service 单元测试：用例快照/去重/截断校验、基线冻结、跑基线比对与差异定位、
画笔参数应用与恢复、基线引用保护、比对函数本身。

运行：python -m unittest py_chain.test_eval -v
"""

import json
import math
import tempfile
import time
import unittest
from pathlib import Path

from py_chain import chan_core
from py_chain.eval_service import EvalManager, align_bis, align_marks, compare_bis, compare_marks

INTERVALS = {"D": 86400, "240": 14400, "60": 3600, "15": 900, "3": 180}
T0 = 1788192000  # 2026-09-01 00:00 CST


def synth_bars(res, n=240):
    """正弦波动K线：交替顶底足够成笔。"""
    bars, prev = [], 4400.0
    for i in range(n):
        close = 4400.0 + 40.0 * math.sin(i / 6.0) + 2.0 * math.sin(i / 1.3)
        o = prev
        bars.append({"time": T0 + i * INTERVALS[res],
                     "open": round(o, 3),
                     "high": round(max(o, close) + 1.5, 3),
                     "low": round(min(o, close) - 1.5, 3),
                     "close": round(close, 3)})
        prev = close
    return bars


def _flip_cfg(cfg):
    """改动一个画笔参数键值，构造与当前不同的参数快照。"""
    out = dict(cfg)
    k = next(iter(chan_core.CHAN_CFG_DEFAULTS))
    dv = chan_core.CHAN_CFG_DEFAULTS[k]
    if isinstance(dv, bool):
        out[k] = not dv
    elif isinstance(dv, (int, float)):
        out[k] = dv + 1
    return out


class EvalManagerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bars_file = root / "bars_all_tf.json"
        self.bars_file.write_text(
            json.dumps({res: synth_bars(res) for res in INTERVALS}), encoding="utf-8")
        self.mgr = EvalManager(data_dir=root / "eval", bars_file=self.bars_file)
        self.saved_cfg = dict(chan_core.CHAN_CFG)

    def tearDown(self):
        chan_core.apply_cfg(self.saved_cfg)
        self.tmp.cleanup()

    def wait_job(self, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = self.mgr.job_snapshot()
            if snap.get("state") != "running":
                return snap
            time.sleep(0.05)
        self.fail("评估任务超时")

    def make_case(self, name="用例A", res="3", **kw):
        out = self.mgr.create_case({"name": name, "res": res, **kw})
        return out["case"]


class CreateCaseTests(EvalManagerTestBase):
    def test_create_case_fields_and_dedup(self):
        c1 = self.make_case("甲", "3")
        c2 = self.make_case("乙", "15")
        self.assertEqual(c1["res"], "3")
        self.assertEqual(c1["fromTs"], None)
        self.assertEqual(c1["barCounts"]["3"], 240)
        self.assertIn("chanCfg", c1)
        # 同源数据内容寻址去重：两个用例共享一份快照
        snaps = list((Path(self.tmp.name) / "eval" / "snapshots").glob("*.json.gz"))
        self.assertEqual(len(snaps), 1)
        cases = {c["id"]: c for c in self.mgr.list_cases()}
        self.assertTrue(cases[c1["id"]]["snapshotOk"])
        self.assertEqual(c1["snapshot"], c2["snapshot"])

    def test_fromTs_truncation_and_reject(self):
        cut = T0 + 120 * INTERVALS["3"]  # 3分钟剩余120根，其余周期远多于50
        c = self.make_case("截断", "3", fromTs=cut)
        self.assertEqual(c["barCounts"]["3"], 120)
        self.assertGreater(c["barCounts"]["D"], 0)
        # 起点太晚：所选 3 分钟被截到 0 根 → 拒绝（未选周期不再拦截）
        too_late = T0 + 230 * INTERVALS["D"]
        with self.assertRaisesRegex(ValueError, "不足"):
            self.make_case("太晚", "3", fromTs=too_late)
        with self.assertRaisesRegex(ValueError, "不足"):
            self.make_case("太晚ALL", "ALL", fromTs=too_late)

    def test_toTs_truncation_and_validation(self):
        # 覆盖为同一 80 天跨度的数据：toTs=60天 时 3 分钟被截、各周期仍 ≥50 根
        self.bars_file.write_text(json.dumps(
            {res: synth_bars(res, n=80 * 86400 // INTERVALS[res]) for res in INTERVALS}),
            encoding="utf-8")
        day = INTERVALS["D"]
        end = T0 + 60 * day
        c = self.make_case("区间", "3", toTs=end)
        self.assertEqual(c["barCounts"]["3"], 60 * 86400 // 180 + 1)  # 含开盘=end 当根
        self.assertEqual(c["toTs"], end)
        # from+to 双端截断
        c2 = self.make_case("双端", "3", fromTs=T0 + 100 * INTERVALS["3"], toTs=end)
        self.assertEqual(c2["barCounts"]["3"], 60 * 86400 // 180 + 1 - 100)
        # toTs 早于 fromTs → 拒绝
        with self.assertRaisesRegex(ValueError, "晚于"):
            self.make_case("倒序", "3", fromTs=end, toTs=T0)
        # D 截剩 11 根：选 3 分钟可建（未选周期不设下限），选 ALL 则拒绝
        early = T0 + 10 * day
        c3 = self.make_case("D短可建", "3", toTs=early)
        self.assertEqual(c3["barCounts"]["D"], 11)
        self.assertEqual(c3["barCounts"]["3"], 10 * 86400 // 180 + 1)
        with self.assertRaisesRegex(ValueError, "不足"):
            self.make_case("太早ALL", "ALL", toTs=early)
        # 所选周期自身截剩不足50根 → 拒绝
        with self.assertRaisesRegex(ValueError, "不足"):
            self.make_case("3分钟太短", "3", toTs=T0 + 40 * INTERVALS["3"])

    def test_missing_bars_file(self):
        self.bars_file.unlink()
        with self.assertRaisesRegex(ValueError, "缓存不存在"):
            self.make_case()

    def test_name_and_res_validation(self):
        with self.assertRaisesRegex(ValueError, "名称"):
            self.make_case("  ")
        with self.assertRaisesRegex(ValueError, "周期"):
            self.make_case("X", "7")


class CompareMarksTests(unittest.TestCase):
    MK = [{"label": "1买", "time": 100, "price": 10.0, "rawTime": 100, "rawPrice": 10.5, "color": "#F23645"},
          {"label": "2卖", "time": 200, "price": 20.0, "rawTime": 190, "rawPrice": 19.5, "color": "#089981"}]

    def test_identical_marks_pass(self):
        out = compare_marks([dict(m) for m in self.MK], [dict(m) for m in self.MK])
        self.assertTrue(out["ok"])
        self.assertIsNone(out["context"])

    def test_field_tamper_pinpointed(self):
        exp = [dict(m) for m in self.MK]
        act = [dict(m) for m in self.MK]
        act[1]["label"] = "真1卖"
        act[1]["color"] = "#F23645"
        out = compare_marks(exp, act)
        self.assertFalse(out["ok"])
        self.assertEqual(out["firstDiff"], 1)
        fields = {d["field"] for d in out["diffs"]}
        self.assertEqual(fields, {"label", "color"})
        self.assertTrue(out["context"] and out["context"]["rows"])

    def test_count_mismatch_and_align(self):
        act = [dict(self.MK[0])]  # 缺第2个点
        out = compare_marks(self.MK, act)
        self.assertFalse(out["ok"])
        self.assertIn("(点)", {d["field"] for d in out["diffs"]})
        rows = align_marks(self.MK, act)
        self.assertEqual(len(rows), 2)
        self.assertNotIn("b", rows[1])  # 第2行仅基线侧


class FreezeRunTests(EvalManagerTestBase):
    def freeze(self, name="基线甲", case_ids=None):
        r = self.mgr.start_freeze({"name": name, "caseIds": case_ids})
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job.get("error"))
        return r["jobId"], job

    def test_freeze_then_run_pass(self):
        c = self.make_case("甲", "3")
        _, job = self.freeze(case_ids=[c["id"]])
        self.assertTrue(all(i["ok"] for i in job["items"]))
        bl = self.mgr.list_baselines()[0]
        exp = bl["expected"][c["id"]]["bis"]["3"]
        self.assertGreater(len(exp), 0)  # 正弦数据必须能成笔，基线才有回归意义
        # 跑基线：逻辑未变 → 全部通过，lastRun 摘要落盘
        self.mgr.start_run([])
        job2 = self.wait_job()
        self.assertEqual(job2["state"], "done")
        self.assertTrue(all(i["ok"] for i in job2["items"]))
        bl = self.mgr.list_baselines()[0]
        self.assertEqual(bl["lastRun"]["pass"], 1)
        self.assertEqual(bl["lastRun"]["total"], 1)
        self.assertTrue(bl["lastRun"]["cases"][c["id"]])

    def test_baseline_bis_detail(self):
        c = self.make_case("明细", "3")
        _, job = self.freeze(case_ids=[c["id"]])
        d = self.mgr.baseline_bis(job["baselineId"])
        self.assertEqual(d["name"], "基线甲")
        self.assertEqual(len(d["cases"]), 1)
        det = d["cases"][0]
        self.assertEqual(det["caseId"], c["id"])
        self.assertEqual(det["res"], "3")
        self.assertIn("frozenAt", det)
        bis = det["bis"]["3"]
        self.assertGreater(len(bis), 0)
        self.assertEqual(len(bis), job["items"][0]["details"]["3"]["nActual"])
        self.assertIn("3", det["marks"])  # 买卖点同步冻结（可能为空列表）
        with self.assertRaisesRegex(ValueError, "基线不存在"):
            self.mgr.baseline_bis("b_nope")

    def test_marks_frozen_compared_and_legacy_skip(self):
        c = self.make_case("带点", "3")
        self.assertIn("pointsCfg", c)  # 用例快照买卖点参数
        self.freeze(name="点基线", case_ids=[c["id"]])
        bl_path = Path(self.tmp.name) / "eval" / "baselines"
        bl_file = next(bl_path.glob("b_*.json"))
        bl = json.loads(bl_file.read_text(encoding="utf-8"))
        exp = bl["expected"][c["id"]]
        self.assertIn("marks", exp)
        # 篡改：向期望注入一个假买卖点 → 运行必须报差异（差异点清单命中）
        exp["marks"]["3"] = [{"label": "1买", "time": 1, "price": 1.0,
                              "rawTime": 1234567890, "rawPrice": 1.0, "color": "#F23645"}]
        bl_file.write_text(json.dumps(bl, ensure_ascii=False), encoding="utf-8")
        res = self.mgr.run_compare(bl["id"])
        det = res[c["id"]]["details"]["3"]
        self.assertFalse(det["ok"])
        m = det["marks"]
        self.assertFalse(m["ok"])
        self.assertTrue(m["diffs"])  # 假点与真实重算必然逐字段冲突或单侧缺失
        # 旧基线兼容：删掉 marks 键 → 只比笔，应通过且 marks 为 None
        del bl["expected"][c["id"]]["marks"]
        bl_file.write_text(json.dumps(bl, ensure_ascii=False), encoding="utf-8")
        res2 = self.mgr.run_compare(bl["id"])
        det2 = res2[c["id"]]["details"]["3"]
        self.assertTrue(det2["ok"])
        self.assertIsNone(det2["marks"])

    def test_points_cfg_drift(self):
        c = self.make_case("漂移", "3")
        self.freeze(name="漂移基线", case_ids=[c["id"]])
        case_file = Path(self.tmp.name) / "eval" / "cases" / f"{c['id']}.json"
        case = json.loads(case_file.read_text(encoding="utf-8"))
        case["pointsCfg"] = {"nearAtrRatio": 0.3, "keep": 1, "class2ZsTol": 0.0, "thirdZsTol": 0.0}
        case_file.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
        res = self.mgr.run_compare(next(iter(json.loads(p.read_text(encoding='utf-8'))["id"]
                                              for p in (Path(self.tmp.name) / 'eval' / 'baselines').glob('b_*.json'))))
        self.assertTrue(res[c["id"]]["drift"])

    def test_run_detects_expected_tamper(self):
        c = self.make_case("甲", "3")
        self.freeze(case_ids=[c["id"]])
        bl_path = Path(self.tmp.name) / "eval" / "baselines"
        bl_file = next(bl_path.glob("b_*.json"))
        bl = json.loads(bl_file.read_text(encoding="utf-8"))
        bis = bl["expected"][c["id"]]["bis"]["3"]
        bis[0]["endTime"] = bis[0]["endTime"] + 180   # 端点时间改动
        bis[0]["endPrice"] = bis[0]["endPrice"] + 5.0
        bis[1]["macdCross"] = not bis[1]["macdCross"]  # 标记字段改动
        bl_file.write_text(json.dumps(bl, ensure_ascii=False), encoding="utf-8")
        res = self.mgr.run_compare(bl["id"])
        det = res[c["id"]]["details"]["3"]
        self.assertFalse(det["ok"])
        self.assertEqual(det["firstDiff"], 0)
        fields = {d["field"] for d in det["diffs"]}
        self.assertIn("endTime", fields)
        self.assertIn("endPrice", fields)
        self.assertIn("macdCross", fields)
        self.assertTrue(det["context"] and det["context"]["rows"])
        # 异步任务路径同样报失败
        self.mgr.start_run([])
        job = self.wait_job()
        self.assertFalse(job["items"][0]["ok"])
        bl2 = self.mgr.list_baselines()[0]
        self.assertEqual(bl2["lastRun"]["pass"], 0)

    def test_run_detects_missing_bi(self):
        c = self.make_case("甲", "3")
        self.freeze(case_ids=[c["id"]])
        bl_file = next((Path(self.tmp.name) / "eval" / "baselines").glob("b_*.json"))
        bl = json.loads(bl_file.read_text(encoding="utf-8"))
        bl["expected"][c["id"]]["bis"]["3"] = bl["expected"][c["id"]]["bis"]["3"][:-1]
        bl_file.write_text(json.dumps(bl, ensure_ascii=False), encoding="utf-8")
        det = self.mgr.run_compare(bl["id"])[c["id"]]["details"]["3"]
        self.assertFalse(det["ok"])
        self.assertEqual(det["nActual"], det["nExpected"] + 1)
        self.assertIn("(笔)", {d["field"] for d in det["diffs"]})

    def test_cfg_drift_and_restore(self):
        c = self.make_case("甲", "3")
        self.freeze(case_ids=[c["id"]])
        # 篡改用例参数快照 → 运行时按用例口径应用并标记漂移，结束后恢复原参数
        case_file = Path(self.tmp.name) / "eval" / "cases" / f"{c['id']}.json"
        case = json.loads(case_file.read_text(encoding="utf-8"))
        case["chanCfg"] = _flip_cfg(case["chanCfg"])
        case_file.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
        res = self.mgr.run_compare(self.mgr.list_baselines()[0]["id"])
        self.assertTrue(res[c["id"]]["drift"])
        self.assertEqual(dict(chan_core.CHAN_CFG), self.saved_cfg)

    def test_cfg_restored_after_normal_run(self):
        c = self.make_case("甲", "3")
        self.freeze(case_ids=[c["id"]])
        self.mgr.run_compare(self.mgr.list_baselines()[0]["id"])
        self.assertEqual(dict(chan_core.CHAN_CFG), self.saved_cfg)

    def test_refresh_freeze_updates_expected(self):
        c = self.make_case("甲", "3")
        self.freeze(case_ids=[c["id"]])
        bl = self.mgr.list_baselines()[0]
        old_bis = bl["expected"][c["id"]]["bis"]["3"]
        # 更新期望（重冻）：expected 被覆盖，id/caseIds 不变
        self.mgr.start_freeze({"id": bl["id"]})
        job = self.wait_job()
        self.assertEqual(job["state"], "done")
        bl2 = self.mgr.list_baselines()[0]
        self.assertEqual(bl2["id"], bl["id"])
        self.assertEqual(bl2["caseIds"], [c["id"]])
        self.assertEqual(bl2["expected"][c["id"]]["bis"]["3"], old_bis)  # 逻辑未变 → 期望不变

    def test_delete_case_blocked_by_baseline(self):
        c = self.make_case("甲", "3")
        self.freeze(case_ids=[c["id"]])
        with self.assertRaisesRegex(ValueError, "基线"):
            self.mgr.delete_case(c["id"])
        bl = self.mgr.list_baselines()[0]
        self.mgr.delete_baseline(bl["id"])
        self.mgr.delete_case(c["id"])  # 不再报错
        self.assertEqual(self.mgr.list_cases(), [])

    def test_busy_conflict(self):
        c = self.make_case("甲", "3")
        self.freeze(case_ids=[c["id"]])
        # 直接占住任务槽：同一时刻只允许一个评估任务
        with self.mgr._job_lock:
            with self.mgr._state_lock:
                self.mgr._job = {"state": "running", "items": []}
        from py_chain.eval_service import BusyError
        with self.assertRaises(BusyError):
            self.mgr.start_run([])
        with self.assertRaises(BusyError):
            self.mgr.start_freeze({"name": "x", "caseIds": [c["id"]]})
        with self.mgr._state_lock:
            self.mgr._job = None


class CompareBisUnitTests(unittest.TestCase):
    @staticmethod
    def bi(t, s=100, e=200, **kw):
        return {"type": "up", "startIdx": s, "endIdx": e, "startTime": t,
                "endTime": t + 100, "startPrice": 10.0, "endPrice": 12.0,
                "rawCount": 5, "span": 2.0, "gapLocked": False, "macdCross": False, **kw}

    def test_equal_ok(self):
        a = [self.bi(1), self.bi(101)]
        self.assertTrue(compare_bis(a, [self.bi(1), self.bi(101)])["ok"])

    def test_field_diff(self):
        d = compare_bis([self.bi(1)], [self.bi(1, endPrice=13.0)])
        self.assertFalse(d["ok"])
        self.assertEqual(d["firstDiff"], 0)
        self.assertEqual(d["diffs"][0]["field"], "endPrice")
        self.assertEqual(d["diffs"][0]["expected"], 10.0 + 2)
        self.assertEqual(d["diffs"][0]["actual"], 13.0)

    def test_len_mismatch_marks_whole_bi(self):
        d = compare_bis([self.bi(1)], [])
        self.assertFalse(d["ok"])
        self.assertEqual(d["diffs"][0]["field"], "(笔)")

    def test_context_window_bounded(self):
        a = [self.bi(i * 100) for i in range(60)]
        b = list(a)
        b[30] = self.bi(30 * 100, endPrice=99.0)
        d = compare_bis(a, b)
        self.assertLessEqual(len(d["context"]["rows"]), 11)

    def test_align_bis_marks_singles(self):
        a = [self.bi(1), self.bi(101), self.bi(201)]
        b = [self.bi(1), self.bi(201)]
        rows = align_bis(a, b)
        singles = [r for r in rows if ("a" in r) != ("b" in r)]
        self.assertEqual(len(singles), 1)
        self.assertEqual(singles[0]["idxA"], 1)


if __name__ == "__main__":
    unittest.main()
