"""marks 模块单测：双周期可见性并集、全宽支阻横线、单行标记（替换语义）。"""
import unittest
from unittest.mock import Mock

from . import marks


class IntervalVisibilityTests(unittest.TestCase):
    def test_single_res_matches_legacy_output(self):
        # 回归：单周期输入与旧实现逐字节一致
        self.assertEqual(
            marks._interval_visibility_js('3'),
            '{ seconds: false, minutes: true, minutesFrom: 3, minutesTo: 3, '
            'hours: false, days: false, weeks: false, months: false }')
        self.assertEqual(
            marks._interval_visibility_js('30S'),
            '{ seconds: true, secondsFrom: 30, secondsTo: 30, minutes: false, '
            'hours: false, days: false, weeks: false, months: false }')
        self.assertEqual(
            marks._interval_visibility_js('60'),
            '{ seconds: false, minutes: false, hours: true, hoursFrom: 1, hoursTo: 1, '
            'days: false, weeks: false, months: false }')
        self.assertEqual(
            marks._interval_visibility_js('240'),
            '{ seconds: false, minutes: false, hours: true, hoursFrom: 4, hoursTo: 4, '
            'days: false, weeks: false, months: false }')
        self.assertEqual(
            marks._interval_visibility_js('D'),
            '{ seconds: false, minutes: false, hours: false, days: true, '
            'weeks: false, months: false }')

    def test_none_for_empty_or_unrestrictable(self):
        # 空/周/月线（锚定周期）→ 不限；列表首项（markRes）不限 → 整体不限
        for res in (None, '', 'W', 'M', ['W', '240'], ['M', '3'], 'bad'):
            self.assertIsNone(marks._interval_visibility_js(res), res)

    def test_union_dual_tf(self):
        # 不同类周期（分钟 + 小时）：各自启用
        self.assertEqual(
            marks._interval_visibility_js(['3', '60']),
            '{ seconds: false, minutes: true, minutesFrom: 3, minutesTo: 3, '
            'hours: true, hoursFrom: 1, hoursTo: 1, '
            'days: false, weeks: false, months: false }')
        # 同类周期：连续区间 3..15（中间周期 5m/10m 也会显示）
        self.assertEqual(
            marks._interval_visibility_js(['3', '15']),
            '{ seconds: false, minutes: true, minutesFrom: 3, minutesTo: 15, '
            'hours: false, days: false, weeks: false, months: false }')
        # 秒 + 分钟
        self.assertEqual(
            marks._interval_visibility_js(['30S', '3']),
            '{ seconds: true, secondsFrom: 30, secondsTo: 30, '
            'minutes: true, minutesFrom: 3, minutesTo: 3, '
            'hours: false, days: false, weeks: false, months: false }')
        # 小时区间 1..4（1H/2H/3H/4H）
        self.assertEqual(
            marks._interval_visibility_js(['60', '240']),
            '{ seconds: false, minutes: false, hours: true, hoursFrom: 1, hoursTo: 4, '
            'days: false, weeks: false, months: false }')
        # 小时 + 日线（days 不带范围，与旧 D 输出口径一致）
        self.assertEqual(
            marks._interval_visibility_js(['240', 'D']),
            '{ seconds: false, minutes: false, hours: true, hoursFrom: 4, hoursTo: 4, '
            'days: true, weeks: false, months: false }')
        # 去重：markRes == periodX 时等同单周期
        self.assertEqual(marks._interval_visibility_js(['3', '3']),
                         marks._interval_visibility_js('3'))

    def test_union_falls_back_when_periodX_invalid(self):
        # periodX 缺失/无法识别 → 退化为 markRes 单周期（绝不只限 periodX）
        for period in (None, '', 'bad', 'W'):
            self.assertEqual(marks._interval_visibility_js(['3', period]),
                             marks._interval_visibility_js('3'), repr(period))

    def test_iv_json_object(self):
        self.assertEqual(
            marks._iv_json(['3', '60']),
            {'seconds': False, 'minutes': True, 'minutesFrom': 3, 'minutesTo': 3,
             'hours': True, 'hoursFrom': 1, 'hoursTo': 1,
             'days': False, 'weeks': False, 'months': False})
        self.assertIsNone(marks._iv_json(['W', '3']))


