"""Create, edit, import, export, and delete IoT environments."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.database import DatabaseIntegrityError
from src.ui.components import empty_state, format_datetime, page_header
from src.ui.services import (
    create_environment,
    delete_environment,
    empty_group_dataframe,
    environment_group_dataframe,
    export_configuration,
    get_database,
    import_configuration,
    list_environments,
    update_environment,
)


GROUP_COLUMNS = {
    "behavior_id": None,
    "group_name": st.column_config.TextColumn(
        "Group name",
        required=True,
        pinned=True,
    ),
    "device_count": st.column_config.NumberColumn(
        "Devices",
        min_value=1,
        max_value=100,
        step=1,
        required=True,
    ),
    "precision": st.column_config.NumberColumn(
        "Precision",
        min_value=0.0,
        max_value=1.0,
        step=0.01,
        format="%.2f",
        required=True,
    ),
    "execution_cost": st.column_config.NumberColumn(
        "Data cost",
        min_value=0.0,
        step=0.01,
        format="%.3f",
        required=True,
    ),
    "data_rate": st.column_config.NumberColumn(
        "Data rate",
        min_value=0.0,
        step=0.1,
        format="%.2f",
        required=True,
    ),
    "profit_expectation": st.column_config.NumberColumn(
        "Expected profit",
        min_value=0.0,
        step=0.01,
        format="%.3f",
        required=True,
    ),
    "initial_balance": st.column_config.NumberColumn(
        "Initial balance",
        min_value=0.0,
        step=1.0,
        format="%.2f",
        required=True,
    ),
    "base_value": st.column_config.NumberColumn(
        "Sensor baseline",
        step=0.1,
        format="%.2f",
        required=True,
    ),
    "unit": st.column_config.TextColumn("Unit", required=True),
}


@st.dialog(
    "Delete environment?",
    icon=":material/delete:",
)
def confirm_delete(environment_id: int, environment_name: str) -> None:
    st.warning(
        f"`{environment_name}` will be permanently removed. "
        "Environments used by experiments or simulations cannot be deleted."
    )
    if st.button(
        "Delete environment",
        type="primary",
        icon=":material/delete_forever:",
        width="stretch",
    ):
        try:
            delete_environment(get_database(), environment_id)
        except DatabaseIntegrityError:
            st.error(
                "This environment is referenced by an experiment or "
                "simulation and cannot be deleted."
            )
        except Exception as exc:
            st.error(f"Failed to delete environment: {exc}")
        else:
            st.toast(
                "Environment deleted.",
                icon=":material/check_circle:",
            )
            st.rerun()


database = get_database()
page_header(
    "Environment Setup",
    "Define reusable device groups and the shared IoT population.",
    icon="sensors",
)

try:
    environments = list_environments(database)
except Exception as exc:
    st.error(f"Failed to load environments: {exc}")
    st.stop()

st.subheader("Saved environments")
if environments:
    environment_table = pd.DataFrame(environments)
    environment_table["updated_at"] = environment_table["updated_at"].map(
        format_datetime
    )
    st.dataframe(
        environment_table[
            [
                "id",
                "name",
                "device_count",
                "group_count",
                "revision",
                "updated_at",
            ]
        ],
        hide_index=True,
        column_config={
            "id": st.column_config.NumberColumn("ID", format="%d"),
            "name": "Environment",
            "device_count": st.column_config.NumberColumn(
                "Devices",
                format="%d",
            ),
            "group_count": st.column_config.NumberColumn(
                "Groups",
                format="%d",
            ),
            "revision": st.column_config.NumberColumn(
                "Revision",
                format="%d",
            ),
            "updated_at": "Updated",
        },
    )
else:
    empty_state(
        "No saved environments",
        "Create the first IoT environment below.",
        icon="sensors",
    )

mode_options = ["Create new"]
if environments:
    mode_options.append("Edit existing")
mode = st.segmented_control(
    "Workspace mode",
    mode_options,
    default=mode_options[0],
    label_visibility="collapsed",
)

if mode == "Create new":
    st.subheader("Create environment")
    with st.form("create_environment_form"):
        name = st.text_input(
            "Environment name",
            placeholder="e.g. Urban air-quality sensors",
        )
        description = st.text_area(
            "Description",
            placeholder="Research scenario, assumptions, and notes.",
        )
        st.markdown("**Device groups**")
        st.caption(
            "Add or remove rows as needed. The total population is limited "
            "to 100 devices."
        )
        groups = st.data_editor(
            empty_group_dataframe(),
            num_rows="dynamic",
            hide_index=True,
            column_config=GROUP_COLUMNS,
            disabled=["behavior_id"],
            key="create_environment_groups",
        )
        submitted = st.form_submit_button(
            "Save environment",
            icon=":material/save:",
            type="primary",
        )

    if submitted:
        try:
            environment_id = create_environment(
                database,
                name=name,
                description=description,
                groups=groups,
            )
        except Exception as exc:
            st.error(f"Failed to save environment: {exc}")
        else:
            st.success(f"Environment #{environment_id} was created.")
            st.rerun()

elif environments:
    st.subheader("Edit environment")
    environment_by_id = {
        int(environment["id"]): environment for environment in environments
    }
    selected_id = st.selectbox(
        "Environment",
        options=list(environment_by_id),
        format_func=lambda value: environment_by_id[value]["name"],
    )
    selected = environment_by_id[selected_id]
    try:
        existing_groups = environment_group_dataframe(
            database,
            selected_id,
        )
    except Exception as exc:
        st.error(f"Failed to load environment: {exc}")
        st.stop()

    with st.form(f"edit_environment_form_{selected_id}"):
        edited_name = st.text_input(
            "Environment name",
            value=selected["name"],
        )
        edited_description = st.text_area(
            "Description",
            value=selected.get("description") or "",
        )
        edited_groups = st.data_editor(
            existing_groups,
            num_rows="dynamic",
            hide_index=True,
            column_config=GROUP_COLUMNS,
            disabled=["behavior_id"],
            key=f"edit_environment_groups_{selected_id}",
        )
        save_changes = st.form_submit_button(
            "Save changes",
            icon=":material/save:",
            type="primary",
        )

    action_columns = st.columns([1, 1, 4])
    with action_columns[0]:
        export_json = export_configuration(database, "environment", selected_id)
        st.download_button(
            "Export JSON",
            data=export_json,
            file_name=f"environment-{selected_id}.json",
            mime="application/json",
            icon=":material/download:",
            width="stretch",
        )
    with action_columns[1]:
        if st.button(
            "Delete",
            icon=":material/delete:",
            width="stretch",
        ):
            confirm_delete(selected_id, selected["name"])

    if save_changes:
        try:
            update_environment(
                database,
                environment_id=selected_id,
                name=edited_name,
                description=edited_description,
                groups=edited_groups,
            )
        except Exception as exc:
            st.error(f"Failed to update environment: {exc}")
        else:
            st.success("Environment updated.")
            st.rerun()

st.divider()
with st.expander("Import an environment configuration"):
    uploaded_bundle = st.file_uploader(
        "Environment JSON bundle",
        type=["json"],
        key="environment_import_file",
    )
    conflict_policy = st.selectbox(
        "Name conflict policy",
        ["rename", "error", "replace"],
        key="environment_conflict_policy",
    )
    if st.button(
        "Import environment",
        disabled=uploaded_bundle is None,
        icon=":material/upload:",
    ):
        try:
            payload = uploaded_bundle.getvalue().decode("utf-8")
            imported = import_configuration(database, payload, conflict=conflict_policy)
        except Exception as exc:
            st.error(f"Failed to import environment: {exc}")
        else:
            st.success(
                f"Imported environment #{imported['environment_id']}."
            )
            st.rerun()
