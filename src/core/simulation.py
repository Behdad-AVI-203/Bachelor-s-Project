"""Dual-network simulation orchestration using a fast virtual clock."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.database import DatabaseService

from .blockchain import BlockchainConfig, BlockchainEngine
from .errors import CoreError, SimulationError
from .incentives import RewardIncentiveMechanism
from .iot import IoTEnvironmentRuntime
from .metrics import compare_metric, score_comparison
from .models import SimulationEvent
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
    """Generate shared events, run A/B engines, and persist all results."""

    def __init__(
        self,
        database: DatabaseService,
        *,
        sample_batch_size: int = 5_000,
        persistence_batch_size: int = 2_000,
    ) -> None:
        if sample_batch_size <= 0 or persistence_batch_size <= 0:
            raise SimulationError("Persistence batch sizes must be positive.")
        self.database = database
        self.sample_batch_size = sample_batch_size
        self.persistence_batch_size = persistence_batch_size

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
            events = PoissonEventGenerator(
                environment,
                traffic_config,
                random_seed=run["random_seed"],
            ).generate()
            self.database.simulations.insert_events(
                (
                    event.to_database_record(simulation_id)
                    for event in events
                ),
                chunk_size=self.persistence_batch_size,
            )
            self._attach_event_ids(simulation_id, events)

            engines = {
                row["network_slot"]: self._build_blockchain_engine(
                    row,
                    environment,
                    random_seed=run["random_seed"],
                )
                for row in network_rows
            }
            summaries = self._execute_events(
                simulation_id=simulation_id,
                run=run,
                events=events,
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
                events_processed=len(events),
                total_events=len(events),
                virtual_time_ms=final_virtual_time,
            )
            return SimulationExecutionResult(
                simulation_id=simulation_id,
                event_count=len(events),
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

    def _execute_events(
        self,
        *,
        simulation_id: int,
        run: Mapping[str, Any],
        events: Sequence[SimulationEvent],
        engines: Mapping[str, BlockchainEngine],
        progress_callback: ProgressCallback | None,
    ) -> dict[str, dict[str, Any]]:
        duration_ms = round(float(run["duration_seconds"]) * 1_000)
        sample_interval_ms = int(run["sample_interval_ms"])
        next_sample_ms = 0
        state_buffer: list[dict[str, Any]] = []
        metric_buffer: list[dict[str, Any]] = []

        for index, event in enumerate(events, start=1):
            while (
                next_sample_ms <= event.scheduled_at_ms
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
                engine.process_event(event)

            if index == len(events) or index % max(1, len(events) // 100) == 0:
                self._emit_progress(
                    progress_callback,
                    simulation_id,
                    phase="running",
                    progress=index / max(1, len(events)),
                    events_processed=index,
                    total_events=len(events),
                    virtual_time_ms=event.scheduled_at_ms,
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
            final_time = engine.flush(duration_ms)
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
        engines: Mapping[str, BlockchainEngine],
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
        engines: Mapping[str, BlockchainEngine],
    ) -> None:
        for engine in engines.values():
            block_records = engine.block_records()
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
            transaction_records = engine.transaction_records(
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
        metric_specs = [
            (
                "active_devices",
                "Active devices",
                "final_active_devices",
                "higher",
                "devices",
            ),
            (
                "churn_rate",
                "Churn rate",
                "final_churn_rate",
                "lower",
                "ratio",
            ),
            (
                "gini_coefficient",
                "Gini coefficient",
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
                "total_rewards",
                "Total device rewards",
                "total_rewards",
                "higher",
                "currency",
            ),
            (
                "total_costs",
                "Total device costs",
                "total_costs",
                "lower",
                "currency",
            ),
        ]
        metrics = []
        for (
            metric_name,
            display_name,
            summary_key,
            direction,
            unit,
        ) in metric_specs:
            metric = compare_metric(
                metric_name=metric_name,
                display_name=display_name,
                value_a=summary_a.get(summary_key),
                value_b=summary_b.get(summary_key),
                preferred_direction=direction,
                unit=unit,
            )
            metric["simulation_id"] = simulation_id
            metrics.append(metric)

        score_a, score_b, winner = score_comparison(metrics)
        comparison = {
            "simulation_id": simulation_id,
            "score_a": score_a,
            "score_b": score_b,
            "winner_slot": winner,
            "summary_json": {
                "metric_winners": {
                    metric["metric_name"]: metric["winner_slot"]
                    for metric in metrics
                }
            },
        }
        self.database.simulations.save_comparison(comparison, metrics)
        return comparison

    def _build_blockchain_engine(
        self,
        network_row: Mapping[str, Any],
        environment: IoTEnvironmentRuntime,
        *,
        random_seed: int,
    ) -> BlockchainEngine:
        bundle = network_row["configuration_snapshot_json"]
        network = bundle.get("network")
        if not isinstance(network, Mapping):
            raise SimulationError(
                "Network configuration snapshot is missing its network data."
            )
        artifacts = {
            artifact["id"]: artifact
            for artifact in bundle.get("code_artifacts", [])
        }

        reward_function = self._load_artifact_function(
            artifacts,
            network.get("reward_artifact_id"),
            "reward function",
        )
        transaction_logic = self._load_artifact_function(
            artifacts,
            network.get("blockchain_logic_artifact_id"),
            "blockchain logic",
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
            parameters=dict(network.get("parameters_json") or {}),
            transaction_logic=transaction_logic,
        )
        return BlockchainEngine(
            simulation_network_id=int(network_row["id"]),
            environment=environment,
            config=config,
            random_seed=random_seed,
            incentive_mechanism=RewardIncentiveMechanism(reward_function),
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

    def _attach_event_ids(
        self,
        simulation_id: int,
        events: Sequence[SimulationEvent],
    ) -> None:
        stored_events = self.database.list_records(
            "simulation_events",
            filters={"simulation_id": simulation_id},
            order_by="sequence_number",
        )
        if len(stored_events) != len(events):
            raise SimulationError(
                "Persisted event count does not match generated event count."
            )
        for event, record in zip(events, stored_events, strict=True):
            if event.sequence_number != record["sequence_number"]:
                raise SimulationError(
                    "Persisted event ordering does not match generation order."
                )
            event.database_id = record["id"]

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
