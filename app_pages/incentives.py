"""Create, edit, import, export, and delete incentive mechanisms."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import pandas as pd
import streamlit as st

from src.database import DatabaseIntegrityError
from src.ui.components import empty_state, format_datetime, page_header
from src.ui.services import (
    DEFAULT_INCENTIVE_CODE,
    INCENTIVE_TEMPLATES,
    delete_incentive,
    export_configuration,
    get_database,
    get_incentive_editor_data,
    list_incentives,
    import_configuration,
    save_incentive,
)


TYPE_LABELS = {
    "built_in": "Built-in",
    "custom": "Custom",
    "legacy_reward": "Legacy reward",
}


def _load_editor_state(
    prefix: str,
    values: Mapping[str, Any],
    identity: str,
) -> None:
    marker = f"{prefix}_loaded_identity"
    if st.session_state.get(marker) == identity:
        return
    for key, value in values.items():
        st.session_state[f"{prefix}_{key}"] = value
    st.session_state[marker] = identity


def _apply_upload(prefix: str, uploaded_file: Any) -> None:
    if uploaded_file is None:
        return
    content = uploaded_file.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    marker = f"{prefix}_upload_digest"
    if st.session_state.get(marker) == digest:
        return
    st.session_state[f"{prefix}_source_code"] = content.decode("utf-8")
    st.session_state[marker] = digest


def _parse_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must contain valid JSON.") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError(f"{label} must be a JSON object.")
    return dict(parsed)


def _render_form(
    *,
    prefix: str,
    initial: Mapping[str, Any],
    incentive_id: int | None,
) -> None:
    identity = f"incentive-{incentive_id}" if incentive_id else "new-incentive"
    _load_editor_state(prefix, initial, identity)

    template_name = st.selectbox(
        "Built-in template",
        options=list(INCENTIVE_TEMPLATES),
        key=f"{prefix}_template",
    )
    template = INCENTIVE_TEMPLATES[template_name]
    st.caption(template["description"])
    if st.button(
        "Apply template",
        key=f"{prefix}_apply_template",
        icon=":material/auto_fix_high:",
    ):
        st.session_state[f"{prefix}_implementation_type"] = (
            template["implementation_type"]
        )
        st.session_state[f"{prefix}_built_in_key"] = template["built_in_key"]
        st.session_state[f"{prefix}_parameters"] = json.dumps(
            template["parameters"],
            indent=2,
        )
        st.rerun()

    implementation_type = st.selectbox(
        "Type",
        options=list(TYPE_LABELS),
        format_func=TYPE_LABELS.get,
        key=f"{prefix}_implementation_type",
    )
    uploaded_file = st.file_uploader(
        "Upload incentive Python code (.py)",
        type=["py"],
        key=f"{prefix}_upload",
        disabled=implementation_type == "built_in",
    )
    try:
        _apply_upload(prefix, uploaded_file)
    except UnicodeDecodeError:
        st.error("Uploaded Python files must use UTF-8 encoding.")

    with st.form(f"{prefix}_form"):
        name = st.text_input("Incentive name", key=f"{prefix}_name")
        description = st.text_area(
            "Description",
            key=f"{prefix}_description",
        )
        first_columns = st.columns(3)
        with first_columns[0]:
            st.caption(f"Type: {TYPE_LABELS[implementation_type]}")
        with first_columns[1]:
            version = st.number_input(
                "Version",
                min_value=1,
                step=1,
                key=f"{prefix}_version",
            )
        with first_columns[2]:
            built_in_key = st.text_input(
                "Built-in mechanism key",
                key=f"{prefix}_built_in_key",
                disabled=implementation_type != "built_in",
            )

        parameters_text = st.text_area(
            "Parameters (JSON object)",
            height=160,
            key=f"{prefix}_parameters",
        )
        metadata_text = st.text_area(
            "Metadata (JSON object)",
            height=130,
            key=f"{prefix}_metadata",
        )

        source_code = ""
        entrypoint = "evaluate"
        if implementation_type != "built_in":
            entrypoint = st.text_input(
                "Python entrypoint",
                key=f"{prefix}_entrypoint",
            )
            source_code = st.text_area(
                "Incentive mechanism Python code",
                height=330,
                key=f"{prefix}_source_code",
                help=(
                    "The function receives the generic incentive context and "
                    "returns an IncentiveOutcome-compatible value."
                ),
            )
        else:
            st.info(
                "Built-in mechanisms use the selected key and JSON parameters. "
                "They do not embed Python code."
            )

        submitted = st.form_submit_button(
            "Save incentive",
            icon=":material/save:",
            type="primary",
        )

    if not submitted:
        return

    try:
        parameters = _parse_json_object(parameters_text, "Parameters")
        metadata = _parse_json_object(metadata_text, "Metadata")
        saved_id = save_incentive(
            get_database(),
            name=name,
            version=int(version),
            description=description,
            implementation_type=implementation_type,
            built_in_key=built_in_key,
            source_code=source_code,
            entrypoint=entrypoint,
            parameters=parameters,
            metadata=metadata,
            incentive_id=incentive_id,
        )
    except Exception as exc:
        st.error(f"Failed to save incentive: {exc}")
    else:
        st.success(f"Incentive #{saved_id} was saved.")
        st.rerun()


@st.dialog("Delete incentive?", icon=":material/delete_forever:")
def confirm_delete(incentive_id: int, incentive_name: str) -> None:
    st.warning(
        f"`{incentive_name}` will be permanently removed. Incentives used "
        "by experiments or simulations cannot be deleted."
    )
    if st.button(
        "Delete incentive",
        type="primary",
        icon=":material/delete_forever:",
        width="stretch",
    ):
        try:
            delete_incentive(get_database(), incentive_id)
        except DatabaseIntegrityError:
            st.error(
                "This incentive is referenced by an experiment or simulation "
                "and cannot be deleted."
            )
        except Exception as exc:
            st.error(f"Failed to delete incentive: {exc}")
        else:
            st.toast("Incentive deleted.", icon=":material/check_circle:")
            st.rerun()


database = get_database()
page_header(
    "Incentive Mechanisms",
    "Create reusable rules that influence participation, contribution, "
    "rewards and penalties.",
    icon="tune",
)

try:
    incentives = list_incentives(database)
except Exception as exc:
    st.error(f"Failed to load incentive mechanisms: {exc}")
    st.stop()

st.subheader("Saved incentive mechanisms")
if incentives:
    table = pd.DataFrame(incentives)
    table["type"] = table["implementation_type"].map(TYPE_LABELS)
    table["created_at"] = table["created_at"].map(format_datetime)
    table["updated_at"] = table["updated_at"].map(format_datetime)
    st.dataframe(
        table[
            [
                "id",
                "name",
                "type",
                "version",
                "description",
                "created_at",
                "updated_at",
            ]
        ],
        hide_index=True,
        column_config={
            "id": st.column_config.NumberColumn("ID", format="%d"),
            "name": "Incentive",
            "type": "Type",
            "version": st.column_config.NumberColumn("Version", format="%d"),
            "description": "Description",
            "created_at": "Created",
            "updated_at": "Updated",
        },
    )
else:
    empty_state(
        "No saved incentives",
        "Create a built-in or custom mechanism before defining an experiment.",
        icon="tune",
    )

mode_options = ["Create new"]
if incentives:
    mode_options.append("Edit existing")
mode = st.segmented_control(
    "Workspace mode",
    mode_options,
    default=mode_options[0],
    label_visibility="collapsed",
)

if mode == "Create new":
    template = INCENTIVE_TEMPLATES["Default reward"]
    _render_form(
        prefix="create_incentive",
        incentive_id=None,
        initial={
            "name": "",
            "description": template["description"],
            "implementation_type": template["implementation_type"],
            "built_in_key": template["built_in_key"],
            "version": 1,
            "parameters": json.dumps(template["parameters"], indent=2),
            "metadata": "{}",
            "entrypoint": "evaluate",
            "source_code": DEFAULT_INCENTIVE_CODE,
        },
    )
elif incentives:
    incentive_by_id = {int(item["id"]): item for item in incentives}
    selected_id = st.selectbox(
        "Incentive",
        options=list(incentive_by_id),
        format_func=lambda value: incentive_by_id[value]["name"],
    )
    try:
        editor_data = get_incentive_editor_data(database, selected_id)
    except Exception as exc:
        st.error(f"Failed to load incentive: {exc}")
        st.stop()

    _render_form(
        prefix="edit_incentive",
        incentive_id=selected_id,
        initial={
            "name": editor_data["name"],
            "description": editor_data.get("description") or "",
            "implementation_type": editor_data["implementation_type"],
            "built_in_key": editor_data.get("built_in_key") or "",
            "version": editor_data["version"],
            "parameters": json.dumps(
                editor_data.get("parameters_json") or {},
                indent=2,
            ),
            "metadata": json.dumps(
                editor_data.get("metadata_json") or {},
                indent=2,
            ),
            "entrypoint": editor_data.get("entrypoint") or "evaluate",
            "source_code": editor_data.get("source_code")
            or DEFAULT_INCENTIVE_CODE,
        },
    )

    details = get_incentive_editor_data(database, selected_id)
    with st.expander("View incentive details"):
        st.json(
            {
                "id": details["id"],
                "name": details["name"],
                "version": details["version"],
                "implementation_type": details["implementation_type"],
                "built_in_key": details.get("built_in_key"),
                "parameters": details.get("parameters_json") or {},
                "metadata": details.get("metadata_json") or {},
                "artifact": details.get("artifact"),
            }
        )

    action_columns = st.columns([1, 1, 4])
    with action_columns[0]:
        export_json = export_configuration(database, "incentive", selected_id)
        st.download_button(
            "Export JSON",
            data=export_json,
            file_name=f"incentive-{selected_id}.json",
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
            confirm_delete(selected_id, incentive_by_id[selected_id]["name"])

st.divider()
with st.expander("Import an incentive configuration"):
    uploaded_bundle = st.file_uploader(
        "Incentive JSON bundle",
        type=["json"],
        key="incentive_import_file",
    )
    conflict_policy = st.selectbox(
        "Name conflict policy",
        ["rename", "error", "replace"],
        key="incentive_conflict_policy",
    )
    if st.button(
        "Import incentive",
        disabled=uploaded_bundle is None,
        icon=":material/upload:",
    ):
        try:
            payload = uploaded_bundle.getvalue().decode("utf-8")
            imported = import_configuration(database, payload, conflict=conflict_policy)
        except Exception as exc:
            st.error(f"Failed to import incentive: {exc}")
        else:
            st.success(
                f"Imported incentive #{imported['incentive_config_id']}."
            )
            st.rerun()
