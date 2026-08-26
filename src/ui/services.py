"""Database-backed services used by Streamlit pages."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

import pandas as pd
import streamlit as st

from src.core import (
    DeviceGroupConfig,
    IoTEnvironmentDefinition,
)
from src.core.errors import PluginValidationError
from src.core.plugins import load_plugin_function
from src.database import Database, DatabaseService, RecordNotFoundError

from .config import DATABASE_PATH
from .read_models import (
    ComparisonSummary,
    comparison_summary_from_experiment,
    comparison_summary_from_history_row,
    comparison_summary_from_results,
)

DEFAULT_REWARD_CODE = """def calculate_reward(context):
    device = context["device"]
    state = context["device_state"]
    base_reward = context["network"]["parameters"].get(
        "base_iot_reward",
        1.0,
    )
    feedback_weight = context["network"]["parameters"].get(
        "feedback_weight",
        0.1,
    )
    return max(
        0.0,
        base_reward * device["precision"]
        + feedback_weight * state["feedback_score"],
    )
"""

NETWORK_TEMPLATES = {
    "Default PoW": {
        "description": (
            "Balanced proof-of-work settings with precision and feedback "
            "based rewards."
        ),
        "difficulty": 2,
        "block_capacity": 30,
        "block_interval_ms": 1_000,
        "fee_rate": 0.01,
        "base_mining_time_ms": 50,
        "base_iot_reward": 1.0,
        "feedback_weight": 0.1,
        "reward_code": DEFAULT_REWARD_CODE,
        "logic_code": "",
    },
    "Low difficulty": {
        "description": (
            "Fast block production for throughput-oriented experiments."
        ),
        "difficulty": 1,
        "block_capacity": 40,
        "block_interval_ms": 500,
        "fee_rate": 0.005,
        "base_mining_time_ms": 25,
        "base_iot_reward": 0.8,
        "feedback_weight": 0.05,
        "reward_code": DEFAULT_REWARD_CODE,
        "logic_code": "",
    },
    "Feedback weighted": {
        "description": (
            "Rewards place stronger emphasis on confirmed feedback scores."
        ),
        "difficulty": 2,
        "block_capacity": 25,
        "block_interval_ms": 1_000,
        "fee_rate": 0.01,
        "base_mining_time_ms": 50,
        "base_iot_reward": 0.7,
        "feedback_weight": 0.35,
        "reward_code": DEFAULT_REWARD_CODE,
        "logic_code": "",
    },
}

INCENTIVE_TEMPLATES = {
    "Default reward": {
        "description": "Precision-weighted baseline incentive.",
        "implementation_type": "built_in",
        "built_in_key": "default_reward",
        "parameters": {
            "base_iot_reward": 1.0,
            "feedback_weight": 0.1,
        },
    },
    "Participation-first": {
        "description": "A built-in profile intended to favor retention.",
        "implementation_type": "built_in",
        "built_in_key": "participation_first",
        "parameters": {
            "base_iot_reward": 1.0,
            "feedback_weight": 0.1,
            "participation_bonus": 0.25,
        },
    },
    "Fairness-aware": {
        "description": "A built-in profile reserved for fairness-oriented rules.",
        "implementation_type": "built_in",
        "built_in_key": "fairness_aware",
        "parameters": {
            "base_iot_reward": 1.0,
            "feedback_weight": 0.1,
            "fairness_weight": 0.25,
        },
    },
}

DEFAULT_INCENTIVE_CODE = """def evaluate(context):
    device = context["device"]
    parameters = context.get("incentive_parameters", {})
    base_reward = parameters.get("base_iot_reward", 1.0)
    return {
        "reward_delta": base_reward * device.get("precision", 1.0),
        "contribution_delta": 1.0,
        "participation_signal": 0.1,
    }
