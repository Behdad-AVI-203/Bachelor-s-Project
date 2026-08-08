"""Streamlit AppTest coverage for complete researcher workflows."""

from __future__ import annotations

from pathlib import Path
from time import sleep

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from src.database import Database
from src.ui.exports import dataframe_to_csv, results_pdf
from src.ui.services import delete_simulation


CUSTOM_UI_REWARD = """def calculate_reward(context):
    return 2.0 * context["device"]["precision"]
"""


def _configure_ui_database(database_path: Path) -> None:
    import src.ui.config as ui_config
    import src.ui.services as ui_services

    ui_config.DATABASE_PATH = database_path
    ui_services.DATABASE_PATH = database_path
    ui_services.get_database.clear()
    st.cache_data.clear()


def _page_app(page: str, database_path: Path) -> AppTest:
    _configure_ui_database(database_path)
    source = (
        "from src.ui.state import initialize_session_state\n"
        "initialize_session_state()\n"
        f"exec(compile(open({page!r}, encoding='utf-8').read(), "
        f"{page!r}, 'exec'))\n"
    )
    return AppTest.from_string(source, default_timeout=60).run()


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _number_input(app: AppTest, label: str):
    return next(
        widget for widget in app.number_input if widget.label == label
    )


def _assert_no_exceptions(app: AppTest) -> None:
    assert not app.exception, [
        exception.message for exception in app.exception
    ]


@pytest.mark.ui
@pytest.mark.integration
def test_complete_ui_research_workflow(tmp_path):
    database_path = tmp_path / "ui-platform.sqlite3"

    environment_page = _page_app(
        "app_pages/environments.py",
        database_path,
    )
    environment_page.text_input[0].set_value("UI environment")
    environment_page.text_area[0].set_value(
        "Created through Streamlit AppTest."
    )
    _button(environment_page, "Save environment").click()
    environment_page.run()
    _assert_no_exceptions(environment_page)

    with Database(database_path) as database:
        database.initialize()
        environments = database.list_records("iot_environments")
        assert len(environments) == 1
        environment = database.configurations.get_environment(
            environments[0]["id"]
        )
        assert len(environment["devices"]) == 10

    first_network_page = _page_app(
        "app_pages/networks.py",
        database_path,
    )
    first_network_page.text_input(
        key="create_network_name"
    ).set_value("Default PoW network")
    _button(first_network_page, "Save network").click()
    first_network_page.run()
    _assert_no_exceptions(first_network_page)

    custom_network_page = _page_app(
        "app_pages/networks.py",
        database_path,
    )
    custom_network_page.file_uploader[0].set_value(
        (
            "custom_reward.py",
            CUSTOM_UI_REWARD.encode("utf-8"),
            "text/x-python",
        )
    )
    custom_network_page.run()
    custom_network_page.text_input(
        key="create_network_name"
    ).set_value("Custom reward network")
    _button(custom_network_page, "Save network").click()
    custom_network_page.run()
    _assert_no_exceptions(custom_network_page)

    with Database(database_path) as database:
        database.initialize()
        networks = database.list_records(
            "blockchain_network_configs",
            order_by="id",
        )
        assert [network["name"] for network in networks] == [
            "Default PoW network",
            "Custom reward network",
        ]
        custom = database.configurations.get_network_config(
            networks[1]["id"]
        )
        assert custom["code_artifacts"][0]["source_code"] == CUSTOM_UI_REWARD

    run_page = _page_app(
        "app_pages/run_simulation.py",
        database_path,
    )
    run_page.text_input[0].set_value("UI end-to-end simulation")
    _number_input(run_page, "Duration (seconds)").set_value(30.0)
    _number_input(run_page, "Poisson λ (tx/s)").set_value(3.0)
    _number_input(run_page, "Random seed (optional)").set_value(42)
    _button(run_page, "Run simulation").click()
    run_page.run()
    _assert_no_exceptions(run_page)

    for _ in range(100):
        if run_page.success:
            break
        sleep(0.05)
        run_page.run()
        _assert_no_exceptions(run_page)
    assert any(
        "completed" in message.value.lower()
        for message in run_page.success
    )

    with Database(database_path) as database:
        database.initialize()
        history = database.simulations.get_simulation_history(limit=10)
        assert history["total"] == 1
        simulation_id = history["items"][0]["id"]
        assert history["items"][0]["status"] == "completed"
        stored_results = database.simulations.get_simulation_results(
            simulation_id
        )
        assert len(stored_results["networks"]) == 2

    results_page = _page_app(
        "app_pages/results.py",
        database_path,
    )
    _assert_no_exceptions(results_page)
    plotly_charts = results_page.get("plotly_chart")
    assert len(plotly_charts) == 1
    assert len(results_page.dataframe) >= 2
    summary = results_page.dataframe[0].value
    assert summary["Network"].str.startswith(("A:", "B:")).all()
    assert set(button.label for button in results_page.download_button) == {
        "Download CSV",
        "Download PDF",
    }

    csv_bytes = dataframe_to_csv(summary)
    pdf_bytes = results_pdf(
        title="UI end-to-end simulation",
        simulation_id=simulation_id,
        rows=summary.fillna("—").to_dict("records"),
    )
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    assert pdf_bytes.startswith(b"%PDF-1.4")

    history_page = _page_app(
        "app_pages/history.py",
        database_path,
    )
    _assert_no_exceptions(history_page)
    assert "UI end-to-end simulation" in set(
        history_page.dataframe[0].value["name"]
    )
    assert history_page.download_button[0].label == "Export history"

    with Database(database_path) as database:
        database.initialize()
        delete_simulation(database, simulation_id)
        assert database.simulations.get_simulation_history(limit=10)[
            "total"
        ] == 0

    deleted_history_page = _page_app(
        "app_pages/history.py",
        database_path,
    )
    _assert_no_exceptions(deleted_history_page)
    assert any(
        "No simulations found" in subheader.value
        for subheader in deleted_history_page.subheader
    )


@pytest.mark.ui
def test_ui_reports_missing_required_environment_name(tmp_path):
    page = _page_app(
        "app_pages/environments.py",
        tmp_path / "missing-name.sqlite3",
    )
    _button(page, "Save environment").click()
    page.run()

    _assert_no_exceptions(page)
    assert any(
        "cannot be empty" in error.value.lower()
        for error in page.error
    )


@pytest.mark.ui
def test_ui_rejects_invalid_python_plugin(tmp_path):
    page = _page_app(
        "app_pages/networks.py",
        tmp_path / "invalid-plugin.sqlite3",
    )
    invalid_source = (
        "import os\n\n"
        "def calculate_reward(context):\n"
        "    return 1\n"
    )
    page.file_uploader[0].set_value(
        (
            "invalid_reward.py",
            invalid_source.encode("utf-8"),
            "text/x-python",
        )
    )
    page.run()
    page.text_input(key="create_network_name").set_value(
        "Invalid plugin network"
    )
    _button(page, "Save network").click()
    page.run()

    _assert_no_exceptions(page)
    assert any(
        "imports are disabled" in error.value.lower()
        for error in page.error
    )


@pytest.mark.ui
def test_main_app_handles_unavailable_database(tmp_path):
    unavailable_path = tmp_path / "database-directory"
    unavailable_path.mkdir()
    _configure_ui_database(unavailable_path)

    app = AppTest.from_file(
        "streamlit_app.py",
        default_timeout=30,
    ).run()

    _assert_no_exceptions(app)
    assert any(
        "failed to initialize" in error.value.lower()
        for error in app.error
    )
