"""聚合状态、合并语义与可导出性证明。

登记的细粒度状态（可跨不重叠块合并）：
- count_star              COUNT(*)
- nonnull_<col>           COUNT(col)（非空计数）
- sum_<col>               SUM(col)
- min_<col>/max_<col>     MIN/MAX(col)
- distinct_ids_<col>      COUNT(DISTINCT col) 的完整可合并身份集合

关键不变量：
- AVG 只能由 sum_<col> 与 nonnull_<col> 重建，不能对平均值再平均；
- 全 NULL 组：SUM/AVG/MIN/MAX -> NULL，COUNT(col)=0；
- 空输入（无任何行）：COUNT 系列 -> 0，其余 -> NULL，且仍输出一行（全局聚合）；
- COUNT(DISTINCT col) 仅当缓存保留完整可合并身份集合时允许复用，普通计数不可替代。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ast_nodes import AggCall, QuerySpec
from .predicate import PROVED, Proof, proved, rejected


def state_count_star() -> str:
    return 'count_star'


def state_nonnull(col: str) -> str:
    return f'nonnull_{col}'


def state_sum(col: str) -> str:
    return f'sum_{col}'


def state_min(col: str) -> str:
    return f'min_{col}'


def state_max(col: str) -> str:
    return f'max_{col}'


def state_distinct(col: str) -> str:
    return f'distinct_ids_{col}'


def required_states(call: AggCall) -> tuple[str, ...]:
    f, col = call.func, call.col
    if f == 'count':
        if call.distinct:
            return (state_distinct(col),)
        return (state_count_star(),) if col is None else (state_nonnull(col),)
    if f == 'sum':
        return (state_sum(col),)
    if f == 'min':
        return (state_min(col),)
    if f == 'max':
        return (state_max(col),)
    if f == 'avg':
        return (state_sum(col), state_nonnull(col))
    raise ValueError(f'未登记聚合 {f}')


# ---------------------------------------------------------------------------
# 分组级聚合状态
# ---------------------------------------------------------------------------

@dataclass
class AggState:
    rows: int = 0
    nonnull: dict[str, int] = field(default_factory=dict)
    sums: dict[str, Any] = field(default_factory=dict)
    mins: dict[str, Any] = field(default_factory=dict)
    maxs: dict[str, Any] = field(default_factory=dict)
    distinct: dict[str, set[Any]] = field(default_factory=dict)

    def add_row(self, row: dict[str, Any], state_names: frozenset[str]) -> None:
        self.rows += 1
        if state_count_star() in state_names:
            pass  # rows 即 COUNT(*)
        for name in state_names:
            if name == state_count_star():
                continue
            if name.startswith('distinct_ids_'):
                col = name[len('distinct_ids_'):]
                v = row.get(col)
                if v is not None:
                    self.distinct.setdefault(col, set()).add(v)
            elif name.startswith('nonnull_'):
                col = name[len('nonnull_'):]
                if row.get(col) is not None:
                    self.nonnull[col] = self.nonnull.get(col, 0) + 1
            elif name.startswith('sum_'):
                col = name[len('sum_'):]
                v = row.get(col)
                if v is not None:
                    self.sums[col] = self.sums.get(col, 0) + v
            elif name.startswith('min_'):
                col = name[len('min_'):]
                v = row.get(col)
                if v is not None and (col not in self.mins or v < self.mins[col]):
                    self.mins[col] = v
            elif name.startswith('max_'):
                col = name[len('max_'):]
                v = row.get(col)
                if v is not None and (col not in self.maxs or v > self.maxs[col]):
                    self.maxs[col] = v

    def merge(self, other: 'AggState') -> 'AggState':
        out = AggState(rows=self.rows + other.rows)
        for col in set(self.nonnull) | set(other.nonnull):
            out.nonnull[col] = self.nonnull.get(col, 0) + other.nonnull.get(col, 0)
        for col in set(self.sums) | set(other.sums):
            if col in self.sums and col in other.sums:
                out.sums[col] = self.sums[col] + other.sums[col]
            elif col in self.sums:
                out.sums[col] = self.sums[col]
            else:
                out.sums[col] = other.sums[col]
        for col in set(self.mins) | set(other.mins):
            vals = [v for v in (self.mins.get(col), other.mins.get(col)) if v is not None]
            if vals:
                out.mins[col] = min(vals)
        for col in set(self.maxs) | set(other.maxs):
            vals = [v for v in (self.maxs.get(col), other.maxs.get(col)) if v is not None]
            if vals:
                out.maxs[col] = max(vals)
        for col in set(self.distinct) | set(other.distinct):
            out.distinct[col] = set()
            out.distinct[col].update(self.distinct.get(col, set()))
            out.distinct[col].update(other.distinct.get(col, set()))
        return out

    def finalize(self, call: AggCall) -> Any:
        f, col = call.func, call.col
        if f == 'count':
            if call.distinct:
                return len(self.distinct.get(col, set()))
            return self.rows if col is None else self.nonnull.get(col, 0)
        if f == 'sum':
            return self.sums.get(col)  # 缺失即全 NULL -> None
        if f == 'min':
            return self.mins.get(col)
        if f == 'max':
            return self.maxs.get(col)
        if f == 'avg':
            n = self.nonnull.get(col, 0)
            if n == 0:
                return None
            return self.sums.get(col) / n
        raise ValueError(f'未登记聚合 {f}')


def empty_input_value(call: AggCall) -> Any:
    """空输入（零行）的 SQL 语义。"""
    if call.func == 'count':
        return 0
    return None


def aggregate_rows(rows: list[dict[str, Any]], groups: tuple[str, ...],
                   state_names: frozenset[str]) -> dict[tuple[Any, ...], AggState]:
    out: dict[tuple[Any, ...], AggState] = {}
    for row in rows:
        key = tuple(row.get(g) for g in groups)
        st = out.setdefault(key, AggState())
        st.add_row(row, state_names)
    return out


# ---------------------------------------------------------------------------
# 可导出性证明
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AggregateProof:
    proof: Proof
    # 每个查询聚合 -> 所需缓存状态名
    bindings: dict[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, Any]:
        return {'proof': self.proof.to_dict(), 'bindings': self.bindings}


def prove_aggregate_export(call: AggCall, available: frozenset[str]) -> Proof:
    need = required_states(call)
    missing = [s for s in need if s not in available]
    if missing:
        if call.func == 'avg':
            return rejected('avg_components_missing',
                            'AVG 必须从正确的 sum 与非空 count 重建，'
                            f'缓存缺少状态 {missing}；对已有平均值再平均会产生偏差')
        if call.func == 'count' and call.distinct:
            return rejected('distinct_identity_missing',
                            '普通计数不能替代 COUNT DISTINCT；仅当缓存保留完整可合并身份集合'
                            f'（{state_distinct(call.col)}）时允许复用，当前缺失')
        return rejected('state_missing', f'缓存缺少重建 {call.func} 所需状态 {missing}')
    return proved('state_exportable',
                  f'{call.func.upper()} 可由登记状态 {list(need)} 合并/重建')


def prove_grouping_compatible(query_groups: tuple[str, ...],
                              cache_groups: tuple[str, ...]) -> Proof:
    q, c = set(query_groups), set(cache_groups)
    if q <= c:
        extra = sorted(c - q)
        return proved('group_rollup',
                      '缓存分组更细或同级，可按查询分组键向上汇总'
                      + (f'；额外细粒度键 {extra} 在合并时折叠' if extra else ''))
    return rejected('group_too_coarse',
                    f'缓存分组 {sorted(c)} 缺少查询分组键 {sorted(q - c)}，无法还原更细分组')


def prove_aggregates(query: QuerySpec, cache_groups: tuple[str, ...],
                     cache_states: frozenset[str]) -> AggregateProof:
    bindings: dict[str, tuple[str, ...]] = {}
    g = prove_grouping_compatible(query.groups, cache_groups)
    if g.verdict != PROVED:
        return AggregateProof(g, bindings)
    steps: list[Proof] = [g]
    for call in query.aggs:
        p = prove_aggregate_export(call, cache_states)
        steps.append(p)
        if p.verdict != PROVED:
            return AggregateProof(p, bindings)
        bindings[call.alias or f'{call.func}_{call.col or "star"}'] = required_states(call)
    return AggregateProof(
        proved('all_aggregates_exportable', '分组与全部聚合均可从缓存状态导出', *steps),
        bindings,
    )
