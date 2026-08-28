"""Accounting for one tools-on agent run: what it asked, what came back.

Split out of the runner script because these are the rules the runner is
supposed to enforce, and rules that only exist inside an async event loop over a
paid API get verified by hoping. Nothing here imports the ADK, so the retry
limit and the guardrail refusal can be exercised directly.

The runner keeps the ADK plumbing and the trace writing. This keeps the counts.
"""

from __future__ import annotations

import json

from app.config import SQL_RETRY_LIMIT
from app.guardrails import Finding, inspect, is_error_response, violations

SQL_ARG_KEYS = ("query", "sql", "statement")


def extract_sql(args: dict) -> str | None:
    """The query text out of a run_query call, whatever the arg is named."""
    for key in SQL_ARG_KEYS:
        value = (args or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def guardrail_refusal(sql: str) -> tuple[dict, list[Finding]] | None:
    """The tool result to return instead of running a violating query.

    Returned from the ADK's before_tool_callback, this replaces the call: the
    database is never reached and the model gets the reason back in place of
    rows. Warnings do not refuse -- they are recorded and the query runs, which
    is the difference between "this shape is usually wrong" and "this must not
    happen".
    """
    bad = violations(inspect(sql))
    if not bad:
        return None
    return {
        "isError": True,
        "error": "Query refused by the client-side guardrail before it reached "
                 "ClickHouse. Fix and retry: "
                 + " | ".join(f"{f.rule}: {f.detail}" for f in bad),
    }, bad


def parse_result(response: dict | None) -> tuple[list[str], list[list]] | None:
    """(columns, rows) out of an mcp-clickhouse run_query response.

    The payload nests the interesting part twice: the MCP envelope carries a
    text block, and that text is itself JSON of the form
    {"columns": [...], "rows": [[...]]}. Pulling it apart means the SSE
    tool_result event can carry a real row count and a preview instead of a
    length-of-string guess.

    Returns None for anything that is not a result set -- an error, a
    list_tables call, a shape mcp-clickhouse changes later. Callers treat that
    as "no rows to report", never as zero rows.
    """
    if not isinstance(response, dict):
        return None

    text = None
    structured = response.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        text = structured["result"]
    else:
        for block in response.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text = block["text"]
                break
    if text is None:
        return None

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    columns = parsed.get("columns")
    rows = parsed.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None
    return [str(c) for c in columns], rows


class QueryRun:
    """What a tools-on stage produced, and how badly it went.

    consecutive_failures, not total failures. An agent that hits an error,
    corrects it, and later hits a different one is working; an agent that fails
    three times running is not converging, and the third failure ends the run
    with insufficient_evidence rather than more turns against a paid API.
    """

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.payloads: list[str] = []
        self.notes: list[str] = []
        self.blocked: list[tuple[str, list[Finding]]] = []
        self.model_errors: list[str] = []
        # Calls and their responses arrive as separate events, so the SQL has
        # to be held between the two. FIFO: the ADK emits them in order, and a
        # guardrail-refused call still produces a response that consumes its
        # entry.
        self.pending_sql: list[str] = []
        self.calls = 0
        self.responses = 0
        self.errors = 0
        self.consecutive_failures = 0
        self.retries_exhausted = False

    # --- recording ----------------------------------------------------------

    def record_call(self, sql: str | None) -> None:
        self.calls += 1
        self.pending_sql.append(sql or "")

    def record_response(self, payload: str) -> bool:
        """Fold one tool response in. Returns whether it was a failure."""
        self.responses += 1
        sql = self.pending_sql.pop(0) if self.pending_sql else ""
        is_error = is_error_response(payload)
        self.errors += is_error
        if is_error:
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 0
            # Only successful results become citable evidence; a query paired
            # with an error message produced no rows to cite.
            self.queries.append(sql)
            self.payloads.append(payload)
        return bool(is_error)

    def record_refusal(self, sql: str, findings: list[Finding]) -> None:
        self.blocked.append((sql, findings))

    def extend(self, other: "QueryRun") -> None:
        """Merge a follow-up run into this stage's accounting.

        Follow-up sessions are how the root pipeline repairs an incomplete
        evidence handoff without pretending one model invocation did work it
        skipped. Only completed query/result pairs become transcript evidence;
        pending calls are session-local and are left out.
        """
        self.queries.extend(other.queries)
        self.payloads.extend(other.payloads)
        self.notes.extend(other.notes)
        self.blocked.extend(other.blocked)
        self.model_errors.extend(other.model_errors)
        self.calls += other.calls
        self.responses += other.responses
        self.errors += other.errors
        self.consecutive_failures = other.consecutive_failures
        self.retries_exhausted = (
            self.retries_exhausted or other.retries_exhausted)

    # --- limits -------------------------------------------------------------

    @property
    def attempts_allowed(self) -> int:
        """The original attempt plus SQL_RETRY_LIMIT corrections."""
        return SQL_RETRY_LIMIT + 1

    def over_retry_limit(self) -> bool:
        return self.consecutive_failures >= self.attempts_allowed

    # --- handing stage one to stage two -------------------------------------

    def transcript(self) -> str:
        """The stage-one record, as the convergence stage receives it."""
        blocks = []
        for i, (sql, payload) in enumerate(
                zip(self.queries, self.payloads), start=1):
            blocks.append(f"QUERY {i}\n{sql.strip()}\n\nRESULT {i}\n{payload}")
        if self.notes:
            blocks.append("AGENT NOTES\n" + "\n\n".join(self.notes))
        return "\n\n".join(blocks)


__all__ = ["QueryRun", "extract_sql", "guardrail_refusal", "parse_result",
           "SQL_ARG_KEYS"]
