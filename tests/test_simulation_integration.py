"""End-to-end simulation engine and persistence tests."""

from __future__ import annotations

from time import perf_counter

import pytest

from src.core import PluginExecutionError, SimulationEngine

from .factories import (
    DEFAULT_REWARD_CODE,
    create_environment,
    create_experiment,
    create_network,
)


@pytest.mark.integration
def test_dual_network_simulation_uses_one_event_stream(
    configured_database,
):
    database, identifiers = configured_database
    progress = []
    started = perf_counter()
    result = SimulationEngine(database).run_experiment(
        identifiers["experiment_id"],
        name="30-second comparison",
        random_seed=42,
        progress_callback=progress.append,
    )
    elapsed = perf_counter() - started

    stored = database.simulations.get_simulation_results(
        result.simulation_id
    )
    events = database.list_records(
        "simulation_events",
        filters={"simulation_id": result.simulation_id},
    )
    transactions_a = stored["recent_transactions"]["A"]
    transactions_b = stored["recent_transactions"]["B"]

    assert result.event_count == len(events)
    assert len(transactions_a) == result.event_count
    assert len(transactions_b) == result.event_count
    assert {
        transaction["event_id"] for transaction in transactions_a
    } == {
        transaction["event_id"] for transaction in transactions_b
    }
    assert {
        network["status"] for network in stored["networks"]
    } == {"completed"}
    assert stored["run"]["status"] == "completed"
    assert len(stored["comparison_metrics"]) == 10
    assert set(stored["network_metrics"]) == {"A", "B"}
    assert progress[0].phase == "preparing"
    assert progress[-1].phase == "completed"
    assert all(
        first.progress <= second.progress
        for first, second in zip(progress, progress[1:], strict=False)
    )
    assert elapsed < 5


@pytest.mark.integration
def test_simulation_is_reproducible_for_same_seed(configured_database):
    database, identifiers = configured_database
    first = SimulationEngine(database).run_experiment(
        identifiers["experiment_id"],
        name="First deterministic run",
        random_seed=42,
    )
    second = SimulationEngine(database).run_experiment(
        identifiers["experiment_id"],
        name="Second deterministic run",
        random_seed=42,
    )

    first_events = database.list_records(
        "simulation_events",
        filters={"simulation_id": first.simulation_id},
        order_by="sequence_number",
    )
    second_events = database.list_records(
        "simulation_events",
        filters={"simulation_id": second.simulation_id},
        order_by="sequence_number",
    )
    comparable_fields = (
        "sequence_number",
        "scheduled_at_ms",
        "event_type",
        "amount",
        "payload_json",
    )

    assert [
        tuple(event[field] for field in comparable_fields)
        for event in first_events
    ] == [
        tuple(event[field] for field in comparable_fields)
        for event in second_events
    ]
    normalized_first = {
        slot: {
            key: value
            for key, value in summary.items()
            if key != "simulation_network_id"
        }
        for slot, summary in first.network_summaries.items()
    }
    normalized_second = {
        slot: {
            key: value
            for key, value in summary.items()
            if key != "simulation_network_id"
        }
        for slot, summary in second.network_summaries.items()
    }
    assert normalized_first == normalized_second


@pytest.mark.integration
def test_plugin_failure_marks_run_and_both_networks_failed(database):
    environment_id = create_environment(database, device_count=3)
    failing_code = """def calculate_reward(context):
    payload = context["transaction"]["payload"]
    return 1.0 / (0 if "value" in payload else 1)
"""
    network_a_id = create_network(
        database,
        name="Failing network",
        reward_code=failing_code,
    )
    network_b_id = create_network(
        database,
        name="Healthy network",
        reward_code=DEFAULT_REWARD_CODE,
    )
    experiment_id = create_experiment(
        database,
        environment_id=environment_id,
        network_a_id=network_a_id,
        network_b_id=network_b_id,
        duration_seconds=5,
        poisson_lambda=2,
        traffic_mix={"iot_data": 1.0},
    )

    with pytest.raises(PluginExecutionError):
        SimulationEngine(database).run_experiment(experiment_id)

    history = database.simulations.get_simulation_history(limit=1)
    run = history["items"][0]
    networks = database.list_records(
        "simulation_networks",
        filters={"simulation_id": run["id"]},
    )
    assert run["status"] == "failed"
    assert run["error_message"]
    assert {network["status"] for network in networks} == {"failed"}


@pytest.mark.performance
@pytest.mark.integration
def test_virtual_clock_handles_100_devices_quickly(database):
    environment_id = create_environment(
        database,
        name="Maximum scale environment",
        device_count=100,
    )
    network_a_id = create_network(
        database,
        name="Performance A",
        difficulty=0,
        parameters={"base_mining_time_ms": 1},
    )
    network_b_id = create_network(
        database,
        name="Performance B",
        difficulty=1,
        parameters={"base_mining_time_ms": 1},
    )
    experiment_id = create_experiment(
        database,
        environment_id=environment_id,
        network_a_id=network_a_id,
        network_b_id=network_b_id,
        duration_seconds=600,
        poisson_lambda=5,
        sample_interval_ms=5_000,
        random_seed=42,
    )

    started = perf_counter()
    result = SimulationEngine(database).run_experiment(experiment_id)
    elapsed = perf_counter() - started

    assert result.event_count > 2_500
    assert result.final_virtual_time_ms >= 600_000
    assert elapsed < 20
