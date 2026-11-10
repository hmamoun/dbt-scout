from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
from common import comparison_ui as ui
from common.comparison import Answer, Definition, Metric, ObjectRef, SqlEvidence
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app" / "streamlit_app.py"
CONTEXT = {"account": "ACCT", "role": "R", "database": "DB", "schema": "S", "warehouse": "WH"}
LIVE_DISCOVER = ui.clients.discover


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.delenv("COMPARISON_CONNECTION_MODE", raising=False)
    conn = Mock()
    connections = {"comparison-owner": Mock(raw_connection=conn)}

    def connect(name, **kwargs):
        name = kwargs.get("comparison_channel", name)
        if name not in connections:
            connections[name] = Mock(raw_connection=Mock())
        return connections[name]

    monkeypatch.setattr(ui.st, "connection", connect)
    monkeypatch.setattr(ui.clients, "context", lambda _: dict(CONTEXT))
    monkeypatch.setenv("SCOUT_REQUEST_REPORT", "DB.S.RPT_AGENT_REQUEST")
    monkeypatch.setattr(
        ui.clients,
        "discover",
        lambda _, kind: [ObjectRef(kind, "DB", "S", "ONE"), ObjectRef(kind, "DB", "S", "TWO")],
    )
    monkeypatch.setattr(
        ui.clients, "definitions", lambda _, obj: [Definition(obj.fqn, "definition")]
    )
    monkeypatch.setattr(ui.clients, "onboarding_questions", lambda _, obj: [])
    monkeypatch.setattr(ui, "load_settings", lambda _: {"default_agent": ""})
    ask = Mock(
        side_effect=lambda *args: Answer(
            text=f"Answer to {args[-1]}",
            sql=[SqlEvidence("select 1")],
            metrics={"total_tokens": Metric(5, "tokens", "fixture", "available")},
        )
    )
    monkeypatch.setattr(ui.clients, "ask_agent", ask)
    return conn, ask, None


def choose(at, kind, side, name="ONE"):
    key = f"comparison:{kind}:{side}"
    at.selectbox(key=f"{key}:object").select(f'"DB"."S"."{name}"').run()


def ask(at, kind, side, question, *, wait=True):
    at.text_area(key=f"comparison:{kind}:{side}:input").input(question)
    at.button(key=f"comparison:{kind}:{side}:ask").click().run()
    if wait:
        finish(at, kind, side)


def finish(at, kind, side):
    pending = f"comparison:{kind}:{side}:pending"
    if pending in at.session_state:
        at.session_state[pending].result(timeout=5)
        at.run()


def test_copy_question_from_other_pane(boundary):
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    choose(at, "agent", "right", "TWO")
    assert not [b for b in at.button if b.key and b.key.startswith("ask-copy-")]
    at.text_area(key="comparison:agent:left:input").input("Shared question").run()
    copy = at.button(key="ask-copy-agent-right")
    assert copy.label == "Copy question from ONE"
    with pytest.raises(KeyError):
        at.button(key="ask-copy-agent-left")
    copy.click().run()
    assert at.text_area(key="comparison:agent:right:input").value == "Shared question"
    assert not [b for b in at.button if b.key and b.key.startswith("ask-copy-")]
    assert not at.exception


def test_metrics_panel_accumulates_once_and_clears_when_either_agent_changes(boundary):
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    choose(at, "agent", "right", "TWO")
    ask(at, "agent", "left", "First")
    ask(at, "agent", "right", "Second")
    ask(at, "agent", "left", "Third")
    state = at.session_state["comparison:agent:metrics"]
    assert len(state["ids"]) == 3
    counts = {metric.label: metric.value for metric in at.metric}
    assert counts == {"ONE": "2", "TWO": "1"}
    totals = next(
        frame.value
        for frame in at.dataframe
        if "ONE" in frame.value.columns
        and "TWO" in frame.value.columns
        and "Measure" in frame.value.columns
        and "Answer: total tokens" in frame.value["Measure"].tolist()
    )
    row = totals.loc[totals["Measure"] == "Answer: total tokens"].iloc[0]
    assert row["ONE"] == 10 and row["TWO"] == 5
    before = len(state["ids"])
    at.run()
    assert len(at.session_state["comparison:agent:metrics"]["ids"]) == before
    assert boundary[1].call_count == 3
    choose(at, "agent", "left", "TWO")
    assert at.session_state["comparison:agent:metrics"]["ids"] == []
    assert len(at.session_state["comparison:agent:left"].turns) == 2
    ask(at, "agent", "right", "New pair")
    assert len(at.session_state["comparison:agent:metrics"]["ids"]) == 1
    choose(at, "agent", "right", "ONE")
    assert at.session_state["comparison:agent:metrics"]["ids"] == []
    assert not at.exception


