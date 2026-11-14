"""聚合状态合并、AVG 重建与可导出性证明测试。"""
import unittest

from reuse_data.planner.aggregates import (
    AggState, aggregate_rows, empty_input_value, prove_aggregate_export,
    prove_aggregates, required_states, state_count_star, state_distinct,
    state_nonnull, state_sum,
)
from reuse_data.planner.ast_nodes import AggCall, QuerySpec
from reuse_data.planner.predicate import PROVED, REJECTED


def ac(func, col=None, distinct=False):
    return AggCall(func, col, distinct, f'{func}_{col or "star"}')


class AggStateSemanticsTests(unittest.TestCase):
    def test_avg_rebuilt_from_sum_and_nonnull_count(self):
        # 租户两天：[10,30] 与 [50]。月平均必须是 90/3=30，
        # 绝不是日平均 (20+50)/2=35（这里恰好不同，能抓到再平均错误）
        s1 = AggState()
        for r in ({'cost': 10}, {'cost': 30}):
            s1.add_row(r, frozenset(required_states(ac('avg', 'cost'))))
        s2 = AggState()
        s2.add_row({'cost': 50}, frozenset(required_states(ac('avg', 'cost'))))
        merged = s1.merge(s2)
        self.assertEqual(merged.finalize(ac('avg', 'cost')), 30)

    def test_all_null_group(self):
        # 全 NULL：SUM/AVG -> None，COUNT(col)=0，COUNT(*)=2，MIN/MAX -> None
        rows = [{'cost': None}, {'cost': None}]
        states = frozenset((state_count_star(), state_nonnull('cost'),
                            state_sum('cost'), 'min_cost', 'max_cost'))
        g = aggregate_rows(rows, (), states)[()]
        self.assertEqual(g.finalize(ac('count', None)), 2)
        self.assertEqual(g.finalize(ac('count', 'cost')), 0)
        self.assertIsNone(g.finalize(ac('sum', 'cost')))
        self.assertIsNone(g.finalize(ac('avg', 'cost')))
        self.assertIsNone(g.finalize(ac('min', 'cost')))
        self.assertIsNone(g.finalize(ac('max', 'cost')))

    def test_empty_input_semantics(self):
        # 空输入：COUNT 系列=0，SUM/AVG/MIN/MAX=None
        self.assertEqual(empty_input_value(ac('count', None)), 0)
        self.assertEqual(empty_input_value(ac('count', 'cost')), 0)
        self.assertIsNone(empty_input_value(ac('sum', 'cost')))
        self.assertIsNone(empty_input_value(ac('avg', 'cost')))
        self.assertIsNone(empty_input_value(ac('max', 'cost')))

    def test_merge_disjoint_distinct_identity_sets(self):
        s1 = AggState()
        s1.add_row({'tenant': 'A'}, frozenset((state_distinct('tenant'),)))
        s1.add_row({'tenant': 'A'}, frozenset((state_distinct('tenant'),)))
        s2 = AggState()
        s2.add_row({'tenant': 'B'}, frozenset((state_distinct('tenant'),)))
        merged = s1.merge(s2)
        self.assertEqual(merged.finalize(ac('count', 'tenant', distinct=True)), 2)

    def test_count_star_distinct_from_count_column(self):
        rows = [{'cost': 1}, {'cost': None}, {'cost': 3}]
        g = aggregate_rows(rows, (),
                           frozenset((state_count_star(), state_nonnull('cost'))))[()]
        self.assertEqual(g.finalize(ac('count', None)), 3)
        self.assertEqual(g.finalize(ac('count', 'cost')), 2)


class ExportabilityProofTests(unittest.TestCase):
    def test_avg_requires_both_components(self):
        # 只有 sum 没有 nonnull count -> 拒绝
        p = prove_aggregate_export(ac('avg', 'cost'),
                                   frozenset((state_sum('cost'),)))
        self.assertEqual(p.verdict, REJECTED)
        # 两者齐备 -> 可导出
        p2 = prove_aggregate_export(
            ac('avg', 'cost'),
            frozenset((state_sum('cost'), state_nonnull('cost'))))
        self.assertEqual(p2.verdict, PROVED)

    def test_avg_cannot_reuse_average_value(self):
        # 只有一个 avg 占位状态，没有 sum/count 组件 -> 拒绝再平均
        p = prove_aggregate_export(ac('avg', 'cost'),
                                   frozenset(('avg_cost_placeholder',)))
        self.assertEqual(p.verdict, REJECTED)

    def test_count_distinct_needs_identity_set(self):
        call = ac('count', 'tenant', distinct=True)
        self.assertEqual(prove_aggregate_export(
            call, frozenset((state_count_star(),))).verdict, REJECTED)
        self.assertEqual(prove_aggregate_export(
            call, frozenset((state_distinct('tenant'),))).verdict, PROVED)

    def test_grouping_must_be_at_least_as_fine(self):
        q = QuerySpec('t', None, ('tenant',), (ac('sum', 'cost'),), 0, 2)
        # 缓存更细（tenant,day）-> 可向上汇总
        ok = prove_aggregates(q, ('tenant', 'day'),
                              frozenset((state_sum('cost'),)))
        self.assertEqual(ok.proof.verdict, PROVED)
        # 缓存更粗（只有 day）-> 无法还原 tenant
        bad = prove_aggregates(q, ('day',), frozenset((state_sum('cost'),)))
        self.assertEqual(bad.proof.verdict, REJECTED)


if __name__ == '__main__':
    unittest.main()
