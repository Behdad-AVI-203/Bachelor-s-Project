"""Integration coverage for schema, CRUD, and portable configurations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.database import (
    Database,
    DatabaseIntegrityError,
    RecordNotFoundError,
)

from .factories import (
    create_environment,
    create_network,
    create_platform_configuration,
)


EXPECTED_TABLES = {
    "schema_migrations",
    "code_artifacts",
    "iot_environments",
    "device_behaviors",
    "environment_devices",
    "blockchain_network_configs",
    "incentive_mechanism_configs",
    "experiment_configs",
    "configuration_bundles",
    "simulation_runs",
    "simulation_networks",
    "simulation_devices",
    "simulation_events",
    "blocks",
    "network_transactions",
    "device_state_samples",
    "network_metric_samples",
    "network_run_summaries",
    "simulation_comparisons",
    "comparison_metrics",
}

EXPECTED_INDEXES = {
    "idx_devices_environment",
    "idx_devices_behavior",
    "idx_runs_created",
    "idx_runs_status",
    "idx_simulation_networks_run",
    "idx_simulation_devices_run",
    "idx_events_timeline",
    "idx_events_type",
    "idx_blocks_timeline",
    "idx_transactions_timeline",
    "idx_transactions_status_type",
    "idx_transactions_block",
    "idx_device_states_timeline",
    "idx_incentive_configs_artifact",
    "idx_experiments_network",
    "idx_experiments_incentives",
}


@pytest.mark.integration
def test_schema_tables_foreign_keys_and_indexes(database):
    tables = {
        row["name"]
        for row in database.store.execute_query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    indexes = {
        row["name"]
        for row in database.store.execute_query(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    foreign_keys = database.store.execute_query("PRAGMA foreign_keys")
    experiment_keys = database.store.execute_query(
        "PRAGMA foreign_key_list(experiment_configs)"
    )

    assert EXPECTED_TABLES <= tables
    assert EXPECTED_INDEXES <= indexes
    assert foreign_keys[0]["foreign_keys"] == 1
    assert {row["on_delete"] for row in experiment_keys} == {"RESTRICT"}


def test_generic_crud_and_json_round_trip(database):
    environment_id = database.create_record(
        "iot_environments",
        {
            "name": "CRUD environment",
            "description": "Initial",
            "metadata_json": {"source": "pytest"},
        },
    )

    created = database.get_record(
        "iot_environments",
        {"id": environment_id},
    )
    assert created["metadata_json"] == {"source": "pytest"}

    affected = database.update_records(
        "iot_environments",
        {
            "description": "Updated",
            "metadata_json": {"source": "pytest", "revision": 2},
        },
        {"id": environment_id},
    )
    assert affected == 1
    assert database.get_record(
        "iot_environments",
        {"id": environment_id},
    )["description"] == "Updated"

    assert database.delete_records(
        "iot_environments",
        {"id": environment_id},
    ) == 1
    with pytest.raises(RecordNotFoundError):
        database.get_record(
            "iot_environments",
            {"id": environment_id},
        )


def test_network_configuration_crud(database):
    network_id = create_network(
        database,
        name="CRUD network",
        difficulty=1,
    )
    created = database.configurations.get_network_config(network_id)
    assert created["pow_difficulty"] == 1
    assert len(created["code_artifacts"]) == 1

    assert database.update_records(
        "blockchain_network_configs",
        {"pow_difficulty": 3},
        {"id": network_id},
    ) == 1
    assert database.get_record(
        "blockchain_network_configs",
        {"id": network_id},
    )["pow_difficulty"] == 3

    assert database.delete_records(
        "blockchain_network_configs",
        {"id": network_id},
    ) == 1
    assert database.get_record(
        "blockchain_network_configs",
        {"id": network_id},
        required=False,
    ) is None


def test_incentive_experiment_configuration_and_export_import(
    database,
    tmp_path,
):
    environment_id = create_environment(database, name="New model environment")
    network_id = database.configurations.create_network_model_config(
        name="Shared network",
        pow_difficulty=1,
        max_transactions_per_block=20,
        target_block_time_ms=500,
        transaction_fee_rate=0.01,
        parameters={"base_mining_time_ms": 5},
    )
    legacy_reward_id = database.configurations.create_code_artifact(
        artifact_type="reward_function",
        name="Legacy reward",
        entrypoint="calculate_reward",
        source_code="def calculate_reward(context):\n    return 1.0",
        validation_status="valid",
    )
    custom_incentive_id = database.configurations.create_code_artifact(
        artifact_type="incentive_mechanism",
        name="Custom incentive implementation",
        entrypoint="evaluate_incentive",
        source_code=(
            "def evaluate_incentive(context):\n"
            "    return {'reward': 2.0, 'reputation_delta': 0.1}"
        ),
        validation_status="valid",
    )
    incentive_a_id = database.configurations.create_incentive_config(
        name="Legacy reward incentive",
        implementation_type="legacy_reward",
        code_artifact_id=legacy_reward_id,
        parameters={"reward_scale": 1.0},
        metadata={"origin": "legacy_reward_function"},
    )
    incentive_b_id = database.configurations.create_incentive_config(
        name="Contribution incentive",
        implementation_type="custom",
        code_artifact_id=custom_incentive_id,
        parameters={"penalty_weight": 0.25},
    )
    experiment_id = database.configurations.create_experiment_config(
        name="Incentive comparison",
        environment_id=environment_id,
        network_config_id=network_id,
        incentive_a_config_id=incentive_a_id,
        incentive_b_config_id=incentive_b_id,
        poisson_lambda=3,
        duration_seconds=30,
        sample_interval_ms=1_000,
        default_random_seed=42,
    )

    experiment = database.configurations.get_experiment_config(experiment_id)
    assert experiment["comparison_model"] == "incentive_mechanisms"
    assert experiment["network"]["id"] == network_id
    assert experiment["incentive_a"]["implementation_type"] == (
        "legacy_reward"
    )
    assert experiment["incentive_b"]["code_artifacts"][0][
        "artifact_type"
    ] == "incentive_mechanism"
    assert experiment["network_a_config_id"] is None
    assert experiment["network_b_config_id"] is None

    payload = database.configurations.export_configuration_json(
        "full",
        experiment_id,
    )
    exported = json.loads(payload)
    assert exported["schema_version"] == 2
    assert "network_bundle" in exported
    assert "incentive_a_bundle" in exported
    assert "network_a_bundle" not in exported

    target = Database(tmp_path / "new-model-import.sqlite3")
    target.initialize()
    try:
        imported = target.configurations.import_configuration_json(payload)
        imported_experiment = target.configurations.get_experiment_config(
            imported["experiment_config_id"]
        )
        assert imported_experiment["comparison_model"] == (
            "incentive_mechanisms"
        )
        assert imported_experiment["network"]["name"] == "Shared network"
        assert imported_experiment["incentive_a"]["name"] == (
            "Legacy reward incentive"
        )
        assert imported_experiment["incentive_b"]["name"] == (
            "Contribution incentive"
        )
    finally:
        target.close()


@pytest.mark.integration
def test_foreign_key_restrict_and_cascade_behavior(database):
    independent_id = create_environment(
        database,
        name="Independent environment",
        device_count=2,
    )
    independent = database.configurations.get_environment(independent_id)
    device_ids = [device["id"] for device in independent["devices"]]

    database.delete_records(
        "iot_environments",
        {"id": independent_id},
    )
    for device_id in device_ids:
        assert database.get_record(
            "environment_devices",
            {"id": device_id},
            required=False,
        ) is None

    identifiers = create_platform_configuration(database)
    with pytest.raises(DatabaseIntegrityError):
        database.delete_records(
            "iot_environments",
            {"id": identifiers["environment_id"]},
        )
    with pytest.raises(DatabaseIntegrityError):
        database.delete_records(
            "blockchain_network_configs",
            {"id": identifiers["network_a_id"]},
        )


@pytest.mark.integration
def test_full_configuration_export_import_and_bundle_verification(
    configured_database,
    tmp_path,
):
    source, identifiers = configured_database
    payload = source.configurations.export_configuration_json(
        "full",
        identifiers["experiment_id"],
    )

    target = Database(tmp_path / "imported.sqlite3")
    target.initialize()
    try:
        imported = target.configurations.import_configuration_json(payload)
        experiment = target.configurations.get_experiment_config(
            imported["experiment_config_id"]
        )
        bundle = target.configurations.load_configuration_bundle(
            imported["bundle_id"]
        )

        assert len(experiment["environment"]["devices"]) == 6
        assert experiment["network_a"]["name"] == "Network A"
        assert experiment["network_b"]["name"] == "Network B"
        assert bundle["payload"]["bundle_type"] == "full"

        renamed = target.configurations.import_configuration_json(
            payload,
            conflict="rename",
        )
        renamed_experiment = target.configurations.get_experiment_config(
            renamed["experiment_config_id"]
        )
        assert renamed_experiment["name"] != experiment["name"]
    finally:
        target.close()


@pytest.mark.integration
def test_version_one_bundle_remains_importable(configured_database, tmp_path):
    source, identifiers = configured_database
    bundle = source.configurations.export_configuration(
        "full",
        identifiers["experiment_id"],
    )
    bundle["schema_version"] = 1
    experiment = bundle["experiment"]
    experiment.pop("comparison_model", None)
    experiment.pop("network_config_id", None)
    experiment.pop("incentive_a_config_id", None)
    experiment.pop("incentive_b_config_id", None)
    for nested_key in (
        "environment_bundle",
        "network_a_bundle",
        "network_b_bundle",
    ):
        bundle[nested_key]["schema_version"] = 1

    target = Database(tmp_path / "version-one-import.sqlite3")
    target.initialize()
    try:
        imported = target.configurations.import_configuration_json(bundle)
        imported_experiment = target.configurations.get_experiment_config(
            imported["experiment_config_id"]
        )
        assert imported_experiment["comparison_model"] == "legacy_networks"
        assert imported_experiment["network_a"]["name"] == "Network A"
        assert imported_experiment["network_b"]["name"] == "Network B"
    finally:
        target.close()


@pytest.mark.integration
def test_migration_preserves_legacy_experiments_and_run_snapshots(tmp_path):
    database_path = tmp_path / "legacy-v1.sqlite3"
    schema_path = (
        Path(__file__).parents[1] / "src" / "database" / "schema.sql"
    )
    legacy_snapshot = {
        "schema_version": 1,
        "bundle_type": "full",
        "experiment": {"name": "Legacy experiment"},
    }
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        connection.execute(
            """
            INSERT INTO iot_environments (id, name)
            VALUES (1, 'Legacy environment')
            """
        )
        connection.execute(
            """
            INSERT INTO code_artifacts (
                id,
                artifact_type,
                name,
                entrypoint,
                source_code,
                sha256,
                validation_status
            )
            VALUES (
                1,
                'reward_function',
                'Legacy reward',
                'calculate_reward',
                'def calculate_reward(context): return 1',
                ?,
                'valid'
            )
            """,
            ("0" * 64,),
        )
        for network_id, network_name in ((1, "Legacy A"), (2, "Legacy B")):
            connection.execute(
                """
                INSERT INTO blockchain_network_configs (
                    id,
                    name,
                    reward_artifact_id
                )
                VALUES (?, ?, 1)
                """,
                (network_id, network_name),
            )
        connection.execute(
            """
            INSERT INTO experiment_configs (
                id,
                name,
                environment_id,
                network_a_config_id,
                network_b_config_id,
                poisson_lambda,
                duration_seconds,
                sample_interval_ms
            )
            VALUES (1, 'Legacy experiment', 1, 1, 2, 3, 30, 1000)
            """
        )
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id,
                experiment_config_id,
                environment_id,
                name,
                status,
                poisson_lambda,
                duration_seconds,
                sample_interval_ms,
                random_seed,
                configuration_snapshot_json
            )
            VALUES (1, 1, 1, 'Completed legacy run', 'completed',
                    3, 30, 1000, 42, ?)
            """,
            (json.dumps(legacy_snapshot),),
        )
        connection.commit()
    finally:
        connection.close()

    migrated = Database(database_path)
    migrated.initialize()
    try:
        experiment = migrated.get_record(
            "experiment_configs",
            {"id": 1},
        )
        run = migrated.get_record("simulation_runs", {"id": 1})
        versions = {
            row["version"]
            for row in migrated.list_records("schema_migrations")
        }
        violations = migrated.store.execute_query(
            "PRAGMA foreign_key_check"
        )

        assert experiment["comparison_model"] == "legacy_networks"
        assert experiment["network_a_config_id"] == 1
        assert experiment["network_b_config_id"] == 2
        assert run["status"] == "completed"
        assert run["configuration_snapshot_json"] == legacy_snapshot
        assert versions == {1, 2}
        assert violations == []

        migrated.initialize()
        assert len(migrated.list_records("schema_migrations")) == 2
    finally:
        migrated.close()


@pytest.mark.integration
def test_simulation_run_crud_from_experiment(configured_database):
    database, identifiers = configured_database
    run = database.simulations.create_run_from_experiment(
        identifiers["experiment_id"],
        name="Prepared run",
        random_seed=99,
    )

    database.simulations.update_run_status(run["simulation_id"], "running")
    history = database.simulations.get_simulation_history(limit=10)
    network_rows = database.list_records(
        "simulation_networks",
        filters={"simulation_id": run["simulation_id"]},
    )

    assert history["total"] == 1
    assert history["items"][0]["status"] == "running"
    assert {row["network_slot"] for row in network_rows} == {"A", "B"}
    assert run["device_count"] == 6

    database.delete_records(
        "simulation_runs",
        {"id": run["simulation_id"]},
    )
    assert database.get_record(
        "simulation_runs",
        {"id": run["simulation_id"]},
        required=False,
    ) is None
