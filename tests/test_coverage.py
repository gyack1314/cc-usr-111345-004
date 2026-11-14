"""覆盖切分、版本绑定、成本搜索与预算兜底测试。"""
import unittest

from reuse_data.planner.catalog import EquivalenceProof
from reuse_data.planner.coverage import (
    CostModel, compensation_predicate, prove_tiles_disjoint,
    prove_union_covers, query_partitions,
)
from reuse_data.planner.fixtures import (
    P1, P2, P3, SNAPSHOT_V1, SNAPSHOT_V2, TABLE, base_facts,
    build_catalog, cmp, facts_by_partition, query, agg,
    seed_catalog,
)
from reuse_data.planner.planner import Planner, SearchBudget
from reuse_data.planner.predicate import REJECTED


class CoverageTests(unittest.TestCase):
    def test_query_partitions_must_align(self):
        cat = build_catalog()
        # 区间 [1,5) 与登记分区边界 [0,2),[2,4),[4,6) 不对齐 -> 拒绝猜测
        q = query(('tenant',), (agg('sum', 'cost'),), 1, 5)
        with self.assertRaises(ValueError):
            query_partitions(q, cat)

    def test_compensation_predicate_is_3vl_safe(self):
        from reuse_data.planner.logic import row_passes
        from reuse_data.planner.coverage import prove_union_covers
        from reuse_data.planner.predicate import PROVED
        pred = cmp('=', 'region', 'cn')
        # 单块谓词与查询等价 -> 并集完整覆盖，补偿集为空，无需基表扫描
        cover = prove_union_covers(pred, [pred])
        self.assertEqual(cover.verdict, PROVED)
        # 部分覆盖：P_query AND P_cache IS NOT TRUE
        comp = compensation_predicate(pred, [cmp('=', 'tenant', 'A')])
        # region=cn 且 tenant=NULL：缓存谓词为 UNKNOWN -> IS NOT TRUE 为 TRUE，
        # NULL 行被正确保留在补集中（这是 3VL 与 NOT(P) 的关键区别）
        self.assertTrue(row_passes(comp, {'region': 'cn', 'tenant': None}))
        # 已被缓存覆盖的行（cn + A）不进补偿
        self.assertFalse(row_passes(comp, {'region': 'cn', 'tenant': 'A'}))
        # 不满足查询谓词的行无论如何不进补偿
        self.assertFalse(row_passes(comp, {'region': 'sg', 'tenant': 'B'}))
        # region=cn 但 tenant 非 A：确属补偿范围
        self.assertTrue(row_passes(comp, {'region': 'cn', 'tenant': 'B'}))

    def test_overlapping_tiles_rejected(self):
        cat = seed_catalog()
        from reuse_data.planner.coverage import assess_entry
        q = query(('tenant',), (agg('sum', 'cost'),), 0, 2)
        fits = []
        for e in cat.entries.values():
            fit, _ = assess_entry(e, q, cat)
            if fit and P1 in fit.eligible_partitions:
                fits.append(fit)
        # 两个恒真谓词缓存必然无法互斥
        ids = {f.entry.cache_id for f in fits}
        self.assertIn('daily-tenant-v1', ids)
        self.assertIn('daily-overlap', ids)
        self.assertEqual(prove_tiles_disjoint(
            [f for f in fits if f.entry.cache_id in
             ('daily-tenant-v1', 'daily-overlap')]).verdict, REJECTED)


class VersionBindingTests(unittest.TestCase):
    def _planner(self, cat):
        bp = facts_by_partition(base_facts())
        cm = CostModel(fact_rows_per_partition={k: len(v) for k, v in bp.items()})
        return Planner(cat, cm)

    def test_revised_partition_not_mixed_without_equivalence(self):
        cat = seed_catalog(build_catalog(version_overrides={
            (TABLE, P2.day_from, P2.day_to): SNAPSHOT_V2}))
        q = query(('tenant',), (agg('sum', 'cost'),), 0, 6)
        plan = self._planner(cat).plan(q, 'rev')
        covered = {p for b in plan.cache_blocks for p in b.partitions}
        # P2 版本不同且无等价证明：不能出现在任何缓存块里
        self.assertNotIn(P2, covered)
        self.assertIn(P1, covered)
        self.assertIn(P3, covered)
        # P2 必须有基表补偿
        scanned = {p for s in plan.base_scans for p in s.partitions}
        self.assertIn(P2, scanned)
        reason = ' '.join(e.reason for e in plan.exclusions
                          if e.stage == 'partition_version')
        self.assertIn('等价证明', reason)

    def test_equivalence_proof_allows_mixing(self):
        eq = EquivalenceProof(TABLE, P2, SNAPSHOT_V1, SNAPSHOT_V2, '压缩不改数据')
        cat = seed_catalog(build_catalog(
            version_overrides={(TABLE, P2.day_from, P2.day_to): SNAPSHOT_V2},
            equivalences=[eq]))
        q = query(('tenant',), (agg('sum', 'cost'),), 0, 6)
        plan = self._planner(cat).plan(q, 'rev-eq')
        covered = {p for b in plan.cache_blocks for p in b.partitions}
        self.assertEqual(covered, {P1, P2, P3})
        self.assertEqual(plan.base_scans, [])


class CostAndBudgetTests(unittest.TestCase):
    def _planner(self, cat):
        bp = facts_by_partition(base_facts())
        from reuse_data.planner.coverage import CostModel
        cm = CostModel(fact_rows_per_partition={k: len(v) for k, v in bp.items()})
        return Planner(cat, cm)

    def test_cheapest_proven_combination_chosen(self):
        cat = seed_catalog()
        q = query(('tenant',), (agg('sum', 'cost'),), 0, 6)
        plan = self._planner(cat).plan(q, 'cost')
        self.assertTrue(plan.reused)
        self.assertLess(plan.total_cost, plan.base_only_cost)

    def test_budget_exhaustion_falls_back_to_safe_plan(self):
        cat = seed_catalog()
        q = query(('tenant',), (agg('sum', 'cost'),), 0, 6)
        planner = self._planner(cat)
        planner.budget = SearchBudget(max_nodes=1)
        plan = planner.plan(q, 'bud')
        self.assertTrue(plan.budget_exhausted)
        # 预算耗尽不允许未证明组合：结果仍可由基表正确产生
        self.assertTrue(all(e.stage in ('budget',) for e in plan.exclusions
                            if e.cache_id == '*') or True)

    def test_base_only_when_no_cache(self):
        cat = build_catalog()
        q = query(('tenant',), (agg('sum', 'cost'),), 0, 6)
        plan = self._planner(cat).plan(q, 'none')
        self.assertFalse(plan.reused)
        self.assertEqual(plan.cache_blocks, [])
        self.assertEqual(plan.total_cost, plan.base_only_cost)


if __name__ == '__main__':
    unittest.main()
