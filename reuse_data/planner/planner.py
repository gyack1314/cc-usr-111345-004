"""聚合复用规划器：在已证明的覆盖块上做成本最优切分。

规则：
- 每个分区独立选择一组互斥缓存块（谓词互斥必须有证明），其余走基表补偿；
- 组合收益不能简单相加：同分区组合必须先过互斥证明，成本按实际登记扫描量计算；
- 多个可行组合用登记扫描成本选择；预算（搜索节点数）耗尽时返回当时已证明
  可行的最优计划，若无任何可行复用则返回纯基表方案（永远有安全兜底）；
- 缓存重叠、版本混用、无法证明包含等情况逐条记录排除原因。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .ast_nodes import Node, QuerySpec
from .catalog import Catalog, Partition
from .coverage import (
    CacheFit, CostModel, Exclusion, assess_entry, compensation_predicate,
    prove_tiles_disjoint, prove_union_covers, query_partitions,
)
from .predicate import Proof, is_contradiction, is_proved, proved, rejected


@dataclass(frozen=True)
class CacheBlock:
    cache_id: str
    partitions: tuple[Partition, ...]
    residual_predicate: Optional[Node]
    implication_proof: Proof
    aggregate_proof: Proof
    read_cost: int


@dataclass(frozen=True)
class BaseScan:
    partitions: tuple[Partition, ...]
    predicate: Node
    read_cost: int


@dataclass
class Plan:
    query_id: str
    cache_blocks: list[CacheBlock] = field(default_factory=list)
    base_scans: list[BaseScan] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)
    proofs: list[Proof] = field(default_factory=list)
    nodes_explored: int = 0
    budget_exhausted: bool = False
    total_cost: int = 0
    base_only_cost: int = 0
    reused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            'query_id': self.query_id,
            'reused': self.reused,
            'total_cost': self.total_cost,
            'base_only_cost': self.base_only_cost,
            'base_rows_saved': self.base_only_cost - self.total_cost,
            'nodes_explored': self.nodes_explored,
            'budget_exhausted': self.budget_exhausted,
            'cache_blocks': [
                {
                    'cache_id': b.cache_id,
                    'partitions': [p.key() for p in b.partitions],
                    'residual_predicate': _pred_repr(b.residual_predicate),
                    'implication': b.implication_proof.to_dict(),
                    'aggregate': b.aggregate_proof.to_dict(),
                    'read_cost': b.read_cost,
                } for b in self.cache_blocks
            ],
            'base_scans': [
                {
                    'partitions': [p.key() for p in s.partitions],
                    'predicate': _pred_repr(s.predicate),
                    'read_cost': s.read_cost,
                } for s in self.base_scans
            ],
            'exclusions': [e.to_dict() for e in self.exclusions],
            'proofs': [p.to_dict() for p in self.proofs],
        }


def _pred_repr(p: Optional[Node]) -> Any:
    if p is None:
        return None
    from .ast_nodes import to_dict
    return to_dict(p)


@dataclass
class SearchBudget:
    max_nodes: int = 1000


@dataclass
class _Choice:
    partitions: tuple[Partition, ...]
    fits: tuple[CacheFit, ...]
    disjoint_proof: Proof
    cost: int


class Planner:
    def __init__(self, catalog: Catalog, costs: Optional[CostModel] = None,
                 budget: Optional[SearchBudget] = None):
        self.catalog = catalog
        self.costs = costs or CostModel()
        self.budget = budget or SearchBudget()
        self.nodes = 0

    def plan(self, query: QuerySpec, query_id: str = 'query') -> Plan:
        plan = Plan(query_id=query_id)
        parts = query_partitions(query, self.catalog)
        base_total = self.costs.base_cost(tuple(parts))
        plan.base_only_cost = base_total

        # 1) 逐条评估候选缓存，保留排除原因
        fits: list[CacheFit] = []
        for entry in self.catalog.entries.values():
            fit, exclusion = assess_entry(entry, query, self.catalog)
            if exclusion is not None:
                plan.exclusions.append(exclusion)
            if fit is not None:
                fits.append(fit)
                # 分区级版本剪枝也要显式登记，说明每个被排除分区的去向
                for pp, reason in zip(fit.pruned_partitions, fit.prune_reasons):
                    plan.exclusions.append(Exclusion(
                        fit.entry.cache_id, 'partition_version',
                        f'分区 {pp.key()} 转基表补偿：{reason}'))

        # 2) 每个分区枚举可行的缓存组合（含空组合），按成本选最优
        # partition -> 该分区被哪些 fit 覆盖
        per_partition: dict[Partition, list[CacheFit]] = {p: [] for p in parts}
        for fit in fits:
            for p in fit.eligible_partitions:
                if p in per_partition:
                    per_partition[p].append(fit)

@dataclass
class _Choice:
    partitions: tuple[Partition, ...]
    fits: tuple[CacheFit, ...]
    disjoint_proof: Proof
    cost: int
    fully_covered: bool  # 块并集是否完整覆盖该分区的查询真集


class Planner:
    def __init__(self, catalog: Catalog, costs: Optional[CostModel] = None,
                 budget: Optional[SearchBudget] = None):
        self.catalog = catalog
        self.costs = costs or CostModel()
        self.budget = budget or SearchBudget()
        self.nodes = 0

    def plan(self, query: QuerySpec, query_id: str = 'query') -> Plan:
        plan = Plan(query_id=query_id)
        parts = query_partitions(query, self.catalog)
        base_total = self.costs.base_cost(tuple(parts))
        plan.base_only_cost = base_total

        # 1) 逐条评估候选缓存，保留排除原因
        fits: list[CacheFit] = []
        for entry in self.catalog.entries.values():
            fit, exclusion = assess_entry(entry, query, self.catalog)
            if exclusion is not None:
                plan.exclusions.append(exclusion)
            if fit is not None:
                fits.append(fit)
                # 分区级版本剪枝也要显式登记，说明每个被排除分区的去向
                for pp, reason in zip(fit.pruned_partitions, fit.prune_reasons):
                    plan.exclusions.append(Exclusion(
                        fit.entry.cache_id, 'partition_version',
                        f'分区 {pp.key()} 转基表补偿：{reason}'))

        # 2) 每个分区枚举可行的缓存组合（含空组合），按成本选最优
        # partition -> 该分区被哪些 fit 覆盖
        per_partition: dict[Partition, list[CacheFit]] = {p: [] for p in parts}
        for fit in fits:
            for p in fit.eligible_partitions:
                if p in per_partition:
                    per_partition[p].append(fit)

        chosen: dict[Partition, list[CacheFit]] = {}
        total_cache_cost = 0
        for p in parts:
            candidates = per_partition[p]
            best: Optional[_Choice] = None
            search_complete = True
            for combo in self._nonempty_subsets(candidates, plan):
                self._tick(plan)
                if plan.budget_exhausted:
                    # 组合空间未枚举完：任何"最优"都只是局部结论，
                    # 该分区放弃复用、回退基表，绝不返回未充分证明的组合。
                    search_complete = False
                    break
                dj = prove_tiles_disjoint(list(combo))
                if not is_proved(dj):
                    plan.exclusions.append(Exclusion(
                        '+'.join(f.entry.cache_id for f in combo),
                        'overlap', dj.reason))
                    continue
                cache_cost = sum(self.costs.cache_cost(f.entry, (p,)) for f in combo)
                cover = prove_union_covers(
                    query.predicate, [f.entry.predicate for f in combo])
                fully = is_proved(cover)
                # 完整覆盖：该分区免基表扫描，有效成本=缓存成本。
                # 部分覆盖：补偿仍需扫描整分区，有效成本=基表+缓存（缓存为净增开销）。
                effective = cache_cost if fully else self.costs.base_cost((p,)) + cache_cost
                choice = _Choice((p,), tuple(combo), dj, effective, fully)
                if best is None or choice.cost < best.cost:
                    best = choice
            if not search_complete:
                plan.exclusions.append(Exclusion(
                    '*', 'budget',
                    f'分区 {p.key()} 的缓存组合枚举在预算内未完成，按已证明的基表方案兜底'))
                chosen[p] = []
                continue
            # 只有严格比纯基表便宜才复用；部分覆盖的净增组合自然不会胜出
            if best is not None and best.cost < self.costs.base_cost((p,)):
                chosen[p] = list(best.fits)
                total_cache_cost += sum(
                    self.costs.cache_cost(f.entry, (p,)) for f in best.fits)
                plan.proofs.append(best.disjoint_proof)
            else:
                if best is not None:
                    plan.exclusions.append(Exclusion(
                        '+'.join(f.entry.cache_id for f in best.fits),
                        'cost',
                        f'分区 {p.key()} 缓存组合有效成本 {best.cost} 不低于纯基表 '
                        f'{self.costs.base_cost((p,))}，复用不省读取，改用基表'))
                chosen[p] = []

        # 3) 物化计划：缓存按条目聚合分区，基表扫描按相邻相同补偿谓词合并
        self._build_blocks(plan, chosen, query)
        self._build_base_scans(plan, chosen, parts, query)

        plan.total_cost = total_cache_cost + sum(s.read_cost for s in plan.base_scans)
        plan.reused = bool(plan.cache_blocks)
        if not plan.reused:
            plan.proofs.insert(0, rejected(
                'no_reuse', '没有任何缓存通过全部证明，采用纯基表扫描兜底'))
        else:
            plan.proofs.insert(0, proved(
                'coverage_complete',
                '查询区间由互斥缓存块与 3VL 安全基表补偿完整覆盖，无重叠无缺口'))
        return plan

    def _tick(self, plan: Plan) -> None:
        self.nodes += 1
        plan.nodes_explored = self.nodes
        if self.nodes > self.budget.max_nodes:
            plan.budget_exhausted = True

    def _nonempty_subsets(self, items: list[CacheFit], plan: Plan):
        """枚举非空子集；预算耗尽后停止扩张，已证明可行的组合仍然可用。"""
        n = len(items)
        for mask in range(1, 1 << n):
            if plan.budget_exhausted:
                return
            yield tuple(items[i] for i in range(n) if mask & (1 << i))

    def _build_blocks(self, plan: Plan, chosen: dict[Partition, list[CacheFit]],
                      query: QuerySpec) -> None:
        by_entry: dict[str, dict[str, Any]] = {}
        for p, fit_list in chosen.items():
            for fit in fit_list:
                slot = by_entry.setdefault(fit.entry.cache_id, {
                    'fit': fit, 'partitions': []})
                slot['partitions'].append(p)
        for cache_id, slot in by_entry.items():
            fit: CacheFit = slot['fit']
            ps = tuple(sorted(slot['partitions'], key=lambda x: x.key()))
            cost = self.costs.cache_cost(fit.entry, ps)
            plan.cache_blocks.append(CacheBlock(
                cache_id=cache_id,
                partitions=ps,
                residual_predicate=fit.residual_predicate,
                implication_proof=fit.implication,
                aggregate_proof=fit.aggregate.proof,
                read_cost=cost,
            ))

    def _build_base_scans(self, plan: Plan, chosen: dict[Partition, list[CacheFit]],
                          parts: list[Partition], query: QuerySpec) -> None:
        # 连续且补偿谓词相同的分区合并成一次基表扫描。
        # 若缓存块并集被证明完整覆盖查询真集（互斥 + 双向包含），
        # 则该分区补偿集为空，不发扫描、不读基表。
        runs: list[list[Partition]] = []
        run_preds: list[Node] = []
        for p in parts:
            fits = chosen[p]
            if fits:
                cover = prove_union_covers(
                    query.predicate, [f.entry.predicate for f in fits])
                if is_proved(cover):
                    plan.proofs.append(cover)
                    continue
            pred = compensation_predicate(
                query.predicate, [f.entry.predicate for f in fits])
            if is_contradiction(pred):
                continue
            if runs and _same(pred, run_preds[-1]):
                runs[-1].append(p)
            else:
                runs.append([p])
                run_preds.append(pred)
        for run_ps, pred in zip(runs, run_preds):
            ps = tuple(run_ps)
            plan.base_scans.append(BaseScan(
                partitions=ps, predicate=pred,
                read_cost=self.costs.base_cost(ps)))


def _same(a: Node, b: Node) -> bool:
    from .ast_nodes import canonical_json
    return canonical_json(a) == canonical_json(b)