class DrawChunkTests(unittest.TestCase):
    ROW = {'direction': 'long', 'price': 2663.25, 'time': 100,
           'markRes': '3', 'periodX': '60',
           'exits': [{'type': 'stopSr', 'time': 200, 'price': 2650.0}]}

    def expr(self, **kwargs):
        client = Mock()
        client.evaluate.return_value = ['id1', 'id2']
        ids = marks._draw_chunk(client, [self.ROW], marks._colors(None), **kwargs)
        self.assertEqual(ids, ['id1', 'id2'])
        return client.evaluate.call_args.args[0]

    def test_default_bulk_args(self):
        expr = self.expr()
        self.assertIn("text: 'ML·BUY 2663.25'", expr)
        self.assertIn("text: 'ML·止损 2650.00'", expr)
        self.assertIn('mark_list_ids', expr)
        self.assertNotIn('mark_single_ids', expr)
        # 默认（非 dual_tf）只带 markRes 单周期 IV
        self.assertIn('minutesFrom: 3', expr)
        self.assertNotIn('hoursFrom: 1', expr)

    def test_dual_tf_with_single_prefix(self):
        expr = self.expr(prefix=marks.SINGLE_PREFIX, ids_key=marks.SINGLE_IDS_KEY,
                         dual_tf=True)
        self.assertIn("text: 'ML·单BUY 2663.25'", expr)
        self.assertIn("text: 'ML·单止损 2650.00'", expr)
        self.assertIn('mark_single_ids', expr)
        self.assertNotIn('mark_list_ids', expr)
        # 双周期 IV 并集：分钟 3 + 小时 1 同时启用
        self.assertIn('minutesFrom: 3, minutesTo: 3', expr)
        self.assertIn('hoursFrom: 1, hoursTo: 1', expr)


class DrawSrChunkTests(unittest.TestCase):
    ROW = {'time': 100, 'nearSr': 2650.5, 'markRes': '3', 'periodX': '60'}

    def expr(self, chunk=None, **kwargs):
        client = Mock()
        client.evaluate.return_value = {'ids': ['sr1'], 'skipped': 0}
        marks._draw_sr_chunk(client, chunk or [self.ROW], '#787B86', **kwargs)
        return client.evaluate.call_args.args[0]

    def test_full_width_line_with_dual_tf_iv(self):
        expr = self.expr()
        self.assertIn("shape: 'horizontal_line'", expr)
        # 不再是限宽 trend_line 线段（无K线端点/跨度逻辑）
        self.assertNotIn('trend_line', expr)
        self.assertNotIn('m_bars', expr)
        self.assertNotIn('createMultipointShape', expr)
        # 文本与默认键
        self.assertIn("'ML·SR ' + s.price.toFixed(2)", expr)
        self.assertIn('mark_sr_ids', expr)
        # 每行 IV 并集以 JSON 对象随 items 传入（minutes 3 + hours 1）
        self.assertIn('"iv": {"seconds": false, "minutes": true, '
                      '"minutesFrom": 3, "minutesTo": 3', expr)
        self.assertIn('"hours": true, "hoursFrom": 1, "hoursTo": 1', expr)

    def test_single_prefix_and_key(self):
        expr = self.expr(prefix=marks.SINGLE_PREFIX + 'SR', ids_key=marks.SINGLE_IDS_KEY)
        self.assertIn("'ML·单SR ' + s.price.toFixed(2)", expr)
        self.assertIn('mark_single_ids', expr)
        self.assertNotIn('mark_sr_ids', expr)

    def test_iv_missing_when_unrestrictable(self):
        expr = self.expr(chunk=[{'time': 100, 'nearSr': 2650.5, 'markRes': 'W'}])
        self.assertIn('"iv": null', expr)


