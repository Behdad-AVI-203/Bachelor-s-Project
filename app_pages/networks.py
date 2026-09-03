"""Create, edit, import, export, and delete network configurations."""

from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd
import streamlit as st

from src.database import DatabaseIntegrityError
from src.ui.components import empty_state, format_datetime, page_header
from src.ui.services import (
    NETWORK_TEMPLATES,
    delete_network,
    export_configuration,
    get_database,
    get_network_editor_data,
    list_networks,
    import_configuration,
    save_network,
)


def _load_editor_state(
    prefix: str,
    values: dict[str, Any],
    identity: str,
) -> None:
    marker = f"{prefix}_loaded_identity"
    if st.session_state.get(marker) == identity:
        return
    for key, value in values.items():
        st.session_state[f"{prefix}_{key}"] = value
    st.session_state[marker] = identity


def _apply_template(prefix: str, template_name: str) -> None:
    template = NETWORK_TEMPLATES[template_name]
    for key in (
        "model_type",
        "difficulty",
        "block_capacity",
        "block_interval_ms",
        "fee_rate",
        "base_mining_time_ms",
        "logic_code",
    ):
        st.session_state[f"{prefix}_{key}"] = template[key]


def _apply_upload(prefix: str, field: str, uploaded_file: Any) -> None:
    if uploaded_file is None:
        return
    content = uploaded_file.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    marker = f"{prefix}_{field}_upload_digest"
    if st.session_state.get(marker) != digest:
        st.session_state[f"{prefix}_{field}"] = content.decode("utf-8")
        st.session_state[marker] = digest


