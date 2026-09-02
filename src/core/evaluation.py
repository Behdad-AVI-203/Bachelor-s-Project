"""Incentive-focused evaluation metrics for one simulation arm."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from math import sqrt
from statistics import fmean, stdev
from typing import Any

from .incentives import IncentiveOutcome
from .metrics import gini_coefficient
from .models import NetworkDeviceState, TransactionType


class MetricCategory(StrEnum):
    INCENTIVE_EFFECTIVENESS = "incentive_effectiveness"
    DEVICE_BEHAVIOR = "device_behavior"
    NETWORK_CONTEXT = "network_context"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """Authoritative semantic and scoring metadata for one metric."""

    name: str
    owner: str
    category: MetricCategory
    direction: str
    aggregation: str
    denominator: str
    participates_in_scoring: bool
    contextual_only: bool
    version: str = "1.0"


PRIMARY_METRIC_DEFINITIONS = {
    "opportunity_participation_rate": MetricDefinition(
        "opportunity_participation_rate", "evaluation",
        MetricCategory.INCENTIVE_EFFECTIVENESS, "higher", "ratio",
        "all external opportunities; rejected actions count as participation",
        True, False,
    ),
    "final_retention_rate": MetricDefinition(
        "final_retention_rate", "evaluation",
        MetricCategory.INCENTIVE_EFFECTIVENESS, "higher", "final_ratio",
        "all enabled devices; active at final virtual time",
        True, False,
    ),
    "useful_contribution_rate": MetricDefinition(
        "useful_contribution_rate", "evaluation",
        MetricCategory.INCENTIVE_EFFECTIVENESS, "higher", "ratio",
        "IoT-data opportunities; operational usefulness rule",
        True, False,
    ),
    "useful_contribution_per_active_device": MetricDefinition(
        "useful_contribution_per_active_device", "evaluation",
        MetricCategory.INCENTIVE_EFFECTIVENESS, "higher", "ratio",
        "average active devices over sampled virtual time",
        False, False,
    ),
    "incentive_cost_per_useful_contribution": MetricDefinition(
        "incentive_cost_per_useful_contribution", "evaluation",
        MetricCategory.INCENTIVE_EFFECTIVENESS, "lower", "ratio",
        "useful contribution count",
        True, False,
    ),
    "reward_distribution_fairness": MetricDefinition(
        "reward_distribution_fairness", "evaluation",
        MetricCategory.INCENTIVE_EFFECTIVENESS, "higher", "gini_complement",
        "all enabled devices, including inactive and zero-reward devices",
        True, False,
    ),
    "utility_distribution_fairness": MetricDefinition(
        "utility_distribution_fairness", "evaluation",
        MetricCategory.INCENTIVE_EFFECTIVENESS, "higher", "gini_complement",
        "all enabled devices, including inactive devices",
        False, False,
    ),
    "average_net_utility_per_device": MetricDefinition(
        "average_net_utility_per_device", "evaluation",
        MetricCategory.INCENTIVE_EFFECTIVENESS, "higher", "mean",
        "all enabled devices",
        True, False,
    ),
    "non_negative_utility_rate": MetricDefinition(
        "non_negative_utility_rate", "evaluation",
        MetricCategory.INCENTIVE_EFFECTIVENESS, "higher", "ratio",
        "all enabled devices",
        False, False,
    ),
}

METRIC_DEFINITIONS = {
    **PRIMARY_METRIC_DEFINITIONS,
    "throughput": MetricDefinition(
        "throughput", "network", MetricCategory.NETWORK_CONTEXT,
        "higher", "mean", "virtual duration", False, True,
    ),
    "confirmation_latency": MetricDefinition(
        "confirmation_latency", "network", MetricCategory.NETWORK_CONTEXT,
        "lower", "mean", "confirmed actions", False, True,
    ),
}


def metric_definitions() -> dict[str, MetricDefinition]:
    """Return a copy of the authoritative metric registry."""
    return dict(METRIC_DEFINITIONS)


def metric_semantics() -> dict[str, str]:
    """Return concise operational definitions for research documentation."""
    return {
        "useful_contribution": (
            "An accepted IoT-data outcome with contribution_delta > 0, "
            "unless the selected mechanism explicitly supplies "
            "details.useful_contribution."
        ),
        "incentive_cost": (
            "Rewards minus penalties; penalties are transfers to the device "
            "ledger, not assumed system savings."
        ),
        "fairness": (
            "One minus the non-negative Gini coefficient over all enabled "
            "devices. Equal zero rewards are reported as undefined-success "
            "fairness (0.0) rather than success."
        ),
        "utility": (
            "Cumulative rewards minus penalties minus device execution cost."
        ),
    }


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
        if total_rewards == 0:
            reward_fairness = 0.0
        utility_fairness = 1.0 - gini_coefficient(
            state.cumulative_profit for state in device_states
        )
        utilities = [state.cumulative_profit for state in device_states]
        average_utility = (
            fmean(utilities) if utilities else 0.0
        )
        non_negative_utility_rate = (
            sum(utility >= 0 for utility in utilities) / device_count
            if device_count
            else 0.0
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
            "average_net_utility_per_device": average_utility,
            "non_negative_utility_rate": non_negative_utility_rate,
            "incentive_evaluation_count": self.incentive_evaluations,
        }
