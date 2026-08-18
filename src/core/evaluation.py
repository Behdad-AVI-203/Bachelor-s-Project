"""Incentive-focused evaluation metrics for one simulation arm."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .incentives import IncentiveOutcome
from .metrics import gini_coefficient
from .models import NetworkDeviceState, TransactionType


@dataclass(slots=True)
class IncentiveEvaluationAccumulator:
    """Collect arm-level observations without owning network execution."""

    opportunities_seen: int = 0
    iot_data_opportunities: int = 0
    actions_created: int = 0
    iot_data_actions: int = 0
    incentive_evaluations: int = 0
    total_reward: float = 0.0
    total_penalty: float = 0.0
    useful_contribution_count: int = 0
    active_ratio_total: float = 0.0
    active_ratio_samples: int = 0

    def record_opportunity(self, event_type: TransactionType) -> None:
        """Record one shared opportunity seen by this arm."""
        self.opportunities_seen += 1
        if event_type == TransactionType.IOT_DATA:
            self.iot_data_opportunities += 1

    def record_action(self, event_type: TransactionType) -> None:
        """Record an action selected by device behavior."""
        self.actions_created += 1
        if event_type == TransactionType.IOT_DATA:
            self.iot_data_actions += 1

    def record_active_ratio(
        self,
        active_device_count: int,
        total_device_count: int,
    ) -> None:
        """Record one time-series active-device observation."""
        if total_device_count <= 0:
            return
        self.active_ratio_total += (
            float(active_device_count) / total_device_count
        )
        self.active_ratio_samples += 1

    def record_incentive_outcome(
        self,
        outcome: IncentiveOutcome,
        *,
        useful: bool,
    ) -> None:
        """Record the effects returned by an incentive mechanism."""
        self.incentive_evaluations += 1
        self.total_reward += outcome.reward_delta
        self.total_penalty += outcome.penalty_delta
        if useful:
            self.useful_contribution_count += 1

    def summarize(
        self,
        states: Iterable[NetworkDeviceState],
    ) -> dict[str, Any]:
        """Return comparable incentive-effectiveness metrics."""
        device_states = list(states)
        device_count = len(device_states)
        active_count = sum(state.active for state in device_states)
        final_retention = (
            active_count / device_count if device_count else 0.0
        )
        average_active_ratio = (
            self.active_ratio_total / self.active_ratio_samples
            if self.active_ratio_samples
            else final_retention
        )
        average_active_count = average_active_ratio * device_count
        participation_rate = (
            self.actions_created / self.opportunities_seen
            if self.opportunities_seen
            else 0.0
        )
        useful_rate = (
            self.useful_contribution_count
            / self.iot_data_opportunities
            if self.iot_data_opportunities
            else 0.0
        )
        useful_per_active = (
            self.useful_contribution_count / average_active_count
            if average_active_count > 0
            else 0.0
        )
        total_rewards = sum(
            state.cumulative_reward for state in device_states
        )
        total_penalties = sum(
            state.cumulative_penalties for state in device_states
        )
        useful_count = self.useful_contribution_count
        net_incentive_cost = total_rewards - total_penalties
        cost_per_useful = (
            net_incentive_cost / useful_count
            if useful_count
            else None
        )
        reward_fairness = 1.0 - gini_coefficient(
            state.cumulative_reward for state in device_states
        )
        utility_fairness = 1.0 - gini_coefficient(
            state.cumulative_profit for state in device_states
        )

        return {
            "final_retention_rate": final_retention,
            "average_active_device_ratio": average_active_ratio,
            "opportunity_participation_rate": participation_rate,
            "churn_rate": 1.0 - final_retention,
            "useful_contribution_count": useful_count,
            "useful_contribution_rate": useful_rate,
            "useful_contribution_per_active_device": useful_per_active,
            "total_rewards": total_rewards,
            "total_penalties": total_penalties,
            "net_incentive_cost": net_incentive_cost,
            "incentive_cost_per_useful_contribution": cost_per_useful,
            "reward_distribution_fairness": reward_fairness,
            "utility_distribution_fairness": utility_fairness,
            "incentive_evaluation_count": self.incentive_evaluations,
        }