def _render_network_form(
    *,
    prefix: str,
    initial: dict[str, Any],
    network_id: int | None,
) -> None:
    identity = f"network-{network_id}" if network_id else "new-network"
    _load_editor_state(prefix, initial, identity)

    template_name = st.selectbox(
        "Built-in template",
        options=list(NETWORK_TEMPLATES),
        key=f"{prefix}_template",
        help="Apply a template, then refine every parameter and code field.",
    )
    template = NETWORK_TEMPLATES[template_name]
    st.caption(template["description"])
    if st.button(
        "Apply template",
        key=f"{prefix}_apply_template",
        icon=":material/auto_fix_high:",
    ):
        _apply_template(prefix, template_name)
        st.rerun()

    upload_columns = st.columns(2)
    with upload_columns[0]:
        reward_upload = st.file_uploader(
            "Legacy reward plugin (.py, optional)",
            type=["py"],
            key=f"{prefix}_reward_upload",
            help=(
                "Compatibility only. New incentive experiments must define "
                "rewards through Incentive Mechanisms."
            ),
        )
    with upload_columns[1]:
        logic_upload = st.file_uploader(
            "Load custom network logic (.py)",
            type=["py"],
            key=f"{prefix}_logic_upload",
        )
    try:
        _apply_upload(prefix, "reward_code", reward_upload)
        _apply_upload(prefix, "logic_code", logic_upload)
    except UnicodeDecodeError:
        st.error("Uploaded Python files must use UTF-8 encoding.")

    with st.form(f"{prefix}_form"):
        name = st.text_input("Network name", key=f"{prefix}_name")
        description = st.text_area(
            "Description",
            key=f"{prefix}_description",
        )

        st.markdown("**Network processing settings**")
        model_columns = st.columns(2)
        with model_columns[0]:
            model_types = ["pow"]
            model_type_labels = {"pow": "Proof of Work (PoW)"}
            model_type = st.selectbox(
                "Network model type",
                options=model_types,
                format_func=model_type_labels.get,
                key=f"{prefix}_model_type",
                help="Proof of Work is currently the only built-in model.",
            )
        is_pow = model_type == "pow"
        with model_columns[1]:
            difficulty = st.number_input(
                "PoW difficulty" if is_pow else "Processing difficulty",
                min_value=0,
                max_value=8 if is_pow else 100,
                step=1,
                key=f"{prefix}_difficulty",
                help="PoW leading-zero difficulty." if is_pow else None,
            )
        capacity_columns = st.columns(2)
        with capacity_columns[0]:
            block_capacity = st.number_input(
                "Processing batch capacity",
                min_value=1,
                max_value=10_000,
                step=1,
                key=f"{prefix}_block_capacity",
            )
        with capacity_columns[1]:
            block_interval_ms = st.number_input(
                "Target processing interval (ms)",
                min_value=1,
                step=100,
                key=f"{prefix}_block_interval_ms",
            )
        fee_columns = st.columns(2)
        with fee_columns[0]:
            fee_rate = st.number_input(
                "Action fee rate",
                min_value=0.0,
                step=0.001,
                format="%.4f",
                key=f"{prefix}_fee_rate",
            )
        with fee_columns[1]:
            base_mining_time_ms = st.number_input(
                "Base processing time (ms)",
                min_value=0.001,
                step=1.0,
                format="%.3f",
                key=f"{prefix}_base_mining_time_ms",
            )
        st.markdown("**Connectivity (static probabilistic model)**")
        connectivity_columns = st.columns(2)
        with connectivity_columns[0]:
            link_availability = st.slider(
                "Link availability probability",
                0.0,
                1.0,
                step=0.01,
                key=f"{prefix}_link_availability",
            )
        with connectivity_columns[1]:
            packet_delivery = st.slider(
                "Packet delivery success probability",
                0.0,
                1.0,
                step=0.01,
                key=f"{prefix}_packet_delivery",
            )
        st.markdown("**Optional network logic plugin**")
        logic_entrypoint = st.text_input(
            "Network logic entrypoint",
            key=f"{prefix}_logic_entrypoint",
        )
        logic_code = st.text_area(
            "Custom network logic Python code",
            height=220,
            key=f"{prefix}_logic_code",
            help=(
                "Leave empty to use the built-in action handling logic."
            ),
        )
        submitted = st.form_submit_button(
            "Save network",
            icon=":material/save:",
            type="primary",
        )

    if submitted:
        try:
            compatibility_reward_code = st.session_state.get(
                f"{prefix}_reward_code",
                "",
            )
            compatibility_reward_entrypoint = st.session_state.get(
                f"{prefix}_reward_entrypoint",
                "calculate_reward",
            )
            saved_id = save_network(
                get_database(),
                name=name,
                description=description,
                model_type=str(model_type),
                difficulty=int(difficulty),
                block_capacity=int(block_capacity),
                block_interval_ms=int(block_interval_ms),
                fee_rate=float(fee_rate),
                base_mining_time_ms=float(base_mining_time_ms),
                base_iot_reward=0.0,
                feedback_weight=0.0,
                reward_code=compatibility_reward_code,
                reward_entrypoint=compatibility_reward_entrypoint,
                logic_code=logic_code,
                logic_entrypoint=logic_entrypoint,
                network_id=network_id,
                legacy_reward_compatibility=bool(
                    st.session_state.get(f"{prefix}_reward_code", "").strip()
                ),
                connectivity_parameters={
                    "link_availability_probability": float(
                        link_availability
                    ),
                    "packet_delivery_success_probability": float(
                        packet_delivery
                    ),
                },
            )
        except Exception as exc:
            st.error(f"Failed to save network: {exc}")
        else:
            st.success(f"Network #{saved_id} was saved.")
            st.rerun()


@st.dialog("Delete network?", icon=":material/delete:")
def confirm_delete(network_id: int, network_name: str) -> None:
    st.warning(
        f"`{network_name}` will be permanently removed. Networks used by "
        "experiments or simulations cannot be deleted."
    )
    if st.button(
        "Delete network",
        type="primary",
        icon=":material/delete_forever:",
        width="stretch",
    ):
        try:
            delete_network(get_database(), network_id)
        except DatabaseIntegrityError:
            st.error(
                "This network is referenced by an experiment or simulation "
                "and cannot be deleted."
            )
        except Exception as exc:
            st.error(f"Failed to delete network: {exc}")
        else:
            st.toast("Network deleted.", icon=":material/check_circle:")
            st.rerun()


database = get_database()
page_header(
    "Network Setup",
    "Configure network processing, validation, fees, connectivity, and network plugins.",
    icon="account_tree",
)

try:
    networks = list_networks(database)
except Exception as exc:
    st.error(f"Failed to load network configurations: {exc}")
    st.stop()

