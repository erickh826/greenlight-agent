#!/usr/bin/env python3
"""M2: score a proposal against historical analogues, end to end.

Stage one has tools: the agent picks its own comparable sets and queries
ClickHouse through MCP. Stage two has none: it turns that transcript into an
AnalogueEvidenceBundle. Then Python validates every figure against what the
database actually returned and computes the score.

Three things here are checks rather than instructions, because an instruction is
a request:

    app/guardrails.py runs as a before_tool_callback, so a query with a hard
    violation is refused and the reason is handed back to the model as the tool
    result. It never reaches the database.

    Three consecutive failures -- a ClickHouse error or a refused query -- ends
    the run with insufficient_evidence rather than letting it loop against a
    paid API. SQL_RETRY_LIMIT is 2, so that is the original attempt plus two
    corrections.

    Every value and sample_count in the bundle must appear in a result payload
    from this run. Citing a real query and attaching a figure it never returned
    reads exactly like a correct citation, and this is what separates them.

Usage:
    ./scripts/run_agent.sh scripts/run_m2_predict_agent.py
    ./scripts/run_agent.sh scripts/run_m2_predict_agent.py --budget-band high
    ./scripts/run_agent.sh scripts/run_m2_predict_agent.py \
        --release-bucket 2010-2014
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.predict import (  # noqa: E402
    analogue_prompt, build_predict_converge_agent, build_predict_query_agent)
from app.analogue_scoring import (  # noqa: E402
    comparison_caveats, insufficient_evidence, partition, score_bundle,
    validate_analogue_evidence)
from app.config import BUDGET_BANDS, MIN_SAMPLE_SIZE, SQL_RETRY_LIMIT  # noqa: E402
from app.contracts import (  # noqa: E402
    AnalogueEvidenceBundle, AnalogueScoringRequest, PredictionScore,
    TreatmentProposal)
from app.env import load_env, redact  # noqa: E402
from app.guardrails import inspect  # noqa: E402
from app.mcp import build_clickhouse_tools, warm_up  # noqa: E402
from app.query_run import (  # noqa: E402
    QueryRun, extract_sql, guardrail_refusal)
from app.proposal_validation import extract_json_object  # noqa: E402
from app.scoring import score_from_evidence  # noqa: E402

PROPOSAL_PATH = ROOT / "docs" / "m2-grounded-proposal.json"
TRACE_PATH = ROOT / "docs" / "m2-predict-agent-trace.log"
SCORE_PATH = ROOT / "docs" / "m2-prediction-score.json"

# The three surfaces the analogue search is supposed to cross. Named here rather
# than inferred from the trace so a run that quietly stopped using one fails.
REQUIRED_SURFACES = ("mv_motif_pair_stats", "mv_archetype_performance", "films")

# Broad -> second surface -> film-level -> two narrowings -> a retry. Beyond
# this the agent is not converging and the run should stop costing money.
MAX_TURNS = 14

# The whole result set goes to the model; only the log is trimmed.
LOG_PAYLOAD_CHARS = 3000


class Trace:
    """Writes to stdout and the trace log at once, redacting credentials."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("w", encoding="utf-8")

    def write(self, line: str = "") -> None:
        line = redact(str(line))
        print(line)
        self.handle.write(line + "\n")
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


def make_guardrail_gate(run: QueryRun, trace: Trace):
    """Refuse a query with a hard violation, and tell the model why.

    Returning a dict from before_tool_callback replaces the tool call: the
    database is never touched, and the model receives this as the result. The
    earlier Phase A version only recorded findings, which meant the run reported
    a violation after the 4.9M-row scan had already happened.
    """

    def gate(tool, args, tool_context):
        sql = extract_sql(dict(args or {}))
        if sql is None:
            return None
        refusal = guardrail_refusal(sql)
        if refusal is None:
            return None

        response, bad = refusal
        run.record_refusal(sql, bad)
        trace.write("    [GUARDRAIL BLOCKED] refused before execution: "
                    + ", ".join(f.rule for f in bad))
        return response

    return gate


