"""Session-local preferences and optional explicit enrichment; no settings tables."""

import os
import re

import streamlit as st


def load_settings(_session=None) -> dict[str, str]:
    return {"default_agent": str(st.session_state.get("scout:default_agent", ""))}


def optional_report_fqn() -> str | None:
    value = os.environ.get("SCOUT_REQUEST_REPORT", "").strip()
    if not value:
        return None
    if not re.fullmatch(r"[A-Z0-9_$]+\.[A-Z0-9_$]+\.[A-Z0-9_$]+", value):
        raise ValueError("SCOUT_REQUEST_REPORT must be an uppercase DATABASE.SCHEMA.RELATION identifier.")
    return value