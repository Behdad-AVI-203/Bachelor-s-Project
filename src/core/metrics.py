"""Metric calculations shared by network and comparison reporting."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from statistics import fmean, pvariance
from typing import Any


def gini_coefficient(values: Iterable[float]) -> float:
    """Calculate a non-negative Gini coefficient."""
    sorted_values = sorted(max(0.0, float(value)) for value in values)
    count = len(sorted_values)
    if count == 0:
        return 0.0
    total = sum(sorted_values)
    if total == 0:
        return 0.0

    weighted_sum = sum(
        index * value
        for index, value in enumerate(sorted_values, start=1)
    )
    return (2 * weighted_sum) / (count * total) - (count + 1) / count


def balance_variance(values: Iterable[float]) -> float:
    """Calculate population variance, returning zero for small samples."""
    prepared = [float(value) for value in values]
    if len(prepared) < 2:
        return 0.0
    return float(pvariance(prepared))


def average(values: Iterable[float]) -> float | None:
    """Return a floating-point mean or ``None`` for an empty collection."""
    prepared = [float(value) for value in values]
    return float(fmean(prepared)) if prepared else None


def compare_metric(
    *,
    metric_name: str,
    display_name: str,
    value_a: float | int | None,
    value_b: float | int | None,
    preferred_direction: str,
    unit: str | None = None,
) -> dict[str, Any]:
    """Build one normalized A/B comparison record."""
    numeric_a = float(value_a) if value_a is not None else None
    numeric_b = float(value_b) if value_b is not None else None
    absolute_delta = (
        numeric_a - numeric_b
        if numeric_a is not None and numeric_b is not None
        else None
    )
    relative_delta = None
    if absolute_delta is not None and numeric_b not in {None, 0}:
        relative_delta = absolute_delta / abs(numeric_b)

    winner = None
    if numeric_a is not None and numeric_b is not None:
        if abs(numeric_a - numeric_b) < 1e-12:
            winner = "TIE"
        elif preferred_direction == "higher":
            winner = "A" if numeric_a > numeric_b else "B"
        elif preferred_direction == "lower":
            winner = "A" if numeric_a < numeric_b else "B"

    return {
        "metric_name": metric_name,
        "display_name": display_name,
        "unit": unit,
        "value_a": numeric_a,
        "value_b": numeric_b,
        "absolute_delta": absolute_delta,
        "relative_delta": relative_delta,
        "preferred_direction": preferred_direction,
        "winner_slot": winner,
        "details_json": {},
    }


def score_comparison(
    metrics: Iterable[Mapping[str, Any]],
) -> tuple[float, float, str]:
    """Score each network by its count of metric wins."""
    score_a = 0.0
    score_b = 0.0
    for metric in metrics:
        if metric.get("winner_slot") == "A":
            score_a += 1
        elif metric.get("winner_slot") == "B":
            score_b += 1
        elif metric.get("winner_slot") == "TIE":
            score_a += 0.5
            score_b += 0.5

    if score_a == score_b:
        winner = "TIE"
    else:
        winner = "A" if score_a > score_b else "B"
    return score_a, score_b, winner
