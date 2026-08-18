"""UI-facing read models for legacy and incentive comparisons."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


INCENTIVE_COMPARISON = "incentive_mechanisms"
LEGACY_COMPARISON = "legacy_networks"


@dataclass(frozen=True, slots=True)
class ComparisonLabels:
    """Display labels for the two arms of a comparison."""

    arm_a: str
    arm_b: str
    arm_a_role: str
    arm_b_role: str
    score_a: str
    score_b: str
    winner: str
    shared_network: str

    def to_dict(self) -> dict[str, str]:
        """Return labels in a template-friendly form."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ComparisonSummary:
    """Stable UI summary independent of database table terminology."""

    comparison_model: str
    environment_name: str
    shared_network_name: str | None
    incentive_a_name: str | None
    incentive_b_name: str | None
    network_a_name: str | None
    network_b_name: str | None
    labels: ComparisonLabels

    @property
    def is_legacy(self) -> bool:
        """Return whether this summary represents a legacy network comparison."""
        return self.comparison_model != INCENTIVE_COMPARISON

    @property
    def arm_a_name(self) -> str:
        """Return the display name for arm A."""
        return self.labels.arm_a

    @property
    def arm_b_name(self) -> str:
        """Return the display name for arm B."""
        return self.labels.arm_b

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable representation for Streamlit pages."""
        return {
            "comparison_model": self.comparison_model,
            "is_legacy": self.is_legacy,
            "environment_name": self.environment_name,
            "shared_network_name": self.shared_network_name,
            "incentive_a_name": self.incentive_a_name,
            "incentive_b_name": self.incentive_b_name,
            "network_a_name": self.network_a_name,
            "network_b_name": self.network_b_name,
            "arm_a_name": self.arm_a_name,
            "arm_b_name": self.arm_b_name,
            "labels": self.labels.to_dict(),
        }


def comparison_summary_from_experiment(
    experiment: Mapping[str, Any],
) -> ComparisonSummary:
    """Build a summary from a saved experiment read model."""
    model = _comparison_model(experiment)
    environment = _mapping(experiment.get("environment"))
    environment_name = _first_text(
        environment.get("name"),
        experiment.get("environment_name"),
        "Environment",
    )

    if model == INCENTIVE_COMPARISON:
        network = _mapping(experiment.get("network"))
        incentive_a = _mapping(experiment.get("incentive_a"))
        incentive_b = _mapping(experiment.get("incentive_b"))
        shared_network_name = _first_text(
            network.get("name"),
            experiment.get("shared_network_name"),
            experiment.get("network_name"),
            "Shared network",
        )
        incentive_a_name = _first_text(
            incentive_a.get("name"),
            experiment.get("incentive_a_name"),
            "Incentive A",
        )
        incentive_b_name = _first_text(
            incentive_b.get("name"),
            experiment.get("incentive_b_name"),
            "Incentive B",
        )
        labels = ComparisonLabels(
            arm_a=incentive_a_name,
            arm_b=incentive_b_name,
            arm_a_role="Incentive A",
            arm_b_role="Incentive B",
            score_a=f"{incentive_a_name} score",
            score_b=f"{incentive_b_name} score",
            winner="Winning incentive",
            shared_network=shared_network_name,
        )
        return ComparisonSummary(
            comparison_model=model,
            environment_name=environment_name,
            shared_network_name=shared_network_name,
            incentive_a_name=incentive_a_name,
            incentive_b_name=incentive_b_name,
            network_a_name=None,
            network_b_name=None,
            labels=labels,
        )

    network_a = _mapping(experiment.get("network_a"))
    network_b = _mapping(experiment.get("network_b"))
    network_a_name = _first_text(
        network_a.get("name"),
        experiment.get("network_a_name"),
        "Network A",
    )
    network_b_name = _first_text(
        network_b.get("name"),
        experiment.get("network_b_name"),
        "Network B",
    )
    labels = ComparisonLabels(
        arm_a=network_a_name,
        arm_b=network_b_name,
        arm_a_role="Network A",
        arm_b_role="Network B",
        score_a=f"{network_a_name} score",
        score_b=f"{network_b_name} score",
        winner="Winning network",
        shared_network="Shared network",
    )
    return ComparisonSummary(
        comparison_model=model,
        environment_name=environment_name,
        shared_network_name=None,
        incentive_a_name=None,
        incentive_b_name=None,
        network_a_name=network_a_name,
        network_b_name=network_b_name,
        labels=labels,
    )


def comparison_summary_from_results(
    results: Mapping[str, Any],
) -> ComparisonSummary:
    """Build a summary from the existing results read model."""
    run = _mapping(results.get("run"))
    snapshot = _mapping(run.get("configuration_snapshot_json"))
    if snapshot:
        snapshot_source = _experiment_source_from_snapshot(snapshot)
        if snapshot_source and _has_comparison_arms(snapshot_source):
            return comparison_summary_from_experiment(snapshot_source)

    networks = results.get("networks")
    if not isinstance(networks, list):
        networks = results.get("arms")
    network_rows = [
        _mapping(row) for row in networks or [] if isinstance(row, Mapping)
    ]
    source: dict[str, Any] = {
        "comparison_model": run.get("comparison_model", LEGACY_COMPARISON),
        "environment_name": run.get("environment_name"),
    }
    if source["comparison_model"] == INCENTIVE_COMPARISON:
        source["network"] = _network_from_arm(network_rows, "A")
        source["incentive_a"] = _incentive_from_arm(network_rows, "A")
        source["incentive_b"] = _incentive_from_arm(network_rows, "B")
    else:
        source["network_a"] = _network_from_arm(network_rows, "A")
        source["network_b"] = _network_from_arm(network_rows, "B")
    return comparison_summary_from_experiment(source)


def comparison_summary_from_history_row(
    row: Mapping[str, Any],
) -> ComparisonSummary:
    """Build a summary from a history row, including future enriched fields."""
    source = dict(row)
    model = _comparison_model(source)
    source["comparison_model"] = model
    if model == INCENTIVE_COMPARISON:
        source["network"] = {
            "name": source.get("shared_network_name")
            or source.get("network_name")
        }
        source["incentive_a"] = {"name": source.get("incentive_a_name")}
        source["incentive_b"] = {"name": source.get("incentive_b_name")}
    else:
        source["network_a"] = {"name": source.get("network_a_name")}
        source["network_b"] = {"name": source.get("network_b_name")}
    return comparison_summary_from_experiment(source)


def _experiment_source_from_snapshot(
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Flatten an immutable experiment snapshot for summary construction."""
    experiment = dict(_mapping(snapshot.get("experiment")))
    model = _comparison_model(experiment)
    environment_bundle = _mapping(snapshot.get("environment_bundle"))
    environment = _mapping(environment_bundle.get("environment"))
    experiment["environment"] = environment
    experiment["comparison_model"] = model

    if model == INCENTIVE_COMPARISON:
        network_bundle = _mapping(snapshot.get("network_bundle"))
        incentive_a_bundle = _mapping(snapshot.get("incentive_a_bundle"))
        incentive_b_bundle = _mapping(snapshot.get("incentive_b_bundle"))
        experiment["network"] = _mapping(network_bundle.get("network"))
        experiment["incentive_a"] = _mapping(
            incentive_a_bundle.get("incentive")
        )
        experiment["incentive_b"] = _mapping(
            incentive_b_bundle.get("incentive")
        )
    else:
        network_a_bundle = _mapping(snapshot.get("network_a_bundle"))
        network_b_bundle = _mapping(snapshot.get("network_b_bundle"))
        experiment["network_a"] = _mapping(network_a_bundle.get("network"))
        experiment["network_b"] = _mapping(network_b_bundle.get("network"))
    return experiment


