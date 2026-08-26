"""Unit tests for IoT, traffic, metrics, plugins, and blockchain logic."""

from __future__ import annotations

import inspect
import random

import pytest

from src.core import (
    ActionDecisionContext,
    BlockchainConfig,
    BlockchainEngine,
    BlockchainError,
    ConnectivityOutcome,
    DeviceAction,
    DeviceGroupConfig,
    DeviceProfile,
    EnvironmentError,
    ExternalOpportunity,
    IncentiveContext,
    IncentiveOutcome,
    IoTEnvironmentRuntime,
    NetworkModel,
    NetworkDeviceState,
    NetworkOutcome,
    ParticipationContext,
    PluginIncentiveMechanism,
    PluginExecutionError,
    PluginValidationError,
    PoWNetworkModel,
    ProbabilisticConnectivityPolicy,
    ProfitExpectationBehavior,
    RewardIncentiveMechanism,
    SimulationArmRuntime,
    SimulationEvent,
    TransactionStatus,
    TransactionType,
)
from src.core.metrics import (
    balance_variance,
    gini_coefficient,
    weighted_incentive_effectiveness,
    weighted_score_comparison,
)
from src.core.plugins import load_plugin_function
from src.core.traffic import PoissonEventGenerator, PoissonTrafficConfig
import src.core.orchestration as orchestration_module


class MockNetworkModel:
    """Minimal non-PoW implementation used to exercise orchestration."""

    current_time_ms = 0
    simulation_network_id = 99
    legacy_reward_compatibility = False

    def __init__(self, environment):
        self.device_states = {
            device.database_id: NetworkDeviceState(
                profile=device,
                balance=device.initial_balance,
            )
            for device in environment.devices
        }
        self._outcomes = []
        self.rewards = {}

    def process_action(self, action):
        outcome = NetworkOutcome(
            action_id=action.database_id,
            status=TransactionStatus.CONFIRMED.value,
            accepted=True,
            submitted_at_ms=action.scheduled_at_ms,
            finalized_at_ms=action.scheduled_at_ms,
            metadata={"mock": True},
        )
        self._outcomes.append(outcome)
        return outcome

    def advance_to(self, elapsed_ms):
        self.current_time_ms = max(self.current_time_ms, elapsed_ms)

    def finalize(self, elapsed_ms):
        self.advance_to(elapsed_ms)
        return self.current_time_ms

    def drain_outcomes(self):
        outcomes, self._outcomes = self._outcomes, []
        return outcomes

    def network_context(self):
        return {"name": "Mock network", "parameters": {}}

    def action_context_for_outcome(self, outcome):
        return {
            "action_type": TransactionType.IOT_DATA.value,
            "sender_device_id": 1,
            "target_device_id": None,
            "amount": 0,
            "payload": {"value": 20.0},
            "submitted_at_ms": outcome.submitted_at_ms,
        }

    def record_incentive_effect(self, outcome, reward):
        self.rewards[outcome.action_id] = reward

    def device_state_records(self, elapsed_ms):
        return [
            {
                "simulation_network_id": self.simulation_network_id,
                "simulation_device_id": state.profile.database_id,
                "elapsed_ms": elapsed_ms,
                "balance": state.balance,
                "cumulative_reward": state.cumulative_reward,
                "cumulative_cost": state.cumulative_cost,
                "cumulative_profit": state.cumulative_profit,
                "feedback_score": state.feedback_score,
                "submitted_transactions": state.submitted_transactions,
                "confirmed_transactions": state.confirmed_transactions,
                "rejected_transactions": state.rejected_transactions,
                "is_active": state.active,
                "churn_reason": state.churn_reason,
                "extra_state_json": {},
            }
            for state in self.device_states.values()
        ]

    def metric_record(self, elapsed_ms):
        active = sum(state.active for state in self.device_states.values())
        return {
            "simulation_network_id": self.simulation_network_id,
            "elapsed_ms": elapsed_ms,
            "active_device_count": active,
            "custom_metrics_json": {},
        }

    def summary_record(self):
        active = sum(state.active for state in self.device_states.values())
        return {
            "simulation_network_id": self.simulation_network_id,
            "final_active_devices": active,
            "custom_summary_json": {},
        }

    def persistence_records(self, reference_ids=None):
        return [], []

    def persist_to_database(self, database, *, chunk_size=1_000):
        return None

    def compatibility_view(self, view_name):
        return []


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


