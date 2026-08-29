"""FastAPI surface for the browser demo.

Three things a judge has to be able to do from a URL: start a run, watch the SQL
go past as it happens, and pick one of the two proposals. Everything here exists
for that, and the analysis itself is unchanged -- app/pipeline.py is the same
code the CLI runs, so what the browser shows is not a second implementation that
could drift into looking better than the real one.

Deliberate shapes, each with a reason:

    POST /run returns a run_id immediately and does the work in a background
    task. The analysis takes about four minutes; an HTTP request that waits for
    it dies to a proxy timeout long before it finishes.

    The admission slot guards the analysis phase only, and is released when the
    proposals are ready. Holding it across the approval gate means one visitor
    who walks away after seeing their proposals locks every later visitor out
    until the process restarts -- which, on a single-instance deployment during
    judging, is the whole demo.

    google.adk is imported inside the background task, not at module scope.
    That keeps the API testable without the agent stack, and keeps a broken
    model dependency from taking down /health.

SINGLE INSTANCE ONLY. RunStore and InProcessEventBus live in this process's
memory, so a second instance would serve /events for runs it has never heard of
and the stream would hang open producing nothing. See app/events.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.env import load_env
from app.events import Event, InProcessEventBus, make_event
from app.state import RunState, RunStore

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

# The analysis is the expensive, rate-limited part: a Gemini agent loop plus
# live ClickHouse queries. One at a time on a single instance.
ANALYSIS_SLOT = asyncio.Semaphore(1)
# Media generation is separately expensive and separately limited, so an
# approval landing while someone else's storyboard renders waits its turn
# instead of doubling the Imagen bill.
MEDIA_SLOT = asyncio.Semaphore(1)

# A run sitting at the approval gate holds no slot, but it does hold memory and
# a stream. After this it is closed out so the store does not grow without
# bound across a day of judging.
APPROVAL_TTL_SEC = 600

# Public demo limits. The prompt is bounded because it goes into a model call
# billed to us, and free-form text on an open URL is an invitation.
MAX_PROMPT_CHARS = 400
RATE_LIMIT_RUNS = 3
RATE_LIMIT_WINDOW_SEC = 600

# How often finished runs are forgotten, and how old they must be. Judging runs
# for hours; without this the store and every run's event history stay in memory
# for the life of the process.
SWEEP_INTERVAL_SEC = 300
RUN_RETENTION_SEC = 3600


async def _sweep_loop() -> None:
    """Forget finished runs, from the store and the bus together."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SEC)
        for run_id in store.sweep_ids(RUN_RETENTION_SEC):
            bus.discard(run_id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if (ROOT / ".env").exists():
        load_env()
    sweeper = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        sweeper.cancel()


app = FastAPI(title="Greenlight", docs_url=None, redoc_url=None,
              lifespan=lifespan)
store = RunStore()
bus = InProcessEventBus()
_recent_runs: dict[str, deque[float]] = defaultdict(deque)


class RunRequest(BaseModel):
    prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)


class ApproveRequest(BaseModel):
    variant: Literal["grounded", "wildcard"]


