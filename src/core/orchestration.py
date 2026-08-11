"""Simulation-level coordination for one comparison arm."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .behavior import (
    DeviceBehavior,
    ParticipationContext,
    ProfitExpectationBehavior,
)
from .blockchain import BlockchainConfig, PoWNetworkModel
from .errors import SimulationError
from .incentives import (
    IncentiveContext,
    IncentiveMechanism,
    IncentiveOutcome,
    RewardIncentiveMechanism,
)
from .models import (
    NetworkDeviceState,
    NetworkTransaction,
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
    ) -> None:
        self.network_model = network_model
        self.incentive_mechanism = (
            incentive_mechanism or RewardIncentiveMechanism()
        )
        self.device_behavior = device_behavior or ProfitExpectationBehavior()

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

    def process_action(self, action: SimulationEvent) -> NetworkOutcome:
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
        return self.network_model.device_state_records(elapsed_ms)

    def metric_record(self, elapsed_ms: int) -> dict[str, Any]:
        return self.network_model.metric_record(elapsed_ms)

    def summary_record(self) -> dict[str, Any]:
        return self.network_model.summary_record()

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
                transaction.reward = incentive_outcome.reward
                state.balance += incentive_outcome.reward
                state.cumulative_reward += incentive_outcome.reward

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
            "cumulative_cost": state.cumulative_cost,
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
