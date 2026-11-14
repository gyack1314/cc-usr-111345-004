"""缓存资产登记：来源快照、schema、分组、谓词、实际覆盖分区、摘要与版本。

适用与否完全由语义证明决定，缓存“更新”（创建时间更晚）本身不构成适用理由。
分区版本不同且没有登记的等价证明时不得混用；执行器在执行时还要再次验证
内容摘要与来源绑定，计划生成后失效的缓存不能返回旧结果。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .ast_nodes import Node, QuerySpec, to_dict
from .predicate import Proof, proved, rejected


@dataclass(frozen=True)
class Partition:
    # 分区按半开整数日区间 [day_from, day_to) 定义；边界须与查询区间对齐
    day_from: int
    day_to: int

    def covers_days(self, fd: int, td: int) -> bool:
        return self.day_from == fd and self.day_to == td

    def key(self) -> tuple[int, int]:
        return (self.day_from, self.day_to)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Partition) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(('Partition', self.day_from, self.day_to))


@dataclass(frozen=True)
class EquivalenceProof:
    """两个快照版本在某事实表、某分区上的等价证明（如只做不改数据的压缩）。"""
    table: str
    partition: Partition
    version_a: str
    version_b: str
    reason: str

    def relates(self, table: str, p: Partition, v1: str, v2: str) -> bool:
        if self.table != table or p.key() != self.partition.key() or v1 == v2:
            return False
        return {v1, v2} == {self.version_a, self.version_b}


@dataclass(frozen=True)
class CacheEntry:
    cache_id: str
    source_table: str
    source_snapshot: str
    schema_fingerprint: str          # 事实表 schema 的指纹
    groups: tuple[str, ...]
    predicate: Optional[Node]
    state_names: tuple[str, ...]
    state_version: str               # 聚合状态算法版本
    partitions: tuple[Partition, ...]           # 实际覆盖分区（半开日区间）
    partition_versions: dict[tuple[int, int], str]  # 每分区来源数据版本
    content_digest: str              # 物化聚合内容的 SHA256（执行时复验）
    source_binding: str              # 来源快照绑定标识
    payload: Any = None              # 物化的分组状态（执行器读取）
    created_at: int = 0              # 仅作登记信息，不参与适用判断


class StaleCacheError(ValueError):
    pass


class DigestError(ValueError):
    pass


@dataclass
class Catalog:
    fact_schemas: dict[str, tuple[str, ...]] = field(default_factory=dict)
    fact_versions: dict[tuple[str, int, int], str] = field(default_factory=dict)
    equivalences: list[EquivalenceProof] = field(default_factory=list)
    entries: dict[str, CacheEntry] = field(default_factory=dict)
    # 运行期失效标记（计划后失效 -> 执行器必须拒绝）
    invalidated: set[str] = field(default_factory=set)

    def register_fact(self, table: str, schema: tuple[str, ...],
                      partitions: list[Partition], version: str) -> None:
        self.fact_schemas[table] = tuple(schema)
        for p in partitions:
            self.fact_versions[(table, p.day_from, p.day_to)] = version

    def fact_version(self, table: str, p: Partition) -> Optional[str]:
        return self.fact_versions.get((table, p.day_from, p.day_to))

    def add_equivalence(self, proof: EquivalenceProof) -> None:
        self.equivalences.append(proof)

    def has_equivalence(self, table: str, p: Partition, v1: str, v2: str) -> bool:
        return any(e.relates(table, p, v1, v2) for e in self.equivalences)

    def register_cache(self, entry: CacheEntry) -> None:
        if entry.cache_id in self.entries:
            raise ValueError(f'缓存 {entry.cache_id} 已登记')
        self.entries[entry.cache_id] = entry

    def invalidate(self, cache_id: str) -> None:
        self.invalidated.add(cache_id)

    def is_valid(self, cache_id: str) -> bool:
        return cache_id not in self.invalidated and cache_id in self.entries


def fingerprint_schema(schema: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps(list(schema), sort_keys=True).encode('utf-8')
    ).hexdigest()[:16]


def _canonical(obj: Any) -> Any:
    """把分组状态规范化为可 JSON 序列化的稳定结构（元组键 -> 列表键）。"""
    from .aggregates import AggState
    if isinstance(obj, dict):
        return {json.dumps(list(k), ensure_ascii=False, sort_keys=True, default=str)
                if isinstance(k, tuple) else str(k): _canonical(v)
                for k, v in sorted(obj.items(), key=lambda kv: repr(kv[0]))}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return sorted((_canonical(v) for v in obj), key=repr)
    if isinstance(obj, AggState):
        return {
            'rows': obj.rows,
            'nonnull': _canonical(obj.nonnull),
            'sums': _canonical(obj.sums),
            'mins': _canonical(obj.mins),
            'maxs': _canonical(obj.maxs),
            'distinct': _canonical({k: list(v) for k, v in obj.distinct.items()}),
        }
    return obj


def digest_payload(payload: Any, entry: CacheEntry) -> str:
    body = {
        'cache_id': entry.cache_id,
        'source_snapshot': entry.source_snapshot,
        'source_binding': entry.source_binding,
        'groups': list(entry.groups),
        'predicate': to_dict(entry.predicate) if entry.predicate else None,
        'state_names': list(entry.state_names),
        'state_version': entry.state_version,
        'partitions': [p.key() for p in entry.partitions],
        'payload': _canonical(payload),
    }
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
    ).hexdigest()


def verify_entry(entry: CacheEntry, catalog: Catalog, query: QuerySpec) -> Proof:
    """条目级静态复验：失效标记、来源表、schema、内容摘要、状态版本。

    分区数据版本是“逐分区”性质，放在 partition_version_proof 中单独剪枝，
    这样一个多分区缓存只有部分分区过期时，其余分区仍可安全复用。
    """
    if not catalog.is_valid(entry.cache_id):
        return rejected('cache_invalidated',
                        f'缓存 {entry.cache_id} 已在计划生成后失效，不能返回旧结果')
    if entry.source_table != query.table:
        return rejected('source_table_mismatch',
                        f'缓存来源表 {entry.source_table} 与查询表 {query.table} 不一致')
    fp = fingerprint_schema(catalog.fact_schemas.get(query.table, ()))
    if entry.schema_fingerprint != fp:
        return rejected('schema_mismatch',
                        f'缓存 schema 指纹 {entry.schema_fingerprint} 与当前事实表 {fp} 不一致')
    actual = digest_payload(entry.payload, entry)
    if actual != entry.content_digest:
        return rejected('digest_mismatch',
                        f'缓存 {entry.cache_id} 内容摘要不一致（期望 {entry.content_digest[:12]}…，'
                        f'实得 {actual[:12]}…）')
    if entry.state_version not in _SUPPORTED_STATE_VERSIONS:
        return rejected('state_version_unknown',
                        f'聚合状态版本 {entry.state_version} 未登记，无法安全合并')
    return proved('entry_static_verified',
                  f'缓存 {entry.cache_id} 的失效标记、来源表、schema、摘要与状态版本通过复验')


def partition_version_proof(entry: CacheEntry, catalog: Catalog,
                            p: 'Partition') -> Proof:
    """单分区来源绑定：缓存版本等于当前版本，或登记了等价证明。"""
    cur = catalog.fact_version(entry.source_table, p)
    bound = entry.partition_versions.get(p.key())
    if cur is None:
        return rejected('partition_unknown',
                        f'事实表缺少分区 {p.key()} 的版本登记')
    if bound == cur:
        return proved('partition_version_match',
                      f'分区 {p.key()} 缓存与事实表同为版本 {cur}')
    if catalog.has_equivalence(entry.source_table, p, bound or '', cur):
        return proved('partition_equivalence',
                      f'分区 {p.key()} 版本 {bound!r} 与 {cur!r} 登记了等价证明')
    return rejected(
        'partition_version_conflict',
        f'分区 {p.key()} 缓存版本 {bound!r} 与当前版本 {cur!r} 不同且无等价证明，'
        '该分区不得混用，查询不能来自半旧半新的表')


_SUPPORTED_STATE_VERSIONS = frozenset({'aggstate.v1'})
