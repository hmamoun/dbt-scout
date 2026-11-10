"""Deployment smoke test. Documentation: dbt_scout/docs/materializations.md#sample-streamlit."""

import os

import pandas as pd
import streamlit as st


st.set_page_config(page_title="Scout Deployment Check", page_icon=":material/fact_check:", layout="wide")
st.title("Scout Deployment Check")
local_preview = os.environ.get("SCOUT_LOCAL_PREVIEW") == "1"

if local_preview:
    context = {"Database": "LOCAL", "Schema": "PREVIEW", "Role": "LOCAL_PREVIEW", "Warehouse": "NOT_CONNECTED"}
else:
    try:
        connection = st.connection("snowflake")
        runtime = connection.query(
            'select current_database() as "Database", current_schema() as "Schema", '
            'current_role() as "Role", current_warehouse() as "Warehouse"',
            ttl=0,
        )
        context = runtime.iloc[0].to_dict()
    except Exception:
        st.error("Snowflake session unavailable.")
        st.stop()

st.caption("Local preview" if local_preview else "Snowflake session connected")
if st.button("Refresh", icon=":material/refresh:"):
    st.rerun()

runtime_tab, sample_tab = st.tabs(["Runtime", "Sample Data"])
with runtime_tab:
    st.dataframe(pd.DataFrame(context.items(), columns=["Property", "Value"]), hide_index=True)
    st.subheader("Deployment Checks")
    st.dataframe(
        pd.DataFrame([
            {"Check": "Python entrypoint", "Status": "Passed"},
            {"Check": "Streamlit widgets", "Status": "Passed"},
            {"Check": "Snowflake query", "Status": "Not run" if local_preview else "Passed"},
        ]),
        hide_index=True,
    )

with sample_tab:
    st.subheader("Synthetic Object Inventory")
    inventory = pd.DataFrame({"Object Type": ["Tables", "Views", "Agents", "Streamlit Apps"], "Count": [12, 8, 1, 1]})
    selected = st.multiselect("Object Types", inventory["Object Type"], default=inventory["Object Type"].tolist())
    filtered = inventory[inventory["Object Type"].isin(selected)]
    if filtered.empty:
        st.info("No object types selected.")
    else:
        st.bar_chart(filtered, x="Object Type", y="Count", color="#007f73")
        st.dataframe(filtered, hide_index=True)