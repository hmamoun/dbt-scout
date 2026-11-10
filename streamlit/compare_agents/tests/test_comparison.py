import json

import pytest
from common.comparison import (
    Definition,
    Execution,
    Metric,
    ObjectRef,
    Pane,
    Turn,
    agent_labels,
    comparison_key,
    export_comparison,
    metric_delta,
    run_metric_rows,
    text_diff,
)
from common.comparison_sql import bounded_sql


def test_short_agent_labels_disambiguate_duplicate_names_and_same_agent():
    a = ObjectRef("agent", "DB", "S", "ONE")
    b = ObjectRef("agent", "DB", "OTHER", "ONE")
    assert agent_labels([a, ObjectRef("agent", "DB", "S", "TWO")]) == ["ONE", "TWO"]
    assert agent_labels([a, b]) == ["S.ONE", "OTHER.ONE"]
    assert agent_labels([a, a]) == ["DB.S.ONE (1)", "DB.S.ONE (2)"]


def test_run_totals_keep_units_missing_and_cost_sources_separate():
    obj = ObjectRef("agent", "DB", "S", "ONE")
    first, second = [Turn(obj, str(n), [], [], {}) for n in (1, 2)]
    first.answer.metrics = {
        "input_tokens": Metric(3, "tokens", status="available"),
        "output_tokens": Metric(7, "tokens", status="available"),
        "total_tokens": Metric(10, "tokens", status="available"),
        "cortex_credits": Metric(0.5, "credits", status="available"),
    }
    second.answer.metrics = {
        "total_tokens": Metric(999, "tokens", status="pending"),
        "answer_latency": Metric(0, "seconds", status="available"),
    }
    for turn in (first, second):
        turn.executions = [Execution(0, "select 1", query_id="same-query")]
        turn.executions[0].metrics["warehouse_compute"] = Metric(2, "credits", status="available")
    totals, history = run_metric_rows([("ONE", [first, second]), ("TWO", [])])
    by_measure = {row["Measure"]: row for row in totals}
    assert by_measure["Answer: total tokens"]["ONE"] == 10
    assert by_measure["Answer: total tokens"]["TWO"] is None
    assert by_measure["Answer: input tokens"]["ONE"] == 3
    assert by_measure["Answer: cortex credits"]["ONE"] == 0.5
    assert by_measure["SQL: warehouse compute"]["ONE"] == 2
    assert by_measure["Answer: answer latency"]["ONE"] == 0
    assert len([row for row in history if row["Query ID"] == "same-query"]) == 1
    pending = next(
        row for row in history if row["Run"] == 2 and row["Measure"] == "Answer: total tokens"
    )
    assert pending["Value"] is None and pending["Status"] == "pending"


def test_quoted_identifier_and_independent_panes():
    obj = ObjectRef("agent", 'DB"1', "space schema", "Mixed.Name")
    assert obj.fqn == '"DB""1"."space schema"."Mixed.Name"'
    left, right = Pane(), Pane()
    left.select(obj)
    left.messages.append({"role": "user"})
    assert right.messages == []
    original = left.conversation_id
    left.select(obj)
    assert left.conversation_id == original
    left.select(ObjectRef("agent", "DB", "S", "other"))
    assert left.messages == []
    assert left.conversation_id != original


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (
            Metric(10, "tokens", status="available"),
            Metric(15, "tokens", status="available"),
            (5, 50),
        ),
        (
            Metric(0, "tokens", status="available"),
            Metric(3, "tokens", status="available"),
            (3, None),
        ),
        (Metric(), Metric(3, "tokens", status="available"), (None, None)),
        (
            Metric(1, "seconds", status="available"),
            Metric(2, "tokens", status="available"),
            (None, None),
        ),
    ],
)
def test_metric_deltas(left, right, expected):
    assert metric_delta(left, right) == expected


def test_export_is_selected_evidence_without_private_context_or_rows():
    from common.comparison import Execution

    obj = ObjectRef("semantic_view", "DB", "S", "V")
    turn = Turn(obj, "question", [Definition(obj.fqn, "create semantic view")], [], {"role": "R"})
    turn.answer.text = "Public answer"
    turn.answer.conversation_message = {"secret": "not for export"}
    turn.executions.append(Execution(0, "select 1", rows=[["sensitive row"]]))
    markdown, structured = export_comparison(turn, turn)
    data = json.loads(structured)
    assert data["left"]["answer"]["text"] == "Public answer"
    assert data["left"]["executions"][0]["preview_row_count"] == 1
    assert "sensitive row" not in markdown + structured
    assert "not for export" not in markdown + structured
    assert data["left"]["definitions"][0]["hash"] == turn.definitions[0].hash
    before = comparison_key(turn, turn, "model", "prompt")
    turn.answer.text = "changed"
    assert comparison_key(turn, turn, "model", "prompt") != before
    assert "-one" in text_diff("one", "two")
    assert text_diff("one", "one") == ""


@pytest.mark.parametrize(
    "sql",
    [
        "select 1",
        "SELECT department, COUNT(*) AS n FROM db.s.t GROUP BY department ORDER BY n",
        "WITH base AS (SELECT * FROM db.s.t) SELECT * FROM base",
        'SELECT "Mixed Name" FROM "DB"."S"."T" WHERE x = 1',
        "SELECT CASE WHEN x IS NULL THEN 0 ELSE x END FROM t",
        "SELECT * FROM t LIMIT 100000",
        "SELECT SUM(x) OVER (PARTITION BY y) FROM t",
    ],
)
def test_safe_sql_is_bounded(sql):
    bounded = bounded_sql(sql)
    assert bounded.endswith("LIMIT 1000")
    assert bounded.startswith("SELECT * FROM (")


@pytest.mark.parametrize(
    "sql",
    [
        "select 1; drop table t",
        "delete from t",
        "call do_work()",
        "copy into @stage from t",
        "select SYSTEM$SEND_EMAIL('x')",
        "select SNOWFLAKE.CORTEX.COMPLETE('x', 'y')",
        "select my_udf(x) from t",
        "select * from table(generator(rowcount=>100))",
        "select * from identifier('t')",
        "select * from @stage",
        "select * into t from a",
        "select * from t for update",
        "select * from t union select * from a",
        "",
        "select $$unfinished",
    ],
)
def test_unsupported_or_mutating_sql_fails_closed(sql):
    with pytest.raises(ValueError):
        bounded_sql(sql)
