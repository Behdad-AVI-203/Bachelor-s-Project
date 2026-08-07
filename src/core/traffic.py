"""Poisson event-stream generation for shared A/B simulation input."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .errors import SimulationError
from .iot import IoTEnvironmentRuntime
from .models import DeviceProfile, SimulationEvent, TransactionType


@dataclass(frozen=True, slots=True)
class PoissonTrafficConfig:
    """Parameters controlling the canonical transaction stream."""

    lambda_per_second: float
    duration_seconds: float
    traffic_mix: dict[str, float] = field(default_factory=dict)
    transfer_amount_min: float = 0.1
    transfer_amount_max: float = 5.0
    positive_feedback_probability: float = 0.7
    max_events: int = 1_000_000

    def validate(self) -> None:
        """Validate traffic and safety-limit parameters."""
        if self.lambda_per_second <= 0:
            raise SimulationError("Poisson lambda must be greater than zero.")
        if self.duration_seconds <= 0:
            raise SimulationError("Simulation duration must be positive.")
        if self.transfer_amount_min <= 0:
            raise SimulationError(
                "Minimum transfer amount must be greater than zero."
            )
        if self.transfer_amount_max < self.transfer_amount_min:
            raise SimulationError(
                "Maximum transfer amount cannot be below the minimum."
            )
        if not 0 <= self.positive_feedback_probability <= 1:
            raise SimulationError(
                "Positive-feedback probability must be between 0 and 1."
            )
        if self.max_events <= 0:
            raise SimulationError("Maximum event count must be positive.")


class PoissonEventGenerator:
    """Generate one reproducible event stream for both network engines."""

    DEFAULT_MIX = {
        TransactionType.TRANSFER.value: 0.2,
        TransactionType.IOT_DATA.value: 0.7,
        TransactionType.FEEDBACK.value: 0.1,
    }

    def __init__(
        self,
        environment: IoTEnvironmentRuntime,
        config: PoissonTrafficConfig,
        *,
        random_seed: int,
    ) -> None:
        config.validate()
        self.environment = environment
        self.config = config
        self.random_source = random.Random(random_seed)
        self.event_types, self.event_weights = self._prepare_traffic_mix(
            config.traffic_mix
        )

    def generate(self) -> list[SimulationEvent]:
        """Generate all events without sleeping or using wall-clock time."""
        events: list[SimulationEvent] = []
        current_time_seconds = 0.0

        while True:
            current_time_seconds += self.random_source.expovariate(
                self.config.lambda_per_second
            )
            if current_time_seconds > self.config.duration_seconds:
                break
            if len(events) >= self.config.max_events:
                raise SimulationError(
                    "Generated event count exceeded the configured safety "
                    "limit."
                )

            scheduled_at_ms = round(current_time_seconds * 1_000)
            event_type = self.random_source.choices(
                self.event_types,
                weights=self.event_weights,
                k=1,
            )[0]
            events.append(
                self._create_event(
                    len(events),
                    scheduled_at_ms,
                    event_type,
                )
            )

        return events

    def _create_event(
        self,
        sequence_number: int,
        scheduled_at_ms: int,
        event_type: TransactionType,
    ) -> SimulationEvent:
        devices = list(self.environment.devices)
        if len(devices) < 2 and event_type in {
            TransactionType.TRANSFER,
            TransactionType.FEEDBACK,
        }:
            event_type = TransactionType.IOT_DATA

        if event_type == TransactionType.IOT_DATA:
            sender = self._weighted_data_device()
            payload = self.environment.generate_sensor_data(
                sender.database_id,
                scheduled_at_ms,
                self.random_source,
            )
            return SimulationEvent(
                sequence_number=sequence_number,
                scheduled_at_ms=scheduled_at_ms,
                event_type=event_type,
                sender_device_id=sender.database_id,
                target_device_id=None,
                payload=payload,
            )

        sender, target = self.random_source.sample(devices, k=2)
        if event_type == TransactionType.TRANSFER:
            amount = round(
                self.random_source.uniform(
                    self.config.transfer_amount_min,
                    self.config.transfer_amount_max,
                ),
                8,
            )
            return SimulationEvent(
                sequence_number=sequence_number,
                scheduled_at_ms=scheduled_at_ms,
                event_type=event_type,
                sender_device_id=sender.database_id,
                target_device_id=target.database_id,
                amount=amount,
            )

        feedback = (
            1
            if self.random_source.random()
            < self.config.positive_feedback_probability
            else -1
        )
        return SimulationEvent(
            sequence_number=sequence_number,
            scheduled_at_ms=scheduled_at_ms,
            event_type=event_type,
            sender_device_id=sender.database_id,
            target_device_id=target.database_id,
            payload={"feedback": feedback},
        )

    def _weighted_data_device(self) -> DeviceProfile:
        devices = list(self.environment.devices)
        weights = [max(0, device.data_rate) for device in devices]
        if sum(weights) == 0:
            return self.random_source.choice(devices)
        return self.random_source.choices(devices, weights=weights, k=1)[0]

    def _prepare_traffic_mix(
        self,
        traffic_mix: dict[str, float],
    ) -> tuple[list[TransactionType], list[float]]:
        prepared = dict(self.DEFAULT_MIX)
        if traffic_mix:
            prepared.update(traffic_mix)

        event_types = []
        weights = []
        for transaction_type in TransactionType:
            weight = float(prepared.get(transaction_type.value, 0))
            if weight < 0:
                raise SimulationError(
                    "Traffic-mix weights cannot be negative."
                )
            if weight > 0:
                event_types.append(transaction_type)
                weights.append(weight)

        if not event_types:
            raise SimulationError(
                "Traffic mix must enable at least one transaction type."
            )
        return event_types, weights