def test_metrics_are_in_sidebar_below_notes_with_two_main_chat_columns(boundary):
    at = AppTest.from_file(str(APP)).run()
    assert [heading.value for heading in at.sidebar.subheader] == [
        "Testing session",
        "Test metrics",
    ]
    assert at.sidebar.expander[0].label == "Notes and limits"
    assert not any(heading.value == "Test metrics" for heading in at.main.subheader)
    assert len([column for column in at.main.get("column") if column.weight == 0.5]) == 2
    choose(at, "agent", "left")
    ask(at, "agent", "left", "Sidebar metrics")
    assert at.sidebar.metric[0].value == "1"
    assert (
        at.sidebar.button(key="comparison:agent:metrics:refresh").label == "Refresh all run metrics"
    )
    assert any("Measure" in frame.value.columns for frame in at.sidebar.dataframe)
    assert not at.exception


def test_changing_other_agent_excludes_old_inflight_run_from_metrics(boundary):
    entered, release = Event(), Event()

    def blocked(*_):
        entered.set()
        assert release.wait(5)
        return Answer(
            text="Old pair answer",
            metrics={"total_tokens": Metric(5, "tokens", status="available")},
        )

    boundary[1].side_effect = blocked
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    choose(at, "agent", "right", "TWO")
    try:
        ask(at, "agent", "left", "Pending old pair", wait=False)
        assert entered.wait(5)
        choose(at, "agent", "right", "ONE")
    finally:
        release.set()
    finish(at, "agent", "left")
    assert at.session_state["comparison:agent:metrics"]["ids"] == []
    assert at.session_state["comparison:agent:left"].turns[-1].answer.text == "Old pair answer"
    assert not at.exception


def test_heatmap_compares_only_values_within_same_measure():
    import pandas as pd

    assert ui._heatmap_row(pd.Series([None, 0, 10])) == [
        "",
        "background-color: #dbeafe; color: #172554",
        "background-color: #ffedd5; color: #431407",
    ]
    assert ui._heatmap_row(pd.Series([5, 5])) == ["", ""]


def test_refresh_all_metrics_enriches_recorded_runs_without_resubmitting(boundary, monkeypatch):
    refresh = Mock(
        side_effect=lambda _, turn, __: turn.answer.metrics.update(
            {"cortex_credits": Metric(0.25, "credits", status="available")}
        )
    )
    monkeypatch.setattr(ui.clients, "refresh_answer", refresh)
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    choose(at, "agent", "right", "TWO")
    ask(at, "agent", "left", "One")
    ask(at, "agent", "left", "Two")
    at.button(key="comparison:agent:metrics:refresh").click().run()
    assert refresh.call_count == 2
    assert boundary[1].call_count == 2
    assert len(at.session_state["comparison:agent:metrics"]["ids"]) == 2
    at.button(key="comparison:agent:left:reset").click().run()
    assert len(at.session_state["comparison:agent:metrics"]["ids"]) == 2
    assert not at.exception


def test_default_agent_only_initializes_right_pane(boundary, monkeypatch):
    preference = {"default_agent": "db.s.two"}
    monkeypatch.setattr(ui, "load_settings", lambda _: preference)
    at = AppTest.from_file(str(APP)).run()
    assert at.selectbox(key="comparison:agent:left:object").value is None
    assert at.selectbox(key="comparison:agent:right:object").value == '"DB"."S"."TWO"'
    assert at.session_state["comparison:agent:right"].selected.name == "TWO"
    choose(at, "agent", "right", "ONE")
    at.run()
    assert at.selectbox(key="comparison:agent:right:object").value == '"DB"."S"."ONE"'
    preference["default_agent"] = "DB.S.ONE"
    at.run()
    assert at.selectbox(key="comparison:agent:left:object").value is None
    assert boundary[1].call_count == 0
    assert not at.exception


