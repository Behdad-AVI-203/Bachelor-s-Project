"""Configure and execute a dual-network simulation in the background."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

import streamlit as st

from src.ui.background import drain_progress, submit_simulation
from src.ui.components import empty_state, format_decimal, page_header
from src.ui.config import DATABASE_PATH
from src.ui.services import get_database, list_environments, list_networks


def _reset_job_state() -> None:
    st.session_state.simulation_job_future = None
    st.session_state.simulation_job_queue = None
    st.session_state.simulation_job_updates = []
    st.session_state.simulation_job_result = None
    st.session_state.simulation_job_error = None
    st.session_state.simulation_job_experiment_id = None


def _summary_cards(result: Any) -> None:
    comparison = result.comparison
    winner = comparison.get("winner_slot") or "TIE"
    score_columns = st.columns(3)
    score_columns[0].metric(
        "Network A score",
        format_decimal(comparison.get("score_a"), 2),
        border=True,
    )
    score_columns[1].metric(
        "Network B score",
        format_decimal(comparison.get("score_b"), 2),
        border=True,
    )
    score_columns[2].metric(
        "Winner",
        "Tie" if winner == "TIE" else f"Network {winner}",
        border=True,
    )

    network_columns = st.columns(2)
    for column, slot in zip(network_columns, ("A", "B"), strict=True):
        summary = result.network_summaries[slot]
        with column, st.container(border=True):
            st.markdown(f"**Network {slot}**")
            values = st.columns(3)
            values[0].metric(
                "Churn",
                format_decimal(summary["final_churn_rate"], 3),
            )
            values[1].metric(
                "Gini",
                format_decimal(summary["final_gini_coefficient"], 3),
            )
            values[2].metric(
                "Throughput",
                f"{format_decimal(summary['average_throughput_tps'], 2)} tx/s",
            )


@st.fragment(run_every=0.75)
def simulation_monitor() -> None:
    future = st.session_state.simulation_job_future
    progress_queue = st.session_state.simulation_job_queue
    if future is None or progress_queue is None:
        return

    updates = drain_progress(progress_queue)
    if updates:
        st.session_state.simulation_job_updates.extend(updates)

    if future.done() and st.session_state.simulation_job_result is None:
        try:
            st.session_state.simulation_job_result = future.result()
        except Exception as exc:
            st.session_state.simulation_job_error = str(exc)

    error = st.session_state.simulation_job_error
    result = st.session_state.simulation_job_result
    progress_updates = st.session_state.simulation_job_updates

    if error:
        st.error(f"Simulation failed: {error}")
        if st.button(
            "Clear failed run",
            icon=":material/refresh:",
            key="clear_failed_simulation",
        ):
            _reset_job_state()
            st.rerun(scope="app")
        return

    latest = progress_updates[-1] if progress_updates else None
    if result is None:
        progress = latest.progress if latest else 0.0
        phase = latest.phase.replace("_", " ").title() if latest else "Queued"
        st.progress(progress, text=f"{phase} · {progress * 100:.0f}%")
        with st.status(
            "Simulation running on the virtual clock",
            expanded=True,
            state="running",
        ) as status:
            if latest:
                status.write(
                    f"Processed {latest.events_processed:,} of "
                    f"{latest.total_events:,} events."
                )
                status.write(
                    f"Virtual time: {latest.virtual_time_ms / 1_000:,.2f} s"
                )
            for update in progress_updates[-6:]:
                status.write(
                    f"{update.phase.title()}: "
                    f"{update.events_processed:,} events at "
                    f"{update.virtual_time_ms / 1_000:,.2f} s"
                )
        return

    st.success(
        f"Simulation #{result.simulation_id} completed with "
        f"{result.event_count:,} shared events.",
        icon=":material/check_circle:",
    )
    _summary_cards(result)
    action_columns = st.columns([1, 1, 4])
    with action_columns[0]:
        if st.button(
            "Detailed results",
            icon=":material/query_stats:",
            type="primary",
            width="stretch",
            key="completed_results",
        ):
            st.session_state.selected_simulation_id = result.simulation_id
            st.switch_page("app_pages/results.py")
    with action_columns[1]:
        if st.button(
            "New simulation",
            icon=":material/refresh:",
            width="stretch",
            key="new_simulation",
        ):
            _reset_job_state()
            st.rerun(scope="app")


database = get_database()
page_header(
    "Run Simulation",
    "Run two networks against one reproducible Poisson event stream.",
    icon="play_circle",
)

try:
    environments = list_environments(database)
    networks = list_networks(database)
except Exception as exc:
    st.error(f"Failed to load simulation configurations: {exc}")
    st.stop()

if not environments or len(networks) < 2:
    missing = []
    if not environments:
        missing.append("one IoT environment")
    if len(networks) < 2:
        missing.append("two blockchain networks")
    empty_state(
        "Configuration required",
        "Create " + " and ".join(missing) + " before running a comparison.",
        icon="warning",
    )
    navigation_columns = st.columns(2)
    if navigation_columns[0].button(
        "Set up environments",
        icon=":material/sensors:",
        width="stretch",
    ):
        st.switch_page("app_pages/environments.py")
    if navigation_columns[1].button(
        "Set up networks",
        icon=":material/account_tree:",
        width="stretch",
    ):
        st.switch_page("app_pages/networks.py")
    st.stop()

job_active = (
    st.session_state.simulation_job_future is not None
    and st.session_state.simulation_job_result is None
    and st.session_state.simulation_job_error is None
)

environment_by_id = {
    int(environment["id"]): environment for environment in environments
}
network_by_id = {int(network["id"]): network for network in networks}
network_ids = list(network_by_id)

with st.form("run_simulation_form"):
    st.subheader("Comparison setup")
    selection_columns = st.columns(3)
    with selection_columns[0]:
        environment_id = st.selectbox(
            "IoT environment",
            options=list(environment_by_id),
            format_func=lambda value: (
                f"{environment_by_id[value]['name']} "
                f"({environment_by_id[value]['device_count']} devices)"
            ),
            disabled=job_active,
        )
    with selection_columns[1]:
        network_a_id = st.selectbox(
            "Network A",
            options=network_ids,
            format_func=lambda value: network_by_id[value]["name"],
            disabled=job_active,
        )
    with selection_columns[2]:
        network_b_id = st.selectbox(
            "Network B",
            options=network_ids,
            index=1,
            format_func=lambda value: network_by_id[value]["name"],
            disabled=job_active,
        )

    run_name = st.text_input(
        "Simulation name",
        value=f"Comparison {datetime.now():%Y-%m-%d %H:%M:%S}",
        disabled=job_active,
    )
    parameter_columns = st.columns(4)
    with parameter_columns[0]:
        duration_seconds = st.number_input(
            "Duration (seconds)",
            min_value=1.0,
            max_value=86_400.0,
            value=300.0,
            step=30.0,
            disabled=job_active,
        )
    with parameter_columns[1]:
        poisson_lambda = st.number_input(
            "Poisson λ (tx/s)",
            min_value=0.01,
            max_value=10_000.0,
            value=2.0,
            step=0.1,
            disabled=job_active,
        )
    with parameter_columns[2]:
        sample_interval_ms = st.number_input(
            "Sample interval (ms)",
            min_value=100,
            max_value=60_000,
            value=1_000,
            step=100,
            disabled=job_active,
        )
    with parameter_columns[3]:
        random_seed = st.number_input(
            "Random seed (optional)",
            min_value=0,
            value=None,
            step=1,
            placeholder="Generated automatically",
            disabled=job_active,
        )

    st.markdown("**Traffic mix weights**")
    traffic_columns = st.columns(3)
    with traffic_columns[0]:
        transfer_weight = st.number_input(
            "Transfer",
            min_value=0.0,
            value=0.2,
            step=0.05,
            disabled=job_active,
        )
    with traffic_columns[1]:
        iot_data_weight = st.number_input(
            "IoT data",
            min_value=0.0,
            value=0.7,
            step=0.05,
            disabled=job_active,
        )
    with traffic_columns[2]:
        feedback_weight = st.number_input(
            "Feedback",
            min_value=0.0,
            value=0.1,
            step=0.05,
            disabled=job_active,
        )

    with st.expander("Advanced traffic parameters"):
        advanced_columns = st.columns(3)
        with advanced_columns[0]:
            transfer_min = st.number_input(
                "Minimum transfer",
                min_value=0.001,
                value=0.1,
                step=0.1,
                disabled=job_active,
            )
        with advanced_columns[1]:
            transfer_max = st.number_input(
                "Maximum transfer",
                min_value=0.001,
                value=5.0,
                step=0.5,
                disabled=job_active,
            )
        with advanced_columns[2]:
            positive_feedback = st.number_input(
                "Positive feedback probability",
                min_value=0.0,
                max_value=1.0,
                value=0.7,
                step=0.05,
                disabled=job_active,
            )

    submitted = st.form_submit_button(
        "Run simulation",
        icon=":material/play_arrow:",
        type="primary",
        disabled=job_active,
    )

if submitted:
    try:
        if network_a_id == network_b_id:
            raise ValueError("Network A and Network B must be different.")
        if not run_name.strip():
            raise ValueError("Simulation name is required.")
        if transfer_min > transfer_max:
            raise ValueError(
                "Minimum transfer cannot exceed maximum transfer."
            )
        if transfer_weight + iot_data_weight + feedback_weight <= 0:
            raise ValueError(
                "At least one traffic type must have a positive weight."
            )

        experiment_name = (
            f"UI experiment {datetime.now():%Y%m%d-%H%M%S}-"
            f"{uuid4().hex[:8]}"
        )
        experiment_id = database.configurations.create_experiment_config(
            name=experiment_name,
            description=f"Generated by the UI for {run_name.strip()}",
            environment_id=int(environment_id),
            network_a_config_id=int(network_a_id),
            network_b_config_id=int(network_b_id),
            poisson_lambda=float(poisson_lambda),
            duration_seconds=float(duration_seconds),
            sample_interval_ms=int(sample_interval_ms),
            default_random_seed=(
                int(random_seed) if random_seed is not None else None
            ),
            traffic_mix={
                "transfer": float(transfer_weight),
                "iot_data": float(iot_data_weight),
                "feedback": float(feedback_weight),
            },
            parameters={
                "transfer_amount_min": float(transfer_min),
                "transfer_amount_max": float(transfer_max),
                "positive_feedback_probability": float(positive_feedback),
            },
        )
        future, progress_queue = submit_simulation(
            str(DATABASE_PATH),
            experiment_id,
            run_name=run_name.strip(),
            random_seed=(
                int(random_seed) if random_seed is not None else None
            ),
        )
        _reset_job_state()
        st.session_state.simulation_job_future = future
        st.session_state.simulation_job_queue = progress_queue
        st.session_state.simulation_job_experiment_id = experiment_id
    except Exception as exc:
        st.error(f"Unable to start simulation: {exc}")
    else:
        st.rerun()

simulation_monitor()
