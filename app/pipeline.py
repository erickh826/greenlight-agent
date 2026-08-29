"""Root orchestration: prompt in, two scored proposals out.

Why this is Python and not ADK's SequentialAgent
------------------------------------------------
SequentialAgent chains sub-agents inside one invocation and one session. This
pipeline is not a chain of model calls -- it is model calls with decisions
between them:

    Phase B's output is parsed and validated against the Phase A transcript
    before anything downstream is allowed to use it;
    a failing variant does not stop the run, it produces a VariantOutcome with
    its errors and the other variant continues;
    the score is computed in app/scoring.py from validated evidence, not by an
    agent, so it cannot be a step in an agent chain at all.

The ADK's own answer for that shape is a custom BaseAgent rather than
SequentialAgent, and a custom BaseAgent would put the sub-agents back in one
shared session -- which is the specific thing Phase B must not have. Phase B has
an output_schema and no tools, and it must read the Phase A transcript as data
it is quoting, not as its own conversation history it can extend. Giving it that
history is how a "no tools" stage starts describing queries it ran.

So the sequence lives here, each stage gets its own Runner and session, and the
handoff between them is an explicit transcript. Every stage still emits ADK
events onto the bus, so the SSE stream sees one continuous run.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Sequence

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.predict import (
    analogue_prompt, build_predict_converge_agent, build_predict_query_agent)
from app.agents.recombine import (
    build_recombine_phase_a_agent, build_recombine_phase_b_grounded_agent,
    build_recombine_phase_b_wildcard_agent)
from app.agents.storyboard import build_storyboard_agent, storyboard_prompt
from app.analogue_scoring import (
    comparison_caveats, insufficient_evidence, resolve_bundle, score_bundle,
    validate_analogue_evidence)
from app.config import PROPOSAL_VARIANTS, SQL_RETRY_LIMIT
from app.contracts import (
    AnalogueEvidenceBundle, AnalogueScoringRequest, GreenlightRunResult,
    PredictionScore, StoryboardPlan, TreatmentProposal, VariantOutcome)
from app.media import validate_storyboard_plan
from app.events import Event, make_event
from app.proposal_validation import (
    extract_json_object, parse_treatment_proposal, validate_grounded_proposal,
    validate_wildcard_proposal)
from app.query_run import QueryRun, extract_sql, guardrail_refusal, parse_result

Emit = Callable[[Event], None]

# Broad -> second surface -> narrowings -> a retry. Beyond this the agent is not
# converging and the run should stop costing money. PredictAgent gets a separate
# ceiling because wildcard proposals can legitimately require more pair and
# archetype checks before convergence.
MAX_TURNS = 14
PREDICT_MAX_TURNS = 24
PHASE_A_COMPLETION_MAX_TURNS = 4

# Phase B can only ground motif_tags and character_archetypes in the Phase A
# transcript. Require at least one successful query against each aggregate before
# handing it off; otherwise the no-tools stage is forced to invent half of the
# vocabulary it needs.
RECOMBINE_REQUIRED_SURFACES = (
    "mv_motif_pair_stats",
    "mv_archetype_performance",
)

# Rows shown in a tool_result event. The model gets the whole set; this is what
# the interface renders beside the query.
PREVIEW_ROWS = 5

DEFAULT_PROMPT = (
    "Find promising narrative recombinations for a mid-budget original film. "
    "Use the database yourself: start broad, cite sample counts, and do not "
    "invent columns or vocabulary terms."
)


def recombine_surfaces_seen(run: QueryRun) -> set[str]:
    """Required Phase A evidence surfaces that returned a successful result."""
    haystack = "\n".join(run.queries).lower()
    return {surface for surface in RECOMBINE_REQUIRED_SURFACES
            if surface in haystack}


def recombine_missing_surfaces(run: QueryRun) -> list[str]:
    seen = recombine_surfaces_seen(run)
    return [surface for surface in RECOMBINE_REQUIRED_SURFACES
            if surface not in seen]


def _guardrail_gate(run: QueryRun, emit: Emit, agent: str):
    """Refuse a violating query before it reaches ClickHouse.

    Returning a dict from before_tool_callback replaces the call entirely: the
    database is never touched and the model receives this as the tool result,
    so the refusal doubles as the correction it retries from.
    """

    def gate(tool, args, tool_context):
        sql = extract_sql(dict(args or {}))
        if sql is None:
            return None
        refusal = guardrail_refusal(sql)
        if refusal is None:
            return None

        response, findings = refusal
        run.record_refusal(sql, findings)
        emit(make_event("tool_error", agent=agent, tool=getattr(tool, "name", "?"),
                        error=response["error"],
                        retry=run.consecutive_failures))
        return response

    return gate


async def drive(agent, prompt: str, emit: Emit, *, app_name: str,
                run: QueryRun | None = None,
                max_turns: int = MAX_TURNS) -> QueryRun:
    """Run one agent to completion, emitting events as it goes.

    One driver for both stage kinds. A no-tools agent simply produces no
    function_call parts, so its output arrives in run.notes and run.calls stays
    at zero -- which is then something the caller can assert rather than assume.
    """
    run = run or QueryRun()
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=app_name, user_id="greenlight", session_id=app_name)
    runner = Runner(app_name=app_name, agent=agent,
                    session_service=session_service)

    emit(make_event("agent_start", agent=agent.name))
    started: list[float] = []

    try:
        async for event in runner.run_async(
            user_id="greenlight", session_id=app_name,
            new_message=types.Content(
                role="user", parts=[types.Part(text=prompt)]),
        ):
            for part in (event.content.parts if event.content else []) or []:
                if part.function_call:
                    args = dict(part.function_call.args or {})
                    sql = extract_sql(args)
                    run.record_call(sql)
                    started.append(time.monotonic())
                    emit(make_event("tool_call", agent=agent.name,
                                    tool=part.function_call.name, args=args))

                elif part.function_response:
                    response = part.function_response.response
                    payload = json.dumps(response, indent=2, default=str)
                    elapsed = ((time.monotonic() - started.pop(0)) * 1000
                               if started else 0.0)
                    is_error = run.record_response(payload)

                    if is_error:
                        emit(make_event("tool_error", agent=agent.name,
                                        error=payload[:2000],
                                        elapsed_ms=elapsed,
                                        retry=run.consecutive_failures))
                    else:
                        parsed = parse_result(
                            response if isinstance(response, dict) else None)
                        columns, rows = parsed or ([], [])
                        emit(make_event(
                            "tool_result", agent=agent.name,
                            rows=len(rows), elapsed_ms=elapsed,
                            preview=[[str(v) for v in r]
                                     for r in rows[:PREVIEW_ROWS]],
                            payload={"columns": columns} if columns else {}))

                elif part.text and part.text.strip():
                    run.notes.append(part.text.strip())
                    emit(make_event("agent_output", agent=agent.name,
                                    message=part.text.strip()))

            if run.over_retry_limit():
                run.retries_exhausted = True
                emit(make_event(
                    "stage_failed", agent=agent.name,
                    retry=run.consecutive_failures,
                    message=f"{run.consecutive_failures} consecutive "
                            f"failures; stopping at the limit of "
                            f"{run.attempts_allowed} attempts"))
                break
            if run.calls > max_turns:
                emit(make_event("stage_failed", agent=agent.name,
                                message=f"more than {max_turns} tool calls"))
                break
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        run.model_errors.append(message)
        # Not terminal: drive() runs one stage and has no idea whether the run
        # can continue without it. Only run_greenlight decides that.
        emit(make_event(
            "stage_failed", agent=agent.name,
            message="model execution failed after "
                    f"{run.calls} tool calls and {run.responses} responses: "
                    + message[:1200]))

    return run


# --- stages -----------------------------------------------------------------

async def recombine_phase_a(model: str, toolset, prompt: str,
                            emit: Emit) -> QueryRun:
    """Autonomous querying. Everything downstream quotes this transcript."""
    run = QueryRun()
    agent = build_recombine_phase_a_agent(model, toolset)
    agent.before_tool_callback = _guardrail_gate(run, emit, agent.name)
    run = await drive(agent, prompt, emit, app_name="recombine_a", run=run)

    for attempt in range(SQL_RETRY_LIMIT):
        missing = recombine_missing_surfaces(run)
        if not missing:
            break

        emit(make_event(
            "agent_output", agent="root",
            message="Phase A handoff is missing required evidence surface(s): "
                    + ", ".join(missing)
                    + ". Asking RecombineAgent to retrieve the missing "
                      "aggregate evidence before Phase B."))

        extra = QueryRun()
        completion_agent = build_recombine_phase_a_agent(model, toolset)
        completion_agent.before_tool_callback = _guardrail_gate(
            extra, emit, completion_agent.name)
        extra = await drive(
            completion_agent,
            PHASE_A_COMPLETION_PROMPT.format(
                missing=", ".join(missing),
                transcript=run.transcript()),
            emit,
            app_name=f"recombine_a_complete_{attempt + 1}",
            run=extra,
            max_turns=PHASE_A_COMPLETION_MAX_TURNS,
        )
        run.extend(extra)

    return run


PHASE_A_COMPLETION_PROMPT = """The previous Phase A transcript is incomplete
for grounded Phase B. It is missing successful ClickHouse results from:
{missing}

