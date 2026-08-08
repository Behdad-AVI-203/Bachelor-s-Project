"""Simulation history, deletion, and CSV export."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.ui.components import empty_state, format_datetime, page_header
from src.ui.exports import dataframe_to_csv
from src.ui.services import delete_simulation, get_database


def _history_frame(rows: list[dict]) -> pd.DataFrame:
    dataframe = pd.DataFrame(rows)
    if dataframe.empty:
        return dataframe
    dataframe["created_at"] = dataframe["created_at"].map(format_datetime)
    dataframe["average_churn_rate"] = dataframe[
        ["network_a_churn_rate", "network_b_churn_rate"]
    ].mean(axis=1)
    dataframe["winner"] = dataframe.apply(
        lambda row: (
            row["network_a_name"]
            if row["winner_slot"] == "A"
            else row["network_b_name"]
            if row["winner_slot"] == "B"
            else "Tie"
            if row["winner_slot"] == "TIE"
            else "—"
        ),
        axis=1,
    )
    return dataframe


@st.dialog("Delete simulation?", icon=":material/delete:")
def confirm_delete(simulation_id: int, simulation_name: str) -> None:
    st.warning(
        f"Simulation #{simulation_id} (`{simulation_name}`) and all of its "
        "events, blocks, transactions, states, and metrics will be removed."
    )
    confirmation = st.text_input(
        "Type DELETE to confirm",
        key=f"delete_simulation_confirmation_{simulation_id}",
    )
    if st.button(
        "Delete simulation",
        type="primary",
        icon=":material/delete_forever:",
        disabled=confirmation != "DELETE",
        width="stretch",
    ):
        try:
            delete_simulation(get_database(), simulation_id)
        except Exception as exc:
            st.error(f"Failed to delete simulation: {exc}")
        else:
            if st.session_state.get("selected_simulation_id") == simulation_id:
                st.session_state.selected_simulation_id = None
            st.toast("Simulation deleted.", icon=":material/check_circle:")
            st.rerun()


database = get_database()
page_header(
    "History",
    "Review, export, and manage every persisted simulation run.",
    icon="history",
)

status_filter = st.selectbox(
    "Status",
    ["All", "completed", "running", "failed", "cancelled", "created"],
    width=240,
)

try:
    selected_status = None if status_filter == "All" else status_filter
    first_page = database.simulations.get_simulation_history(
        status=selected_status,
        limit=1,
    )
    total = int(first_page["total"])
    rows = (
        database.simulations.get_simulation_history(
            status=selected_status,
            limit=max(total, 1),
        )["items"]
        if total
        else []
    )
    all_first_page = database.simulations.get_simulation_history(limit=1)
    all_total = int(all_first_page["total"])
    all_rows = (
        database.simulations.get_simulation_history(
            limit=max(all_total, 1),
        )["items"]
        if all_total
        else []
    )
except Exception as exc:
    st.error(f"Failed to load simulation history: {exc}")
    st.stop()

if not rows:
    empty_state(
        "No simulations found",
        "Run a comparison or change the status filter.",
        icon="history",
    )
    st.stop()

history = _history_frame(rows)
full_history = _history_frame(all_rows)
display_columns = [
    "id",
    "name",
    "environment_name",
    "network_a_name",
    "network_b_name",
    "created_at",
    "status",
    "average_churn_rate",
    "winner",
    "score_a",
    "score_b",
]
selection = st.dataframe(
    history[display_columns],
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key="history_table",
    column_config={
        "id": st.column_config.NumberColumn("ID", format="%d"),
        "name": st.column_config.TextColumn("Simulation", pinned=True),
        "environment_name": "Environment",
        "network_a_name": "Network A",
        "network_b_name": "Network B",
        "created_at": "Created",
        "status": "Status",
        "average_churn_rate": st.column_config.NumberColumn(
            "Average churn",
            format="%.4f",
        ),
        "winner": "Winner",
        "score_a": st.column_config.NumberColumn(
            "Score A",
            format="%.2f",
        ),
        "score_b": st.column_config.NumberColumn(
            "Score B",
            format="%.2f",
        ),
    },
)

st.caption(f"{total:,} simulation records · Select one row for actions.")
action_columns = st.columns([1, 1, 1, 4])
with action_columns[0]:
    st.download_button(
        "Export history",
        data=dataframe_to_csv(full_history[display_columns]),
        file_name="simulation-history.csv",
        mime="text/csv",
        icon=":material/download:",
        width="stretch",
    )

selected_rows = selection.selection.rows
if selected_rows:
    selected = rows[selected_rows[0]]
    with action_columns[1]:
        if st.button(
            "View results",
            icon=":material/query_stats:",
            width="stretch",
            disabled=selected["status"] != "completed",
        ):
            st.session_state.selected_simulation_id = selected["id"]
            st.switch_page("app_pages/results.py")
    with action_columns[2]:
        if st.button(
            "Delete",
            icon=":material/delete:",
            width="stretch",
            disabled=selected["status"] == "running",
        ):
            confirm_delete(selected["id"], selected["name"])
