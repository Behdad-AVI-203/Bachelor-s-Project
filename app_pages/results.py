"""Detailed side-by-side simulation results and exports."""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.ui.components import (
    empty_state,
    format_datetime,
    page_header,
)
from src.ui.exports import dataframe_to_csv, results_pdf
from src.ui.services import get_database


COLORS = {"A": "#2563EB", "B": "#0F766E"}


def _metric_frame(
    results: dict[str, Any],
) -> pd.DataFrame:
    comparison = results.get("comparison") or {}
    rows = []
    for network in results["networks"]:
        slot = network["network_slot"]
        rows.append(
            {
                "Network": f"{slot}: {network['network_name']}",
                "Churn rate": network.get("final_churn_rate"),
                "Average balance": _final_average_balance(
                    results["latest_device_states"].get(slot, [])
                ),
                "Gini coefficient": network.get(
                    "final_gini_coefficient"
                ),
                "Balance variance": network.get(
                    "final_balance_variance"
                ),
                "Total transactions": network.get("total_transactions"),
                "Confirmed transactions": network.get(
                    "confirmed_transactions"
                ),
                "Total blocks": network.get("total_blocks"),
                "Throughput (tx/s)": network.get(
                    "average_throughput_tps"
                ),
                "Comparison score": comparison.get(
                    "score_a" if slot == "A" else "score_b"
                ),
            }
        )
    return pd.DataFrame(rows)


def _final_average_balance(states: list[dict[str, Any]]) -> float | None:
    balances = [
        float(state["balance"])
        for state in states
        if state.get("balance") is not None
    ]
    return sum(balances) / len(balances) if balances else None


def _average_balance_series(
    database: Any,
    networks: list[dict[str, Any]],
) -> pd.DataFrame:
    rows = []
    for network in networks:
        slot = network["network_slot"]
        states = database.simulations.get_device_states(network["id"])
        frame = pd.DataFrame(states)
        if frame.empty:
            continue
        grouped = frame.groupby("elapsed_ms", as_index=False)["balance"].mean()
        for record in grouped.to_dict("records"):
            rows.append(
                {
                    "Elapsed seconds": record["elapsed_ms"] / 1_000,
                    "Average balance": record["balance"],
                    "Network": (
                        f"{slot}: {network['network_name']}"
                    ),
                    "Slot": slot,
                }
            )
    return pd.DataFrame(rows)


def _network_series(
    results: dict[str, Any],
    value_key: str,
    value_label: str,
) -> pd.DataFrame:
    names = {
        network["network_slot"]: network["network_name"]
        for network in results["networks"]
    }
    rows = []
    for slot, samples in results["network_metrics"].items():
        for sample in samples:
            rows.append(
                {
                    "Elapsed seconds": sample["elapsed_ms"] / 1_000,
                    value_label: sample.get(value_key),
                    "Network": f"{slot}: {names[slot]}",
                    "Slot": slot,
                }
            )
    return pd.DataFrame(rows)


def _line_chart(
    dataframe: pd.DataFrame,
    *,
    value_column: str,
    title: str,
) -> None:
    figure = go.Figure()
    if not dataframe.empty:
        for slot, group in dataframe.groupby("Slot"):
            figure.add_trace(
                go.Scatter(
                    x=group["Elapsed seconds"],
                    y=group[value_column],
                    name=group["Network"].iloc[0],
                    mode="lines",
                    line={"color": COLORS.get(slot), "width": 2.5},
                    hovertemplate=(
                        "Time: %{x:.2f} s<br>"
                        f"{value_column}: %{{y:.4f}}<extra></extra>"
                    ),
                )
            )
    figure.update_layout(
        title=title,
        xaxis_title="Virtual time (seconds)",
        yaxis_title=value_column,
        hovermode="x unified",
        legend_title="Network",
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
    )
    st.plotly_chart(figure, width="stretch")


database = get_database()
page_header(
    "Results",
    "Inspect economic fairness, participation, and throughput side by side.",
    icon="query_stats",
)

try:
    first_page = database.simulations.get_simulation_history(
        status="completed",
        limit=1,
    )
    total = int(first_page["total"])
    completed_runs = (
        database.simulations.get_simulation_history(
            status="completed",
            limit=max(total, 1),
        )["items"]
        if total
        else []
    )
except Exception as exc:
    st.error(f"Failed to load completed simulations: {exc}")
    st.stop()

if not completed_runs:
    empty_state(
        "No completed results",
        "Run a simulation to generate comparison charts and exports.",
        icon="query_stats",
    )
    if st.button(
        "Run simulation",
        icon=":material/play_circle:",
        type="primary",
    ):
        st.switch_page("app_pages/run_simulation.py")
    st.stop()

run_by_id = {int(run["id"]): run for run in completed_runs}
selected_from_state = st.session_state.get("selected_simulation_id")
default_id = (
    int(selected_from_state)
    if selected_from_state in run_by_id
    else int(completed_runs[0]["id"])
)
selected_id = st.selectbox(
    "Simulation",
    options=list(run_by_id),
    index=list(run_by_id).index(default_id),
    format_func=lambda value: (
        f"#{value} — {run_by_id[value]['name']} "
        f"({format_datetime(run_by_id[value]['created_at'])})"
    ),
)
st.session_state.selected_simulation_id = selected_id