def test_inaccessible_default_does_not_select_another_agent(boundary, monkeypatch):
    monkeypatch.setattr(ui, "load_settings", lambda _: {"default_agent": "OTHER.S.TWO"})
    at = AppTest.from_file(str(APP)).run()
    assert at.selectbox(key="comparison:agent:left:object").value is None
    assert at.selectbox(key="comparison:agent:right:object").value is None
    assert any("saved default agent is not accessible" in warning.value for warning in at.warning)
    assert not at.exception


def test_onboarding_picker_fills_only_its_pane_without_submitting(boundary, monkeypatch):
    lookup = Mock(
        side_effect=lambda _, obj: [f"{obj.name} question one?", f"{obj.name} question two?"]
    )
    monkeypatch.setattr(ui.clients, "onboarding_questions", lookup)
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    choose(at, "agent", "right", "TWO")
    left = 'comparison:agent:left:onboarding:"DB"."S"."ONE"'
    right = 'comparison:agent:right:onboarding:"DB"."S"."TWO"'
    assert at.selectbox(key=left).options == ["ONE question one?", "ONE question two?"]
    at.selectbox(key=left).select("ONE question two?").run()
    assert at.text_area(key="comparison:agent:left:input").value == "ONE question two?"
    assert at.text_area(key="comparison:agent:right:input").value == ""
    at.selectbox(key=right).select("TWO question one?").run()
    assert at.text_area(key="comparison:agent:right:input").value == "TWO question one?"
    boundary[1].assert_not_called()
    at.run()
    assert lookup.call_count == 2
    ask(at, "agent", "left", "ONE question two?")
    assert at.session_state["comparison:agent:left"].turns[-1].question == "ONE question two?"
    choose(at, "agent", "left", "TWO")
    assert at.selectbox(key='comparison:agent:left:onboarding:"DB"."S"."TWO"').value is None
    at.sidebar.button(key="comparison:agent:discover").click().run()
    assert lookup.call_count > 2
    assert not at.exception


def test_onboarding_access_failure_does_not_disable_ask(boundary, monkeypatch):
    lookup = Mock(side_effect=ValueError("Metadata denied"))
    monkeypatch.setattr(ui.clients, "onboarding_questions", lookup)
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    assert any("Metadata denied" in error.value for error in at.error)
    assert not at.button(key="comparison:agent:left:ask").disabled
    at.run()
    assert lookup.call_count == 1
    assert any("Refresh available objects to retry" in caption.value for caption in at.caption)
    at.sidebar.button(key="comparison:agent:discover").click().run()
    assert lookup.call_count == 2
    assert not at.exception


@pytest.mark.parametrize(
    "kind,page",
    [("agent", "8_Compare_Agents.py")],
)
def test_independent_chats_reruns_comparison_and_reset(boundary, kind, page):
    _, agent, analyst = boundary
    at = AppTest.from_file(str(APP)).run()
    assert not at.exception
    assert any("Pick an object" in caption.value for caption in at.caption)
    assert at.sidebar.button(key=f"comparison:{kind}:discover")
    assert any("1,000 rows" in caption.value for caption in at.sidebar.caption)
    assert not any("1,000 rows" in info.value for info in at.main.info)
    choose(at, kind, "left")
    choose(at, kind, "right", "TWO")
    ask(at, kind, "left", "Left question")
    assert not at.exception
    ask(at, kind, "right", "Different right question")
    assert not at.exception
    left = at.session_state[f"comparison:{kind}:left"]
    right = at.session_state[f"comparison:{kind}:right"]
    assert left.turns[0].question == "Left question"
    assert right.turns[0].question == "Different right question"
    assert left.turns[0].object.name == "ONE" and right.turns[0].object.name == "TWO"
    client = agent if kind == "agent" else analyst.ask
    assert client.call_count == 2
    at.run()
    at.button(key=f"comparison:{kind}:refresh_metrics").click().run()
    assert client.call_count == 2
    before = right.conversation_id
    at.button(key=f"comparison:{kind}:left:reset").click().run()
    assert at.session_state[f"comparison:{kind}:right"].conversation_id == before
    assert len(at.session_state[f"comparison:{kind}:left"].turns) == 1
    assert not at.exception
    assert any("Exploratory comparison" in warning.value for warning in at.warning)
    assert len(at.get("download_button")) == 2