Run only the additional broad aggregate query or queries needed to cover those
missing surfaces. Use mcp-clickhouse. Do not write a TreatmentProposal and do
not answer from memory.

Use these query shapes if the matching surface is missing:

mv_motif_pair_stats:
SELECT motif_a, motif_b,
       countMerge(sample_count) AS n_roi,
       quantileMerge(0.5)(roi_median) AS roi_median,
       countMerge(interest_sample_count) AS n_interest,
       quantileMerge(0.5)(interest_pct_median) AS interest_median
FROM mv_motif_pair_stats
GROUP BY motif_a, motif_b
HAVING n_roi >= 8
ORDER BY roi_median DESC, interest_median DESC
LIMIT 10

mv_archetype_performance:
SELECT archetype,
       countMerge(sample_count) AS n_roi,
       quantileMerge(0.5)(roi_median) AS roi_median,
       countMerge(interest_sample_count) AS n_interest,
       quantileMerge(0.5)(interest_pct_median) AS interest_median
FROM mv_archetype_performance
GROUP BY archetype
HAVING n_roi >= 8
ORDER BY roi_median DESC, interest_median DESC
LIMIT 10

PREVIOUS PHASE A TRANSCRIPT
===========================
{transcript}
"""


PHASE_B_PROMPT = """Create exactly one {variant} TreatmentProposal from the
Phase A transcript below.