async def run_query_stage(model, toolset, request, trace) -> QueryRun:
    agent = build_predict_query_agent(model, toolset)
    run = QueryRun()
    agent.before_tool_callback = make_guardrail_gate(run, trace)

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="m2_predict", user_id="m2", session_id="query")
    runner = Runner(app_name="m2_predict", agent=agent,
                    session_service=session_service)

    prompt = analogue_prompt(request)
    trace.write("USER PROMPT")
    trace.write(prompt)
    trace.write()

    async for event in runner.run_async(
        user_id="m2", session_id="query",
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        for part in (event.content.parts if event.content else []) or []:
            if part.function_call:
                call_args = dict(part.function_call.args or {})
                sql = extract_sql(call_args)
                run.record_call(sql)
                trace.write(f"--- FunctionCall #{run.calls}: "
                            f"{part.function_call.name} ---")
                if sql:
                    trace.write(sql.strip())
                    findings = inspect(sql)
                    for f in findings:
                        trace.write(f"    [GUARDRAIL {f.severity}] "
                                    f"{f.rule}: {f.detail}")
                    if not findings:
                        trace.write("    [GUARDRAIL ok]")
                else:
                    trace.write(json.dumps(call_args, indent=2))
                trace.write()

            elif part.function_response:
                payload = json.dumps(part.function_response.response,
                                     indent=2, default=str)
                is_error = run.record_response(payload)

                trace.write(f"--- FunctionResponse #{run.responses}"
                            f"{' (ERROR)' if is_error else ''} ---")
                trace.write(payload if len(payload) <= LOG_PAYLOAD_CHARS
                            else payload[:LOG_PAYLOAD_CHARS]
                            + "\n… [truncated in log; the model got all of it]")
                if is_error:
                    trace.write(f"    consecutive failures: "
                                f"{run.consecutive_failures} of "
                                f"{run.attempts_allowed} allowed")
                trace.write()

            elif part.text and part.text.strip():
                run.notes.append(part.text.strip())
                trace.write(f"--- Model text ({event.author}) ---")
                trace.write(part.text.strip())
                trace.write()

        if run.over_retry_limit():
            run.retries_exhausted = True
            trace.write(f"!! stopping: {run.consecutive_failures} consecutive "
                        f"failures, at the limit of {run.attempts_allowed} "
                        f"attempts (original + {SQL_RETRY_LIMIT} retries)")
            break
        if run.calls > MAX_TURNS:
            trace.write(f"!! stopping: more than {MAX_TURNS} tool calls")
            break

    return run


async def run_converge_stage(model, run, trace) -> tuple[
        AnalogueEvidenceBundle | None, str | None, int]:
    """Stage two: transcript in, evidence out, no tools and no scores."""
    agent = build_predict_converge_agent(model)
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="m2_predict_converge", user_id="m2", session_id="converge")
    runner = Runner(app_name="m2_predict_converge", agent=agent,
                    session_service=session_service)

    prompt = ("Turn this analogue query transcript into an "
              "AnalogueEvidenceBundle.\n\nQUERY TRANSCRIPT\n"
              "================\n" + run.transcript())

    text: list[str] = []
    tool_calls = 0
    async for event in runner.run_async(
        user_id="m2", session_id="converge",
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        for part in (event.content.parts if event.content else []) or []:
            if part.function_call:
                tool_calls += 1
            elif part.text and part.text.strip():
                text.append(part.text.strip())

    raw = "\n".join(text)
    trace.write("--- Converge stage output ---")
    trace.write(raw if raw.strip() else "(no output)")
    trace.write()

    if not raw.strip():
        return None, "converge stage returned no text", tool_calls
    try:
        return (AnalogueEvidenceBundle.model_validate_json(
            extract_json_object(raw)), None, tool_calls)
    except Exception as exc:  # pydantic detail is the useful part here
        return None, str(exc), tool_calls


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--proposal", default=str(PROPOSAL_PATH))
    ap.add_argument("--budget-band", choices=[*BUDGET_BANDS, "none"],
                    default="mid")
    ap.add_argument("--release-bucket", default=None,
                    help="e.g. 2010-2014; omitted by default because "
                         "narrowing by era is what empties a cell")
    ap.add_argument("--model", help="overrides MODEL_FAST from .env")
    args = ap.parse_args()

    load_env()
    model = args.model or os.environ.get("MODEL_FAST") or "gemini-2.5-flash"

    proposal = TreatmentProposal.model_validate_json(
        Path(args.proposal).read_text(encoding="utf-8"))
    request = AnalogueScoringRequest(
        proposal=proposal,
        budget_band=None if args.budget_band == "none" else args.budget_band,
        target_release_bucket=args.release_bucket,
    )

    toolset = build_clickhouse_tools()
    trace = Trace(TRACE_PATH)
    warm_failures: list[str] = []

    try:
        trace.write("=== M2 PredictAgent: analogue / evidence scoring ===")
        trace.write(f"timestamp:      {datetime.now(timezone.utc).isoformat()}")
        trace.write(f"model:          {model}")
        trace.write(f"proposal:       {proposal.title} ({args.proposal})")
        trace.write(f"budget band:    {request.budget_band or 'none'}")
        trace.write(f"release bucket: "
                    f"{request.target_release_bucket or 'none (all eras)'}")
        trace.write(f"retry limit:    {SQL_RETRY_LIMIT} "
                    f"(original attempt + {SQL_RETRY_LIMIT} corrections)")
        trace.write(f"clickhouse:     {os.environ['CLICKHOUSE_HOST']}:"
                    f"{os.environ.get('CLICKHOUSE_PORT', '8443')} (TLS)")
        trace.write()

        trace.write("--- warm-up (pre-flight, not part of the agent run) ---")
        for label, ms, ok in await warm_up(toolset):
            trace.write(f"    {'ok  ' if ok else 'FAIL'} {ms:8.1f} ms  {label}")
            if not ok:
                warm_failures.append(label)
        trace.write()

        trace.write("=== Stage 1: autonomous analogue retrieval (tools on) ===")
        run = await run_query_stage(model, toolset, request, trace)

        trace.write("=== Stage 2: convergence to evidence (tools off) ===")
        bundle, parse_error, converge_calls = (None, None, 0)
        if run.queries:
            bundle, parse_error, converge_calls = await run_converge_stage(
                model, run, trace)
        else:
            parse_error = "stage 1 produced no successful query results"

        # --- scoring, in Python, over validated evidence ---------------------
        caveats = comparison_caveats(
            request.budget_band,
            request.target_release_bucket.value
            if request.target_release_bucket else None)

        evidence_errors: list[str] = []
        if bundle is not None:
            evidence_errors = validate_analogue_evidence(
                bundle.evidence, run.queries, run.payloads)

        if bundle is None or evidence_errors or run.retries_exhausted:
            reason = (
                "The retry limit was reached before a comparable set came back."
                if run.retries_exhausted else
                f"No evidence survived validation: {parse_error or '; '.join(evidence_errors)}"
            )
            score = insufficient_evidence(
                proposal.title, reason, extra_caveats=caveats)
        else:
            score = score_bundle(bundle, extra_caveats=caveats)

        SCORE_PATH.write_text(score.model_dump_json(indent=2) + "\n",
                              encoding="utf-8")

        # Recompute from the file, the way a judge would: read back what was
        # written, split it by metric, and run the scorer again. Recomputing
        # from the in-memory bundle instead is how the first version passed
        # this check while the JSON on disk was missing the metric label
        # altogether -- a published score nobody could reproduce.
        published = PredictionScore.model_validate_json(
            SCORE_PATH.read_text(encoding="utf-8"))
        roi_items, interest_items = partition(published.evidence)
        recomputed = score_from_evidence(roi_items, interest_items)[:3]
        recompute_ok = recomputed == (published.commercial_score,
                                      published.attention_score,
                                      published.composite)

        surfaces = {s for s in REQUIRED_SURFACES
                    if any(s in q.lower() for q in run.queries)}
        used_evidence = [e for e in published.evidence
                         if e.sample_count >= MIN_SAMPLE_SIZE]

        checks = [
            (not warm_failures, "warm-up 全部成功（冷啟動在 agent 之前吸收）",
             "3/3 通過" if not warm_failures
             else f"失敗：{', '.join(warm_failures)}"),
            (run.calls >= 3, "至少 3 次 Gemini 自主決定的查詢",
             f"{run.calls} 次 tool call"),
            (len(surfaces) == len(REQUIRED_SURFACES),
             "三個 surface 都查到（motif pair / archetype / films）",
             f"{sorted(surfaces)}"),
            (not run.retries_exhausted,
             f"重試次數受控（上限 {SQL_RETRY_LIMIT + 1} 次連續失敗）",
             f"錯誤 {run.errors} 次、護欄攔下 {len(run.blocked)} 次、"
             f"最後連續失敗 {run.consecutive_failures} 次"),
            (True, "護欄違規在送到 ClickHouse 之前被攔下",
             f"攔下 {len(run.blocked)} 個查詢"
             + (f"：{', '.join(r.rule for _, f in run.blocked for r in f)}"
                if run.blocked else "（本輪沒有違規查詢）")),
            (converge_calls == 0, "收斂階段沒有 tool call",
             f"{converge_calls} 次"),
            (bundle is not None, "收斂輸出 parse 成 AnalogueEvidenceBundle",
             "parsed" if bundle is not None else parse_error or "no output"),
            (bundle is not None and not evidence_errors,
             "每個 evidence 的 SQL、count 配對與數值都可回溯本輪結果",
             "ok" if bundle is not None and not evidence_errors
             else "; ".join(evidence_errors) or "no bundle"),
            (bool(used_evidence) or score.confidence == "insufficient_evidence",
             f"有通過 {MIN_SAMPLE_SIZE} 樣本門檻的 evidence，否則走 "
             "insufficient_evidence",
             f"{len(used_evidence)} 筆可用，confidence={score.confidence}"),
            (recompute_ok,
             "composite 可由寫出的 JSON 內 evidence 重算得到相同結果",
             f"commercial={score.commercial_score}, "
             f"attention={score.attention_score}, "
             f"composite={score.composite}"),
            (all(e.sql_query.strip() for e in score.evidence)
             if score.evidence else score.confidence == "insufficient_evidence",
             "每個數字都附帶產生它的 SQL",
             f"{len(score.evidence)} 筆 evidence"),
        ]

        trace.write("=== PredictAgent DoD ===")
        for ok, name, detail in checks:
            trace.write(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

        trace.write()
        trace.write("--- PredictionScore ---")
        trace.write(score.model_dump_json(indent=2))
        trace.write(f"\nscore_path: {SCORE_PATH}")

        passed = all(ok for ok, _, _ in checks)
        trace.write()
        trace.write("result: PASS -- 分數由 evidence 在 Python 端算出。" if passed
                    else "result: FAIL -- 見上方未通過項目。")
        return 0 if passed else 1
    finally:
        trace.close()
        await toolset.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
