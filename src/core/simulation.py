"""Dual-network simulation orchestration using a fast virtual clock."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from src.database import DatabaseService

from .blockchain import (
    INCENTIVE_PARAMETER_KEYS,
    BlockchainConfig,
    PoWNetworkModel,
)
from .connectivity import ProbabilisticConnectivityPolicy
from .errors import CoreError, SimulationError
from .incentives import (
    IncentiveMechanism,
    IncentiveMechanismRegistry,
    PluginIncentiveMechanism,
    RewardIncentiveMechanism,
    default_incentive_registry,
)
from .iot import IoTEnvironmentRuntime
from .metrics import (
    compare_metric,
    incentive_evaluation_configuration,
    score_comparison,
    weighted_incentive_effectiveness,
    weighted_score_comparison,
)
from .models import ExternalOpportunity
from .orchestration import SimulationArmRuntime
from .network import (
    NetworkModel,
    NetworkModelFactory,
    NetworkModelRegistry,
)
from .plugins import load_plugin_function
from .traffic import PoissonEventGenerator, PoissonTrafficConfig


@dataclass(frozen=True, slots=True)
class SimulationProgress:
    """UI-neutral progress update emitted during virtual execution."""

    simulation_id: int
    phase: str
    progress: float
    events_processed: int
    total_events: int
    virtual_time_ms: int


@dataclass(frozen=True, slots=True)
class SimulationExecutionResult:
    """Final identifiers and summaries returned to callers."""

    simulation_id: int
    event_count: int
    final_virtual_time_ms: int
    network_summaries: dict[str, dict[str, Any]]
    comparison: dict[str, Any]
    exogenous_fingerprint: str = ""


ProgressCallback = Callable[[SimulationProgress], None]


class SimulationEngine:
    """Generate shared opportunities, run A/B arms, and persist results."""

    def __init__(
        self,
        database: DatabaseService,
        *,
        sample_batch_size: int = 5_000,
        persistence_batch_size: int = 2_000,
        network_model_factory: NetworkModelFactory | None = None,
        network_model_registry: NetworkModelRegistry | None = None,
        incentive_mechanism_registry: IncentiveMechanismRegistry | None = None,
    ) -> None:
        if sample_batch_size <= 0 or persistence_batch_size <= 0:
            raise SimulationError("Persistence batch sizes must be positive.")
        self.database = database
        self.sample_batch_size = sample_batch_size
        self.persistence_batch_size = persistence_batch_size
        self.network_model_factory = network_model_factory
        self.network_model_registry = (
            network_model_registry or self._default_network_model_registry()
        )
        self.incentive_mechanism_registry = (
            incentive_mechanism_registry or default_incentive_registry()
        )

    def run_experiment(
        self,
        experiment_config_id: int,
        *,
        name: str | None = None,
        random_seed: int | None = None,
        progress_callback: ProgressCallback | None = None,
        configuration_snapshot: Mapping[str, Any] | None = None,
    ) -> SimulationExecutionResult:
        """Create and execute a fresh run from a saved experiment."""
        run = self.database.simulations.create_run_from_experiment(
            experiment_config_id,
            name=name,
            random_seed=random_seed,
            configuration_snapshot=configuration_snapshot,
        )
        return self.run_simulation(
            run["simulation_id"],
            progress_callback=progress_callback,
        )

    def run_simulation(
        self,
        simulation_id: int,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> SimulationExecutionResult:
        """Execute an existing prepared simulation without real-time delays."""
        run = self.database.get_record(
            "simulation_runs",
            {"id": simulation_id},
        )
        network_rows = self.database.list_records(
            "simulation_networks",
            filters={"simulation_id": simulation_id},
            order_by="network_slot",
        )
        if {row["network_slot"] for row in network_rows} != {"A", "B"}:
            raise SimulationError(
                "A simulation requires exactly Network A and Network B."
            )
        if run["status"] not in {"created", "queued"}:
            raise SimulationError(
                f"Simulation {simulation_id} cannot run from status "
                f"'{run['status']}'."
            )
        if self.database.list_records(
            "simulation_events",
            filters={"simulation_id": simulation_id},
            limit=1,
        ):
            raise SimulationError(
                "This simulation already contains a generated event stream."
            )

        network_ids = [row["id"] for row in network_rows]
        try:
            self.database.simulations.update_run_status(
                simulation_id,
                "running",
            )
            for network_id in network_ids:
                self.database.simulations.update_network_status(
                    network_id,
                    "running",
                )

            self._emit_progress(
                progress_callback,
                simulation_id,
                phase="preparing",
                progress=0,
                events_processed=0,
                total_events=0,
                virtual_time_ms=0,
            )
            device_rows = self.database.list_records(
                "simulation_devices",
                filters={"simulation_id": simulation_id},
                order_by="device_key",
            )
            environment = IoTEnvironmentRuntime.from_simulation_records(
                device_rows
            )
            traffic_config = self._traffic_config(run)
            opportunities = PoissonEventGenerator(
                environment,
                traffic_config,
                random_seed=run["random_seed"],
            ).generate()
            self.database.simulations.insert_events(
                (
                    opportunity.to_database_record(simulation_id)
                    for opportunity in opportunities
                ),
                chunk_size=self.persistence_batch_size,
            )
            self._attach_opportunity_ids(simulation_id, opportunities)

            engines = {
                row["network_slot"]: self._build_network_model(
                    row,
                    environment,
                    random_seed=run["random_seed"],
                )
                for row in network_rows
            }
            summaries = self._execute_opportunities(
                simulation_id=simulation_id,
                run=run,
                opportunities=opportunities,
                engines=engines,
                progress_callback=progress_callback,
            )
            self._persist_network_results(engines)
            comparison = self._persist_comparison(
                simulation_id,
                summaries,
                comparison_model=self._comparison_model(run),
                evaluation_config=self._evaluation_config(run),
            )

            for network_id in network_ids:
                self.database.simulations.update_network_status(
                    network_id,
                    "completed",
                )
            self.database.simulations.update_run_status(
                simulation_id,
                "completed",
            )
            final_virtual_time = max(
                engine.current_time_ms for engine in engines.values()
            )
            self._emit_progress(
                progress_callback,
                simulation_id,
                phase="completed",
                progress=1,
                events_processed=len(opportunities),
                total_events=len(opportunities),
                virtual_time_ms=final_virtual_time,
            )
            return SimulationExecutionResult(
                simulation_id=simulation_id,
                event_count=len(opportunities),
                final_virtual_time_ms=final_virtual_time,
                network_summaries=summaries,
                comparison=comparison,
                exogenous_fingerprint=self._exogenous_fingerprint(
                    run,
                    device_rows,
                    network_rows,
                    opportunities,
                ),
            )
        except Exception as exc:
            self._mark_failed(simulation_id, network_ids, exc)
            if isinstance(exc, CoreError):
                raise
            raise SimulationError(
                f"Simulation {simulation_id} failed: {exc}."
            ) from exc

    def _execute_opportunities(
        self,
        *,
        simulation_id: int,
        run: Mapping[str, Any],
        opportunities: Sequence[ExternalOpportunity],
        engines: Mapping[str, SimulationArmRuntime],
        progress_callback: ProgressCallback | None,
    ) -> dict[str, dict[str, Any]]:
        duration_ms = round(float(run["duration_seconds"]) * 1_000)
        sample_interval_ms = int(run["sample_interval_ms"])
        next_sample_ms = 0
        state_buffer: list[dict[str, Any]] = []
        metric_buffer: list[dict[str, Any]] = []

        for index, opportunity in enumerate(opportunities, start=1):
            while (
                next_sample_ms <= opportunity.scheduled_at_ms
                and next_sample_ms <= duration_ms
            ):
                self._collect_samples(
                    engines,
                    next_sample_ms,
                    state_buffer,
                    metric_buffer,
                )
                self._flush_sample_buffers(
                    state_buffer,
                    metric_buffer,
                    force=False,
                )
                next_sample_ms += sample_interval_ms

            for engine in engines.values():
                engine.process_opportunity(opportunity.copy())

            if (
                index == len(opportunities)
                or index % max(1, len(opportunities) // 100) == 0
            ):
                self._emit_progress(
                    progress_callback,
                    simulation_id,
                    phase="running",
                    progress=index / max(1, len(opportunities)),
                    events_processed=index,
                    total_events=len(opportunities),
                    virtual_time_ms=opportunity.scheduled_at_ms,
                )

        while next_sample_ms <= duration_ms:
            self._collect_samples(
                engines,
                next_sample_ms,
                state_buffer,
                metric_buffer,
            )
            self._flush_sample_buffers(
                state_buffer,
                metric_buffer,
                force=False,
            )
            next_sample_ms += sample_interval_ms

        for engine in engines.values():
            final_time = engine.finalize(duration_ms)
            state_buffer.extend(engine.device_state_records(final_time))
            metric_buffer.append(engine.metric_record(final_time))

        self._flush_sample_buffers(
            state_buffer,
            metric_buffer,
            force=True,
        )
        return {
            slot: engine.summary_record()
            for slot, engine in engines.items()
        }

    def _collect_samples(
        self,
        engines: Mapping[str, SimulationArmRuntime],
        elapsed_ms: int,
        state_buffer: list[dict[str, Any]],
        metric_buffer: list[dict[str, Any]],
    ) -> None:
        for engine in engines.values():
            engine.advance_to(elapsed_ms)
            state_buffer.extend(engine.device_state_records(elapsed_ms))
            metric_buffer.append(engine.metric_record(elapsed_ms))

    def _flush_sample_buffers(
        self,
        state_buffer: list[dict[str, Any]],
        metric_buffer: list[dict[str, Any]],
        *,
        force: bool,
    ) -> None:
        if force or len(state_buffer) >= self.sample_batch_size:
            if state_buffer:
                self.database.simulations.upsert_device_state_samples(
                    state_buffer,
                    chunk_size=self.persistence_batch_size,
                )
                state_buffer.clear()
        if force or len(metric_buffer) >= self.sample_batch_size:
            if metric_buffer:
                self.database.simulations.upsert_network_metric_samples(
                    metric_buffer,
                    chunk_size=self.persistence_batch_size,
                )
                metric_buffer.clear()

    def _persist_network_results(
        self,
        engines: Mapping[str, SimulationArmRuntime],
    ) -> None:
        for engine in engines.values():
            engine.persist_to_database(
                self.database,
                chunk_size=self.persistence_batch_size,
            )
            self.database.simulations.upsert_network_summary(
                engine.summary_record()
            )

    def _persist_comparison(
        self,
        simulation_id: int,
        summaries: Mapping[str, Mapping[str, Any]],
        *,
        comparison_model: str = "legacy_networks",
        evaluation_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        summary_a = summaries["A"]
        summary_b = summaries["B"]
        legacy_comparison = comparison_model == "legacy_networks"
        network_metric_specs = [
            (
                "confirmed_transactions",
                "Confirmed transactions",
                "confirmed_transactions",
                "higher",
                "transactions",
            ),
            (
                "rejected_transactions",
                "Rejected transactions",
                "rejected_transactions",
                "lower",
                "transactions",
            ),
            (
                "throughput",
                "Average throughput",
                "average_throughput_tps",
                "higher",
                "tx/s",
            ),
            (
                "confirmation_latency",
                "Average confirmation time",
                "average_confirmation_ms",
                "lower",
                "ms",
            ),
            (
                "total_blocks",
                "Total blocks",
                "total_blocks",
                "higher",
                "blocks",
            ),
            (
                "total_fees",
                "Network fees",
                "total_fees",
                "lower",
                "currency",
            ),
        ]
        incentive_metric_specs = [
            (
                "active_devices",
                "Active devices",
                "final_active_devices",
                "higher",
                "devices",
            ),
            (
                "final_retention_rate",
                "Final retention rate (endpoint)",
                "final_retention_rate",
                "higher",
                "ratio",
            ),
            (
                "average_active_device_ratio",
                "Average active-device ratio",
                "average_active_device_ratio",
                "higher",
                "ratio",
            ),
            (
                "opportunity_participation_rate",
                "Opportunity participation rate",
                "opportunity_participation_rate",
                "higher",
                "ratio",
            ),
            (
                "churn_rate",
                "Endpoint churn rate",
                "churn_rate",
                "lower",
                "ratio",
            ),
            (
                "useful_contribution_count",
                "Mechanism-credited contributions",
                "useful_contribution_count",
                "higher",
                "contributions",
            ),
            (
                "useful_contribution_rate",
                "Mechanism-credited contribution rate",
                "useful_contribution_rate",
                "higher",
                "ratio",
            ),
            (
                "useful_contribution_per_active_device",
                "Mechanism-credited contributions per active device",
                "useful_contribution_per_active_device",
                "higher",
                "contributions/device",
            ),
            (
                "total_rewards",
                "Total incentive rewards",
                "total_rewards",
                "neutral",
                "currency",
            ),
            (
                "total_penalties",
                "Total incentive penalties",
                "total_penalties",
                "lower",
                "currency",
            ),
            (
        "net_incentive_cost",
        "Reward expenditure" if not legacy_comparison else "Net incentive cost",
        (
            "legacy_net_incentive_cost"
            if legacy_comparison
            else "net_incentive_cost"
        ),
        "lower",
        "currency",
    ),
    (
        "incentive_cost_per_useful_contribution",
        (
            "Reward expenditure per mechanism-credited contribution"
            if not legacy_comparison
            else "Incentive cost per useful contribution"
        ),
        (
            "legacy_incentive_cost_per_useful_contribution"
            if legacy_comparison
            else "incentive_cost_per_useful_contribution"
        ),
        "lower",
        "currency/contribution",
    ),
            (
                "reward_distribution_fairness",
                "Reward distribution fairness",
                "reward_distribution_fairness",
                "higher",
                "ratio",
            ),
            (
        "utility_distribution_fairness",
        "Profit distribution fairness (device economic balance)",
        "utility_distribution_fairness",
        "higher",
        "ratio",
    ),
        ]
        metric_groups = {
            "network_performance": network_metric_specs,
            "incentive_effectiveness": incentive_metric_specs,
        }
        metrics_by_category: dict[str, list[dict[str, Any]]] = {}
        for category, specs in metric_groups.items():
            category_metrics = []
            for (
                metric_name,
                display_name,
                summary_key,
                direction,
                unit,
            ) in specs:
                metric = compare_metric(
                    metric_name=metric_name,
                    display_name=display_name,
                    value_a=self._summary_metric_value(
                        summary_a,
                        summary_key,
                    ),
                    value_b=self._summary_metric_value(
                        summary_b,
                        summary_key,
                    ),
                    preferred_direction=direction,
                    unit=unit,
                )
                metric["simulation_id"] = simulation_id
                metric["details_json"] = {"category": category}
                if category == "incentive_effectiveness":
                    metric["details_json"]["dimension"] = (
                        {
                            "opportunity_participation_rate": "participation",
                            "final_retention_rate": "retention",
                            "useful_contribution_rate": (
                                "useful_contribution"
                            ),
                            "incentive_cost_per_useful_contribution": (
                                "incentive_efficiency"
                            ),
                            "reward_distribution_fairness": "fairness",
                        }.get(summary_key)
                    )
                category_metrics.append(metric)
            metrics_by_category[category] = category_metrics

        metrics = [
            metric
            for category_metrics in metrics_by_category.values()
            for metric in category_metrics
        ]
        legacy_score_a, legacy_score_b, legacy_winner = (
            score_comparison(metrics)
        )
        if comparison_model == "incentive_mechanisms":
            incentive_score = weighted_incentive_effectiveness(
                metrics_by_category["incentive_effectiveness"],
                weights=(
                    evaluation_config or incentive_evaluation_configuration()
                ).get("weights"),
                references=(
                    evaluation_config or incentive_evaluation_configuration()
                ).get("references"),
                normalization=(
                    evaluation_config or incentive_evaluation_configuration()
                ).get("normalization", "reference_range_clipped"),
            )
            network_score = weighted_score_comparison(
                {"network_performance": metrics_by_category["network_performance"]},
                weights={"network_performance": 1.0},
            )
            weighted = incentive_score
        else:
            weighted = weighted_score_comparison(metrics_by_category)
            incentive_score = weighted["categories"].get(
                "incentive_effectiveness",
                {},
            )
            network_score = weighted["categories"].get(
                "network_performance",
                {},
            )
        category_scores = weighted.get("categories", {})
        if comparison_model == "incentive_mechanisms":
            category_scores = {
                "incentive_effectiveness": incentive_score,
                "network_performance": network_score,
            }
        comparison = {
            "simulation_id": simulation_id,
            "score_a": weighted["score_a"],
            "score_b": weighted["score_b"],
            "winner_slot": weighted["winner_slot"],
            "summary_json": {
                "metric_winners": {
                    metric["metric_name"]: metric["winner_slot"]
                    for metric in metrics
                },
                "network_performance": category_scores.get(
                    "network_performance",
                    {},
                ),
                "incentive_effectiveness": category_scores.get(
                    "incentive_effectiveness",
                    {},
                ),
                "incentive_effectiveness_score": incentive_score,
                "network_context_score": network_score,
                "comparison_model": comparison_model,
                "combined": weighted,
                "legacy_unweighted": {
                    "score_a": legacy_score_a,
                    "score_b": legacy_score_b,
                    "winner_slot": legacy_winner,
                },
                "evaluation_configuration": (
                    evaluation_config or incentive_evaluation_configuration()
                ),
            },
        }
        self.database.simulations.save_comparison(comparison, metrics)
        return comparison

    @staticmethod
    def _comparison_model(run: Mapping[str, Any]) -> str:
        snapshot = run.get("configuration_snapshot_json")
        if isinstance(snapshot, Mapping):
            experiment = snapshot.get("experiment")
            if isinstance(experiment, Mapping):
                return str(
                    experiment.get("comparison_model", "legacy_networks")
                )
        return "legacy_networks"

    @staticmethod
    def _exogenous_fingerprint(
        run: Mapping[str, Any],
        device_rows: Sequence[Mapping[str, Any]],
        network_rows: Sequence[Mapping[str, Any]],
        opportunities: Sequence[ExternalOpportunity],
    ) -> str:
        """Hash shared run inputs, excluding incentive and arm identities."""
        del network_rows
        snapshot = run.get("configuration_snapshot_json", {})
        comparison_model = (
            snapshot.get("experiment", {}).get("comparison_model")
            if isinstance(snapshot, Mapping)
            else None
        )
        if comparison_model == "incentive_mechanisms":
            network_configuration = snapshot.get("network_bundle", {})
        else:
            network_configuration = {
                "network_a": snapshot.get("network_a_bundle", {}),
                "network_b": snapshot.get("network_b_bundle", {}),
            }
        payload = {
            "effective_seed": run.get("random_seed"),
            "experiment": {
                key: snapshot
                .get("experiment", {})
                .get(key)
                for key in (
                    "comparison_model",
                    "poisson_lambda",
                    "duration_seconds",
                    "sample_interval_ms",
                    "traffic_mix_json",
                    "parameters_json",
                )
            },
            "environment_devices": [
                {
                    **{
                        key: device.get(key)
                        for key in (
                            "device_key",
                            "group_name",
                            "precision",
                            "data_rate",
                            "execution_cost",
                            "profit_expectation",
                            "initial_balance",
                        )
                    },
                    "behavior": device.get("behavior_snapshot_json", {}),
                }
                for device in device_rows
            ],
            "network_configuration": network_configuration,
            "opportunities": [
                {
                    "sequence_number": opportunity.sequence_number,
                    "scheduled_at_ms": opportunity.scheduled_at_ms,
                    "event_type": opportunity.event_type.value,
                    "sender_device_key": opportunity.sender_device_key,
                    "target_device_key": opportunity.target_device_key,
                    "amount": opportunity.amount,
                    "payload": opportunity.payload,
                }
                for opportunity in opportunities
            ],
        }
        canonical = json.dumps(
            SimulationEngine._fingerprint_value(payload),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _fingerprint_value(value: Any) -> Any:
        """Remove persistence-only identity and volatile timestamp fields."""
        if isinstance(value, Mapping):
            return {
                key: SimulationEngine._fingerprint_value(item)
                for key, item in value.items()
                if key not in {
                    "id",
                    "environment_id",
                    "behavior_id",
                    "network_config_id",
                    "network_a_config_id",
                    "network_b_config_id",
                    "incentive_a_config_id",
                    "incentive_b_config_id",
                    "reward_artifact_id",
                    "blockchain_logic_artifact_id",
                    "code_artifact_id",
                    "created_at",
                    "updated_at",
                    "exported_at",
                }
            }
        if isinstance(value, list):
            return [
                SimulationEngine._fingerprint_value(item)
                for item in value
            ]
        return value

    @staticmethod
    def _evaluation_config(
        run: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = run.get("configuration_snapshot_json")
        if isinstance(snapshot, Mapping):
            execution = snapshot.get("execution")
            if isinstance(execution, Mapping):
                config = execution.get("evaluation_configuration")
                if isinstance(config, Mapping):
                    return dict(config)
        return incentive_evaluation_configuration()

    @staticmethod
    def _summary_metric_value(
        summary: Mapping[str, Any],
        key: str,
    ) -> Any:
        """Read a standard summary field or its JSON extension."""
        if key in summary:
            return summary[key]
        custom_summary = summary.get("custom_summary_json")
        if isinstance(custom_summary, Mapping):
            return custom_summary.get(key)
        return None

    def _build_network_model(
        self,
        network_row: Mapping[str, Any],
        environment: IoTEnvironmentRuntime,
        *,
        random_seed: int,
    ) -> SimulationArmRuntime:
        arm_bundle = network_row["configuration_snapshot_json"]
        if not isinstance(arm_bundle, Mapping):
            raise SimulationError(
                "Simulation arm snapshot is not a JSON object."
            )

        if arm_bundle.get("comparison_model") == (
            "incentive_mechanisms"
        ):
            network_bundle = arm_bundle.get("network_bundle")
            incentive_bundle = arm_bundle.get("incentive_bundle")
            if not isinstance(network_bundle, Mapping):
                raise SimulationError(
                    "Incentive-comparison arm is missing its network bundle."
                )
            if not isinstance(incentive_bundle, Mapping):
                raise SimulationError(
                    "Incentive-comparison arm is missing its incentive "
                    "bundle."
                )
        else:
            network_bundle = arm_bundle
            incentive_bundle = None

        network = network_bundle.get("network")
        if not isinstance(network, Mapping):
            raise SimulationError(
                "Network configuration snapshot is missing its network data."
            )
        is_incentive_comparison = incentive_bundle is not None
        network_parameters = dict(network.get("parameters_json") or {})
        if is_incentive_comparison:
            if network.get("reward_artifact_id") is not None:
                raise SimulationError(
                    "New incentive-comparison experiments cannot use a "
                    "network reward artifact."
                )
            forbidden_parameters = sorted(
                INCENTIVE_PARAMETER_KEYS.intersection(network_parameters)
            )
            if forbidden_parameters:
                raise SimulationError(
                    "New incentive-comparison network configuration contains "
                    "incentive parameters: "
                    + ", ".join(forbidden_parameters)
                    + "."
                )
        artifacts = {
            artifact["id"]: artifact
            for artifact in network_bundle.get("code_artifacts", [])
        }

        transaction_logic = self._load_artifact_function(
            artifacts,
            network.get("blockchain_logic_artifact_id"),
            "blockchain logic",
        )
        model_type = str(
            network.get("network_model_type")
            or network.get("model_type")
            or network.get("consensus_type")
            or "pow"
        ).lower()
        config = None
        if model_type == "pow":
            connectivity_policy = self._connectivity_policy(
                network_parameters,
                random_seed=random_seed,
            )
            config = BlockchainConfig(
                network_name=str(network["name"]),
                network_slot=str(network_row["network_slot"]),
                pow_difficulty=int(network["pow_difficulty"]),
                max_transactions_per_block=int(
                    network["max_transactions_per_block"]
                ),
                target_block_time_ms=network.get("target_block_time_ms"),
                transaction_fee_rate=float(network["transaction_fee_rate"]),
                parameters=network_parameters,
                transaction_logic=transaction_logic,
                legacy_reward_compatibility=not is_incentive_comparison,
                connectivity_policy=connectivity_policy,
            )
        factory = self.network_model_factory
        if factory is not None:
            factory_kwargs = {
                "simulation_network_id": int(network_row["id"]),
                "environment": environment,
                "config": config,
                "network_config": network,
                "random_seed": random_seed,
            }
            try:
                network_model = factory(**factory_kwargs)
            except TypeError as exc:
                if "network_config" not in str(exc):
                    raise
                factory_kwargs.pop("network_config")
                network_model = factory(**factory_kwargs)
        else:
            try:
                network_model = self.network_model_registry.create(
                    model_type,
                    simulation_network_id=int(network_row["id"]),
                    environment=environment,
                    config=config,
                    network_config=network,
                    random_seed=random_seed,
                )
            except ValueError as exc:
                raise SimulationError(str(exc)) from exc
        if not isinstance(network_model, NetworkModel):
            raise SimulationError(
                "Network model factory must return a NetworkModel."
            )
        return SimulationArmRuntime(
            network_model=network_model,
            incentive_mechanism=self._load_incentive_mechanism(
                incentive_bundle,
                network_bundle,
                legacy_reward_compatibility=not is_incentive_comparison,
            ),
            random_seed=random_seed,
        )

    @staticmethod
    def _default_network_model_factory(**kwargs) -> NetworkModel:
        """Build the current built-in PoW network adapter."""
        kwargs.pop("network_config", None)
        return PoWNetworkModel(**kwargs)

    @classmethod
    def _default_network_model_registry(cls) -> NetworkModelRegistry:
        registry = NetworkModelRegistry()
        registry.register("pow", cls._default_network_model_factory)
        return registry

    @staticmethod
    def _connectivity_policy(
        parameters: Mapping[str, Any],
        *,
        random_seed: int,
    ) -> ProbabilisticConnectivityPolicy | None:
        keys = {
            "link_availability_probability",
            "packet_delivery_success_probability",
            "communication_failure_probability",
            "latency_distribution_ms",
        }
        if not keys.intersection(parameters):
            return None
        latency = parameters.get("latency_distribution_ms", ())
        if isinstance(latency, list):
            latency = tuple(latency)
        if not isinstance(latency, tuple):
            raise SimulationError(
                "Connectivity latency distribution must be a list of "
                "non-negative integers."
            )
        try:
            return ProbabilisticConnectivityPolicy(
                random_seed=random_seed,
                link_availability_probability=float(
                    parameters.get(
                        "link_availability_probability",
                        1.0,
                    )
                ),
                packet_delivery_success_probability=float(
                    parameters.get(
                        "packet_delivery_success_probability",
                        1.0,
                    )
                ),
                communication_failure_probability=float(
                    parameters.get(
                        "communication_failure_probability",
                        0.0,
                    )
                ),
                latency_distribution_ms=tuple(int(value) for value in latency),
            )
        except (TypeError, ValueError) as exc:
            raise SimulationError(
                f"Invalid connectivity configuration: {exc}."
            ) from exc

    def _build_pow_network_model(
        self,
        network_row: Mapping[str, Any],
        environment: IoTEnvironmentRuntime,
        *,
        random_seed: int,
    ) -> SimulationArmRuntime:
        """Compatibility wrapper for callers using the old private helper."""
        return self._build_network_model(
            network_row,
            environment,
            random_seed=random_seed,
        )

    def _load_incentive_mechanism(
        self,
        incentive_bundle: Mapping[str, Any] | None,
        network_bundle: Mapping[str, Any],
        *,
        legacy_reward_compatibility: bool = True,
    ) -> IncentiveMechanism:
        if incentive_bundle is None:
            network = network_bundle.get("network")
            if not isinstance(network, Mapping):
                raise SimulationError(
                    "Network snapshot is missing its network data."
                )
            reward_function = self._load_artifact_function(
                {
                    artifact["id"]: artifact
                    for artifact in network_bundle.get("code_artifacts", [])
                },
                network.get("reward_artifact_id"),
                "reward function",
            )
            return RewardIncentiveMechanism(
                reward_function,
                use_network_parameters=legacy_reward_compatibility,
            )

        incentive = incentive_bundle.get("incentive")
        if not isinstance(incentive, Mapping):
            raise SimulationError(
                "Incentive bundle is missing its configuration."
            )
        artifacts = {
            artifact["id"]: artifact
            for artifact in incentive_bundle.get("code_artifacts", [])
        }
        implementation_type = str(
            incentive.get("implementation_type", "")
        )
        parameters = dict(incentive.get("parameters_json") or {})
        if implementation_type == "built_in":
            key = incentive.get("built_in_key")
            if not key:
                raise SimulationError(
                    "Built-in incentive configuration is missing built_in_key."
                )
            try:
                return self.incentive_mechanism_registry.create(
                    str(key),
                    parameters,
                )
            except Exception as exc:
                if isinstance(exc, SimulationError):
                    raise
                raise SimulationError(str(exc)) from exc
        if implementation_type == "custom":
            function = self._load_artifact_function(
                artifacts,
                incentive.get("code_artifact_id"),
                "incentive mechanism",
            )
            if function is None:
                raise SimulationError(
                    "Custom incentive configuration has no code artifact."
                )
            return PluginIncentiveMechanism(
                function,
                parameters=parameters,
            )
        if implementation_type == "legacy_reward":
            function = self._load_artifact_function(
                artifacts,
                incentive.get("code_artifact_id"),
                "incentive mechanism",
            )
            return RewardIncentiveMechanism(
                function,
                parameters=parameters,
                use_network_parameters=False,
            )
        raise SimulationError(
            "Unsupported incentive implementation type: "
            f"{implementation_type}."
        )

    @staticmethod
    def _load_artifact_function(
        artifacts: Mapping[int, Mapping[str, Any]],
        artifact_id: int | None,
        label: str,
    ):
        if artifact_id is None:
            return None
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise SimulationError(
                f"The configured {label} artifact is missing from the "
                "immutable run snapshot."
            )
        return load_plugin_function(
            artifact["source_code"],
            artifact["entrypoint"],
            plugin_name=f"{label}: {artifact['name']}",
        )

    @staticmethod
    def _traffic_config(run: Mapping[str, Any]) -> PoissonTrafficConfig:
        snapshot = run["configuration_snapshot_json"]
        experiment = snapshot.get("experiment")
        if not isinstance(experiment, Mapping):
            raise SimulationError(
                "Simulation configuration snapshot is missing experiment data."
            )
        parameters = dict(experiment.get("parameters_json") or {})
        return PoissonTrafficConfig(
            lambda_per_second=float(run["poisson_lambda"]),
            duration_seconds=float(run["duration_seconds"]),
            traffic_mix=dict(experiment.get("traffic_mix_json") or {}),
            transfer_amount_min=float(
                parameters.get("transfer_amount_min", 0.1)
            ),
            transfer_amount_max=float(
                parameters.get("transfer_amount_max", 5.0)
            ),
            positive_feedback_probability=float(
                parameters.get("positive_feedback_probability", 0.7)
            ),
            max_events=int(parameters.get("max_events", 1_000_000)),
        )

    def _attach_opportunity_ids(
        self,
        simulation_id: int,
        opportunities: Sequence[ExternalOpportunity],
    ) -> None:
        stored_opportunities = self.database.list_records(
            "simulation_events",
            filters={"simulation_id": simulation_id},
            order_by="sequence_number",
        )
        if len(stored_opportunities) != len(opportunities):
            raise SimulationError(
                "Persisted event count does not match generated event count."
            )
        for opportunity, record in zip(
            opportunities,
            stored_opportunities,
            strict=True,
        ):
            if opportunity.sequence_number != record["sequence_number"]:
                raise SimulationError(
                    "Persisted event ordering does not match generation order."
                )
            opportunity.database_id = record["id"]

    def _mark_failed(
        self,
        simulation_id: int,
        network_ids: list[int],
        error: Exception,
    ) -> None:
        message = str(error)
        for network_id in network_ids:
            try:
                self.database.simulations.update_network_status(
                    network_id,
                    "failed",
                    error_message=message,
                )
            except Exception:
                pass
        try:
            self.database.simulations.update_run_status(
                simulation_id,
                "failed",
                error_message=message,
            )
        except Exception:
            pass

    @staticmethod
    def _emit_progress(
        callback: ProgressCallback | None,
        simulation_id: int,
        *,
        phase: str,
        progress: float,
        events_processed: int,
        total_events: int,
        virtual_time_ms: int,
    ) -> None:
        if callback is None:
            return
        callback(
            SimulationProgress(
                simulation_id=simulation_id,
                phase=phase,
                progress=max(0, min(1, progress)),
                events_processed=events_processed,
                total_events=total_events,
                virtual_time_ms=virtual_time_ms,
            )
        )
