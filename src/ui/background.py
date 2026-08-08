"""Background simulation execution for responsive Streamlit pages."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from queue import Empty, Queue
from typing import Any

import streamlit as st

from src.core import SimulationEngine, SimulationExecutionResult
from src.database import Database


@st.cache_resource
def get_simulation_executor() -> ThreadPoolExecutor:
    """Return the process-wide simulation worker pool."""
    return ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="simulation-worker",
    )


def submit_simulation(
    database_path: str,
    experiment_config_id: int,
    *,
    run_name: str,
    random_seed: int | None,
) -> tuple[Future[SimulationExecutionResult], Queue[Any]]:
    """Submit one simulation and return its future and progress queue."""
    progress_queue: Queue[Any] = Queue()
    future = get_simulation_executor().submit(
        _run_simulation,
        database_path,
        experiment_config_id,
        run_name,
        random_seed,
        progress_queue,
    )
    return future, progress_queue


def drain_progress(progress_queue: Queue[Any]) -> list[Any]:
    """Remove all currently available progress messages without blocking."""
    updates = []
    while True:
        try:
            updates.append(progress_queue.get_nowait())
        except Empty:
            return updates


def _run_simulation(
    database_path: str,
    experiment_config_id: int,
    run_name: str,
    random_seed: int | None,
    progress_queue: Queue[Any],
) -> SimulationExecutionResult:
    database = Database(database_path)
    database.initialize()
    try:
        return SimulationEngine(database).run_experiment(
            experiment_config_id,
            name=run_name,
            random_seed=random_seed,
            progress_callback=progress_queue.put,
        )
    finally:
        database.close()
