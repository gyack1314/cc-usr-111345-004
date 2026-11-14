"""端到端场景：每个复用计划都与独立逐事实参照引擎的结果袋一致。

复用成功的判定标准（题面要求）：确实少读基表，且结果不变。
"""
import unittest

from reuse_data.planner.scenarios import all_scenarios


class ScenarioContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = all_scenarios()

    def test_every_scenario_matches_reference_bag(self):
        for r in self.results:
            with self.subTest(scenario=r.name):
                self.assertTrue(
                    r.match,
                    msg=f'{r.name} 复用结果与逐事实参照引擎不一致')

    def test_reuse_scenarios_actually_read_less_base(self):
        by_name = {r.name: r for r in self.results}
        # 这些场景存在可证明的缓存复用，基表事实读取量必须低于纯基表方案
        for name in ('avg-rollup', 'overlap-cache', 'partial-revision',
                     'revision-with-equivalence', 'count-distinct',
                     'equivalent-predicate-cache'):
            r = by_name[name]
            with self.subTest(scenario=name):
                self.assertTrue(r.plan['reused'])
                self.assertLess(r.stats['fact_rows_read'],
                                r.stats['total_fact_rows'])
                self.assertLess(r.plan['total_cost'], r.plan['base_only_cost'])

    def test_equivalent_predicate_cache_needs_no_base_scan(self):
        r = next(x for x in self.results
                 if x.name == 'equivalent-predicate-cache')
        self.assertEqual(r.plan['base_scans'], [])
        self.assertEqual(r.stats['fact_rows_read'], 0)
        self.assertTrue(r.match)

    def test_nonexhaustive_residual_conservatively_keeps_compensation(self):
        # 窄缓存无法证明穷尽查询取值时，规划器不得假设覆盖，必须保留补偿/回退
        r = next(x for x in self.results if x.name == 'residual-filter')
        self.assertTrue(r.match)
        self.assertTrue(
            r.plan['base_scans'] or
            any(e['stage'] == 'cost' for e in r.plan['exclusions']))

    def test_overlap_exclusion_recorded(self):
        r = next(x for x in self.results if x.name == 'overlap-cache')
        stages = {(e['stage'], e['cache_id']) for e in r.plan['exclusions']}
        self.assertTrue(any(stage == 'overlap' for stage, _ in stages))

    def test_security_scenarios_blocked_at_execution(self):
        for name in ('invalidated-after-plan', 'digest-tampered'):
            r = next(x for x in self.results if x.name == name)
            with self.subTest(scenario=name):
                self.assertIn('expected_error', r.extra)

    def test_empty_result_and_global_semantics(self):
        grouped = next(x for x in self.results if x.name == 'empty-result')
        self.assertEqual(grouped.result_rows, [])  # 分组查询空集 -> 零行
        global_empty = next(x for x in self.results if x.name == 'empty-global')
        row = global_empty.result_rows[0]
        self.assertEqual(row['values'][0], 0)       # COUNT(*)=0
        self.assertIsNone(row['values'][1])         # AVG=NULL
        self.assertIsNone(row['values'][2])         # MAX=NULL

    def test_partial_revision_keeps_new_and_old_separated(self):
        r = next(x for x in self.results if x.name == 'partial-revision')
        # 修订分区走基表，未修订分区可用缓存，且结果仍与参照一致
        self.assertTrue(r.match)
        self.assertTrue(any(
            e['stage'] == 'partition_version' and '等价证明' in e['reason']
            for e in r.plan['exclusions']))

    def test_plan_lists_block_destinations_and_compensation(self):
        r = next(x for x in self.results if x.name == 'partial-revision')
        self.assertTrue(r.plan['cache_blocks'])
        self.assertTrue(r.plan['base_scans'])
        for scan in r.plan['base_scans']:
            self.assertIsNotNone(scan['predicate'])  # 补偿谓词显式登记


if __name__ == '__main__':
    unittest.main()
