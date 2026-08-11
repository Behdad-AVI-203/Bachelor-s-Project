"""Device behavior contracts and the default participation policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .incentives import IncentiveOutcome


@dataclass(frozen=True, slots=True)
class ParticipationContext:
    """Generic state used to decide whether a device remains active."""

    device: Mapping[str, Any]
    device_state: Mapping[str, Any]
    action: Mapping[str, Any]
    network_outcome: Mapping[str, Any]
    incentive_outcome: IncentiveOutcome | None = None
    random_value: float | None = None


@dataclass(frozen=True, slots=True)
class ParticipationDecision:
    """Result of evaluating one device's voluntary participation."""

    active: bool
    utility: float | None = None
    reason: str | None = None


class DeviceBehavior(Protocol):
    """Minimal interface for device participation policies."""

    def decide_participation(
        self,
        context: ParticipationContext,
    ) -> ParticipationDecision:
        """Return the device's participation decision after an outcome."""


@dataclass(frozen=True, slots=True)
class ProfitExpectationBehavior:
    """Preserve the existing average-profit participation policy."""

    def decide_participation(
        self,
        context: ParticipationContext,
    ) -> ParticipationDecision:
        state = context.device_state
        if not bool(state.get("active", True)):
            return ParticipationDecision(
                active=False,
                utility=self._average_profit(state),
                reason=state.get("churn_reason"),
            )

        average_profit = self._average_profit(state)
        if average_profit is None:
            return ParticipationDecision(active=True)

        incentive_signal = 0.0
        if (
            context.incentive_outcome is not None
            and context.incentive_outcome.participation_signal is not None
        ):
            incentive_signal = float(
                context.incentive_outcome.participation_signal
            )
        utility = average_profit + incentive_signal
        profit_expectation = float(
            context.device.get("profit_expectation", 0)
        )
        if utility < profit_expectation:
            return ParticipationDecision(
                active=False,
                utility=utility,
                reason=(
                    "Average data profit fell below the configured "
                    "expectation."
                ),
            )
        return ParticipationDecision(active=True, utility=utility)

    @staticmethod
    def _average_profit(state: Mapping[str, Any]) -> float | None:
        data_submissions = int(state.get("data_submissions", 0))
        if data_submissions == 0:
            return None
        cumulative_reward = float(state.get("cumulative_reward", 0))
        cumulative_cost = float(state.get("cumulative_cost", 0))
        return (cumulative_reward - cumulative_cost) / data_submissions
