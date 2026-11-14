"""SQL 三值逻辑（3VL）求值与谓词规范化。

三值：True / False / None(UNKNOWN)。
过滤语义与 SQL 一致：只有判定为 True 的行保留；UNKNOWN 与 False 都被剔除。
"""
from __future__ import annotations

from typing import Any, Optional

from .ast_nodes import (
    And, BooleanVal, Column, Compare, IsNotNull, IsNotTrue, IsNull,
    Literal, Node, Not, Or,
)

TRUE = True
FALSE = False
UNKNOWN = None


def not3(v: Optional[bool]) -> Optional[bool]:
    if v is UNKNOWN:
        return UNKNOWN
    return not v


def and3(a: Optional[bool], b: Optional[bool]) -> Optional[bool]:
    if a is FALSE or b is FALSE:
        return FALSE
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return TRUE


def or3(a: Optional[bool], b: Optional[bool]) -> Optional[bool]:
    if a is TRUE or b is TRUE:
        return TRUE
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return FALSE


def eval_expr(node: Node, row: dict[str, Any]) -> Any:
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Column):
        return row.get(node.name)
    raise ValueError('非法表达式')


def _cmp(op: str, a: Any, b: Any) -> Optional[bool]:
    if a is None or b is None:
        return UNKNOWN
    try:
        if op == '=':
            return a == b
        if op == '<>':
            return a != b
        if op == '>':
            return a > b
        if op == '>=':
            return a >= b
        if op == '<':
            return a < b
        if op == '<=':
            return a <= b
    except TypeError:
        return UNKNOWN
    raise ValueError(f'未知运算符 {op}')


def eval_predicate(node: Node, row: dict[str, Any]) -> Optional[bool]:
    """按 3VL 求值；不在登记范围内的结构抛错而不是猜测。"""
    if isinstance(node, BooleanVal):
        return node.value
    if isinstance(node, Compare):
        return _cmp(node.op, eval_expr(node.left, row), eval_expr(node.right, row))
    if isinstance(node, IsNull):
        return eval_expr(node.value, row) is None
    if isinstance(node, IsNotNull):
        return eval_expr(node.value, row) is not None
    if isinstance(node, IsNotTrue):
        # P IS NOT TRUE：P 为 False 或 UNKNOWN 时为 True
        return eval_predicate(node.value, row) is not TRUE
    if isinstance(node, And):
        acc: Optional[bool] = TRUE
        for c in node.children:
            acc = and3(acc, eval_predicate(c, row))
        return acc
    if isinstance(node, Or):
        acc = UNKNOWN
        for c in node.children:
            acc = or3(acc, eval_predicate(c, row))
        return acc
    if isinstance(node, Not):
        return not3(eval_predicate(node.child, row))
    raise ValueError(f'谓词超出登记范围: {node.kind}')


def row_passes(node: Optional[Node], row: dict[str, Any]) -> bool:
    """SQL WHERE 语义：仅 True 通过。"""
    if node is None:
        return True
    return eval_predicate(node, row) is TRUE


# ---------------------------------------------------------------------------
# DNF 规范化：谓词 -> 合取子句列表；None 表示该子句恒真，False 常量整体剔除。
# 不支持的结构保留原样留给包含判定器返回 UNKNOWN，绝不猜测。
# ---------------------------------------------------------------------------

class DnfError(ValueError):
    pass


def push_not(node: Node) -> Node:
    """把 NOT 下推到叶子（德摩根 + 比较取反）。无法处理时抛 DnfError。"""
    if isinstance(node, Not):
        inner = node.child
        if isinstance(inner, Compare):
            inv = {'=': '<>', '<>': '=', '>': '<=', '<=': '>', '<': '>=', '>=': '<'}
            if inner.op not in inv:
                raise DnfError('NOT 无法下推')
            return Compare(inv[inner.op], inner.left, inner.right)
        if isinstance(inner, IsNull):
            return IsNotNull(inner.value)
        if isinstance(inner, IsNotNull):
            return IsNull(inner.value)
        if isinstance(inner, BooleanVal):
            return BooleanVal(not3(inner.value))
        if isinstance(inner, And):
            return Or(tuple(push_not(Not(c)) for c in inner.children))
        if isinstance(inner, Or):
            return And(tuple(push_not(Not(c)) for c in inner.children))
        if isinstance(inner, Not):
            return push_not(inner.child)
        if isinstance(inner, IsNotTrue):
            # NOT(P IS NOT TRUE) <=> P IS TRUE；登记范围内没有 IS TRUE 节点，
            # 直接返回 P（在 WHERE 语境下 P 为真即通过，UNKNOWN 不通过，二者等价）。
            return inner.value
        raise DnfError('NOT 无法下推到登记结构')
    if isinstance(node, And):
        return And(tuple(push_not(c) for c in node.children))
    if isinstance(node, Or):
        return Or(tuple(push_not(c) for c in node.children))
    if isinstance(node, IsNotTrue):
        return IsNotTrue(push_not(node.value))
    return node


def _flatten(node: Node, kind: type) -> tuple[Node, ...]:
    out: list[Node] = []
    for c in node.children:
        if isinstance(c, kind):
            out.extend(_flatten(c, kind))
        else:
            out.append(c)
    return tuple(out)


def to_dnf(node: Optional[Node]) -> list[tuple[Node, ...]]:
    """返回 DNF：外层列表为 OR，内层元组为 AND。

    恒真 -> [()]；恒假 -> []。无法规范化的结构整体作为单叶子子句保留，
    由包含判定器显式给出 UNKNOWN，而不是被猜成包含。
    """
    if node is None:
        return [()]
    node = push_not(node)
    if isinstance(node, BooleanVal):
        return [()] if node.value is TRUE else []
    if isinstance(node, Or):
        node = Or(_flatten(node, Or))
        out: list[tuple[Node, ...]] = []
        for c in node.children:
            out.extend(to_dnf(c))
        return _dedupe_clauses(out)
    if isinstance(node, And):
        node = And(_flatten(node, And))
        clauses: list[tuple[Node, ...]] = [()]
        for c in node.children:
            sub = to_dnf(c)
            if not sub:
                return []
            clauses = [a + b for a in clauses for b in sub]
        return _dedupe_clauses(clauses)
    # 叶子（含 IsNotTrue 等）：保留为单子句
    return [(node,)]


def _dedupe_clauses(clauses: list[tuple[Node, ...]]) -> list[tuple[Node, ...]]:
    seen: set[tuple[str, ...]] = set()
    out: list[tuple[Node, ...]] = []
    for c in clauses:
        key = tuple(sorted(_leaf_key(x) for x in c))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _leaf_key(n: Node) -> str:
    from .ast_nodes import canonical_json
    return canonical_json(n)
