"""Unit tests for IoT, traffic, metrics, plugins, and blockchain logic."""

from __future__ import annotations

import random

import pytest

from src.core import (
    BlockchainConfig,
    BlockchainEngine,
    BlockchainError,
    DeviceGroupConfig,
    DeviceProfile,
    EnvironmentError,
    IncentiveContext,
    IncentiveOutcome,
    IoTEnvironmentRuntime,
    ParticipationContext,
    PluginExecutionError,
    PluginValidationError,
    ProfitExpectationBehavior,
    RewardIncentiveMechanism,
    SimulationEvent,
    TransactionStatus,
    TransactionType,
)
from src.core.metrics import balance_variance, gini_coefficient
from src.core.plugins import load_plugin_function
from src.core.traffic import PoissonEventGenerator, PoissonTrafficConfig


def _runtime(
    *,
    device_count: int = 2,
    execution_cost: float = 0.5,
    profit_expectation: float = 0.0,
) -> IoTEnvironmentRuntime:
    devices = [
        DeviceProfile(
            database_id=index,
            device_key=f"device-{index}",
            display_name=f"Device {index}",
            initial_balance=10.0,
            precision=0.8 + index * 0.05,
            execution_cost=execution_cost,
            data_rate=float(index),
            profit_expectation=profit_expectation,
            parameters={"base_value": 20.0, "unit": "C"},
        )
        for index in range(1, device_count + 1)
    ]
    return IoTEnvironmentRuntime(devices)


def _blockchain_config(**overrides) -> BlockchainConfig:
    values = {
        "network_name": "Unit network",
        "network_slot": "A",
        "pow_difficulty": 0,
        "max_transactions_per_block": 1,
        "target_block_time_ms": None,
        "transaction_fee_rate": 0.1,
        "parameters": {"base_mining_time_ms": 1},
    }
    values.update(overrides)
    return BlockchainConfig(**values)


def test_device_group_validation_and_runtime_limits():
    valid = DeviceGroupConfig(
        name="Sensors",
        device_count=100,
        precision=1.0,
        execution_cost=0.0,
        data_rate=1.0,
        profit_expectation=0.0,
    )
    valid.validate()

    invalid = DeviceGroupConfig(
        name="",
        device_count=0,
        precision=1.1,
        execution_cost=-1,
        data_rate=-1,
        profit_expectation=-1,
    )
    with pytest.raises(EnvironmentError):
        invalid.validate()
    with pytest.raises(EnvironmentError):
        _runtime(device_count=101)


def test_sensor_data_is_deterministic_and_contains_cost():
    runtime = _runtime()
    first = runtime.generate_sensor_data(
        1,
        15_000,
        random.Random(123),
    )
    second = runtime.generate_sensor_data(
        1,
        15_000,
        random.Random(123),
    )

    assert first == second
    assert first["unit"] == "C"
    assert first["execution_cost"] == 0.5
    assert first["generated_at_ms"] == 15_000


def test_poisson_event_generation_is_reproducible():
    runtime = _runtime(device_count=5)
    config = PoissonTrafficConfig(
        lambda_per_second=3.0,
        duration_seconds=30.0,
    )
    first = PoissonEventGenerator(
        runtime,
        config,
        random_seed=42,
    ).generate()
    second = PoissonEventGenerator(
        runtime,
        config,
        random_seed=42,
    ).generate()

    assert first == second
    assert first
    assert all(
        first[index].scheduled_at_ms <= first[index + 1].scheduled_at_ms
        for index in range(len(first) - 1)
    )
    assert {
        event.event_type for event in first
    } <= set(TransactionType)


def test_blockchain_processes_transfer_feedback_and_iot_reward():
    runtime = _runtime()
    engine = BlockchainEngine(
        simulation_network_id=1,
        environment=runtime,
        config=_blockchain_config(),
        random_seed=7,
        incentive_mechanism=RewardIncentiveMechanism(
            lambda context: 3.5
        ),
    )
    events = [
        SimulationEvent(
            sequence_number=0,
            scheduled_at_ms=100,
            event_type=TransactionType.TRANSFER,
            sender_device_id=1,
            target_device_id=2,
            amount=2.0,
        ),
        SimulationEvent(
            sequence_number=1,
            scheduled_at_ms=200,
            event_type=TransactionType.FEEDBACK,
            sender_device_id=1,
            target_device_id=2,
            payload={"feedback": 1},
        ),
        SimulationEvent(
            sequence_number=2,
            scheduled_at_ms=300,
            event_type=TransactionType.IOT_DATA,
            sender_device_id=1,
            target_device_id=None,
            payload={"value": 21.5},
        ),
    ]

    for event in events:
        engine.process_event(event)
    engine.flush(300)

    sender = engine.device_states[1]
    target = engine.device_states[2]
    assert all(
        transaction.status == TransactionStatus.CONFIRMED
        for transaction in engine.transactions
    )
    assert len(engine.blocks) == 3
    assert sender.balance == pytest.approx(11.3)
    assert sender.cumulative_reward == pytest.approx(3.5)
    assert sender.cumulative_cost == pytest.approx(0.7)
    assert target.balance == pytest.approx(12.0)
    assert target.feedback_score == 1