def _client_ip(request: Request) -> str:
    """Cloud Run puts the real client first in X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    seen = _recent_runs[ip]
    cutoff = time.time() - RATE_LIMIT_WINDOW_SEC
    while seen and seen[0] < cutoff:
        seen.popleft()
    if len(seen) >= RATE_LIMIT_RUNS:
        return True
    seen.append(time.time())
    return False


async def _analyse(run_id: str, prompt: str) -> None:
    """Background task: the CLI pipeline, publishing to this run's stream.

    Imports are local on purpose -- see the module docstring. The slot is held
    for the analysis and released the moment proposals exist, before the run
    waits on a human.
    """
    from app.mcp import build_clickhouse_tools, warm_up
    from app.pipeline import DEFAULT_PROMPT, run_greenlight

    run = store.require(run_id)
    toolset = None
    gate_event: Event | None = None

    def emit(event: Event) -> None:
        """Publish analysis progress, but hold the gate until state is stored."""
        nonlocal gate_event
        stamped: Event = {**event, "run_id": run_id}
        if stamped["type"] == "awaiting_approval":
            gate_event = stamped
            return
        bus.publish(run_id, stamped)

    try:
        async with ANALYSIS_SLOT:
            toolset = build_clickhouse_tools()
            await warm_up(toolset)
            result, _, _ = await run_greenlight(
                os.environ.get("MODEL_FAST") or "gemini-2.5-flash",
                toolset,
                emit,
                prompt=prompt or DEFAULT_PROMPT,
                run_id=run_id,
            )

        run.proposals = [o.proposal.model_dump(mode="json")
                         for o in result.outcomes if o.proposal]
        run.scores = [o.score.model_dump(mode="json")
                      for o in result.outcomes if o.score]
        if not run.scores:
            raise RuntimeError("no variant produced a score")

        run.transition(RunState.AWAITING_APPROVAL)
        if gate_event is None:
            gate_event = make_event("awaiting_approval", agent="root")
        bus.publish(run_id, {**gate_event, "run_id": run_id,
                             "proposals": run.proposals, "scores": run.scores})
        asyncio.create_task(_expire_approval(run_id))
    except Exception as exc:
        run.fail(f"{type(exc).__name__}: {exc}")
        bus.publish(run_id, make_event("error", run_id=run_id, agent="root",
                                       error=str(exc)[:1000]))
        bus.close(run_id)
    finally:
        if toolset is not None:
            with contextlib.suppress(Exception):
                await toolset.close()


async def _expire_approval(run_id: str) -> None:
    """Close out a run nobody came back to approve."""
    await asyncio.sleep(APPROVAL_TTL_SEC)
    run = store.get(run_id)
    if run is None or run.state is not RunState.AWAITING_APPROVAL:
        return
    run.fail(f"no variant approved within {APPROVAL_TTL_SEC // 60} minutes")
    bus.publish(run_id, make_event("error", run_id=run_id, agent="root",
                                   error="approval window expired"))
    bus.close(run_id)


async def _render_approved_variant(run_id: str, variant: str) -> None:
    """Storyboard and render media after HITL approval.

    Approval itself is a cheap state transition. This is the expensive branch:
    one no-tools storyboard pass, three Imagen calls, three TTS calls and GCS
    uploads. It has its own slot so a media render cannot overlap another media
    render, while analysis remains free to start for the next visitor.
    """
    from app.contracts import TreatmentProposal
    from app.media import render_storyboard_media
    from app.pipeline import plan_storyboard

    def emit(event: Event) -> None:
        bus.publish(run_id, {**event, "run_id": run_id})

    def progress(message: str) -> None:
        bus.publish(run_id, make_event("agent_output", run_id=run_id,
                                       agent="media", message=message))

    try:
        async with MEDIA_SLOT:
            run = store.require(run_id)
            proposal_data = next(
                (p for p in run.proposals if p.get("variant") == variant),
                None)
            if proposal_data is None:
                raise RuntimeError(f"run produced no {variant} proposal")

            proposal = TreatmentProposal.model_validate(proposal_data)
            model = os.environ.get("MODEL_FAST") or "gemini-2.5-flash"
            plan, errors, _ = await plan_storyboard(model, proposal, emit)
            if plan is None:
                raise RuntimeError("storyboard plan was not produced: "
                                   + "; ".join(errors))
            if errors:
                raise RuntimeError("storyboard plan rejected: "
                                   + "; ".join(errors))

            bus.publish(run_id, make_event("agent_start", run_id=run_id,
                                           agent="media"))
            assets = await render_storyboard_media(
                run_id, plan, progress=progress)

            run.scenes = [a.model_dump(mode="json") for a in assets]
            bus.publish(run_id, make_event("media_ready", run_id=run_id,
                                           agent="media", scenes=run.scenes))
            run.transition(RunState.DONE)
            bus.publish(run_id, make_event("done", run_id=run_id,
                                           agent="root"))
            bus.close(run_id)
    except Exception as exc:
        run = store.get(run_id)
        if run is not None:
            run.fail(f"{type(exc).__name__}: {exc}")
        bus.publish(run_id, make_event("error", run_id=run_id, agent="media",
                                       error=str(exc)[:1000]))
        bus.close(run_id)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    """Liveness only, and deliberately cheap.

    No MCP call here. The ClickHouse cold start has been measured at 32
    seconds, and a startup probe that waits on it fails, kills the container and
    restarts it into the same cold start. Readiness lives at /ready.
    """
    return {"ok": True, "runs": store.count()}


@app.get("/ready")
async def ready() -> dict:
    """Warms the MCP path the agents use, and reports what it cost."""
    from app.mcp import build_clickhouse_tools, warm_up

    toolset = build_clickhouse_tools()
    try:
        results = await warm_up(toolset)
    finally:
        with contextlib.suppress(Exception):
            await toolset.close()
    return {"ok": all(ok for _, _, ok in results),
            "checks": [{"query": label, "ms": round(ms, 1), "ok": ok}
                       for label, ms, ok in results]}


@app.post("/run")
async def start_run(body: RunRequest, request: Request) -> dict:
    if _rate_limited(_client_ip(request)):
        raise HTTPException(
            429, f"at most {RATE_LIMIT_RUNS} runs per "
                 f"{RATE_LIMIT_WINDOW_SEC // 60} minutes from one address")
    if ANALYSIS_SLOT.locked():
        raise HTTPException(
            409, "another analysis is running; this demo handles one at a "
                 "time. Try again in a few minutes.")

    run = store.create(prompt=body.prompt.strip())
    asyncio.create_task(_analyse(run.run_id, run.prompt))
    return {"run_id": run.run_id, "state": run.state.value}


@app.get("/events/{run_id}")
async def events(run_id: str) -> StreamingResponse:
    if store.get(run_id) is None:
        raise HTTPException(404, f"unknown run: {run_id}")

    async def stream():
        async for event in bus.subscribe(run_id):
            yield (f"event: {event['type']}\n"
                   f"data: {json.dumps(event, default=str)}\n\n")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Without this an nginx or Cloud Run proxy buffers the stream and
            # the whole trace arrives at once when the run ends, which looks
            # exactly like the agent doing nothing for four minutes.
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/approve/{run_id}")
async def approve(run_id: str, body: ApproveRequest) -> dict:
    run = store.get(run_id)
    if run is None:
        raise HTTPException(404, f"unknown run: {run_id}")
    if run.state is not RunState.AWAITING_APPROVAL:
        raise HTTPException(
            409, f"run is {run.state.value}, not awaiting approval")
    if body.variant not in {p.get("variant") for p in run.proposals}:
        raise HTTPException(
            400, f"run produced no {body.variant} proposal")

    run.approved_variant = body.variant
    run.transition(RunState.STORYBOARD)
    bus.publish(run_id, make_event(
        "agent_output", run_id=run_id, agent="root",
        message=f"approved {body.variant}; queued storyboard and media"))
    asyncio.create_task(_render_approved_variant(run_id, body.variant))
    return {"run_id": run_id, "state": run.state.value,
            "approved_variant": body.variant}


@app.get("/runs/{run_id}")
async def run_status(run_id: str) -> dict:
    run = store.get(run_id)
    if run is None:
        raise HTTPException(404, f"unknown run: {run_id}")
    return {"run_id": run.run_id, "state": run.state.value,
            "prompt": run.prompt, "proposals": run.proposals,
            "scores": run.scores, "approved_variant": run.approved_variant,
            "scenes": run.scenes, "error": run.error}


__all__ = ["app", "store", "bus"]
