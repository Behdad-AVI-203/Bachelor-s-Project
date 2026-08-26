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
    assert len(stored["comparison_metrics"]) >= 20
    assert {
        metric["details_json"]["category"]
        for metric in stored["comparison_metrics"]
    } == {
        "network_performance",
        "incentive_effectiveness",
    }
    assert set(stored["comparison"]["summary_json"]) >= {
        "network_performance",
        "incentive_effectiveness",
        "combined",
    }
    assert set(stored["network_metrics"]) == {"A", "B"}
    assert progress[0].phase == "preparing"
    assert progress[-1].phase == "completed"
    assert all(
        first.progress <= second.progress
        for first, second in zip(progress, progress[1:], strict=False)
    )
    assert elapsed < 5


@pytest.mark.integration
def test_incentive_comparison_simulation_shares_one_network(
    database,
):
    environment_id = create_environment(
        database,
        name="Incentive environment",
        device_count=4,
    )
    network_id = create_network(
        database,
        name="Shared PoW network",
        reward_code=None,
    )
    reward_a_id = database.configurations.create_code_artifact(
        artifact_type="reward_function",
        name="Incentive A reward",
        entrypoint="calculate_reward",
        source_code=(
            "def calculate_reward(context):\n"
            "    return 1.0\n"
        ),
        validation_status="valid",
    )
    reward_b_id = database.configurations.create_code_artifact(
        artifact_type="incentive_mechanism",
        name="Incentive B reward",
        entrypoint="evaluate_incentive",
        source_code=(
            "def evaluate_incentive(context):\n"
            "    return {\n"
            "        'reward': 0.0,\n"
            "        'penalty_delta': 5.0,\n"
            "        'participation_signal': -10.0,\n"
            "    }\n"
        ),
        validation_status="valid",
    )
    incentive_a_id = database.configurations.create_incentive_config(
        name="Incentive A",
        implementation_type="legacy_reward",
        code_artifact_id=reward_a_id,
    )
    incentive_b_id = database.configurations.create_incentive_config(
        name="Incentive B",
        implementation_type="custom",
        code_artifact_id=reward_b_id,
    )
    experiment_id = database.configurations.create_experiment_config(
        name="Incentive comparison run",
        environment_id=environment_id,
        network_config_id=network_id,
        incentive_a_config_id=incentive_a_id,
        incentive_b_config_id=incentive_b_id,
        poisson_lambda=3,
        duration_seconds=10,
        sample_interval_ms=1_000,
        default_random_seed=42,
        traffic_mix={"iot_data": 1.0},
    )

    result = SimulationEngine(database).run_experiment(
        experiment_id,
        random_seed=42,
    )
    run = database.get_record(
        "simulation_runs",
        {"id": result.simulation_id},
    )
    arms = database.list_records(
        "simulation_networks",
        filters={"simulation_id": result.simulation_id},
        order_by="network_slot",
    )
    stored_events = database.list_records(
        "simulation_events",
        filters={"simulation_id": result.simulation_id},
    )
    stored = database.simulations.get_simulation_results(
        result.simulation_id,
    )

    assert run["configuration_snapshot_json"]["experiment"][
        "comparison_model"
    ] == "incentive_mechanisms"
    assert len(arms) == 2
    assert {arm["network_config_id"] for arm in arms} == {network_id}
    assert {
        arm["configuration_snapshot_json"]["incentive_bundle"][
            "incentive"
        ]["name"]
        for arm in arms
    } == {"Incentive A", "Incentive B"}
    assert result.event_count == len(stored_events)
    transaction_event_ids_a = {
        transaction["event_id"]
        for transaction in stored["recent_transactions"]["A"]
    }
    transaction_event_ids_b = {
        transaction["event_id"]
        for transaction in stored["recent_transactions"]["B"]
    }
    assert transaction_event_ids_b < transaction_event_ids_a
    assert transaction_event_ids_a | transaction_event_ids_b <= {
        event["id"] for event in stored_events
    }
    assert result.network_summaries["B"]["custom_summary_json"][
        "opportunity_participation_rate"
    ] < result.network_summaries["A"]["custom_summary_json"][
        "opportunity_participation_rate"
    ]
    assert result.network_summaries["A"]["total_rewards"] != (
        result.network_summaries["B"]["total_rewards"]
    )


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

    def normalized_blocks(simulation_id):
        return [
            {
                key: block[key]
                for key in (
                    "network_slot",
                    "height",
                    "block_hash",
                    "previous_hash",
                    "mined_at_ms",
                    "nonce",
                    "difficulty",
                    "transaction_count",
                    "total_fees",
                    "total_block_reward",
                    "mining_duration_ms",
                )
            }
            for block in database.simulations.get_blocks(
                simulation_id,
                limit=10_000,
            )
        ]

    assert normalized_blocks(first.simulation_id) == normalized_blocks(
        second.simulation_id
    )


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
