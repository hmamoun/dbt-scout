import json
from unittest.mock import Mock

import pytest
from common import comparison_clients as clients
from common.comparison import Execution, ObjectRef, Pane
from snowflake.connector import ProgrammingError

CONTEXT = {"account": "ACCT", "role": "R", "database": "DB", "schema": "S", "warehouse": "WH"}


class Cursor:
    def __init__(self):
        self.description = [("value",)]
        self.sfqid = "query-1"
        self.commands = []
        self.closed = False

    def execute(self, command, params=None, **kwargs):
        self.commands.append((command, params, kwargs))
        return self

    def fetchall(self):
        return [(42,)]

    def fetchmany(self, size):
        return [(number,) for number in range(size)]

    def close(self):
        self.closed = True


def test_query_binds_qmark_and_closes_cursor():
    cursor = Cursor()
    conn = Mock()
    conn.cursor.return_value = cursor
    assert clients.query(conn, "select %s", ("$$';",)) == [{"value": 42}]
    assert cursor.commands == [("select ?", ("$$';",), {})]
    assert cursor.closed


def test_query_preserves_pyformat_for_existing_snowpark_connection():
    cursor = Cursor()
    conn = Mock(is_pyformat=True)
    conn.cursor.return_value = cursor
    assert clients.query(conn, "select %s", ("$$';",)) == [{"value": 42}]
    assert cursor.commands == [("select %s", ("$$';",), {})]
    assert cursor.closed


def test_parse_all_sql_suggestions_tools_and_omit_thinking():
    payload = {
        "request_id": "req1",
        "content": [
            {"type": "thinking", "thinking": {"text": "PRIVATE"}},
            {"type": "text", "text": "Answer"},
            {"type": "tool_use", "tool_use": {"name": "analyst", "input": "PRIVATE"}},
            {
                "type": "tool_results",
                "tool_results": {
                    "content": [
                        {
                            "type": "sql",
                            "statement": "select 1",
                            "confidence": {"verified_query_used": {"name": "known"}},
                        },
                        {
                            "type": "tool_result",
                            "content": [
                                {
                                    "type": "sql",
                                    "statement": "select 2",
                                    "is_verified_query": False,
                                    "query_id": "q2",
                                }
                            ],
                        },
                    ]
                },
            },
            {"type": "suggestions", "suggestions": ["Try this"]},
        ],
        "warnings": [{"code": "denied", "message": "Tool missing"}],
        "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    }
    answer = clients.parse_answer(payload, "agent")
    assert answer.text == "Answer"
    assert len(answer.sql) == 2
    assert answer.sql[0].verified and answer.sql[0].verified_name == "known"
    assert answer.sql[1].verified is False and answer.sql[1].query_id == "q2"
    assert answer.metrics["total_tokens"].value == 5
    assert answer.tools == ["analyst"]
    assert answer.suggestions == ["Try this"]
    assert "PRIVATE" not in repr(answer)
    assert "Tool missing" in answer.warnings[0]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"content": {}},
        {"content": [{"type": "text", "text": 1}]},
        {"content": [{"type": "sql"}]},
        {"content": [{"type": "thinking"}]},
        {"content": [{"type": "suggestions", "suggestions": [1]}]},
    ],
)
def test_malformed_responses_are_not_success(payload):
    with pytest.raises(ValueError):
        clients.parse_answer(payload, "agent")


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "Agent execution timed out.", "code": "399999"},
        {"status": "error", "message": "Agent execution timed out.", "code": "399999"},
        {"error": {"message": "Agent execution timed out.", "code": "399999"}},
    ],
)
def test_agent_service_error_preserves_reason_and_code(payload):
    with pytest.raises(ValueError, match=r"399999.*Agent execution timed out"):
        clients.parse_answer(payload, "agent")


def test_top_level_public_content_is_not_mistaken_for_message_envelope():
    answer = clients.parse_answer(
        {"message": "complete", "content": [{"type": "text", "text": "Public answer"}]},
        "agent",
    )
    assert answer.text == "Public answer"


def test_service_error_does_not_advance_conversation(monkeypatch):
    pane = Pane(selected=ObjectRef("agent", "DB", "S", "A"))
    monkeypatch.setattr(
        clients,
        "query",
        Mock(return_value=[{"response": '{"message":"Agent timed out","code":"E1"}'}]),
    )
    with pytest.raises(ValueError, match="Agent timed out"):
        clients.ask_agent(Mock(), pane, "question")
    assert pane.messages == []
    assert pane.thread_id is None


