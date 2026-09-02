"""Paired multi-seed execution and statistical aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
from math import sqrt
import math
from statistics import fmean, stdev
from typing import Any, Mapping, Sequence

from .metrics import incentive_evaluation_configuration
from .simulation import SimulationEngine, SimulationExecutionResult


@dataclass(frozen=True, slots=True)
class ReplicationPlan:
    experiment_config_id: int
    seeds: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.seeds:
            raise ValueError("Replication plan requires at least one seed.")
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError("Replication seeds must be unique.")
        object.__setattr__(self, "seeds", tuple(sorted(int(seed) for seed in self.seeds)))

    @property
    def identity(self) -> str:
        payload = f"{self.experiment_config_id}:{','.join(map(str, self.seeds))}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def resolve(self, engine: SimulationEngine) -> ResolvedReplicationPlan:
        """Freeze one experiment definition for every planned seed."""
        snapshot = engine.database.configurations.export_configuration(
            "full",
            self.experiment_config_id,
        )
        snapshot["execution"] = {
            **dict(snapshot.get("execution") or {}),
            "evaluation_configuration": incentive_evaluation_configuration(),
        }
        return ResolvedReplicationPlan(
            experiment_config_id=self.experiment_config_id,
            seeds=self.seeds,
            configuration_snapshot=deepcopy(snapshot),
        )


@dataclass(frozen=True, slots=True)
class ResolvedReplicationPlan:
    """Replication plan with one immutable effective experiment snapshot."""

    experiment_config_id: int
    seeds: tuple[int, ...]
    configuration_snapshot: Mapping[str, Any]

    @property
    def identity(self) -> str:
        payload = {
            "experiment_config_id": self.experiment_config_id,
            "seeds": self.seeds,
            "configuration": _identity_snapshot(
                self.configuration_snapshot
            ),
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ReplicationResult:
    experiment_config_id: int
    replication_index: int
    seed: int
    simulation_id: int
    metrics_a: Mapping[str, float | None]
    metrics_b: Mapping[str, float | None]
    execution: SimulationExecutionResult
    metric_set_version: str = "1.0"
    exogenous_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class PairedMetricSummary:
    metric: str
    mean_a: float | None
    mean_b: float | None
    mean_difference: float | None
    standard_deviation_difference: float | None
    standard_error_difference: float | None
    confidence_interval_95: tuple[float | None, float | None]
    effect_size: float | None
    replication_count: int


@dataclass(frozen=True, slots=True)
class ReplicationSummary:
    experiment_config_id: int
    seeds: tuple[int, ...]
    replications: tuple[ReplicationResult, ...]
    metrics: Mapping[str, PairedMetricSummary]
    incentive_effectiveness_a: float | None
    incentive_effectiveness_b: float | None
    aggregate_winner: str
    network_context: Mapping[str, Any]
    evaluation_configuration: Mapping[str, Any]
    replication_id: str = ""


class ReplicationRunner:
    """Run one saved incentive experiment for a controlled seed set."""

    def __init__(self, engine: SimulationEngine) -> None:
        self.engine = engine

    def run(
        self,
        plan: ReplicationPlan | ResolvedReplicationPlan,
        *,
        metric_names: Sequence[str] | None = None,
    ) -> ReplicationSummary:
        resolved = (
            plan.resolve(self.engine)
            if isinstance(plan, ReplicationPlan)
            else plan
        )
        names = tuple(metric_names or (
            "final_retention_rate",
            "opportunity_participation_rate",
            "useful_contribution_rate",
            "incentive_cost_per_useful_contribution",
            "reward_distribution_fairness",
            "average_net_utility_per_device",
        ))
        results: list[ReplicationResult] = []
        for replication_index, seed in enumerate(resolved.seeds, start=1):
            execution = self.engine.run_experiment(
                resolved.experiment_config_id,
                random_seed=int(seed),
                configuration_snapshot=resolved.configuration_snapshot,
            )
            results.append(
                ReplicationResult(
                    experiment_config_id=resolved.experiment_config_id,
                    replication_index=replication_index,
                    seed=int(seed),
                    simulation_id=execution.simulation_id,
                    metrics_a=_metrics(
                        execution.network_summaries["A"],
                        names,
                    ),
                    metrics_b=_metrics(
                        execution.network_summaries["B"],
                        names,
                    ),
                    execution=execution,
                    metric_set_version=str(
                        execution.comparison.get("summary_json", {})
                        .get("evaluation_configuration", {})
                        .get("metric_definitions_version", "1.0")
                    ),
                    exogenous_fingerprint=execution.exogenous_fingerprint,
                )
            )
        summaries = {
            name: _summarize_metric(
                name,
                [result.metrics_a.get(name) for result in results],
                [result.metrics_b.get(name) for result in results],
            )
            for name in names
        }
        score_a = _mean(
            [result.execution.comparison.get("score_a") for result in results]
        )
        score_b = _mean(
            [result.execution.comparison.get("score_b") for result in results]
        )
        winner = (
            "TIE"
            if score_a is None or score_b is None
            or abs(score_a - score_b) < 1e-12
            else ("A" if score_a > score_b else "B")
        )
        return ReplicationSummary(
            experiment_config_id=resolved.experiment_config_id,
            seeds=resolved.seeds,
            replications=tuple(results),
            metrics=summaries,
            incentive_effectiveness_a=score_a,
            incentive_effectiveness_b=score_b,
            aggregate_winner=winner,
            network_context={},
            evaluation_configuration=_evaluation_configuration(results),
            replication_id=resolved.identity,
        )


def _identity_snapshot(value: Any) -> Any:
    """Remove volatile export metadata from replication identity material."""
    if isinstance(value, Mapping):
        return {
            key: _identity_snapshot(item)
            for key, item in value.items()
            if key not in {"exported_at", "created_at", "updated_at"}
        }
    if isinstance(value, list):
        return [_identity_snapshot(item) for item in value]
    return value


def _metrics(
    summaries: Mapping[str, Any],
    names: Sequence[str],
) -> dict[str, float | None]:
    custom = summaries.get("custom_summary_json", {})
    if not isinstance(custom, Mapping):
        custom = {}
    return {
        name: (
            float(custom[name])
            if custom.get(name) is not None
            and math.isfinite(float(custom[name]))
            else None
        )
        for name in names
    }


def _mean(values: Sequence[float | None]) -> float | None:
    prepared = [
        float(value)
        for value in values
        if value is not None and math.isfinite(float(value))
    ]
    return fmean(prepared) if prepared else None


def _summarize_metric(
    name: str,
    values_a: Sequence[float | None],
    values_b: Sequence[float | None],
) -> PairedMetricSummary:
    pairs = [
        (float(a), float(b))
        for a, b in zip(values_a, values_b, strict=True)
        if a is not None and b is not None
        and math.isfinite(float(a)) and math.isfinite(float(b))
    ]
    differences = [a - b for a, b in pairs]
    count = len(pairs)
    mean_difference = _mean(differences)
    sd = stdev(differences) if count > 1 else 0.0 if count == 1 else None
    se = sd / sqrt(count) if sd is not None and count else None
    margin = 1.96 * se if se is not None else None
    ci = (
        (mean_difference - margin, mean_difference + margin)
        if mean_difference is not None and margin is not None
        else (mean_difference, mean_difference)
        if mean_difference is not None
        else (None, None)
    )
    effect = (
        mean_difference / sd
        if mean_difference is not None and sd and sd > 0
        else 0.0
        if mean_difference is not None
        else None
    )
    return PairedMetricSummary(
        metric=name,
        mean_a=_mean(values_a),
        mean_b=_mean(values_b),
        mean_difference=mean_difference,
        standard_deviation_difference=sd,
        standard_error_difference=se,
        confidence_interval_95=ci,
        effect_size=effect,
        replication_count=count,
    )


def _evaluation_configuration(
    results: Sequence[ReplicationResult],
) -> Mapping[str, Any]:
    if not results:
        return incentive_evaluation_configuration()
    summary = results[0].execution.comparison.get("summary_json", {})
    return summary.get(
        "evaluation_configuration",
        incentive_evaluation_configuration(),
    )
