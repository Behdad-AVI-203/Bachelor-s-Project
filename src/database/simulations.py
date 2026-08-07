"""Simulation execution persistence and result-query operations."""

from __future__ import annotations

import secrets
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .configurations import ConfigurationRepository
from .connection import Record, SQLiteDatabase
from .errors import RecordNotFoundError, ValidationError


class SimulationRepository:
    """Persist simulation runs and provide result/history read models."""

    def __init__(
        self,
        database: SQLiteDatabase,
        configurations: ConfigurationRepository,
    ) -> None:
        self.database = database
        self.configurations = configurations

    def create_run_from_experiment(
        self,
        experiment_config_id: int,
        *,
        name: str | None = None,
        random_seed: int | None = None,
    ) -> Record:
        """Snapshot an experiment and create its A/B runtime records."""
        snapshot = self.configurations.export_configuration(
            "full",
            experiment_config_id,
        )
        experiment = snapshot["experiment"]
        environment_bundle = snapshot["environment_bundle"]
        selected_seed = random_seed
        if selected_seed is None:
            selected_seed = experiment.get("default_random_seed")
        if selected_seed is None:
            selected_seed = secrets.randbits(63)

        with self.database.transaction() as connection:
            simulation_id = self.database._insert(
                connection,
                "simulation_runs",
                {
                    "experiment_config_id": experiment_config_id,
                    "environment_id": experiment["environment_id"],
                    "name": name or experiment["name"],
                    "status": "created",
                    "poisson_lambda": experiment["poisson_lambda"],
                    "duration_seconds": experiment["duration_seconds"],
                    "sample_interval_ms": experiment["sample_interval_ms"],
                    "random_seed": selected_seed,
                    "configuration_snapshot_json": snapshot,
                },
            )

            network_ids = {}
            for slot, bundle_key, config_key in (
                ("A", "network_a_bundle", "network_a_config_id"),
                ("B", "network_b_bundle", "network_b_config_id"),
            ):
                network_ids[slot] = self.database._insert(
                    connection,
                    "simulation_networks",
                    {
                        "simulation_id": simulation_id,
                        "network_slot": slot,
                        "network_config_id": experiment[config_key],
                        "status": "created",
                        "configuration_snapshot_json": snapshot[bundle_key],
                    },
                )

            behavior_by_id = {
                behavior["id"]: behavior
                for behavior in environment_bundle["device_behaviors"]
            }
            simulation_devices = []
            for device in environment_bundle["devices"]:
                if not device["enabled"]:
                    continue
                behavior = behavior_by_id[device["behavior_id"]]
                overrides = device.get("overrides_json") or {}
                simulation_devices.append(
                    {
                        "simulation_id": simulation_id,
                        "source_device_id": device["id"],
                        "device_key": device["device_key"],
                        "display_name": device.get("display_name"),
                        "initial_balance": device["initial_balance"],
                        "precision": overrides.get(
                            "precision",
                            behavior["precision"],
                        ),
                        "execution_cost": overrides.get(
                            "execution_cost",
                            behavior["execution_cost"],
                        ),
                        "data_rate": overrides.get(
                            "data_rate",
                            behavior["data_rate"],
                        ),
                        "profit_expectation": overrides.get(
                            "profit_expectation",
                            behavior["profit_expectation"],
                        ),
                        "behavior_snapshot_json": {
                            "behavior": behavior,
                            "device_overrides": overrides,
                        },
                    }
                )

            if simulation_devices:
                self.database._insert_many(
                    connection,
                    "simulation_devices",
                    simulation_devices,
                )

        return {
            "simulation_id": simulation_id,
            "network_a_id": network_ids["A"],
            "network_b_id": network_ids["B"],
            "random_seed": selected_seed,
            "device_count": len(simulation_devices),
        }

    def create_simulation_run(self, values: Mapping[str, Any]) -> int:
        """Create a simulation run from an already prepared snapshot."""
        return self.database.create_record("simulation_runs", values)

    def create_simulation_network(self, values: Mapping[str, Any]) -> int:
        """Create a Network A or Network B runtime record."""
        return self.database.create_record("simulation_networks", values)

    def insert_simulation_devices(
        self,
        devices: Iterable[Mapping[str, Any]],
        *,
        chunk_size: int = 500,
    ) -> int:
        """Insert immutable runtime device snapshots."""
        return self.database.create_records(
            "simulation_devices",
            devices,
            chunk_size=chunk_size,
        )

    def insert_events(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        chunk_size: int = 1_000,
    ) -> int:
        """Insert the canonical Poisson-generated event stream."""
        return self.database.create_records(
            "simulation_events",
            events,
            chunk_size=chunk_size,
        )

    def insert_blocks(
        self,
        blocks: Iterable[Mapping[str, Any]],
        *,
        chunk_size: int = 500,
    ) -> int:
        """Insert mined blocks in one transaction."""
        return self.database.create_records(
            "blocks",
            blocks,
            chunk_size=chunk_size,
        )

    def insert_transactions(
        self,
        transactions: Iterable[Mapping[str, Any]],
        *,
        chunk_size: int = 1_000,
    ) -> int:
        """Insert network-specific transaction outcomes efficiently."""
        return self.database.create_records(
            "network_transactions",
            transactions,
            chunk_size=chunk_size,
        )

    def upsert_device_state_samples(
        self,
        samples: Iterable[Mapping[str, Any]],
        *,
        chunk_size: int = 2_000,
    ) -> int:
        """Batch insert or replace device-state samples by timestamp."""
        return self.database.upsert_records(
            "device_state_samples",
            samples,
            conflict_columns=(
                "simulation_network_id",
                "simulation_device_id",
                "elapsed_ms",
            ),
            chunk_size=chunk_size,
        )

    def upsert_network_metric_samples(
        self,
        samples: Iterable[Mapping[str, Any]],
        *,
        chunk_size: int = 2_000,
    ) -> int:
        """Batch insert or replace network metric samples by timestamp."""
        return self.database.upsert_records(
            "network_metric_samples",
            samples,
            conflict_columns=("simulation_network_id", "elapsed_ms"),
            chunk_size=chunk_size,
        )

    def upsert_network_summary(self, summary: Mapping[str, Any]) -> int:
        """Store the final summary for one simulated network."""
        return self.database.upsert_records(
            "network_run_summaries",
            [summary],
            conflict_columns=("simulation_network_id",),
        )

    def save_comparison(
        self,
        summary: Mapping[str, Any],
        metrics: Iterable[Mapping[str, Any]],
    ) -> None:
        """Atomically store the overall and per-metric A/B comparison."""
        metric_records = list(metrics)
        simulation_id = summary.get("simulation_id")
        if simulation_id is None:
            raise ValidationError(
                "Comparison summary requires a simulation_id."
            )

        with self.database.transaction() as connection:
            self.database._upsert_many(
                connection,
                "simulation_comparisons",
                [summary],
                conflict_columns=("simulation_id",),
            )
            if metric_records:
                for metric in metric_records:
                    if metric.get("simulation_id") != simulation_id:
                        raise ValidationError(
                            "All comparison metrics must use the same "
                            "simulation_id."
                        )
                self.database._upsert_many(
                    connection,
                    "comparison_metrics",
                    metric_records,
                    conflict_columns=("simulation_id", "metric_name"),
                )

    def update_run_status(
        self,
        simulation_id: int,
        status: str,
        *,
        error_message: str | None = None,
    ) -> None:
        """Update lifecycle timestamps alongside the run status."""
        values: Record = {
            "status": status,
            "error_message": error_message,
        }
        if status == "running":
            values["started_at"] = self._utc_now()
        if status in {"completed", "failed", "cancelled"}:
            values["completed_at"] = self._utc_now()

        affected = self.database.update_records(
            "simulation_runs",
            values,
            {"id": simulation_id},
        )
        if not affected:
            raise RecordNotFoundError(
                f"Simulation run {simulation_id} does not exist."
            )

    def update_network_status(
        self,
        simulation_network_id: int,
        status: str,
        *,
        error_message: str | None = None,
    ) -> None:
        """Update lifecycle timestamps for a network runtime."""
        values: Record = {
            "status": status,
            "error_message": error_message,
        }
        if status == "running":
            values["started_at"] = self._utc_now()
        if status in {"completed", "failed", "cancelled"}:
            values["completed_at"] = self._utc_now()

        affected = self.database.update_records(
            "simulation_networks",
            values,
            {"id": simulation_network_id},
        )
        if not affected:
            raise RecordNotFoundError(
                f"Simulation network {simulation_network_id} does not exist."
            )

    def get_simulation_results(
        self,
        simulation_id: int,
        *,
        include_time_series: bool = True,
        transaction_limit: int = 250,
    ) -> Record:
        """Return the complete read model required by the results page."""
        run = self.database.get_record(
            "simulation_runs",
            {"id": simulation_id},
        )
        networks = self.database.execute_query(
            """
            SELECT
                sn.*,
                nc.name AS network_name,
                nc.version AS network_version,
                nc.pow_difficulty,
                nrs.final_active_devices,
                nrs.final_churn_rate,
                nrs.final_gini_coefficient,
                nrs.final_balance_variance,
                nrs.total_transactions,
                nrs.confirmed_transactions,
                nrs.rejected_transactions,
                nrs.total_blocks,
                nrs.average_throughput_tps,
                nrs.average_confirmation_ms,
                nrs.total_rewards,
                nrs.total_costs,
                nrs.custom_summary_json,
                (
                    SELECT COUNT(*)
                    FROM blocks AS b
                    WHERE b.simulation_network_id = sn.id
                ) AS stored_block_count,
                (
                    SELECT COUNT(*)
                    FROM network_transactions AS nt
                    WHERE nt.simulation_network_id = sn.id
                ) AS stored_transaction_count
            FROM simulation_networks AS sn
            JOIN blockchain_network_configs AS nc
                ON nc.id = sn.network_config_id
            LEFT JOIN network_run_summaries AS nrs
                ON nrs.simulation_network_id = sn.id
            WHERE sn.simulation_id = ?
            ORDER BY sn.network_slot
            """,
            (simulation_id,),
        )

        comparison = self.database.get_record(
            "simulation_comparisons",
            {"simulation_id": simulation_id},
            required=False,
        )
        comparison_metrics = self.database.list_records(
            "comparison_metrics",
            filters={"simulation_id": simulation_id},
            order_by="metric_name",
        )

        result: Record = {
            "run": run,
            "networks": networks,
            "comparison": comparison,
            "comparison_metrics": comparison_metrics,
            "network_metrics": {},
            "latest_device_states": {},
            "recent_transactions": {},
        }

        for network in networks:
            slot = network["network_slot"]
            network_id = network["id"]
            if include_time_series:
                result["network_metrics"][slot] = self.get_network_metrics(
                    network_id
                )
            result["latest_device_states"][slot] = (
                self._get_latest_device_states(network_id)
            )
            result["recent_transactions"][slot] = self.get_transactions(
                simulation_id,
                network_slot=slot,
                limit=transaction_limit,
            )

        return result

    def get_simulation_history(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Record:
        """Return a paginated history-page read model."""
        if limit <= 0 or offset < 0:
            raise ValidationError(
                "History limit must be positive and offset non-negative."
            )

        where_sql = ""
        parameters: list[Any] = []
        if status is not None:
            where_sql = "WHERE sr.status = ?"
            parameters.append(status)

        count_rows = self.database.execute_query(
            f"SELECT COUNT(*) AS total FROM simulation_runs AS sr {where_sql}",
            parameters,
        )
        total = int(count_rows[0]["total"])

        rows = self.database.execute_query(
            f"""
            WITH network_history AS (
                SELECT
                    sn.simulation_id,
                    MAX(
                        CASE WHEN sn.network_slot = 'A'
                        THEN nc.name END
                    ) AS network_a_name,
                    MAX(
                        CASE WHEN sn.network_slot = 'B'
                        THEN nc.name END
                    ) AS network_b_name,
                    MAX(
                        CASE WHEN sn.network_slot = 'A'
                        THEN nrs.final_churn_rate END
                    ) AS network_a_churn_rate,
                    MAX(
                        CASE WHEN sn.network_slot = 'B'
                        THEN nrs.final_churn_rate END
                    ) AS network_b_churn_rate,
                    MAX(
                        CASE WHEN sn.network_slot = 'A'
                        THEN nrs.final_gini_coefficient END
                    ) AS network_a_gini,
                    MAX(
                        CASE WHEN sn.network_slot = 'B'
                        THEN nrs.final_gini_coefficient END
                    ) AS network_b_gini,
                    MAX(
                        CASE WHEN sn.network_slot = 'A'
                        THEN nrs.total_transactions END
                    ) AS network_a_transactions,
                    MAX(
                        CASE WHEN sn.network_slot = 'B'
                        THEN nrs.total_transactions END
                    ) AS network_b_transactions,
                    MAX(
                        CASE WHEN sn.network_slot = 'A'
                        THEN nrs.total_blocks END
                    ) AS network_a_blocks,
                    MAX(
                        CASE WHEN sn.network_slot = 'B'
                        THEN nrs.total_blocks END
                    ) AS network_b_blocks
                FROM simulation_networks AS sn
                JOIN blockchain_network_configs AS nc
                    ON nc.id = sn.network_config_id
                LEFT JOIN network_run_summaries AS nrs
                    ON nrs.simulation_network_id = sn.id
                GROUP BY sn.simulation_id
            )
            SELECT
                sr.id,
                sr.experiment_config_id,
                sr.environment_id,
                sr.name,
                sr.status,
                sr.poisson_lambda,
                sr.duration_seconds,
                sr.sample_interval_ms,
                sr.random_seed,
                sr.started_at,
                sr.completed_at,
                sr.error_message,
                sr.created_at,
                env.name AS environment_name,
                ec.name AS experiment_name,
                nh.network_a_name,
                nh.network_b_name,
                nh.network_a_churn_rate,
                nh.network_b_churn_rate,
                nh.network_a_gini,
                nh.network_b_gini,
                nh.network_a_transactions,
                nh.network_b_transactions,
                nh.network_a_blocks,
                nh.network_b_blocks,
                sc.score_a,
                sc.score_b,
                sc.winner_slot
            FROM simulation_runs AS sr
            JOIN iot_environments AS env
                ON env.id = sr.environment_id
            LEFT JOIN experiment_configs AS ec
                ON ec.id = sr.experiment_config_id
            LEFT JOIN network_history AS nh
                ON nh.simulation_id = sr.id
            LEFT JOIN simulation_comparisons AS sc
                ON sc.simulation_id = sr.id
            {where_sql}
            ORDER BY sr.created_at DESC, sr.id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, limit, offset],
        )

        return {
            "items": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_network_metrics(
        self,
        simulation_network_id: int,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[Record]:
        """Return ordered network-level time-series samples."""
        clauses = ["simulation_network_id = ?"]
        parameters: list[Any] = [simulation_network_id]

        if start_ms is not None:
            clauses.append("elapsed_ms >= ?")
            parameters.append(start_ms)
        if end_ms is not None:
            clauses.append("elapsed_ms <= ?")
            parameters.append(end_ms)

        return self.database.execute_query(
            f"""
            SELECT *
            FROM network_metric_samples
            WHERE {' AND '.join(clauses)}
            ORDER BY elapsed_ms
            """,
            parameters,
        )

    def get_device_states(
        self,
        simulation_network_id: int,
        *,
        device_ids: Sequence[int] | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[Record]:
        """Return device-state time series for charts and tables."""
        clauses = ["dss.simulation_network_id = ?"]
        parameters: list[Any] = [simulation_network_id]

        if device_ids is not None:
            if not device_ids:
                return []
            placeholders = ", ".join("?" for _ in device_ids)
            clauses.append(
                f"dss.simulation_device_id IN ({placeholders})"
            )
            parameters.extend(device_ids)
        if start_ms is not None:
            clauses.append("dss.elapsed_ms >= ?")
            parameters.append(start_ms)
        if end_ms is not None:
            clauses.append("dss.elapsed_ms <= ?")
            parameters.append(end_ms)

        return self.database.execute_query(
            f"""
            SELECT
                dss.*,
                sd.device_key,
                sd.display_name
            FROM device_state_samples AS dss
            JOIN simulation_devices AS sd
                ON sd.id = dss.simulation_device_id
            WHERE {' AND '.join(clauses)}
            ORDER BY dss.simulation_device_id, dss.elapsed_ms
            """,
            parameters,
        )

    def get_transactions(
        self,
        simulation_id: int,
        *,
        network_slot: str | None = None,
        status: str | None = None,
        transaction_type: str | None = None,
        limit: int = 250,
        offset: int = 0,
    ) -> list[Record]:
        """Return paginated transaction details for a simulation."""
        if limit <= 0 or offset < 0:
            raise ValidationError(
                "Transaction limit must be positive and offset non-negative."
            )

        clauses = ["sn.simulation_id = ?"]
        parameters: list[Any] = [simulation_id]
        if network_slot is not None:
            clauses.append("sn.network_slot = ?")
            parameters.append(network_slot)
        if status is not None:
            clauses.append("nt.status = ?")
            parameters.append(status)
        if transaction_type is not None:
            clauses.append("nt.transaction_type = ?")
            parameters.append(transaction_type)

        return self.database.execute_query(
            f"""
            SELECT
                nt.*,
                sn.network_slot,
                sender.device_key AS sender_device_key,
                target.device_key AS target_device_key,
                b.height AS block_height
            FROM network_transactions AS nt
            JOIN simulation_networks AS sn
                ON sn.id = nt.simulation_network_id
            LEFT JOIN simulation_devices AS sender
                ON sender.id = nt.sender_device_id
            LEFT JOIN simulation_devices AS target
                ON target.id = nt.target_device_id
            LEFT JOIN blocks AS b
                ON b.id = nt.block_id
            WHERE {' AND '.join(clauses)}
            ORDER BY nt.submitted_at_ms DESC, nt.id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, limit, offset],
        )

    def get_blocks(
        self,
        simulation_id: int,
        *,
        network_slot: str | None = None,
        limit: int = 250,
        offset: int = 0,
    ) -> list[Record]:
        """Return paginated block details for a simulation."""
        if limit <= 0 or offset < 0:
            raise ValidationError(
                "Block limit must be positive and offset non-negative."
            )

        clauses = ["sn.simulation_id = ?"]
        parameters: list[Any] = [simulation_id]
        if network_slot is not None:
            clauses.append("sn.network_slot = ?")
            parameters.append(network_slot)

        return self.database.execute_query(
            f"""
            SELECT b.*, sn.network_slot
            FROM blocks AS b
            JOIN simulation_networks AS sn
                ON sn.id = b.simulation_network_id
            WHERE {' AND '.join(clauses)}
            ORDER BY b.height DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, limit, offset],
        )

    def _get_latest_device_states(
        self,
        simulation_network_id: int,
    ) -> list[Record]:
        return self.database.execute_query(
            """
            WITH latest AS (
                SELECT
                    simulation_device_id,
                    MAX(elapsed_ms) AS elapsed_ms
                FROM device_state_samples
                WHERE simulation_network_id = ?
                GROUP BY simulation_device_id
            )
            SELECT
                dss.*,
                sd.device_key,
                sd.display_name
            FROM latest
            JOIN device_state_samples AS dss
                ON dss.simulation_network_id = ?
                AND dss.simulation_device_id = latest.simulation_device_id
                AND dss.elapsed_ms = latest.elapsed_ms
            JOIN simulation_devices AS sd
                ON sd.id = dss.simulation_device_id
            ORDER BY sd.device_key
            """,
            (simulation_network_id, simulation_network_id),
        )

    @staticmethod
    def group_samples_by_network_slot(
        samples: Iterable[Mapping[str, Any]],
    ) -> dict[str, list[Record]]:
        """Group joined samples by their A/B slot for chart construction."""
        grouped: defaultdict[str, list[Record]] = defaultdict(list)
        for sample in samples:
            grouped[str(sample["network_slot"])].append(dict(sample))
        return dict(grouped)

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