def _simulation_arm(
    *,
    runtime: IoTEnvironmentRuntime | None = None,
    config: BlockchainConfig | None = None,
    reward_function=None,
) -> SimulationArmRuntime:
    return SimulationArmRuntime(
        network_model=PoWNetworkModel(
            simulation_network_id=1,
            environment=runtime or _runtime(),
            config=config or _blockchain_config(),
            random_seed=7,
        ),
        incentive_mechanism=RewardIncentiveMechanism(
            reward_function or (lambda context: 3.5)
        ),
    )


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
        isinstance(opportunity, ExternalOpportunity)
        for opportunity in first
    )
    assert all(
        first[index].scheduled_at_ms <= first[index + 1].scheduled_at_ms
        for index in range(len(first) - 1)
    )
    assert {
        event.event_type for event in first
    } <= set(TransactionType)


def test_blockchain_processes_transfer_feedback_and_iot_reward():
    runtime = _runtime()
    engine = _simulation_arm(runtime=runtime)
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
    engine = _simulation_arm(
        runtime=runtime,
        config=_blockchain_config(transaction_fee_rate=0.0),
        reward_function=lambda context: 0.0,
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


def test_incentive_comparison_network_plugin_cannot_emit_rewards():
    config = _blockchain_config(
        legacy_reward_compatibility=False,
        transaction_logic=lambda context: {"reward": 99.0},
    )
    model = PoWNetworkModel(
        simulation_network_id=1,
        environment=_runtime(execution_cost=0.0),
        config=config,
        random_seed=7,
    )

    with pytest.raises(
        BlockchainError,
        match="cannot provide incentive outputs",
    ):
        model.process_event(
            SimulationEvent(
                sequence_number=0,
                scheduled_at_ms=100,
                event_type=TransactionType.IOT_DATA,
                sender_device_id=1,
                target_device_id=None,
                payload={"value": 20.0},
            )
        )


def test_incentive_reward_does_not_read_network_parameters():
    mechanism = RewardIncentiveMechanism(
        parameters={"base_iot_reward": 2.0},
        use_network_parameters=False,
    )
    context = IncentiveContext(
        network={
            "transaction_fee_rate": 0.01,
            "parameters": {"base_iot_reward": 100.0},
        },
        device={"precision": 0.5},
        device_state={"feedback_score": 0.0},
        action={"payload": {}},
        network_outcome={"status": "confirmed"},
        elapsed_ms=0,
    )

    assert mechanism.evaluate(context).reward_delta == pytest.approx(1.0)


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


def test_incentive_outcome_supports_generic_delta_names():
    outcome = IncentiveOutcome(
        reward_delta=2.0,
        penalty_delta=0.5,
        contribution_delta=1.25,
        reputation_delta=-0.2,
        participation_signal=0.4,
    )

    assert outcome.reward == pytest.approx(2.0)
    assert outcome.reward_delta == pytest.approx(2.0)
    assert outcome.penalty == pytest.approx(0.5)
    assert outcome.penalty_delta == pytest.approx(0.5)
    assert outcome.contribution_score == pytest.approx(1.25)
    assert outcome.contribution_delta == pytest.approx(1.25)


def test_incentive_state_updates_and_metrics_are_persistable():
    runtime = _runtime(execution_cost=0.0)
    arm = SimulationArmRuntime(
        network_model=PoWNetworkModel(
            simulation_network_id=1,
            environment=runtime,
            config=_blockchain_config(transaction_fee_rate=0.0),
            random_seed=7,
        ),
        incentive_mechanism=PluginIncentiveMechanism(
            lambda context: {
                "reward_delta": 2.0,
                "penalty_delta": 0.5,
                "reputation_delta": 0.25,
                "contribution_delta": 1.0,
                "participation_signal": 0.5,
                "details": {"useful_contribution": True},
            }
        ),
    )
    arm.process_opportunity(
        ExternalOpportunity(
            sequence_number=0,
            scheduled_at_ms=100,
            event_type=TransactionType.IOT_DATA,
            sender_device_id=1,
            target_device_id=None,
            payload={"value": 20.0},
        )
    )
    arm.flush(100)

    state = arm.device_states[1]
    assert state.balance == pytest.approx(11.5)
    assert state.cumulative_reward == pytest.approx(2.0)
    assert state.cumulative_penalties == pytest.approx(0.5)
    assert state.reputation_score == pytest.approx(0.25)
    assert state.contribution_score == pytest.approx(1.0)
    assert state.useful_contribution_count == 1
    assert state.last_participation_signal == pytest.approx(0.5)

    summary = arm.summary_record()["custom_summary_json"]
    assert summary["participation_rate"] == pytest.approx(1.0)
    assert summary["average_reward"] == pytest.approx(2.0)
    assert summary["average_penalty"] == pytest.approx(0.5)
    assert summary["average_reputation"] == pytest.approx(0.125)
    assert summary["average_contribution"] == pytest.approx(0.5)
    assert summary["useful_contribution_count"] == 1
    assert summary["final_retention_rate"] == pytest.approx(1.0)
    assert summary["average_active_device_ratio"] == pytest.approx(1.0)
    assert summary["opportunity_participation_rate"] == pytest.approx(1.0)
    assert summary["useful_contribution_rate"] == pytest.approx(1.0)
    assert summary[
        "useful_contribution_per_active_device"
    ] == pytest.approx(0.5)
    assert summary["total_rewards"] == pytest.approx(2.0)
    assert summary["total_penalties"] == pytest.approx(0.5)
    assert summary["net_incentive_cost"] == pytest.approx(1.5)
    assert summary[
        "incentive_cost_per_useful_contribution"
    ] == pytest.approx(1.5)
    assert summary["reward_distribution_fairness"] == pytest.approx(0.5)
    assert summary["utility_distribution_fairness"] == pytest.approx(0.5)

    state_record = arm.device_state_records(100)[0]["extra_state_json"]
    assert state_record["cumulative_penalties"] == pytest.approx(0.5)
    assert state_record["reputation_score"] == pytest.approx(0.25)
    assert state_record["contribution_score"] == pytest.approx(1.0)


def test_penalties_and_reputation_influence_future_participation():
    behavior = ProfitExpectationBehavior()
    decision = behavior.decide_participation(
        ParticipationContext(
            device={"profit_expectation": 0.0},
            device_state={
                "active": True,
                "cumulative_reward": 1.0,
                "cumulative_penalties": 2.0,
                "cumulative_cost": 0.0,
                "data_submissions": 1,
                "reputation_score": -1.0,
                "contribution_score": 0.0,
            },
            action={"action_type": "iot_data"},
            network_outcome={"status": "confirmed"},
        )
    )

    assert not decision.active
    assert decision.utility == pytest.approx(-1.1)


def test_weighted_comparison_separates_metric_categories():
    result = weighted_score_comparison(
        {
            "network_performance": [
                {"winner_slot": "B"},
                {"winner_slot": "B"},
            ],
            "incentive_effectiveness": [
                {"winner_slot": "A"},
                {"winner_slot": "A"},
            ],
        },
        weights={
            "network_performance": 0.25,
            "incentive_effectiveness": 0.75,
        },
    )

    assert result["categories"]["network_performance"]["winner_slot"] == "B"
    assert (
        result["categories"]["incentive_effectiveness"]["winner_slot"]
        == "A"
    )
    assert result["score_a"] == pytest.approx(0.75)
    assert result["score_b"] == pytest.approx(0.25)
    assert result["winner_slot"] == "A"


def test_incentive_score_ignores_network_context_metrics():
    incentive_metrics = [
        {
            "metric_name": "opportunity_participation_rate",
            "value_a": 0.8,
            "value_b": 0.6,
            "preferred_direction": "higher",
            "details_json": {"dimension": "participation"},
        },
        {
            "metric_name": "final_retention_rate",
            "value_a": 0.7,
            "value_b": 0.5,
            "preferred_direction": "higher",
            "details_json": {"dimension": "retention"},
        },
    ]
    network_a_fast = [
        {"winner_slot": "A"},
        {"winner_slot": "A"},
    ]
    network_b_fast = [
        {"winner_slot": "B"},
        {"winner_slot": "B"},
    ]

    first = weighted_incentive_effectiveness(incentive_metrics)
    second = weighted_incentive_effectiveness(incentive_metrics)

    assert first == second
    assert first["winner_slot"] == "A"
    assert weighted_score_comparison(
        {"network_performance": network_a_fast},
        weights={"network_performance": 1.0},
    )["winner_slot"] == "A"
    assert weighted_score_comparison(
        {"network_performance": network_b_fast},
        weights={"network_performance": 1.0},
    )["winner_slot"] == "B"


def test_incentive_score_uses_one_metric_per_dimension():
    metrics = [
        {
            "metric_name": "participation",
            "value_a": 0.9,
            "value_b": 0.5,
            "preferred_direction": "higher",
            "details_json": {"dimension": "participation"},
        },
        {
            "metric_name": "retention",
            "value_a": 0.8,
            "value_b": 0.6,
            "preferred_direction": "higher",
            "details_json": {"dimension": "retention"},
        },
        {
            "metric_name": "correlated_participation",
            "value_a": 1.0,
            "value_b": 0.0,
            "preferred_direction": "higher",
            "details_json": {"dimension": "participation"},
        },
    ]

    result = weighted_incentive_effectiveness(metrics)

    assert set(result["dimensions"]) == {"participation", "retention"}
    assert result["dimensions"]["participation"]["metric_name"] == (
        "participation"
    )


def test_default_behavior_turns_active_opportunity_into_action():
    behavior = ProfitExpectationBehavior()
    opportunity = ExternalOpportunity(
        sequence_number=0,
        scheduled_at_ms=100,
        event_type=TransactionType.IOT_DATA,
        sender_device_id=1,
        target_device_id=None,
        payload={"value": 20.0},
        database_id=7,
    )

    active_decision = behavior.decide_action(
        ActionDecisionContext(
            device={"device_key": "device-1"},
            device_state={"active": True},
            opportunity=opportunity,
        )
    )
    inactive_decision = behavior.decide_action(
        ActionDecisionContext(
            device={"device_key": "device-1"},
            device_state={
                "active": False,
                "churn_reason": "Participation ended.",
            },
            opportunity=opportunity,
        )
    )

    assert active_decision.action is not None
    assert active_decision.action.database_id == opportunity.database_id
    assert inactive_decision.action is None
    assert inactive_decision.reason == "Participation ended."


def test_opportunity_copies_isolate_arm_local_payload_changes():
    opportunity = ExternalOpportunity(
        sequence_number=0,
        scheduled_at_ms=100,
        event_type=TransactionType.IOT_DATA,
        sender_device_id=1,
        target_device_id=None,
        payload={"value": 20.0},
    )

    arm_a_opportunity = opportunity.copy()
    arm_b_opportunity = opportunity.copy()
    arm_a_opportunity.payload["value"] = 99.0

    assert arm_b_opportunity.payload["value"] == 20.0
    assert opportunity.payload["value"] == 20.0


def test_shared_opportunity_can_produce_action_or_no_action_per_arm():
    opportunity = ExternalOpportunity(
        sequence_number=0,
        scheduled_at_ms=100,
        event_type=TransactionType.IOT_DATA,
        sender_device_id=1,
        target_device_id=None,
        payload={"value": 20.0},
        database_id=9,
    )
    active_arm = _simulation_arm()
    inactive_arm = _simulation_arm(
        config=_blockchain_config(network_slot="B")
    )
    inactive_arm.device_states[1].active = False
    inactive_arm.device_states[1].churn_reason = "Participation ended."

    active_outcome = active_arm.process_opportunity(opportunity)
    inactive_outcome = inactive_arm.process_opportunity(opportunity)

    assert active_outcome is not None
    assert inactive_outcome is None
    assert len(active_arm.transactions) == 1
    assert inactive_arm.transactions == []
    assert inactive_arm.non_participation_records[0].opportunity_sequence == 0
    assert active_arm.metric_record(100)["custom_metrics_json"] == {
        "pending_mining": True,
        "virtual_time_ms": 100,
        "opportunities_seen": 1,
        "actions_created": 1,
        "non_participation_count": 0,
        "opportunity_participation_rate": 1.0,
    }
    assert inactive_arm.summary_record()["custom_summary_json"][
        "non_participation_count"
    ] == 1


def test_blockchain_rejects_invalid_events_and_backward_time():
    engine = _simulation_arm()
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


def test_pow_model_implements_network_model_contract():
    model = PoWNetworkModel(
        simulation_network_id=1,
        environment=_runtime(),
        config=_blockchain_config(),
        random_seed=7,
    )
    outcome = model.process_action(
        SimulationEvent(
            sequence_number=0,
            scheduled_at_ms=100,
            event_type=TransactionType.TRANSFER,
            sender_device_id=1,
            target_device_id=2,
            amount=100.0,
        )
    )

    assert isinstance(model, NetworkModel)
    assert isinstance(outcome, NetworkOutcome)
    assert not outcome.accepted
    assert outcome.status == TransactionStatus.REJECTED.value
    assert BlockchainEngine is PoWNetworkModel
    assert not hasattr(model, "incentive_mechanism")
    assert not hasattr(model, "device_behavior")


def test_pow_model_emits_outcomes_without_applying_external_policies():
    model = PoWNetworkModel(
        simulation_network_id=1,
        environment=_runtime(),
        config=_blockchain_config(),
        random_seed=7,
    )
    transaction = model.process_event(
        SimulationEvent(
            sequence_number=0,
            scheduled_at_ms=100,
            event_type=TransactionType.IOT_DATA,
            sender_device_id=1,
            target_device_id=None,
            payload={"value": 20.0},
        )
    )
    model.finalize(100)
    outcomes = model.drain_outcomes()

    assert transaction.status == TransactionStatus.CONFIRMED
    assert transaction.reward == 0
    assert model.device_states[1].cumulative_reward == 0
    assert [outcome.status for outcome in outcomes] == ["confirmed"]


def _connectivity_action() -> DeviceAction:
    return DeviceAction(
        sequence_number=4,
        scheduled_at_ms=250,
        event_type=TransactionType.IOT_DATA,
        sender_device_id=1,
        target_device_id=None,
        payload={"value": 20.0},
        database_id=44,
    )


def test_connectivity_policy_is_deterministic_for_same_seed_and_action():
    policy_a = ProbabilisticConnectivityPolicy(
        random_seed=42,
        link_availability_probability=0.6,
        packet_delivery_success_probability=0.7,
        communication_failure_probability=0.1,
        latency_distribution_ms=(0, 5, 10),
    )
    policy_b = ProbabilisticConnectivityPolicy(
        random_seed=42,
        link_availability_probability=0.6,
        packet_delivery_success_probability=0.7,
        communication_failure_probability=0.1,
        latency_distribution_ms=(0, 5, 10),
    )

    first = policy_a.evaluate(_connectivity_action())
    second = policy_b.evaluate(_connectivity_action())

    assert isinstance(first, ConnectivityOutcome)
    assert first == second


def test_connectivity_configuration_changes_pow_network_outcome():
    action = _connectivity_action()
    available = PoWNetworkModel(
        simulation_network_id=1,
        environment=_runtime(execution_cost=0.0),
        config=_blockchain_config(
            connectivity_policy=ProbabilisticConnectivityPolicy(
                random_seed=42,
                link_availability_probability=1.0,
            )
        ),
        random_seed=42,
    )
    unavailable = PoWNetworkModel(
        simulation_network_id=2,
        environment=_runtime(execution_cost=0.0),
        config=_blockchain_config(
            connectivity_policy=ProbabilisticConnectivityPolicy(
                random_seed=42,
                link_availability_probability=0.0,
            )
        ),
        random_seed=42,
    )

    available_outcome = available.process_action(action)
    unavailable_outcome = unavailable.process_action(_connectivity_action())

    assert available_outcome.accepted
    assert unavailable_outcome.status == TransactionStatus.REJECTED.value
    assert unavailable_outcome.metadata["connectivity"]["delivered"] is False


def test_ab_networks_receive_identical_connectivity_conditions():
    policy_a = ProbabilisticConnectivityPolicy(
        random_seed=99,
        link_availability_probability=0.5,
        packet_delivery_success_probability=0.8,
        latency_distribution_ms=(2, 8),
    )
    policy_b = ProbabilisticConnectivityPolicy(
        random_seed=99,
        link_availability_probability=0.5,
        packet_delivery_success_probability=0.8,
        latency_distribution_ms=(2, 8),
    )
    model_a = PoWNetworkModel(
        simulation_network_id=1,
        environment=_runtime(execution_cost=0.0),
        config=_blockchain_config(
            connectivity_policy=policy_a,
        ),
        random_seed=99,
    )
    model_b = PoWNetworkModel(
        simulation_network_id=2,
        environment=_runtime(execution_cost=0.0),
        config=_blockchain_config(
            network_slot="B",
            connectivity_policy=policy_b,
        ),
        random_seed=99,
    )

    outcome_a = model_a.process_action(_connectivity_action())
    outcome_b = model_b.process_action(_connectivity_action())

    assert outcome_a.metadata["connectivity"] == (
        outcome_b.metadata["connectivity"]
    )


def test_simulation_arm_runs_with_generic_non_pow_network():
    runtime = _runtime(device_count=1, execution_cost=0.0)
    assert isinstance(MockNetworkModel(runtime), NetworkModel)
    arm = SimulationArmRuntime(
        network_model=MockNetworkModel(runtime),
        incentive_mechanism=RewardIncentiveMechanism(
            lambda context: 2.0
        ),
    )

    outcome = arm.process_opportunity(
        ExternalOpportunity(
            sequence_number=0,
            scheduled_at_ms=100,
            event_type=TransactionType.IOT_DATA,
            sender_device_id=1,
            target_device_id=None,
            payload={"value": 20.0},
            database_id=7,
        )
    )

    assert outcome is not None
    assert arm.device_states[1].cumulative_reward == pytest.approx(2.0)
    assert arm.network_model.rewards[7] == pytest.approx(2.0)


def test_network_model_registry_resolves_configured_type():
    from src.core import NetworkModelRegistry

    registry = NetworkModelRegistry()
    marker = object()
    registry.register("mock", lambda **kwargs: marker)

    assert registry.create("MOCK") is marker


def test_registry_pow_factory_preserves_pow_behavior():
    from src.core.simulation import SimulationEngine

    config = _blockchain_config()
    kwargs = {
        "simulation_network_id": 1,
        "environment": _runtime(execution_cost=0.0),
        "config": config,
        "random_seed": 17,
    }
    direct = PoWNetworkModel(**kwargs)
    resolved = SimulationEngine._default_network_model_registry().create(
        "pow",
        **kwargs,
    )
    action = _connectivity_action()

    assert isinstance(resolved, PoWNetworkModel)
    assert direct.process_action(action) == resolved.process_action(action)


def test_simulation_persistence_uses_network_adapter_not_block_tables():
    class FakeDatabase:
        class Simulations:
            def __init__(self):
                self.summaries = []

            def upsert_network_summary(self, summary):
                self.summaries.append(summary)

        def __init__(self):
            self.simulations = self.Simulations()

    class AdapterArm:
        simulation_network_id = 1

        def __init__(self):
            self.persisted = False

        def persist_to_database(self, database, *, chunk_size):
            self.persisted = True

        def summary_record(self):
            return {
                "simulation_network_id": self.simulation_network_id,
                "custom_summary_json": {},
            }

    database = FakeDatabase()
    engine = object.__new__(__import__(
        "src.core.simulation",
        fromlist=["SimulationEngine"],
    ).SimulationEngine)
    engine.database = database
    engine.persistence_batch_size = 10
    arm = AdapterArm()

    engine._persist_network_results({"A": arm})

    assert arm.persisted
    assert database.simulations.summaries


def test_orchestration_has_no_pow_type_imports():
    source = inspect.getsource(orchestration_module)
    assert "PoWNetworkModel" not in source
    assert "BlockchainConfig" not in source


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
