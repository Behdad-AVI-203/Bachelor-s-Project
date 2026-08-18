"""Metric calculations shared by network and comparison reporting."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from statistics import fmean, pvariance
from typing import Any

DEFAULT_COMPARISON_WEIGHTS = {
    "network_performance": 0.25,
    "incentive_effectiveness": 0.75,
}


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


def weighted_score_comparison(
    metrics_by_category: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    weights: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Score category-normalized metric wins with configurable weights."""
    configured_weights = dict(weights or DEFAULT_COMPARISON_WEIGHTS)
    category_scores: dict[str, dict[str, Any]] = {}
    total_weight = 0.0
    combined_a = 0.0
    combined_b = 0.0

    for category, category_metrics in metrics_by_category.items():
        prepared = list(category_metrics)
        if not prepared:
            continue
        weight = max(0.0, float(configured_weights.get(category, 0.0)))
        if weight == 0:
            continue
        raw_a, raw_b, raw_winner = score_comparison(prepared)
        scored_count = sum(
            metric.get("winner_slot") in {"A", "B", "TIE"}
            for metric in prepared
        )
        if scored_count == 0:
            continue
        normalized_a = raw_a / scored_count
        normalized_b = raw_b / scored_count
        category_scores[category] = {
            "score_a": normalized_a,
            "score_b": normalized_b,
            "winner_slot": raw_winner,
            "metric_count": scored_count,
            "weight": weight,
        }
        total_weight += weight
        combined_a += weight * normalized_a
        combined_b += weight * normalized_b

    if total_weight:
        combined_a /= total_weight
        combined_b /= total_weight
    if abs(combined_a - combined_b) < 1e-12:
        winner = "TIE"
    else:
        winner = "A" if combined_a > combined_b else "B"

    return {
        "score_a": combined_a,
        "score_b": combined_b,
        "winner_slot": winner,
        "weights": configured_weights,
        "categories": category_scores,
    }
