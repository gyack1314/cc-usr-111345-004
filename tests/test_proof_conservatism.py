"""证明接口的保守性与边界语义补充测试。"""
import unittest

from reuse_data.planner.ast_nodes import (
    And, BooleanVal, Column, Compare, IsNotTrue, Literal, Or,
)
from reuse_data.planner.predicate import (
    PROVED, REJECTED, UNKNOWN_VERDICT, prove_implies,
)


def cmp(op, col, v):
    return Compare(op, Column(col), Literal(v))


class ConservatismTests(unittest.TestCase):
    def test_unsupported_structure_never_guessed_contained(self):
        # IS NOT TRUE 包裹的复合比较无法完全分析 -> unknown，而不是 proved
        cached = And((cmp('>=', 'day', 0), cmp('<', 'day', 6),
                      IsNotTrue(cmp('>', 'cost', 100))))
        query = And((cmp('>=', 'day', 0), cmp('<', 'day', 6)))
        self.assertEqual(prove_implies(cached, query).verdict, PROVED)
        # 反向：无法证明缓存的 NOT TRUE 约束能推出 cost>5
        tricky = IsNotTrue(cmp('>', 'cost', 100))
        self.assertIn(prove_implies(tricky, cmp('>', 'cost', 50)).verdict,
                      (UNKNOWN_VERDICT, REJECTED))
        self.assertNotEqual(
            prove_implies(tricky, cmp('>', 'cost', 50)).verdict, PROVED)

    def test_and_or_containment(self):
        cached = And((cmp('=', 'tenant', 'A'),
                      Or((cmp('=', 'region', 'cn'),
                          cmp('=', 'region', 'sg')))))
        # 窄分支推出宽 OR 缓存：tenant=A AND region=cn 必然落在缓存内
        q1 = And((cmp('=', 'tenant', 'A'), cmp('=', 'region', 'cn')))
        self.assertEqual(prove_implies(q1, cached).verdict, PROVED)
        # 反向不蕴涵：缓存的 sg 分支不满足 region=cn，是真实反例
        self.assertEqual(prove_implies(cached, q1).verdict, REJECTED)
        # 这种“更宽 OR 缓存”由规划器走残差过滤复用（见 scenarios.residual-filter），
        # 而不是靠谓词包含直接替代扫描
        # 仅 region=cn 缺少 tenant=A 约束，不能推出完整缓存谓词
        q2 = cmp('=', 'region', 'cn')
        self.assertNotEqual(prove_implies(q2, cached).verdict, PROVED)

    def test_half_open_day_endpoint(self):
        # 整数日 [0,2) 包含 day=0,1，不含 day=2
        cached = And((cmp('>=', 'day', 0), cmp('<', 'day', 2)))
        inside = And((cmp('>=', 'day', 0), cmp('<=', 'day', 1)))
        outside = And((cmp('>=', 'day', 0), cmp('<=', 'day', 2)))
        self.assertEqual(prove_implies(cached, inside).verdict, PROVED)
        self.assertEqual(prove_implies(cached, outside).verdict, REJECTED)

    def test_neq_three_valued(self):
        # region <> 'us' 对 region=NULL 为 UNKNOWN，不能当作通过
        cached = cmp('<>', 'region', 'us')
        # 已知 region='cn' 的等值缓存能推出 <> us
        self.assertEqual(prove_implies(cmp('=', 'region', 'cn'),
                                       cmp('<>', 'region', 'us')).verdict, PROVED)
        # 反向不蕴涵：<> us 包含 NULL 行（UNKNOWN），不能推出 = cn
        self.assertEqual(prove_implies(cached,
                                       cmp('=', 'region', 'cn')).verdict, REJECTED)

    def test_boolean_constants(self):
        self.assertEqual(prove_implies(BooleanVal(False),
                                       cmp('=', 'a', 1)).verdict, PROVED)  # 空真空
        self.assertEqual(prove_implies(BooleanVal(True),
                                       cmp('=', 'a', 1)).verdict, REJECTED)

    def test_dnf_never_swallows_unknown_as_proof(self):
        # 一个分支可证、另一个分支不透明：整体最多 unknown，不能升级 proved
        good = cmp('=', 'a', 1)
        opaque = IsNotTrue(cmp('>', 'x', 9))
        cached = Or((good, opaque))
        query = cmp('=', 'a', 1)
        self.assertNotEqual(prove_implies(cached, query).verdict, PROVED)


class FilterOnFinerCacheTests(unittest.TestCase):
    def test_filter_column_must_survive_in_keys_or_constants(self):
        from reuse_data.planner.predicate import check_filter_columns
        # 缓存按 (tenant, day) 分组，查询却要按 region 过滤，且缓存无常量证明
        verdict = check_filter_columns(
            cmp('=', 'region', 'cn'),
            frozenset({'tenant', 'day'}), None).verdict
        self.assertEqual(verdict, REJECTED)
        # region 进了分组键 -> 允许（缓存更细，残差过滤可行）
        self.assertEqual(check_filter_columns(
            cmp('=', 'region', 'cn'),
            frozenset({'tenant', 'day', 'region'}), None).verdict, PROVED)


if __name__ == '__main__':
    unittest.main()
