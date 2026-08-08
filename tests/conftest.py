"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.database import Database

from .factories import create_platform_configuration


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    """Provide an initialized SQLite database for one test."""
    instance = Database(tmp_path / "platform.sqlite3")
    instance.initialize()
    yield instance
    instance.close()


@pytest.fixture
def configured_database(database):
    """Provide a database with a complete reusable A/B configuration."""
    identifiers = create_platform_configuration(database)
    return database, identifiers