def test_onboarding_questions_are_read_from_selected_agent(monkeypatch):
    fake = Mock(
        return_value=[
            {
                "agent_spec": json.dumps(
                    {
                        "instructions": {
                            "sample_questions": [
                                {"question": " First question? "},
                                {"question": "Second question?"},
                                {"question": "First question?"},
                            ]
                        }
                    }
                )
            }
        ]
    )
    monkeypatch.setattr(clients, "query", fake)
    obj = ObjectRef("agent", "DB", "S", "A")
    assert clients.onboarding_questions(Mock(), obj) == ["First question?", "Second question?"]
    assert fake.call_args.args[1] == 'DESCRIBE AGENT "DB"."S"."A"'
    fake.return_value = [{"agent_spec": {"instructions": {}}}]
    assert clients.onboarding_questions(Mock(), obj) == []


@pytest.mark.parametrize(
    "instructions",
    [
        None,
        {"sample_questions": {}},
        {"sample_questions": [{"question": ""}]},
        {"sample_questions": ["not a question object"]},
    ],
)
def test_invalid_onboarding_is_not_silently_empty(monkeypatch, instructions):
    monkeypatch.setattr(
        clients, "query", Mock(return_value=[{"agent_spec": {"instructions": instructions}}])
    )
    with pytest.raises(ValueError, match="(instructions|onboarding)"):
        clients.onboarding_questions(Mock(), ObjectRef("agent", "DB", "S", "A"))


def test_agent_parameterization_threads_and_full_fallback_context(monkeypatch):
    obj = ObjectRef("agent", "DB", "S", "A")
    pane = Pane(selected=obj)
    responses = [
        {
            "content": [{"type": "text", "text": "first"}],
            "metadata": {"thread_id": 12, "message_id": 31},
        },
        {"content": [{"type": "text", "text": "second"}]},
        {"content": [{"type": "text", "text": "third"}]},
    ]
    calls = []

    def fake_query(conn, sql, params):
        calls.append((sql, params, json.loads(params[1])))
        return [{"response": json.dumps(responses.pop(0))}]

    monkeypatch.setattr(clients, "query", fake_query)
    clients.ask_agent(Mock(), pane, "quote '$$ question")
    assert "quote" not in calls[0][0]
    assert calls[0][1][2] is True
    assert pane.thread_id == 12 and pane.parent_message_id == 31
    clients.ask_agent(Mock(), pane, "follow up")
    assert calls[1][2]["thread_id"] == 12 and calls[1][2]["parent_message_id"] == 31
    assert len(calls[1][2]["messages"]) == 1 and calls[1][1][2] is False
    assert pane.thread_id is None
    clients.ask_agent(Mock(), pane, "third question")
    assert len(calls[2][2]["messages"]) == 5
    assert calls[2][1][2] is False


def test_agent_without_server_ids_keeps_history_and_disables_auto_thread(monkeypatch):
    pane = Pane(selected=ObjectRef("agent", "DB", "S", "A"))
    query = Mock(return_value=[{"response": {"content": [{"type": "text", "text": "Answer"}]}}])
    monkeypatch.setattr(clients, "query", query)
    for question in ("First", "Second", "Third"):
        clients.ask_agent(Mock(), pane, question)
    calls = query.call_args_list
    assert [call.args[2][2] for call in calls] == [True, False, False]
    assert [len(json.loads(call.args[2][1])["messages"]) for call in calls] == [1, 3, 5]
    assert [message["role"] for message in pane.messages] == ["user", "assistant"] * 3


@pytest.mark.parametrize("close_connection", [True, False])
def test_explicit_preview_bounds_timeout_and_isolation(monkeypatch, close_connection):
    cursor = Cursor()
    conn = Mock()
    conn.cursor.return_value = cursor
    monkeypatch.setattr(clients, "context", lambda _: CONTEXT)
    execution = clients.execute_preview(
        lambda: conn, CONTEXT, "select * from t", 0, "run", close_connection=close_connection
    )
    assert len(execution.rows) == 1000
    assert execution.query_id == "query-1"
    assert cursor.commands[0][0] == "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 60"
    assert cursor.commands[2][0].endswith("LIMIT 1000")
    assert cursor.commands[2][2] == {"timeout": 60}
    assert execution.metrics["warehouse_compute"].status == "pending"
    assert cursor.closed
    assert conn.close.call_count == int(close_connection)


