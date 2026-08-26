"""Overview dashboard for incentive-comparison experiments."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from src.ui.components import (
    empty_state,
    format_datetime,
    format_decimal,
    page_header,
)
from src.ui.services import get_database, get_dashboard_view


def _latest_incentive_result(
    data: dict[str, Any],
    simulation_id: int,
) -> dict[str, Any] | None:
    return next(
        (
            result
            for result in data["incentive_results"]
            if int(result["id"]) == int(simulation_id)
        ),
        None,
    )


def _render_latest_incentive(
    data: dict[str, Any],
    latest: dict[str, Any],
) -> None:
    summary = latest["experiment_summary"]
    result = _latest_incentive_result(data, int(latest["id"]))
    with st.container(border=True):
        heading_columns = st.columns([3, 1])
        with heading_columns[0]:
            st.subheader(latest["name"])
            st.caption(
                f"Run #{latest['id']} · "
                f"{latest['environment_name']} · "
                f"{format_datetime(latest['created_at'])}"
            )
            st.caption(
                f"Shared network: "
                f"{summary.get('shared_network_name') or '—'}"
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
        for column, slot in zip(
            comparison_columns,
            ("A", "B"),
            strict=True,
        ):
            name = summary.get(
                "incentive_a_name"
                if slot == "A"
                else "incentive_b_name"
            ) or f"Incentive {slot}"
            metrics = (result or {}).get("arms", {}).get(slot, {})
            with column:
                st.markdown(f"**Incentive {slot}: {name}**")
                subcolumns = st.columns(3)
                subcolumns[0].metric(
                    "Retention",
                    format_decimal(metrics.get("retention_rate"), 3),
                )
                subcolumns[1].metric(
                    "Participation",
                    format_decimal(metrics.get("participation_rate"), 3),
                )
                subcolumns[2].metric(
                    "Useful contribution",
                    format_decimal(
                        metrics.get("useful_contribution_count"),
                        2,
                    ),
                )


def _render_latest_legacy(latest: dict[str, Any]) -> None:
    with st.container(border=True):
        st.subheader(latest["name"])
        st.caption(
            f"Legacy run #{latest['id']} · "
            f"{latest['environment_name']} · "
            f"{format_datetime(latest['created_at'])}"
        )
        comparison_columns = st.columns(2)
        for column, slot in zip(
            comparison_columns,
            ("A", "B"),
            strict=True,
        ):
            prefix = f"network_{slot.lower()}"
            with column:
                st.markdown(
                    f"**Network {slot}: "
                    f"{latest.get(f'{prefix}_name') or '—'}**"
                )
                subcolumns = st.columns(3)
                subcolumns[0].metric(
                    "Churn",
                    format_decimal(
                        latest.get(f"{prefix}_churn_rate"),
                        3,
                    ),
                )
                subcolumns[1].metric(
                    "Gini",
                    format_decimal(latest.get(f"{prefix}_gini"), 3),
                )
                subcolumns[2].metric(
                    "Transactions",
                    latest.get(f"{prefix}_transactions") or 0,
                )


def _recent_incentive_frame(
    rows: list[dict[str, Any]],
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": row["id"],
                "name": row["name"],
                "environment": row["environment_name"],
                "Incentive A": row.get("incentive_a_name") or "—",
                "Incentive B": row.get("incentive_b_name") or "—",
                "status": row["status"],
                "created_at": format_datetime(row["created_at"]),
            }
            for row in rows
        ]
    )


def _recent_legacy_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": row["id"],
                "name": row["name"],
                "environment": row["environment_name"],
                "Network A": row.get("network_a_name") or "—",
                "Network B": row.get("network_b_name") or "—",
                "status": row["status"],
                "created_at": format_datetime(row["created_at"]),
            }
            for row in rows
        ]
    )


data = None
page_header(
    "Dashboard",
    "Evaluate incentive mechanisms under identical IoT and network conditions.",
    icon="dashboard",
)

try:
    data = get_dashboard_view(get_database())
except Exception as exc:
    st.error(f"Failed to load dashboard data: {exc}")
    st.stop()

incentive_summary = data["incentive_summary"]
legacy_summary = data["legacy_summary"]
metric_columns = st.columns(4)
metric_columns[0].metric(
    "Incentive experiments",
    incentive_summary["run_count"],
    help="All runs using one shared network and two incentive mechanisms.",
    border=True,
)
metric_columns[1].metric(
    "Completed incentive runs",
    incentive_summary["completed_count"],
    border=True,
)
metric_columns[2].metric(
    "Average retention",
    format_decimal(
        incentive_summary["average_retention_rate"],
        3,
    ),
    help="Mean final retention across both incentive arms.",
    border=True,
)
metric_columns[3].metric(
    "Average participation",
    format_decimal(
        incentive_summary["average_participation_rate"],
        3,
    ),
    help="Mean opportunity participation across both incentive arms.",
    border=True,
)

st.subheader("Incentive effectiveness")
effectiveness_columns = st.columns(4)
effectiveness_columns[0].metric(
    "Best-performing incentive",
    incentive_summary["best_incentive"],
    border=True,
)
effectiveness_columns[1].metric(
    "Useful contributions",
    format_decimal(
        incentive_summary["useful_contribution_count"],
        2,
    ),
    border=True,
)
effectiveness_columns[2].metric(
    "Average useful contribution",
    format_decimal(
        incentive_summary["average_useful_contribution_count"],
        2,
    ),
    border=True,
)
effectiveness_columns[3].metric(
    "Avg. incentive cost / contribution",
    format_decimal(
        incentive_summary["average_cost_per_useful_contribution"],
        3,
    ),
    help="Lower values indicate better incentive efficiency.",
    border=True,
)

st.subheader("Workspace")
workspace_columns = st.columns(4)
workspace_columns[0].metric(
    "Saved environments",
    len(data["environments"]),
    border=True,
)
workspace_columns[1].metric(
    "Saved network models",
    len(data["networks"]),
    border=True,
)
workspace_columns[2].metric(
    "Saved incentives",
    len(data["incentives"]),
    border=True,
)
workspace_columns[3].metric(
    "Research readiness",
    (
        "Ready"
        if data["environments"]
        and data["networks"]
        and len(data["incentives"]) >= 2
        else "Setup needed"
    ),
    border=True,
)

st.subheader("Latest simulation")
latest = data["latest"]
if latest is None:
    empty_state(
        "No simulations yet",
        "Create an environment, network model, and two incentives.",
        icon="science",
    )
    if st.button(
        "Configure the platform",
        icon=":material/settings:",
        type="primary",
    ):
        st.switch_page("app_pages/environments.py")
    st.stop()

if latest.get("is_legacy"):
    _render_latest_legacy(latest)
else:
    _render_latest_incentive(data, latest)

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

st.subheader("Recent incentive experiments")
recent_incentives = _recent_incentive_frame(
    data["incentive_runs"][:8]
)
if recent_incentives.empty:
    st.info("No incentive-comparison experiments have been run yet.")
else:
    st.dataframe(
        recent_incentives,
        hide_index=True,
        column_config={
            "id": st.column_config.NumberColumn("ID", format="%d"),
            "name": "Simulation",
            "environment": "Environment",
            "Incentive A": "Incentive A",
            "Incentive B": "Incentive B",
            "status": "Status",
            "created_at": "Created",
        },
    )

st.subheader("Legacy network comparisons")
recent_legacy = _recent_legacy_frame(data["legacy_runs"][:8])
if recent_legacy.empty:
    st.info("No legacy network-comparison runs are present.")
else:
    st.dataframe(
        recent_legacy,
        hide_index=True,
        column_config={
            "id": st.column_config.NumberColumn("ID", format="%d"),
            "name": "Simulation",
            "environment": "Environment",
            "Network A": "Network A",
            "Network B": "Network B",
            "status": "Status",
            "created_at": "Created",
        },
    )

st.caption(
    f"Legacy runs: {legacy_summary['run_count']:,} · "
    f"Completed legacy runs: {legacy_summary['completed_count']:,} · "
    f"Legacy average churn: "
    f"{format_decimal(legacy_summary['average_churn_rate'], 3)}"
)
