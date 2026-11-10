"""Shared two-pane rendering; external calls happen only on explicit actions."""

import json
import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from typing import cast
from uuid import uuid4

import pandas as pd
import streamlit as st
from snowflake.connector import Error as SnowflakeError
from snowflake.snowpark.exceptions import SnowparkSQLException
from streamlit.errors import StreamlitAPIException, StreamlitSecretNotFoundError

from . import comparison_clients as clients
from .agent_selection import initialize_agent_selection, matching_agent
from .common import (
    comparison_connection_mode,
    discover_agents,
    get_comparison_connections,
)
from .comparison import (
    Execution,
    Metric,
    ObjectRef,
    Pane,
    Turn,
    agent_labels,
    comparison_key,
    export_comparison,
    fingerprint,
    judge_evidence,
    metric_delta,
    run_metric_rows,
    text_diff,
)
from .comparison_sql import bounded_sql
from .prompts import COMPARISON_PROMPT
from .preferences import load_settings, optional_report_fqn

LOGGER = logging.getLogger(__name__)
_ERRORS = (SnowflakeError, ValueError)
_COMPOSER_CSS = """<style>
[class*="st-key-ask-compose-"] [class*="st-key-ask-copy-"] { display: none; }
[class*="st-key-ask-compose-"]:focus-within [class*="st-key-ask-copy-"] { display: block; }
</style>"""


def _show_error(action: str, exc: Exception) -> None:
    if isinstance(exc, SnowflakeError):
        LOGGER.warning("Comparison %s failed: code=%s", action, exc.errno)
        if action == "AI judge":
            st.error(
                f"AI judge failed (Snowflake code {exc.errno}). "
                "Open Snowflake error details below for the service reason and query ID. "
                "The code alone does not establish a model or privilege problem. "
                "No automatic retry was made."
            )
            with st.expander("Snowflake error details", expanded=True):
                st.text(str(exc.msg or "Snowflake returned no error message.")[:4000])
                if exc.sfqid:
                    st.caption(f"Query ID: {exc.sfqid}")
                if exc.sqlstate:
                    st.caption(f"SQLSTATE: {exc.sqlstate}")
                st.caption(
                    "This is the separate Cortex COMPLETE judge call, not DATA_AGENT_RUN. "
                    "Check the selected judge model's availability, allowed-model policy, "
                    "Cortex permissions and request limits against the returned reason. "
                    "Query history for this ID can provide the full error. "
                    "Diagnostic text is limited to 4,000 characters; request SQL and evidence "
                    "are not added by the app."
                )
            return
        st.error(
            f"{action} failed (Snowflake code {exc.errno}). Check effective-role access and configuration."
        )
    else:
        st.error(f"{action}: {exc}")


def _preview_connection() -> clients.Connection:
    _, preview = get_comparison_connections()
    return cast(clients.Connection, preview.raw_connection)