"""


@st.cache_resource
def get_database() -> DatabaseService:
    """Return the shared UI database facade."""
    database = Database(DATABASE_PATH)
    database.initialize()
    return database


def list_incentives(database: DatabaseService) -> list[dict[str, Any]]:
    """Return independent incentive configurations and artifact metadata."""
    return database.store.execute_query(
        """
        SELECT
            incentive.id,
            incentive.name,
            incentive.version,
            incentive.description,
            incentive.implementation_type,
            incentive.built_in_key,
            incentive.code_artifact_id,
            incentive.parameters_json,
            incentive.metadata_json,
            incentive.created_at,
            COALESCE(artifact.created_at, incentive.created_at)
                AS updated_at,
            artifact.name AS artifact_name,
            artifact.entrypoint AS artifact_entrypoint,
            artifact.validation_status
        FROM incentive_mechanism_configs AS incentive
        LEFT JOIN code_artifacts AS artifact
            ON artifact.id = incentive.code_artifact_id
        ORDER BY incentive.created_at DESC, incentive.id DESC
        """
    )


def get_incentive_editor_data(
    database: DatabaseService,
    incentive_id: int,
) -> dict[str, Any]:
    """Load one incentive and its optional source artifact."""
    incentive = database.configurations.get_incentive_config(incentive_id)
    artifacts = incentive.get("code_artifacts") or []
    artifact = next(
        (
            item
            for item in artifacts
            if item.get("id") == incentive.get("code_artifact_id")
        ),
        None,
    )
    return {
        **incentive,
        "entrypoint": artifact.get("entrypoint") if artifact else "evaluate",
        "source_code": artifact.get("source_code")
        if artifact
        else DEFAULT_INCENTIVE_CODE,
        "artifact": artifact,
    }


def save_incentive(
    database: DatabaseService,
    *,
    name: str,
    version: int,
    description: str | None,
    implementation_type: str,
    built_in_key: str | None,
    source_code: str,
    entrypoint: str,
    parameters: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    incentive_id: int | None = None,
) -> int:
    """Validate and create or update an independent incentive."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Incentive name is required.")
    if version <= 0:
        raise ValueError("Incentive version must be positive.")
    if implementation_type not in {
        "built_in",
        "custom",
        "legacy_reward",
    }:
        raise ValueError("Unsupported incentive implementation type.")

    clean_key = built_in_key.strip() if built_in_key else None
    clean_source = source_code or ""
    clean_entrypoint = entrypoint.strip() or "evaluate"
    if implementation_type == "built_in":
        if not clean_key:
            raise ValueError("Built-in incentives require a mechanism key.")
        artifact_id = None
    else:
        if not clean_source.strip():
            raise ValueError("Custom and legacy incentives require Python code.")
        try:
            plugin = load_plugin_function(
                clean_source,
                clean_entrypoint,
                plugin_name=f"{clean_name} incentive",
            )
            plugin(
                {
                    "network": {"parameters": {}},
                    "device": {
                        "precision": 1.0,
                        "execution_cost": 0.1,
                        "data_rate": 1.0,
                        "profit_expectation": 0,
                    },
                    "device_state": {
                        "balance": 0,
                        "feedback_score": 0,
                        "cumulative_reward": 0,
                        "cumulative_penalties": 0,
                        "cumulative_cost": 0,
                        "data_submissions": 1,
                    },
                    "action": {"action_type": "iot_data", "payload": {}},
                    "network_outcome": {
                        "status": "confirmed",
                        "accepted": True,
                        "metadata": {},
                    },
                    "transaction": {
                        "payload": {},
                        "submitted_at_ms": 0,
                    },
                    "block_height": 0,
                    "incentive_parameters": dict(parameters or {}),
                    "elapsed_ms": 0,
                }
            )
        except PluginValidationError:
            raise
        except Exception as exc:
            raise ValueError(f"Incentive plugin validation failed: {exc}") from exc

        current = (
            get_incentive_editor_data(database, incentive_id)
            if incentive_id is not None
            else None
        )
        artifact_id = _save_artifact_version(
            database,
            artifact_type=(
                "reward_function"
                if implementation_type == "legacy_reward"
                else "incentive_mechanism"
            ),
            name=f"{clean_name} incentive",
            entrypoint=clean_entrypoint,
            source_code=clean_source,
            current_artifact_id=(
                current.get("code_artifact_id") if current else None
            ),
        )

    values = {
        "name": clean_name,
        "version": version,
        "description": description.strip() if description else None,
        "implementation_type": implementation_type,
        "built_in_key": clean_key,
        "code_artifact_id": artifact_id,
        "parameters_json": dict(parameters or {}),
        "metadata_json": dict(metadata or {}),
    }
    if incentive_id is None:
        return database.create_record(
            "incentive_mechanism_configs",
            values,
        )
    database.update_records(
        "incentive_mechanism_configs",
        values,
        {"id": incentive_id},
    )
    return incentive_id


def delete_incentive(
    database: DatabaseService,
    incentive_id: int,
) -> None:
    """Delete an unused incentive and clean up its orphaned artifact."""
    incentive = database.get_record(
        "incentive_mechanism_configs",
        {"id": incentive_id},
    )
    artifact_id = incentive.get("code_artifact_id")
    database.delete_records(
        "incentive_mechanism_configs",
        {"id": incentive_id},
    )
    if artifact_id is not None:
        remaining = database.list_records(
            "incentive_mechanism_configs",
            filters={"code_artifact_id": artifact_id},
            limit=1,
        )
        if not remaining:
            database.delete_records(
                "code_artifacts",
                {"id": artifact_id},
            )


