"""Reusable native Streamlit components and formatting helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import streamlit as st

from src.database import DatabaseService

from .config import APP_VERSION, DATABASE_PATH


def page_header(
    title: str,
    description: str,
    *,
    icon: str,
) -> None:
    """Render a consistent page title and supporting description."""
    st.title(f":material/{icon}: {title}")
    st.caption(description)


def render_app_sidebar(database: DatabaseService) -> None:
    """Render compact application and database information."""
    with st.sidebar:
        st.subheader(":material/hub: Research simulator")
        st.caption(
            "Compare blockchain incentive mechanisms under identical "
            "IoT traffic."
        )
        st.badge(
            "SQLite connected",
            icon=":material/check_circle:",
            color="green",
        )
        st.caption(f"Database: `{DATABASE_PATH.name}`")
        st.caption(f"Application version {APP_VERSION}")
        if st.button(
            "Check database",
            icon=":material/database:",
            width="stretch",
        ):
            try:
                result = database.store.execute_query(
                    "PRAGMA integrity_check"
                )
                if result and result[0].get("integrity_check") == "ok":
                    st.toast(
                        "Database integrity check passed.",
                        icon=":material/check_circle:",
                    )
                else:
                    st.warning("Database integrity check returned warnings.")
            except Exception as exc:
                st.error(f"Database check failed: {exc}")


def empty_state(
    title: str,
    message: str,
    *,
    icon: str = "inbox",
) -> None:
    """Render a lightweight empty-state card."""
    with st.container(border=True, horizontal_alignment="center"):
        st.subheader(f":material/{icon}: {title}")
        st.caption(message, text_alignment="center")


def format_datetime(value: Any) -> str:
    """Format stored ISO timestamps for tables and captions."""
    if not value:
        return "—"
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def format_decimal(value: Any, digits: int = 3) -> str:
    """Format nullable numeric values for compact UI display."""
    if value is None:
        return "—"
    return f"{float(value):,.{digits}f}"


def status_color(status: str) -> str:
    """Map simulation statuses to Streamlit badge colors."""
    return {
        "completed": "green",
        "running": "blue",
        "queued": "orange",
        "created": "gray",
        "failed": "red",
        "cancelled": "gray",
    }.get(status, "gray")
