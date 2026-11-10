"""Session-owned comparison evidence and pure calculations."""

import difflib
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

ObjectKind = Literal["semantic_view", "agent"]
MetricStatus = Literal["available", "pending", "unavailable", "not applicable", "failed"]


def now() -> str:
    return datetime.now(UTC).isoformat()


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ObjectRef:
    kind: ObjectKind
    database: str
    schema: str
    name: str

    @property
    def fqn(self) -> str:
        return ".".join(
            '"' + part.replace('"', '""') + '"' for part in (self.database, self.schema, self.name)
        )


@dataclass
class Metric:
    value: float | None = None
    unit: str = ""
    source: str = ""
    status: MetricStatus = "unavailable"
    fetched_at: str = field(default_factory=now)
    detail: str = ""


@dataclass(frozen=True)
class Definition:
    fqn: str
    text: str
    captured_at: str = field(default_factory=now)
    error: str = ""

    @property
    def hash(self) -> str:
        return fingerprint(self.text)


@dataclass
class SqlEvidence:
    statement: str
    verified: bool | None = None
    verified_name: str | None = None
    query_id: str | None = None
    result_columns: list[str] = field(default_factory=list)
    result_rows: list[list] = field(default_factory=list, repr=False)


@dataclass
class Answer:
    text: str = ""
    sql: list[SqlEvidence] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Metric] = field(default_factory=dict)
    conversation_message: dict = field(default_factory=dict, repr=False)


@dataclass
class Execution:
    sql_index: int
    executed_sql: str
    query_id: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list, repr=False)
    metrics: dict[str, Metric] = field(default_factory=dict)
    error: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    started_at: str = field(default_factory=now)
    origin: str = "explicit app preview"
    warehouse: str = ""


@dataclass
class Turn:
    object: ObjectRef
    question: str
    definitions: list[Definition]
    context_questions: list[str]
    context: dict[str, str]
    answer: Answer = field(default_factory=Answer)
    executions: list[Execution] = field(default_factory=list)
    error: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    started_at: str = field(default_factory=now)


@dataclass
class Pane:
    selected: ObjectRef | None = None
    conversation_id: str = field(default_factory=lambda: str(uuid4()))
    turns: list[Turn] = field(default_factory=list)
    active_ids: list[str] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    thread_id: int | None = None
    parent_message_id: int = 0

    def reset(self, selected: ObjectRef | None) -> None:
        self.selected = selected
        self.conversation_id = str(uuid4())
        self.active_ids = []
        self.messages = []
        self.thread_id = None
        self.parent_message_id = 0

    def select(self, selected: ObjectRef | None) -> None:
        if self.selected != selected:
            self.reset(selected)


def agent_labels(objects: list[ObjectRef | None]) -> list[str]:
    """Prefer short names, disambiguating schemas and repeated selections."""
    labels = [obj.name if obj else "Select agent" for obj in objects]
    names = list(labels)
    for index, obj in enumerate(objects):
        if obj and names.count(obj.name) > 1:
            labels[index] = f"{obj.schema}.{obj.name}"
    if len(set(labels)) != len(labels):
        labels = [
            f"{obj.database}.{obj.schema}.{obj.name} ({index + 1})" if obj else f"Agent {index + 1}"
            for index, obj in enumerate(objects)
        ]
    return labels


def run_metric_rows(groups: list[tuple[str, list[Turn]]]) -> tuple[list[dict], list[dict]]:
    """Summarize measured values only; keep units and answer/SQL costs separate."""
    history = []
    totals: dict[tuple[str, str], dict] = {}
    additive = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "answer_latency",
        "cortex_credits",
        "warehouse_compute",
        "execution_time",
        "total_elapsed_time",
        "bytes_scanned",
        "rows_produced",
        "preview_elapsed",
        "preview_rows",
        "credits_used_query_acceleration",
        "credits_attributed_compute",
    }
    for label, turns in groups:
        seen_queries: set[str] = set()
        for number, turn in enumerate(turns, 1):
            evidence = [
                (
                    "Answer",
                    "",
                    {
                        "total_tokens": Metric(unit="tokens"),
                        "cortex_credits": Metric(unit="credits"),
                        "answer_latency": Metric(unit="seconds"),
                        **turn.answer.metrics,
                    },
                )
            ]
            for execution in turn.executions:
                identity = execution.query_id or execution.id
                if identity not in seen_queries:
                    seen_queries.add(identity)
                    evidence.append(
                        (
                            "SQL",
                            identity,
                            {
                                "warehouse_compute": Metric(unit="credits"),
                                **execution.metrics,
                            },
                        )
                    )
            for scope, query_id, metrics in evidence:
                for name, metric in metrics.items():
                    available = (
                        metric.status == "available"
                        and metric.value is not None
                        and math.isfinite(metric.value)
                    )
                    measure = f"{scope}: {name.replace('_', ' ')}"
                    history.append(
                        {
                            "Agent": label,
                            "Run": number,
                            "Run ID": turn.id,
                            "Question": turn.question,
                            "Result": "Failed" if turn.error else "Completed",
                            "Measure": measure,
                            "Value": metric.value if available else None,
                            "Unit": metric.unit,
                            "Status": metric.status,
                            "Source": metric.source,
                            "Query ID": query_id,
                        }
                    )
                    if name in additive:
                        row = totals.setdefault(
                            (measure, metric.unit), {"Measure": measure, "Unit": metric.unit}
                        )
                        if available:
                            row[label] = row.get(label, 0) + metric.value
    labels = [label for label, _ in groups]
    summary = []
    for _, row in sorted(totals.items()):
        summary.append({**row, **{label: row.get(label) for label in labels}})
    return summary, history


