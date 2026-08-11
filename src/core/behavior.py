"""Device behavior contracts and the default participation policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .incentives import IncentiveOutcome
from .models import DeviceAction, ExternalOpportunity


@dataclass(frozen=True, slots=True)
class ActionDecisionContext:
    """State available when deciding whether to act on an opportunity."""

    device: Mapping[str, Any]
    device_state: Mapping[str, Any]
    opportunity: ExternalOpportunity
    last_incentive_outcome: IncentiveOutcome | None = None
    random_value: float | None = None


@dataclass(frozen=True, slots=True)
class ActionDecision:
    """A produced action or an explicit non-participation decision."""

    action: DeviceAction | None
    reason: str | None = None


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

    def decide_action(
        self,
        context: ActionDecisionContext,
    ) -> ActionDecision:
        """Return an action or explicit no-action decision."""

    def decide_participation(
        self,
        context: ParticipationContext,
    ) -> ParticipationDecision:
        """Return the device's participation decision after an outcome."""


@dataclass(frozen=True, slots=True)
class ProfitExpectationBehavior:
    """Preserve the existing average-profit participation policy."""

    def decide_action(
        self,
        context: ActionDecisionContext,
    ) -> ActionDecision:
        """Act on every opportunity while the device remains active."""
        if not bool(context.device_state.get("active", True)):
            return ActionDecision(
                action=None,
                reason=(
                    context.device_state.get("churn_reason")
                    or "Device is not currently participating."
                ),
            )
        return ActionDecision(action=context.opportunity.to_action())

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
