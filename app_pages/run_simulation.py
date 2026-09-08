"""Select and execute a saved experiment in the background."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import streamlit as st

from src.ui.background import drain_progress, submit_simulation
from src.ui.components import empty_state, format_decimal, page_header
from src.ui.config import DATABASE_PATH
from src.ui.services import (
    get_database,
    get_experiment_comparison_summary,
    get_experiment_editor_data,
    list_experiments,
)


def _reset_job_state() -> None:
    st.session_state.simulation_job_future = None
    st.session_state.simulation_job_queue = None
    st.session_state.simulation_job_updates = []
    st.session_state.simulation_job_result = None
    st.session_state.simulation_job_error = None
    st.session_state.simulation_job_experiment_id = None
    st.session_state.selected_experiment_summary = None


def _selected_summary() -> dict[str, Any]:
    summary = st.session_state.get("selected_experiment_summary")
    if isinstance(summary, dict):
        return summary
    return {
        "arm_a_name": "Arm A",
        "arm_b_name": "Arm B",
        "labels": {
            "arm_a_role": "Arm A",
            "arm_b_role": "Arm B",
            "score_a": "Arm A score",
            "score_b": "Arm B score",
            "winner": "Comparison winner",
        },
    }


def _summary_cards(result: Any) -> None:
    comparison = result.comparison
    summary = _selected_summary()
    labels = summary.get("labels") or {}
    arm_a_name = summary.get("arm_a_name", "Arm A")
    arm_b_name = summary.get("arm_b_name", "Arm B")
    winner = comparison.get("winner_slot") or "TIE"
    winner_name = (
        "Tie"
        if winner == "TIE"
        else arm_a_name
        if winner == "A"
        else arm_b_name
        if winner == "B"
        else "Not scored"
    )

    score_columns = st.columns(3)
    score_columns[0].metric(
        labels.get("score_a", f"{arm_a_name} score"),
        format_decimal(comparison.get("score_a"), 2),
        border=True,
    )
    score_columns[1].metric(
        labels.get("score_b", f"{arm_b_name} score"),
        format_decimal(comparison.get("score_b"), 2),
        border=True,
    )
    score_columns[2].metric(
        labels.get("winner", "Comparison winner"),
        winner_name,
        border=True,
    )

    arm_columns = st.columns(2)
    for column, slot, arm_name in zip(
        arm_columns,
        ("A", "B"),
        (arm_a_name, arm_b_name),
        strict=True,
    ):
        arm_summary = result.network_summaries[slot]
        with column, st.container(border=True):
            st.markdown(f"**{arm_name}**")
            values = st.columns(3)
            values[0].metric(
                "Churn",
                format_decimal(arm_summary["final_churn_rate"], 3),
            )
            values[1].metric(
                "Gini",
                format_decimal(arm_summary["final_gini_coefficient"], 3),
            )
            values[2].metric(
                "Throughput",
                f"{format_decimal(arm_summary['average_throughput_tps'], 2)} "
                "actions/s",
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
                    f"{latest.total_events:,} opportunities."
                )
                status.write(
                    f"Virtual time: {latest.virtual_time_ms / 1_000:,.2f} s"
                )
            for update in progress_updates[-6:]:
                status.write(
                    f"{update.phase.title()}: "
                    f"{update.events_processed:,} opportunities at "
                    f"{update.virtual_time_ms / 1_000:,.2f} s"
                )
        return

    st.success(
        f"Simulation #{result.simulation_id} completed with "
        f"{result.event_count:,} shared opportunities.",
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
    "Execute a saved incentive-comparison experiment under reproducible "
    "conditions.",
    icon="play_circle",
)

try:
    experiments = list_experiments(database)
except Exception as exc:
    st.error(f"Failed to load experiments: {exc}")
    st.stop()

if not experiments:
    empty_state(
        "Configuration required",
        "Create an experiment with an environment, shared network model, "
        "and two incentive mechanisms before running a simulation.",
        icon="warning",
    )
    if st.button(
        "Set up experiments",
        icon=":material/science:",
        type="primary",
        width="stretch",
    ):
        st.switch_page("app_pages/experiments.py")
    st.stop()

job_active = (
    st.session_state.simulation_job_future is not None
    and st.session_state.simulation_job_result is None
    and st.session_state.simulation_job_error is None
)

experiment_by_id = {int(item["id"]): item for item in experiments}
experiment_ids = list(experiment_by_id)
selected_experiment_id = st.selectbox(
    "Experiment",
    options=experiment_ids,
    format_func=lambda value: experiment_by_id[value]["name"],
    disabled=job_active,
)

try:
    selected_experiment = get_experiment_editor_data(
        database,
        selected_experiment_id,
    )
    selected_summary = get_experiment_comparison_summary(
        database,
        selected_experiment_id,
    ).to_dict()
except Exception as exc:
    st.error(f"Failed to load experiment: {exc}")
    st.stop()

st.session_state.selected_experiment_summary = selected_summary
summary_columns = st.columns(4)
summary_columns[0].metric(
    "Environment",
    selected_summary["environment_name"],
    border=True,
)
summary_columns[1].metric(
    "Shared network model",
    selected_summary.get("shared_network_name")
    or f"{selected_summary.get('network_a_name')} / "
    f"{selected_summary.get('network_b_name')}",
    border=True,
)
summary_columns[2].metric(
    "Incentive A",
    selected_summary.get("incentive_a_name")
    or selected_summary.get("network_a_name")
    or "Legacy Network A",
    border=True,
)
summary_columns[3].metric(
    "Incentive B",
    selected_summary.get("incentive_b_name")
    or selected_summary.get("network_b_name")
    or "Legacy Network B",
    border=True,
)

with st.container(border=True):
    st.markdown("**Saved experiment parameters**")
    parameter_columns = st.columns(4)
    parameter_columns[0].metric(
        "Comparison model",
        (
            "Incentive mechanisms"
            if selected_experiment["comparison_model"]
            == "incentive_mechanisms"
            else "Legacy networks"
        ),
    )
    parameter_columns[1].metric(
        "Duration",
        f"{selected_experiment['duration_seconds']:,.2f} s",
    )
    parameter_columns[2].metric(
        "Poisson λ",
        f"{selected_experiment['poisson_lambda']:,.3f}/s",
    )
    parameter_columns[3].metric(
        "Seed",
        selected_experiment.get("default_random_seed")
        if selected_experiment.get("default_random_seed") is not None
        else "Generated",
    )
    st.caption(
        "Traffic mix: "
        + ", ".join(
            f"{key}={value}"
            for key, value in (
                selected_experiment.get("traffic_mix_json") or {}
            ).items()
        )
    )

with st.form("run_simulation_form"):
    run_name = st.text_input(
        "Simulation run name",
        value=(
            f"{selected_experiment['name']} · "
            f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        ),
        key="simulation_run_name",
        disabled=job_active,
    )
    seed_override = st.number_input(
        "Optional seed override",
        min_value=0,
        value=None,
        step=1,
        placeholder="Use the saved experiment seed",
        key="simulation_seed_override",
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
        if not run_name.strip():
            raise ValueError("Simulation run name is required.")
        random_seed = (
            int(seed_override) if seed_override is not None else None
        )
        future, progress_queue = submit_simulation(
            str(DATABASE_PATH),
            selected_experiment_id,
            run_name=run_name.strip(),
            random_seed=random_seed,
        )
        _reset_job_state()
        st.session_state.simulation_job_future = future
        st.session_state.simulation_job_queue = progress_queue
        st.session_state.simulation_job_experiment_id = (
            selected_experiment_id
        )
        st.session_state.selected_experiment_summary = selected_summary
    except Exception as exc:
        st.error(f"Unable to start simulation: {exc}")
    else:
        st.rerun()

simulation_monitor()
