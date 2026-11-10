"""Standalone-only state transitions; external I/O is supplied by the reference fixture."""

from unittest.mock import Mock

from common import comparison_ui as ui
from snowflake.connector import ProgrammingError
from streamlit.testing.v1 import AppTest

from .test_comparison_pages import APP, ask, boundary, choose


def test_missing_optional_reporting_does_not_block_agent_requests(boundary, monkeypatch):
    monkeypatch.delenv("SCOUT_REQUEST_REPORT", raising=False)
    refresh = Mock()
    monkeypatch.setattr(ui.clients, "refresh_answer", refresh)
    app = AppTest.from_file(str(APP)).run()
    choose(app, "agent", "left")
    ask(app, "agent", "left", "No reporting tables installed")
    app.button(key="comparison:agent:metrics:refresh").click().run()
    refresh.assert_not_called()
    assert app.sidebar.metric[0].value == "1"
    assert boundary[1].call_count == 1
    assert not app.exception


def test_failed_optional_enrichment_keeps_direct_metrics_and_never_replays(boundary, monkeypatch):
    monkeypatch.setattr(ui.clients, "refresh_answer", Mock(side_effect=ProgrammingError("Denied")))
    app = AppTest.from_file(str(APP)).run()
    choose(app, "agent", "left")
    ask(app, "agent", "left", "Retain direct usage")
    app.button(key="comparison:agent:metrics:refresh").click().run()
    pane = app.session_state["comparison:agent:left"]
    assert pane.turns[-1].answer.metrics["total_tokens"].value == 5
    assert boundary[1].call_count == 1
    assert any("Refresh run metrics failed" in error.value for error in app.error)
    assert not app.exception


def test_refresh_reconciles_removed_selection_without_erasing_history(boundary, monkeypatch):
    app = AppTest.from_file(str(APP)).run()
    choose(app, "agent", "left")
    ask(app, "agent", "left", "Keep this evidence")
    original = ui.clients.discover
    monkeypatch.setattr(ui.clients, "discover", lambda conn, kind: [obj for obj in original(conn, kind) if obj.name == "TWO"])
    app.button(key="comparison:agent:discover").click().run()
    assert app.selectbox(key="comparison:agent:left:object").value is None
    assert len(app.session_state["comparison:agent:left"].turns) == 1
    assert app.session_state["comparison:agent:metrics"]["ids"] == []
    assert boundary[1].call_count == 1
    assert not app.exception