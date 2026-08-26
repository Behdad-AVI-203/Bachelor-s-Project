"""Generic network-model contracts used by simulation orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from .models import DeviceAction

NetworkModelFactory = Callable[..., "NetworkModel"]


@dataclass(frozen=True, slots=True)
class NetworkOutcome:
    """Generic result of a network model processing one device action."""

    action_id: int | None
    status: str
    accepted: bool
    submitted_at_ms: int
    finalized_at_ms: int | None = None
    fee: float = 0
    rejection_reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_context(self) -> dict[str, Any]:
        """Return a JSON-compatible representation for other components."""
        confirmation_latency_ms = None
        if self.finalized_at_ms is not None:
            confirmation_latency_ms = (
                self.finalized_at_ms - self.submitted_at_ms
            )
        return {
            "action_id": self.action_id,
            "status": self.status,
            "accepted": self.accepted,
            "submitted_at_ms": self.submitted_at_ms,
            "confirmed_at_ms": self.finalized_at_ms,
            "confirmation_latency_ms": confirmation_latency_ms,
            "fee": self.fee,
            "rejection_reason": self.rejection_reason,
            "evidence": dict(self.evidence),
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class NetworkModel(Protocol):
    """Generic network contract required by simulation orchestration."""

    current_time_ms: int
    simulation_network_id: int
    device_states: Mapping[int, Any]

    def process_action(self, action: DeviceAction) -> NetworkOutcome:
        """Submit one device action to the network model."""

    def advance_to(self, elapsed_ms: int) -> None:
        """Advance network-specific processing to a virtual timestamp."""

    def finalize(self, elapsed_ms: int) -> int:
        """Finish pending network work and return final virtual time."""

    def drain_outcomes(self) -> list[NetworkOutcome]:
        """Return and clear terminal outcomes produced since the last drain."""

    def network_context(self) -> Mapping[str, Any]:
        """Return network facts/configuration for incentive evaluation."""

    def action_context_for_outcome(
        self,
        outcome: NetworkOutcome,
    ) -> Mapping[str, Any]:
        """Return generic action facts associated with a network outcome."""

    def record_incentive_effect(
        self,
        outcome: NetworkOutcome,
        reward: float,
    ) -> None:
        """Record an incentive effect for later persistence/export."""

    def device_state_records(
        self,
        elapsed_ms: int,
    ) -> list[dict[str, Any]]:
        """Return device runtime snapshots in persistence-ready form."""

    def metric_record(self, elapsed_ms: int) -> dict[str, Any]:
        """Return network metric data for one virtual timestamp."""

    def summary_record(self) -> dict[str, Any]:
        """Return the final network summary."""

    def persistence_records(
        self,
        reference_ids: Mapping[int, int] | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Return opaque persistence records for blocks and transactions."""
