"""三值逻辑与谓词包含证明测试。"""
import unittest

from reuse_data.planner.ast_nodes import (
    And, BooleanVal, Column, Compare, IsNotNull, IsNotTrue, IsNull,
    Literal, Not, Or,
)
from reuse_data.planner.logic import FALSE, TRUE, UNKNOWN, eval_predicate, to_dnf
from reuse_data.planner.predicate import (
    PROVED, REJECTED, UNKNOWN_VERDICT, check_filter_columns,
    complement_true, is_contradiction, prove_disjoint, prove_implies,
)


def c(name):
    return Column(name)


def l(v):
    return Literal(v)


def cmp(op, col, v):
    return Compare(op, Column(col), Literal(v))


class ThreeValuedLogicTests(unittest.TestCase):
    def test_null_comparison_is_unknown(self):
        row = {'x': None}
        self.assertIs(eval_predicate(cmp('=', 'x', 1), row), UNKNOWN)
        self.assertIs(eval_predicate(cmp('>', 'x', 1), row), UNKNOWN)
        self.assertIs(eval_predicate(IsNull(c('x')), row), TRUE)
        self.assertIs(eval_predicate(IsNotNull(c('x')), row), FALSE)

    def test_is_not_true_captures_unknown(self):
        # P IS NOT TRUE：False 与 UNKNOWN 都为 True
        self.assertIs(eval_predicate(IsNotTrue(cmp('=', 'x', 1)), {'x': None}), TRUE)
        self.assertIs(eval_predicate(IsNotTrue(cmp('=', 'x', 1)), {'x': 2}), TRUE)
        self.assertIs(eval_predicate(IsNotTrue(cmp('=', 'x', 1)), {'x': 1}), FALSE)

    def test_and_or_truth_tables(self):
        from reuse_data.planner.logic import and3, or3, not3
        self.assertIs(and3(TRUE, UNKNOWN), UNKNOWN)
        self.assertIs(and3(FALSE, UNKNOWN), FALSE)
        self.assertIs(or3(FALSE, UNKNOWN), UNKNOWN)
        self.assertIs(or3(TRUE, UNKNOWN), TRUE)
        self.assertIs(not3(UNKNOWN), UNKNOWN)


class DnfTests(unittest.TestCase):
    def test_distributive_normal_form(self):
        p = Or((And((cmp('=', 'a', 1), cmp('=', 'b', 2))), cmp('=', 'a', 3)))
        dnf = to_dnf(p)
        self.assertEqual(len(dnf), 2)

    def test_not_pushdown(self):
        p = Not(cmp('>', 'x', 5))
        dnf = to_dnf(p)
        (lit,) = dnf[0]
        self.assertIsInstance(lit, Compare)
        self.assertEqual(lit.op, '<=')

    def test_contradiction_and_tautology(self):
        self.assertEqual(to_dnf(BooleanVal(False)), [])
        self.assertEqual(to_dnf(BooleanVal(True)), [()])
        self.assertTrue(is_contradiction(BooleanVal(False)))


