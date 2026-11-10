"""Local-only browser harness, excluded from the deployable artifact map."""

import json
from types import SimpleNamespace
from uuid import uuid4

from common import comparison_ui as ui

CONTEXT = {
    "account": "SYNTHETIC", "role": "TEST_OWNER", "user": "TEST_VIEWER",
    "secondary_roles": "[]", "database": "DEMO_DB", "schema": "WORKBENCH",
    "warehouse": "DEMO_WH",
}


def synthetic_connection(name, **kwargs):
    return SimpleNamespace(raw_connection=kwargs.get("comparison_channel", name))


def synthetic_query(connection, sql, params=None):
    if "CURRENT_ACCOUNT()" in sql:
        return [dict(CONTEXT)]
    if sql == "SHOW AGENTS IN ACCOUNT":
        return [
            {"database_name": "DEMO_DB", "schema_name": "WORKBENCH", "name": name}
            for name in ("Analyst_A", "Analyst_B")
        ]
    if sql.startswith("DESCRIBE AGENT"):
        return [{"agent_spec": {"models": {"orchestration": "auto"}, "instructions": {
            "response": "Identify synthetic answers explicitly.",
            "sample_questions": [{"question": "Compare this synthetic inventory."}],
        }, "tools": []}}]
    if "DATA_AGENT_RUN" in sql:
        question = json.loads(params[1])["messages"][-1]["content"][0]["text"]
        return [{"response": json.dumps({
            "request_id": str(uuid4()),
            "content": [{"type": "text", "text": f"Synthetic answer from {params[0]}: {question}"}],
            "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
        })}]
    raise ValueError("Synthetic harness: SQL previews, judge calls, and live telemetry are disabled.")


ui.st.connection = synthetic_connection
ui.clients.query = synthetic_query
ui.comparison_connection_mode = lambda: "owner"
ui.render_page()
ui.st.sidebar.warning("Synthetic browser-review data only. No Snowflake connection or billable calls.")