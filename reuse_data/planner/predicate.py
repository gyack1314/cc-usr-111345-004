"""谓词包含判定的证明接口。

核心命题（SQL 三值逻辑下）：
    对任意行 r，P_cached(r) IS TRUE  ⇒  P_query(r) IS TRUE
只有在登记结构内给出逐子句证明才判 proved；无法分析的结构一律 unknown，
绝不把不支持的逻辑猜成包含，也不比较 SQL 字符串。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .ast_nodes import (
    BooleanVal, Column, Compare, IsNotNull, IsNotTrue, IsNull,
    Literal, Node, equality_constants, predicate_columns,
)
from .logic import FALSE, TRUE, UNKNOWN, to_dnf
from .ranges import Range, all_range, cmp_to_range

PROVED = 'proved'
REJECTED = 'rejected'
UNKNOWN_VERDICT = 'unknown'

_UNSET = object()


@dataclass(frozen=True)
class Proof:
    verdict: str
    rule: str
    reason: str
    premises: tuple[Any, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        out = {'verdict': self.verdict, 'rule': self.rule, 'reason': self.reason}
        if self.premises:
            out['premises'] = [
                p.to_dict() if isinstance(p, Proof) else str(p) for p in self.premises
            ]
        return out


def proved(rule: str, reason: str, *premises: Any) -> Proof:
    return Proof(PROVED, rule, reason, tuple(premises))


def rejected(rule: str, reason: str, *premises: Any) -> Proof:
    return Proof(REJECTED, rule, reason, tuple(premises))


def unknown(rule: str, reason: str, *premises: Any) -> Proof:
    return Proof(UNKNOWN_VERDICT, rule, reason, tuple(premises))


def is_proved(p: Proof) -> bool:
    return p.verdict == PROVED


# ---------------------------------------------------------------------------
# 合取子句上的列约束
# ---------------------------------------------------------------------------

@dataclass
class _ColInfo:
    eq: Any = _UNSET
    wants_null: Optional[bool] = None  # True=IS NULL, False=IS NOT NULL, None=未知
    rng: Optional[Range] = None
    neq: list[Any] = field(default_factory=list)


@dataclass
class _ClauseModel:
    cols: dict[str, _ColInfo] = field(default_factory=dict)
    not_true_leaves: list[Node] = field(default_factory=list)
    opaque: list[Node] = field(default_factory=list)
    impossible: bool = False
    opaque_reason: Optional[str] = None

    def info(self, col: str) -> _ColInfo:
        return self.cols.setdefault(col, _ColInfo())


def _split_compare(node: Compare) -> tuple[Optional[str], str, Any]:
    if isinstance(node.left, Column) and isinstance(node.right, Literal):
        return node.left.name, node.op, node.right.value
    if isinstance(node.right, Column) and isinstance(node.left, Literal):
        col = node.right.name
        inv = {'=': '=', '<>': '<>', '>': '<', '<': '>', '>=': '<=', '<=': '>='}
        return col, inv[node.op], node.left.value
    return None, node.op, None


def model_clause(clause: tuple[Node, ...]) -> _ClauseModel:
    m = _ClauseModel()
    for lit in clause:
        if isinstance(lit, BooleanVal):
            if lit.value is FALSE:
                m.impossible = True
            continue
        if isinstance(lit, IsNull):
            info = m.info(lit.value.name)
            if info.wants_null is False or info.eq is not _UNSET:
                m.impossible = True
            info.wants_null = True
            continue
        if isinstance(lit, IsNotNull):
            info = m.info(lit.value.name)
            if info.wants_null is True:
                m.impossible = True
            info.wants_null = False
            continue
        if isinstance(lit, Compare):
            col, op, val = _split_compare(lit)
            if col is None:
                m.opaque.append(lit)
                m.opaque_reason = '比较表达式不是 列-字面量 形式'
                continue
            info = m.info(col)
            if info.wants_null is True:
                m.impossible = True  # NULL 行比较结果为 UNKNOWN，与 IS NULL 矛盾
            info.wants_null = False
            if op == '=':
                if info.eq is not _UNSET and info.eq != val:
                    m.impossible = True
                info.eq = val
                r = cmp_to_range('=', val)
                info.rng = r if info.rng is None else info.rng.intersect(r)
            elif op == '<>':
                info.neq.append(val)
            else:
                try:
                    r = cmp_to_range(op, val)
                except TypeError:
                    m.opaque.append(lit)
                    m.opaque_reason = f'列 {col} 的类型不支持区间推理'
                    continue
                info.rng = r if info.rng is None else info.rng.intersect(r)
                if info.rng is not None and info.rng.is_empty_domain():
                    m.impossible = True
            continue
        if isinstance(lit, IsNotTrue):
            m.not_true_leaves.append(lit.value)
            continue
        m.opaque.append(lit)
        m.opaque_reason = f'子句含未纳入包含判定的结构: {lit.kind}'
    return m


# ---------------------------------------------------------------------------
# 单叶子的蕴涵/反驳
# ---------------------------------------------------------------------------

def _eq_range(value: Any) -> Range:
    return cmp_to_range('=', value)


def _prove_literal_under(m: _ClauseModel, target: Node,
                         target_clause: tuple[Node, ...] = ()) -> Proof:
    """合取约束 m 为真时，target 是否必然为真（3VL）。"""
    if isinstance(target, BooleanVal):
        if target.value is TRUE:
            return proved('boolean_true', 'TRUE 恒成立')
        return rejected('boolean_false', 'FALSE/UNKNOWN 不可能为 TRUE')

    if isinstance(target, IsNull):
        info = m.cols.get(target.value.name)
        if info and info.wants_null is True:
            return proved('is_null_match', f'{target.value.name} 被约束为 NULL')
        if info and (info.wants_null is False or info.eq is not _UNSET):
            return rejected('is_null_conflict', f'{target.value.name} 已知非 NULL')
        return unknown('null_state_unknown', f'{target.value.name} 的可空性未知')

    if isinstance(target, IsNotNull):
        info = m.cols.get(target.value.name)
        if info and (info.wants_null is False or info.eq is not _UNSET):
            return proved('is_not_null_match', f'{target.value.name} 已知非 NULL')
        if info and info.wants_null is True:
            return rejected('is_not_null_conflict', f'{target.value.name} 已知为 NULL')
        return unknown('null_state_unknown', f'{target.value.name} 的可空性未知')

    if isinstance(target, Compare):
        col, op, val = _split_compare(target)
        if col is None:
            return unknown('unsupported_compare', '比较表达式不是 列-字面量 形式')
        info = m.cols.get(col)
        if info is None:
            return unknown('no_constraint', f'缓存子句未约束列 {col}')
        if info.wants_null is True:
            return rejected('three_valued_null',
                            f'{col} 为 NULL 时 {op} 判定为 UNKNOWN，不满足 TRUE 过滤')
        if op == '=':
            if info.eq is not _UNSET:
                if info.eq == val:
                    return proved('equality_constant', f'{col} 被等值约束为 {val!r}')
                return rejected('equality_conflict',
                                f'{col}={info.eq!r} 与 {col}={val!r} 冲突')
            if info.neq:
                # 缓存只有 <> 约束：列可取 NULL 或多个其他值，无法锁定等值
                return rejected('equality_under_neq',
                                f'{col} 仅有 <> 约束，无法证明其恒等于 {val!r}')
            if info.wants_null is False and info.rng is not None:
                if info.rng.contains(_eq_range(val)) and _is_point(info.rng, val):
                    return proved('range_point', f'{col} 的区间只含 {val!r}')
            return unknown('equality_underconstrained', f'{col} 缺少足够精确的等值约束')
        if op == '<>':
            if info.eq is not _UNSET:
                if info.eq != val:
                    return proved('nequality_constant', f'{col}={info.eq!r} 必然 <> {val!r}')
                return rejected('nequality_conflict', f'{col} 恒等于 {val!r}')
            if info.wants_null is False and info.rng is not None \
                    and info.rng.intersect(_eq_range(val)) is None:
                return proved('nequality_range', f'{col} 的区间与 {val!r} 不相交')
            # 仅有 <> 约束（或约束不足）：列可取多个值甚至 NULL，
            # 无法保证不等于该值就等于某个特定值，属明确反例而非不可判定。
            if info.neq:
                return rejected('nequality_too_weak',
                                f'{col} 仅有 <> 约束，无法推出等值 {val!r}（NULL 与其他值均为反例）')
            return unknown('nequality_underconstrained', f'{col} 的 <> 约束无法证明')
        # 有序比较：把查询子句在该列上的全部范围约束合并成一个目标区间，
        # 再判断缓存列的整体约束区间是否包含它（不能拿单条无界比较与整个缓存区间比）。
        if info.wants_null is not False:
            return unknown('null_state_unknown', f'需要 {col} 非 NULL 才能做区间包含')
        if info.rng is None:
            return unknown('no_range', f'缓存子句未给出 {col} 的区间')
        target_range = _column_range_from_clause(target_clause, col)
        if target_range is None:
            return unknown('range_unsatisfiable', f'查询在 {col} 上的区间约束矛盾或不可分析')
        if info.rng.contains(target_range):
            return proved('range_containment',
                          f'{col} 缓存区间 {info.rng} 包含查询合并区间 {target_range}')
        return rejected('range_exceeds',
                        f'{col} 缓存区间 {info.rng} 不包含查询合并区间 {target_range}')

    if isinstance(target, IsNotTrue):
        # 先尝试同叶子匹配
        for leaf in m.not_true_leaves:
            if _same_node(leaf, target.value):
                return proved('not_true_match', '缓存已显式登记 IS NOT TRUE 同一谓词')
        inner = target.value
        if isinstance(inner, IsNull):
            info = m.cols.get(inner.value.name)
            if info and info.wants_null is False:
                return proved('not_true_isnull', f'{inner.value.name} 非 NULL，IS NULL 不为 TRUE')
            if info and info.wants_null is True:
                return rejected('not_true_isnull', f'{inner.value.name} 为 NULL，IS NULL 为 TRUE')
        if isinstance(inner, IsNotNull):
            info = m.cols.get(inner.value.name)
            if info and info.wants_null is True:
                return proved('not_true_isnotnull', f'{inner.value.name} 为 NULL，IS NOT NULL 不为 TRUE')
            if info and info.wants_null is False:
                return rejected('not_true_isnotnull', f'{inner.value.name} 非 NULL，IS NOT NULL 为 TRUE')
        if isinstance(inner, Compare):
            col, op, val = _split_compare(inner)
            if col is not None:
                info = m.cols.get(col)
                if info and info.wants_null is True:
                    return proved('not_true_null', f'{col} 为 NULL，比较结果 UNKNOWN 不为 TRUE')
                if op == '=' and info is not None and info.eq is not _UNSET:
                    if info.eq != val:
                        return proved('not_true_equality', f'{col}={info.eq!r} 时 {col}={val!r} 为假')
                    return rejected('not_true_equality', f'{col} 恒等于 {val!r}，谓词为 TRUE')
        return unknown('not_true_underconstrained', 'IS NOT TRUE 内部谓词无法证明')

    return unknown('unsupported_literal', f'未登记的叶子类型 {target.kind}')


def _is_point(rng: Range, value: Any) -> bool:
    if isinstance(value, int):
        return rng.lower == value and rng.upper == value + 1
    return rng.lower == value and rng.upper == value


def _column_range_from_clause(clause: tuple[Node, ...], col: str) -> Optional[Range]:
    """合并目标子句在某列上的全部有序比较，返回该列被约束到的区间。"""
    rng: Range = all_range()
    for lit in clause:
        if isinstance(lit, Compare):
            c, op, val = _split_compare(lit)
            if c != col or op in ('=', '<>'):
                continue
            try:
                r = cmp_to_range(op, val)
            except TypeError:
                return None
            rng = rng.intersect(r)
            if rng is None:
                return None
    return rng


def _same_node(a: Node, b: Node) -> bool:
    from .ast_nodes import canonical_json
    return canonical_json(a) == canonical_json(b)


def clause_implies(clause: tuple[Node, ...], target_clause: tuple[Node, ...]) -> Proof:
    m = model_clause(clause)
    if m.impossible:
        return proved('vacuous_clause', '缓存子句自身矛盾（空集），蕴涵空真')
    if m.opaque:
        return unknown('opaque_clause', m.opaque_reason or '缓存子句含无法分析的结构')
    # 空合取子句 = 无任何约束（恒真）：任何非平凡查询叶子都有反例行。
    # 布尔 TRUE 叶子可证；其余一律 rejected（有明确反例），不升级为 unknown。
    if not clause:
        for target in target_clause:
            if isinstance(target, BooleanVal) and target.value is TRUE:
                continue
            return rejected('unconstrained_clause',
                            f'缓存子句无约束（恒真），无法保证叶子 {target.kind} 恒为 TRUE')
        return proved('tautology_implies_tautology', '恒真推出恒真')
    steps: list[Proof] = []
    for target in target_clause:
        step = _prove_literal_under(m, target, target_clause)
        steps.append(step)
        if step.verdict != PROVED:
            return step
    return proved('clause_all_literals', '查询子句的每个叶子都被缓存子句蕴涵', *steps)


def prove_implies(cached: Optional[Node], query: Optional[Node]) -> Proof:
    """证明 cached ⇒ query（3VL TRUE 保持）。"""
    if query is None:
        return proved('query_no_filter', '查询无过滤，任何缓存行都满足')
    dnf_c = to_dnf(cached)
    dnf_q = to_dnf(query)
    if not dnf_c:
        return proved('cached_unsatisfiable', '缓存谓词恒假，蕴涵空真')
    if not dnf_q:
        return rejected('query_unsatisfiable', '查询谓词恒假而缓存谓词非恒假')
    clause_reports: list[Proof] = []
    saw_unknown = False
    for cc in dnf_c:
        # 每个缓存分支只要蕴涵查询 DNF 的“任意一个”分支即可。
        # 汇总该缓存分支对所有查询分支的判定：有 proved 即成功；
        # 否则若存在 unknown 则整体 unknown（不能强行拒绝或接受）；
        # 全部 rejected 才说明该缓存分支是明确反例。
        branch_best: Optional[Proof] = None
        branch_saw_unknown = False
        for cq in dnf_q:
            r = clause_implies(cc, cq)
            if is_proved(r):
                branch_best = r
                break
            if r.verdict == UNKNOWN_VERDICT:
                branch_saw_unknown = True
            branch_best = r
        assert branch_best is not None
        if is_proved(branch_best):
            clause_reports.append(branch_best)
            continue
        if branch_saw_unknown:
            saw_unknown = True
            clause_reports.append(unknown(
                'branch_inconclusive',
                '该缓存分支对查询的所有分支均无法证明包含，且至少一个不可判定'))
        else:
            return rejected('clause_counterexample',
                            '存在缓存满足而查询不满足的合取分支', branch_best)
    if saw_unknown:
        return unknown('dnf_inconclusive', '部分合取分支无法在登记结构内证明', *clause_reports)
    return proved('dnf_all_clauses', '缓存 DNF 的每个分支都蕴涵查询的某个分支', *clause_reports)


# ---------------------------------------------------------------------------
# 互斥证明：两个谓词不可能同时为 TRUE（用于重叠缓存检测）
# ---------------------------------------------------------------------------

def prove_disjoint(p1: Optional[Node], p2: Optional[Node]) -> Proof:
    if p1 is None or p2 is None:
        return rejected('tautology_overlap', '恒真谓词与任何谓词都可能重叠')
    d1, d2 = to_dnf(p1), to_dnf(p2)
    if not d1 or not d2:
        return proved('unsat_operand', '其中一个谓词恒假，必然互斥')
    reports: list[Proof] = []
    saw_unknown = False
    for a in d1:
        for b in d2:
            r = _conjunction_unsat(a, b)
            reports.append(r)
            if r.verdict != PROVED:
                if r.verdict == UNKNOWN_VERDICT:
                    saw_unknown = True
                else:
                    return rejected('possible_overlap', '存在可能同时满足的合取分支', r)
    if saw_unknown:
        return unknown('disjoint_inconclusive', '互斥性无法完全证明', *reports)
    return proved('all_pairs_unsat', '所有 DNF 分支两两矛盾', *reports)


def _conjunction_unsat(a: tuple[Node, ...], b: tuple[Node, ...]) -> Proof:
    m = model_clause(a + b)
    if m.impossible:
        return proved('direct_contradiction', '合取后出现 NULL/非 NULL、等值或区间矛盾')
    if m.opaque:
        return unknown('opaque_clause', m.opaque_reason or '含无法分析的结构')
    # 合并后无矛盾即可能同时满足
    return rejected('no_contradiction', '合取分支可同时满足')


def complement_true(pred: Optional[Node]) -> Node:
    """P IS NOT TRUE 的 AST 构造：补偿扫描的 3VL 安全补集。

    与 NOT(P) 不同，NULL 行也会被正确保留在补集中（UNKNOWN 不是 TRUE）。
    """
    if pred is None:
        return BooleanVal(False)
    return IsNotTrue(pred)


def is_contradiction(node: Optional[Node]) -> bool:
    """纯结构判定：谓词是否恒假（DNF 为空）。"""
    return not to_dnf(node)


def is_tautology(node: Optional[Node]) -> bool:
    """纯结构判定：谓词是否恒真（DNF 只剩空子句）。"""
    dnf = to_dnf(node)
    return dnf == [()]


# ---------------------------------------------------------------------------
# 过滤列保留检查：缓存可更细，但未保留在分组键/可证明常量中的过滤列不能再筛选
# ---------------------------------------------------------------------------

def check_filter_columns(query_pred: Optional[Node],
                         cache_groups: frozenset[str],
                         cache_pred: Optional[Node]) -> Proof:
    used = predicate_columns(query_pred)
    constants = equality_constants(cache_pred)
    missing = sorted(
        c for c in used
        if c not in cache_groups and c not in constants
    )
    if missing:
        return rejected('filter_column_dropped',
                        f'过滤列 {missing} 既不在缓存分组键 {sorted(cache_groups)} 中，'
                        '也不是缓存谓词内可证明的常量列，复用后无法再按该列筛选')
    return proved('filter_columns_retained',
                  '所有查询过滤列均保留在缓存分组键或可证明常量中')
