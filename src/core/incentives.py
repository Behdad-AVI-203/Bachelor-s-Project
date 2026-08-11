"""Incentive-mechanism contracts and the current reward implementation."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import IncentiveError, PluginExecutionError

RewardFunction = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class IncentiveContext:
    """Generic information available when evaluating confirmed activity."""

    network: Mapping[str, Any]
    device: Mapping[str, Any]
    device_state: Mapping[str, Any]
    action: Mapping[str, Any]
    network_outcome: Mapping[str, Any]
    elapsed_ms: int
    legacy_reward_override: Any | None = None

    def to_plugin_context(self) -> dict[str, Any]:
        """Return the current plugin contract plus generic boundary fields."""
        network_outcome = dict(self.network_outcome)
        metadata = network_outcome.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        action = dict(self.action)
        return {
            "network": dict(self.network),
            "device": dict(self.device),
            "device_state": dict(self.device_state),
            "action": action,
            "network_outcome": network_outcome,
            "elapsed_ms": self.elapsed_ms,
            "transaction": {
                "payload": dict(action.get("payload") or {}),
                "submitted_at_ms": action.get("submitted_at_ms"),
            },
            "block_height": metadata.get("block_height"),
        }


@dataclass(frozen=True, slots=True)
class IncentiveOutcome:
    """Economic and policy effects produced by an incentive mechanism."""

    reward: float = 0
    penalty: float = 0
    contribution_score: float | None = None
    reputation_delta: float = 0
    participation_signal: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


class IncentiveMechanism(Protocol):
    """Minimal interface implemented by built-in and custom mechanisms."""

    def evaluate(self, context: IncentiveContext) -> IncentiveOutcome:
        """Evaluate one confirmed action and return incentive effects."""


@dataclass(frozen=True, slots=True)
class RewardIncentiveMechanism:
    """Preserve the existing reward function behind the new contract."""

    reward_function: RewardFunction | None = None

    def evaluate(self, context: IncentiveContext) -> IncentiveOutcome:
        """Calculate the current IoT-data reward without network coupling."""
        if context.legacy_reward_override is not None:
            reward = context.legacy_reward_override
        elif self.reward_function is None:
            parameters = context.network.get("parameters")
            if not isinstance(parameters, Mapping):
                parameters = {}
            base_reward = float(parameters.get("base_iot_reward", 1.0))
            feedback_weight = float(parameters.get("feedback_weight", 0.1))
            reward = (
                base_reward * float(context.device["precision"])
                + feedback_weight
                * float(context.device_state["feedback_score"])
            )
        else:
            try:
                reward = self.reward_function(context.to_plugin_context())
            except PluginExecutionError:
                raise
            except Exception as exc:
                raise IncentiveError(
                    f"Reward function failed: {exc}."
                ) from exc

        return IncentiveOutcome(
            reward=self._non_negative_number(reward, "calculated reward")
        )

    @staticmethod
    def _non_negative_number(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise IncentiveError(f"{label.title()} must be numeric.") from exc
        if not math.isfinite(number) or number < 0:
            raise IncentiveError(
                f"{label.title()} must be finite and non-negative."
            )
        return number
