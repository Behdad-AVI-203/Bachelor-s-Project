"""Detailed incentive-comparison results and exports."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.ui.components import empty_state, format_datetime, page_header
from src.ui.exports import dataframe_to_csv, results_pdf
from src.ui.services import (
    build_results_export_frame,
    get_completed_simulations,
    get_database,
    get_simulation_device_states,
    get_simulation_results_view,
)


COLORS = {"A": "#2563EB", "B": "#0F766E"}


def _arms(results: dict[str, Any]) -> list[dict[str, Any]]:
    """Return comparison arms without exposing database table terminology."""
    return list(results.get("arms") or results.get("networks") or [])


def _summary(results: dict[str, Any]) -> dict[str, Any]:
    """Return the comparison-aware UI summary."""
    return dict(results.get("experiment_summary") or {})


def _arm_names(summary: dict[str, Any]) -> dict[str, str]:
    labels = summary.get("labels") or {}
    return {
        "A": str(
            summary.get("arm_a_name")
            or labels.get("arm_a")
            or "Arm A"
        ),
        "B": str(
            summary.get("arm_b_name")
            or labels.get("arm_b")
            or "Arm B"
        ),
    }


def _arm_roles(summary: dict[str, Any]) -> dict[str, str]:
    labels = summary.get("labels") or {}
    legacy = bool(summary.get("is_legacy"))
    defaults = (
        {"A": "Network A", "B": "Network B"}
        if legacy
        else {"A": "Incentive A", "B": "Incentive B"}
    )
    return {
        "A": str(labels.get("arm_a_role") or defaults["A"]),
        "B": str(labels.get("arm_b_role") or defaults["B"]),
    }


def _display_arm_label(
    summary: dict[str, Any],
    slot: str,
    name: str | None = None,
) -> str:
    names = _arm_names(summary)
    roles = _arm_roles(summary)
    return f"{roles.get(slot, f'Arm {slot}')}: {name or names.get(slot)}"


def _custom_summary(arm: dict[str, Any]) -> dict[str, Any]:
    value = arm.get("custom_summary_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _metric_value(
    arm: dict[str, Any],
    key: str,
    *,
    fallback: Any = None,
) -> Any:
    value = arm.get(key)
    if value is not None:
        return value
    return _custom_summary(arm).get(key, fallback)


def _final_average_balance(states: list[dict[str, Any]]) -> float | None:
    balances = [
        float(state["balance"])
        for state in states
        if state.get("balance") is not None
    ]
    return sum(balances) / len(balances) if balances else None


def _average_balance_series(
    database: Any,
    arms: list[dict[str, Any]],
    summary: dict[str, Any],
) -> pd.DataFrame:
    rows = []
    for arm in arms:
        slot = arm["network_slot"]
        states = get_simulation_device_states(database, arm["id"])
        frame = pd.DataFrame(states)
        if frame.empty:
            continue
        grouped = frame.groupby("elapsed_ms", as_index=False)["balance"].mean()
        for record in grouped.to_dict("records"):
            rows.append(
                {
                    "Elapsed seconds": record["elapsed_ms"] / 1_000,
                    "Average balance": record["balance"],
                    "Arm": _display_arm_label(
                        summary,
                        slot,
                        arm.get("network_name"),
                    ),
                    "Slot": slot,
                }
            )
    return pd.DataFrame(rows)


def _network_series(
    results: dict[str, Any],
    summary: dict[str, Any],
    value_key: str,
    value_label: str,
) -> pd.DataFrame:
    names = {
        arm["network_slot"]: arm.get("network_name")
        for arm in _arms(results)
    }
    rows = []
    for slot, samples in results.get("network_metrics", {}).items():
        for sample in samples:
            rows.append(
                {
                    "Elapsed seconds": sample["elapsed_ms"] / 1_000,
                    value_label: sample.get(value_key),
                    "Arm": _display_arm_label(
                        summary,
                        slot,
                        names.get(slot),
                    ),
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
                    name=group["Arm"].iloc[0],
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
        legend_title="Comparison arm",
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
    )
    st.plotly_chart(figure, width="stretch")


def _experiment_name(results: dict[str, Any]) -> str:
    run = results["run"]
    snapshot = run.get("configuration_snapshot_json") or {}
    experiment = snapshot.get("experiment") if isinstance(snapshot, dict) else {}
    if isinstance(experiment, dict) and experiment.get("name"):
        return str(experiment["name"])
    return str(run.get("name") or "Simulation")


def _incentive_effectiveness_frame(
    results: dict[str, Any],
    summary: dict[str, Any],
) -> pd.DataFrame:
    """Build the primary evaluation table from persisted arm summaries."""
    metric_specs = [
        ("Final retention rate", "final_retention_rate", "higher"),
        ("Average active-device ratio", "average_active_device_ratio", "higher"),
        ("Opportunity participation rate", "opportunity_participation_rate", "higher"),
        ("Churn rate", "churn_rate", "lower"),
        ("Useful contribution count", "useful_contribution_count", "higher"),
        ("Useful contribution rate", "useful_contribution_rate", "higher"),
        (
            "Useful contribution per active device",
            "useful_contribution_per_active_device",
            "higher",
        ),
        ("Total rewards", "total_rewards", "neutral"),
        ("Total penalties", "total_penalties", "lower"),
        ("Net incentive cost", "net_incentive_cost", "lower"),
        (
            "Incentive cost per useful contribution",
            "incentive_cost_per_useful_contribution",
            "lower",
        ),
        ("Reward distribution fairness", "reward_distribution_fairness", "higher"),
        ("Utility distribution fairness", "utility_distribution_fairness", "higher"),
    ]
    arm_by_slot = {arm["network_slot"]: arm for arm in _arms(results)}
    rows = []
    for display_name, key, direction in metric_specs:
        value_a = _metric_value(arm_by_slot.get("A", {}), key)
        value_b = _metric_value(arm_by_slot.get("B", {}), key)
        if value_a is None and value_b is None:
            continue
        winner = _winner_for_values(value_a, value_b, direction)
        rows.append(
            {
                "Metric": display_name,
                _arm_roles(summary)["A"]: value_a,
                _arm_roles(summary)["B"]: value_b,
                "Winner": (
                    _arm_roles(summary).get(winner)
                    if winner in {"A", "B"}
                    else "Tie"
                ),
            }
        )
    return pd.DataFrame(rows)


def _winner_for_values(
    value_a: Any,
    value_b: Any,
    direction: str,
) -> str | None:
    if value_a is None or value_b is None:
        return None
    if value_a == value_b or direction == "neutral":
        return None
    if direction == "lower":
        return "A" if value_a < value_b else "B"
    return "A" if value_a > value_b else "B"


def _network_context_frame(
    results: dict[str, Any],
    summary: dict[str, Any],
) -> pd.DataFrame:
    arms = _arms(results)
    if len(arms) != 2:
        return pd.DataFrame()
    metrics = [
        ("Throughput (tx/s)", "average_throughput_tps"),
        ("Average confirmation latency (ms)", "average_confirmation_ms"),
        ("Total blocks", "total_blocks"),
        ("Network fees", "total_fees"),
        ("Confirmed transactions", "confirmed_transactions"),
    ]
    by_slot = {arm["network_slot"]: arm for arm in _arms(results)}
    return pd.DataFrame(
        [
            {
                "Metric": label,
                _arm_roles(summary)["A"]: _metric_value(
                    by_slot.get("A", {}),
                    key,
                ),
                _arm_roles(summary)["B"]: _metric_value(
                    by_slot.get("B", {}),
                    key,
                ),
            }
            for label, key in metrics
        ]
    )


database = get_database()
summary: dict[str, Any] = {}
page_header(
    "Results",
    "Compare incentive effectiveness under the same IoT environment and network model.",
    icon="query_stats",
)

try:
    completed_runs = get_completed_simulations(database)
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
        results = get_simulation_results_view(database, selected_id)
        summary = _summary(results)
        arms = _arms(results)
        average_balance = _average_balance_series(
            database,
            arms,
            summary,
        )
except Exception as exc:
    st.error(f"Failed to load simulation results: {exc}")
    st.stop()

run = results["run"]
comparison = results.get("comparison") or {}
arm_names = _arm_names(summary)
arm_roles = _arm_roles(summary)
legacy = bool(summary.get("is_legacy"))

with st.container(border=True):
    title_columns = st.columns([3, 2])
    with title_columns[0]:
        st.subheader(_experiment_name(results))
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
            else (
                f"{arm_roles.get(winner, 'Arm')} — "
                f"{arm_names.get(winner, 'Not scored')}"
                if winner in {"A", "B"}
                else "Not scored"
            )
        )
        st.metric(
            "Best-performing network"
            if legacy
            else "Best-performing incentive mechanism",
            winner_text,
        )

st.subheader("Experiment context")
context = {
    "Experiment": _experiment_name(results),
    "IoT Environment": summary.get("environment_name") or "—",
    "Shared Network Model": (
        summary.get("shared_network_name")
        if not legacy
        else "Legacy network comparison"
    ),
    "Incentive A" if not legacy else "Network A": (
        arm_names["A"]
    ),
    "Incentive B" if not legacy else "Network B": (
        arm_names["B"]
    ),
    "Comparison model": (
        "Incentive mechanisms"
        if not legacy
        else "Legacy networks"
    ),
}
st.dataframe(
    pd.DataFrame([context]),
    hide_index=True,
    width="stretch",
)

st.subheader("Incentive effectiveness" if not legacy else "Comparison metrics")
effectiveness_frame = _incentive_effectiveness_frame(results, summary)
if effectiveness_frame.empty:
    st.info("No incentive-effectiveness metrics were stored for this run.")
else:
    st.dataframe(effectiveness_frame, hide_index=True, width="stretch")

metric_columns = st.columns(4)
metric_columns[0].metric(
    f"{arm_roles['A']} score",
    comparison.get("score_a") or 0,
    border=True,
    format="%.2f",
)
metric_columns[1].metric(
    f"{arm_roles['B']} score",
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
            summary,
            "gini_coefficient",
            "Gini coefficient",
        )
        _line_chart(
            gini_series,
            value_column="Gini coefficient",
            title="Balance fairness over time",
        )
if chart_tabs[2].open:
    with chart_tabs[2]:
        throughput_series = _network_series(
            results,
            summary,
            "throughput_tps",
            "Throughput (tx/s)",
        )
        _line_chart(
            throughput_series,
            value_column="Throughput (tx/s)",
            title="Network throughput context",
        )

st.subheader("Network context")
network_context = _network_context_frame(results, summary)
if network_context.empty:
    st.info("No network-context metrics were stored for this run.")
else:
    st.dataframe(network_context, hide_index=True, width="stretch")

export_frame = build_results_export_frame(results)
export_rows = export_frame.fillna("—").to_dict("records")
export_columns = st.columns([1, 1, 4])
with export_columns[0]:
    st.download_button(
        "Download CSV",
        data=dataframe_to_csv(export_frame),
        file_name=f"simulation-{selected_id}-results.csv",
        mime="text/csv",
        icon=":material/download:",
        width="stretch",
    )
with export_columns[1]:
    st.download_button(
        "Download PDF",
        data=results_pdf(
            title=_experiment_name(results),
            simulation_id=selected_id,
            rows=export_rows,
        ),
        file_name=f"simulation-{selected_id}-results.pdf",
        mime="application/pdf",
        icon=":material/picture_as_pdf:",
        width="stretch",
    )

st.subheader("Detailed comparison")
comparison_frame = pd.DataFrame(results.get("comparison_metrics") or [])
if comparison_frame.empty:
    st.info("No detailed comparison metrics were stored for this run.")
else:
    comparison_frame = comparison_frame.copy()
    if "details_json" in comparison_frame:
        comparison_frame["Category"] = comparison_frame["details_json"].map(
            lambda value: (
                value.get("category", "comparison")
                if isinstance(value, dict)
                else "comparison"
            )
        )
    st.dataframe(
        comparison_frame[
            [
                "Category",
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
            "Category": "Metric category",
            "display_name": "Metric",
            "value_a": st.column_config.NumberColumn(
                arm_roles["A"],
                format="%.4f",
            ),
            "value_b": st.column_config.NumberColumn(
                arm_roles["B"],
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
