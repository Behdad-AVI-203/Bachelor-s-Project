"""Streamlit entry point for the blockchain IoT simulation platform."""

from __future__ import annotations

import streamlit as st

from src.database import DatabaseError
from src.ui.components import render_app_sidebar
from src.ui.services import get_database
from src.ui.state import initialize_session_state

st.set_page_config(
    page_title="Blockchain IoT simulation platform",
    page_icon=":material/hub:",
    layout="wide",
    initial_sidebar_state="collapsed",
)

initialize_session_state()

try:
    database = get_database()
except DatabaseError as exc:
    st.error(
        f"Failed to initialize the platform database: {exc}",
        icon=":material/database_off:",
    )
    st.stop()

pages = [
    st.Page(
        "app_pages/dashboard.py",
        title="Dashboard",
        icon=":material/dashboard:",
        default=True,
    ),
    st.Page(
        "app_pages/environments.py",
        title="Environments",
        icon=":material/sensors:",
    ),
    st.Page(
        "app_pages/networks.py",
        title="Networks",
        icon=":material/account_tree:",
    ),
    st.Page(
        "app_pages/run_simulation.py",
        title="Run simulation",
        icon=":material/play_circle:",
    ),
    st.Page(
        "app_pages/results.py",
        title="Results",
        icon=":material/query_stats:",
    ),
    st.Page(
        "app_pages/history.py",
        title="History",
        icon=":material/history:",
    ),
]

render_app_sidebar(database)
navigation = st.navigation(pages, position="top")
navigation.run()