def _network_from_arm(
    network_rows: list[Mapping[str, Any]],
    slot: str,
) -> dict[str, Any]:
    for row in network_rows:
        if row.get("network_slot") == slot:
            return {"name": row.get("network_name") or row.get("name")}
    return {}


def _incentive_from_arm(
    network_rows: list[Mapping[str, Any]],
    slot: str,
) -> dict[str, Any]:
    for row in network_rows:
        if row.get("network_slot") != slot:
            continue
        snapshot = _mapping(row.get("configuration_snapshot_json"))
        bundle = _mapping(snapshot.get("incentive_bundle"))
        return _mapping(bundle.get("incentive"))
    return {}


def _comparison_model(source: Mapping[str, Any]) -> str:
    model = source.get("comparison_model")
    if model:
        return str(model)
    if any(
        source.get(key) is not None
        for key in ("incentive_a_config_id", "incentive_b_config_id")
    ):
        return INCENTIVE_COMPARISON
    return LEGACY_COMPARISON


def _has_comparison_arms(source: Mapping[str, Any]) -> bool:
    """Return whether a source contains enough data for snapshot resolution."""
    model = _comparison_model(source)
    if model == INCENTIVE_COMPARISON:
        return any(
            source.get(key)
            for key in ("network", "incentive_a", "incentive_b")
        )
    return any(source.get(key) for key in ("network_a", "network_b"))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