def test_object_change_preserves_evidence_and_resets_context(boundary):
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    ask(at, "agent", "left", "question")
    key = "comparison:agent:left"
    at.selectbox(key=f"{key}:object").select('"DB"."S"."TWO"').run()
    pane = at.session_state[key]
    assert not pane.active_ids
    assert pane.turns[0].object.name == "ONE"
    assert pane.selected.name == "TWO"
    assert not at.exception


def test_lookup_lists_fully_qualified_names_of_page_kind(boundary, monkeypatch):
    monkeypatch.setattr(
        ui.clients,
        "discover",
        lambda _, kind: [
            ObjectRef(kind, "OTHER", "SALES", "Sales_Agent"),
            ObjectRef(kind, "DB", "S", "ONE"),
        ],
    )
    at = AppTest.from_file(str(APP)).run()
    box = at.selectbox(key="comparison:agent:left:object")
    assert box.label == "Agent 1"
    assert box.options == ["DB.S.ONE", "OTHER.SALES.Sales_Agent"]
    assert "2 agents" in box.placeholder
    box.select('"OTHER"."SALES"."Sales_Agent"').run()
    assert at.session_state["comparison:agent:left"].selected.name == "Sales_Agent"
    assert at.session_state["comparison:agent:right"].selected is None
    assert any("2 agents available" in caption.value for caption in at.sidebar.caption)
    assert not at.exception


def test_empty_discovery_and_failure_are_distinct(boundary, monkeypatch):
    monkeypatch.setattr(ui.clients, "discover", lambda *args: [])
    page = str(APP)
    at = AppTest.from_file(page).run()
    assert any("returned no agents" in warning.value for warning in at.warning)
    monkeypatch.setattr(ui.clients, "discover", Mock(side_effect=ValueError("Discovery failed")))
    at.button(key="comparison:agent:discover").click().run()
    assert any("Discovery failed" in error.value for error in at.error)
    assert not at.exception


def test_both_panes_read_live_show_and_ignore_stale_object_list(boundary, monkeypatch):
    rows = [
        {"database_name": "DB", "schema_name": "S", "name": name}
        for name in ("ONE", "TWO", "NEW_WITHOUT_LOGS")
    ]
    query = Mock(side_effect=lambda *_: list(rows))
    monkeypatch.setattr(ui.clients, "discover", LIVE_DISCOVER)
    monkeypatch.setattr(ui.clients, "query", query)
    at = AppTest.from_file(str(APP)).run()
    assert not at.exception
    expected = ["DB.S.NEW_WITHOUT_LOGS", "DB.S.ONE", "DB.S.TWO"]
    for side in ("left", "right"):
        assert at.selectbox(key=f"comparison:agent:{side}:object").options == expected
    query.assert_called_once_with(boundary[0], "SHOW AGENTS IN ACCOUNT")
    at.session_state["comparison:agent:objects"] = [
        ObjectRef("agent", "DB", "S", name) for name in ("ONE", "TWO")
    ]
    rows.append({"database_name": "OTHER", "schema_name": "S", "name": "FOUR"})
    at.run()
    for side in ("left", "right"):
        assert at.selectbox(key=f"comparison:agent:{side}:object").options == [
            *expected,
            "OTHER.S.FOUR",
        ]
    assert query.call_count == 2
    boundary[1].assert_not_called()
    assert not at.exception


@pytest.mark.parametrize("mode", ["local", "owner"])
def test_empty_discovery_explains_actual_connection_mode(boundary, monkeypatch, mode):
    monkeypatch.setenv("COMPARISON_CONNECTION_MODE", mode)
    if mode == "local":
        for key in ("account", "user", "role", "warehouse", "database", "schema"):
            monkeypatch.setenv(f"SNOWFLAKE_{key.upper()}", f"test-{key}")
    monkeypatch.setattr(ui.clients, "discover", lambda *args: [])
    at = AppTest.from_file(str(APP)).run()
    message = next(warning.value for warning in at.warning if "returned no agents" in warning.value)
    assert "Primary role: R" in message
    if mode == "local":
        assert "local developer SSO connection" in message
        assert "Caller grants do not apply" in message
        assert "app owner's role" not in message
        assert "for this caller connection" not in message
    else:
        assert "for this owner-rights connection" in message
        assert "app owner's privileges" in message
        assert "No alternate credentials" in message
    boundary[1].assert_not_called()
    assert not at.exception


