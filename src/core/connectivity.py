"""Lightweight deterministic connectivity policies for static IoT networks."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .models import DeviceAction


@dataclass(frozen=True, slots=True)
class ConnectivityOutcome:
    """Result of evaluating one communication opportunity."""

    available: bool
    delivered: bool
    latency_ms: int = 0
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_context(self) -> dict[str, Any]:
        """Return a JSON-compatible network context."""
        return {
            "available": self.available,
            "delivered": self.delivered,
            "latency_ms": self.latency_ms,
            "failure_reason": self.failure_reason,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class ConnectivityPolicy(Protocol):
    """Minimal contract for deterministic communication availability."""

    def evaluate(self, action: DeviceAction) -> ConnectivityOutcome:
        """Evaluate delivery of one action."""


@dataclass(frozen=True, slots=True)
class ProbabilisticConnectivityPolicy:
    """Static probabilistic connectivity with deterministic hash-based draws.

    Connectivity is evaluated when a device action attempts communication.
    This is intentional for the current abstract model: a device that chooses
    no action has no network transmission to deliver, while equivalent
    attempts receive identical hash-derived conditions in both arms.
    """

    random_seed: int = 0
    link_availability_probability: float = 1.0
    packet_delivery_success_probability: float = 1.0
    communication_failure_probability: float = 0.0
    latency_distribution_ms: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        probabilities = (
            self.link_availability_probability,
            self.packet_delivery_success_probability,
            self.communication_failure_probability,
        )
        if any(
            not math.isfinite(value) or not 0 <= value <= 1
            for value in probabilities
        ):
            raise ValueError("Connectivity probabilities must be in [0, 1].")
        if any(
            not isinstance(value, int) or value < 0
            for value in self.latency_distribution_ms
        ):
            raise ValueError(
                "Connectivity latency values must be non-negative integers."
            )

    def evaluate(self, action: DeviceAction) -> ConnectivityOutcome:
        """Evaluate using draws derived solely from seed and action identity."""
        key = self._identity_key(action)
        link_draw = self._draw(key, "link")
        packet_draw = self._draw(key, "packet")
        failure_draw = self._draw(key, "failure")

        available = link_draw < self.link_availability_probability
        delivered = (
            available
            and packet_draw < self.packet_delivery_success_probability
            and failure_draw >= self.communication_failure_probability
        )
        latency_ms = 0
        if delivered and self.latency_distribution_ms:
            index = int(
                self._draw(key, "latency")
                * len(self.latency_distribution_ms)
            )
            latency_ms = self.latency_distribution_ms[
                min(index, len(self.latency_distribution_ms) - 1)
            ]

        reason = None
        if not available:
            reason = "Communication link unavailable."
        elif not delivered:
            reason = "Packet delivery failed."

        return ConnectivityOutcome(
            available=available,
            delivered=delivered,
            latency_ms=latency_ms,
            failure_reason=reason,
            metadata={
                "link_draw": link_draw,
                "packet_draw": packet_draw,
                "failure_draw": failure_draw,
            },
        )

    def _identity_key(self, action: DeviceAction) -> str:
        """Use opportunity identity, not mutable payload, for shared conditions."""
        identity = (
            self.random_seed,
            action.sequence_number,
            action.scheduled_at_ms,
            action.sender_device_id,
            action.target_device_id,
            action.event_type.value,
        )
        return repr(identity)

    @staticmethod
    def _draw(identity: str, stream: str) -> float:
        digest = hashlib.sha256(
            f"connectivity:{stream}:{identity}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big") / 2**64


def connectivity_context(
    outcome: ConnectivityOutcome,
) -> Mapping[str, Any]:
    """Expose a connectivity outcome as a generic network metadata mapping."""
    return outcome.to_context()