st.subheader("Saved networks")
if networks:
    network_table = pd.DataFrame(networks)
    network_table["created_at"] = network_table["created_at"].map(
        format_datetime
    )
    st.dataframe(
        network_table[
            [
                "id",
                "name",
                "pow_difficulty",
                "max_transactions_per_block",
                "target_block_time_ms",
                "created_at",
            ]
        ],
        hide_index=True,
        column_config={
            "id": st.column_config.NumberColumn("ID", format="%d"),
            "name": "Network",
            "pow_difficulty": st.column_config.NumberColumn(
                "Processing difficulty",
                format="%d",
            ),
            "max_transactions_per_block": st.column_config.NumberColumn(
                "Batch capacity",
                format="%d",
            ),
            "target_block_time_ms": st.column_config.NumberColumn(
                "Batch interval (ms)",
                format="%d",
            ),
            "created_at": "Created",
        },
    )
else:
    empty_state(
        "No saved networks",
        "Create at least two networks for comparative simulation.",
        icon="account_tree",
    )

mode_options = ["Create new"]
if networks:
    mode_options.append("Edit existing")
mode = st.segmented_control(
    "Workspace mode",
    mode_options,
    default=mode_options[0],
    label_visibility="collapsed",
)

if mode == "Create new":
    template = NETWORK_TEMPLATES["Default processing"]
    _render_network_form(
        prefix="create_network",
        network_id=None,
        initial={
            "name": "",
            "description": "",
            "model_type": template["model_type"],
            "difficulty": template["difficulty"],
            "block_capacity": template["block_capacity"],
            "block_interval_ms": template["block_interval_ms"],
            "fee_rate": template["fee_rate"],
            "base_mining_time_ms": template["base_mining_time_ms"],
            "link_availability": 1.0,
            "packet_delivery": 1.0,
            "base_iot_reward": template["base_iot_reward"],
            "feedback_weight": template["feedback_weight"],
            "reward_entrypoint": "calculate_reward",
            "reward_code": "",
            "logic_entrypoint": "process_transaction",
            "logic_code": template["logic_code"],
        },
    )
elif networks:
    network_by_id = {
        int(network["id"]): network for network in networks
    }
    selected_id = st.selectbox(
        "Network",
        options=list(network_by_id),
        format_func=lambda value: network_by_id[value]["name"],
    )
    try:
        editor_data = get_network_editor_data(database, selected_id)
    except Exception as exc:
        st.error(f"Failed to load network: {exc}")
        st.stop()

    _render_network_form(
        prefix="edit_network",
        network_id=selected_id,
        initial={
            "name": editor_data["name"],
            "description": editor_data.get("description") or "",
            "model_type": editor_data.get("consensus_type", "pow"),
            "difficulty": editor_data["pow_difficulty"],
            "block_capacity": editor_data[
                "max_transactions_per_block"
            ],
            "block_interval_ms": (
                editor_data.get("target_block_time_ms") or 1_000
            ),
            "fee_rate": editor_data["transaction_fee_rate"],
            "base_mining_time_ms": editor_data["base_mining_time_ms"],
            "link_availability": (
                editor_data.get("parameters_json", {}).get(
                    "link_availability_probability",
                    1.0,
                )
            ),
            "packet_delivery": (
                editor_data.get("parameters_json", {}).get(
                    "packet_delivery_success_probability",
                    1.0,
                )
            ),
            "base_iot_reward": editor_data["base_iot_reward"],
            "feedback_weight": editor_data["feedback_weight"],
            "reward_entrypoint": editor_data["reward_entrypoint"],
            "reward_code": editor_data["reward_code"],
            "logic_entrypoint": editor_data["logic_entrypoint"],
            "logic_code": editor_data["logic_code"],
        },
    )
    action_columns = st.columns([1, 1, 4])
    with action_columns[0]:
        export_json = export_configuration(database, "network", selected_id)
        st.download_button(
            "Export JSON",
            data=export_json,
            file_name=f"network-{selected_id}.json",
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
            confirm_delete(
                selected_id,
                network_by_id[selected_id]["name"],
            )

st.divider()
with st.expander("Import a network configuration"):
    uploaded_bundle = st.file_uploader(
        "Network JSON bundle",
        type=["json"],
        key="network_import_file",
    )
    conflict_policy = st.selectbox(
        "Name conflict policy",
        ["rename", "error", "replace"],
        key="network_conflict_policy",
    )
    if st.button(
        "Import network",
        disabled=uploaded_bundle is None,
        icon=":material/upload:",
    ):
        try:
            payload = uploaded_bundle.getvalue().decode("utf-8")
            imported = import_configuration(database, payload, conflict=conflict_policy)
        except Exception as exc:
            st.error(f"Failed to import network: {exc}")
        else:
            st.success(
                f"Imported network #{imported['network_config_id']}."
            )
            st.rerun()
