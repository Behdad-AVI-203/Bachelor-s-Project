"""Dual-network simulation orchestration using a fast virtual clock."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.database import DatabaseService

from .blockchain import (
    INCENTIVE_PARAMETER_KEYS,
    BlockchainConfig,
    PoWNetworkModel,
)
from .errors import CoreError, SimulationError
from .incentives import (
    IncentiveMechanism,
    PluginIncentiveMechanism,
    RewardIncentiveMechanism,
)
from .iot import IoTEnvironmentRuntime
from .metrics import (
    compare_metric,
    score_comparison,
    weighted_score_comparison,
)
from .models import ExternalOpportunity
from .orchestration import SimulationArmRuntime
from .network import NetworkModel, NetworkModelFactory
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
    ) -> None:
        if sample_batch_size <= 0 or persistence_batch_size <= 0:
            raise SimulationError("Persistence batch sizes must be positive.")
        self.database = database
        self.sample_batch_size = sample_batch_size
        self.persistence_batch_size = persistence_batch_size
        self.network_model_factory = (
            network_model_factory or self._default_network_model_factory
        )

    def run_experiment(
        self,
        experiment_config_id: int,
        *,
        name: str | None = None,
        random_seed: int | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> SimulationExecutionResult:
        """Create and execute a fresh run from a saved experiment."""
        run = self.database.simulations.create_run_from_experiment(
            experiment_config_id,
            name=name,
            random_seed=random_seed,
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
            block_records, _ = engine.persistence_records()
            if block_records:
                self.database.simulations.insert_blocks(
                    block_records,
                    chunk_size=self.persistence_batch_size,
                )
            stored_blocks = self.database.list_records(
                "blocks",
                filters={
                    "simulation_network_id": engine.simulation_network_id
                },
                order_by="height",
            )
            block_ids_by_height = {
                block["height"]: block["id"] for block in stored_blocks
            }
            _, transaction_records = engine.persistence_records(
                block_ids_by_height
            )
            if transaction_records:
                self.database.simulations.insert_transactions(
                    transaction_records,
                    chunk_size=self.persistence_batch_size,
                )
            self.database.simulations.upsert_network_summary(
                engine.summary_record()
            )

    def _persist_comparison(
        self,
        simulation_id: int,
        summaries: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        summary_a = summaries["A"]
        summary_b = summaries["B"]
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
                "Final retention rate",
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
                "Churn rate",
                "churn_rate",
                "lower",
                "ratio",
            ),
            (
                "useful_contribution_count",
                "Useful contributions",
                "useful_contribution_count",
                "higher",
                "contributions",
            ),
            (
                "useful_contribution_rate",
                "Useful contribution rate",
                "useful_contribution_rate",
                "higher",
                "ratio",
            ),
            (
                "useful_contribution_per_active_device",
                "Useful contributions per active device",
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
                "Net incentive cost",
                "net_incentive_cost",
                "lower",
                "currency",
            ),
            (
                "incentive_cost_per_useful_contribution",
                "Incentive cost per useful contribution",
                "incentive_cost_per_useful_contribution",
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
                "Utility distribution fairness",
                "utility_distribution_fairness",
                "higher",
                "ratio",
            ),
            (
                "gini_coefficient",
                "Balance Gini coefficient",
                "final_gini_coefficient",
                "lower",
                None,
            ),
            (
                "balance_variance",
                "Balance variance",
                "final_balance_variance",
                "lower",
                None,
            ),
            (
                "total_costs",
                "Total device/network costs",
                "total_costs",
                "lower",
                "currency",
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
        weighted = weighted_score_comparison(metrics_by_category)
        category_scores = weighted["categories"]
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
                "combined": weighted,
                "legacy_unweighted": {
                    "score_a": legacy_score_a,
                    "score_b": legacy_score_b,
                    "winner_slot": legacy_winner,
                },
            },
        }
        self.database.simulations.save_comparison(comparison, metrics)
        return comparison

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
        consensus_type = str(network.get("consensus_type", "pow")).lower()
        if consensus_type != "pow":
            raise SimulationError(
                f"Unsupported network consensus type: {consensus_type}."
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
        )
        network_model = self.network_model_factory(
            simulation_network_id=int(network_row["id"]),
            environment=environment,
            config=config,
            random_seed=random_seed,
        )
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
        return PoWNetworkModel(**kwargs)

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
            return RewardIncentiveMechanism(
                parameters=parameters,
                use_network_parameters=False,
            )
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