def list_experiments(database: DatabaseService) -> list[dict[str, Any]]:
    """Return a comparison-aware list of saved experiments."""
    return database.store.execute_query(
        """
        SELECT
            experiment.id,
            experiment.name,
            experiment.description,
            experiment.comparison_model,
            experiment.environment_id,
            environment.name AS environment_name,
            experiment.network_config_id,
            network.name AS network_name,
            experiment.incentive_a_config_id,
            incentive_a.name AS incentive_a_name,
            experiment.incentive_b_config_id,
            incentive_b.name AS incentive_b_name,
            experiment.network_a_config_id,
            legacy_a.name AS network_a_name,
            experiment.network_b_config_id,
            legacy_b.name AS network_b_name,
            experiment.poisson_lambda,
            experiment.duration_seconds,
            experiment.sample_interval_ms,
            experiment.default_random_seed,
            experiment.traffic_mix_json,
            experiment.parameters_json,
            experiment.created_at,
            experiment.updated_at
        FROM experiment_configs AS experiment
        JOIN iot_environments AS environment
            ON environment.id = experiment.environment_id
        LEFT JOIN blockchain_network_configs AS network
            ON network.id = experiment.network_config_id
        LEFT JOIN incentive_mechanism_configs AS incentive_a
            ON incentive_a.id = experiment.incentive_a_config_id
        LEFT JOIN incentive_mechanism_configs AS incentive_b
            ON incentive_b.id = experiment.incentive_b_config_id
        LEFT JOIN blockchain_network_configs AS legacy_a
            ON legacy_a.id = experiment.network_a_config_id
        LEFT JOIN blockchain_network_configs AS legacy_b
            ON legacy_b.id = experiment.network_b_config_id
        ORDER BY experiment.updated_at DESC, experiment.id DESC
        """
    )


def get_experiment_editor_data(
    database: DatabaseService,
    experiment_id: int,
) -> dict[str, Any]:
    """Load one experiment and its referenced configuration details."""
    experiment = database.configurations.get_experiment_config(experiment_id)
    if experiment.get("comparison_model") != "incentive_mechanisms":
        return experiment
    return experiment


def save_experiment(
    database: DatabaseService,
    *,
    name: str,
    description: str | None,
    environment_id: int | None,
    network_id: int | None,
    incentive_a_id: int | None,
    incentive_b_id: int | None,
    poisson_lambda: float,
    duration_seconds: float,
    sample_interval_ms: int,
    random_seed: int | None,
    traffic_mix: Mapping[str, Any] | None,
    parameters: Mapping[str, Any] | None,
    experiment_id: int | None = None,
) -> int:
    """Create or update a new incentive-comparison experiment."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Experiment name is required.")
    current = None
    if experiment_id is not None:
        current = database.configurations.get_experiment_config(experiment_id)
        if current.get("comparison_model") != "incentive_mechanisms":
            raise ValueError("Legacy experiments are read-only in this editor.")
    if environment_id is None:
        raise ValueError("An IoT environment must be selected.")
    if network_id is None:
        raise ValueError("A shared network model must be selected.")
    if incentive_a_id is None or incentive_b_id is None:
        raise ValueError("Both incentive mechanisms must be selected.")
    if poisson_lambda <= 0:
        raise ValueError("Poisson λ must be greater than zero.")
    if duration_seconds <= 0:
        raise ValueError("Simulation duration must be greater than zero.")
    if sample_interval_ms <= 0:
        raise ValueError("Sample interval must be greater than zero.")

    environment = database.get_record(
        "iot_environments",
        {"id": environment_id},
    )
    network = database.get_record(
        "blockchain_network_configs",
        {"id": network_id},
    )
    incentive_a = database.get_record(
        "incentive_mechanism_configs",
        {"id": incentive_a_id},
    )
    incentive_b = database.get_record(
        "incentive_mechanism_configs",
        {"id": incentive_b_id},
    )
    if not all((environment, network, incentive_a, incentive_b)):
        raise ValueError(
            "The selected environment, network, and incentives must exist."
        )

    values = {
        "name": clean_name,
        "description": description.strip() if description else None,
        "environment_id": environment_id,
        "comparison_model": "incentive_mechanisms",
        "network_a_config_id": None,
        "network_b_config_id": None,
        "network_config_id": network_id,
        "incentive_a_config_id": incentive_a_id,
        "incentive_b_config_id": incentive_b_id,
        "poisson_lambda": float(poisson_lambda),
        "duration_seconds": float(duration_seconds),
        "sample_interval_ms": int(sample_interval_ms),
        "default_random_seed": random_seed,
        "traffic_mix_json": dict(traffic_mix or {}),
        "parameters_json": dict(parameters or {}),
    }
    if experiment_id is None:
        return database.create_record("experiment_configs", values)

    values["updated_at"] = _utc_now()
    database.update_records(
        "experiment_configs",
        values,
        {"id": experiment_id},
    )
    return experiment_id


def clone_experiment(
    database: DatabaseService,
    experiment_id: int,
    *,
    name: str,
) -> int:
    """Clone a new incentive-comparison experiment for reproducible studies."""
    source = database.configurations.get_experiment_config(experiment_id)
    if source.get("comparison_model") != "incentive_mechanisms":
        raise ValueError("Only incentive-comparison experiments can be cloned.")
    return save_experiment(
        database,
        name=name,
        description=source.get("description"),
        environment_id=source["environment_id"],
        network_id=source["network_config_id"],
        incentive_a_id=source["incentive_a_config_id"],
        incentive_b_id=source["incentive_b_config_id"],
        poisson_lambda=float(source["poisson_lambda"]),
        duration_seconds=float(source["duration_seconds"]),
        sample_interval_ms=int(source["sample_interval_ms"]),
        random_seed=source.get("default_random_seed"),
        traffic_mix=source.get("traffic_mix_json") or {},
        parameters=source.get("parameters_json") or {},
    )


def get_experiment_comparison_summary(
    database: DatabaseService,
    experiment_id: int,
) -> ComparisonSummary:
    """Return the display summary for one saved experiment."""
    experiment = database.configurations.get_experiment_config(experiment_id)
    return comparison_summary_from_experiment(experiment)


def get_simulation_comparison_summary(
    database: DatabaseService,
    simulation_id: int,
) -> ComparisonSummary:
    """Return the display summary for one immutable simulation run."""
    results = database.simulations.get_simulation_results(
        simulation_id,
        include_time_series=False,
        transaction_limit=1,
    )
    return comparison_summary_from_results(results)


def get_simulation_results_view(
    database: DatabaseService,
    simulation_id: int,
    *,
    include_time_series: bool = True,
    transaction_limit: int = 250,
) -> dict[str, Any]:
    """Return results with comparison-aware, UI-safe arm terminology."""
    results = database.simulations.get_simulation_results(
        simulation_id,
        include_time_series=include_time_series,
        transaction_limit=transaction_limit,
    )
    summary = comparison_summary_from_results(results)
    view = dict(results)
    view["experiment_summary"] = summary.to_dict()
    view["arms"] = view.pop("networks", [])
    return view


def get_simulation_history_view(
    database: DatabaseService,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Return history rows enriched with unified comparison summaries."""
    history = database.simulations.get_simulation_history(
        status=status,
        limit=limit,
        offset=offset,
    )
    items = []
    for row in history["items"]:
        try:
            summary = get_simulation_comparison_summary(
                database,
                int(row["id"]),
            )
        except Exception:
            summary = comparison_summary_from_history_row(row)
        enriched = dict(row)
        enriched["experiment_summary"] = summary.to_dict()
        enriched.update(
            {
                "comparison_model": summary.comparison_model,
                "is_legacy": summary.is_legacy,
                "shared_network_name": summary.shared_network_name,
                "incentive_a_name": summary.incentive_a_name,
                "incentive_b_name": summary.incentive_b_name,
                "arm_a_name": summary.arm_a_name,
                "arm_b_name": summary.arm_b_name,
            }
        )
        items.append(enriched)
    return {"total": history["total"], "items": items}