def test_ask_failure_is_preserved_and_no_implicit_retry(boundary):
    _, agent, _ = boundary
    agent.side_effect = ValueError("Answer unavailable")
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    ask(at, "agent", "left", "question")
    assert at.session_state["comparison:agent:left"].turns[0].error
    assert "Answer unavailable" in at.session_state["comparison:agent:left"].turns[0].error
    assert agent.call_count == 1
    at.run()
    assert agent.call_count == 1
    assert not at.exception


def test_empty_question_is_not_submitted(boundary):
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    ask(at, "agent", "left", "   ")
    boundary[1].assert_not_called()
    assert any("nonempty question" in error.value for error in at.error)
    assert not at.session_state["comparison:agent:left"].turns
    assert "comparison:agent:left:pending" not in at.session_state
    assert not at.exception


def test_compact_judge_evidence_and_collapsed_differences(boundary, monkeypatch):
    import json

    monkeypatch.setattr(
        ui.clients, "definitions", lambda _, obj: [Definition(obj.fqn, "x" * 45000)]
    )
    judge = Mock(return_value={"text": "Compact comparison", "usage": {}, "model": "fixture"})
    monkeypatch.setattr(ui.clients, "judge", judge)
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    choose(at, "agent", "right", "TWO")
    ask(at, "agent", "left", "left")
    ask(at, "agent", "right", "right")
    differences = next(
        e for e in at.expander if e.label == "Answer, SQL and definition differences"
    )
    assert differences.proto.expanded is False
    assert not at.button(key="comparison:agent:judge").disabled
    at.button(key="comparison:agent:judge").click().run()
    evidence = json.loads(judge.call_args.args[3])
    assert "text" not in evidence["left"]["definitions"][0]
    assert evidence["left"]["definitions"][0]["text_characters"] == 45000
    assert "Definition text omitted" in evidence["left"]["evidence_limitations"]
    at.checkbox(key="comparison:agent:judge_definitions").check().run()
    assert at.button(key="comparison:agent:judge").disabled
    assert any("stale" in info.value for info in at.info)
    assert judge.call_count == 1
    assert not at.exception


def test_judge_failure_is_visible_without_replaying_agents(boundary, monkeypatch):
    monkeypatch.setattr(
        ui.clients, "judge", Mock(side_effect=ValueError("Judge returned no supported answer."))
    )
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    choose(at, "agent", "right", "TWO")
    ask(at, "agent", "left", "left")
    ask(at, "agent", "right", "right")
    at.button(key="comparison:agent:judge").click().run()
    assert any("Judge returned no supported answer" in error.value for error in at.error)
    assert boundary[1].call_count == 2
    assert not at.exception


def test_snowflake_judge_failure_exposes_reason_and_query_id_without_retry(boundary, monkeypatch):
    from snowflake.connector import ProgrammingError

    judge = Mock(
        side_effect=ProgrammingError(
            msg="Request failed for external function: selected model is not enabled.",
            errno=512513,
            sqlstate="P0000",
            sfqid="judge-query-id",
        )
    )
    monkeypatch.setattr(ui.clients, "judge", judge)
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    choose(at, "agent", "right", "TWO")
    ask(at, "agent", "left", "One")
    ask(at, "agent", "right", "Two")
    at.button(key="comparison:agent:judge").click().run()
    assert any("512513" in error.value for error in at.error)
    assert any("selected model is not enabled" in text.value for text in at.text)
    assert any("judge-query-id" in caption.value for caption in at.caption)
    assert any("SQLSTATE: P0000" in caption.value for caption in at.caption)
    at.run()
    judge.assert_called_once()
    assert boundary[1].call_count == 2
    assert not at.exception