def test_preview_context_mismatch_never_executes_sql(monkeypatch):
    cursor = Cursor()
    conn = Mock()
    conn.cursor.return_value = cursor
    monkeypatch.setattr(clients, "context", lambda _: {**CONTEXT, "role": "OTHER"})
    with pytest.raises(ValueError, match="differs"):
        clients.execute_preview(lambda: conn, CONTEXT, "select 1", 0, "run")
    assert cursor.commands == []
    conn.close.assert_called_once()


def test_preview_sql_error_remains_failure(monkeypatch):
    cursor = Cursor()
    cursor.execute = Mock(side_effect=ProgrammingError(msg="timeout", errno=604))
    conn = Mock()
    conn.cursor.return_value = cursor
    monkeypatch.setattr(clients, "context", lambda _: CONTEXT)
    result = clients.execute_preview(lambda: conn, CONTEXT, "select 1", 0, "run")
    assert "604" in result.error
    assert not result.rows
    conn.close.assert_called_once()


def test_refresh_exact_id_pending_denied_then_available(monkeypatch):
    execution = Execution(0, "select 1", query_id="exact-id")
    fake = Mock(side_effect=[[], []])
    monkeypatch.setattr(clients, "query", fake)
    clients.refresh_execution(Mock(), execution)
    assert fake.call_args.args[2] == ("exact-id",)
    assert execution.metrics["warehouse_compute"].status == "pending"
    fake.side_effect = [[], ProgrammingError(msg="denied")]
    clients.refresh_execution(Mock(), execution)
    assert execution.metrics["warehouse_compute"].status == "unavailable"
    fake.side_effect = [
        [{"execution_time": 100, "bytes_scanned": 50}],
        [{"credits_attributed_compute": 0.125, "credits_used_query_acceleration": 0}],
    ]
    clients.refresh_execution(Mock(), execution)
    assert execution.metrics["warehouse_compute"].value == 0.125
    assert execution.metrics["query_acceleration"].value == 0
    assert execution.metrics["bytes_scanned"].value == 50


def test_discovery_definition_and_denied_reference(monkeypatch):
    fake = Mock(
        side_effect=[
            [{"database_name": "DB", "schema_name": "S", "name": "A"}],
            [
                {
                    "agent_spec": json.dumps(
                        {
                            "instructions": {"response": "be concise"},
                            "tool_resources": {"a": {"semantic_view": "DB.S.V"}},
                        }
                    )
                }
            ],
            ProgrammingError(msg="denied"),
        ]
    )
    monkeypatch.setattr(clients, "query", fake)
    obj = clients.discover(Mock(), "agent")[0]
    definitions = clients.definitions(Mock(), obj)
    assert "be concise" in definitions[0].text
    assert definitions[1].error
    assert fake.call_args.args[2] == ("DB.S.V",)


def test_judge_separate_usage_and_size_gate(monkeypatch):
    fake = Mock(
        return_value=[
            {
                "response": json.dumps(
                    {
                        "choices": [{"messages": "Evidence-backed summary"}],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                    }
                )
            }
        ]
    )
    monkeypatch.setattr(clients, "query", fake)
    result = clients.judge(Mock(), "model", "instructions", "evidence")
    assert result["text"] == "Evidence-backed summary"
    assert result["usage"]["total_tokens"]["value"] == 30
    assert "PARSE_JSON(%s)::ARRAY" in fake.call_args.args[1]
    assert json.loads(fake.call_args.args[2][1])[0]["role"] == "system"
    with pytest.raises(ValueError, match="40,000"):
        clients.judge(Mock(), "model", "prompt", "x" * 40001)
    assert fake.call_count == 1


@pytest.mark.parametrize(
    "payload",
    [None, [], {"choices": {}}, {"choices": [None]}, {"choices": [{"messages": ""}]}],
)
def test_malformed_judge_response_is_explicit(monkeypatch, payload):
    monkeypatch.setattr(clients, "query", Mock(return_value=[{"response": json.dumps(payload)}]))
    with pytest.raises(ValueError, match="Judge"):
        clients.judge(Mock(), "model", "prompt", "evidence")


