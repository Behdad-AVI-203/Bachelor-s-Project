"""Simulation-level coordination for one comparison arm."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from .behavior import (
    ActionDecisionContext,
    DeviceBehavior,
    ParticipationContext,
    ProfitExpectationBehavior,
)
from .blockchain import BlockchainConfig, PoWNetworkModel
from .evaluation import IncentiveEvaluationAccumulator
from .errors import SimulationError
from .incentives import (
    IncentiveContext,
    IncentiveMechanism,
    IncentiveOutcome,
    RewardIncentiveMechanism,
)
from .models import (
    DeviceAction,
    ExternalOpportunity,
    NetworkDeviceState,
    NetworkTransaction,
    NonParticipationRecord,
    SimulationEvent,
    TransactionStatus,
    TransactionType,
)
from .network import NetworkOutcome


class SimulationArmRuntime:
    """Coordinate network, incentive, and device behavior for one arm."""

    def __init__(
        self,
        *,
        network_model: PoWNetworkModel,
        incentive_mechanism: IncentiveMechanism | None = None,
        device_behavior: DeviceBehavior | None = None,
        random_seed: int = 0,
    ) -> None:
        self.network_model = network_model
        self.incentive_mechanism = (
            incentive_mechanism or RewardIncentiveMechanism()
        )
        self.device_behavior = device_behavior or ProfitExpectationBehavior()
        self.random_seed = int(random_seed)
        self.non_participation_records: list[NonParticipationRecord] = []
        self.last_incentive_outcomes: dict[int, IncentiveOutcome] = {}
        self.opportunities_seen = 0
        self.actions_created = 0
        self.incentive_evaluations = 0
        self.useful_contribution_count = 0
        self._total_reward = 0.0
        self._total_penalty = 0.0
        self.evaluation = IncentiveEvaluationAccumulator()
        self._device_opportunities: dict[int, int] = {}
        self._device_actions: dict[int, int] = {}

    @property
    def simulation_network_id(self) -> int:
        return self.network_model.simulation_network_id

    @property
    def current_time_ms(self) -> int:
        return self.network_model.current_time_ms

    @property
    def config(self) -> BlockchainConfig:
        return self.network_model.config

    @property
    def device_states(self) -> dict[int, NetworkDeviceState]:
        return self.network_model.device_states

    @property
    def transactions(self) -> list[NetworkTransaction]:
        return self.network_model.transactions

    @property
    def blocks(self):
        return self.network_model.blocks

    def process_opportunity(
        self,
        opportunity: ExternalOpportunity,
    ) -> NetworkOutcome | None:
        """Let device behavior decide whether an opportunity becomes action."""
        self.advance_to(opportunity.scheduled_at_ms)
        state = self._get_state(opportunity.sender_device_id)
        device_id = opportunity.sender_device_id
        self.opportunities_seen += 1
        self.evaluation.record_opportunity(opportunity.event_type)
        self._increment_device_count(self._device_opportunities, device_id)

        decision = self.device_behavior.decide_action(
            ActionDecisionContext(
                device=(
                    self._device_context(state)
                    if state is not None
                    else {}
                ),
                device_state=(
                    self._device_state_context(state)
                    if state is not None
                    else {}
                ),
                opportunity=opportunity,
                last_incentive_outcome=(
                    self.last_incentive_outcomes.get(device_id)
                    if device_id is not None
                    else None
                ),
                random_value=self._decision_random_value(opportunity),
            )
        )
        if decision.action is None:
            self.non_participation_records.append(
                NonParticipationRecord(
                    opportunity_sequence=opportunity.sequence_number,
                    scheduled_at_ms=opportunity.scheduled_at_ms,
                    device_id=device_id,
                    reason=decision.reason or "Device chose not to act.",
                )
            )
            return None

        action = decision.action
        if action.database_id is None:
            action.database_id = opportunity.database_id
        self.actions_created += 1
        self.evaluation.record_action(action.event_type)
        self._increment_device_count(self._device_actions, device_id)
        return self.process_action(action)

    def process_action(self, action: DeviceAction) -> NetworkOutcome:
        """Run one action through network, incentive, and behavior stages."""
        self.advance_to(action.scheduled_at_ms)
        outcome = self.network_model.process_action(action)
        transaction = self._transaction_for_outcome(outcome)
        terminal_outcomes = self.network_model.drain_outcomes()
        submission_outcome = next(
            (
                terminal
                for terminal in terminal_outcomes
                if self._transaction_hash(terminal)
                == transaction.transaction_hash
            ),
            outcome,
        )
        self._apply_submission_state(transaction, submission_outcome)
        self._apply_terminal_outcomes(terminal_outcomes)
        return outcome

    def _decision_random_value(
        self,
        opportunity: ExternalOpportunity,
    ) -> float:
        """Return a stable behavior draw independent of execution order."""
        material = {
            "seed": self.random_seed,
            "stream": "device_behavior",
            "sequence_number": opportunity.sequence_number,
            "scheduled_at_ms": opportunity.scheduled_at_ms,
            "sender_device_id": opportunity.sender_device_id,
            "target_device_id": opportunity.target_device_id,
            "event_type": opportunity.event_type.value,
        }
        digest = hashlib.sha256(
            repr(sorted(material.items())).encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    def process_event(self, event: SimulationEvent) -> NetworkTransaction:
        """Compatibility API returning the concrete PoW transaction."""
        return self._transaction_for_outcome(self.process_action(event))

    def advance_to(self, elapsed_ms: int) -> None:
        """Advance network time, then apply completed external policies."""
        self.network_model.advance_to(elapsed_ms)
        self._apply_terminal_outcomes(self.network_model.drain_outcomes())

    def finalize(self, elapsed_ms: int) -> int:
        """Finalize network work, then apply all remaining outcomes."""
        final_time = self.network_model.finalize(elapsed_ms)
        self._apply_terminal_outcomes(self.network_model.drain_outcomes())
        return final_time

    def flush(self, start_at_ms: int) -> int:
        """Compatibility alias for finalizing the simulation arm."""
        return self.finalize(start_at_ms)

    def device_state_records(
        self,
        elapsed_ms: int,
    ) -> list[dict[str, Any]]:
        records = self.network_model.device_state_records(elapsed_ms)
        for record in records:
            device_id = int(record["simulation_device_id"])
            state = self.device_states.get(device_id)
            extra_state = dict(record.get("extra_state_json") or {})
            opportunities = self._device_opportunities.get(device_id, 0)
            actions = self._device_actions.get(device_id, 0)
            extra_state.update(
                {
                    "opportunities_seen": opportunities,
                    "actions_created": actions,
                    "non_participation_count": opportunities - actions,
                }
            )
            if state is not None:
                extra_state.update(
                    {
                        "cumulative_penalties": (
                            state.cumulative_penalties
                        ),
                        "reputation_score": state.reputation_score,
                        "contribution_score": state.contribution_score,
                        "useful_contribution_count": (
                            state.useful_contribution_count
                        ),
                        "last_participation_signal": (
                            state.last_participation_signal
                        ),
                    }
                )
            record["extra_state_json"] = extra_state
        return records

    def metric_record(self, elapsed_ms: int) -> dict[str, Any]:
        record = self.network_model.metric_record(elapsed_ms)
        self.evaluation.record_active_ratio(
            int(record["active_device_count"]),
            len(self.device_states),
        )
        custom_metrics = dict(record.get("custom_metrics_json") or {})
        custom_metrics.update(self._participation_statistics())
        if self.incentive_evaluations:
            custom_metrics.update(
                self.evaluation.summarize(self.device_states.values())
            )
        record["custom_metrics_json"] = custom_metrics
        return record

    def summary_record(self) -> dict[str, Any]:
        record = self.network_model.summary_record()
        custom_summary = dict(record.get("custom_summary_json") or {})
        custom_summary.update(self._participation_statistics())
        if self.incentive_evaluations:
            custom_summary.update(
                self.evaluation.summarize(self.device_states.values())
            )
        record["custom_summary_json"] = custom_summary
        return record

    def block_records(self) -> list[dict[str, Any]]:
        return self.network_model.block_records()

    def transaction_records(
        self,
        block_ids_by_height: Mapping[int, int],
    ) -> list[dict[str, Any]]:
        return self.network_model.transaction_records(block_ids_by_height)

    def _apply_submission_state(
        self,
        transaction: NetworkTransaction,
        outcome: NetworkOutcome,
    ) -> None:
        state = self._get_state(transaction.sender_device_id)
        if state is None:
            return
        state.submitted_transactions += 1
        if outcome.status == TransactionStatus.REJECTED.value:
            state.rejected_transactions += 1

        data_cost_charged = bool(
            outcome.metadata.get("data_cost_charged")
        )
        if (
            transaction.transaction_type == TransactionType.IOT_DATA
            and (outcome.accepted or data_cost_charged)
        ):
            state.data_submissions += 1
            state.cumulative_cost += state.profile.execution_cost

    def _apply_terminal_outcomes(
        self,
        outcomes: list[NetworkOutcome],
    ) -> None:
        for outcome in outcomes:
            transaction = self._transaction_for_outcome(outcome)
            state = self._get_state(transaction.sender_device_id)
            if state is None:
                continue

            if outcome.status == TransactionStatus.CONFIRMED.value:
                state.confirmed_transactions += 1

            if transaction.transaction_type != TransactionType.IOT_DATA:
                continue

            incentive_outcome = None
            if outcome.status == TransactionStatus.CONFIRMED.value:
                incentive_outcome = self.incentive_mechanism.evaluate(
                    self._incentive_context(
                        transaction,
                        state,
                        outcome,
                    )
                )
                self._apply_incentive_outcome(
                    transaction,
                    state,
                    incentive_outcome,
                )
                self.last_incentive_outcomes[
                    state.profile.database_id
                ] = incentive_outcome

            if (
                outcome.status == TransactionStatus.CONFIRMED.value
                or bool(outcome.metadata.get("data_cost_charged"))
            ):
                self._update_participation(
                    transaction,
                    state,
                    outcome,
                    incentive_outcome,
                )

    def _apply_incentive_outcome(
        self,
        transaction: NetworkTransaction,
        state: NetworkDeviceState,
        outcome: IncentiveOutcome,
    ) -> None:
        """Apply incentive effects to runtime state outside the network model."""
        reward_delta = outcome.reward_delta
        penalty_delta = outcome.penalty_delta
        contribution_delta = outcome.contribution_delta

        transaction.reward = reward_delta
        state.balance += reward_delta - penalty_delta
        state.cumulative_reward += reward_delta
        state.cumulative_penalties += penalty_delta
        state.reputation_score += outcome.reputation_delta
        state.contribution_score += contribution_delta
        state.last_participation_signal = outcome.participation_signal

        self.incentive_evaluations += 1
        self._total_reward += reward_delta
        self._total_penalty += penalty_delta

        useful = outcome.details.get("useful_contribution")
        if useful is None:
            useful = outcome.details.get("useful")
        if useful is None:
            useful = contribution_delta > 0
        if bool(useful):
            state.useful_contribution_count += 1
            self.useful_contribution_count += 1
        self.evaluation.record_incentive_outcome(
            outcome,
            useful=bool(useful),
        )

    def _incentive_context(
        self,
        transaction: NetworkTransaction,
        state: NetworkDeviceState,
        outcome: NetworkOutcome,
    ) -> IncentiveContext:
        return IncentiveContext(
            network=self.config.plugin_context(),
            device=self._device_context(state),
            device_state=self._device_state_context(state),
            action=self._action_context(transaction),
            network_outcome=outcome.to_context(),
            elapsed_ms=outcome.finalized_at_ms or outcome.submitted_at_ms,
            legacy_reward_override=transaction.reward_override,
        )

    def _update_participation(
        self,
        transaction: NetworkTransaction,
        state: NetworkDeviceState,
        outcome: NetworkOutcome,
        incentive_outcome: IncentiveOutcome | None,
    ) -> None:
        decision = self.device_behavior.decide_participation(
            ParticipationContext(
                device=self._device_context(state),
                device_state=self._device_state_context(state),
                action=self._action_context(transaction),
                network_outcome=outcome.to_context(),
                incentive_outcome=incentive_outcome,
            )
        )
        state.active = decision.active
        state.churn_reason = decision.reason if not decision.active else None

    def _transaction_for_outcome(
        self,
        outcome: NetworkOutcome,
    ) -> NetworkTransaction:
        return self.network_model.get_transaction(
            self._transaction_hash(outcome)
        )

    @staticmethod
    def _transaction_hash(outcome: NetworkOutcome) -> str:
        transaction_hash = outcome.metadata.get("transaction_hash")
        if not isinstance(transaction_hash, str) or not transaction_hash:
            raise SimulationError(
                "A network outcome is missing its transaction reference."
            )
        return transaction_hash

    def _get_state(
        self,
        device_id: int | None,
    ) -> NetworkDeviceState | None:
        if device_id is None:
            return None
        return self.device_states.get(device_id)

    @staticmethod
    def _increment_device_count(
        counts: dict[int, int],
        device_id: int | None,
    ) -> None:
        if device_id is not None:
            counts[device_id] = counts.get(device_id, 0) + 1

    def _participation_statistics(self) -> dict[str, Any]:
        no_action_count = len(self.non_participation_records)
        participation_rate = (
            self.actions_created / self.opportunities_seen
            if self.opportunities_seen
            else 0
        )
        states = list(self.device_states.values())
        device_count = len(states)
        statistics = {
            "opportunities_seen": self.opportunities_seen,
            "actions_created": self.actions_created,
            "non_participation_count": no_action_count,
            "opportunity_participation_rate": participation_rate,
        }
        if self.incentive_evaluations:
            statistics.update(
                {
                    "participation_rate": participation_rate,
                    "average_reward": (
                        self._total_reward / self.incentive_evaluations
                    ),
                    "average_penalty": (
                        self._total_penalty / self.incentive_evaluations
                    ),
                    "average_reputation": (
                        sum(state.reputation_score for state in states)
                        / device_count
                        if device_count
                        else 0
                    ),
                    "average_contribution": (
                        sum(state.contribution_score for state in states)
                        / device_count
                        if device_count
                        else 0
                    ),
                    "useful_contribution_count": (
                        self.useful_contribution_count
                    ),
                    "incentive_evaluation_count": (
                        self.incentive_evaluations
                    ),
                }
            )
        return statistics

    @staticmethod
    def _device_context(state: NetworkDeviceState) -> dict[str, Any]:
        return {
            "device_key": state.profile.device_key,
            "precision": state.profile.precision,
            "execution_cost": state.profile.execution_cost,
            "data_rate": state.profile.data_rate,
            "profit_expectation": state.profile.profit_expectation,
            "parameters": dict(state.profile.parameters),
        }

    @staticmethod
    def _device_state_context(
        state: NetworkDeviceState,
    ) -> dict[str, Any]:
        return {
            "balance": state.balance,
            "active": state.active,
            "churn_reason": state.churn_reason,
            "feedback_score": state.feedback_score,
            "cumulative_reward": state.cumulative_reward,
            "cumulative_penalties": state.cumulative_penalties,
            "cumulative_cost": state.cumulative_cost,
            "reputation_score": state.reputation_score,
            "contribution_score": state.contribution_score,
            "useful_contribution_count": (
                state.useful_contribution_count
            ),
            "last_participation_signal": (
                state.last_participation_signal
            ),
            "data_submissions": state.data_submissions,
        }

    @staticmethod
    def _action_context(
        transaction: NetworkTransaction,
    ) -> dict[str, Any]:
        return {
            "action_type": transaction.transaction_type.value,
            "sender_device_id": transaction.sender_device_id,
            "target_device_id": transaction.target_device_id,
            "amount": transaction.amount,
            "payload": dict(transaction.payload),
            "submitted_at_ms": transaction.submitted_at_ms,
        }
