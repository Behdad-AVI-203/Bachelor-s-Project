"""Overview dashboard for saved configurations and simulation results."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pandas as pd
import streamlit as st

from src.ui.components import (
    empty_state,
    format_datetime,
    format_decimal,
    page_header,
)
from src.ui.services import get_database, list_environments, list_networks


def _load_dashboard_data() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    database = get_database()
    first_page = database.simulations.get_simulation_history(limit=1)
    total = int(first_page["total"])
    history = (
        database.simulations.get_simulation_history(
            limit=max(total, 1),
        )["items"]
        if total
        else []
    )
    return history, list_environments(database), list_networks(database)


page_header(
    "Dashboard",
    "A research overview of configurations and comparative simulation runs.",
    icon="dashboard",
)

try:
    history, environments, networks = _load_dashboard_data()
except Exception as exc:
    st.error(f"Failed to load dashboard data: {exc}")
    st.stop()

completed = [row for row in history if row["status"] == "completed"]
churn_values = [
    float(value)
    for row in completed
    for value in (
        row.get("network_a_churn_rate"),
        row.get("network_b_churn_rate"),
    )
    if value is not None
]

wins: Counter[str] = Counter()
for row in completed:
    winner = row.get("winner_slot")
    if winner == "A" and row.get("network_a_name"):
        wins[str(row["network_a_name"])] += 1
    elif winner == "B" and row.get("network_b_name"):
        wins[str(row["network_b_name"])] += 1

best_network = wins.most_common(1)[0][0] if wins else "Not available"
average_churn = sum(churn_values) / len(churn_values) if churn_values else None

metric_columns = st.columns(4)
metric_columns[0].metric(
    "Total simulations",
    len(history),
    help="All created, running, completed, and failed simulation runs.",
    border=True,
)
metric_columns[1].metric(
    "Completed runs",
    len(completed),
    border=True,
)
metric_columns[2].metric(
    "Average churn",
    format_decimal(average_churn, 2)
    if average_churn is not None
    else "—",
    help="Mean final churn across both networks in completed runs.",
    border=True,
)
metric_columns[3].metric(
    "Best network so far",
    best_network,
    help="Network configuration with the most comparison wins.",
    border=True,
)

st.subheader("Workspace")
workspace_columns = st.columns(3)
workspace_columns[0].metric(
    "Saved environments",
    len(environments),
    border=True,
)
workspace_columns[1].metric(
    "Saved networks",
    len(networks),
    border=True,
)
workspace_columns[2].metric(
    "Research readiness",
    "Ready" if environments and len(networks) >= 2 else "Setup needed",
    border=True,
)

st.subheader("Latest simulation")
if not history:
    empty_state(
        "No simulations yet",
        "Create an environment and two networks, then run a comparison.",
        icon="science",
    )
    if st.button(
        "Configure the platform",
        icon=":material/settings:",
        type="primary",
    ):
        st.switch_page("app_pages/environments.py")
    st.stop()

latest = history[0]
with st.container(border=True):
    heading_columns = st.columns([3, 1])
    with heading_columns[0]:
        st.subheader(latest["name"])
        st.caption(
            f"Run #{latest['id']} · {latest['environment_name']} · "
            f"{format_datetime(latest['created_at'])}"
        )
    with heading_columns[1]:
        st.badge(
            str(latest["status"]).title(),
            color=(
                "green"
                if latest["status"] == "completed"
                else "red"
                if latest["status"] == "failed"
                else "blue"
            ),
            icon=":material/check_circle:"
            if latest["status"] == "completed"
            else ":material/schedule:",
        )

    comparison_columns = st.columns(2)
    for column, slot in zip(comparison_columns, ("A", "B"), strict=True):
        prefix = f"network_{slot.lower()}"
        with column:
            st.markdown(f"**Network {slot}: {latest[f'{prefix}_name']}**")
            subcolumns = st.columns(3)
            subcolumns[0].metric(
                "Churn",
                format_decimal(latest.get(f"{prefix}_churn_rate"), 3),
            )
            subcolumns[1].metric(
                "Gini",
                format_decimal(latest.get(f"{prefix}_gini"), 3),
            )
            subcolumns[2].metric(
                "Transactions",
                latest.get(f"{prefix}_transactions") or 0,
            )

    action_columns = st.columns([1, 1, 4])
    with action_columns[0]:
        if st.button(
            "View results",
            icon=":material/query_stats:",
            type="primary",
            width="stretch",
        ):
            st.session_state.selected_simulation_id = latest["id"]
            st.switch_page("app_pages/results.py")
    with action_columns[1]:
        if st.button(
            "Run another",
            icon=":material/play_circle:",
            width="stretch",
        ):
            st.switch_page("app_pages/run_simulation.py")

st.subheader("Recent activity")
recent = pd.DataFrame(history[:8])
display = recent[
    [
        "id",
        "name",
        "environment_name",
        "network_a_name",
        "network_b_name",
        "status",
        "created_at",
    ]
].copy()
display["created_at"] = display["created_at"].map(format_datetime)
st.dataframe(
    display,
    hide_index=True,
    column_config={
        "id": st.column_config.NumberColumn("ID", format="%d"),
        "name": "Simulation",
        "environment_name": "Environment",
        "network_a_name": "Network A",
        "network_b_name": "Network B",
        "status": "Status",
        "created_at": "Created",
    },
)
