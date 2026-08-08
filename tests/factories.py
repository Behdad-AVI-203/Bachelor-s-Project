"""Reusable factories for database, core, and UI tests."""

from __future__ import annotations

from typing import Any

from src.core import DeviceGroupConfig, IoTEnvironmentDefinition
from src.database import DatabaseService


DEFAULT_REWARD_CODE = """def calculate_reward(context):
    device = context["device"]
    parameters = context["network"]["parameters"]
    return (
        parameters.get("base_iot_reward", 1.0) * device["precision"]
        + parameters.get("feedback_weight", 0.1)
        * context["device_state"]["feedback_score"]
    )
"""

CUSTOM_REWARD_CODE = """def calculate_reward(context):
    precision = context["device"]["precision"]
    feedback = context["device_state"]["feedback_score"]
    return max(0.0, 1.5 * precision + 0.25 * feedback)
"""


def create_environment(
    database: DatabaseService,
    *,
    name: str = "Test environment",
    device_count: int = 6,
    precision: float = 0.9,
    execution_cost: float = 0.1,
    data_rate: float = 1.0,
    profit_expectation: float = 0.0,
    initial_balance: float = 10.0,
) -> int:
    """Create one normalized environment through the core definition."""
    definition = IoTEnvironmentDefinition(
        name=name,
        description="Automated test environment",
        groups=[
            DeviceGroupConfig(
                name="Sensors",
                device_count=device_count,
                precision=precision,
                execution_cost=execution_cost,
                data_rate=data_rate,
                profit_expectation=profit_expectation,
                initial_balance=initial_balance,
                behavior_parameters={
                    "base_value": 20.0,
                    "unit": "C",
                },
            )
        ],
    )
    return definition.save(database.configurations)


def create_network(
    database: DatabaseService,
    *,
    name: str,
    difficulty: int = 1,
    reward_code: str | None = DEFAULT_REWARD_CODE,
    parameters: dict[str, Any] | None = None,
) -> int:
    """Create a network and optional reward-function artifact."""
    reward_artifact_id = None
    if reward_code is not None:
        reward_artifact_id = database.configurations.create_code_artifact(
            artifact_type="reward_function",
            name=f"{name} reward",
            entrypoint="calculate_reward",
            source_code=reward_code,
            validation_status="valid",
        )
    return database.configurations.create_network_config(
        name=name,
        description="Automated test network",
        pow_difficulty=difficulty,
        max_transactions_per_block=20,
        target_block_time_ms=500,
        transaction_fee_rate=0.01,
        reward_artifact_id=reward_artifact_id,
        parameters={
            "base_mining_time_ms": 5,
            "base_iot_reward": 1.0,
            "feedback_weight": 0.1,
            **(parameters or {}),
        },
    )


def create_experiment(
    database: DatabaseService,
    *,
    environment_id: int,
    network_a_id: int,
    network_b_id: int,
    name: str = "Test experiment",
    duration_seconds: float = 30.0,
    poisson_lambda: float = 3.0,
    sample_interval_ms: int = 1_000,
    random_seed: int = 42,
    traffic_mix: dict[str, float] | None = None,
) -> int:
    """Create a reusable A/B experiment configuration."""
    return database.configurations.create_experiment_config(
        name=name,
        description="Automated test experiment",
        environment_id=environment_id,
        network_a_config_id=network_a_id,
        network_b_config_id=network_b_id,
        poisson_lambda=poisson_lambda,
        duration_seconds=duration_seconds,
        sample_interval_ms=sample_interval_ms,
        default_random_seed=random_seed,
        traffic_mix=traffic_mix
        or {
            "transfer": 0.2,
            "iot_data": 0.7,
            "feedback": 0.1,
        },
        parameters={
            "transfer_amount_min": 0.1,
            "transfer_amount_max": 2.0,
            "positive_feedback_probability": 0.75,
        },
    )


def create_platform_configuration(
    database: DatabaseService,
    *,
    device_count: int = 6,
    duration_seconds: float = 30.0,
    poisson_lambda: float = 3.0,
    sample_interval_ms: int = 1_000,
    random_seed: int = 42,
    traffic_mix: dict[str, float] | None = None,
) -> dict[str, int]:
    """Create an environment, two networks, and one experiment."""
    environment_id = create_environment(
        database,
        device_count=device_count,
    )
    network_a_id = create_network(
        database,
        name="Network A",
        difficulty=1,
    )
    network_b_id = create_network(
        database,
        name="Network B",
        difficulty=2,
        reward_code=CUSTOM_REWARD_CODE,
    )
    experiment_id = create_experiment(
        database,
        environment_id=environment_id,
        network_a_id=network_a_id,
        network_b_id=network_b_id,
        duration_seconds=duration_seconds,
        poisson_lambda=poisson_lambda,
        sample_interval_ms=sample_interval_ms,
        random_seed=random_seed,
        traffic_mix=traffic_mix,
    )
    return {
        "environment_id": environment_id,
        "network_a_id": network_a_id,
        "network_b_id": network_b_id,
        "experiment_id": experiment_id,
    }