class ImplicationTests(unittest.TestCase):
    def test_range_containment_proved(self):
        # 缓存区间更宽 [1,5)；查询是两端有界且落在其中的更窄区间 [2,4)
        cached = And((cmp('>=', 'day', 1), cmp('<', 'day', 5)))
        query = And((cmp('>=', 'day', 2), cmp('<', 'day', 4)))
        self.assertEqual(prove_implies(cached, query).verdict, PROVED)
        # 半开端点：缓存 day<5 包含查询 day<=4（整数域上界同为 5）
        query2 = And((cmp('>=', 'day', 2), cmp('<=', 'day', 4)))
        self.assertEqual(prove_implies(cached, query2).verdict, PROVED)

    def test_bounded_cache_cannot_prove_unbounded_query(self):
        # 有界缓存 [1,5) 不能回答无界 day>=2：day=10 在查询内但缓存外
        cached = And((cmp('>=', 'day', 1), cmp('<', 'day', 5)))
        query = cmp('>=', 'day', 2)
        self.assertEqual(prove_implies(cached, query).verdict, REJECTED)

    def test_range_not_contained_rejected(self):
        cached = And((cmp('>=', 'day', 2), cmp('<', 'day', 4)))
        query = And((cmp('>=', 'day', 1), cmp('<', 'day', 5)))
        self.assertEqual(prove_implies(cached, query).verdict, REJECTED)

    def test_equality_implies_range(self):
        cached = cmp('=', 'tenant', 'A')
        query = cmp('=', 'tenant', 'A')
        self.assertEqual(prove_implies(cached, query).verdict, PROVED)

    def test_conflicting_equality_rejected(self):
        cached = cmp('=', 'tenant', 'A')
        query = cmp('=', 'tenant', 'B')
        self.assertEqual(prove_implies(cached, query).verdict, REJECTED)

    def test_or_branch_containment(self):
        cached = Or((cmp('=', 'r', 'cn'), cmp('=', 'r', 'sg')))
        query = cmp('=', 'r', 'cn')
        # 整体不蕴涵：sg 分支不满足 cn
        self.assertEqual(prove_implies(cached, query).verdict, REJECTED)
        # 反向：cn => (cn OR sg) 成立
        self.assertEqual(prove_implies(query, cached).verdict, PROVED)

    def test_three_valued_null_does_not_satisfy_filter(self):
        # 缓存仅知道 tenant IS NULL；查询 tenant='A' 必须拒绝（NULL 比较为 UNKNOWN）
        cached = IsNull(c('tenant'))
        query = cmp('=', 'tenant', 'A')
        self.assertEqual(prove_implies(cached, query).verdict, REJECTED)

    def test_is_not_true_complement_keeps_nulls(self):
        pred = cmp('=', 'region', 'cn')
        comp = complement_true(pred)
        self.assertIs(eval_predicate(comp, {'region': None}), TRUE)   # NULL 保留
        self.assertIs(eval_predicate(comp, {'region': 'sg'}), TRUE)
        self.assertIs(eval_predicate(comp, {'region': 'cn'}), FALSE)

    def test_unknown_not_guessed_as_contained(self):
        # IS NOT TRUE 包裹无法完全分析的结构 -> unknown，绝不猜成 proved
        cached = IsNotTrue(cmp('>', 'cost', 10))
        query = cmp('>', 'cost', 5)
        verdict = prove_implies(cached, query).verdict
        self.assertEqual(verdict, UNKNOWN_VERDICT)

    def test_no_string_comparison(self):
        # 同语义但子句顺序不同，证明器按结构分析而非 SQL 字符串
        p1 = And((cmp('>', 'a', 1), cmp('<', 'a', 9)))
        p2 = And((cmp('<', 'a', 9), cmp('>', 'a', 1)))
        # 两者约束完全相同，互相蕴涵（区间交集后等价）
        self.assertEqual(prove_implies(p1, p2).verdict, PROVED)
        self.assertEqual(prove_implies(p2, p1).verdict, PROVED)


class DisjointTests(unittest.TestCase):
    def test_equality_partitions_disjoint(self):
        p1 = cmp('=', 'region', 'cn')
        p2 = cmp('=', 'region', 'us')
        self.assertEqual(prove_disjoint(p1, p2).verdict, PROVED)

    def test_overlapping_ranges_not_disjoint(self):
        p1 = And((cmp('>=', 'day', 0), cmp('<', 'day', 3)))
        p2 = And((cmp('>=', 'day', 2), cmp('<', 'day', 5)))
        self.assertEqual(prove_disjoint(p1, p2).verdict, REJECTED)

    def test_tautology_cannot_be_disjoint(self):
        self.assertEqual(prove_disjoint(None, cmp('=', 'a', 1)).verdict, REJECTED)


class FilterColumnTests(unittest.TestCase):
    def test_filter_column_in_groups_ok(self):
        p = check_filter_columns(cmp('=', 'region', 'cn'),
                                 frozenset({'tenant', 'region'}), None)
        self.assertEqual(p.verdict, PROVED)

    def test_filter_column_proven_constant_ok(self):
        cache_pred = cmp('=', 'region', 'cn')
        p = check_filter_columns(cmp('=', 'region', 'cn'),
                                 frozenset({'tenant'}), cache_pred)
        self.assertEqual(p.verdict, PROVED)

    def test_dropped_filter_column_rejected(self):
        p = check_filter_columns(cmp('=', 'region', 'cn'),
                                 frozenset({'tenant'}), None)
        self.assertEqual(p.verdict, REJECTED)


if __name__ == '__main__':
    unittest.main()