def get_dashboard_view(database: DatabaseService) -> dict[str, Any]:
    """Return comparison-aware aggregates for the dashboard page."""
    first_page = database.simulations.get_simulation_history(limit=1)
    total = int(first_page["total"])
    history = (
        get_simulation_history_view(
            database,
            limit=max(total, 1),
        )
        if total
        else {"total": 0, "items": []}
    )
    rows = list(history["items"])
    completed = [row for row in rows if row["status"] == "completed"]
    incentive_rows = [
        row for row in rows if not bool(row.get("is_legacy"))
    ]
    legacy_rows = [row for row in rows if bool(row.get("is_legacy"))]
    completed_incentive_rows = [
        row for row in completed if not bool(row.get("is_legacy"))
    ]
    completed_legacy_rows = [
        row for row in completed if bool(row.get("is_legacy"))
    ]

    incentive_results: list[dict[str, Any]] = []
    for row in completed_incentive_rows:
        try:
            result = get_simulation_results_view(
                database,
                int(row["id"]),
                include_time_series=False,
                transaction_limit=1,
            )
        except Exception:
            continue
        summary = result["experiment_summary"]
        arms = {
            arm["network_slot"]: arm
            for arm in result.get("arms", [])
        }
        arm_metrics = {}
        for slot, arm in arms.items():
            custom = arm.get("custom_summary_json") or {}
            if isinstance(custom, str):
                try:
                    custom = json.loads(custom)
                except json.JSONDecodeError:
                    custom = {}
            if not isinstance(custom, Mapping):
                custom = {}
            churn = arm.get("final_churn_rate")
            retention = custom.get("final_retention_rate")
            if retention is None and churn is not None:
                retention = 1.0 - float(churn)
            arm_metrics[slot] = {
                "retention_rate": retention,
                "participation_rate": custom.get(
                    "opportunity_participation_rate"
                ),
                "useful_contribution_count": custom.get(
                    "useful_contribution_count"
                ),
                "net_incentive_cost": custom.get("net_incentive_cost"),
                "cost_per_useful_contribution": custom.get(
                    "incentive_cost_per_useful_contribution"
                ),
            }
        comparison = result.get("comparison") or {}
        comparison_summary = comparison.get("summary_json") or {}
        incentive_category = comparison_summary.get(
            "incentive_effectiveness"
        ) or {}
        incentive_winner = incentive_category.get(
            "winner_slot",
            comparison.get("winner_slot"),
        )
        incentive_results.append(
            {
                "id": row["id"],
                "name": row["name"],
                "summary": summary,
                "winner_slot": incentive_winner,
                "arms": arm_metrics,
            }
        )

    def _mean_metric(metric: str) -> float | None:
        values = [
            float(value)
            for result in incentive_results
            for arm in result["arms"].values()
            for value in [arm.get(metric)]
            if value is not None
        ]
        return sum(values) / len(values) if values else None

    useful_values = [
        float(arm["useful_contribution_count"])
        for result in incentive_results
        for arm in result["arms"].values()
        if arm.get("useful_contribution_count") is not None
    ]
    incentive_wins: Counter[str] = Counter()
    for result in incentive_results:
        winner = result["winner_slot"]
        if winner in {"A", "B"}:
            winner_name = result["summary"].get(
                "incentive_a_name"
                if winner == "A"
                else "incentive_b_name"
            )
            if winner_name:
                incentive_wins[str(winner_name)] += 1

    legacy_wins: Counter[str] = Counter()
    for row in completed_legacy_rows:
        winner = row.get("winner_slot")
        winner_name = (
            row.get("network_a_name")
            if winner == "A"
            else row.get("network_b_name")
            if winner == "B"
            else None
        )
        if winner_name:
            legacy_wins[str(winner_name)] += 1

    return {
        "history": rows,
        "environments": list_environments(database),
        "networks": list_networks(database),
        "incentives": list_incentives(database),
        "completed": completed,
        "incentive_runs": incentive_rows,
        "legacy_runs": legacy_rows,
        "incentive_results": incentive_results,
        "latest": rows[0] if rows else None,
        "incentive_summary": {
            "run_count": len(incentive_rows),
            "completed_count": len(incentive_results),
            "best_incentive": (
                incentive_wins.most_common(1)[0][0]
                if incentive_wins
                else "Not available"
            ),
            "wins": dict(incentive_wins),
            "average_retention_rate": _mean_metric("retention_rate"),
            "average_participation_rate": _mean_metric(
                "participation_rate"
            ),
            "useful_contribution_count": sum(useful_values),
            "average_useful_contribution_count": (
                sum(useful_values) / len(useful_values)
                if useful_values
                else None
            ),
            "average_cost_per_useful_contribution": _mean_metric(
                "cost_per_useful_contribution"
            ),
        },
        "legacy_summary": {
            "run_count": len(legacy_rows),
            "completed_count": len(completed_legacy_rows),
            "best_network": (
                legacy_wins.most_common(1)[0][0]
                if legacy_wins
                else "Not available"
            ),
            "wins": dict(legacy_wins),
            "average_churn_rate": (
                sum(
                    float(value)
                    for row in completed_legacy_rows
                    for value in (
                        row.get("network_a_churn_rate"),
                        row.get("network_b_churn_rate"),
                    )
                    if value is not None
                )
                / sum(
                    1
                    for row in completed_legacy_rows
                    for value in (
                        row.get("network_a_churn_rate"),
                        row.get("network_b_churn_rate"),
                    )
                    if value is not None
                )
                if any(
                    value is not None
                    for row in completed_legacy_rows
                    for value in (
                        row.get("network_a_churn_rate"),
                        row.get("network_b_churn_rate"),
                    )
                )
                else None
            ),
        },
    }