def test_request_usage_requires_exact_agent_id_and_unique_row(monkeypatch):
    from common.comparison import Turn

    turn = Turn(ObjectRef("agent", "DB", "S", "A"), "q", [], [], {})
    fake = Mock(return_value=[{"tokens": 21, "token_credits": 0.03}])
    monkeypatch.setattr(clients, "query", fake)
    clients.refresh_answer(Mock(), turn, "DB.S.RPT_AGENT_REQUEST")
    fake.assert_not_called()
    turn.answer.identifiers["request_id"] = "exact-request"
    clients.refresh_answer(Mock(), turn, "DB.S.RPT_AGENT_REQUEST")
    assert fake.call_args.args[2] == ("exact-request",)
    assert turn.answer.metrics["total_tokens"].value == 21
    assert turn.answer.metrics["cortex_credits"].value == 0.03
    fake.return_value = [{"tokens": 1}, {"tokens": 2}]
    with pytest.raises(ValueError, match="not unique"):
        clients.refresh_answer(Mock(), turn, "DB.S.RPT_AGENT_REQUEST")
    with pytest.raises(ValueError, match="Invalid"):
        clients.refresh_answer(Mock(), turn, "bad; DROP TABLE a")


def test_request_usage_pending_and_denied_not_zero(monkeypatch):
    from common.comparison import Turn

    turn = Turn(ObjectRef("agent", "DB", "S", "A"), "q", [], [], {})
    turn.answer.identifiers["request_id"] = "exact"
    fake = Mock(return_value=[])
    monkeypatch.setattr(clients, "query", fake)
    clients.refresh_answer(Mock(), turn, "DB.S.R")
    assert turn.answer.metrics["total_tokens"].status == "pending"
    assert turn.answer.metrics["total_tokens"].value is None
    turn.answer.metrics.clear()
    fake.side_effect = ProgrammingError(msg="denied")
    clients.refresh_answer(Mock(), turn, "DB.S.R")
    assert turn.answer.metrics["cortex_credits"].status == "unavailable"
    assert turn.answer.metrics["cortex_credits"].value is None


def test_semantic_definition_and_empty_definition_failure(monkeypatch):
    obj = ObjectRef("semantic_view", "DB", "S", "V")
    fake = Mock(return_value=[{"definition": "create semantic view"}])
    monkeypatch.setattr(clients, "query", fake)
    assert clients.definitions(Mock(), obj)[0].text == "create semantic view"
    fake.return_value = []
    with pytest.raises(ValueError, match="empty"):
        clients.definitions(Mock(), obj)


def test_query_failure_always_closes_cursor():
    cursor = Cursor()
    cursor.execute = Mock(side_effect=ProgrammingError(msg="denied"))
    conn = Mock()
    conn.cursor.return_value = cursor
    with pytest.raises(ProgrammingError):
        clients.query(conn, "select 1")
    assert cursor.closed


def test_documented_json_tool_result_preserves_sql_and_bounded_rows_without_reasoning():
    payload = {
        "content": [
            {
                "type": "tool_results",
                "tool_results": {
                    "content": [
                        {
                            "type": "json",
                            "json": {
                                "sql": "SELECT N FROM DB.S.T",
                                "verified_query_used": True,
                                "sql_explanation": "A public explanation",
                                "think": "PRIVATE",
                                "result_set": {
                                    "statementHandle": "exact-tool-query",
                                    "resultSetMetaData": {"rowType": [{"name": "N"}]},
                                    "data": [[number] for number in range(1200)],
                                },
                            },
                        }
                    ]
                },
            }
        ]
    }
    answer = clients.parse_answer(payload, "agent")
    assert answer.sql[0].query_id == "exact-tool-query"
    assert answer.sql[0].verified is True
    assert answer.sql[0].result_columns == ["N"]
    assert len(answer.sql[0].result_rows) == 1000
    assert answer.text == "A public explanation"
    assert "PRIVATE" not in repr(answer)


def test_malformed_tool_result_set_is_not_rendered():
    with pytest.raises(ValueError, match="malformed"):
        clients.parse_answer(
            {
                "content": [
                    {
                        "type": "json",
                        "json": {
                            "sql": "select 1",
                            "result_set": {
                                "resultSetMetaData": {"rowType": [{"name": "N"}]},
                                "data": [[1, 2]],
                            },
                        },
                    }
                ]
            },
            "agent",
        )
