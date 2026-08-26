"""Tests for comparison-aware UI read models."""

from __future__ import annotations

from pathlib import Path

from src.ui.read_models import (
    INCENTIVE_COMPARISON,
    comparison_summary_from_experiment,
    comparison_summary_from_results,
)
from src.ui.services import (
    build_results_export_frame,
    get_dashboard_view,
    get_experiment_comparison_summary,
)

from src.core import SimulationEngine


def test_incentive_experiment_summary_uses_shared_network_and_incentives():
    summary = comparison_summary_from_experiment(
        {
            "comparison_model": INCENTIVE_COMPARISON,
            "environment": {"name": "Static sensors"},
            "network": {"name": "PoW test network"},
            "incentive_a": {"name": "Balanced incentives"},
            "incentive_b": {"name": "Participation-first"},
        }
    )

    assert not summary.is_legacy
    assert summary.environment_name == "Static sensors"
    assert summary.shared_network_name == "PoW test network"
    assert summary.incentive_a_name == "Balanced incentives"
    assert summary.incentive_b_name == "Participation-first"
    assert summary.arm_a_name == "Balanced incentives"
    assert summary.labels.score_a == "Balanced incentives score"
    assert summary.labels.winner == "Winning incentive"


def test_legacy_experiment_summary_preserves_network_arm_labels():
    summary = comparison_summary_from_experiment(
        {
            "comparison_model": "legacy_networks",
            "environment": {"name": "Legacy environment"},
            "network_a": {"name": "Network A"},
            "network_b": {"name": "Network B"},
        }
    )

    assert summary.is_legacy
    assert summary.shared_network_name is None
    assert summary.incentive_a_name is None
    assert summary.arm_a_name == "Network A"
    assert summary.arm_b_name == "Network B"
    assert summary.labels.winner == "Winning network"


def test_results_export_contains_snapshot_and_metric_categories():
    frame = build_results_export_frame(
        {
            "run": {
                "name": "Study",
                "configuration_snapshot_json": {
                    "experiment": {"comparison_model": INCENTIVE_COMPARISON}
                },
            },
            "experiment_summary": {
                "comparison_model": INCENTIVE_COMPARISON,
                "is_legacy": False,
                "environment_name": "Environment",
                "shared_network_name": "Network",
                "incentive_a_name": "A",
                "incentive_b_name": "B",
            },
            "comparison": {"winner_slot": "A"},
            "comparison_metrics": [
                {
                    "display_name": "Retention",
                    "value_a": 1,
                    "value_b": 0.5,
                    "winner_slot": "A",
                    "details_json": {"category": "incentive_effectiveness"},
                }
            ],
        }
    )
    assert set(frame["category"]) == {
        "experiment_context",
        "incentive_effectiveness",
    }
    assert frame.iloc[0]["winner_type"] == "incentive"
    assert "experiment" in frame.iloc[0]["experiment_snapshot"]


def test_streamlit_pages_do_not_query_repositories_directly():
    pages = Path("app_pages")
    forbidden = (
        "database.simulations",
        "database.configurations",
        "database.store",
    )
    for path in pages.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not any(token in source for token in forbidden), path


def test_results_summary_reads_immutable_incentive_snapshot():
    results = {
        "run": {
            "configuration_snapshot_json": {
                "experiment": {
                    "comparison_model": INCENTIVE_COMPARISON,
                },
                "environment_bundle": {
                    "environment": {"name": "Snapshot environment"},
                },
                "network_bundle": {
                    "network": {"name": "Snapshot network"},
                },
                "incentive_a_bundle": {
                    "incentive": {"name": "Snapshot A"},
                },
                "incentive_b_bundle": {
                    "incentive": {"name": "Snapshot B"},
                },
            }
        },
        "networks": [],
    }

    summary = comparison_summary_from_results(results)

    assert summary.environment_name == "Snapshot environment"
    assert summary.shared_network_name == "Snapshot network"
    assert summary.incentive_a_name == "Snapshot A"
    assert summary.incentive_b_name == "Snapshot B"


def test_database_backed_experiment_summary_uses_configuration_repository(
    configured_database,
):
    database, identifiers = configured_database
    incentive_a_id = database.configurations.create_incentive_config(
        name="Built-in A",
        version=1,
        implementation_type="built_in",
        built_in_key="default",
    )
    incentive_b_id = database.configurations.create_incentive_config(
        name="Built-in B",
        version=1,
        implementation_type="built_in",
        built_in_key="default",
    )
    experiment_id = database.configurations.create_experiment_config(
        name="Incentive comparison",
        environment_id=identifiers["environment_id"],
        network_config_id=identifiers["network_a_id"],
        incentive_a_config_id=incentive_a_id,
        incentive_b_config_id=incentive_b_id,
        poisson_lambda=1.0,
        duration_seconds=1.0,
        sample_interval_ms=1_000,
    )

    summary = get_experiment_comparison_summary(database, experiment_id)

    assert summary.comparison_model == INCENTIVE_COMPARISON
    assert summary.shared_network_name == "Network A"
    assert summary.incentive_a_name == "Built-in A"
    assert summary.incentive_b_name == "Built-in B"


def test_dashboard_view_separates_incentive_and_legacy_runs(
    configured_database,
):
    database, identifiers = configured_database
    legacy_result = SimulationEngine(database).run_experiment(
        identifiers["experiment_id"],
        random_seed=42,
    )
    database.update_records(
        "blockchain_network_configs",
        {
            "reward_artifact_id": None,
            "parameters_json": {"base_mining_time_ms": 5},
        },
        {"id": identifiers["network_a_id"]},
    )

    incentive_a_id = database.configurations.create_incentive_config(
        name="Dashboard incentive A",
        version=1,
        implementation_type="built_in",
        built_in_key="default_reward",
    )
    incentive_b_id = database.configurations.create_incentive_config(
        name="Dashboard incentive B",
        version=1,
        implementation_type="built_in",
        built_in_key="default_reward",
    )
    incentive_experiment_id = (
        database.configurations.create_experiment_config(
            name="Dashboard incentive experiment",
            environment_id=identifiers["environment_id"],
            network_config_id=identifiers["network_a_id"],
            incentive_a_config_id=incentive_a_id,
            incentive_b_config_id=incentive_b_id,
            poisson_lambda=1.0,
            duration_seconds=2.0,
            sample_interval_ms=1_000,
            default_random_seed=42,
            traffic_mix={"iot_data": 1.0},
        )
    )
    incentive_result = SimulationEngine(database).run_experiment(
        incentive_experiment_id,
        random_seed=42,
    )

    dashboard = get_dashboard_view(database)

    assert [row["id"] for row in dashboard["legacy_runs"]] == [
        legacy_result.simulation_id
    ]
    assert [row["id"] for row in dashboard["incentive_runs"]] == [
        incentive_result.simulation_id
    ]
    assert dashboard["incentive_summary"]["run_count"] == 1
    assert dashboard["legacy_summary"]["run_count"] == 1
    assert dashboard["incentive_summary"]["best_incentive"] != (
        dashboard["legacy_summary"]["best_network"]
    )
