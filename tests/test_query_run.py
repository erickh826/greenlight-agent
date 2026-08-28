"""Tests for the retry limit and the guardrail refusal.

Both live inside an async loop over a paid API in the runner, which is exactly
why they are here: a limit that has never been reached is a limit nobody has
checked. app/query_run.py holds the accounting so these can run offline.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app.config import SQL_RETRY_LIMIT  # noqa: E402
from app.query_run import (  # noqa: E402
    QueryRun, extract_sql, guardrail_refusal)

OK_PAYLOAD = '{"content": [{"text": "n,roi\\n25,3.51"}], "isError": false}'
ERR_PAYLOAD = ('{"content": [{"text": "Code: 47. DB::Exception: Unknown '
               'expression identifier roi_p50"}], "isError": true}')
GOOD_SQL = ("SELECT archetype, countMerge(sample_count) AS n "
            "FROM mv_archetype_performance GROUP BY archetype")
SCAN_SQL = "SELECT avg(views) FROM film_attention"


def run_with(*payloads) -> QueryRun:
    run = QueryRun()
    for payload in payloads:
        run.record_call(GOOD_SQL)
        run.record_response(payload)
    return run


# --- retry accounting -------------------------------------------------------

def test_third_consecutive_failure_hits_the_limit():
    run = run_with(ERR_PAYLOAD, ERR_PAYLOAD)
    assert not run.over_retry_limit()          # original attempt + 1 retry
    run.record_call(GOOD_SQL)
    run.record_response(ERR_PAYLOAD)
    assert run.over_retry_limit()
    assert run.consecutive_failures == SQL_RETRY_LIMIT + 1


def test_a_success_resets_the_streak():
    """Two failures, a correction, then a later failure is not exhaustion."""
    run = run_with(ERR_PAYLOAD, ERR_PAYLOAD, OK_PAYLOAD, ERR_PAYLOAD)
    assert run.errors == 3
    assert run.consecutive_failures == 1
    assert not run.over_retry_limit()


def test_failed_queries_are_not_citable():
    run = run_with(ERR_PAYLOAD, OK_PAYLOAD)
    assert run.queries == [GOOD_SQL]
    assert run.payloads == [OK_PAYLOAD]


def test_attempts_allowed_is_original_plus_retries():
    assert QueryRun().attempts_allowed == SQL_RETRY_LIMIT + 1


def test_call_and_response_stay_paired_across_events():
    """The SQL is held between the call event and its response event."""
    run = QueryRun()
    run.record_call("SELECT 1")
    run.record_call("SELECT 2")
    run.record_response(OK_PAYLOAD)
    run.record_response(OK_PAYLOAD)
    assert run.queries == ["SELECT 1", "SELECT 2"]


def test_extend_merges_follow_up_run_without_pending_sql():
    base = run_with(OK_PAYLOAD)
    extra = QueryRun()
    extra.notes.append("needed archetype surface")
    extra.model_errors.append("ResourceExhausted: 429")
    extra.record_call("SELECT 2")
    extra.record_response(OK_PAYLOAD)
    extra.record_call("SELECT 3")

    base.extend(extra)

    assert base.calls == 3
    assert base.responses == 2
    assert base.queries == [GOOD_SQL, "SELECT 2"]
    assert base.payloads == [OK_PAYLOAD, OK_PAYLOAD]
    assert base.notes == ["needed archetype surface"]
    assert base.model_errors == ["ResourceExhausted: 429"]
    assert base.pending_sql == []


# --- the guardrail refuses before the database is touched -------------------

def test_violating_query_is_refused_with_a_reason():
    refusal = guardrail_refusal(SCAN_SQL)
    assert refusal is not None
    response, findings = refusal
    assert response["isError"] is True
    assert "unbounded_attention_scan" in response["error"]
    assert [f.rule for f in findings] == ["unbounded_attention_scan"]


def test_clean_query_is_not_refused():
    assert guardrail_refusal(GOOD_SQL) is None


def test_a_warning_does_not_refuse():
    """narrow_without_floor is worth recording, not worth blocking."""
    warned = ("SELECT archetype, countMerge(sample_count) AS n "
              "FROM mv_archetype_performance "
              "WHERE release_bucket = '2010-2014' GROUP BY archetype")
    assert guardrail_refusal(warned) is None


def test_refusal_counts_as_a_failure_in_the_retry_budget():
    """A refused query comes back through record_response like any error."""
    run = QueryRun()
    for _ in range(SQL_RETRY_LIMIT + 1):
        run.record_call(SCAN_SQL)
        response, findings = guardrail_refusal(SCAN_SQL)
        run.record_refusal(SCAN_SQL, findings)
        run.record_response(str(response).replace("'", '"'))
    assert run.over_retry_limit()
    assert len(run.blocked) == SQL_RETRY_LIMIT + 1
    assert run.queries == []


# --- plumbing ---------------------------------------------------------------

def test_extract_sql_accepts_the_names_mcp_clickhouse_uses():
    assert extract_sql({"query": "SELECT 1"}) == "SELECT 1"
    assert extract_sql({"sql": "SELECT 2"}) == "SELECT 2"
    assert extract_sql({"database": "default"}) is None
    assert extract_sql({"query": "   "}) is None


def test_transcript_pairs_each_query_with_its_result():
    run = run_with(OK_PAYLOAD)
    run.notes.append("mid-budget set held at 13 films")
    transcript = run.transcript()
    assert "QUERY 1" in transcript and "RESULT 1" in transcript
    assert GOOD_SQL in transcript
    assert "mid-budget set held at 13 films" in transcript
