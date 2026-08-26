"""Create, edit, clone, import, and export experiment configurations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import pandas as pd
import streamlit as st

from src.ui.components import empty_state, format_datetime, page_header
from src.ui.services import (
    clone_experiment,
    export_configuration,
    get_database,
    get_experiment_editor_data,
    list_environments,
    list_experiments,
    list_incentives,
    import_configuration,
    list_networks,
    save_experiment,
)


def _parse_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must contain valid JSON.") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError(f"{label} must be a JSON object.")
    return dict(parsed)


def _traffic_mix(
    transfer_weight: float,
    iot_data_weight: float,
    feedback_weight: float,
) -> dict[str, float]:
    values = {
        "transfer": float(transfer_weight),
        "iot_data": float(iot_data_weight),
        "feedback": float(feedback_weight),
    }
    if sum(values.values()) <= 0:
        raise ValueError("At least one traffic weight must be positive.")
    return values


def _render_experiment_form(
    *,
    database: Any,
    prefix: str,
    initial: Mapping[str, Any],
    experiment_id: int | None,
    environments: list[dict[str, Any]],
    networks: list[dict[str, Any]],
    incentives: list[dict[str, Any]],
) -> None:
    environment_by_id = {
        int(item["id"]): item for item in environments
    }
    network_by_id = {int(item["id"]): item for item in networks}
    incentive_by_id = {int(item["id"]): item for item in incentives}

    environment_options = list(environment_by_id)
    network_options = list(network_by_id)
    incentive_options = list(incentive_by_id)
    if not environment_options or not network_options or not incentive_options:
        st.warning(
            "Create at least one environment, one network model, and one "
            "incentive mechanism before creating an experiment. The same "
            "incentive may be selected in both arms for control runs."
        )
        return

    initial_environment = initial.get("environment_id")
    initial_network = initial.get("network_config_id")
    initial_incentive_a = initial.get("incentive_a_config_id")
    initial_incentive_b = initial.get("incentive_b_config_id")

    with st.form(f"{prefix}_form"):
        st.subheader(
            "Create experiment"
            if experiment_id is None
            else "Edit experiment"
        )
        name = st.text_input(
            "Experiment name",
            value=str(initial.get("name") or ""),
            key=f"{prefix}_name",
        )
        description = st.text_area(
            "Description",
            value=str(initial.get("description") or ""),
            key=f"{prefix}_description",
        )

        selection_columns = st.columns(4)
        with selection_columns[0]:
            environment_id = st.selectbox(
                "IoT environment",
                options=environment_options,
                index=(
                    environment_options.index(initial_environment)
                    if initial_environment in environment_options
                    else 0
                ),
                format_func=lambda value: (
                    f"{environment_by_id[value]['name']} "
                    f"({environment_by_id[value]['device_count']} devices)"
                ),
                key=f"{prefix}_environment",
            )
        with selection_columns[1]:
            network_id = st.selectbox(
                "Shared network model",
                options=network_options,
                index=(
                    network_options.index(initial_network)
                    if initial_network in network_options
                    else 0
                ),
                format_func=lambda value: network_by_id[value]["name"],
                key=f"{prefix}_network",
            )
        with selection_columns[2]:
            incentive_a_id = st.selectbox(
                "Incentive A",
                options=incentive_options,
                index=(
                    incentive_options.index(initial_incentive_a)
                    if initial_incentive_a in incentive_options
                    else 0
                ),
                format_func=lambda value: incentive_by_id[value]["name"],
                key=f"{prefix}_incentive_a",
            )
        with selection_columns[3]:
            default_b_index = (
                incentive_options.index(initial_incentive_b)
                if initial_incentive_b in incentive_options
                else min(1, len(incentive_options) - 1)
            )
            incentive_b_id = st.selectbox(
                "Incentive B",
                options=incentive_options,
                index=default_b_index,
                format_func=lambda value: incentive_by_id[value]["name"],
                key=f"{prefix}_incentive_b",
            )

        st.markdown("**Execution parameters**")
        parameter_columns = st.columns(4)
        with parameter_columns[0]:
            duration_seconds = st.number_input(
                "Duration (seconds)",
                min_value=0.001,
                value=float(initial.get("duration_seconds") or 30.0),
                step=1.0,
                key=f"{prefix}_duration",
            )
        with parameter_columns[1]:
            poisson_lambda = st.number_input(
                "Poisson λ (events/second)",
                min_value=0.001,
                value=float(initial.get("poisson_lambda") or 3.0),
                step=0.1,
                key=f"{prefix}_lambda",
            )
        with parameter_columns[2]:
            sample_interval_ms = st.number_input(
                "Sample interval (ms)",
                min_value=1,
                value=int(initial.get("sample_interval_ms") or 1_000),
                step=100,
                key=f"{prefix}_sample_interval",
            )
        with parameter_columns[3]:
            seed_text = st.text_input(
                "Random seed (optional)",
                value=(
                    ""
                    if initial.get("default_random_seed") is None
                    else str(initial["default_random_seed"])
                ),
                key=f"{prefix}_seed",
            )

        traffic = initial.get("traffic_mix_json") or {}
        st.markdown("**Traffic mix**")
        traffic_columns = st.columns(3)
        with traffic_columns[0]:
            transfer_weight = st.number_input(
                "Transfer weight",
                min_value=0.0,
                value=float(traffic.get("transfer", 0.2)),
                step=0.1,
                key=f"{prefix}_transfer_weight",
            )
        with traffic_columns[1]:
            iot_data_weight = st.number_input(
                "IoT-data weight",
                min_value=0.0,
                value=float(traffic.get("iot_data", 0.7)),
                step=0.1,
                key=f"{prefix}_iot_data_weight",
            )
        with traffic_columns[2]:
            feedback_weight = st.number_input(
                "Feedback weight",
                min_value=0.0,
                value=float(traffic.get("feedback", 0.1)),
                step=0.1,
                key=f"{prefix}_feedback_weight",
            )

        parameters_text = st.text_area(
            "Additional traffic parameters (JSON object)",
            value=json.dumps(
                initial.get("parameters_json") or {},
                indent=2,
            ),
            height=140,
            key=f"{prefix}_parameters",
        )

        submitted = st.form_submit_button(
            "Save experiment",
            type="primary",
            icon=":material/save:",
        )

    if not submitted:
        return

    try:
        random_seed = int(seed_text.strip()) if seed_text.strip() else None
        parameters = _parse_json_object(
            parameters_text,
            "Additional traffic parameters",
        )
        traffic_mix = _traffic_mix(
            transfer_weight,
            iot_data_weight,
            feedback_weight,
        )
        saved_id = save_experiment(
            database,
            name=name,
            description=description,
            environment_id=int(environment_id),
            network_id=int(network_id),
            incentive_a_id=int(incentive_a_id),
            incentive_b_id=int(incentive_b_id),
            poisson_lambda=float(poisson_lambda),
            duration_seconds=float(duration_seconds),
            sample_interval_ms=int(sample_interval_ms),
            random_seed=random_seed,
            traffic_mix=traffic_mix,
            parameters=parameters,
            experiment_id=experiment_id,
        )
    except Exception as exc:
        st.error(f"Failed to save experiment: {exc}")
    else:
        st.success(f"Experiment #{saved_id} was saved.")
        st.rerun()


@st.dialog("Clone experiment")
def clone_dialog(database: Any, experiment_id: int, source_name: str) -> None:
    clone_name = st.text_input(
        "New experiment name",
        value=f"{source_name} copy {datetime.now():%Y%m%d-%H%M%S}",
        key=f"clone_name_{experiment_id}",
    )
    if st.button(
        "Clone experiment",
        type="primary",
        icon=":material/content_copy:",
        width="stretch",
    ):
        try:
            clone_id = clone_experiment(
                database,
                experiment_id,
                name=clone_name,
            )
        except Exception as exc:
            st.error(f"Failed to clone experiment: {exc}")
        else:
            st.success(f"Experiment #{clone_id} was cloned.")
            st.rerun()


database = get_database()
page_header(
    "Experiments",
    "Define a reproducible environment, network model, and incentive "
    "mechanism comparison.",
    icon="science",
)

try:
    environments = list_environments(database)
    networks = list_networks(database)
    incentives = list_incentives(database)
    experiments = list_experiments(database)
except Exception as exc:
    st.error(f"Failed to load experiment configuration data: {exc}")
    st.stop()

st.subheader("Saved experiments")
if experiments:
    table = pd.DataFrame(experiments)
    table["model"] = table["comparison_model"].map(
        {
            "incentive_mechanisms": "Incentive comparison",
            "legacy_networks": "Legacy network comparison",
        }
    )
    table["created_at"] = table["created_at"].map(format_datetime)
    table["updated_at"] = table["updated_at"].map(format_datetime)
    table["network_display"] = table.apply(
        lambda row: row["network_name"]
        if row["comparison_model"] == "incentive_mechanisms"
        else f"{row['network_a_name']} / {row['network_b_name']}",
        axis=1,
    )
    table["incentive_display"] = table.apply(
        lambda row: f"{row['incentive_a_name']} / {row['incentive_b_name']}"
        if row["comparison_model"] == "incentive_mechanisms"
        else "—",
        axis=1,
    )
    st.dataframe(
        table[
            [
                "id",
                "name",
                "model",
                "environment_name",
                "network_display",
                "incentive_display",
                "created_at",
                "updated_at",
            ]
        ],
        hide_index=True,
        column_config={
            "id": st.column_config.NumberColumn("ID", format="%d"),
            "name": "Experiment",
            "model": "Comparison model",
            "environment_name": "Environment",
            "network_display": "Network",
            "incentive_display": "Incentive A / B",
            "created_at": "Created",
            "updated_at": "Updated",
        },
    )
else:
    empty_state(
        "No saved experiments",
        "Create an incentive-comparison experiment below.",
        icon="science",
    )

mode_options = ["Create new"]
if experiments:
    mode_options.append("Edit existing")
mode = st.segmented_control(
    "Workspace mode",
    mode_options,
    default=mode_options[0],
    label_visibility="collapsed",
)

if mode == "Create new":
    _render_experiment_form(
        database=database,
        prefix="create_experiment",
        initial={
            "description": "",
            "duration_seconds": 30.0,
            "poisson_lambda": 3.0,
            "sample_interval_ms": 1_000,
            "traffic_mix_json": {
                "transfer": 0.2,
                "iot_data": 0.7,
                "feedback": 0.1,
            },
            "parameters_json": {
                "transfer_amount_min": 0.1,
                "transfer_amount_max": 2.0,
                "positive_feedback_probability": 0.75,
            },
        },
        experiment_id=None,
        environments=environments,
        networks=networks,
        incentives=incentives,
    )
elif experiments:
    experiment_by_id = {int(item["id"]): item for item in experiments}
    selected_id = st.selectbox(
        "Experiment",
        options=list(experiment_by_id),
        format_func=lambda value: experiment_by_id[value]["name"],
    )
    selected = experiment_by_id[selected_id]
    editor_data = get_experiment_editor_data(database, selected_id)
    if selected["comparison_model"] != "incentive_mechanisms":
        st.info(
            "This is a legacy Network A/B experiment. It remains readable "
            "but cannot be edited or converted here."
        )
    else:
        _render_experiment_form(
            database=database,
            prefix=f"edit_experiment_{selected_id}",
            initial=editor_data,
            experiment_id=selected_id,
            environments=environments,
            networks=networks,
            incentives=incentives,
        )

    action_columns = st.columns([1, 1, 4])
    with action_columns[0]:
        export_json = export_configuration(database, "experiment", selected_id)
        st.download_button(
            "Export JSON",
            data=export_json,
            file_name=f"experiment-{selected_id}.json",
            mime="application/json",
            icon=":material/download:",
            width="stretch",
        )
    with action_columns[1]:
        if selected["comparison_model"] == "incentive_mechanisms":
            if st.button(
                "Clone",
                icon=":material/content_copy:",
                width="stretch",
            ):
                clone_dialog(database, selected_id, selected["name"])

st.divider()
with st.expander("Import an experiment configuration"):
    uploaded_bundle = st.file_uploader(
        "Experiment JSON bundle",
        type=["json"],
        key="experiment_import_file",
    )
    conflict_policy = st.selectbox(
        "Name conflict policy",
        ["rename", "error", "replace"],
        key="experiment_conflict_policy",
    )
    if st.button(
        "Import experiment",
        disabled=uploaded_bundle is None,
        icon=":material/upload:",
    ):
        try:
            payload = uploaded_bundle.getvalue().decode("utf-8")
            bundle = json.loads(payload)
            if not isinstance(bundle, Mapping) or bundle.get(
                "bundle_type"
            ) not in {"experiment", "full"}:
                raise ValueError(
                    "The uploaded JSON must be an experiment or full bundle."
                )
            imported = import_configuration(
                database,
                bundle,
                conflict=conflict_policy,
            )
        except Exception as exc:
            st.error(f"Failed to import experiment: {exc}")
        else:
            st.success(
                f"Imported experiment #{imported['experiment_config_id']}."
            )
            st.rerun()
