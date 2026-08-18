"""Tests for incentive-management UI services."""

from __future__ import annotations

import pytest

from src.core.errors import PluginValidationError
from src.ui.services import (
    delete_incentive,
    get_incentive_editor_data,
    list_incentives,
    save_incentive,
)


CUSTOM_INCENTIVE = """def evaluate(context):
    return {
        "reward_delta": 2.0,
        "penalty_delta": 0.1,
        "contribution_delta": 1.0,
        "reputation_delta": 0.5,
        "participation_signal": 0.2,
    }
"""


def test_save_and_load_custom_incentive(configured_database):
    database, _ = configured_database

    incentive_id = save_incentive(
        database,
        name="Custom participation",
        version=1,
        description="Test custom mechanism",
        implementation_type="custom",
        built_in_key=None,
        source_code=CUSTOM_INCENTIVE,
        entrypoint="evaluate",
        parameters={"bonus": 2},
        metadata={"owner": "test"},
    )

    rows = list_incentives(database)
    row = next(item for item in rows if item["id"] == incentive_id)
    editor = get_incentive_editor_data(database, incentive_id)

    assert row["implementation_type"] == "custom"
    assert row["artifact_name"] == "Custom participation incentive"
    assert editor["source_code"] == CUSTOM_INCENTIVE
    assert editor["parameters_json"] == {"bonus": 2}
    assert editor["metadata_json"] == {"owner": "test"}


def test_save_builtin_incentive_without_code(configured_database):
    database, _ = configured_database

    incentive_id = save_incentive(
        database,
        name="Built-in participation",
        version=1,
        description=None,
        implementation_type="built_in",
        built_in_key="participation_first",
        source_code="",
        entrypoint="evaluate",
        parameters={"participation_bonus": 0.25},
        metadata={},
    )

    editor = get_incentive_editor_data(database, incentive_id)
    assert editor["implementation_type"] == "built_in"
    assert editor["built_in_key"] == "participation_first"
    assert editor["code_artifact_id"] is None


def test_invalid_custom_incentive_is_rejected(configured_database):
    database, _ = configured_database

    with pytest.raises(PluginValidationError):
        save_incentive(
            database,
            name="Invalid custom",
            version=1,
            description=None,
            implementation_type="custom",
            built_in_key=None,
            source_code="def evaluate(:",
            entrypoint="evaluate",
            parameters={},
            metadata={},
        )


def test_unreferenced_incentive_can_be_deleted(configured_database):
    database, _ = configured_database
    incentive_id = save_incentive(
        database,
        name="Disposable incentive",
        version=1,
        description=None,
        implementation_type="built_in",
        built_in_key="default_reward",
        source_code="",
        entrypoint="evaluate",
        parameters={},
        metadata={},
    )

    delete_incentive(database, incentive_id)

    assert not database.list_records(
        "incentive_mechanism_configs",
        filters={"id": incentive_id},
    )
