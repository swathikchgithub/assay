"""Robust statistics for the temporal invariants.

Median/MAD rather than mean/stdev: a single restatement or outage spike would
drag a mean-based band wide enough to hide the next one.
"""

from __future__ import annotations

from statistics import median
from typing import Sequence

MAD_TO_SIGMA = 1.4826  # consistency constant for normally distributed data


def mad(values: Sequence[float]) -> float:
    """Median absolute deviation. Time: O(n log n). Space: O(n)."""
    if not values:
        return 0.0
    centre = median(values)
    return median([abs(v - centre) for v in values])


def envelope(values: Sequence[float], k: float) -> tuple[float, float]:
    """Robust band of half-width k sigma around the median.

    Falls back to a proportional band when MAD is zero (a perfectly flat
    series), which otherwise produces a zero-width band that fails on any
    movement at all.
    """
    if not values:
        return (0.0, 0.0)
    centre = median(values)
    spread = mad(values) * MAD_TO_SIGMA
    if spread == 0:
        spread = abs(centre) * 0.05
    return (centre - k * spread, centre + k * spread)
