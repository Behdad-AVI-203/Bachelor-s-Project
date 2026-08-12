"""Connection management and generic CRUD operations for SQLite."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from typing import Any

from .errors import (
    DatabaseError,
    DatabaseIntegrityError,
    RecordNotFoundError,
    ValidationError,
)

Record = dict[str, Any]


class SQLiteDatabase:
    """Manage SQLite connections and provide safe, generic CRUD operations."""

    TABLES = frozenset(
        {
            "schema_migrations",
            "code_artifacts",
            "iot_environments",
            "device_behaviors",
            "environment_devices",
            "blockchain_network_configs",
            "incentive_mechanism_configs",
            "experiment_configs",
            "configuration_bundles",
            "simulation_runs",
            "simulation_networks",
            "simulation_devices",
            "simulation_events",
            "blocks",
            "network_transactions",
            "device_state_samples",
            "network_metric_samples",
            "network_run_summaries",
            "simulation_comparisons",
            "comparison_metrics",
        }
    )

    def __init__(
        self,
        database_path: str | Path,
        *,
        timeout: float = 30.0,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if timeout <= 0:
            raise ValidationError("Connection timeout must be positive.")
        if busy_timeout_ms < 0:
            raise ValidationError("Busy timeout cannot be negative.")

        requested_path = str(database_path)
        self._uses_memory_database = requested_path == ":memory:"
        self._uses_uri = self._uses_memory_database
        self.database_path = (
            f"file:simulation_platform_{id(self)}?mode=memory&cache=shared"
            if self._uses_memory_database
            else requested_path
        )
        self.timeout = timeout
        self.busy_timeout_ms = busy_timeout_ms
        self._column_cache: dict[str, frozenset[str]] = {}
        self._anchor_connection: sqlite3.Connection | None = None

        if not self._uses_memory_database:
            Path(self.database_path).expanduser().resolve().parent.mkdir(
                parents=True,
                exist_ok=True,
            )
        else:
            try:
                self._anchor_connection = self._open_connection()
            except sqlite3.Error as exc:
                raise DatabaseError(
                    "Unable to create the in-memory SQLite database."
                ) from exc

    def close(self) -> None:
        """Close resources retained for a shared in-memory database."""
        if self._anchor_connection is not None:
            self._anchor_connection.close()
            self._anchor_connection = None

    def __enter__(self) -> SQLiteDatabase:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def initialize(self) -> None:
        """Create the base schema and apply pending versioned migrations."""
        schema_path = Path(__file__).with_name("schema.sql")

        try:
            schema_sql = schema_path.read_text(encoding="utf-8")
            with self.connection() as connection:
                connection.executescript(schema_sql)
                connection.commit()
                self._apply_migrations(connection)
        except OSError as exc:
            raise DatabaseError(
                f"Unable to read database schema at {schema_path}."
            ) from exc
        except sqlite3.Error as exc:
            raise DatabaseError("Unable to initialize the database.") from exc

        self._column_cache.clear()

    def _apply_migrations(self, connection: sqlite3.Connection) -> None:
        """Apply each unapplied SQL migration exactly once."""
        migrations_path = Path(__file__).with_name("migrations")
        if not migrations_path.exists():
            return

        applied_versions = {
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        migrations: list[tuple[int, str, Path]] = []
        for migration_path in migrations_path.glob("*.sql"):
            match = re.fullmatch(r"(\d+)_([^.]+)\.sql", migration_path.name)
            if match is None:
                continue
            migrations.append(
                (
                    int(match.group(1)),
                    match.group(2).replace("_", " "),
                    migration_path,
                )
            )

        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            for version, description, migration_path in sorted(migrations):
                if version in applied_versions:
                    continue
                migration_sql = migration_path.read_text(encoding="utf-8")
                try:
                    connection.executescript(
                        f"BEGIN IMMEDIATE;\n{migration_sql}\n"
                    )
                    connection.execute(
                        """
                        INSERT INTO schema_migrations (version, description)
                        VALUES (?, ?)
                        """,
                        (version, description),
                    )
                    violations = connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                    if violations:
                        raise sqlite3.IntegrityError(
                            "A schema migration introduced foreign-key "
                            "violations."
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open a configured connection and guarantee that it is closed."""
        connection: sqlite3.Connection | None = None

        try:
            connection = self._open_connection()
            yield connection
        except sqlite3.Error as exc:
            raise DatabaseError("Unable to use the SQLite database.") from exc
        finally:
            if connection is not None:
                connection.close()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
            uri=self._uses_uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}"
        )
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run operations atomically, rolling back whenever an error occurs."""
        with self.connection() as connection:
            try:
                connection.execute("BEGIN")
                yield connection
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DatabaseIntegrityError(str(exc)) from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise DatabaseError("Database transaction failed.") from exc
            except Exception:
                connection.rollback()
                raise

    def create_record(self, table: str, values: Mapping[str, Any]) -> int:
        """Insert one record and return its SQLite row identifier."""
        with self.transaction() as connection:
            return self._insert(connection, table, values)

    def create_records(
        self,
        table: str,
        records: Iterable[Mapping[str, Any]],
        *,
        chunk_size: int = 1_000,
    ) -> int:
        """Insert records efficiently using chunked ``executemany`` calls."""
        with self.transaction() as connection:
            total = 0
            for batch in self._iter_batches(records, chunk_size):
                total += self._insert_many(
                    connection,
                    table,
                    batch,
                    chunk_size=chunk_size,
                )
            return total

    def upsert_records(
        self,
        table: str,
        records: Iterable[Mapping[str, Any]],
        *,
        conflict_columns: Sequence[str],
        update_columns: Sequence[str] | None = None,
        chunk_size: int = 1_000,
    ) -> int:
        """Insert or update records using a declared SQLite conflict target."""
        with self.transaction() as connection:
            total = 0
            for batch in self._iter_batches(records, chunk_size):
                total += self._upsert_many(
                    connection,
                    table,
                    batch,
                    conflict_columns=conflict_columns,
                    update_columns=update_columns,
                    chunk_size=chunk_size,
                )
            return total

    def get_record(
        self,
        table: str,
        filters: Mapping[str, Any],
        *,
        required: bool = True,
    ) -> Record | None:
        """Return the first matching record."""
        records = self.list_records(table, filters=filters, limit=1)
        if records:
            return records[0]
        if required:
            raise RecordNotFoundError(
                f"No record found in {table} for filters {dict(filters)}."
            )
        return None

    def list_records(
        self,
        table: str,
        *,
        filters: Mapping[str, Any] | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Record]:
        """Return records using equality filters and validated ordering."""
        self._validate_table(table)
        filters = filters or {}

        with self.connection() as connection:
            columns = self._get_columns(connection, table)
            self._validate_columns(table, filters, columns)

            sql = f"SELECT * FROM {table}"
            parameters: list[Any] = []

            if filters:
                clauses = []
                for column, value in filters.items():
                    if value is None:
                        clauses.append(f"{column} IS NULL")
                    else:
                        clauses.append(f"{column} = ?")
                        parameters.append(self._normalize_value(column, value))
                sql += f" WHERE {' AND '.join(clauses)}"

            if order_by is not None:
                self._validate_columns(table, [order_by], columns)
                direction = "DESC" if descending else "ASC"
                sql += f" ORDER BY {order_by} {direction}"

            if limit is not None:
                if limit < 0 or offset < 0:
                    raise ValidationError(
                        "Limit and offset must be non-negative."
                    )
                sql += " LIMIT ? OFFSET ?"
                parameters.extend([limit, offset])
            elif offset:
                raise ValidationError("Offset requires a limit.")

            try:
                rows = connection.execute(sql, parameters).fetchall()
            except sqlite3.Error as exc:
                raise DatabaseError(
                    f"Unable to read records from {table}."
                ) from exc

        return [self._row_to_dict(row) for row in rows]

    def update_records(
        self,
        table: str,
        values: Mapping[str, Any],
        filters: Mapping[str, Any],
    ) -> int:
        """Update matching records and return the affected row count."""
        if not values:
            raise ValidationError("At least one value must be updated.")
        if not filters:
            raise ValidationError("Update operations require filters.")

        with self.transaction() as connection:
            columns = self._get_columns(connection, table)
            self._validate_columns(table, values, columns)
            self._validate_columns(table, filters, columns)

            assignments = ", ".join(f"{column} = ?" for column in values)
            parameters = [
                self._normalize_value(column, value)
                for column, value in values.items()
            ]
            clauses, filter_parameters = self._build_filter_clause(filters)
            parameters.extend(filter_parameters)

            cursor = connection.execute(
                f"UPDATE {table} SET {assignments} WHERE {clauses}",
                parameters,
            )
            return cursor.rowcount

    def delete_records(
        self,
        table: str,
        filters: Mapping[str, Any],
    ) -> int:
        """Delete matching records and return the affected row count."""
        if not filters:
            raise ValidationError("Delete operations require filters.")

        with self.transaction() as connection:
            columns = self._get_columns(connection, table)
            self._validate_columns(table, filters, columns)
            clauses, parameters = self._build_filter_clause(filters)
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE {clauses}",
                parameters,
            )
            return cursor.rowcount

    def execute_query(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> list[Record]:
        """Execute a parameterized read query used by repository classes."""
        if not sql.lstrip().upper().startswith(("SELECT", "WITH", "PRAGMA")):
            raise ValidationError("execute_query only accepts read queries.")

        with self.connection() as connection:
            try:
                rows = connection.execute(sql, parameters).fetchall()
            except sqlite3.Error as exc:
                raise DatabaseError("Database query failed.") from exc

        return [self._row_to_dict(row) for row in rows]

    def _insert(
        self,
        connection: sqlite3.Connection,
        table: str,
        values: Mapping[str, Any],
    ) -> int:
        self._validate_table(table)
        if not values:
            raise ValidationError("Insert values cannot be empty.")

        columns = self._get_columns(connection, table)
        self._validate_columns(table, values, columns)
        names = list(values)
        placeholders = ", ".join("?" for _ in names)
        parameters = [
            self._normalize_value(column, values[column]) for column in names
        ]

        cursor = connection.execute(
            f"""
            INSERT INTO {table} ({", ".join(names)})
            VALUES ({placeholders})
            """,
            parameters,
        )
        return int(cursor.lastrowid)

    def _insert_many(
        self,
        connection: sqlite3.Connection,
        table: str,
        records: Sequence[Mapping[str, Any]],
        *,
        chunk_size: int = 1_000,
    ) -> int:
        self._validate_chunk_size(chunk_size)
        columns = self._validate_batch_records(connection, table, records)
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders})"
        )

        for start in range(0, len(records), chunk_size):
            batch = records[start : start + chunk_size]
            parameters = [
                tuple(
                    self._normalize_value(column, record[column])
                    for column in columns
                )
                for record in batch
            ]
            connection.executemany(sql, parameters)

        return len(records)

    def _upsert_many(
        self,
        connection: sqlite3.Connection,
        table: str,
        records: Sequence[Mapping[str, Any]],
        *,
        conflict_columns: Sequence[str],
        update_columns: Sequence[str] | None = None,
        chunk_size: int = 1_000,
    ) -> int:
        self._validate_chunk_size(chunk_size)
        columns = self._validate_batch_records(connection, table, records)
        table_columns = self._get_columns(connection, table)
        self._validate_columns(table, conflict_columns, table_columns)

        if update_columns is None:
            update_columns = [
                column
                for column in columns
                if column not in conflict_columns
            ]
        self._validate_columns(table, update_columns, table_columns)

        placeholders = ", ".join("?" for _ in columns)
        conflict_target = ", ".join(conflict_columns)
        if update_columns:
            assignments = ", ".join(
                f"{column} = excluded.{column}" for column in update_columns
            )
            conflict_action = f"DO UPDATE SET {assignments}"
        else:
            conflict_action = "DO NOTHING"

        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_target}) {conflict_action}"
        )

        for start in range(0, len(records), chunk_size):
            batch = records[start : start + chunk_size]
            parameters = [
                tuple(
                    self._normalize_value(column, record[column])
                    for column in columns
                )
                for record in batch
            ]
            connection.executemany(sql, parameters)

        return len(records)

    def _validate_batch_records(
        self,
        connection: sqlite3.Connection,
        table: str,
        records: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        self._validate_table(table)
        if not records:
            raise ValidationError("Batch records cannot be empty.")

        columns = list(records[0])
        if not columns:
            raise ValidationError("Batch records cannot be empty.")

        expected_columns = set(columns)
        for record in records[1:]:
            if set(record) != expected_columns:
                raise ValidationError(
                    "Every record in a batch must contain identical columns."
                )

        table_columns = self._get_columns(connection, table)
        self._validate_columns(table, columns, table_columns)
        return columns

    def _get_columns(
        self,
        connection: sqlite3.Connection,
        table: str,
    ) -> frozenset[str]:
        self._validate_table(table)
        cached = self._column_cache.get(table)
        if cached is not None:
            return cached

        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        columns = frozenset(row["name"] for row in rows)
        if not columns:
            raise ValidationError(
                f"Table {table} is unavailable. Initialize the database first."
            )

        self._column_cache[table] = columns
        return columns

    def _validate_table(self, table: str) -> None:
        if table not in self.TABLES:
            raise ValidationError(f"Unsupported database table: {table}.")

    @staticmethod
    def _validate_columns(
        table: str,
        supplied: Mapping[str, Any] | Iterable[str],
        available: frozenset[str],
    ) -> None:
        supplied_columns = set(supplied)
        invalid = supplied_columns - available
        if invalid:
            invalid_names = ", ".join(sorted(invalid))
            raise ValidationError(
                f"Invalid columns for {table}: {invalid_names}."
            )

    @staticmethod
    def _validate_chunk_size(chunk_size: int) -> None:
        if chunk_size <= 0:
            raise ValidationError("Chunk size must be greater than zero.")

    @classmethod
    def _iter_batches(
        cls,
        records: Iterable[Mapping[str, Any]],
        chunk_size: int,
    ) -> Iterator[list[Mapping[str, Any]]]:
        cls._validate_chunk_size(chunk_size)
        iterator = iter(records)
        while batch := list(islice(iterator, chunk_size)):
            yield batch

    def _build_filter_clause(
        self,
        filters: Mapping[str, Any],
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []

        for column, value in filters.items():
            if value is None:
                clauses.append(f"{column} IS NULL")
            else:
                clauses.append(f"{column} = ?")
                parameters.append(self._normalize_value(column, value))

        return " AND ".join(clauses), parameters

    @staticmethod
    def _normalize_value(column: str, value: Any) -> Any:
        if column.endswith("_json") and not isinstance(value, str):
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        if isinstance(value, bool):
            return int(value)
        return value

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Record:
        record = dict(row)
        for column, value in record.items():
            if (
                column.endswith("_json")
                and isinstance(value, str)
                and value
            ):
                try:
                    record[column] = json.loads(value)
                except json.JSONDecodeError:
                    pass
        return record