def list_environments(database: DatabaseService) -> list[dict[str, Any]]:
    """Return environments with behavior and enabled-device counts."""
    return database.store.execute_query(
        """
        SELECT
            env.id,
            env.name,
            env.description,
            env.revision,
            env.created_at,
            env.updated_at,
            COUNT(DISTINCT dev.id) AS device_count,
            COUNT(DISTINCT dev.behavior_id) AS group_count
        FROM iot_environments AS env
        LEFT JOIN environment_devices AS dev
            ON dev.environment_id = env.id
            AND dev.enabled = 1
        GROUP BY env.id
        ORDER BY env.updated_at DESC, env.id DESC
        """
    )


def environment_group_dataframe(
    database: DatabaseService,
    environment_id: int,
) -> pd.DataFrame:
    """Convert normalized behavior/device rows into editable group rows."""
    environment = database.configurations.get_environment(environment_id)
    behavior_by_id = {
        behavior["id"]: behavior
        for behavior in environment["device_behaviors"]
    }
    grouped_devices: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for device in environment["devices"]:
        grouped_devices[device["behavior_id"]].append(device)

    rows = []
    for behavior_id, devices in grouped_devices.items():
        behavior = behavior_by_id[behavior_id]
        parameters = behavior.get("parameters_json") or {}
        metadata = devices[0].get("metadata_json") or {}
        rows.append(
            {
                "behavior_id": behavior_id,
                "group_name": metadata.get(
                    "group_name",
                    behavior["name"].split(":")[-1].strip(),
                ),
                "device_count": len(devices),
                "precision": behavior["precision"],
                "execution_cost": behavior["execution_cost"],
                "data_rate": behavior["data_rate"],
                "profit_expectation": behavior["profit_expectation"],
                "initial_balance": devices[0]["initial_balance"],
                "base_value": parameters.get("base_value", 20.0),
                "unit": parameters.get("unit", "unit"),
            }
        )

    return pd.DataFrame(rows, columns=environment_group_columns())


def empty_group_dataframe() -> pd.DataFrame:
    """Return one editable default group row."""
    return pd.DataFrame(
        [
            {
                "behavior_id": pd.NA,
                "group_name": "Sensor group",
                "device_count": 10,
                "precision": 0.9,
                "execution_cost": 0.1,
                "data_rate": 1.0,
                "profit_expectation": 0.0,
                "initial_balance": 10.0,
                "base_value": 20.0,
                "unit": "unit",
            }
        ],
        columns=environment_group_columns(),
    )


