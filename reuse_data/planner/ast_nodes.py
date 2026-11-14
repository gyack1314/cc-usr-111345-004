"""受限 AST 登记：查询与缓存谓词、聚合调用的统一表示。

只允许单事实表上的受限结构：
- 表达式：列引用、字面量（int/str/bool/None）
- 谓词：比较(=,<>,>,>=,<,<=)、IS NULL、IS NOT NULL、IS NOT TRUE、AND、OR、布尔常量
- 聚合：COUNT/SUM/MIN/MAX/AVG（含 COUNT DISTINCT）
- 查询：单事实表 + 谓词 + 分组键 + 聚合列表 + 半开日期区间[from_day, to_day)

任何不在登记范围内的节点都会被显式拒绝（unsupported），绝不猜测语义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

CMP_OPS = ('=', '<>', '>', '>=', '<', '<=')


class AstError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    kind: str = field(init=False)


@dataclass(frozen=True)
class Literal(Node):
    value: Any  # int / str / bool / None
    kind: str = field(default='lit', init=False)


@dataclass(frozen=True)
class Column(Node):
    name: str
    kind: str = field(default='col', init=False)


@dataclass(frozen=True)
class Compare(Node):
    op: str
    left: Node
    right: Node
    kind: str = field(default='cmp', init=False)


@dataclass(frozen=True)
class IsNull(Node):
    value: Node
    kind: str = field(default='is_null', init=False)


@dataclass(frozen=True)
class IsNotNull(Node):
    value: Node
    kind: str = field(default='is_not_null', init=False)


@dataclass(frozen=True)
class IsNotTrue(Node):
    value: Node
    kind: str = field(default='is_not_true', init=False)


@dataclass(frozen=True)
class And(Node):
    children: tuple[Node, ...]
    kind: str = field(default='and', init=False)


@dataclass(frozen=True)
class Or(Node):
    children: tuple[Node, ...]
    kind: str = field(default='or', init=False)


@dataclass(frozen=True)
class Not(Node):
    child: Node
    kind: str = field(default='not', init=False)


@dataclass(frozen=True)
class BooleanVal(Node):
    # True / False / None(UNKNOWN)
    value: Optional[bool]
    kind: str = field(default='bool', init=False)


@dataclass(frozen=True)
class AggCall(Node):
    func: str  # count/sum/min/max/avg
    col: Optional[str]  # None 表示 COUNT(*)
    distinct: bool = False
    alias: Optional[str] = None
    kind: str = field(default='agg', init=False)


@dataclass(frozen=True)
class QuerySpec(Node):
    table: str
    predicate: Optional[Node]
    groups: tuple[str, ...]
    aggs: tuple[AggCall, ...]
    from_day: int  # 包含
    to_day: int    # 不包含
    kind: str = field(default='query', init=False)


_NODE_KINDS = {
    'lit': Literal,
    'col': Column,
    'cmp': Compare,
    'is_null': IsNull,
    'is_not_null': IsNotNull,
    'is_not_true': IsNotTrue,
    'and': And,
    'or': Or,
    'not': Not,
    'bool': BooleanVal,
    'agg': AggCall,
    'query': QuerySpec,
}


def to_dict(node: Node) -> Any:
    if isinstance(node, Literal):
        return {'kind': 'lit', 'value': node.value}
    if isinstance(node, BooleanVal):
        return {'kind': 'bool', 'value': node.value}
    if isinstance(node, Column):
        return {'kind': 'col', 'name': node.name}
    if isinstance(node, Compare):
        return {'kind': 'cmp', 'op': node.op,
                'left': to_dict(node.left), 'right': to_dict(node.right)}
    if isinstance(node, IsNull):
        return {'kind': 'is_null', 'value': to_dict(node.value)}
    if isinstance(node, IsNotNull):
        return {'kind': 'is_not_null', 'value': to_dict(node.value)}
    if isinstance(node, IsNotTrue):
        return {'kind': 'is_not_true', 'value': to_dict(node.value)}
    if isinstance(node, (And, Or)):
        return {'kind': node.kind, 'children': [to_dict(c) for c in node.children]}
    if isinstance(node, Not):
        return {'kind': 'not', 'child': to_dict(node.child)}
    if isinstance(node, AggCall):
        return {'kind': 'agg', 'func': node.func, 'col': node.col,
                'distinct': node.distinct, 'alias': node.alias}
    if isinstance(node, QuerySpec):
        return {
            'kind': 'query', 'table': node.table,
            'predicate': to_dict(node.predicate) if node.predicate else None,
            'groups': list(node.groups),
            'aggs': [to_dict(a) for a in node.aggs],
            'from_day': node.from_day, 'to_day': node.to_day,
        }
    raise AstError(f'未知节点类型: {type(node)!r}')


def from_dict(data: Any) -> Node:
    if not isinstance(data, dict) or 'kind' in data:
        if not isinstance(data, dict):
            raise AstError('AST 节点必须是对象')
    kind = data.get('kind')
    cls = _NODE_KINDS.get(kind)
    if cls is None:
        raise AstError(f'未登记的节点类型: {kind!r}')
    if kind == 'lit':
        v = data['value']
        if v is not None and not isinstance(v, (int, str, bool)):
            raise AstError('字面量仅支持 int/str/bool/None')
        return Literal(v)
    if kind == 'bool':
        v = data['value']
        if v is not None and not isinstance(v, bool):
            raise AstError('布尔常量仅支持 true/false/null')
        return BooleanVal(v)
    if kind == 'col':
        name = data['name']
        if not isinstance(name, str) or not name:
            raise AstError('列名必须是非空字符串')
        return Column(name)
    if kind == 'cmp':
        op = data['op']
        if op not in CMP_OPS:
            raise AstError(f'不支持的比较运算符: {op!r}')
        left, right = from_dict(data['left']), from_dict(data['right'])
        if not _is_expr(left) or not _is_expr(right):
            raise AstError('比较运算只允许列与字面量')
        if not (isinstance(left, Column) or isinstance(right, Column)):
            raise AstError('比较运算必须引用至少一列，不允许字面量与字面量比较')
        if isinstance(left, Column) and isinstance(right, Column):
            raise AstError('受限登记不支持两列直接比较')
        return Compare(op, left, right)
    if kind == 'is_null':
        return IsNull(_as_col(from_dict(data['value'])))
    if kind == 'is_not_null':
        return IsNotNull(_as_col(from_dict(data['value'])))
    if kind == 'is_not_true':
        return IsNotTrue(from_dict(data['value']))
    if kind in ('and', 'or'):
        children = tuple(from_dict(c) for c in data['children'])
        if len(children) < 2:
            raise AstError(f'{kind} 至少需要两个子节点')
        for c in children:
            if not isinstance(c, (Compare, IsNull, IsNotNull, IsNotTrue, And, Or, Not, BooleanVal)):
                raise AstError(f'{kind} 子节点必须是谓词')
        return And(children) if kind == 'and' else Or(children)
    if kind == 'not':
        return Not(from_dict(data['child']))
    if kind == 'agg':
        func = data['func']
        if func not in ('count', 'sum', 'min', 'max', 'avg'):
            raise AstError(f'不支持的聚合: {func!r}')
        col = data['col']
        if col is not None and not isinstance(col, str):
            raise AstError('聚合列必须是列名或 null(COUNT(*))')
        if func == 'count' and col is None and data.get('distinct'):
            raise AstError('COUNT DISTINCT 必须指定列')
        return AggCall(func, col, bool(data.get('distinct')), data.get('alias'))
    if kind == 'query':
        groups = tuple(data['groups'])
        if not all(isinstance(g, str) for g in groups) or len(set(groups)) != len(groups):
            raise AstError('分组键必须是互不重复的列名')
        aggs = tuple(from_dict(a) for a in data['aggs'])
        if not aggs or not all(isinstance(a, AggCall) for a in aggs):
            raise AstError('查询必须声明至少一个登记聚合')
        pred = from_dict(data['predicate']) if data.get('predicate') else None
        if pred is not None and not _is_predicate(pred):
            raise AstError('查询过滤必须是谓词')
        fd, td = data['from_day'], data['to_day']
        if not isinstance(fd, int) or not isinstance(td, int) or fd >= td:
            raise AstError('日期区间必须是 from_day < to_day 的整数半开区间')
        return QuerySpec(data['table'], pred, groups, aggs, fd, td)
    raise AstError(f'未登记的节点类型: {kind!r}')


def _is_expr(n: Node) -> bool:
    return isinstance(n, (Column, Literal))


def _as_col(n: Node) -> Column:
    if not isinstance(n, Column):
        raise AstError('IS [NOT] NULL 只能作用于列')
    return n


def _is_predicate(n: Node) -> bool:
    return isinstance(n, (Compare, IsNull, IsNotNull, IsNotTrue, And, Or, Not, BooleanVal))


def validate(node: Node) -> None:
    """递归校验 AST 落在受限登记范围内，否则抛 AstError。"""
    if isinstance(node, QuerySpec):
        if node.predicate:
            validate(node.predicate)
        for a in node.aggs:
            validate(a)
        return
    if isinstance(node, Compare):
        if node.op not in CMP_OPS or not _is_expr(node.left) or not _is_expr(node.right):
            raise AstError('非法比较表达式')
        if not (isinstance(node.left, Column) or isinstance(node.right, Column)):
            raise AstError('比较运算必须引用至少一列，不允许字面量与字面量比较')
        if isinstance(node.left, Column) and isinstance(node.right, Column):
            raise AstError('受限登记不支持两列直接比较')
        return
    if isinstance(node, (IsNull, IsNotNull)):
        if not isinstance(node.value, Column):
            raise AstError('IS [NOT] NULL 只能作用于列')
        return
    if isinstance(node, IsNotTrue):
        if not _is_predicate(node.value):
            raise AstError('IS NOT TRUE 只能作用于谓词')
        validate(node.value)
        return
    if isinstance(node, (And, Or)):
        for c in node.children:
            validate(c)
        return
    if isinstance(node, Not):
        validate(node.child)
        return
    if isinstance(node, (Column, Literal, BooleanVal, AggCall)):
        return
    raise AstError(f'节点超出受限 AST: {node.kind}')


def predicate_columns(node: Optional[Node]) -> frozenset[str]:
    if node is None:
        return frozenset()
    if isinstance(node, Column):
        return frozenset({node.name})
    if isinstance(node, (Literal, BooleanVal)):
        return frozenset()
    if isinstance(node, Compare):
        return predicate_columns(node.left) | predicate_columns(node.right)
    if isinstance(node, (IsNull, IsNotNull)):
        return predicate_columns(node.value)
    if isinstance(node, IsNotTrue):
        return predicate_columns(node.value)
    if isinstance(node, (And, Or)):
        out: frozenset[str] = frozenset()
        for c in node.children:
            out |= predicate_columns(c)
        return out
    if isinstance(node, Not):
        return predicate_columns(node.child)
    raise AstError(f'无法提取列: {node.kind}')


def equality_constants(node: Optional[Node]) -> dict[str, Any]:
    """从合取范式顶层提取可证明的 col = 常量 映射。"""
    out: dict[str, Any] = {}
    if node is None:
        return out
    if isinstance(node, And):
        for c in node.children:
            out.update(equality_constants(c))
        return out
    if isinstance(node, Compare) and node.op == '=':
        if isinstance(node.left, Column) and isinstance(node.right, Literal):
            out[node.left.name] = node.right.value
        elif isinstance(node.right, Column) and isinstance(node.left, Literal):
            out[node.right.name] = node.left.value
    return out


def canonical_json(node: Node) -> str:
    import json
    return json.dumps(to_dict(node), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
