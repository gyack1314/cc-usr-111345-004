"""计划执行器：执行时复验来源绑定与内容摘要，读取缓存块并与基表补偿合并。

安全要点：
- 每个缓存块在“读取前”重新验证摘要、来源绑定、分区版本与失效标记；
  计划生成后被失效的缓存直接拒绝，绝不返回旧结果。
- 缓存块若带残差谓词，按 SQL 3VL 重新过滤物化分组（过滤列已证明被保留）。
- 基表补偿谓词为 P_query IS NOT TRUE 形式，NULL 行不会被错误丢弃。
- 块间状态只做可合并聚合状态的合并；AVG 在最终结果处由 sum/非空 count 重建。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .aggregates import AggState, aggregate_rows, empty_input_value
from .ast_nodes import QuerySpec
from .catalog import Catalog, partition_version_proof, verify_entry
from .logic import row_passes
from .planner import CacheBlock, Plan
from .predicate import REJECTED


class ExecutionError(ValueError):
    pass


# 基表读取器：(table, partitions, predicate) -> 通过 3VL 过滤的事实行
FactReader = Callable[[str, tuple, Any], list[dict[str, Any]]]


@dataclass
class ExecutionStats:
    fact_rows_read: int = 0
    fact_rows_passed: int = 0
    cache_blocks_used: int = 0
    cache_groups_read: int = 0
    rejections: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    rows: list[dict[str, Any]]
    stats: ExecutionStats

    def as_bag(self) -> list[tuple[tuple[Any, ...], tuple[Any, ...]]]:
        """无序结果袋：(分组键, 聚合值元组)，供独立参照引擎逐事实核对。"""
        out = []
        for r in self.rows:
            out.append((tuple(r['group']), tuple(r['values'])))
        return sorted(out, key=lambda x: repr(x))


class Executor:
    def __init__(self, catalog: Catalog, fact_reader: FactReader):
        self.catalog = catalog
        self.read_facts = fact_reader

    def execute(self, plan: Plan, query: QuerySpec) -> ExecutionResult:
        stats = ExecutionStats()
        merged: dict[tuple[Any, ...], AggState] = {}
        # 状态超集：查询所需 ∪ 所有被选缓存块登记的状态。
        # 基表补偿必须产出与缓存块相同口径的状态，合并才完整（例如部分分区
        # 用缓存的身份集合回答 COUNT DISTINCT，其余分区仍需基表收集身份）。
        state_names = self._all_state_names(query, plan)

        # 1) 缓存块：读取前复验（静态绑定 + 逐分区版本绑定）
        for block in plan.cache_blocks:
            entry = self.catalog.entries.get(block.cache_id)
            if entry is None:
                raise ExecutionError(f'缓存 {block.cache_id} 已不存在')
            proof = verify_entry(entry, self.catalog, query)
            if proof.verdict == REJECTED:
                raise ExecutionError(f'执行期复验失败: {proof.reason}')
            for p in block.partitions:
                vp = partition_version_proof(entry, self.catalog, p)
                if vp.verdict == REJECTED:
                    raise ExecutionError(f'执行期分区绑定失败: {vp.reason}')
            payload = entry.payload
            if payload is None:
                raise ExecutionError(f'缓存 {block.cache_id} 没有物化内容')
            for gkey, state in self._read_block(entry, block, state_names):
                rolled = self._rollup_key(gkey, entry.groups, query.groups)
                merged[rolled] = merged.get(rolled, AggState()).merge(state)
                stats.cache_groups_read += 1
            stats.cache_blocks_used += 1

        # 2) 基表补偿扫描
        for scan in plan.base_scans:
            rows = self.read_facts(query.table, scan.partitions, scan.predicate)
            stats.fact_rows_read += len(rows)
            passed = [r for r in rows if row_passes(scan.predicate, r)]
            stats.fact_rows_passed += len(passed)
            scanned = aggregate_rows(passed, query.groups, state_names)
            for key, st in scanned.items():
                merged[key] = merged.get(key, AggState()).merge(st)

        # 3) 最终化（含全局聚合空输入语义）
        result_rows = self._finalize(merged, query)
        return ExecutionResult(result_rows, stats)

    def _all_state_names(self, query: QuerySpec, plan: Plan) -> frozenset[str]:
        from .aggregates import required_states
        names: set[str] = set()
        for call in query.aggs:
            names.update(required_states(call))
        for block in plan.cache_blocks:
            names.update(self.catalog.entries[block.cache_id].state_names)
        return frozenset(names)

    def _read_block(self, entry, block: CacheBlock,
                    state_names: frozenset[str]):
        """读取缓存物化分组（严格限定在块声明分区内），应用残差谓词，
        并只保留查询/合并所需状态。"""
        for p in block.partitions:
            part_payload = entry.payload.get(p.key())
            if part_payload is None:
                raise ExecutionError(
                    f'缓存 {block.cache_id} 缺少分区 {p.key()} 的物化内容（来源绑定不完整）')
            for gkey, state in part_payload.items():
                gkey = tuple(gkey)
                if block.residual_predicate is not None:
                    row_like = dict(zip(entry.groups, gkey))
                    if not row_passes(block.residual_predicate, row_like):
                        continue
                yield gkey, self._project_state(state, state_names)

    def _project_state(self, state: AggState,
                       state_names: frozenset[str]) -> AggState:
        out = AggState(rows=state.rows)
        from .aggregates import state_count_star
        for col, v in state.nonnull.items():
            if f'nonnull_{col}' in state_names:
                out.nonnull[col] = v
        if state_count_star() in state_names:
            out.rows = state.rows
        else:
            out.rows = 0
        for col, v in state.sums.items():
            if f'sum_{col}' in state_names:
                out.sums[col] = v
        for col, v in state.mins.items():
            if f'min_{col}' in state_names:
                out.mins[col] = v
        for col, v in state.maxs.items():
            if f'max_{col}' in state_names:
                out.maxs[col] = v
        for col, v in state.distinct.items():
            if f'distinct_ids_{col}' in state_names:
                out.distinct[col] = set(v)
        return out

    def _rollup_key(self, gkey: tuple[Any, ...], cache_groups: tuple[str, ...],
                    query_groups: tuple[str, ...]) -> tuple[Any, ...]:
        if cache_groups == query_groups:
            return gkey
        idx = [cache_groups.index(g) for g in query_groups]
        return tuple(gkey[i] for i in idx)

    def _finalize(self, merged: dict[tuple[Any, ...], AggState],
                  query: QuerySpec) -> list[dict[str, Any]]:
        if not query.groups:
            # 全局聚合：空输入也输出一行（COUNT 系列=0，其余 NULL）
            st = merged.get((), AggState())
            if st.rows == 0:
                values = tuple(empty_input_value(call) for call in query.aggs)
            else:
                values = tuple(st.finalize(call) for call in query.aggs)
            return [{'group': [], 'values': list(values)}]
        out: list[dict[str, Any]] = []
        for key in sorted(merged, key=lambda x: repr(x)):
            st = merged[key]
            out.append({
                'group': list(key),
                'values': [st.finalize(call) for call in query.aggs],
            })
        return out