def test_explicit_sql_and_judge_are_not_repeated(boundary, monkeypatch):
    from common.comparison import Execution

    execute = Mock(
        side_effect=lambda *args, **kwargs: Execution(
            0,
            "select 1",
            query_id="q",
            columns=["N"],
            rows=[[1]],
            metrics={"warehouse_compute": Metric(0.125, "credits", "fixture", "available")},
        )
    )
    judge = Mock(return_value={"text": "Compare evidence", "usage": {}, "model": "fixture"})
    monkeypatch.setattr(ui.clients, "execute_preview", execute)
    monkeypatch.setattr(ui.clients, "judge", judge)
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    choose(at, "agent", "right")
    ask(at, "agent", "left", "question")
    ask(at, "agent", "right", "question")
    assert execute.call_count == judge.call_count == 0
    [button for button in at.button if button.label == "Run SQL 1"][0].click().run()
    [button for button in at.button if button.label == "Run SQL 1"][1].click().run()
    at.button(key="comparison:agent:judge").click().run()
    assert execute.call_count == 2 and judge.call_count == 1
    assert execute.call_args.kwargs == {"close_connection": False}
    at.run()
    assert execute.call_count == 2 and judge.call_count == 1
    at.text_area(key="comparison:agent:prompt").input("Changed prompt").run()
    assert any("stale" in info.value for info in at.info)
    assert not at.exception


def test_missing_connection_shows_explicit_error(monkeypatch):
    from streamlit.errors import StreamlitAPIException

    monkeypatch.setattr(ui.st, "connection", Mock(side_effect=StreamlitAPIException("missing")))
    at = AppTest.from_file(str(APP)).run()
    assert any("Snowflake connection unavailable" in error.value for error in at.error)
    assert not at.exception


def test_comparison_connections_use_isolated_owner_rights(boundary, monkeypatch):
    from common.common import ComparisonSnowflakeConnection

    connections = {
        name: Mock(raw_connection=Mock())
        for name in (
            "comparison-owner",
            "comparison-owner-preview",
            "comparison-owner-agent-left",
            "comparison-owner-agent-right",
        )
    }
    connect = Mock(side_effect=lambda name, **kwargs: connections[kwargs["comparison_channel"]])
    monkeypatch.setattr(ui.st, "connection", connect)
    at = AppTest.from_file(str(APP)).run()
    assert not at.exception
    assert {call.args[0] for call in connect.call_args_list} == {"snowflake"}
    assert {call.kwargs["comparison_channel"] for call in connect.call_args_list} == set(
        connections
    )
    assert ComparisonSnowflakeConnection.scope() == "session"
    for call in connect.call_args_list:
        assert call.kwargs["type"] is ComparisonSnowflakeConnection
    assert any("Owner-rights account" in caption.value for caption in at.sidebar.caption)
    for connection in connections.values():
        connection.raw_connection.cursor.assert_not_called()
    choose(at, "agent", "left")
    choose(at, "agent", "right", "TWO")
    ask(at, "agent", "left", "Left owner question")
    ask(at, "agent", "right", "Right owner question")
    assert (
        boundary[1].call_args_list[0].args[0]
        is connections["comparison-owner-agent-left"].raw_connection
    )
    assert (
        boundary[1].call_args_list[1].args[0]
        is connections["comparison-owner-agent-right"].raw_connection
    )


def test_empty_owner_discovery_explains_owner_access(boundary, monkeypatch):
    discover = Mock(return_value=[])
    monkeypatch.setattr(ui.clients, "discover", discover)
    at = AppTest.from_file(str(APP)).run()
    assert any(
        "app owner's privileges" in warning.value and "Primary role: R" in warning.value
        for warning in at.warning
    )
    discover.assert_called_once_with(boundary[0], "agent")
    assert not at.exception


def test_owner_connection_channel_is_not_forwarded_to_connector(monkeypatch):
    from common.common import ComparisonSnowflakeConnection
    from streamlit.connections import SnowflakeConnection

    connect = Mock()
    monkeypatch.setattr(SnowflakeConnection, "_connect", connect)
    connection = ComparisonSnowflakeConnection(
        connection_name="snowflake", comparison_channel="comparison-owner-agent-left"
    )
    connect.assert_called_once_with()
    assert connection.raw_connection is connect.return_value


