"""IoT environment configuration, grouping, and sensor-data generation."""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from typing import Any

from src.database.configurations import ConfigurationRepository

from .errors import EnvironmentError
from .models import DeviceProfile


@dataclass(frozen=True, slots=True)
class DeviceGroupConfig:
    """Configuration shared by a group of similar IoT devices."""

    name: str
    device_count: int
    precision: float
    execution_cost: float
    data_rate: float
    profit_expectation: float
    initial_balance: float = 0
    behavior_parameters: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Validate group values before expanding or saving devices."""
        if not self.name.strip():
            raise EnvironmentError("Device group names cannot be empty.")
        if self.device_count <= 0:
            raise EnvironmentError("Device groups require at least one device.")
        if not 0 <= self.precision <= 1:
            raise EnvironmentError("Device precision must be between 0 and 1.")
        if self.execution_cost < 0:
            raise EnvironmentError("Execution cost cannot be negative.")
        if self.data_rate < 0:
            raise EnvironmentError("Data rate cannot be negative.")
        if self.profit_expectation < 0:
            raise EnvironmentError("Profit expectation cannot be negative.")


@dataclass(slots=True)
class IoTEnvironmentDefinition:
    """User-facing IoT environment composed of device groups."""

    name: str
    groups: list[DeviceGroupConfig]
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Validate environment names, groups, and total device count."""
        if not self.name.strip():
            raise EnvironmentError("Environment names cannot be empty.")
        if not self.groups:
            raise EnvironmentError(
                "An IoT environment requires at least one device group."
            )
        for group in self.groups:
            group.validate()
        total_devices = sum(group.device_count for group in self.groups)
        if total_devices > 100:
            raise EnvironmentError(
                "IoT environments cannot contain more than 100 devices."
            )

    def save(self, configurations: ConfigurationRepository) -> int:
        """Expand groups and persist the normalized environment records."""
        self.validate()
        environment_metadata = {
            **self.metadata,
            "device_groups": [
                {
                    "name": group.name,
                    "device_count": group.device_count,
                }
                for group in self.groups
            ],
        }
        environment_id = configurations.create_environment(
            name=self.name,
            description=self.description,
            metadata=environment_metadata,
        )
        behavior_ids: list[int] = []

        try:
            for group in self.groups:
                behavior_id = configurations.create_device_behavior(
                    name=f"{self.name}: {group.name}",
                    precision=group.precision,
                    execution_cost=group.execution_cost,
                    data_rate=group.data_rate,
                    profit_expectation=group.profit_expectation,
                    parameters=group.behavior_parameters,
                )
                behavior_ids.append(behavior_id)
                prefix = self._slug(group.name)
                for index in range(1, group.device_count + 1):
                    configurations.create_environment_device(
                        environment_id=environment_id,
                        behavior_id=behavior_id,
                        device_key=f"{prefix}-{index:03d}",
                        display_name=f"{group.name} {index}",
                        initial_balance=group.initial_balance,
                        metadata={"group_name": group.name},
                    )
        except Exception:
            try:
                configurations.database.delete_records(
                    "iot_environments",
                    {"id": environment_id},
                )
                for behavior_id in behavior_ids:
                    configurations.database.delete_records(
                        "device_behaviors",
                        {"id": behavior_id},
                    )
            except Exception:
                pass
            raise

        return environment_id

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug or "device"


class IoTEnvironmentRuntime:
    """Immutable device registry used by the event and network engines."""

    def __init__(self, devices: list[DeviceProfile]) -> None:
        if not devices:
            raise EnvironmentError(
                "A simulation requires at least one enabled IoT device."
            )
        if len(devices) > 100:
            raise EnvironmentError(
                "A simulation cannot contain more than 100 IoT devices."
            )
        self.devices = tuple(devices)
        self.devices_by_id = {
            device.database_id: device for device in self.devices
        }
        if len(self.devices_by_id) != len(self.devices):
            raise EnvironmentError("Runtime device identifiers must be unique.")

    @classmethod
    def from_simulation_records(
        cls,
        records: list[dict[str, Any]],
    ) -> IoTEnvironmentRuntime:
        """Build runtime profiles from immutable simulation-device rows."""
        devices = []
        for record in records:
            snapshot = record.get("behavior_snapshot_json") or {}
            behavior = snapshot.get("behavior") or {}
            source_metadata = snapshot.get("source_metadata") or {}
            devices.append(
                DeviceProfile(
                    database_id=record["id"],
                    device_key=record["device_key"],
                    display_name=record.get("display_name"),
                    initial_balance=float(record["initial_balance"]),
                    precision=float(record["precision"]),
                    execution_cost=float(record["execution_cost"]),
                    data_rate=float(record["data_rate"]),
                    profit_expectation=float(
                        record["profit_expectation"]
                    ),
                    parameters=dict(
                        behavior.get("parameters_json") or {}
                    ),
                    group_name=source_metadata.get("group_name"),
                )
            )
        return cls(devices)

    def get_device(self, device_id: int) -> DeviceProfile:
        """Return one runtime device or raise a domain-specific error."""
        try:
            return self.devices_by_id[device_id]
        except KeyError as exc:
            raise EnvironmentError(
                f"Unknown simulation device identifier: {device_id}."
            ) from exc

    def generate_sensor_data(
        self,
        device_id: int,
        scheduled_at_ms: int,
        random_source: random.Random,
    ) -> dict[str, Any]:
        """Generate deterministic noisy sensor data for a device."""
        device = self.get_device(device_id)
        parameters = device.parameters
        base_value = float(parameters.get("base_value", 20.0))
        amplitude = float(parameters.get("amplitude", 5.0))
        period_ms = max(float(parameters.get("period_ms", 60_000)), 1)
        noise_scale = float(parameters.get("noise_scale", 1.0))
        phase = (scheduled_at_ms % period_ms) / period_ms
        true_value = base_value + amplitude * math.sin(2 * math.pi * phase)
        noise_std = max(0, 1 - device.precision) * noise_scale
        observed_value = true_value + random_source.gauss(0, noise_std)

        return {
            "value": round(observed_value, 6),
            "true_value": round(true_value, 6),
            "unit": parameters.get("unit", "unit"),
            "precision": device.precision,
            "execution_cost": device.execution_cost,
            "generated_at_ms": scheduled_at_ms,
        }
