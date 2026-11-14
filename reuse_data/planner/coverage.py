"""覆盖范围证明与切分。

一个缓存条目对查询的资格由四类证明共同决定，缺一不可：
1. 来源/schema/状态版本/分区版本绑定（catalog.verify_entry）；
2. 谓词包含：cached TRUE ⇒ query TRUE（否则缓存含查询外的行，无法剔除）；
3. 过滤列保留：查询过滤列必须在缓存分组键或可证明常量中；
4. 分组与聚合可导出（aggregates.prove_aggregates）。

切分按事实表登记分区进行。同一分区可由一个（或经互斥证明的多个）缓存块
覆盖，剩余行用 3VL 安全补偿谓词 P_query IS NOT TRUE 回基表扫描。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .aggregates import AggregateProof, prove_aggregates
from .ast_nodes import And, BooleanVal, Node, QuerySpec
from .catalog import (
    Catalog, CacheEntry, Partition, partition_version_proof, verify_entry,
)
from .predicate import (
    PROVED, REJECTED, UNKNOWN_VERDICT, Proof, check_filter_columns,
    complement_true, is_proved, prove_disjoint, prove_implies,
    proved, rejected, unknown,
)


@dataclass(frozen=True)
class CacheFit:
    entry: CacheEntry
    eligible_partitions: tuple[Partition, ...]
    pruned_partitions: tuple[Partition, ...]  # 因版本冲突被逐分区剪枝
    prune_reasons: tuple[str, ...]
    implication: Proof
    aggregate: AggregateProof
    binding: Proof
    filter_cols: Proof
    # cached TRUE => query TRUE 为 proved 时，缓存行需要再施加的查询过滤
    residual_predicate: Optional[Node]

    @property
    def eligible(self) -> bool:
        return (is_proved(self.implication) and is_proved(self.aggregate.proof)
                and is_proved(self.binding) and is_proved(self.filter_cols)
                and bool(self.eligible_partitions))


@dataclass
class Exclusion:
    cache_id: str
    stage: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {'cache_id': self.cache_id, 'stage': self.stage, 'reason': self.reason}


def _intersect_partitions(entry: CacheEntry, query: QuerySpec) -> tuple[Partition, ...]:
    out: list[Partition] = []
    for p in entry.partitions:
        # 缓存分区与查询半开区间相交；只有完整落在查询区间内的分区才能整段复用
        if p.day_from >= query.from_day and p.day_to <= query.to_day:
            out.append(p)
    return tuple(out)


def assess_entry(entry: CacheEntry, query: QuerySpec,
                 catalog: Catalog) -> tuple[Optional[CacheFit], Optional[Exclusion]]:
    binding = verify_entry(entry, catalog, query)
    if binding.verdict != PROVED:
        return None, Exclusion(entry.cache_id, 'binding', binding.reason)

    intersected = _intersect_partitions(entry, query)
    if not intersected:
        return None, Exclusion(entry.cache_id, 'coverage',
                               '缓存实际覆盖分区与查询日期区间没有完整落在其内的交集')

    # 分区级版本剪枝：版本冲突/无等价证明的分区从可用集合剔除，
    # 其余同版（或有等价证明）分区仍可复用，冲突分区转基表补偿。
    eligible: list[Partition] = []
    pruned: list[Partition] = []
    prune_reasons: list[str] = []
    for p in intersected:
        vp = partition_version_proof(entry, catalog, p)
        if is_proved(vp):
            eligible.append(p)
        else:
            pruned.append(p)
            prune_reasons.append(f'{p.key()}: {vp.reason}')
    if not eligible:
        return None, Exclusion(entry.cache_id, 'binding',
                               '所有相交分区均未通过版本绑定：' + '; '.join(prune_reasons))

    impl = prove_implies(entry.predicate, query.predicate)
    if impl.verdict == REJECTED:
        return None, Exclusion(entry.cache_id, 'predicate',
                               f'谓词不包含：{impl.reason}')

    fc = check_filter_columns(query.predicate, frozenset(entry.groups), entry.predicate)
    if fc.verdict != PROVED:
        return None, Exclusion(entry.cache_id, 'filter_column', fc.reason)

    agg = prove_aggregates(query, entry.groups, frozenset(entry.state_names))
    if agg.proof.verdict != PROVED:
        return None, Exclusion(entry.cache_id, 'aggregate', agg.proof.reason)

    if impl.verdict == UNKNOWN_VERDICT:
        # 无法证明包含 -> 该缓存不能作为块使用（既不能保证行集合正确，
        # 也无法构造可靠补偿），显式排除而非猜测。
        return None, Exclusion(entry.cache_id, 'predicate',
                               f'谓词包含无法证明（unknown）：{impl.reason}')

    # 缓存谓词等于查询谓词时无需残差；否则缓存更宽，读取后再按查询谓词过滤
    residual = None if _same_predicate(entry.predicate, query.predicate) else query.predicate
    fit = CacheFit(entry, tuple(eligible), tuple(pruned), tuple(prune_reasons),
                   impl, agg, binding, fc, residual)
    return fit, None


def _same_predicate(a: Optional[Node], b: Optional[Node]) -> bool:
    from .ast_nodes import canonical_json
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return canonical_json(a) == canonical_json(b)


def prove_equivalent(p1: Optional[Node], p2: Optional[Node]) -> Proof:
    """3VL 等价：两个方向的 TRUE 保持都被证明。"""
    fwd = prove_implies(p1, p2)
    back = prove_implies(p2, p1)
    if is_proved(fwd) and is_proved(back):
        return proved('predicate_equivalent',
                      '两个方向的谓词包含均被证明，行集合（含 NULL 处理）等价',
                      fwd, back)
    if fwd.verdict == REJECTED or back.verdict == REJECTED:
        return rejected('not_equivalent',
                        f'谓词不等价：{fwd.reason} / {back.reason}')
    return unknown('equivalence_inconclusive',
                   '等价性无法在登记结构内完全证明', fwd, back)


def prove_union_covers(query_pred: Optional[Node],
                       tile_preds: list[Optional[Node]]) -> Proof:
    """互斥块并集完整覆盖查询真集：query TRUE ⇒ OR(tiles) TRUE。

    判定方法（3VL 可靠）：
    1. 每个块都已单独证明 tile TRUE ⇒ query TRUE（调用方在资格评估时保证）；
    2. 本函数证明反向：把查询谓词与“所有块谓词都不为 TRUE”合取，
       若该合取不可满足（矛盾），则任何满足查询的行必然命中至少一个块。
    块已由 prove_tiles_disjoint 证明互斥，因此并集是无重叠的精确切分。
    """
    if not tile_preds:
        return rejected('no_tiles', '没有缓存块，无从覆盖')
    from .ast_nodes import And as AndNode, BooleanVal, Or as OrNode
    from .predicate import prove_disjoint, prove_implies

    # 方向一的常规快速路径：单块谓词与查询等价
    if len(tile_preds) == 1:
        return prove_equivalent(tile_preds[0], query_pred)

    union = OrNode(tuple(p if p is not None else BooleanVal(True)
                         for p in tile_preds))
    direct = prove_implies(query_pred, union)
    if direct.verdict == PROVED:
        return direct

    # 方向二：query 为真但所有块都不为真 是否矛盾。
    # 构造 query AND AND(t_i IS NOT TRUE)，用 DNF 可满足性判定。
    complements = [complement_true(t) for t in tile_preds]
    leftover = AndNode(tuple(
        ([query_pred] if query_pred is not None else []) + complements))
    disj = prove_disjoint(query_pred if query_pred is not None
                          else BooleanVal(True), leftover)
    # query 与“漏网行”互斥，即不存在漏网行 -> 覆盖完整
    if disj.verdict == PROVED:
        return proved('union_exhaustive',
                      '查询为真且所有缓存块均不为真的合取不可满足，块并集穷尽查询真集',
                      disj)
    if disj.verdict == REJECTED:
        return rejected('union_has_leftover',
                        '存在满足查询但不命中任何缓存块的行，需要基表补偿', disj)
    return unknown('union_coverage_inconclusive',
                   '块并集覆盖性无法在登记结构内完全证明', direct, disj)


# ---------------------------------------------------------------------------
# 多缓存互斥：同分区内若多个缓存块并存，必须证明谓词互斥，否则会重复计数
# ---------------------------------------------------------------------------

def prove_tiles_disjoint(fits: list[CacheFit]) -> Proof:
    for i in range(len(fits)):
        for j in range(i + 1, len(fits)):
            a, b = fits[i].entry.predicate, fits[j].entry.predicate
            r = prove_disjoint(a, b)
            if r.verdict != PROVED:
                return rejected(
                    'cache_overlap',
                    f'缓存 {fits[i].entry.cache_id} 与 {fits[j].entry.cache_id} '
                    f'无法证明互斥（{r.reason}）；重叠会重复计数，且 SUM/COUNT/AVG '
                    '无法靠相减去重')
    return proved('tiles_disjoint', '同分区所有缓存块的行集合两两不重叠')


def compensation_predicate(query_pred: Optional[Node],
                           cached_preds: list[Optional[Node]]) -> Node:
    """基表补偿谓词：查询为真，但不被任何缓存块覆盖（3VL 安全）。

    P_query AND (P_c1 IS NOT TRUE) AND (P_c2 IS NOT TRUE) ...
    NULL 行：P_ci 为 UNKNOWN -> IS NOT TRUE 为 TRUE，正确保留在补集中。
    """
    if not cached_preds:
        return query_pred if query_pred is not None else BooleanVal(True)
    parts: list[Node] = []
    if query_pred is not None:
        parts.append(query_pred)
    for pc in cached_preds:
        parts.append(complement_true(pc))
    if not parts:
        return BooleanVal(True)
    return And(tuple(parts))


# ---------------------------------------------------------------------------
# 登记扫描成本（单位：事实行读取量的估计；缓存块按其登记行数折算）
# ---------------------------------------------------------------------------

@dataclass
class CostModel:
    fact_rows_per_partition: dict[tuple[int, int], int] = field(default_factory=dict)
    # 每缓存块的登记读取成本（物化状态行数等）；缺省给 1
    cache_read_cost: dict[str, int] = field(default_factory=dict)
    base_fixed_cost: int = 0   # 每次基表扫描的固定开销

    def base_cost(self, partitions: tuple[Partition, ...]) -> int:
        return self.base_fixed_cost + sum(
            self.fact_rows_per_partition.get(p.key(), 0) for p in partitions
        )

    def cache_cost(self, entry: CacheEntry, partitions: tuple[Partition, ...]) -> int:
        per = self.cache_read_cost.get(entry.cache_id, 1)
        return per * len(partitions)


def query_partitions(query: QuerySpec, catalog: Catalog) -> list[Partition]:
    """查询区间按事实表登记分区对齐切分；未登记分区边界直接报错，不猜测。"""
    keys = sorted((k for k in catalog.fact_versions if k[0] == query.table),
                  key=lambda k: (k[1], k[2]))
    parts = [Partition(fd, td) for (_, fd, td) in keys]
    aligned = [p for p in parts if p.day_from >= query.from_day and p.day_to <= query.to_day]
    covered_from = query.from_day
    for p in aligned:
        if p.day_from != covered_from:
            raise ValueError(
                f'查询区间 [{query.from_day},{query.to_day}) 与登记分区不对齐：'
                f'缺口 [{covered_from},{p.day_from})')
        covered_from = p.day_to
    if covered_from != query.to_day:
        raise ValueError(
            f'查询区间 [{query.from_day},{query.to_day}) 未被登记分区完整覆盖，'
            f'止于 {covered_from}')
    return aligned
