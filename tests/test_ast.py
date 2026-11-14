"""受限 AST 登记与往返（序列化/反序列化/拒绝未登记结构）测试。"""
import unittest

from reuse_data.planner.ast_nodes import (
    AggCall, AstError, Compare, Column, Literal, QuerySpec, from_dict,
    to_dict, validate,
)


class AstRegistrationTests(unittest.TestCase):
    def test_roundtrip(self):
        q = QuerySpec(
            't',
            Compare('>', Column('day'), Literal(1)),
            ('tenant',),
            (AggCall('avg', 'cost', False, 'a'),), 0, 10)
        again = from_dict(to_dict(q))
        self.assertEqual(to_dict(again), to_dict(q))

    def test_unknown_node_rejected(self):
        with self.assertRaises(AstError):
            from_dict({'kind': 'window_function', 'name': 'row_number'})

    def test_unsupported_aggregate_rejected(self):
        with self.assertRaises(AstError):
            from_dict({'kind': 'agg', 'func': 'median', 'col': 'cost',
                       'distinct': False, 'alias': None})

    def test_bad_date_interval_rejected(self):
        base = {'kind': 'query', 'table': 't', 'predicate': None,
                'groups': [], 'aggs': [to_dict(AggCall('count', None))]}
        bad1 = dict(base, from_day=5, to_day=5)
        bad2 = dict(base, from_day=9, to_day=3)
        with self.assertRaises(AstError):
            from_dict(bad1)
        with self.assertRaises(AstError):
            from_dict(bad2)

    def test_compare_must_reference_column(self):
        node = Compare('=', Literal(1), Literal(2))
        with self.assertRaises(AstError):
            validate(node)
        with self.assertRaises(AstError):
            from_dict(to_dict(node))

    def test_distinct_star_rejected(self):
        with self.assertRaises(AstError):
            from_dict({'kind': 'agg', 'func': 'count', 'col': None,
                       'distinct': True, 'alias': None})

    def test_duplicate_group_keys_rejected(self):
        with self.assertRaises(AstError):
            from_dict({'kind': 'query', 'table': 't', 'predicate': None,
                       'groups': ['a', 'a'],
                       'aggs': [to_dict(AggCall('count', None))],
                       'from_day': 0, 'to_day': 2})


if __name__ == '__main__':
    unittest.main()
