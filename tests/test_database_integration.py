"""Integration coverage for schema, CRUD, and portable configurations."""

from __future__ import annotations

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
