"""Public facade for the platform database layer."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .configurations import ConfigurationRepository
from .connection import Record, SQLiteDatabase
from .simulations import SimulationRepository


class DatabaseService:
    """Expose generic CRUD and domain repositories through one entry point."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        timeout: float = 30.0,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.store = SQLiteDatabase(
            database_path,
            timeout=timeout,
            busy_timeout_ms=busy_timeout_ms,
        )
        self.configurations = ConfigurationRepository(self.store)
        self.simulations = SimulationRepository(
            self.store,
            self.configurations,
        )

    def initialize(self) -> None:
        """Initialize the schema."""
        self.store.initialize()

    def close(self) -> None:
        """Close retained database resources."""
        self.store.close()

    def __enter__(self) -> DatabaseService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_record(self, table: str, values: Mapping[str, Any]) -> int:
        """Create a record in any supported table."""
        return self.store.create_record(table, values)

    def create_records(
        self,
        table: str,
        records: Iterable[Mapping[str, Any]],
        *,
        chunk_size: int = 1_000,
    ) -> int:
        """Create multiple records in any supported table."""
        return self.store.create_records(
            table,
            records,
            chunk_size=chunk_size,
        )

    def get_record(
        self,
        table: str,
        filters: Mapping[str, Any],
        *,
        required: bool = True,
    ) -> Record | None:
        """Read one record from any supported table."""
        return self.store.get_record(
            table,
            filters,
            required=required,
        )

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
        """Read records from any supported table."""
        return self.store.list_records(
            table,
            filters=filters,
            order_by=order_by,
            descending=descending,
            limit=limit,
            offset=offset,
        )

    def update_records(
        self,
        table: str,
        values: Mapping[str, Any],
        filters: Mapping[str, Any],
    ) -> int:
        """Update records in any supported table."""
        return self.store.update_records(table, values, filters)

    def delete_records(
        self,
        table: str,
        filters: Mapping[str, Any],
    ) -> int:
        """Delete records from any supported table."""
        return self.store.delete_records(table, filters)

    def upsert_records(
        self,
        table: str,
        records: Iterable[Mapping[str, Any]],
        *,
        conflict_columns: Sequence[str],
        update_columns: Sequence[str] | None = None,
        chunk_size: int = 1_000,
    ) -> int:
        """Upsert records in any supported table."""
        return self.store.upsert_records(
            table,
            records,
            conflict_columns=conflict_columns,
            update_columns=update_columns,
            chunk_size=chunk_size,
        )


Database = DatabaseService
