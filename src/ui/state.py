"""Central initialization for per-browser Streamlit session state."""

from __future__ import annotations

import streamlit as st


def initialize_session_state() -> None:
    """Initialize state shared across multiple pages."""
    defaults = {
        "selected_simulation_id": None,
        "simulation_job_future": None,
        "simulation_job_queue": None,
        "simulation_job_updates": [],
        "simulation_job_result": None,
        "simulation_job_error": None,
        "simulation_job_experiment_id": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
