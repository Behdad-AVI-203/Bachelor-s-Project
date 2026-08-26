"""Incentive-mechanism contracts and the current reward implementation."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import IncentiveError, PluginExecutionError

RewardFunction = Callable[[Mapping[str, Any]], Any]
IncentiveFunction = Callable[[Mapping[str, Any]], Any]


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


@dataclass(frozen=True, slots=True, init=False)
class IncentiveOutcome:
    """Economic and policy effects produced by an incentive mechanism."""

    reward: float
    penalty: float
    contribution_score: float | None = None
    reputation_delta: float = 0
    participation_signal: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        reward: float = 0,
        penalty: float = 0,
        contribution_score: float | None = None,
        reputation_delta: float = 0,
        participation_signal: float | None = None,
        details: dict[str, Any] | None = None,
        *,
        reward_delta: float | None = None,
        penalty_delta: float | None = None,
        contribution_delta: float | None = None,
    ) -> None:
        """Create an outcome while accepting both current and legacy names."""
        if reward_delta is not None:
            reward = reward_delta
        if penalty_delta is not None:
            penalty = penalty_delta
        if contribution_delta is not None:
            contribution_score = contribution_delta
        object.__setattr__(self, "reward", reward)
        object.__setattr__(self, "penalty", penalty)
        object.__setattr__(self, "contribution_score", contribution_score)
        object.__setattr__(self, "reputation_delta", reputation_delta)
        object.__setattr__(self, "participation_signal", participation_signal)
        object.__setattr__(self, "details", dict(details or {}))

    @property
    def reward_delta(self) -> float:
        """Return the reward change using the generic delta terminology."""
        return float(self.reward)

    @property
    def penalty_delta(self) -> float:
        """Return the penalty change using the generic delta terminology."""
        return float(self.penalty)

    @property
    def contribution_delta(self) -> float:
        """Return the contribution change for this outcome."""
        return float(self.contribution_score or 0)


class IncentiveMechanism(Protocol):
    """Minimal interface implemented by built-in and custom mechanisms."""

    def evaluate(self, context: IncentiveContext) -> IncentiveOutcome:
        """Evaluate one confirmed action and return incentive effects."""


@dataclass(frozen=True, slots=True)
class RewardIncentiveMechanism:
    """Preserve the existing reward function behind the new contract."""

    reward_function: RewardFunction | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    use_network_parameters: bool = True

    def evaluate(self, context: IncentiveContext) -> IncentiveOutcome:
        """Calculate the current IoT-data reward without network coupling."""
        if context.legacy_reward_override is not None:
            reward = context.legacy_reward_override
        elif self.reward_function is None:
            parameters = dict(self.parameters)
            if not parameters and self.use_network_parameters:
                network_parameters = context.network.get("parameters")
                if isinstance(network_parameters, Mapping):
                    parameters = dict(network_parameters)
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


@dataclass(frozen=True, slots=True)
class PluginIncentiveMechanism:
    """Adapt a generic incentive plugin result to ``IncentiveOutcome``."""

    incentive_function: IncentiveFunction
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def evaluate(self, context: IncentiveContext) -> IncentiveOutcome:
        plugin_context = context.to_plugin_context()
        plugin_context["incentive_parameters"] = dict(self.parameters)
        try:
            result = self.incentive_function(plugin_context)
        except PluginExecutionError:
            raise
        except Exception as exc:
            raise IncentiveError(
                f"Incentive mechanism failed: {exc}."
            ) from exc

        if isinstance(result, Mapping):
            details = result.get("details") or {}
            if not isinstance(details, Mapping):
                raise IncentiveError(
                    "Incentive plugin details must be a mapping."
                )
            return IncentiveOutcome(
                reward_delta=self._number(
                    result.get("reward_delta", result.get("reward", 0)),
                    "reward",
                ),
                penalty_delta=self._number(
                    result.get("penalty_delta", result.get("penalty", 0)),
                    "penalty",
                ),
                contribution_delta=self._optional_number(
                    result.get(
                        "contribution_delta",
                        result.get("contribution_score"),
                    ),
                    "contribution delta",
                    allow_negative=True,
                ),
                reputation_delta=self._number(
                    result.get("reputation_delta", 0),
                    "reputation delta",
                    allow_negative=True,
                ),
                participation_signal=self._optional_number(
                    result.get("participation_signal"),
                    "participation signal",
                    allow_negative=True,
                ),
                details=dict(details),
            )

        return IncentiveOutcome(
            reward=self._number(result, "reward"),
        )

    @staticmethod
    def _number(
        value: Any,
        label: str,
        *,
        allow_negative: bool = False,
    ) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise IncentiveError(f"{label.title()} must be numeric.") from exc
        if not math.isfinite(number) or (
            not allow_negative and number < 0
        ):
            raise IncentiveError(
                f"{label.title()} must be finite and "
                f"{'numeric' if allow_negative else 'non-negative'}."
            )
        return number

    @classmethod
    def _optional_number(
        cls,
        value: Any,
        label: str,
        *,
        allow_negative: bool = False,
    ) -> float | None:
        if value is None:
            return None
        return cls._number(
            value,
            label,
            allow_negative=allow_negative,
        )