def text_diff(left: str, right: str, left_label: str = "Left", right_label: str = "Right") -> str:
    return "\n".join(
        difflib.unified_diff(
            left.splitlines(),
            right.splitlines(),
            fromfile=left_label,
            tofile=right_label,
            lineterm="",
        )
    )


def metric_delta(left: Metric, right: Metric) -> tuple[float | None, float | None]:
    if (
        left.status != "available"
        or right.status != "available"
        or left.value is None
        or right.value is None
        or left.unit != right.unit
    ):
        return None, None
    delta = right.value - left.value
    return delta, (100 * delta / abs(left.value) if left.value != 0 else None)


def turn_evidence(turn: Turn) -> dict:
    """Public evidence only; result rows and model reasoning never enter exports."""
    data = asdict(turn)
    data["object"]["fqn"] = turn.object.fqn
    data["answer"].pop("conversation_message")
    for sql in data["answer"]["sql"]:
        sql["preview_row_count"] = len(sql.pop("result_rows"))
    for definition, original in zip(data["definitions"], turn.definitions, strict=True):
        definition["hash"] = original.hash
    for execution in data["executions"]:
        execution["preview_row_count"] = len(execution.pop("rows"))
    return data


def comparison_key(
    left: Turn,
    right: Turn,
    model: str,
    prompt: str,
    execution_ids: list[str | None] | None = None,
) -> str:
    return fingerprint(
        json.dumps(
            [turn_evidence(left), turn_evidence(right), model, prompt, execution_ids],
            sort_keys=True,
            default=str,
        )
    )


def judge_evidence(turn: Turn, *, include_definitions: bool = False) -> dict:
    data = turn_evidence(turn)
    if not include_definitions:
        for definition in data["definitions"]:
            definition["text_characters"] = len(definition.pop("text"))
        data["evidence_limitations"] = (
            "Definition text omitted by the selected judge scope; only identity, hashes, "
            "capture times and availability are included. Do not infer instructions, "
            "tools or verified-query configuration from these hashes."
        )
    return data


def export_comparison(
    left: Turn,
    right: Turn,
    judge: dict | None = None,
    execution_ids: list[str | None] | None = None,
) -> tuple[str, str]:
    data = {
        "format_version": 1,
        "exported_at": now(),
        "limitations": (
            "Exploratory comparison; independent questions/histories and mutable source data "
            "do not establish a controlled A/B test. Bounded previews do not prove equality. "
            "Result rows are omitted. Missing telemetry is not zero."
        ),
        "left": turn_evidence(left),
        "right": turn_evidence(right),
        "judge": judge,
        "selected_sql_execution_ids": execution_ids,
    }
    structured = json.dumps(data, indent=2, default=str)
    sections = [
        "# Object comparison",
        data["limitations"],
        "Selected SQL executions (left/right): " + json.dumps(execution_ids),
    ]
    for label, turn in (("Left", left), ("Right", right)):
        sections.extend(
            [
                f"## {label}: {turn.object.fqn}",
                f"Run: {turn.id}\n\nQuestion: {turn.question}",
                turn.answer.text or turn.error or "No narrative returned.",
                "### SQL",
                *["```sql\n" + sql.statement + "\n```" for sql in turn.answer.sql],
                "### Evidence\n```json\n"
                + json.dumps(turn_evidence(turn), indent=2, default=str)
                + "\n```",
            ]
        )
    if judge:
        sections.append("## Optional AI insights\n" + str(judge["text"]))
    return "\n\n".join(sections), structured
