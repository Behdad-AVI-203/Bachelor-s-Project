"""Configuration CRUD, JSON export, and JSON import operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from .connection import Record, SQLiteDatabase
from .errors import ConfigurationError, ValidationError

ConflictPolicy = Literal["error", "replace", "rename"]


class ConfigurationRepository:
    """Manage reusable platform configurations and portable JSON bundles."""

    SCHEMA_VERSION = 1

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def create_code_artifact(
        self,
        *,
        artifact_type: str,
        name: str,
        entrypoint: str,
        source_code: str,
        version: int = 1,
        original_filename: str | None = None,
        validation_status: str = "pending",
        validation_message: str | None = None,
    ) -> int:
        """Store uploaded Python source and its deterministic checksum."""
        return self.database.create_record(
            "code_artifacts",
            {
                "artifact_type": artifact_type,
                "name": name,
                "version": version,
                "original_filename": original_filename,
                "entrypoint": entrypoint,
                "source_code": source_code,
                "sha256": self._sha256_text(source_code),
                "validation_status": validation_status,
                "validation_message": validation_message,
            },
        )

    def create_environment(
        self,
        *,
        name: str,
        description: str | None = None,
        revision: int = 1,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        """Create a reusable IoT environment."""
        return self.database.create_record(
            "iot_environments",
            {
                "name": name,
                "description": description,
                "revision": revision,
                "metadata_json": dict(metadata or {}),
            },
        )

    def create_device_behavior(
        self,
        *,
        name: str,
        precision: float,
        execution_cost: float,
        data_rate: float,
        profit_expectation: float = 0,
        version: int = 1,
        profit_expectation_artifact_id: int | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> int:
        """Create a reusable device-behavior definition."""
        return self.database.create_record(
            "device_behaviors",
            {
                "name": name,
                "version": version,
                "precision": precision,
                "execution_cost": execution_cost,
                "data_rate": data_rate,
                "profit_expectation": profit_expectation,
                "profit_expectation_artifact_id": (
                    profit_expectation_artifact_id
                ),
                "parameters_json": dict(parameters or {}),
            },
        )

    def create_environment_device(
        self,
        *,
        environment_id: int,
        behavior_id: int,
        device_key: str,
        display_name: str | None = None,
        initial_balance: float = 0,
        enabled: bool = True,
        overrides: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        """Add one configured IoT device to an environment."""
        return self.database.create_record(
            "environment_devices",
            {
                "environment_id": environment_id,
                "behavior_id": behavior_id,
                "device_key": device_key,
                "display_name": display_name,
                "initial_balance": initial_balance,
                "enabled": enabled,
                "overrides_json": dict(overrides or {}),
                "metadata_json": dict(metadata or {}),
            },
        )

    def create_network_config(
        self,
        *,
        name: str,
        version: int = 1,
        description: str | None = None,
        consensus_type: str = "pow",
        pow_difficulty: int = 2,
        max_transactions_per_block: int = 30,
        target_block_time_ms: int | None = None,
        transaction_fee_rate: float = 0,
        reward_artifact_id: int | None = None,
        blockchain_logic_artifact_id: int | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> int:
        """Create a blockchain-network configuration."""
        return self.database.create_record(
            "blockchain_network_configs",
            {
                "name": name,
                "version": version,
                "description": description,
                "consensus_type": consensus_type,
                "pow_difficulty": pow_difficulty,
                "max_transactions_per_block": max_transactions_per_block,
                "target_block_time_ms": target_block_time_ms,
                "transaction_fee_rate": transaction_fee_rate,
                "reward_artifact_id": reward_artifact_id,
                "blockchain_logic_artifact_id": (
                    blockchain_logic_artifact_id
                ),
                "parameters_json": dict(parameters or {}),
            },
        )

    def create_experiment_config(
        self,
        *,
        name: str,
        environment_id: int,
        network_a_config_id: int,
        network_b_config_id: int,
        poisson_lambda: float,
        duration_seconds: float,
        sample_interval_ms: int,
        description: str | None = None,
        default_random_seed: int | None = None,
        traffic_mix: Mapping[str, Any] | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> int:
        """Save a complete reusable A/B experiment configuration."""
        return self.database.create_record(
            "experiment_configs",
            {
                "name": name,
                "description": description,
                "environment_id": environment_id,
                "network_a_config_id": network_a_config_id,
                "network_b_config_id": network_b_config_id,
                "poisson_lambda": poisson_lambda,
                "duration_seconds": duration_seconds,
                "sample_interval_ms": sample_interval_ms,
                "default_random_seed": default_random_seed,
                "traffic_mix_json": dict(traffic_mix or {}),
                "parameters_json": dict(parameters or {}),
            },
        )

    def get_environment(self, environment_id: int) -> Record:
        """Return an environment with its devices and behavior records."""
        environment = self.database.get_record(
            "iot_environments",
            {"id": environment_id},
        )
        devices = self.database.list_records(
            "environment_devices",
            filters={"environment_id": environment_id},
            order_by="device_key",
        )
        behavior_ids = sorted({device["behavior_id"] for device in devices})
        behaviors = [
            self.database.get_record(
                "device_behaviors",
                {"id": behavior_id},
            )
            for behavior_id in behavior_ids
        ]

        return {
            **environment,
            "devices": devices,
            "device_behaviors": behaviors,
        }

    def get_network_config(self, network_config_id: int) -> Record:
        """Return a network configuration and referenced Python artifacts."""
        network = self.database.get_record(
            "blockchain_network_configs",
            {"id": network_config_id},
        )
        artifacts = self._get_artifacts(
            {
                network.get("reward_artifact_id"),
                network.get("blockchain_logic_artifact_id"),
            }
        )
        return {**network, "code_artifacts": artifacts}

    def get_experiment_config(self, experiment_config_id: int) -> Record:
        """Return a saved experiment with its referenced configuration rows."""
        experiment = self.database.get_record(
            "experiment_configs",
            {"id": experiment_config_id},
        )
        return {
            **experiment,
            "environment": self.get_environment(
                experiment["environment_id"]
            ),
            "network_a": self.get_network_config(
                experiment["network_a_config_id"]
            ),
            "network_b": self.get_network_config(
                experiment["network_b_config_id"]
            ),
        }

    def export_configuration(
        self,
        bundle_type: str,
        entity_id: int,
    ) -> Record:
        """Build a portable configuration bundle from normalized records."""
        if bundle_type == "environment":
            return self._export_environment(entity_id)
        if bundle_type == "network":
            return self._export_network(entity_id)
        if bundle_type in {"experiment", "full"}:
            return self._export_experiment(entity_id, bundle_type=bundle_type)
        raise ValidationError(f"Unsupported bundle type: {bundle_type}.")

    def export_configuration_json(
        self,
        bundle_type: str,
        entity_id: int,
        *,
        indent: int | None = 2,
    ) -> str:
        """Serialize a portable configuration bundle as JSON text."""
        bundle = self.export_configuration(bundle_type, entity_id)
        return json.dumps(
            bundle,
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
        )

    def save_configuration_bundle(
        self,
        *,
        name: str,
        bundle: Mapping[str, Any],
        source: str = "export",
    ) -> int:
        """Persist a versioned, checksummed JSON configuration package."""
        payload = self._canonical_json(bundle)
        return self.database.create_record(
            "configuration_bundles",
            {
                "name": name,
                "bundle_type": bundle.get("bundle_type"),
                "schema_version": bundle.get(
                    "schema_version",
                    self.SCHEMA_VERSION,
                ),
                "payload_json": payload,
                "sha256": self._sha256_text(payload),
                "source": source,
            },
        )

    def load_configuration_bundle(self, bundle_id: int) -> Record:
        """Load and checksum-verify a saved configuration package."""
        record = self.database.get_record(
            "configuration_bundles",
            {"id": bundle_id},
        )
        payload = record["payload_json"]
        canonical_payload = self._canonical_json(payload)

        if self._sha256_text(canonical_payload) != record["sha256"]:
            raise ConfigurationError(
                f"Configuration bundle {bundle_id} failed checksum validation."
            )

        return {**record, "payload": payload}

    def import_configuration_json(
        self,
        payload: str | Mapping[str, Any],
        *,
        conflict: ConflictPolicy = "error",
        save_bundle: bool = True,
        bundle_name: str | None = None,
    ) -> Record:
        """Import a portable bundle into normalized configuration tables."""
        if conflict not in {"error", "replace", "rename"}:
            raise ValidationError(
                "Conflict policy must be error, replace, or rename."
            )

        if isinstance(payload, str):
            try:
                bundle = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    "Configuration payload is not valid JSON."
                ) from exc
        else:
            bundle = dict(payload)

        self._validate_bundle(bundle)
        bundle_type = bundle["bundle_type"]

        with self.database.transaction() as connection:
            imported: Record
            if bundle_type == "environment":
                imported = {
                    "environment_id": self._import_environment_bundle(
                        connection,
                        bundle,
                        conflict,
                    )
                }
            elif bundle_type == "network":
                imported = {
                    "network_config_id": self._import_network_bundle(
                        connection,
                        bundle,
                        conflict,
                    )
                }
            elif bundle_type in {"experiment", "full"}:
                imported = self._import_experiment_bundle(
                    connection,
                    bundle,
                    conflict,
                )
            else:
                raise ConfigurationError(
                    f"Unsupported bundle type: {bundle_type}."
                )

            if save_bundle:
                canonical_payload = self._canonical_json(bundle)
                imported["bundle_id"] = self.database._insert(
                    connection,
                    "configuration_bundles",
                    {
                        "name": bundle_name
                        or self._default_bundle_name(bundle),
                        "bundle_type": bundle_type,
                        "schema_version": bundle["schema_version"],
                        "payload_json": canonical_payload,
                        "sha256": self._sha256_text(canonical_payload),
                        "source": "import",
                    },
                )

        return imported

    def _export_environment(self, environment_id: int) -> Record:
        environment = self.database.get_record(
            "iot_environments",
            {"id": environment_id},
        )
        devices = self.database.list_records(
            "environment_devices",
            filters={"environment_id": environment_id},
            order_by="device_key",
        )
        behavior_ids = sorted({device["behavior_id"] for device in devices})
        behaviors = [
            self.database.get_record(
                "device_behaviors",
                {"id": behavior_id},
            )
            for behavior_id in behavior_ids
        ]
        artifact_ids = {
            behavior.get("profit_expectation_artifact_id")
            for behavior in behaviors
        }

        return {
            "schema_version": self.SCHEMA_VERSION,
            "bundle_type": "environment",
            "exported_at": self._utc_now(),
            "environment": environment,
            "device_behaviors": behaviors,
            "devices": devices,
            "code_artifacts": self._get_artifacts(artifact_ids),
        }

    def _export_network(self, network_config_id: int) -> Record:
        network = self.database.get_record(
            "blockchain_network_configs",
            {"id": network_config_id},
        )
        artifact_ids = {
            network.get("reward_artifact_id"),
            network.get("blockchain_logic_artifact_id"),
        }

        return {
            "schema_version": self.SCHEMA_VERSION,
            "bundle_type": "network",
            "exported_at": self._utc_now(),
            "network": network,
            "code_artifacts": self._get_artifacts(artifact_ids),
        }

    def _export_experiment(
        self,
        experiment_config_id: int,
        *,
        bundle_type: str,
    ) -> Record:
        experiment = self.database.get_record(
            "experiment_configs",
            {"id": experiment_config_id},
        )

        return {
            "schema_version": self.SCHEMA_VERSION,
            "bundle_type": bundle_type,
            "exported_at": self._utc_now(),
            "experiment": experiment,
            "environment_bundle": self._export_environment(
                experiment["environment_id"]
            ),
            "network_a_bundle": self._export_network(
                experiment["network_a_config_id"]
            ),
            "network_b_bundle": self._export_network(
                experiment["network_b_config_id"]
            ),
        }

    def _import_experiment_bundle(
        self,
        connection: sqlite3.Connection,
        bundle: Mapping[str, Any],
        conflict: ConflictPolicy,
    ) -> Record:
        environment_id = self._import_environment_bundle(
            connection,
            self._require_mapping(bundle, "environment_bundle"),
            conflict,
        )
        network_a_id = self._import_network_bundle(
            connection,
            self._require_mapping(bundle, "network_a_bundle"),
            conflict,
        )
        network_b_id = self._import_network_bundle(
            connection,
            self._require_mapping(bundle, "network_b_bundle"),
            conflict,
        )

        experiment = self._clean_record(
            self._require_mapping(bundle, "experiment"),
            extra_fields={
                "environment_id",
                "network_a_config_id",
                "network_b_config_id",
            },
        )
        experiment["environment_id"] = environment_id
        experiment["network_a_config_id"] = network_a_id
        experiment["network_b_config_id"] = network_b_id

        experiment_id = self._insert_named_record(
            connection,
            "experiment_configs",
            experiment,
            key_columns=("name",),
            conflict=conflict,
        )

        return {
            "experiment_config_id": experiment_id,
            "environment_id": environment_id,
            "network_a_config_id": network_a_id,
            "network_b_config_id": network_b_id,
        }

    def _import_environment_bundle(
        self,
        connection: sqlite3.Connection,
        bundle: Mapping[str, Any],
        conflict: ConflictPolicy,
    ) -> int:
        artifact_map = self._import_artifacts(
            connection,
            bundle.get("code_artifacts", []),
            conflict,
        )
        behavior_map: dict[int, int] = {}

        for raw_behavior in bundle.get("device_behaviors", []):
            behavior = self._clean_record(
                raw_behavior,
                extra_fields={"profit_expectation_artifact_id"},
            )
            old_behavior_id = raw_behavior.get("id")
            old_artifact_id = raw_behavior.get(
                "profit_expectation_artifact_id"
            )
            behavior["profit_expectation_artifact_id"] = artifact_map.get(
                old_artifact_id
            )
            new_behavior_id = self._insert_named_record(
                connection,
                "device_behaviors",
                behavior,
                key_columns=("name", "version"),
                conflict=conflict,
            )
            if old_behavior_id is not None:
                behavior_map[int(old_behavior_id)] = new_behavior_id

        environment = self._clean_record(
            self._require_mapping(bundle, "environment")
        )
        existing = self._find_one(
            connection,
            "iot_environments",
            {"name": environment["name"]},
        )

        if existing and conflict == "error":
            raise ConfigurationError(
                f"Environment '{environment['name']}' already exists."
            )
        if existing and conflict == "rename":
            environment["name"] = self._unique_name(
                connection,
                "iot_environments",
                environment["name"],
            )
            existing = None

        if existing:
            environment_id = int(existing["id"])
            self._update_by_id(
                connection,
                "iot_environments",
                environment_id,
                environment,
            )
            connection.execute(
                "DELETE FROM environment_devices WHERE environment_id = ?",
                (environment_id,),
            )
        else:
            environment_id = self.database._insert(
                connection,
                "iot_environments",
                environment,
            )

        for raw_device in bundle.get("devices", []):
            old_behavior_id = raw_device.get("behavior_id")
            behavior_id = behavior_map.get(old_behavior_id)
            if behavior_id is None:
                raise ConfigurationError(
                    "An imported device references an unknown behavior."
                )

            device = self._clean_record(
                raw_device,
                extra_fields={"environment_id", "behavior_id"},
            )
            device["environment_id"] = environment_id
            device["behavior_id"] = behavior_id
            self.database._insert(connection, "environment_devices", device)

        return environment_id

    def _import_network_bundle(
        self,
        connection: sqlite3.Connection,
        bundle: Mapping[str, Any],
        conflict: ConflictPolicy,
    ) -> int:
        artifact_map = self._import_artifacts(
            connection,
            bundle.get("code_artifacts", []),
            conflict,
        )
        raw_network = self._require_mapping(bundle, "network")
        network = self._clean_record(
            raw_network,
            extra_fields={
                "reward_artifact_id",
                "blockchain_logic_artifact_id",
            },
        )
        network["reward_artifact_id"] = artifact_map.get(
            raw_network.get("reward_artifact_id")
        )
        network["blockchain_logic_artifact_id"] = artifact_map.get(
            raw_network.get("blockchain_logic_artifact_id")
        )

        return self._insert_named_record(
            connection,
            "blockchain_network_configs",
            network,
            key_columns=("name", "version"),
            conflict=conflict,
        )

    def _import_artifacts(
        self,
        connection: sqlite3.Connection,
        artifacts: Any,
        conflict: ConflictPolicy,
    ) -> dict[int, int]:
        artifact_map: dict[int, int] = {}

        for raw_artifact in artifacts:
            source_code = raw_artifact.get("source_code")
            expected_sha = raw_artifact.get("sha256")
            if not isinstance(source_code, str):
                raise ConfigurationError(
                    "Code artifacts must contain Python source text."
                )
            if self._sha256_text(source_code) != expected_sha:
                raise ConfigurationError(
                    f"Code artifact '{raw_artifact.get('name')}' "
                    "failed checksum validation."
                )

            exact = self._find_one(
                connection,
                "code_artifacts",
                {
                    "artifact_type": raw_artifact["artifact_type"],
                    "sha256": expected_sha,
                    "entrypoint": raw_artifact["entrypoint"],
                },
            )
            if exact:
                new_artifact_id = int(exact["id"])
            else:
                artifact = self._clean_record(raw_artifact)
                new_artifact_id = self._insert_named_record(
                    connection,
                    "code_artifacts",
                    artifact,
                    key_columns=("artifact_type", "name", "version"),
                    conflict=conflict,
                )

            old_artifact_id = raw_artifact.get("id")
            if old_artifact_id is not None:
                artifact_map[int(old_artifact_id)] = new_artifact_id

        return artifact_map

    def _insert_named_record(
        self,
        connection: sqlite3.Connection,
        table: str,
        values: Record,
        *,
        key_columns: tuple[str, ...],
        conflict: ConflictPolicy,
    ) -> int:
        keys = {column: values[column] for column in key_columns}
        existing = self._find_one(connection, table, keys)

        if not existing:
            return self.database._insert(connection, table, values)
        if conflict == "error":
            label = ", ".join(f"{key}={value!r}" for key, value in keys.items())
            raise ConfigurationError(
                f"A {table} record already exists with {label}."
            )
        if conflict == "rename":
            if "name" not in values:
                raise ConfigurationError(
                    f"Records in {table} cannot be renamed automatically."
                )
            values["name"] = self._unique_name(
                connection,
                table,
                values["name"],
                version=values.get("version"),
                artifact_type=values.get("artifact_type"),
            )
            return self.database._insert(connection, table, values)

        record_id = int(existing["id"])
        self._update_by_id(connection, table, record_id, values)
        return record_id

    def _update_by_id(
        self,
        connection: sqlite3.Connection,
        table: str,
        record_id: int,
        values: Mapping[str, Any],
    ) -> None:
        columns = self.database._get_columns(connection, table)
        self.database._validate_columns(table, values, columns)
        assignments = ", ".join(f"{column} = ?" for column in values)
        parameters = [
            self.database._normalize_value(column, value)
            for column, value in values.items()
        ]
        parameters.append(record_id)
        connection.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?",
            parameters,
        )

    def _find_one(
        self,
        connection: sqlite3.Connection,
        table: str,
        filters: Mapping[str, Any],
    ) -> Record | None:
        columns = self.database._get_columns(connection, table)
        self.database._validate_columns(table, filters, columns)
        clauses, parameters = self.database._build_filter_clause(filters)
        row = connection.execute(
            f"SELECT * FROM {table} WHERE {clauses} LIMIT 1",
            parameters,
        ).fetchone()
        return self.database._row_to_dict(row) if row else None

    def _unique_name(
        self,
        connection: sqlite3.Connection,
        table: str,
        original_name: str,
        *,
        version: int | None = None,
        artifact_type: str | None = None,
    ) -> str:
        suffix = 1
        while True:
            candidate = f"{original_name} (Imported {suffix})"
            filters: Record = {"name": candidate}
            if version is not None:
                filters["version"] = version
            if artifact_type is not None:
                filters["artifact_type"] = artifact_type
            if not self._find_one(connection, table, filters):
                return candidate
            suffix += 1

    def _get_artifacts(self, artifact_ids: set[Any]) -> list[Record]:
        artifacts: list[Record] = []
        for artifact_id in sorted(
            value for value in artifact_ids if value is not None
        ):
            artifact = self.database.get_record(
                "code_artifacts",
                {"id": artifact_id},
                required=False,
            )
            if artifact:
                artifacts.append(artifact)
        return artifacts

    def _validate_bundle(self, bundle: Mapping[str, Any]) -> None:
        if bundle.get("schema_version") != self.SCHEMA_VERSION:
            raise ConfigurationError(
                "Unsupported configuration schema version."
            )
        if bundle.get("bundle_type") not in {
            "environment",
            "network",
            "experiment",
            "full",
        }:
            raise ConfigurationError(
                "Configuration bundle type is missing or invalid."
            )

    @staticmethod
    def _clean_record(
        record: Mapping[str, Any],
        *,
        extra_fields: set[str] | None = None,
    ) -> Record:
        excluded = {"id", "created_at", "updated_at"}
        excluded.update(extra_fields or set())
        return {
            key: value
            for key, value in record.items()
            if key not in excluded
        }

    @staticmethod
    def _require_mapping(
        bundle: Mapping[str, Any],
        key: str,
    ) -> Mapping[str, Any]:
        value = bundle.get(key)
        if not isinstance(value, Mapping):
            raise ConfigurationError(
                f"Configuration bundle is missing '{key}'."
            )
        return value

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _default_bundle_name(bundle: Mapping[str, Any]) -> str:
        bundle_type = str(bundle["bundle_type"]).title()
        for key in ("experiment", "environment", "network"):
            value = bundle.get(key)
            if isinstance(value, Mapping) and value.get("name"):
                return f"{bundle_type}: {value['name']}"
        return f"{bundle_type} configuration"
