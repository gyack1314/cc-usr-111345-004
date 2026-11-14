"""独立参照引擎：逐事实扫描基表重算查询，用于核对规划器结果袋。

实现刻意独立于 planner/executor 的复用路径：只依赖 3VL 求值与聚合状态，
直接对完整事实表按查询过滤后分组计算。小资料上必须与复用计划结果逐组相等，
否则复用失败。
"""
from __future__ import annotations

from typing import Any

from .aggregates import AggState, aggregate_rows, empty_input_value, required_states
from .ast_nodes import QuerySpec
from .logic import row_passes


class ReferenceEngine:
    def __init__(self, facts_by_partition: dict[tuple[int, int], list[dict[str, Any]]]):
        self._facts = facts_by_partition

    def all_rows_in(self, table: str, partitions, predicate) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in partitions:
            out.extend(self._facts.get(p.key(), []))
        return out

    def run(self, query: QuerySpec) -> list[dict[str, Any]]:
        rows = []
        for part_rows in self._facts.values():
            rows.extend(part_rows)
        passed = [r for r in rows
                  if query.from_day <= r['day'] < query.to_day
                  and row_passes(query.predicate, r)]
        state_names = frozenset().union(
            *(set(required_states(a)) for a in query.aggs))
        groups = aggregate_rows(passed, query.groups, state_names)

        if not query.groups:
            st = groups.get((), AggState())
            if st.rows == 0:
                values = [empty_input_value(a) for a in query.aggs]
            else:
                values = [st.finalize(a) for a in query.aggs]
            return [{'group': [], 'values': values}]

        out = []
        for key in sorted(groups, key=lambda x: repr(x)):
            st = groups[key]
            out.append({
                'group': list(key),
                'values': [st.finalize(a) for a in query.aggs],
            })
        return out

    def run_bag(self, query: QuerySpec):
        return sorted(
            ((tuple(r['group']), tuple(r['values'])) for r in self.run(query)),
            key=lambda x: repr(x),
        )