def environment_group_columns() -> list[str]:
    """Return the stable device-group editor column order."""
    return [
        "behavior_id",
        "group_name",
        "device_count",
        "precision",
        "execution_cost",
        "data_rate",
        "profit_expectation",
        "initial_balance",
        "base_value",
        "unit",
    ]


def validate_group_editor(
    groups: pd.DataFrame,
) -> list[DeviceGroupConfig]:
    """Validate editable rows and convert them to domain configurations."""
    if groups.empty:
        raise ValueError("Add at least one device group.")

    prepared = groups.dropna(how="all").copy()
    configurations = []
    for index, row in prepared.iterrows():
        group_name = str(row.get("group_name", "")).strip()
        if not group_name:
            raise ValueError(f"Group row {index + 1} needs a name.")
        config = DeviceGroupConfig(
            name=group_name,
            device_count=int(row["device_count"]),
            precision=float(row["precision"]),
            execution_cost=float(row["execution_cost"]),
            data_rate=float(row["data_rate"]),
            profit_expectation=float(row["profit_expectation"]),
            initial_balance=float(row["initial_balance"]),
            behavior_parameters={
                "base_value": float(row["base_value"]),
                "unit": str(row.get("unit") or "unit"),
            },
        )
        config.validate()
        configurations.append(config)

    if sum(group.device_count for group in configurations) > 100:
        raise ValueError("An environment can contain at most 100 devices.")
    return configurations


def create_environment(
    database: DatabaseService,
    *,
    name: str,
    description: str | None,
    groups: pd.DataFrame,
) -> int:
    """Create and persist an environment from the group editor."""
    group_configs = validate_group_editor(groups)
    definition = IoTEnvironmentDefinition(
        name=name.strip(),
        description=description.strip() if description else None,
        groups=group_configs,
    )
    return definition.save(database.configurations)


def update_environment(
    database: DatabaseService,
    *,
    environment_id: int,
    name: str,
    description: str | None,
    groups: pd.DataFrame,
) -> None:
    """Update environment metadata, behaviors, and group device counts."""
    group_configs = validate_group_editor(groups)
    environment = database.configurations.get_environment(environment_id)
    old_devices = environment["devices"]
    old_behavior_ids = {
        behavior["id"] for behavior in environment["device_behaviors"]
    }
    used_behavior_ids: set[int] = set()
    existing_keys = {device["device_key"] for device in old_devices}
    rows = groups.dropna(how="all").reset_index(drop=True)

    database.update_records(
        "iot_environments",
        {
            "name": name.strip(),
            "description": description.strip() if description else None,
            "revision": int(environment["revision"]) + 1,
            "metadata_json": {
                "device_groups": [
                    {
                        "name": config.name,
                        "device_count": config.device_count,
                    }
                    for config in group_configs
                ]
            },
            "updated_at": _utc_now(),
        },
        {"id": environment_id},
    )

    for row, group in zip(rows.to_dict("records"), group_configs, strict=True):
        raw_behavior_id = row.get("behavior_id")
        behavior_id = (
            int(raw_behavior_id)
            if raw_behavior_id is not None
            and not pd.isna(raw_behavior_id)
            and int(raw_behavior_id) in old_behavior_ids
            else None
        )
        parameters = {
            "base_value": float(row["base_value"]),
            "unit": str(row.get("unit") or "unit"),
        }
        if behavior_id is None:
            behavior_id = database.configurations.create_device_behavior(
                name=f"{name.strip()}: {group.name}",
                precision=group.precision,
                execution_cost=group.execution_cost,
                data_rate=group.data_rate,
                profit_expectation=group.profit_expectation,
                parameters=parameters,
            )
        else:
            database.update_records(
                "device_behaviors",
                {
                    "name": f"{name.strip()}: {group.name}",
                    "precision": group.precision,
                    "execution_cost": group.execution_cost,
                    "data_rate": group.data_rate,
                    "profit_expectation": group.profit_expectation,
                    "parameters_json": parameters,
                },
                {"id": behavior_id},
            )
        used_behavior_ids.add(behavior_id)

        group_devices = [
            device
            for device in old_devices
            if device["behavior_id"] == behavior_id
        ]
        retained_devices = group_devices[: group.device_count]
        removed_devices = group_devices[group.device_count :]
        for device in retained_devices:
            database.update_records(
                "environment_devices",
                {
                    "initial_balance": group.initial_balance,
                    "metadata_json": {"group_name": group.name},
                },
                {"id": device["id"]},
            )
        for device in removed_devices:
            database.delete_records(
                "environment_devices",
                {"id": device["id"]},
            )
            existing_keys.discard(device["device_key"])

        for index in range(
            len(retained_devices) + 1,
            group.device_count + 1,
        ):
            device_key = _next_device_key(
                _slug(group.name),
                index,
                existing_keys,
            )
            existing_keys.add(device_key)
            database.configurations.create_environment_device(
                environment_id=environment_id,
                behavior_id=behavior_id,
                device_key=device_key,
                display_name=f"{group.name} {index}",
                initial_balance=group.initial_balance,
                metadata={"group_name": group.name},
            )

    removed_behavior_ids = old_behavior_ids - used_behavior_ids
    for behavior_id in removed_behavior_ids:
        for device in old_devices:
            if device["behavior_id"] == behavior_id:
                database.delete_records(
                    "environment_devices",
                    {"id": device["id"]},
                )
        try:
            database.delete_records(
                "device_behaviors",
                {"id": behavior_id},
            )
        except Exception:
            pass


