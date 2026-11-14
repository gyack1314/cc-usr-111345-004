"""演示与核对用的小资料夹具：事实表、分区版本与各类缓存资产。

资料刻意保持小体量，便于独立逐事实重算。覆盖场景：
不同组大小的平均、NULL 过滤、重叠日期缓存、部分分区修订、查询空集、
谓词过滤列丢失、COUNT DISTINCT 身份集合、缓存失效与摘要损坏。
"""
from __future__ import annotations

from typing import Any, Optional

from .aggregates import (
    aggregate_rows, state_count_star, state_distinct, state_max, state_min,
    state_nonnull, state_sum,
)
from .ast_nodes import (
    AggCall, And, Column, Compare, IsNotNull, IsNull,
    Literal, Or, QuerySpec,
)
from .catalog import (
    Catalog, CacheEntry, Partition, digest_payload, fingerprint_schema,
)
from .logic import row_passes

TABLE = 'requests'
SCHEMA = ('id', 'tenant', 'region', 'day', 'cost')
STATE_VERSION = 'aggstate.v1'
SNAPSHOT_V1 = 'snap-2026-11-01'
SNAPSHOT_V2 = 'snap-2026-11-08'

# 半开日分区
P1 = Partition(0, 2)
P2 = Partition(2, 4)
P3 = Partition(4, 6)
PARTITIONS = [P1, P2, P3]


def col(name: str) -> Column:
    return Column(name)


def lit(value: Any) -> Literal:
    return Literal(value)


def cmp(op: str, c: str, value: Any) -> Compare:
    return Compare(op, Column(c), Literal(value))


def is_null(c: str) -> IsNull:
    return IsNull(Column(c))


def is_not_null(c: str) -> IsNotNull:
    return IsNotNull(Column(c))


def agg(func: str, c: Optional[str], distinct: bool = False,
        alias: Optional[str] = None) -> AggCall:
    return AggCall(func, c, distinct, alias)


def query(groups: tuple[str, ...], aggs: tuple[AggCall, ...],
          fd: int, td: int, predicate=None, table: str = TABLE) -> QuerySpec:
    return QuerySpec(table, predicate, groups, aggs, fd, td)


def base_facts() -> list[dict[str, Any]]:
    # 12 行；cost 含 None 以检验 NULL/3VL；region 用于过滤列丢失场景
    return [
        {'id': 'f01', 'tenant': 'A', 'region': 'cn', 'day': 0, 'cost': 10},
        {'id': 'f02', 'tenant': 'A', 'region': 'cn', 'day': 0, 'cost': 30},
        {'id': 'f03', 'tenant': 'A', 'region': 'sg', 'day': 1, 'cost': 50},
        {'id': 'f04', 'tenant': 'B', 'region': 'cn', 'day': 1, 'cost': None},
        {'id': 'f05', 'tenant': 'B', 'region': 'cn', 'day': 2, 'cost': 40},
        {'id': 'f06', 'tenant': 'A', 'region': 'sg', 'day': 2, 'cost': 70},
        {'id': 'f07', 'tenant': 'B', 'region': 'sg', 'day': 3, 'cost': 20},
        {'id': 'f08', 'tenant': 'A', 'region': 'cn', 'day': 3, 'cost': 90},
        {'id': 'f09', 'tenant': 'C', 'region': 'cn', 'day': 4, 'cost': 5},
        {'id': 'f10', 'tenant': 'C', 'region': 'sg', 'day': 4, 'cost': 15},
        {'id': 'f11', 'tenant': 'B', 'region': 'cn', 'day': 5, 'cost': None},
        {'id': 'f12', 'tenant': 'A', 'region': 'cn', 'day': 5, 'cost': 110},
    ]


def partition_of_day(day: int) -> Partition:
    for p in PARTITIONS:
        if p.day_from <= day < p.day_to:
            return p
    raise KeyError(day)


def facts_by_partition(facts: list[dict[str, Any]]
                       ) -> dict[tuple[int, int], list[dict[str, Any]]]:
    out: dict[tuple[int, int], list[dict[str, Any]]] = {p.key(): [] for p in PARTITIONS}
    for r in facts:
        out[partition_of_day(r['day']).key()].append(r)
    return out


def make_cache(cache_id: str, groups: tuple[str, ...], state_names: tuple[str, ...],
               parts: list[Partition], predicate=None, snapshot: str = SNAPSHOT_V1,
               facts: Optional[list[dict[str, Any]]] = None,
               partition_versions: Optional[dict[tuple[int, int], str]] = None,
               created_at: int = 0) -> CacheEntry:
    """物化缓存。payload 按分区隔离：{partition_key: {group_key: AggState}}，
    这样计划只引用某几个分区时，执行器不会读到块外分组而重复/越界计数。"""
    facts = base_facts() if facts is None else facts
    payload: dict[tuple[int, int], dict[tuple[Any, ...], Any]] = {}
    for p in parts:
        part_rows = [r for r in facts
                     if p.day_from <= r['day'] < p.day_to and row_passes(predicate, r)]
        payload[p.key()] = aggregate_rows(part_rows, groups, frozenset(state_names))
    pvs = partition_versions or {p.key(): snapshot for p in parts}
    entry = CacheEntry(
        cache_id=cache_id,
        source_table=TABLE,
        source_snapshot=snapshot,
        schema_fingerprint=fingerprint_schema(SCHEMA),
        groups=groups,
        predicate=predicate,
        state_names=tuple(state_names),
        state_version=STATE_VERSION,
        partitions=tuple(parts),
        partition_versions=pvs,
        content_digest='',
        source_binding=f'{TABLE}@{snapshot}',
        payload=payload,
        created_at=created_at,
    )
    object.__setattr__(entry, 'content_digest', digest_payload(payload, entry))
    return entry