def test_owner_failure_does_not_fall_back_to_caller(monkeypatch):
    from streamlit.errors import StreamlitAPIException

    connect = Mock(side_effect=StreamlitAPIException("Owner connection unavailable"))
    monkeypatch.setattr(ui.st, "connection", connect)
    at = AppTest.from_file(str(APP)).run()
    assert not at.exception
    assert all(call.args[0] == "snowflake" for call in connect.call_args_list)
    assert any("No alternate credentials" in error.value for error in at.error)


def test_owner_connection_cache_isolates_channels_and_viewer_sessions(monkeypatch):
    from uuid import uuid4

    from streamlit.connections import SnowflakeConnection
    from streamlit.runtime.caching.cache_resource_api import clear_session_cache

    monkeypatch.delenv("COMPARISON_CONNECTION_MODE", raising=False)
    connect = Mock(side_effect=lambda **_: Mock())
    monkeypatch.setattr(SnowflakeConnection, "_connect", connect)
    script = """
import streamlit as st
from common.common import get_comparison_connections
from streamlit.runtime.scriptrunner import get_script_run_ctx
get_script_run_ctx().session_id = st.session_state["test_session_id"]
main, preview = get_comparison_connections()
left, _ = get_comparison_connections("left")
right, _ = get_comparison_connections("right")
st.session_state["connections"] = (main, preview, left, right)
"""
    first = AppTest.from_string(script)
    first.session_state["test_session_id"] = str(uuid4())
    first.run()
    assert not first.exception
    connections = first.session_state["connections"]
    assert len({id(connection.raw_connection) for connection in connections}) == 4
    first.run()
    assert first.session_state["connections"] == connections
    second = AppTest.from_string(script)
    second.session_state["test_session_id"] = str(uuid4())
    second.run()
    assert not second.exception
    assert not {id(connection.raw_connection) for connection in connections} & {
        id(connection.raw_connection) for connection in second.session_state["connections"]
    }
    assert connect.call_count == 8
    assert all(call.kwargs == {} for call in connect.call_args_list)
    clear_session_cache(first.session_state["test_session_id"])
    clear_session_cache(second.session_state["test_session_id"])


def test_explicit_local_mode_uses_isolated_developer_connections(boundary, monkeypatch):
    from common.common import ComparisonSnowflakeConnection

    monkeypatch.setenv("COMPARISON_CONNECTION_MODE", "local")
    for key in ("account", "user", "role", "warehouse", "database", "schema"):
        monkeypatch.setenv(f"SNOWFLAKE_{key.upper()}", f"test-{key}")
    connect = Mock(return_value=Mock(raw_connection=boundary[0]))
    monkeypatch.setattr(ui.st, "connection", connect)
    at = AppTest.from_file(str(APP)).run()
    assert not at.exception
    assert {call.args[0] for call in connect.call_args_list} == {
        "comparison-local",
        "comparison-local-preview",
        "comparison-local-agent-left",
        "comparison-local-agent-right",
    }
    for call in connect.call_args_list:
        assert call.kwargs["type"] is ComparisonSnowflakeConnection
        assert call.kwargs["authenticator"] == "externalbrowser"
        assert call.kwargs["role"] == "test-role"
    assert any("Local developer SSO" in warning.value for warning in at.sidebar.warning)
    choose(at, "agent", "left")
    ask(at, "agent", "left", "Local SQL question")
    boundary[1].assert_called_once()


def test_invalid_connection_mode_does_not_connect(boundary, monkeypatch):
    monkeypatch.setenv("COMPARISON_CONNECTION_MODE", "invalid")
    connect = Mock()
    monkeypatch.setattr(ui.st, "connection", connect)
    at = AppTest.from_file(str(APP)).run()
    assert not at.exception
    connect.assert_not_called()
    assert any("connection unavailable" in error.value for error in at.error)


def test_local_mode_missing_configuration_does_not_connect(boundary, monkeypatch):
    monkeypatch.setenv("COMPARISON_CONNECTION_MODE", "local")
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
    connect = Mock()
    monkeypatch.setattr(ui.st, "connection", connect)
    at = AppTest.from_file(str(APP)).run()
    assert not at.exception
    connect.assert_not_called()
    assert any("configure SNOWFLAKE_ACCOUNT" in error.value for error in at.error)