def _metrics(values: dict[str, Metric]) -> None:
    defaults = {
        "input_tokens": Metric(unit="tokens", detail="Not exposed by this response."),
        "output_tokens": Metric(unit="tokens", detail="Not exposed by this response."),
        "total_tokens": Metric(unit="tokens", detail="Not exposed by this response."),
        "cortex_credits": Metric(
            unit="credits", detail="No exact request-level usage source available."
        ),
    }
    rows = []
    for label, metric in {**defaults, **values}.items():
        rows.append(
            {
                "Measure": label.replace("_", " "),
                "Value": metric.value if metric.status == "available" else None,
                "Unit": metric.unit,
                "Status": metric.status,
                "Source": metric.source or "Not exposed",
                "Fetched at (UTC)": metric.fetched_at,
                "Notes": metric.detail,
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _display_name(obj: ObjectRef) -> str:
    return f"{obj.database}.{obj.schema}.{obj.name}"


def _pick_object(
    objects: list[ObjectRef], key: str, label: str, *, disabled: bool = False
) -> ObjectRef | None:
    choices = {obj.fqn: obj for obj in sorted(objects, key=_display_name)}
    if st.session_state.get(f"{key}:object") not in choices:
        st.session_state.pop(f"{key}:object", None)
    noun = "agent"
    selection = st.selectbox(
        label if label.startswith("Agent ") else f"{label} agent",
        list(choices),
        key=f"{key}:object",
        index=None,
        format_func=lambda fqn: _display_name(choices[fqn]),
        placeholder=f"Type to search {len(choices):,} {noun}s by DATABASE.SCHEMA.NAME",
        disabled=disabled,
    )
    return choices.get(selection) if selection else None


def _submit(
    connection: clients.Connection, pane: Pane, question: str, execution_context: dict[str, str]
) -> tuple[Pane, bool, Exception | None]:
    if pane.selected is None or not question.strip():
        raise ValueError("Select an object and enter a nonempty question.")
    if clients.context(connection) != execution_context:
        raise ValueError("Agent connection context changed. Refresh the testing session.")
    snapshots = clients.definitions(connection, pane.selected)
    previous = next((turn for turn in reversed(pane.turns) if turn.id in pane.active_ids), None)
    changed = previous is not None and [(d.fqn, d.hash) for d in previous.definitions] != [
        (d.fqn, d.hash) for d in snapshots
    ]
    if changed:
        pane.reset(pane.selected)
    turn = Turn(
        pane.selected,
        question.strip(),
        snapshots,
        [t.question for t in pane.turns if t.id in pane.active_ids],
        dict(execution_context),
    )
    pane.turns.append(turn)
    pane.active_ids.append(turn.id)
    try:
        turn.answer = clients.ask_agent(connection, pane, question.strip())
        seen_ids: set[str] = set()
        for index, sql in enumerate(turn.answer.sql):
            if sql.query_id and sql.query_id not in seen_ids:
                seen_ids.add(sql.query_id)
                turn.executions.append(
                    Execution(
                        index,
                        sql.statement,
                        query_id=sql.query_id,
                        columns=sql.result_columns,
                        rows=sql.result_rows,
                        origin="service-reported tool execution",
                    )
                )
    except _ERRORS as exc:
        turn.error = "Answer request failed. No automatic retry was made."
        # A failed request may have advanced the remote thread. Never retry it
        # implicitly or attach the next turn to an uncertain server parent.
        pane.reset(pane.selected)
        return pane, changed, exc
    return pane, changed, None


def _pending(key: str) -> Future[tuple[Pane, bool, Exception | None]] | None:
    return st.session_state.get(f"{key}:pending")


def _finish_pending(key: str) -> None:
    future = _pending(key)
    if future is None or not future.done():
        return
    del st.session_state[f"{key}:pending"]
    epoch = st.session_state.pop(f"{key}:pending_metrics_epoch", None)
    try:
        pane, changed, error = future.result()
    except _ERRORS as exc:
        _show_error("Ask", exc)
        return
    st.session_state[key] = pane
    workspace_key = key.rsplit(":", 1)[0]
    metrics_state = st.session_state.get(f"{workspace_key}:metrics")
    if metrics_state and epoch == metrics_state["epoch"] and pane.turns:
        metrics_state["ids"].append(pane.turns[-1].id)
    if changed:
        st.warning(
            "The definition changed. A new conversation was started; prior turns remain in session history."
        )
    if error is not None:
        if isinstance(error, SnowflakeError):
            detail = f"Snowflake code {error.errno}; check effective-role access and configuration."
        else:
            detail = str(error)
        pane.turns[-1].error = f"Answer request failed: {detail}"
        _show_error("Ask", error)


def _render_turn(connection: clients.Connection, turn: Turn, key: str) -> None:
    st.markdown(f"**Question:** {turn.question}")
    st.caption(f"Run {turn.id} · {turn.started_at} · {turn.object.fqn}")
    if turn.error:
        st.error(turn.error)
    if turn.answer.text:
        st.markdown(turn.answer.text)
    for warning in turn.answer.warnings:
        st.warning(warning)
    for suggestion in turn.answer.suggestions:
        st.info(suggestion)
    for index, sql in enumerate(turn.answer.sql):
        with st.expander(f"SQL {index + 1} · generated (not replayed automatically)"):
            st.code(sql.statement, language="sql")
            verified = (
                "unknown"
                if sql.verified is None
                else ("matched" if sql.verified else "not matched")
            )
            st.caption(
                f"Verified-query use: {verified}"
                + (f" · {sql.verified_name}" if sql.verified_name else "")
            )
            if sql.query_id:
                st.caption(f"Service-reported query ID: {sql.query_id}")
            elif sql.result_rows:
                st.caption("Service-provided result preview (no exact query ID available)")
                st.dataframe(
                    pd.DataFrame(sql.result_rows, columns=sql.result_columns or None),
                    hide_index=True,
                    width="stretch",
                )
            try:
                bounded_sql(sql.statement)
                eligible = True
            except ValueError as exc:
                eligible = False
                st.info(f"Preview disabled: {exc}")
            if st.button(
                f"Run SQL {index + 1}", key=f"{key}:{turn.id}:sql:{index}", disabled=not eligible
            ):
                try:
                    with st.spinner("Running bounded preview..."):
                        execution = clients.execute_preview(
                            _preview_connection,
                            turn.context,
                            sql.statement,
                            index,
                            turn.id,
                            close_connection=False,
                        )
                    turn.executions.append(execution)
                except _ERRORS as exc:
                    _show_error("Run SQL", exc)
            for execution in [e for e in turn.executions if e.sql_index == index]:
                st.caption(
                    f"{execution.origin} {execution.id} · query {execution.query_id or 'unavailable'}"
                )
                st.caption(f"Execution warehouse: {execution.warehouse or 'not yet exposed'}")
                if execution.error:
                    st.error(execution.error)
                else:
                    st.dataframe(
                        pd.DataFrame(execution.rows, columns=execution.columns),
                        hide_index=True,
                        width="stretch",
                    )
                    st.caption(
                        "Preview only: at most 1,000 rows. This does not prove full-result equality."
                    )
                _metrics(execution.metrics)
    with st.expander("Usage and public evidence"):
        _metrics(turn.answer.metrics)
        st.json(
            {
                "identifiers": turn.answer.identifiers,
                "tools": turn.answer.tools,
                "context": turn.context,
            }
        )
        st.caption(
            "Cortex service credits, query warehouse compute and app compute-pool overhead are separate. "
            "App overhead is not attributed per request. Agent-internal queries are not guessed from SQL/time."
        )
    with st.expander("Definition snapshots, instructions and VQR"):
        for definition in turn.definitions:
            st.caption(f"{definition.fqn} · SHA256 {definition.hash} · {definition.captured_at}")
            if definition.error:
                st.warning(definition.error)
            else:
                st.code(
                    definition.text,
                    language="json"
                    if turn.object.kind == "agent" and definition == turn.definitions[0]
                    else "sql",
                )
        st.caption(
            "Definitions expose configuration/VQR coverage, not proof that a verified query was used."
        )


def _copy_question(source: str, target: str) -> None:
    st.session_state[target] = str(st.session_state.get(source) or "")


def _start_request(
    connection: clients.Connection, pane: Pane, key: str, execution_context: dict[str, str]
) -> None:
    if _pending(key) is not None:
        st.info("This pane already has a request in progress.")
        return
    question = str(st.session_state.get(f"{key}:input") or "")
    try:
        if pane.selected is None or not question.strip():
            raise ValueError("Select an agent and enter a nonempty question.")
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"ask-{key.rsplit(':', 1)[-1]}"
        )
        try:
            metrics_state = st.session_state.get(f"{key.rsplit(':', 1)[0]}:metrics", {})
            st.session_state[f"{key}:pending_metrics_epoch"] = metrics_state.get("epoch")
            st.session_state[f"{key}:pending"] = executor.submit(
                _submit, connection, deepcopy(pane), question, dict(execution_context)
            )
        finally:
            executor.shutdown(wait=False)
    except _ERRORS as exc:
        _show_error("Ask", exc)


def _ask_form(
    connection: clients.Connection,
    pane: Pane,
    label: str,
    key: str,
    execution_context: dict[str, str],
) -> None:
    st.caption(f"Conversation {pane.conversation_id}")
    _, kind, side = key.split(":")
    other_side = "right" if side == "left" else "left"
    own_input, other_input = f"{key}:input", f"comparison:{kind}:{other_side}:input"
    # Streamlit has no server-side focus event; CSS in _COMPOSER_CSS shows the copy
    # button only while this composer (question box or the button itself) has focus.
    with st.container(key=f"ask-compose-{kind}-{side}"):
        question = st.text_area(
            "Question",
            key=own_input,
            max_chars=8000,
            height=100,
            label_visibility="collapsed",
            placeholder=f"Ask the {label.lower()} object a question...",
            disabled=_pending(key) is not None,
        )
        other = str(st.session_state.get(other_input) or "")
        if other.strip() and other.strip() != question.strip():
            other_pane = st.session_state.get(f"comparison:{kind}:{other_side}")
            other_name = (
                other_pane.selected.name if other_pane and other_pane.selected else "other agent"
            )
            st.button(
                f"Copy question from {other_name}",
                key=f"ask-copy-{kind}-{side}",
                icon=":material/content_copy:",
                help=other[:300],
                on_click=_copy_question,
                args=(other_input, own_input),
                disabled=_pending(key) is not None,
            )
    st.button(
        "Ask",
        key=f"{key}:ask",
        disabled=_pending(key) is not None,
        type="primary",
        icon=":material/send:",
        on_click=_start_request,
        args=(connection, pane, key, execution_context),
    )


def _pane(
    connection: clients.Connection,
    request_connection: clients.Connection,
    pane: Pane,
    objects: list[ObjectRef],
    label: str,
    key: str,
    execution_context: dict[str, str],
) -> Turn | None:
    with st.container(border=True):
        busy = _pending(key) is not None
        picker, reset = st.columns([5, 1], vertical_alignment="bottom")
        with picker:
            pane.select(_pick_object(objects, key, label, disabled=busy))
            if pane.selected:
                label = pane.selected.name
        with reset:
            if st.button(
                "New chat",
                key=f"{key}:reset",
                icon=":material/restart_alt:",
                help="Start a new conversation; earlier answers stay available for comparison.",
                width="stretch",
                disabled=busy,
            ):
                pane.reset(pane.selected)
        if pane.selected is None:
            st.caption("Pick an object above to start testing.")
        else:
            onboarding_key = f"comparison:agent:onboarding:{pane.selected.fqn}"
            if onboarding_key not in st.session_state:
                try:
                    st.session_state[onboarding_key] = clients.onboarding_questions(
                        connection, pane.selected
                    )
                except _ERRORS as exc:
                    st.session_state[onboarding_key] = None
                    _show_error("Load onboarding questions", exc)
            questions = st.session_state.get(onboarding_key)
            if questions:
                selection_key = f"{key}:onboarding:{pane.selected.fqn}"
                st.selectbox(
                    f"{label} onboarding question",
                    questions,
                    index=None,
                    key=selection_key,
                    placeholder="Pick a starter question or type your own below",
                    disabled=busy,
                    on_change=_copy_question,
                    args=(selection_key, f"{key}:input"),
                    help="Loads this agent's configured question into the composer. Press Ask to send.",
                )
            elif questions == []:
                st.caption("This agent has no configured onboarding questions.")
            else:
                st.caption("Onboarding questions unavailable. Refresh available objects to retry.")
            _ask_form(request_connection, pane, label, key, execution_context)
        if busy:
            st.info(f"Asking {label.lower()} agent… You can ask the other agent while this runs.")
    if not pane.turns:
        return None
    by_id = {turn.id: turn for turn in pane.turns}
    selection = st.selectbox(
        "Answer to inspect and compare",
        list(by_id),
        index=len(by_id) - 1,
        format_func=lambda value: f"{by_id[value].object.name}: {by_id[value].question[:65]}",
        key=f"{key}:turn:{len(by_id)}",
    )
    turn = by_id[selection]
    _render_turn(connection, turn, key)
    return turn


def _comparison(
    connection: clients.Connection,
    left: Turn | None,
    right: Turn | None,
    key: str,
    report_fqn: str | None = None,
) -> None:
    st.divider()
    st.header("Comparison insights")
    if left is None or right is None:
        st.info(
            "Complete a question on each side to compare selected answers. One-sided evidence remains inspectable."
        )
        return
    left_label, right_label = agent_labels([left.object, right.object])
    st.warning(
        "Exploratory comparison: independent questions/histories, changing data and caches "
        "do not establish a controlled A/B test or a correctness winner."
    )
    st.write({f"{left_label} question": left.question, f"{right_label} question": right.question})
    if left.error or right.error:
        st.warning(
            "At least one selected run failed. Compare failure evidence, not two successful answers."
        )
    if st.button("Refresh metrics", key=f"{key}:refresh_metrics"):
        for turn in (left, right):
            if report_fqn:
                try:
                    clients.refresh_answer(connection, turn, report_fqn)
                except _ERRORS as exc:
                    _show_error("Refresh request usage", exc)
            for execution in turn.executions:
                try:
                    clients.refresh_execution(connection, execution)
                except _ERRORS as exc:
                    _show_error("Refresh metrics", exc)
        st.info("Refreshed exact-ID SQL telemetry only. No questions or SQL were re-executed.")
    metrics = []
    for name in sorted(set(left.answer.metrics) | set(right.answer.metrics)):
        lval, rval = (
            left.answer.metrics.get(name, Metric()),
            right.answer.metrics.get(name, Metric()),
        )
        delta, pct = metric_delta(lval, rval)
        metrics.append(
            {
                "Measure": name,
                left_label: lval.value if lval.status == "available" else None,
                right_label: rval.value if rval.status == "available" else None,
                "Unit": lval.unit or rval.unit,
                f"{right_label} minus {left_label}": delta,
                "Change (%)": pct,
            }
        )
    if metrics:
        st.dataframe(
            pd.DataFrame(metrics).style.apply(
                _heatmap_row, axis=1, subset=[left_label, right_label]
            ),
            hide_index=True,
            width="stretch",
        )
    chosen_executions = []
    for label, turn in ((left_label, left), (right_label, right)):
        if not turn.executions:
            chosen_executions.append(None)
            continue
        choices = {execution.id: execution for execution in turn.executions}
        selected = st.selectbox(
            f"{label} SQL execution to compare",
            list(choices),
            index=len(choices) - 1,
            key=f"{key}:execution:{label}:{turn.id}:{len(choices)}",
        )
        chosen_executions.append(choices[selected])
    lexec, rexec = chosen_executions
    execution_ids = [execution.id if execution else None for execution in chosen_executions]
    if lexec is not None and rexec is not None:
        execution_metrics = []
        for name in sorted(set(lexec.metrics) | set(rexec.metrics)):
            lmetric = lexec.metrics.get(name, Metric())
            rmetric = rexec.metrics.get(name, Metric())
            delta, pct = metric_delta(lmetric, rmetric)
            execution_metrics.append(
                {
                    "SQL measure": name,
                    left_label: lmetric.value if lmetric.status == "available" else None,
                    right_label: rmetric.value if rmetric.status == "available" else None,
                    "Unit": lmetric.unit or rmetric.unit,
                    f"{right_label} minus {left_label}": delta,
                    "Change (%)": pct,
                    f"{left_label} status": lmetric.status,
                    f"{right_label} status": rmetric.status,
                }
            )
        if execution_metrics:
            st.dataframe(
                pd.DataFrame(execution_metrics).style.apply(
                    _heatmap_row, axis=1, subset=[left_label, right_label]
                ),
                hide_index=True,
                width="stretch",
            )
        st.caption(
            f"Selected executions: {left_label} {lexec.id}; {right_label} {rexec.id}. "
            f"Preview schemas {'match' if lexec.columns == rexec.columns else 'differ'}. "
            "Neither schema matching nor row previews prove full-result equality."
        )
    with st.expander("Answer, SQL and definition differences", expanded=False):
        for label, ltext, rtext in (
            ("Answers", left.answer.text, right.answer.text),
            (
                "SQL",
                "\n\n".join(s.statement for s in left.answer.sql),
                "\n\n".join(s.statement for s in right.answer.sql),
            ),
            (
                "Definitions / instructions / VQR",
                "\n\n".join(d.text for d in left.definitions),
                "\n\n".join(d.text for d in right.definitions),
            ),
        ):
            st.markdown(f"**{label}**")
            difference = text_diff(ltext, rtext, left_label, right_label)
            if difference:
                st.code(difference, language="diff")
            else:
                st.caption(
                    "No text differences in the available evidence; not proof of correctness."
                )
    with st.expander("Optional AI judge"):
        st.caption(
            "Billable, manual action. Judge usage is separate from the objects under test. "
            "Result rows are not sent. Maximum evidence size: 40,000 characters; no silent truncation."
        )
        model = st.text_input("Judge model", value=os.environ.get("SCOUT_JUDGE_MODEL", ""), key=f"{key}:model")
        st.caption(
            "The judge model is independent of the agents' orchestration models. "
            "The default is not an account-validated model list; choose a model enabled "
            "for this account, region and effective role."
        )
        prompt = st.text_area(
            "Comparison instructions", value=COMPARISON_PROMPT, key=f"{key}:prompt", height=200
        )
        include_definitions = st.checkbox(
            "Include full definitions and instructions",
            key=f"{key}:judge_definitions",
            help="Off: compare answers, SQL, usage and definition hashes. On: also send all definition text, subject to the size limit.",
        )
        st.caption(
            "Definition text is excluded unless selected above. Hashes alone cannot establish "
            "instruction, tool-configuration or verified-query differences."
        )
        evidence = json.dumps(
            {
                "left": judge_evidence(left, include_definitions=include_definitions),
                "right": judge_evidence(right, include_definitions=include_definitions),
                "selected_sql_execution_ids": execution_ids,
            },
            default=str,
        )
        evidence_key = fingerprint(
            comparison_key(left, right, model, prompt, execution_ids) + evidence
        )
        st.caption(f"Selected judge evidence: {len(evidence):,} / 40,000 characters.")
        if st.button("Generate AI insights", key=f"{key}:judge", disabled=len(evidence) > 40000):
            try:
                with st.spinner("Generating comparison insights..."):
                    result = clients.judge(connection, model, prompt, evidence)
                result.update(evidence_key=evidence_key, prompt_hash=fingerprint(prompt))
                st.session_state[f"{key}:judge_result"] = result
            except _ERRORS as exc:
                _show_error("AI judge", exc)
        if len(evidence) > 40000:
            st.info(
                "Evidence exceeds the judge limit; exclude full definitions or select smaller turns. "
                "Nothing is truncated. Deterministic comparisons and exports remain available."
            )
    saved = st.session_state.get(f"{key}:judge_result")
    current_judge = saved if saved and saved["evidence_key"] == evidence_key else None
    if current_judge:
        st.markdown(current_judge["text"])
        st.caption("Judge usage (excluded from object totals)")
        st.json({k: v for k, v in current_judge.items() if k != "text"})
    elif saved:
        st.info(
            "Previous AI insights are stale for the selected evidence/model/prompt. Generate again explicitly if needed."
        )
    markdown, structured = export_comparison(left, right, current_judge, execution_ids)
    st.caption(
        "Session-only history. Downloads omit result rows and private reasoning but may contain "
        "sensitive questions, answers and definitions; handle according to your data policy."
    )
    st.download_button(
        "Download comparison report", markdown, "comparison.md", "text/markdown", key=f"{key}:md"
    )
    st.download_button(
        "Download comparison evidence",
        structured,
        "comparison.json",
        "application/json",
        key=f"{key}:json",
    )


def _metrics_test_pair(key: str, panes: list[Pane]) -> dict:
    pair = tuple(pane.selected.fqn if pane.selected else None for pane in panes)
    state_key = f"{key}:metrics"
    state = st.session_state.get(state_key)
    if state is None or state["pair"] != pair:
        state = {"pair": pair, "epoch": str(uuid4()), "ids": []}
        st.session_state[state_key] = state
    return state


def _heatmap_row(row: pd.Series) -> list[str]:
    numeric = pd.to_numeric(row, errors="coerce")
    values = numeric.dropna()
    if len(values) < 2 or values.max() == values.min():
        return [""] * len(row)
    low, high = values.min(), values.max()
    return [
        ""
        if pd.isna(value)
        else (
            "background-color: #dbeafe; color: #172554"
            if value == low
            else "background-color: #ffedd5; color: #431407"
            if value == high
            else ""
        )
        for value in numeric
    ]


def _metrics_panel(
    connection: clients.Connection, panes: list[Pane], key: str, report_fqn: str | None
) -> None:
    state = _metrics_test_pair(key, panes)
    labels = agent_labels([pane.selected for pane in panes])
    groups = [
        (label, [turn for turn in pane.turns if turn.id in state["ids"]])
        for label, pane in zip(labels, panes, strict=True)
    ]
    st.subheader("Test metrics")
    st.caption("Current agent pair only. Changing either agent clears this panel.")
    for column, (label, turns) in zip(st.columns(2), groups, strict=True):
        column.metric(label, len(turns), help=f"{sum(bool(t.error) for t in turns)} failed runs")
    if not any(turns for _, turns in groups):
        st.info("Metrics appear here after each completed run.")
        return
    if st.button("Refresh all run metrics", key=f"{key}:metrics:refresh"):
        for _, turns in groups:
            for turn in turns:
                try:
                    if report_fqn:
                        clients.refresh_answer(connection, turn, report_fqn)
                    for execution in turn.executions:
                        clients.refresh_execution(connection, execution)
                except _ERRORS as exc:
                    _show_error("Refresh run metrics", exc)
    totals, history = run_metric_rows(groups)
    st.markdown("**Totals**")
    st.caption(
        "Measured subtotals only; missing usage is not zero. Each metric is summed separately, "
        "never tokens plus token components or Cortex plus warehouse credits. "
        "Cumulative answer latency is not warehouse compute time. Judge usage is excluded."
    )
    if totals:
        frame = pd.DataFrame(totals)
        styled = frame.style.apply(_heatmap_row, axis=1, subset=labels).format(
            precision=3, na_rep="Unavailable", subset=labels
        )
        st.dataframe(styled, hide_index=True, width="stretch")
        st.caption(
            "Heatmap: blue = lower, orange = higher within the same measure/unit; not a quality score."
        )
    else:
        st.info("No additive resource measurements exposed yet.")
    st.markdown("**Run history**")
    if history:
        frame = pd.DataFrame(history)
        styles = pd.DataFrame("", index=frame.index, columns=frame.columns)
        for _, indices in frame.groupby(["Measure", "Unit"]).groups.items():
            styles.loc[indices, "Value"] = _heatmap_row(frame.loc[indices, "Value"])
        st.dataframe(
            frame.style.apply(lambda _: styles, axis=None).format(
                {"Value": "{:,.3f}"}, na_rep="Unavailable"
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("Runs completed, but the service exposed no usage metrics.")


def _workspace(
    connection: clients.Connection,
    agent_connections: dict[str, clients.Connection],
    objects: list[ObjectRef],
    execution_context: dict[str, str],
    key: str,
    report_fqn: str | None,
) -> None:
    for side in ("left", "right"):
        st.session_state.setdefault(f"{key}:{side}", Pane())
    panes = [st.session_state[f"{key}:{side}"] for side in ("left", "right")]
    _metrics_test_pair(key, panes)
    left, right = st.columns(2, gap="medium")
    selected = []
    labels = agent_labels([pane.selected for pane in panes])
    for index, (side, column) in enumerate((("left", left), ("right", right))):
        pane_key = f"{key}:{side}"
        with column:
            selected.append(
                _pane(
                    connection,
                    agent_connections[side],
                    st.session_state[pane_key],
                    objects,
                    labels[index] if panes[index].selected else f"Agent {index + 1}",
                    pane_key,
                    execution_context,
                )
            )
    _comparison(connection, selected[0], selected[1], key, report_fqn)


@st.fragment(run_every=1)
def _poll_pending(key: str) -> None:
    if any(
        future is not None and future.done()
        for future in (_pending(f"{key}:left"), _pending(f"{key}:right"))
    ):
        st.rerun()


def render_page() -> None:
    title = "Compare Agents"
    st.set_page_config(page_title=title, layout="wide")
    st.html(_COMPOSER_CSS)
    st.title(title)
    key = "comparison:agent"
    sidebar = st.sidebar.container()
    with sidebar:
        st.subheader("Testing session")
        refresh = st.button(
            "Refresh available objects",
            key=f"{key}:discover",
            icon=":material/refresh:",
            width="stretch",
        )
        with st.expander("Notes and limits", icon=":material/info:"):
            st.caption(
                "Two independent conversations. Nothing is submitted on rerun; "
                "compare the selected answers under **Comparison insights**."
            )
            st.caption(
                "Explicit SQL previews: read-only subset, maximum 1,000 rows and 60-second timeout. "
                "A row limit does not cap warehouse scanning. Selected agents may execute tools internally."
            )
        metrics_container = st.container()
    try:
        main_connection, _ = get_comparison_connections()
        connection = cast(clients.Connection, main_connection.raw_connection)
        execution_context = clients.context(connection)
        agent_connections = {
            side: cast(clients.Connection, get_comparison_connections(side)[0].raw_connection)
            for side in ("left", "right")
        }
    except (
        SnowflakeError,
        ValueError,
        KeyError,
        StreamlitAPIException,
        StreamlitSecretNotFoundError,
    ) as exc:
        LOGGER.warning("Comparison connection unavailable: %s", type(exc).__name__)
        st.error(
            "Snowflake connection unavailable. Deployed comparisons use the app owner's "
            "configured Snowflake connection. Restart the app session after configuration changes. "
            "No alternate credentials or rights mode were tried."
            " For local testing, explicitly set COMPARISON_CONNECTION_MODE=local and "
            "configure SNOWFLAKE_ACCOUNT/USER/ROLE/WAREHOUSE/DATABASE/SCHEMA for developer SSO."
        )
        return
    if comparison_connection_mode() == "local":
        sidebar.warning(
            "Local developer SSO mode — not viewer caller's rights. "
            f"Account: {execution_context['account']} · role: {execution_context['role']} · "
            f"warehouse: {execution_context['warehouse']}."
        )
    else:
        sidebar.caption(
            f"Owner-rights account: {execution_context['account']} · role: {execution_context['role']} · "
            f"warehouse: {execution_context['warehouse']}. Requests use the app owner's privileges, "
            "not the viewer's role. Only test agents and data approved for app viewers."
        )
    context_key = fingerprint(
        json.dumps({"mode": comparison_connection_mode(), **execution_context}, sort_keys=True)
    )
    if st.session_state.get(f"{key}:context") != context_key:
        # Old evidence must not survive a change of access context.
        for old_key in list(st.session_state):
            if str(old_key).startswith(key + ":"):
                del st.session_state[old_key]
        st.session_state[f"{key}:context"] = context_key
    try:
        if refresh:
            for old_key in list(st.session_state):
                if str(old_key).startswith(f"{key}:onboarding:"):
                    del st.session_state[old_key]
        objects = discover_agents()
        st.session_state.pop(f"{key}:objects", None)
    except _ERRORS as exc:
        st.session_state.pop(f"{key}:objects", None)
        _show_error("Object discovery", exc)
        return
    if not objects:
        local = comparison_connection_mode() == "local"
        connection_label = "local developer SSO connection" if local else "owner-rights connection"
        access_guidance = (
            "Caller grants do not apply to local developer SSO. Check the developer's "
            "privileges on the agent and its database/schema. Compare CURRENT_ACCOUNT(), "
            "CURRENT_USER(), CURRENT_ROLE(), CURRENT_SECONDARY_ROLES() and SHOW AGENTS IN ACCOUNT "
            "in a worksheet with this connection's context. Refresh available objects "
            "after correcting access; restart the local server if its connection configuration changes."
            if local
            else (
                "Check the app owner's privileges on the agent and its database/schema. "
                "Caller grants and the viewer's worksheet role do not govern this connection. "
                "Refresh available objects after correcting owner access. "
                "No alternate credentials or rights mode were tried."
            )
        )
        st.warning(
            f"SHOW AGENTS IN ACCOUNT returned no agents for this {connection_label}. "
            f"Account: {execution_context['account']}. User: {execution_context.get('user', 'unavailable')}. "
            f"Primary role: {execution_context['role']}. Secondary roles: "
            f"{execution_context.get('secondary_roles', 'unavailable')}. "
            f"{access_guidance}"
        )
        return
    sidebar.caption(
        f"{len(objects):,} agents available to the effective role. "
        "Source: live SHOW AGENTS IN ACCOUNT; no evaluation-table filter."
    )
    for side in ("left", "right"):
        _finish_pending(f"{key}:{side}")
    try:
        preferred = str(load_settings(None).get("default_agent") or "")
        right_key = f"{key}:right:object"
        if _pending(f"{key}:right") is None:
            initialize_agent_selection(
                st.session_state, right_key, [obj.fqn for obj in objects], preferred
            )
        if preferred and matching_agent([obj.fqn for obj in objects], preferred) is None:
            sidebar.warning("The saved default agent is not accessible under the effective role.")
    except (SnowflakeError, SnowparkSQLException, ValueError, StreamlitAPIException) as exc:
        LOGGER.warning("Default-agent setting unavailable: %s", type(exc).__name__)
        sidebar.warning(
            "Default-agent setting unavailable under the effective role. Select agents manually."
        )
    report_fqn = None
    try:
        report_fqn = optional_report_fqn()
        sidebar.caption("Request enrichment: " + (report_fqn or "not configured; direct response metrics only"))
        sidebar.text_input("Default right agent (optional full identifier)", key="scout:default_agent")
    except (SnowflakeError, ValueError, StreamlitAPIException) as exc:
        LOGGER.warning("Comparison request-usage configuration unavailable: %s", type(exc).__name__)
        sidebar.caption(
            "Request-report enrichment unavailable; direct response and SQL telemetry remain inspectable."
        )
    _workspace(connection, agent_connections, objects, execution_context, key, report_fqn)
    with metrics_container:
        _metrics_panel(
            connection,
            [st.session_state[f"{key}:{side}"] for side in ("left", "right")],
            key,
            report_fqn,
        )
    if any(_pending(f"{key}:{side}") is not None for side in ("left", "right")):
        _poll_pending(key)