try:
    with st.spinner("Loading detailed results..."):
        results = database.simulations.get_simulation_results(selected_id)
        average_balance = _average_balance_series(
            database,
            results["networks"],
        )
except Exception as exc:
    st.error(f"Failed to load simulation results: {exc}")
    st.stop()

run = results["run"]
comparison = results.get("comparison") or {}
network_names = {
    network["network_slot"]: network["network_name"]
    for network in results["networks"]
}

with st.container(border=True):
    title_columns = st.columns([3, 2])
    with title_columns[0]:
        st.subheader(run["name"])
        st.caption(
            f"Simulation #{run['id']} · {format_datetime(run['completed_at'])} "
            f"· λ={run['poisson_lambda']}/s · "
            f"{run['duration_seconds']} virtual seconds"
        )
    with title_columns[1]:
        winner = comparison.get("winner_slot")
        winner_text = (
            "Tie"
            if winner == "TIE"
            else network_names.get(winner, "Not scored")
        )
        st.metric("Comparison winner", winner_text)

summary_frame = _metric_frame(results)
metric_columns = st.columns(4)
metric_columns[0].metric(
    "Network A score",
    comparison.get("score_a") or 0,
    border=True,
    format="%.2f",
)
metric_columns[1].metric(
    "Network B score",
    comparison.get("score_b") or 0,
    border=True,
    format="%.2f",
)
metric_columns[2].metric(
    "Shared random seed",
    run["random_seed"],
    border=True,
)
metric_columns[3].metric(
    "Sample interval",
    f"{run['sample_interval_ms']:,} ms",
    border=True,
)

chart_tabs = st.tabs(
    ["Balances", "Fairness", "Throughput"],
    on_change="rerun",
)
if chart_tabs[0].open:
    with chart_tabs[0]:
        _line_chart(
            average_balance,
            value_column="Average balance",
            title="Average device balance over time",
        )
if chart_tabs[1].open:
    with chart_tabs[1]:
        gini_series = _network_series(
            results,
            "gini_coefficient",
            "Gini coefficient",
        )
        _line_chart(
            gini_series,
            value_column="Gini coefficient",
            title="Gini coefficient over time",
        )
if chart_tabs[2].open:
    with chart_tabs[2]:
        throughput_series = _network_series(
            results,
            "throughput_tps",
            "Throughput (tx/s)",
        )
        _line_chart(
            throughput_series,
            value_column="Throughput (tx/s)",
            title="Confirmed transaction throughput over time",
        )

st.subheader("Final metrics")
st.dataframe(
    summary_frame,
    hide_index=True,
    column_config={
        "Network": st.column_config.TextColumn(pinned=True),
        "Churn rate": st.column_config.NumberColumn(format="%.4f"),
        "Average balance": st.column_config.NumberColumn(format="%.4f"),
        "Gini coefficient": st.column_config.NumberColumn(format="%.4f"),
        "Balance variance": st.column_config.NumberColumn(format="%.4f"),
        "Total transactions": st.column_config.NumberColumn(format="%d"),
        "Confirmed transactions": st.column_config.NumberColumn(format="%d"),
        "Total blocks": st.column_config.NumberColumn(format="%d"),
        "Throughput (tx/s)": st.column_config.NumberColumn(format="%.4f"),
        "Comparison score": st.column_config.NumberColumn(format="%.2f"),
    },
)

export_rows = summary_frame.fillna("—").to_dict("records")
export_columns = st.columns([1, 1, 4])
with export_columns[0]:
    st.download_button(
        "Download CSV",
        data=dataframe_to_csv(summary_frame),
        file_name=f"simulation-{selected_id}-results.csv",
        mime="text/csv",
        icon=":material/download:",
        width="stretch",
    )
with export_columns[1]:
    st.download_button(
        "Download PDF",
        data=results_pdf(
            title=run["name"],
            simulation_id=selected_id,
            rows=export_rows,
        ),
        file_name=f"simulation-{selected_id}-results.pdf",
        mime="application/pdf",
        icon=":material/picture_as_pdf:",
        width="stretch",
    )

st.subheader("Per-metric comparison")
comparison_frame = pd.DataFrame(results["comparison_metrics"])
if comparison_frame.empty:
    st.info("No comparison metrics were stored for this run.")
else:
    st.dataframe(
        comparison_frame[
            [
                "display_name",
                "value_a",
                "value_b",
                "absolute_delta",
                "relative_delta",
                "winner_slot",
            ]
        ],
        hide_index=True,
        column_config={
            "display_name": "Metric",
            "value_a": st.column_config.NumberColumn(
                "Network A",
                format="%.4f",
            ),
            "value_b": st.column_config.NumberColumn(
                "Network B",
                format="%.4f",
            ),
            "absolute_delta": st.column_config.NumberColumn(
                "Absolute delta",
                format="%.4f",
            ),
            "relative_delta": st.column_config.NumberColumn(
                "Relative delta",
                format="%.2f",
            ),
            "winner_slot": "Winner",
        },
    )
