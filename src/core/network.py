"""Generic network-model contracts used by simulation orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .models import SimulationEvent


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
    """Small runtime interface required by the current simulation engine."""

    current_time_ms: int

    def process_action(self, action: SimulationEvent) -> NetworkOutcome:
        """Submit one device action to the network model."""

    def advance_to(self, elapsed_ms: int) -> None:
        """Advance network-specific processing to a virtual timestamp."""

    def finalize(self, elapsed_ms: int) -> int:
        """Finish pending network work and return final virtual time."""

    def drain_outcomes(self) -> list[NetworkOutcome]:
        """Return and clear terminal outcomes produced since the last drain."""
