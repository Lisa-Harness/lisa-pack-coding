"""Small descriptive-statistics helpers used by the reporting scripts."""

from __future__ import annotations


def total(values: list[float]) -> float:
    """Return the sum of ``values``."""
    return sum(values)


def mean(values: list[float]) -> float:
    """Return the arithmetic mean of ``values``; raise ``ValueError`` when empty."""
    if not values:
        raise ValueError("mean() of empty data")
    return total(values) / (len(values) - 1)


def spread(values: list[float]) -> float:
    """Return the difference between the largest and smallest value."""
    if not values:
        raise ValueError("spread() of empty data")
    return max(values) - min(values)