def list_networks(database: DatabaseService) -> list[dict[str, Any]]:
    """Return saved network configurations with plugin names."""
    return database.store.execute_query(
        """
        SELECT
            nc.*,
            reward.name AS reward_name,
            logic.name AS logic_name
        FROM blockchain_network_configs AS nc
        LEFT JOIN code_artifacts AS reward
            ON reward.id = nc.reward_artifact_id
        LEFT JOIN code_artifacts AS logic
            ON logic.id = nc.blockchain_logic_artifact_id
        ORDER BY nc.created_at DESC, nc.id DESC
        """
    )


def delete_environment(
    database: DatabaseService,
    environment_id: int,
) -> None:
    """Delete an unused environment and clean up orphaned behaviors."""
    environment = database.configurations.get_environment(environment_id)
    behavior_ids = {
        device["behavior_id"] for device in environment["devices"]
    }
    database.delete_records("iot_environments", {"id": environment_id})
    for behavior_id in behavior_ids:
        remaining = database.list_records(
            "environment_devices",
            filters={"behavior_id": behavior_id},
            limit=1,
        )
        if not remaining:
            database.delete_records(
                "device_behaviors",
                {"id": behavior_id},
            )


def delete_network(
    database: DatabaseService,
    network_id: int,
) -> None:
    """Delete an unused network and clean up orphaned code artifacts."""
    network = database.get_record(
        "blockchain_network_configs",
        {"id": network_id},
    )
    artifact_ids = {
        network.get("reward_artifact_id"),
        network.get("blockchain_logic_artifact_id"),
    }
    database.delete_records(
        "blockchain_network_configs",
        {"id": network_id},
    )
    for artifact_id in artifact_ids - {None}:
        reward_use = database.list_records(
            "blockchain_network_configs",
            filters={"reward_artifact_id": artifact_id},
            limit=1,
        )
        logic_use = database.list_records(
            "blockchain_network_configs",
            filters={"blockchain_logic_artifact_id": artifact_id},
            limit=1,
        )
        if not reward_use and not logic_use:
            database.delete_records(
                "code_artifacts",
                {"id": artifact_id},
            )


def delete_simulation(
    database: DatabaseService,
    simulation_id: int,
) -> None:
    """Atomically delete a run despite transaction/event RESTRICT links."""
    with database.store.transaction() as connection:
        run = connection.execute(
            """
            SELECT experiment_config_id
            FROM simulation_runs
            WHERE id = ?
            """,
            (simulation_id,),
        ).fetchone()
        if run is None:
            raise RecordNotFoundError(
                f"Simulation run {simulation_id} does not exist."
            )

        connection.execute(
            """
            DELETE FROM network_transactions
            WHERE simulation_network_id IN (
                SELECT id
                FROM simulation_networks
                WHERE simulation_id = ?
            )
            """,
            (simulation_id,),
        )
        connection.execute(
            "DELETE FROM simulation_runs WHERE id = ?",
            (simulation_id,),
        )

        experiment_id = run["experiment_config_id"]
        if experiment_id is None:
            return
        experiment = connection.execute(
            """
            SELECT name
            FROM experiment_configs
            WHERE id = ?
            """,
            (experiment_id,),
        ).fetchone()
        if (
            experiment is not None
            and str(experiment["name"]).startswith("UI experiment ")
        ):
            connection.execute(
                """
                DELETE FROM experiment_configs
                WHERE id = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM simulation_runs
                      WHERE experiment_config_id = ?
                  )
                """,
                (experiment_id, experiment_id),
            )


def get_network_editor_data(
    database: DatabaseService,
    network_id: int,
) -> dict[str, Any]:
    """Load one network and its current plugin source code."""
    network = database.get_record(
        "blockchain_network_configs",
        {"id": network_id},
    )
    reward = _artifact_or_none(
        database,
        network.get("reward_artifact_id"),
    )
    logic = _artifact_or_none(
        database,
        network.get("blockchain_logic_artifact_id"),
    )
    parameters = network.get("parameters_json") or {}
    return {
        **network,
        "reward_code": reward["source_code"] if reward else DEFAULT_REWARD_CODE,
        "reward_entrypoint": (
            reward["entrypoint"] if reward else "calculate_reward"
        ),
        "logic_code": logic["source_code"] if logic else "",
        "logic_entrypoint": (
            logic["entrypoint"] if logic else "process_transaction"
        ),
        "base_mining_time_ms": parameters.get("base_mining_time_ms", 50),
        "base_iot_reward": parameters.get("base_iot_reward", 1.0),
        "feedback_weight": parameters.get("feedback_weight", 0.1),
    }