def standard_states() -> tuple[str, ...]:
    return (state_count_star(), state_nonnull('cost'), state_sum('cost'),
            state_min('cost'), state_max('cost'))


def build_catalog(version_overrides: Optional[dict[tuple[int, int], str]] = None,
                  equivalences: Optional[list] = None) -> Catalog:
    catalog = Catalog()
    versions = {(TABLE, p.day_from, p.day_to): SNAPSHOT_V1 for p in PARTITIONS}
    if version_overrides:
        versions.update(version_overrides)
    catalog.fact_schemas[TABLE] = SCHEMA
    catalog.fact_versions.update(versions)
    for e in equivalences or []:
        catalog.add_equivalence(e)
    return catalog


def seed_catalog(catalog: Optional[Catalog] = None) -> Catalog:
    """登记一组覆盖正常与边界情形的缓存资产。"""
    catalog = catalog or build_catalog()

    # 日粒度（tenant,day）全量缓存：比按 tenant 的查询更细，可向上汇总
    catalog.register_cache(make_cache(
        'daily-tenant-v1', ('tenant', 'day'), standard_states(),
        [P1, P2, P3], predicate=None))

    # 仅覆盖 P1 的日粒度缓存（部分覆盖，需要补偿 P2/P3）
    catalog.register_cache(make_cache(
        'daily-tenant-p1', ('tenant', 'day'), standard_states(), [P1]))

    # 更宽谓词缓存：region=cn OR region=sg（实际全量），谓词包含查询 region='cn'
    wide = Or((cmp('=', 'region', 'cn'), cmp('=', 'region', 'sg')))
    catalog.register_cache(make_cache(
        'daily-wide-region', ('tenant', 'day', 'region'),
        standard_states() + (state_distinct('tenant'),),
        [P1, P2], predicate=wide))

    # 与 wide 互斥的窄块（region='us'，资料内无行 -> 空缓存，仍合法）
    catalog.register_cache(make_cache(
        'daily-region-us', ('tenant', 'day', 'region'),
        standard_states(), [P1, P2], predicate=cmp('=', 'region', 'us')))

    # 残差复用缓存：谓词比查询窄但蕴涵查询（tenant=A AND region=cn => tenant=A）。
    # region 保留在分组键中，查询 tenant=A 时缓存覆盖 cn 部分，sg 行走基表补偿。
    narrow = And((cmp('=', 'tenant', 'A'), cmp('=', 'region', 'cn')))
    catalog.register_cache(make_cache(
        'daily-tenant-a-cn', ('tenant', 'day', 'region'),
        standard_states(), [P1, P2], predicate=narrow))

    # 与 cn 块互斥的 sg 块：两者并集恰好覆盖查询 tenant=A 的全部行，
    # 用于演示“多个互斥缓存块完整覆盖、免基表扫描”的残差切分。
    sg_block = And((cmp('=', 'tenant', 'A'), cmp('=', 'region', 'sg')))
    catalog.register_cache(make_cache(
        'daily-tenant-a-sg', ('tenant', 'day', 'region'),
        standard_states(), [P1, P2], predicate=sg_block))

    # 重叠缓存：与 daily-tenant-v1 覆盖相同分区且谓词都恒真 -> 互斥证明必失败
    catalog.register_cache(make_cache(
        'daily-overlap', ('tenant', 'day'), standard_states(),
        [P1], predicate=None, created_at=99))

    # 过滤列丢失：查询若按 region 过滤，该缓存分组键无 region 且无常量证明
    catalog.register_cache(make_cache(
        'daily-no-region', ('tenant', 'day'), standard_states(), [P2]))

    # 只有 avg 结果、没有 sum/count 组件的缓存（禁止对平均再平均）
    avg_only_states = ('avg_cost_placeholder',)
    catalog.register_cache(make_cache(
        'avg-only-bad', ('tenant', 'day'), avg_only_states, [P3]))

    # 带完整身份集合的 COUNT DISTINCT 缓存
    catalog.register_cache(make_cache(
        'distinct-tenant', ('day',),
        (state_distinct('tenant'), state_count_star()), [P1, P2, P3]))

    # 仅普通计数的缓存，试图冒充 COUNT DISTINCT
    catalog.register_cache(make_cache(
        'count-star-only', ('day',), (state_count_star(),), [P1, P2, P3]))

    return catalog
