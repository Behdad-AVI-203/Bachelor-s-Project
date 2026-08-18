"""Tests for experiment-management UI services."""

from __future__ import annotations

import pytest

from src.ui.services import (
    clone_experiment,
    get_experiment_editor_data,
    list_experiments,
    save_experiment,
)


def _create_incentives(database):
    incentive_a_id = database.configurations.create_incentive_config(
        name="Experiment incentive A",
        version=1,
        implementation_type="built_in",
        built_in_key="default_reward",
    )
    incentive_b_id = database.configurations.create_incentive_config(
        name="Experiment incentive B",
        version=1,
        implementation_type="built_in",
        built_in_key="participation_first",
    )
    return incentive_a_id, incentive_b_id


def test_create_and_list_incentive_comparison_experiment(
    configured_database,
):
    database, identifiers = configured_database
    incentive_a_id, incentive_b_id = _create_incentives(database)

    experiment_id = save_experiment(
        database,
        name="Managed experiment",
        description="Created from the UI service",
        environment_id=identifiers["environment_id"],
        network_id=identifiers["network_a_id"],
        incentive_a_id=incentive_a_id,
        incentive_b_id=incentive_b_id,
        poisson_lambda=3.0,
        duration_seconds=30.0,
        sample_interval_ms=1_000,
        random_seed=42,
        traffic_mix={"iot_data": 1.0},
        parameters={"positive_feedback_probability": 0.75},
    )

    editor = get_experiment_editor_data(database, experiment_id)
    row = next(
        item for item in list_experiments(database)
        if item["id"] == experiment_id
    )

    assert editor["comparison_model"] == "incentive_mechanisms"
    assert editor["network"]["name"] == "Network A"
    assert editor["incentive_a"]["name"] == "Experiment incentive A"
    assert row["network_name"] == "Network A"
    assert row["incentive_b_name"] == "Experiment incentive B"


def test_edit_experiment_preserves_new_comparison_model(configured_database):
    database, identifiers = configured_database
    incentive_a_id, incentive_b_id = _create_incentives(database)
    experiment_id = save_experiment(
        database,
        name="Editable experiment",
        description=None,
        environment_id=identifiers["environment_id"],
        network_id=identifiers["network_a_id"],
        incentive_a_id=incentive_a_id,
        incentive_b_id=incentive_b_id,
        poisson_lambda=1.0,
        duration_seconds=10.0,
        sample_interval_ms=1_000,
        random_seed=None,
        traffic_mix={"iot_data": 1.0},
        parameters={},
    )

    save_experiment(
        database,
        experiment_id=experiment_id,
        name="Edited experiment",
        description="Updated",
        environment_id=identifiers["environment_id"],
        network_id=identifiers["network_b_id"],
        incentive_a_id=incentive_b_id,
        incentive_b_id=incentive_a_id,
        poisson_lambda=2.0,
        duration_seconds=20.0,
        sample_interval_ms=500,
        random_seed=7,
        traffic_mix={"transfer": 0.2, "iot_data": 0.8},
        parameters={"transfer_amount_min": 0.1},
    )

    updated = database.configurations.get_experiment_config(experiment_id)
    assert updated["name"] == "Edited experiment"
    assert updated["comparison_model"] == "incentive_mechanisms"
    assert updated["network_config_id"] == identifiers["network_b_id"]
    assert updated["network_a_config_id"] is None
    assert updated["network_b_config_id"] is None


def test_clone_experiment_copies_reproducible_parameters(configured_database):
    database, identifiers = configured_database
    incentive_a_id, incentive_b_id = _create_incentives(database)
    source_id = save_experiment(
        database,
        name="Source experiment",
        description="Clone source",
        environment_id=identifiers["environment_id"],
        network_id=identifiers["network_a_id"],
        incentive_a_id=incentive_a_id,
        incentive_b_id=incentive_b_id,
        poisson_lambda=4.0,
        duration_seconds=60.0,
        sample_interval_ms=250,
        random_seed=123,
        traffic_mix={"feedback": 1.0},
        parameters={"positive_feedback_probability": 0.5},
    )

    clone_id = clone_experiment(
        database,
        source_id,
        name="Cloned experiment",
    )
    source = database.get_record("experiment_configs", {"id": source_id})
    clone = database.get_record("experiment_configs", {"id": clone_id})

    assert clone["name"] == "Cloned experiment"
    assert clone["comparison_model"] == "incentive_mechanisms"
    for key in (
        "environment_id",
        "network_config_id",
        "incentive_a_config_id",
        "incentive_b_config_id",
        "poisson_lambda",
        "duration_seconds",
        "sample_interval_ms",
        "default_random_seed",
        "traffic_mix_json",
        "parameters_json",
    ):
        assert clone[key] == source[key]


def test_legacy_experiment_cannot_be_edited_or_cloned(configured_database):
    database, identifiers = configured_database

    with pytest.raises(ValueError, match="read-only"):
        save_experiment(
            database,
            experiment_id=identifiers["experiment_id"],
            name="Converted legacy experiment",
            description=None,
            environment_id=identifiers["environment_id"],
            network_id=identifiers["network_a_id"],
            incentive_a_id=None,
            incentive_b_id=None,
            poisson_lambda=1.0,
            duration_seconds=1.0,
            sample_interval_ms=1_000,
            random_seed=None,
            traffic_mix={"iot_data": 1.0},
            parameters={},
        )

    with pytest.raises(ValueError, match="Only incentive-comparison"):
        clone_experiment(
            database,
            identifiers["experiment_id"],
            name="Legacy clone",
        )


def test_missing_experiment_references_are_rejected(configured_database):
    database, identifiers = configured_database

    with pytest.raises(ValueError, match="Both incentive mechanisms"):
        save_experiment(
            database,
            name="Invalid experiment",
            description=None,
            environment_id=identifiers["environment_id"],
            network_id=identifiers["network_a_id"],
            incentive_a_id=None,
            incentive_b_id=None,
            poisson_lambda=1.0,
            duration_seconds=1.0,
            sample_interval_ms=1_000,
            random_seed=None,
            traffic_mix={"iot_data": 1.0},
            parameters={},
        )
