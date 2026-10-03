"""Holdout fence for downloads: the last day a research download may contain."""

from __future__ import annotations

from datetime import date, timedelta

from hedge_fund.validation.holdout_guard import sealed_windows_in_force


def last_research_day(market: str = "*", default: str = "2100-01-01") -> str:
    """The day before the earliest sealed window in force for *market* (fence start,
    embargo included). "*" = every holdout applies."""
    starts = [lo for lo, _ in sealed_windows_in_force(market)]
    if not starts:
        return default
    return (date.fromisoformat(min(starts)) - timedelta(days=1)).isoformat()
