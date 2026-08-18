"""Single-network blockchain and incentive-mechanism simulation engine."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import BlockchainError
from .iot import IoTEnvironmentRuntime
from .metrics import average, balance_variance, gini_coefficient
from .models import (
    Block,
    DeviceAction,
    MiningJob,
    NetworkDeviceState,
    NetworkTransaction,
    TransactionStatus,
    TransactionType,
)
from .network import NetworkModel, NetworkOutcome

TransactionLogicFunction = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class BlockchainConfig:
    """Configuration and plugin callables for one network engine."""

    network_name: str
    network_slot: str
    pow_difficulty: int
    max_transactions_per_block: int
    target_block_time_ms: int | None
    transaction_fee_rate: float
    parameters: dict[str, Any] = field(default_factory=dict)
    transaction_logic: TransactionLogicFunction | None = None

    def validate(self) -> None:
        """Validate values that affect mining and transaction processing."""
        if self.network_slot not in {"A", "B"}:
            raise BlockchainError("Network slots must be A or B.")
        if self.pow_difficulty < 0:
            raise BlockchainError("PoW difficulty cannot be negative.")
        if self.max_transactions_per_block <= 0:
            raise BlockchainError(
                "Blocks must allow at least one transaction."
            )
        if (
            self.target_block_time_ms is not None
            and self.target_block_time_ms <= 0
        ):
            raise BlockchainError(
                "Target block time must be positive when configured."
            )
        if self.transaction_fee_rate < 0:
            raise BlockchainError("Transaction fee rate cannot be negative.")

    def plugin_context(self) -> dict[str, Any]:
        """Return public configuration values for uploaded plugin code."""
        return {
            "network_name": self.network_name,
            "network_slot": self.network_slot,
            "pow_difficulty": self.pow_difficulty,
            "max_transactions_per_block": (
                self.max_transactions_per_block
            ),
            "target_block_time_ms": self.target_block_time_ms,
            "transaction_fee_rate": self.transaction_fee_rate,
            "parameters": dict(self.parameters),
        }


class PoWNetworkModel(NetworkModel):
    """Built-in proof-of-work implementation of the network-model contract."""

    def __init__(
        self,
        *,
        simulation_network_id: int,
        environment: IoTEnvironmentRuntime,
        config: BlockchainConfig,
        random_seed: int,
    ) -> None:
        config.validate()
        self.simulation_network_id = simulation_network_id
        self.environment = environment
        self.config = config
        self.random_source = random.Random(random_seed)
        self.device_states = {
            device.database_id: NetworkDeviceState(
                profile=device,
                balance=device.initial_balance,
            )
            for device in environment.devices
        }
        self.pending_transactions: list[NetworkTransaction] = []
        self.transactions: list[NetworkTransaction] = []
        self.transactions_by_hash: dict[str, NetworkTransaction] = {}
        self.blocks: list[Block] = []
        self._terminal_outcomes: list[NetworkOutcome] = []
        self.mining_job: MiningJob | None = None
        self.current_time_ms = 0
        self.last_block_mined_at_ms = 0
        self._transaction_counter = 0

    def advance_to(self, elapsed_ms: int) -> None:
        """Advance virtual time and finalize any completed mining jobs."""
        if elapsed_ms < self.current_time_ms:
            raise BlockchainError(
                "A blockchain virtual clock cannot move backwards."
            )

        while (
            self.mining_job is not None
            and self.mining_job.completes_at_ms <= elapsed_ms
        ):
            self.current_time_ms = self.mining_job.completes_at_ms
            self._complete_mining_job()
            self._maybe_start_mining(self.current_time_ms)

        self.current_time_ms = elapsed_ms
        self._maybe_start_mining(elapsed_ms)

    def process_action(self, action: DeviceAction) -> NetworkOutcome:
        """Process one action and return its immediate network outcome."""
        transaction = self.process_event(action)
        return self._transaction_network_outcome(transaction)

    def process_event(self, event: DeviceAction) -> NetworkTransaction:
        """Compatibility API returning the concrete PoW transaction."""
        self.advance_to(event.scheduled_at_ms)
        sender = self._get_state(event.sender_device_id)
        target = self._get_state(event.target_device_id)

        if sender is None:
            return self._reject_event(
                event,
                "The event has no valid sender device.",
            )
        if not sender.active:
            return self._reject_event(
                event,
                "The sender device has left this network.",
            )

        decision = self._run_transaction_logic(event)
        if decision.get("reject_reason"):
            return self._reject_event(
                event,
                str(decision["reject_reason"]),
                charge_data_cost=event.event_type == TransactionType.IOT_DATA,
            )

        amount = self._non_negative_number(
            decision.get("amount", event.amount),
            "transaction amount",
        )
        fee = self._non_negative_number(
            decision.get(
                "fee",
                amount * self.config.transaction_fee_rate,
            ),
            "transaction fee",
        )
        reward_override = decision.get("reward")

        payload = dict(event.payload)
        payload_updates = decision.get("payload")
        if payload_updates is not None:
            if not isinstance(payload_updates, Mapping):
                raise BlockchainError(
                    "Custom transaction payload overrides must be mappings."
                )
            payload.update(payload_updates)

        rejection_reason = self._validate_event(
            event,
            sender,
            target,
            amount,
            fee,
            payload,
        )
        if rejection_reason:
            return self._reject_event(
                event,
                rejection_reason,
                charge_data_cost=event.event_type == TransactionType.IOT_DATA,
            )

        if event.event_type in {
            TransactionType.TRANSFER,
            TransactionType.FEEDBACK,
        }:
            sender.reserved_balance += amount + fee

        transaction = NetworkTransaction(
            transaction_hash=self._transaction_hash(
                event,
                amount,
                fee,
                payload,
            ),
            transaction_type=event.event_type,
            status=TransactionStatus.PENDING,
            event_id=event.database_id,
            sender_device_id=event.sender_device_id,
            target_device_id=event.target_device_id,
            submitted_at_ms=event.scheduled_at_ms,
            amount=amount,
            fee=fee,
            payload=payload,
            reward_override=reward_override,
        )
        self.transactions.append(transaction)
        self.transactions_by_hash[transaction.transaction_hash] = transaction
        self.pending_transactions.append(transaction)
        self._maybe_start_mining(event.scheduled_at_ms)
        return transaction

    def flush(self, start_at_ms: int) -> int:
        """Compatibility API for finalizing pending PoW transactions."""
        self.advance_to(max(start_at_ms, self.current_time_ms))

        while self.mining_job is not None or self.pending_transactions:
            if self.mining_job is None:
                self._maybe_start_mining(
                    self.current_time_ms,
                    force=True,
                )
            if self.mining_job is None:
                break
            self.advance_to(self.mining_job.completes_at_ms)

        return self.current_time_ms

    def finalize(self, elapsed_ms: int) -> int:
        """Finish pending PoW work and return the final virtual time."""
        return self.flush(elapsed_ms)

    def drain_outcomes(self) -> list[NetworkOutcome]:
        """Return and clear terminal network outcomes."""
        outcomes = self._terminal_outcomes
        self._terminal_outcomes = []
        return outcomes

    def get_transaction(
        self,
        transaction_hash: str,
    ) -> NetworkTransaction:
        """Return one concrete PoW transaction by its network reference."""
        try:
            return self.transactions_by_hash[transaction_hash]
        except KeyError as exc:
            raise BlockchainError(
                f"Unknown PoW transaction: {transaction_hash}."
            ) from exc

    def device_state_records(self, elapsed_ms: int) -> list[dict[str, Any]]:
        """Build database records for every device at one virtual timestamp."""
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
                "extra_state_json": {
                    "available_balance": state.available_balance,
                    "reserved_balance": state.reserved_balance,
                    "average_data_profit": state.average_data_profit,
                    "data_submissions": state.data_submissions,
                    "cumulative_penalties": state.cumulative_penalties,
                    "reputation_score": state.reputation_score,
                    "contribution_score": state.contribution_score,
                    "useful_contribution_count": (
                        state.useful_contribution_count
                    ),
                    "last_participation_signal": (
                        state.last_participation_signal
                    ),
                },
            }
            for state in self.device_states.values()
        ]

    def metric_record(self, elapsed_ms: int) -> dict[str, Any]:
        """Build one cumulative network-metric sample."""
        states = list(self.device_states.values())
        balances = [state.balance for state in states]
        confirmed = [
            transaction
            for transaction in self.transactions
            if transaction.status == TransactionStatus.CONFIRMED
        ]
        rejected_count = sum(
            transaction.status == TransactionStatus.REJECTED
            for transaction in self.transactions
        )
        pending_count = sum(
            transaction.status == TransactionStatus.PENDING
            for transaction in self.transactions
        )
        latencies = [
            transaction.confirmed_at_ms - transaction.submitted_at_ms
            for transaction in confirmed
            if transaction.confirmed_at_ms is not None
        ]
        active_count = sum(state.active for state in states)
        churned_count = len(states) - active_count
        elapsed_seconds = elapsed_ms / 1_000

        return {
            "simulation_network_id": self.simulation_network_id,
            "elapsed_ms": elapsed_ms,
            "active_device_count": active_count,
            "churned_device_count": churned_count,
            "churn_rate": churned_count / len(states),
            "gini_coefficient": gini_coefficient(balances),
            "balance_variance": balance_variance(balances),
            "generated_transaction_count": len(self.transactions),
            "accepted_transaction_count": len(confirmed) + pending_count,
            "confirmed_transaction_count": len(confirmed),
            "rejected_transaction_count": rejected_count,
            "pending_transaction_count": pending_count,
            "block_count": len(self.blocks),
            "throughput_tps": (
                len(confirmed) / elapsed_seconds
                if elapsed_seconds > 0
                else 0
            ),
            "average_confirmation_ms": average(latencies),
            "total_rewards": sum(
                state.cumulative_reward for state in states
            ),
            "total_fees": sum(
                transaction.fee for transaction in confirmed
            ),
            "custom_metrics_json": {
                "pending_mining": self.mining_job is not None,
                "virtual_time_ms": self.current_time_ms,
            },
        }

    def summary_record(self) -> dict[str, Any]:
        """Build the final precomputed network summary."""
        metrics = self.metric_record(self.current_time_ms)
        return {
            "simulation_network_id": self.simulation_network_id,
            "final_active_devices": metrics["active_device_count"],
            "final_churn_rate": metrics["churn_rate"],
            "final_gini_coefficient": metrics["gini_coefficient"],
            "final_balance_variance": metrics["balance_variance"],
            "total_transactions": metrics["generated_transaction_count"],
            "confirmed_transactions": metrics[
                "confirmed_transaction_count"
            ],
            "rejected_transactions": metrics[
                "rejected_transaction_count"
            ],
            "total_blocks": metrics["block_count"],
            "average_throughput_tps": metrics["throughput_tps"],
            "average_confirmation_ms": metrics[
                "average_confirmation_ms"
            ],
            "total_rewards": metrics["total_rewards"],
            "total_costs": sum(
                state.cumulative_cost
                for state in self.device_states.values()
            ),
            "custom_summary_json": {
                "final_virtual_time_ms": self.current_time_ms,
                "network_slot": self.config.network_slot,
                "total_fees": metrics["total_fees"],
            },
        }

    def block_records(self) -> list[dict[str, Any]]:
        """Convert mined blocks into database records."""
        return [
            {
                "simulation_network_id": self.simulation_network_id,
                "height": block.height,
                "block_hash": block.block_hash,
                "previous_hash": block.previous_hash,
                "mined_at_ms": block.mined_at_ms,
                "nonce": block.nonce,
                "difficulty": block.difficulty,
                "miner_address": block.miner_address,
                "transaction_count": len(block.transaction_hashes),
                "total_fees": block.total_fees,
                "total_block_reward": block.block_reward,
                "mining_duration_ms": block.mining_duration_ms,
                "metadata_json": {
                    "pow_mode": "virtual",
                    "transaction_hashes": block.transaction_hashes,
                },
            }
            for block in self.blocks
        ]

    def transaction_records(
        self,
        block_ids_by_height: Mapping[int, int],
    ) -> list[dict[str, Any]]:
        """Convert transaction outcomes into database records."""
        records = []
        for transaction in self.transactions:
            sender = self._get_state(transaction.sender_device_id)
            target = self._get_state(transaction.target_device_id)
            records.append(
                {
                    "simulation_network_id": self.simulation_network_id,
                    "event_id": transaction.event_id,
                    "block_id": block_ids_by_height.get(
                        transaction.block_height
                    ),
                    "transaction_hash": transaction.transaction_hash,
                    "transaction_type": transaction.transaction_type.value,
                    "status": transaction.status.value,
                    "sender_device_id": transaction.sender_device_id,
                    "target_device_id": transaction.target_device_id,
                    "sender_address": (
                        sender.profile.device_key if sender else None
                    ),
                    "target_address": (
                        target.profile.device_key if target else None
                    ),
                    "submitted_at_ms": transaction.submitted_at_ms,
                    "confirmed_at_ms": transaction.confirmed_at_ms,
                    "amount": transaction.amount,
                    "fee": transaction.fee,
                    "reward": transaction.reward,
                    "payload_json": transaction.payload,
                    "rejection_reason": transaction.rejection_reason,
                }
            )
        return records

    def _run_transaction_logic(
        self,
        event: DeviceAction,
    ) -> dict[str, Any]:
        if self.config.transaction_logic is None:
            return {}

        state_snapshot = {
            state.profile.device_key: {
                "balance": state.balance,
                "available_balance": state.available_balance,
                "active": state.active,
                "feedback_score": state.feedback_score,
                "cumulative_profit": state.cumulative_profit,
            }
            for state in self.device_states.values()
        }
        result = self.config.transaction_logic(
            {
                "event": event.to_plugin_context(),
                "network": self.config.plugin_context(),
                "device_states": state_snapshot,
                "block_height": len(self.blocks),
                "elapsed_ms": event.scheduled_at_ms,
            }
        )
        if result is None:
            return {}
        if not isinstance(result, Mapping):
            raise BlockchainError(
                "Custom blockchain logic must return a mapping or None."
            )
        return dict(result)

    def _validate_event(
        self,
        event: DeviceAction,
        sender: NetworkDeviceState,
        target: NetworkDeviceState | None,
        amount: float,
        fee: float,
        payload: Mapping[str, Any],
    ) -> str | None:
        if event.event_type == TransactionType.TRANSFER:
            if target is None or target is sender:
                return "Transfers require a different target device."
            if amount <= 0:
                return "Transfer amounts must be greater than zero."
            if sender.available_balance < amount + fee:
                return "The sender has insufficient available balance."

        elif event.event_type == TransactionType.IOT_DATA:
            if "value" not in payload:
                return "IoT data transactions require a sensor value."

        elif event.event_type == TransactionType.FEEDBACK:
            if target is None or target is sender:
                return "Feedback requires a different target device."
            feedback_value = payload.get("feedback")
            if feedback_value not in {-1, 0, 1}:
                return "Feedback values must be -1, 0, or 1."
            if sender.available_balance < amount + fee:
                return "The sender has insufficient available balance."

        else:
            return f"Unsupported transaction type: {event.event_type}."

        return None

    def _reject_event(
        self,
        event: DeviceAction,
        reason: str,
        *,
        charge_data_cost: bool = False,
    ) -> NetworkTransaction:
        transaction = NetworkTransaction(
            transaction_hash=self._transaction_hash(
                event,
                event.amount,
                0,
                event.payload,
            ),
            transaction_type=event.event_type,
            status=TransactionStatus.REJECTED,
            event_id=event.database_id,
            sender_device_id=event.sender_device_id,
            target_device_id=event.target_device_id,
            submitted_at_ms=event.scheduled_at_ms,
            amount=event.amount,
            payload=dict(event.payload),
            rejection_reason=reason,
        )
        self.transactions.append(transaction)
        self.transactions_by_hash[transaction.transaction_hash] = transaction
        self._terminal_outcomes.append(
            self._transaction_network_outcome(
                transaction,
                data_cost_charged=charge_data_cost,
            )
        )
        return transaction

    def _maybe_start_mining(
        self,
        current_time_ms: int,
        *,
        force: bool = False,
    ) -> None:
        if self.mining_job is not None or not self.pending_transactions:
            return

        block_is_full = (
            len(self.pending_transactions)
            >= self.config.max_transactions_per_block
        )
        target_time_reached = (
            self.config.target_block_time_ms is not None
            and current_time_ms - self.last_block_mined_at_ms
            >= self.config.target_block_time_ms
        )
        if not force and not block_is_full and not target_time_reached:
            return

        transactions = self.pending_transactions[
            : self.config.max_transactions_per_block
        ]
        del self.pending_transactions[: len(transactions)]
        height = len(self.blocks)
        previous_hash = self.blocks[-1].block_hash if self.blocks else None
        duration = self._mining_duration_ms()
        started_at = current_time_ms
        completes_at = started_at + max(1, math.ceil(duration))
        header = {
            "network": self.config.network_name,
            "height": height,
            "previous_hash": previous_hash,
            "started_at_ms": started_at,
            "transaction_hashes": [
                transaction.transaction_hash for transaction in transactions
            ],
        }
        header_json = json.dumps(header, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(header_json.encode("utf-8")).hexdigest()
        work_range = max(1, 2 ** min(self.config.pow_difficulty, 30))
        nonce = int(digest[:16], 16) % work_range
        candidate_hash = hashlib.sha256(
            f"{header_json}:{nonce}:{self.config.pow_difficulty}".encode(
                "utf-8"
            )
        ).hexdigest()

        self.mining_job = MiningJob(
            height=height,
            started_at_ms=started_at,
            completes_at_ms=completes_at,
            nonce=nonce,
            candidate_hash=candidate_hash,
            previous_hash=previous_hash,
            transactions=transactions,
            mining_duration_ms=duration,
        )

    def _complete_mining_job(self) -> None:
        job = self.mining_job
        if job is None:
            return

        total_fees = 0.0
        for transaction in job.transactions:
            self._apply_transaction(transaction, job)
            total_fees += transaction.fee

        block_reward = self._non_negative_number(
            self.config.parameters.get("miner_reward", 0),
            "miner reward",
        )
        self.blocks.append(
            Block(
                height=job.height,
                block_hash=job.candidate_hash,
                previous_hash=job.previous_hash,
                mined_at_ms=job.completes_at_ms,
                nonce=job.nonce,
                difficulty=self.config.pow_difficulty,
                transaction_hashes=[
                    transaction.transaction_hash
                    for transaction in job.transactions
                ],
                total_fees=total_fees,
                block_reward=block_reward,
                mining_duration_ms=job.mining_duration_ms,
            )
        )
        self.last_block_mined_at_ms = job.completes_at_ms
        self.mining_job = None

    def _apply_transaction(
        self,
        transaction: NetworkTransaction,
        job: MiningJob,
    ) -> None:
        sender = self._get_state(transaction.sender_device_id)
        target = self._get_state(transaction.target_device_id)
        if sender is None:
            raise BlockchainError(
                "A pending transaction lost its sender device state."
            )

        if transaction.transaction_type == TransactionType.TRANSFER:
            if target is None:
                raise BlockchainError(
                    "A pending transfer lost its target device state."
                )
            sender.reserved_balance -= transaction.amount + transaction.fee
            sender.balance -= transaction.amount + transaction.fee
            sender.cumulative_cost += transaction.fee
            target.balance += transaction.amount

        elif transaction.transaction_type == TransactionType.FEEDBACK:
            if target is None:
                raise BlockchainError(
                    "A pending feedback transaction lost its target."
                )
            sender.reserved_balance -= transaction.amount + transaction.fee
            sender.balance -= transaction.amount + transaction.fee
            sender.cumulative_cost += transaction.fee
            target.balance += transaction.amount
            target.feedback_score += float(
                transaction.payload.get("feedback", 0)
            )

        transaction.status = TransactionStatus.CONFIRMED
        transaction.confirmed_at_ms = job.completes_at_ms
        transaction.block_height = job.height
        self._terminal_outcomes.append(
            self._confirmed_network_outcome(transaction, job)
        )

    @staticmethod
    def _confirmed_network_outcome(
        transaction: NetworkTransaction,
        job: MiningJob,
    ) -> NetworkOutcome:
        return NetworkOutcome(
            action_id=transaction.event_id,
            status=TransactionStatus.CONFIRMED.value,
            accepted=True,
            submitted_at_ms=transaction.submitted_at_ms,
            finalized_at_ms=job.completes_at_ms,
            fee=transaction.fee,
            evidence={
                "confirmation_reference": job.candidate_hash,
            },
            metadata={
                "block_height": job.height,
                "transaction_hash": transaction.transaction_hash,
                "transaction_type": transaction.transaction_type.value,
                "data_cost_charged": (
                    transaction.transaction_type == TransactionType.IOT_DATA
                ),
            },
        )

    @staticmethod
    def _transaction_network_outcome(
        transaction: NetworkTransaction,
        *,
        data_cost_charged: bool = False,
    ) -> NetworkOutcome:
        return NetworkOutcome(
            action_id=transaction.event_id,
            status=transaction.status.value,
            accepted=transaction.status not in {
                TransactionStatus.REJECTED,
                TransactionStatus.FAILED,
            },
            submitted_at_ms=transaction.submitted_at_ms,
            finalized_at_ms=transaction.confirmed_at_ms,
            fee=transaction.fee,
            rejection_reason=transaction.rejection_reason,
            metadata={
                "transaction_hash": transaction.transaction_hash,
                "transaction_type": transaction.transaction_type.value,
                "block_height": transaction.block_height,
                "data_cost_charged": data_cost_charged,
            },
        )

    def _mining_duration_ms(self) -> float:
        base_duration = self._non_negative_number(
            self.config.parameters.get("base_mining_time_ms", 50),
            "base mining time",
        )
        difficulty_factor = 2 ** min(self.config.pow_difficulty, 30)
        jitter = self.random_source.uniform(0.9, 1.1)
        return max(1.0, base_duration * difficulty_factor * jitter)

    def _transaction_hash(
        self,
        event: DeviceAction,
        amount: float,
        fee: float,
        payload: Mapping[str, Any],
    ) -> str:
        self._transaction_counter += 1
        material = {
            "network_slot": self.config.network_slot,
            "counter": self._transaction_counter,
            "event_sequence": event.sequence_number,
            "event_id": event.database_id,
            "amount": amount,
            "fee": fee,
            "payload": payload,
        }
        encoded = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _get_state(
        self,
        device_id: int | None,
    ) -> NetworkDeviceState | None:
        if device_id is None:
            return None
        return self.device_states.get(device_id)

    @staticmethod
    def _non_negative_number(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise BlockchainError(f"{label.title()} must be numeric.") from exc
        if not math.isfinite(number) or number < 0:
            raise BlockchainError(
                f"{label.title()} must be finite and non-negative."
            )
        return number


BlockchainEngine = PoWNetworkModel
