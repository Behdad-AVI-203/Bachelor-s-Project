"""Metric calculations shared by network and comparison reporting."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from statistics import fmean, pvariance
from typing import Any

DEFAULT_COMPARISON_WEIGHTS = {
    "network_performance": 0.25,
    "incentive_effectiveness": 0.75,
}

DEFAULT_INCENTIVE_DIMENSION_WEIGHTS = {
    "participation": 0.25,
    "retention": 0.25,
    "useful_contribution": 0.20,
    "incentive_efficiency": 0.15,
    "fairness": 0.15,
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


def weighted_incentive_effectiveness(
    metrics: Iterable[Mapping[str, Any]],
    *,
    weights: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Score incentive arms using one normalized metric per outcome dimension."""
    configured = dict(weights or DEFAULT_INCENTIVE_DIMENSION_WEIGHTS)
    selected: dict[str, Mapping[str, Any]] = {}
    for metric in metrics:
        dimension = metric.get("details_json", {}).get("dimension")
        if dimension and dimension not in selected:
            selected[str(dimension)] = metric

    score_a = score_b = total_weight = 0.0
    dimensions: dict[str, dict[str, Any]] = {}
    for dimension, metric in selected.items():
        weight = max(0.0, float(configured.get(dimension, 0.0)))
        value_a = metric.get("value_a")
        value_b = metric.get("value_b")
        if weight == 0 or value_a is None or value_b is None:
            continue
        numeric_a, numeric_b = float(value_a), float(value_b)
        if abs(numeric_a - numeric_b) < 1e-12:
            normalized_a = normalized_b = 0.5
        else:
            low, high = min(numeric_a, numeric_b), max(numeric_a, numeric_b)
            normalized_a = (numeric_a - low) / (high - low)
            normalized_b = (numeric_b - low) / (high - low)
            if metric.get("preferred_direction") == "lower":
                normalized_a, normalized_b = 1 - normalized_a, 1 - normalized_b
        score_a += weight * normalized_a
        score_b += weight * normalized_b
        total_weight += weight
        dimensions[dimension] = {
            "score_a": normalized_a,
            "score_b": normalized_b,
            "weight": weight,
            "metric_name": metric.get("metric_name"),
        }

    if total_weight:
        score_a /= total_weight
        score_b /= total_weight
    winner = (
        "TIE"
        if abs(score_a - score_b) < 1e-12
        else ("A" if score_a > score_b else "B")
    )
    return {
        "score_a": score_a,
        "score_b": score_b,
        "winner_slot": winner,
        "weights": configured,
        "dimensions": dimensions,
    }
