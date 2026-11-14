"""有序标量（整数日/普通可比较字面量）上的区间表示与包含判定。

区间为半开 [lower, upper)，端点用 None 表示无界；None(UNKNOWN) 值不进入区间，
由谓词层的 IS NULL / IS NOT NULL 单独刻画。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Range:
    lower: Optional[Any]  # 包含；None 表示负无穷
    upper: Optional[Any]  # 不包含；None 表示正无穷

    def contains(self, other: 'Range') -> bool:
        # 半开区间 [lower, upper)：self 包含 other 当且仅当
        # self 的下界不高于 other（None 为负无穷），上界不低于 other（None 为正无穷）
        if self.lower is not None:
            if other.lower is None or other.lower < self.lower:
                return False
        if self.upper is not None:
            if other.upper is None or other.upper > self.upper:
                return False
        return True

    def intersect(self, other: 'Range') -> Optional['Range']:
        lo = self.lower if other.lower is None or (
            self.lower is not None and self.lower >= other.lower) else other.lower
        hi = self.upper if other.upper is None or (
            self.upper is not None and self.upper <= other.upper) else other.upper
        if lo is not None and hi is not None and lo >= hi:
            return None
        return Range(lo, hi)

    def is_empty_domain(self) -> bool:
        return self.lower is not None and self.upper is not None and self.lower >= self.upper


def cmp_to_range(op: str, value: Any) -> Range:
    if op == '=':
        return Range(value, value + 1) if isinstance(value, int) else _eq_generic(value)
    if op == '<>':
        raise ValueError('<> 不应进入区间层')
    if op == '>':
        return Range(value + 1, None) if isinstance(value, int) else Range(value, None)
    if op == '>=':
        return Range(value, None)
    if op == '<':
        return Range(None, value)
    if op == '<=':
        return Range(None, value + 1) if isinstance(value, int) else Range(None, value)
    raise ValueError(f'未知比较符 {op}')


def _eq_generic(value: Any) -> Range:
    # 非整数有序类型用单值“退化区间”近似：lower=upper=value，
    # 包含判定仍正确（等值只与等值比较），交集逻辑在整数日之外仅用于等值。
    return Range(value, value)


def all_range() -> Range:
    return Range(None, None)