def test_unprofitable_device_churns_after_data_confirmation():
    runtime = _runtime(
        execution_cost=2.0,
        profit_expectation=0.5,
    )
    engine = BlockchainEngine(
        simulation_network_id=1,
        environment=runtime,
        config=_blockchain_config(transaction_fee_rate=0.0),
        random_seed=7,
        incentive_mechanism=RewardIncentiveMechanism(
            lambda context: 0.0
        ),
    )
    transaction = engine.process_event(
        SimulationEvent(
            sequence_number=0,
            scheduled_at_ms=100,
            event_type=TransactionType.IOT_DATA,
            sender_device_id=1,
            target_device_id=None,
            payload={"value": 20.0},
        )
    )
    engine.flush(100)

    assert transaction.status == TransactionStatus.CONFIRMED
    assert not engine.device_states[1].active
    assert engine.device_states[1].churn_reason


def test_profit_expectation_behavior_uses_incentive_signal():
    behavior = ProfitExpectationBehavior()
    context = ParticipationContext(
        device={"profit_expectation": 0.5},
        device_state={
            "active": True,
            "cumulative_reward": 1.0,
            "cumulative_cost": 2.0,
            "data_submissions": 1,
        },
        action={"action_type": "iot_data"},
        network_outcome={"status": "confirmed"},
    )

    churn_decision = behavior.decide_participation(context)
    retained_decision = behavior.decide_participation(
        ParticipationContext(
            device=context.device,
            device_state=context.device_state,
            action=context.action,
            network_outcome=context.network_outcome,
            incentive_outcome=IncentiveOutcome(
                participation_signal=2.0
            ),
        )
    )

    assert not churn_decision.active
    assert churn_decision.utility == pytest.approx(-1.0)
    assert retained_decision.active
    assert retained_decision.utility == pytest.approx(1.0)


def test_blockchain_rejects_invalid_events_and_backward_time():
    engine = BlockchainEngine(
        simulation_network_id=1,
        environment=_runtime(),
        config=_blockchain_config(),
        random_seed=7,
        incentive_mechanism=RewardIncentiveMechanism(
            lambda context: 3.5
        ),
    )
    rejected = engine.process_event(
        SimulationEvent(
            sequence_number=0,
            scheduled_at_ms=100,
            event_type=TransactionType.TRANSFER,
            sender_device_id=1,
            target_device_id=2,
            amount=100.0,
        )
    )

    assert rejected.status == TransactionStatus.REJECTED
    assert "insufficient" in rejected.rejection_reason.lower()
    with pytest.raises(BlockchainError):
        engine.advance_to(99)


def test_reward_incentive_mechanism_uses_generic_context():
    captured_context = {}

    def reward_function(context):
        captured_context.update(context)
        return 2.25

    mechanism = RewardIncentiveMechanism(reward_function)
    outcome = mechanism.evaluate(
        IncentiveContext(
            network={"name": "Generic", "parameters": {}},
            device={"device_key": "device-1", "precision": 0.9},
            device_state={"feedback_score": 1},
            action={
                "action_type": "iot_data",
                "payload": {"value": 21.5},
                "submitted_at_ms": 100,
            },
            network_outcome={
                "status": "confirmed",
                "confirmed_at_ms": 150,
                "metadata": {},
            },
            elapsed_ms=150,
        )
    )

    assert outcome == IncentiveOutcome(reward=2.25)
    assert captured_context["action"]["action_type"] == "iot_data"
    assert captured_context["network_outcome"]["status"] == "confirmed"


def test_plugin_validation_and_execution_errors():
    valid = load_plugin_function(
        "def reward(context):\n    return math.sqrt(9)",
        "reward",
        plugin_name="valid plugin",
    )
    assert valid({}) == 3

    with pytest.raises(PluginValidationError):
        load_plugin_function(
            "import os\ndef reward(context):\n    return 1",
            "reward",
            plugin_name="importing plugin",
        )
    with pytest.raises(PluginValidationError):
        load_plugin_function(
            "def broken(:\n    return 1",
            "broken",
            plugin_name="syntax plugin",
        )

    failing = load_plugin_function(
        "def reward(context):\n    return 1 / 0",
        "reward",
        plugin_name="failing plugin",
    )
    with pytest.raises(PluginExecutionError):
        failing({})


def test_metric_calculations():
    assert gini_coefficient([]) == 0
    assert gini_coefficient([10, 10, 10]) == pytest.approx(0)
    assert gini_coefficient([0, 10]) == pytest.approx(0.5)
    assert balance_variance([0, 10]) == pytest.approx(25)