class DrawSingleMarkTests(unittest.TestCase):
    BASE = {'time': 100, 'price': 2663.25, 'direction': 'long',
            'markRes': '3', 'periodX': '60', 'nearSr': 2650.5,
            'exits': [{'type': 'stopSr', 'time': 200, 'price': 2650.0}]}

    def test_clears_previous_then_draws_arrows_and_sr_line(self):
        client = Mock()
        # 依次：清上次单行标记 → 画箭头 → 画支阻横线 → 清坏锚
        client.evaluate.side_effect = [3, ['a1', 'a2'], {'ids': ['s1'], 'skipped': 0}, 0]
        out = marks.draw_single_mark(client, dict(self.BASE))
        exprs = [call.args[0] for call in client.evaluate.call_args_list]
        self.assertEqual(len(exprs), 4)
        self.assertIn('mark_single_ids', exprs[0])
        self.assertIn("PREFIX = 'ML·单'", exprs[0])
        self.assertIn("text: 'ML·单BUY 2663.25'", exprs[1])
        self.assertIn("shape: 'horizontal_line'", exprs[2])
        self.assertIn("'ML·单SR ' + s.price.toFixed(2)", exprs[2])
        self.assertEqual(out, {'drawn': 3, 'cleared': 3})

    def test_filtered_row_only_clears(self):
        client = Mock()
        client.evaluate.return_value = 2
        out = marks.draw_single_mark(client, {**self.BASE, 'status': '同向过滤'})
        self.assertEqual(out, {'drawn': 0, 'cleared': 2, 'skipped': '同向过滤'})
        self.assertEqual(client.evaluate.call_count, 1)

    def test_missing_price_only_clears(self):
        client = Mock()
        client.evaluate.return_value = 0
        out = marks.draw_single_mark(client, {**self.BASE, 'price': None})
        self.assertEqual(out, {'drawn': 0, 'cleared': 0, 'skipped': '缺价格/方向/时间'})

    def test_no_near_sr_skips_line(self):
        client = Mock()
        client.evaluate.side_effect = [0, ['a1'], 0]
        out = marks.draw_single_mark(client, {**self.BASE, 'nearSr': None})
        self.assertEqual(out, {'drawn': 1, 'cleared': 0})
        self.assertEqual(client.evaluate.call_count, 3)

    def test_purge_false_keeps_shapes_without_broken_check(self):
        """回放态 getPoints() 常为空，不能按空锚清掉刚画的标记。"""
        client = Mock()
        client.evaluate.side_effect = [1, ['a1', 'a2'], {'ids': ['s1'], 'skipped': 0}]
        out = marks.draw_single_mark(client, dict(self.BASE), purge=False)
        self.assertEqual(out, {'drawn': 3, 'cleared': 1})
        self.assertEqual(client.evaluate.call_count, 3)
        exprs = [call.args[0] for call in client.evaluate.call_args_list]
        self.assertFalse(any('getPoints' in e for e in exprs))

    def test_draw_failures_degrade_not_raise(self):
        client = Mock()
        client.evaluate.side_effect = RuntimeError('cdp down')
        out = marks.draw_single_mark(client, dict(self.BASE))
        self.assertEqual(out['drawn'], 0)
        self.assertEqual(out['cleared'], 0)


class PrefixKeyInvariantTests(unittest.TestCase):
    def test_single_mark_covered_by_bulk_clear_paths(self):
        # ML·单 属 ML· 前缀族：「删除标记」与「标记进出场」重画都能清掉它
        self.assertTrue(marks.SINGLE_PREFIX.startswith(marks.MARK_PREFIX))
        self.assertTrue((marks.SINGLE_PREFIX + 'SR').startswith(marks.MARK_PREFIX))
        # 但不与「标记支阻位」的 ML·SR 前缀冲突
        self.assertFalse((marks.SINGLE_PREFIX + 'SR').startswith(marks.SR_PREFIX))
        self.assertIn(marks.SINGLE_IDS_KEY, marks.CLEAR_IDS_KEYS)

    def test_clear_marks_resets_single_key_and_keeps_sr(self):
        client = Mock()
        client.evaluate.return_value = 0
        marks._clear_marks(client)
        expr = client.evaluate.call_args.args[0]
        # 前缀兜底会删 ML·单 → 键置空避免死 id
        self.assertIn("localStorage.setItem('mark_single_ids', '[]')", expr)
        # 「标记进出场」仍不删 ML·SR 横线（两按钮互不清除口径不变）
        self.assertIn("p.startsWith(PREFIX) && !p.startsWith('ML·SR')", expr)

    def test_clear_single_marks_scopes_to_single_prefix(self):
        client = Mock()
        client.evaluate.return_value = 0
        marks._clear_single_marks(client)
        expr = client.evaluate.call_args.args[0]
        self.assertIn("PREFIX = 'ML·单'", expr)
        self.assertIn('mark_single_ids', expr)
        self.assertNotIn('mark_sr_ids', expr)
        self.assertNotIn('mark_list_ids', expr)

    def test_dedup_sr_rows_keyed_by_res_price_periodx(self):
        rows = [
            {'time': 100, 'nearSr': 2650.5, 'markRes': '3', 'periodX': '60'},
            # 同周期同价位同 periodX：全宽线完全重叠 → 只画一条
            {'time': 400, 'nearSr': 2650.5, 'markRes': '3', 'periodX': '60'},
            # 不同检测周期：并集可见性不同 → 各画一条
            {'time': 500, 'nearSr': 2650.5, 'markRes': '3', 'periodX': '15'},
            # 无 nearSr / 无 markRes：跳过
            {'time': 600, 'markRes': '3', 'periodX': '60'},
            {'time': 700, 'nearSr': 2651.0, 'periodX': '60'},
        ]
        out = marks._dedup_sr_rows(rows)
        self.assertEqual([r['time'] for r in out], [100, 500])


if __name__ == '__main__':
    unittest.main()
