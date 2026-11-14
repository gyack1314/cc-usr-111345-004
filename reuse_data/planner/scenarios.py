"""端到端场景：规划、执行、独立逐事实核对与扫描统计。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .ast_nodes import QuerySpec
from .catalog import Catalog, EquivalenceProof
from .coverage import CostModel
from .executor import Executor
from .fixtures import (
    P2, SNAPSHOT_V1, SNAPSHOT_V2, TABLE,
    base_facts, build_catalog, facts_by_partition, seed_catalog, standard_states,
)
from .planner import Planner, SearchBudget
from .reference import ReferenceEngine


@dataclass
class ScenarioResult:
    name: str
    description: str
    plan: dict[str, Any]
    result_rows: list[dict[str, Any]]
    reference_rows: list[dict[str, Any]]
    match: bool
    stats: dict[str, Any]
    extra: dict[str, Any]


def _cost_model(by_partition: dict[tuple[int, int], list[dict[str, Any]]]
                ) -> CostModel:
    # 登记扫描成本：基表按事实行数计；缓存按物化状态块固定小额成本，
    # 体现“读预聚合结果比重扫明细便宜”，具体数值来自登记而非猜测。
    return CostModel(
        fact_rows_per_partition={k: len(v) for k, v in by_partition.items()},
        cache_read_cost={}, base_fixed_cost=0)


def _fact_reader(by_partition):
    def read(table, partitions, predicate):
        out = []
        for p in partitions:
            for r in by_partition.get(p.key(), []):
                out.append(dict(r))
        return out
    return read


def _catalog_with_cn_equivalent_cache():
    """缓存谓词 region='cn' 与查询谓词等价，分组保留 region，覆盖 P1/P2。"""
    from .fixtures import make_cache, P1, P2, cmp as fcmp
    cat = seed_catalog()
    cat.register_cache(make_cache(
        'daily-region-cn-equiv', ('tenant', 'day', 'region'),
        standard_states(), [P1, P2], predicate=fcmp('=', 'region', 'cn')))
    return cat


def run_scenario(name: str, description: str, q: QuerySpec, catalog: Catalog,
                 facts: Optional[list[dict[str, Any]]] = None,
                 budget: Optional[SearchBudget] = None,
                 expect_error: Optional[str] = None,
                 after_plan=None) -> ScenarioResult:
    facts = base_facts() if facts is None else facts
    by_partition = facts_by_partition(facts)
    planner = Planner(catalog, _cost_model(by_partition), budget or SearchBudget())
    plan = planner.plan(q, query_id=name)

    # 计划生成后、执行前的外部事件（如缓存失效），用于检验执行期复验
    if after_plan is not None:
        after_plan(catalog, plan)

    extra: dict[str, Any] = {}
    if expect_error:
        try:
            Executor(catalog, _fact_reader(by_partition)).execute(plan, q)
        except Exception as exc:  # noqa: BLE001 - 场景需要记录拒绝行为
            extra['expected_error'] = str(exc)
            assert expect_error in str(exc), f'期望错误含 {expect_error!r}，实得 {exc}'
        else:
            raise AssertionError(f'场景 {name} 期望执行被拒绝，但执行成功')
        return ScenarioResult(name, description, plan.to_dict(), [], [],
                              True, {}, extra)

    executor = Executor(catalog, _fact_reader(by_partition))
    executed = executor.execute(plan, q)
    reference = ReferenceEngine(by_partition).run(q)
    match = executed.as_bag() == ReferenceEngine(by_partition).run_bag(q)
    stats = {
        'fact_rows_read': executed.stats.fact_rows_read,
        'fact_rows_passed': executed.stats.fact_rows_passed,
        'cache_blocks_used': executed.stats.cache_blocks_used,
        'cache_groups_read': executed.stats.cache_groups_read,
        'total_fact_rows': len(facts),
        'base_only_scan_estimate': plan.base_only_cost,
        'plan_cost': plan.total_cost,
    }
    return ScenarioResult(name, description, plan.to_dict(),
                          executed.rows, reference, match, stats, extra)


def all_scenarios() -> list[ScenarioResult]:
    from .fixtures import agg, cmp, query
    from .ast_nodes import IsNotNull, Column
    results: list[ScenarioResult] = []

    # 1) 不同组大小的平均：日粒度缓存向上汇总到 tenant 月平均（sum/count 重建）
    catalog = seed_catalog()
    q = query(('tenant',), (agg('avg', 'cost', alias='avg_cost'),
                            agg('sum', 'cost', alias='sum_cost'),
                            agg('count', None, alias='n')), 0, 6)
    results.append(run_scenario(
        'avg-rollup', '日粒度缓存汇总为租户平均：AVG 由 sum/非空count 重建，禁止平均再平均',
        q, catalog))

    # 2) NULL 过滤：cost IS NOT NULL，验证 3VL 与全 NULL 租户语义
    q = query(('tenant',), (agg('avg', 'cost', alias='avg_cost'),
                            agg('count', 'cost', alias='nonnull'),
                            agg('count', None, alias='n')),
              0, 6, predicate=IsNotNull(Column('cost')))
    results.append(run_scenario(
        'null-filter', 'IS NOT NULL 过滤：NULL 行不进 SUM/AVG/COUNT(col)，但 COUNT(*) 口径独立',
        q, catalog))

    # 3) 重叠日期缓存：daily-overlap 与主缓存同分区同谓词，必须排除一块
    results.append(run_scenario(
        'overlap-cache', '重叠日期缓存：互斥证明失败的组合被排除，避免重复计数',
        query(('tenant',), (agg('sum', 'cost', alias='s'),), 0, 2), catalog))

    # 4) 部分分区修订：P2 升级到 v2，无等价证明 -> v1 缓存的 P2 部分不可用，只复用 P1/P3
    revised = seed_catalog(build_catalog(version_overrides={
        (TABLE, P2.day_from, P2.day_to): SNAPSHOT_V2}))
    results.append(run_scenario(
        'partial-revision', '分区版本修订且无等价证明：缓存仅复用仍同版的分区，修订分区走基表',
        query(('tenant',), (agg('sum', 'cost', alias='s'),
                            agg('avg', 'cost', alias='a')), 0, 6), revised))

    # 5) 部分分区修订但登记了等价证明（压缩不改数据）-> 可继续混用
    eq = EquivalenceProof(TABLE, P2, SNAPSHOT_V1, SNAPSHOT_V2, '仅压缩，数据未变')
    revised_eq = seed_catalog(build_catalog(
        version_overrides={(TABLE, P2.day_from, P2.day_to): SNAPSHOT_V2},
        equivalences=[eq]))
    results.append(run_scenario(
        'revision-with-equivalence', '版本不同但有等价证明：允许复用并保留完整审计链',
        query(('tenant',), (agg('sum', 'cost', alias='s'),), 0, 6), revised_eq))

    # 6) 查询空集：谓词 region=us 无任何行；全局/分组聚合的空输入语义
    results.append(run_scenario(
        'empty-result', '查询空集：分组查询返回零行；另验全局聚合 COUNT=0、SUM/AVG=NULL',
        query(('tenant',), (agg('count', None, alias='n'),
                            agg('sum', 'cost', alias='s')),
              0, 6, predicate=cmp('=', 'region', 'us')), catalog))
    q_global_empty = query((), (agg('count', None, alias='n'),
                                agg('avg', 'cost', alias='a'),
                                agg('max', 'cost', alias='m')),
                           0, 6, predicate=cmp('=', 'region', 'us'))
    results.append(run_scenario(
        'empty-global', '空输入全局聚合：COUNT(*)=0，AVG/MAX=NULL，仍输出一行',
        q_global_empty, catalog))

    # 7) 过滤列丢失：按 region 过滤但缓存分组键不含 region
    results.append(run_scenario(
        'filter-column-dropped', '过滤列未保留：daily-no-region 被显式排除，其他合格缓存补偿',
        query(('tenant',), (agg('sum', 'cost', alias='s'),), 2, 4,
              predicate=cmp('=', 'region', 'cn')), catalog))

    # 8) 残差/补偿判定：窄缓存蕴涵宽查询但无法证明穷尽所有取值，
    #    规划器保守判定存在漏网行 -> 需要基表补偿；若补偿使缓存不省读取则回退基表
    results.append(run_scenario(
        'residual-filter', '窄缓存蕴涵宽查询但覆盖不穷尽：保守保留补偿，成本不优则回退基表',
        query(('tenant',), (agg('sum', 'cost', alias='s'),), 0, 4,
              predicate=cmp('=', 'tenant', 'A')), catalog))

    # 8b) 谓词等价的单块缓存可完整覆盖、免基表（残差谓词为空）
    results.append(run_scenario(
        'equivalent-predicate-cache', '缓存与查询谓词等价：包含双向证明通过，整块复用零基表',
        query(('tenant', 'region'), (agg('sum', 'cost', alias='s'),), 0, 4,
              predicate=cmp('=', 'region', 'cn')),
        _catalog_with_cn_equivalent_cache()))

    # 9) COUNT DISTINCT：有身份集合可复用；普通计数缓存被排除
    results.append(run_scenario(
        'count-distinct', 'COUNT DISTINCT 仅在完整可合并身份集合存在时复用',
        query((), (agg('count', 'tenant', distinct=True, alias='dten'),), 0, 6),
        catalog))

    # 10) 预算耗尽：max_nodes=1，仍返回已证明可行的计划或基表兜底
    results.append(run_scenario(
        'budget-exhausted', '搜索预算耗尽：返回当时已证明可行的最优计划，绝不给出未证明组合',
        query(('tenant',), (agg('sum', 'cost', alias='s'),), 0, 6), catalog,
        budget=SearchBudget(max_nodes=1)))

    # 11) 计划后缓存失效：执行期复验必须拒绝
    def _invalidate(catalog, plan):
        catalog.invalidate('daily-tenant-v1')
    results.append(run_scenario(
        'invalidated-after-plan', '计划后缓存失效：执行期复验拒绝返回旧结果',
        query(('tenant',), (agg('sum', 'cost', alias='s'),), 0, 6),
        seed_catalog(), expect_error='失效', after_plan=_invalidate))

    # 12) 摘要损坏：执行期摘要复验拒绝
    def _tamper(catalog, plan):
        entry = catalog.entries['daily-tenant-v1']
        object.__setattr__(
            entry, 'content_digest',
            entry.content_digest[:-2]
            + ('00' if entry.content_digest[-2:] != '00' else '11'))
    results.append(run_scenario(
        'digest-tampered', '缓存内容摘要损坏：执行期复验拒绝',
        query(('tenant',), (agg('sum', 'cost', alias='s'),), 0, 6),
        seed_catalog(), expect_error='摘要', after_plan=_tamper))

    # 13) 纯基表兜底：空目录
    empty_catalog = build_catalog()
    results.append(run_scenario(
        'base-only-fallback', '无任何缓存：纯基表扫描兜底，结果仍与参照引擎一致',
        query(('tenant',), (agg('sum', 'cost', alias='s'),
                            agg('min', 'cost', alias='mn'),
                            agg('max', 'cost', alias='mx')), 0, 6), empty_catalog))

    return results