def test_definition_drift_starts_new_context(boundary, monkeypatch):
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    ask(at, "agent", "left", "first")
    previous = at.session_state["comparison:agent:left"].conversation_id
    monkeypatch.setattr(ui.clients, "definitions", lambda _, obj: [Definition(obj.fqn, "changed")])
    ask(at, "agent", "left", "second")
    pane = at.session_state["comparison:agent:left"]
    assert len(pane.turns) == 2
    assert len(pane.active_ids) == 1
    assert pane.conversation_id != previous
    assert pane.turns[-1].context_questions == []
    assert any("definition changed" in warning.value for warning in at.warning)


def test_tool_query_evidence_is_not_replayed_or_double_counted(boundary):
    _, agent, _ = boundary
    agent.side_effect = lambda *args: Answer(
        text="Answer",
        sql=[
            SqlEvidence("select 1", query_id="same-query", result_columns=["N"], result_rows=[[1]]),
            SqlEvidence("select 1", query_id="same-query"),
        ],
    )
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    ask(at, "agent", "left", "question")
    pane = at.session_state["comparison:agent:left"]
    assert len(pane.turns[0].executions) == 1
    assert pane.turns[0].executions[0].origin == "service-reported tool execution"
    assert not at.exception


def test_artifact_globs_cover_new_modules_and_pages():
    manifest = (ROOT / "snowflake.yml").read_text()
    assert "common/comparison_ui.py" in manifest and "requirements.txt" in manifest
    for path in (ROOT / "app" / "common").glob("*.py"):
        assert f"common/{path.name}" in manifest
    home = APP.read_text()
    assert "render_page()" in home
    assert "navigation" not in home
    assert not (ROOT / "app" / "pages").exists()


@pytest.mark.parametrize("failing_side", [None, "left", "right"])
def test_both_agents_start_before_either_finishes(boundary, failing_side):
    _, agent, _ = boundary
    started = {side: Event() for side in ("left", "right")}
    release = {side: Event() for side in ("left", "right")}
    connections = {}

    def blocked(connection, pane, question):
        connections[question] = connection
        started[question].set()
        if not release[question].wait(timeout=10):
            raise TimeoutError("Test did not release agent")
        if question == failing_side:
            raise ValueError("Answer unavailable")
        pane.thread_id = 1 if question == "left" else 2
        return Answer(text=f"{question} answer")

    agent.side_effect = blocked
    at = AppTest.from_file(str(APP)).run()
    choose(at, "agent", "left")
    choose(at, "agent", "right", "TWO")
    try:
        ask(at, "agent", "left", "left", wait=False)
        assert started["left"].wait(timeout=2)
        assert at.button(key="comparison:agent:left:ask").disabled
        assert not at.button(key="comparison:agent:right:ask").disabled
        assert at.selectbox(key="comparison:agent:left:object").disabled
        assert at.button(key="comparison:agent:left:reset").disabled
        ask(at, "agent", "right", "right", wait=False)
        assert started["right"].wait(timeout=2)
        assert connections["left"] is not connections["right"]
        assert not at.session_state["comparison:agent:left"].turns
        assert not at.session_state["comparison:agent:right"].turns
        assert not release["left"].is_set()
        assert at.button(key="comparison:agent:right:ask").disabled
        at.run()
        assert agent.call_count == 2
        release["right"].set()
        finish(at, "agent", "right")
        right = at.session_state["comparison:agent:right"]
        assert len(right.turns) == 1
        assert bool(right.turns[0].error) == (failing_side == "right")
        assert at.button(key="comparison:agent:left:ask").disabled
        assert not at.button(key="comparison:agent:right:ask").disabled
        release["left"].set()
        finish(at, "agent", "left")
        left = at.session_state["comparison:agent:left"]
        assert len(left.turns) == 1
        assert bool(left.turns[0].error) == (failing_side == "left")
        if failing_side != "left":
            assert left.thread_id == 1
        if failing_side != "right":
            assert right.thread_id == 2
        at.run()
        assert agent.call_count == 2
        assert not at.exception
    finally:
        for event in release.values():
            event.set()
        for side in ("left", "right"):
            finish(at, "agent", side)