- Copy each evidence sql_query exactly from the transcript.
- Use source_view values that match the table or view in sql_query.
- Keep logline to one sentence and no more than 200 characters.

PHASE A TRANSCRIPT
==================
{transcript}
"""


CORRECTION_PROMPT = """Your previous answer was rejected. Fix exactly this and
return the whole proposal again:

{errors}

Everything else about the task is unchanged."""


async def converge_with_retry(
    *,
    builder: Callable[[], object],
    prompt: str,
    emit: Emit,
    app_name: str,
    label: str,
    parse: Callable[[str], object],
    validate: Callable[[object], list[str]],
) -> tuple[object | None, list[str], QueryRun]:
    """Run a no-tools schema stage, retrying on a rejected answer.

    The budget is SQL_RETRY_LIMIT + 1, the same as a failing query, because the
    failure is the same kind: an attempt that did not work and a stated reason
    it did not. What makes the retry worth having is that response_schema is
    not the guarantee it looks like -- Gemini enforces the shape and the enums
    but not a string maxLength, so a logline running to 214 characters comes
    back as valid JSON that pydantic then refuses. Without a retry that ended
    the variant outright, which is how a run lost its grounded proposal to a
    logline fourteen characters too long while the wildcard scored fine beside
    it.

    The correction carries the validation errors verbatim. A model told "the
    logline must be at most 200 characters" after writing 214 fixes it; a model
    told "try again" writes something else that is also too long.

    Each attempt gets a fresh session. The rejected answer is not context the
    next attempt should build on -- only the stated reason is.
    """
    run = QueryRun()
    errors: list[str] = []
    parsed: object | None = None

    for attempt in range(SQL_RETRY_LIMIT + 1):
        full = prompt if attempt == 0 else (
            prompt + "\n\n" + CORRECTION_PROMPT.format(
                errors="\n".join(f"- {e}" for e in errors)))
        run = await drive(builder(), full, emit,
                          app_name=f"{app_name}_{attempt}")

        errors = []
        if run.calls:
            errors.append(f"{label} made {run.calls} tool calls; this stage "
                          "must not query")

        raw = "\n".join(run.notes)
        parsed = None
        if not raw.strip():
            errors.append(f"{label} returned no output")
        else:
            try:
                parsed = parse(raw)
            except Exception as exc:
                errors.append(f"{label} output did not parse: {exc}")

        if parsed is not None:
            errors += validate(parsed)

        if not errors:
            return parsed, [], run

        emit(make_event("agent_retry", agent=app_name, retry=attempt + 1,
                        message=f"attempt {attempt + 1} of "
                                f"{SQL_RETRY_LIMIT + 1} rejected: "
                                + "; ".join(errors)))

    return parsed, errors, run


async def recombine_phase_b(model: str, variant: str, transcript: str,
                            emit: Emit) -> tuple[TreatmentProposal | None,
                                                 list[str], QueryRun]:
    """Transcript in, one schema-bound proposal out, tools off."""
    builder = (build_recombine_phase_b_grounded_agent if variant == "grounded"
               else build_recombine_phase_b_wildcard_agent)
    validator = (validate_grounded_proposal if variant == "grounded"
                 else validate_wildcard_proposal)

    return await converge_with_retry(
        builder=lambda: builder(model),
        prompt=PHASE_B_PROMPT.format(variant=variant, transcript=transcript),
        emit=emit,
        app_name=f"recombine_b_{variant}",
        label=f"{variant} phase B",
        parse=parse_treatment_proposal,
        validate=lambda proposal: validator(proposal, transcript),
    )


async def plan_storyboard(model: str, proposal: TreatmentProposal,
                          emit: Emit) -> tuple[StoryboardPlan | None,
                                               list[str], QueryRun]:
    """Approved proposal in, one validated StoryboardPlan out, tools off.

    Runs before any image or audio exists, and that ordering is the whole point:
    Imagen and Cloud TTS are the only irreversible spend in this pipeline, and
    a plan can be checked against the approved proposal for nothing. What the
    check is for is not an ugly frame -- it is three good frames of the wrong
    film.
    """
    return await converge_with_retry(
        builder=lambda: build_storyboard_agent(model),
        prompt=storyboard_prompt(proposal),
        emit=emit,
        app_name="storyboard",
        label="storyboard",
        parse=lambda raw: StoryboardPlan.model_validate_json(
            extract_json_object(raw)),
        validate=lambda plan: validate_storyboard_plan(plan, proposal),
    )


class ScoringOutcome:
    """Everything a caller needs to check the scoring, not just its result."""

    def __init__(self, score: PredictionScore, query_run: QueryRun,
                 bundle: AnalogueEvidenceBundle | None,
                 parse_error: str | None, evidence_errors: list[str],
                 converge_calls: int):
        self.score = score
        self.query_run = query_run
        self.bundle = bundle
        self.parse_error = parse_error
        self.evidence_errors = evidence_errors
        self.converge_calls = converge_calls


async def score_proposal(model: str, toolset,
                         request: AnalogueScoringRequest,
                         emit: Emit) -> ScoringOutcome:
    """Retrieve analogues, converge to evidence, score in Python."""
    variant = request.proposal.variant
    query_run = QueryRun()
    query_agent = build_predict_query_agent(model, toolset)
    query_agent.before_tool_callback = _guardrail_gate(
        query_run, emit, query_agent.name)
    query_run = await drive(query_agent, analogue_prompt(request), emit,
                            app_name=f"predict_query_{variant}",
                            run=query_run, max_turns=PREDICT_MAX_TURNS)

    caveats = comparison_caveats(
        request.budget_band,
        request.target_release_bucket.value
        if request.target_release_bucket else None)

    if not query_run.queries:
        return ScoringOutcome(
            score=insufficient_evidence(
                request.proposal.title,
                "No query returned rows: "
                + ("the retry limit was reached." if query_run.retries_exhausted
                   else "the retrieval stage produced no usable result."),
                extra_caveats=caveats),
            query_run=query_run, bundle=None,
            parse_error="stage 1 produced no successful query results",
            evidence_errors=[], converge_calls=0)

    converge_run = await drive(
        build_predict_converge_agent(model),
        "Turn this analogue query transcript into an AnalogueEvidenceBundle."
        "\n\nQUERY TRANSCRIPT\n================\n" + query_run.transcript(),
        emit, app_name=f"predict_converge_{variant}")

    raw = "\n".join(converge_run.notes)
    bundle: AnalogueEvidenceBundle | None = None
    parse_error: str | None = None
    if not raw.strip():
        parse_error = "converge stage returned no text"
    else:
        try:
            bundle = AnalogueEvidenceBundle.model_validate_json(
                extract_json_object(raw))
        except Exception as exc:
            parse_error = str(exc)

    evidence_errors: list[str] = []
    evidence = []
    if bundle is not None:
        evidence, evidence_errors = resolve_bundle(bundle, query_run.queries)
        evidence_errors += validate_analogue_evidence(
            evidence, query_run.queries, query_run.payloads)

    # All or nothing, and the published score carries no evidence rather than
    # rejected evidence: a reader who sees items listed under a score reads them
    # as the score's basis. What was rejected, and why, is in the caveat and the
    # trace.
    if bundle is None or evidence_errors:
        score = insufficient_evidence(
            request.proposal.title,
            "No evidence survived validation: "
            + (parse_error or "; ".join(evidence_errors)),
            extra_caveats=caveats)
    else:
        score = score_bundle(bundle, evidence, extra_caveats=caveats)

    return ScoringOutcome(score=score, query_run=query_run, bundle=bundle,
                          parse_error=parse_error,
                          evidence_errors=evidence_errors,
                          converge_calls=converge_run.calls)


# --- the run ----------------------------------------------------------------

async def run_greenlight(
    model: str,
    toolset,
    emit: Emit,
    *,
    prompt: str = DEFAULT_PROMPT,
    variants: Sequence[str] = PROPOSAL_VARIANTS,
    budget_band: str | None = "mid",
    release_bucket: str | None = None,
    run_id: str | None = None,
) -> tuple[GreenlightRunResult, QueryRun, list[ScoringOutcome]]:
    """Phase A once, then each variant proposed and scored against it.

    Phase A runs once and both variants quote the same transcript, which is what
    makes the wildcard a control rather than a second opinion: the two proposals
    differ in what they chose to do with the evidence, not in what evidence they
    were shown.

    A variant that fails does not end the run. Its VariantOutcome carries the
    errors and the next variant proceeds, because a demo losing the control
    branch should still show the grounded one.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    started = time.time()

    phase_a = await recombine_phase_a(model, toolset, prompt, emit)
    transcript = phase_a.transcript()

    outcomes: list[VariantOutcome] = []
    scorings: list[ScoringOutcome] = []

    for variant in variants:
        proposal, errors, _ = await recombine_phase_b(
            model, variant, transcript, emit)
        if proposal is None or errors:
            outcomes.append(VariantOutcome(variant=variant, proposal=proposal,
                                           score=None,
                                           validation_errors=errors))
            emit(make_event("stage_failed", agent=f"recombine_b_{variant}",
                            message="; ".join(errors)))
            continue

        outcome = await score_proposal(
            model, toolset,
            AnalogueScoringRequest(proposal=proposal, budget_band=budget_band,
                                   target_release_bucket=release_bucket),
            emit)
        scorings.append(outcome)
        outcomes.append(VariantOutcome(variant=variant, proposal=proposal,
                                       score=outcome.score))

    result = GreenlightRunResult(
        run_id=run_id, prompt=prompt, model=model,
        started_at=started, finished_at=time.time(),
        outcomes=outcomes,
        tool_calls=phase_a.calls + sum(s.query_run.calls for s in scorings),
        sql_errors=phase_a.errors + sum(s.query_run.errors for s in scorings),
        guardrail_blocks=len(phase_a.blocked)
        + sum(len(s.query_run.blocked) for s in scorings),
    )
    # awaiting_approval, not done: analysis finishing is not the run finishing.
    # The next thing that happens is a person choosing a variant, and only the
    # caller knows whether there is one -- the CLI publishes `done` right after
    # this, the API keeps the stream open for /approve. Emitting a terminal
    # event here would close every browser's SSE connection at exactly the
    # moment the proposals appear on screen.
    emit(make_event("awaiting_approval", agent="root",
                    proposals=[o.proposal.model_dump(mode="json")
                               for o in outcomes if o.proposal],
                    scores=[o.score.model_dump(mode="json")
                            for o in outcomes if o.score]))
    return result, phase_a, scorings


__all__ = ["run_greenlight", "recombine_phase_a", "recombine_phase_b",
           "score_proposal", "drive", "ScoringOutcome", "DEFAULT_PROMPT",
           "MAX_TURNS", "PREDICT_MAX_TURNS", "RECOMBINE_REQUIRED_SURFACES",
           "recombine_missing_surfaces", "recombine_surfaces_seen"]