def save_network(
    database: DatabaseService,
    *,
    name: str,
    description: str | None,
    difficulty: int,
    block_capacity: int,
    block_interval_ms: int,
    fee_rate: float,
    base_mining_time_ms: float,
    base_iot_reward: float,
    feedback_weight: float,
    reward_code: str,
    reward_entrypoint: str,
    logic_code: str,
    logic_entrypoint: str,
    network_id: int | None = None,
) -> int:
    """Validate plugins and create or update a network configuration."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Network name is required.")
    reward_function = load_plugin_function(
        reward_code,
        reward_entrypoint,
        plugin_name=f"{clean_name} reward function",
    )
    reward_function(
        {
            "network": {"parameters": {}},
            "device": {
                "precision": 1.0,
                "execution_cost": 0.1,
                "data_rate": 1.0,
                "profit_expectation": 0,
            },
            "device_state": {
                "feedback_score": 0,
                "balance": 0,
                "cumulative_reward": 0,
                "cumulative_cost": 0,
                "data_submissions": 1,
            },
            "block_height": 0,
            "elapsed_ms": 0,
            "transaction": {"payload": {}, "submitted_at_ms": 0},
        }
    )
    if logic_code.strip():
        load_plugin_function(
            logic_code,
            logic_entrypoint,
            plugin_name=f"{clean_name} blockchain logic",
        )

    current = (
        get_network_editor_data(database, network_id)
        if network_id is not None
        else None
    )
    created_artifacts = []
    try:
        reward_artifact_id = _save_artifact_version(
            database,
            artifact_type="reward_function",
            name=f"{clean_name} reward",
            entrypoint=reward_entrypoint,
            source_code=reward_code,
            current_artifact_id=(
                current.get("reward_artifact_id") if current else None
            ),
        )
        if reward_artifact_id not in {
            current.get("reward_artifact_id") if current else None
        }:
            created_artifacts.append(reward_artifact_id)

        logic_artifact_id = None
        if logic_code.strip():
            logic_artifact_id = _save_artifact_version(
                database,
                artifact_type="blockchain_logic",
                name=f"{clean_name} logic",
                entrypoint=logic_entrypoint,
                source_code=logic_code,
                current_artifact_id=(
                    current.get("blockchain_logic_artifact_id")
                    if current
                    else None
                ),
            )
            if logic_artifact_id not in {
                current.get("blockchain_logic_artifact_id")
                if current
                else None
            }:
                created_artifacts.append(logic_artifact_id)

        values = {
            "name": clean_name,
            "description": description.strip() if description else None,
            "consensus_type": "pow",
            "pow_difficulty": int(difficulty),
            "max_transactions_per_block": int(block_capacity),
            "target_block_time_ms": (
                int(block_interval_ms) if block_interval_ms > 0 else None
            ),
            "transaction_fee_rate": float(fee_rate),
            "reward_artifact_id": reward_artifact_id,
            "blockchain_logic_artifact_id": logic_artifact_id,
            "parameters_json": {
                "base_mining_time_ms": float(base_mining_time_ms),
                "base_iot_reward": float(base_iot_reward),
                "feedback_weight": float(feedback_weight),
            },
        }
        if network_id is None:
            return database.create_record(
                "blockchain_network_configs",
                {"version": 1, **values},
            )
        database.update_records(
            "blockchain_network_configs",
            values,
            {"id": network_id},
        )
        return network_id
    except Exception:
        for artifact_id in created_artifacts:
            try:
                database.delete_records(
                    "code_artifacts",
                    {"id": artifact_id},
                )
            except Exception:
                pass
        raise


def _save_artifact_version(
    database: DatabaseService,
    *,
    artifact_type: str,
    name: str,
    entrypoint: str,
    source_code: str,
    current_artifact_id: int | None,
) -> int:
    current = _artifact_or_none(database, current_artifact_id)
    if (
        current
        and current["source_code"] == source_code
        and current["entrypoint"] == entrypoint
    ):
        return current["id"]

    versions = database.list_records(
        "code_artifacts",
        filters={"artifact_type": artifact_type},
    )
    next_version = (
        max(
            (
                int(artifact["version"])
                for artifact in versions
                if artifact["name"] == name
            ),
            default=0,
        )
        + 1
    )
    return database.configurations.create_code_artifact(
        artifact_type=artifact_type,
        name=name,
        entrypoint=entrypoint,
        source_code=source_code,
        version=next_version,
        validation_status="valid",
    )


def _artifact_or_none(
    database: DatabaseService,
    artifact_id: int | None,
) -> dict[str, Any] | None:
    if artifact_id is None:
        return None
    return database.get_record(
        "code_artifacts",
        {"id": artifact_id},
        required=False,
    )


def _next_device_key(
    prefix: str,
    start: int,
    existing_keys: set[str],
) -> str:
    index = start
    while f"{prefix}-{index:03d}" in existing_keys:
        index += 1
    return f"{prefix}-{index:03d}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "device"


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
