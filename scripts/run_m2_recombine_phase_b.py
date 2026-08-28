#!/usr/bin/env python3
"""M2 Phase B: turn Phase A evidence into one grounded proposal.

Phase A is where the agent queries ClickHouse. Phase B is deliberately narrower:
tools are off, ADK output_schema is set to TreatmentProposal, and the only
allowed source is the Phase A trace.

Usage:
    ./scripts/run_agent.sh scripts/run_m2_recombine_phase_b.py
    ./scripts/run_agent.sh scripts/run_m2_recombine_phase_b.py --phase-a-trace docs/...
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.recombine import (  # noqa: E402
    build_recombine_phase_b_grounded_agent,
)
from app.contracts import TreatmentProposal  # noqa: E402
from app.env import load_env, redact  # noqa: E402
from app.proposal_validation import (  # noqa: E402
    parse_treatment_proposal,
    validate_grounded_proposal,
)

PHASE_A_TRACE_PATH = ROOT / "docs" / "m2-recombine-phase-a-trace.log"
TRACE_PATH = ROOT / "docs" / "m2-recombine-phase-b-grounded-trace.log"
PROPOSAL_PATH = ROOT / "docs" / "m2-grounded-proposal.json"

PROMPT_TEMPLATE = """Create exactly one grounded TreatmentProposal from the
Phase A transcript below.

Selection policy:
- Prefer a premise supported by both a motif-pair row and an archetype row.
- Copy each evidence sql_query exactly from the transcript.
- Use source_view values that match the table or materialized view in sql_query.
- Do not cite act_structure as evidence unless the transcript queried it.
- Keep logline to one sentence and no more than 200 characters.

PHASE A TRANSCRIPT
==================
{trace}
"""


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


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase-a-trace", default=str(PHASE_A_TRACE_PATH))
    ap.add_argument("--model", help="overrides MODEL_FAST from .env")
    args = ap.parse_args()

    load_env()
    model = args.model or os.environ.get("MODEL_FAST") or "gemini-2.5-flash"
    phase_a_path = Path(args.phase_a_trace)
    phase_a_trace = phase_a_path.read_text(encoding="utf-8")

    agent = build_recombine_phase_b_grounded_agent(model)
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="m2_phase_b", user_id="m2", session_id="phase_b")
    runner = Runner(
        app_name="m2_phase_b",
        agent=agent,
        session_service=session_service,
    )

    trace = Trace(TRACE_PATH)
    calls = 0
    responses = 0
    model_text: list[str] = []
    parse_error: str | None = None
    proposal: TreatmentProposal | None = None
    grounding_errors: list[str] = []

    try:
        trace.write("=== M2 Phase B: grounded proposal, no tools ===")
        trace.write(f"timestamp:       {datetime.now(timezone.utc).isoformat()}")
        trace.write(f"model:           {model}")
        trace.write("tools:           disabled")
        trace.write("response_schema: TreatmentProposal")
        trace.write(f"phase_a_trace:   {phase_a_path}")
        trace.write()

        prompt = PROMPT_TEMPLATE.format(trace=phase_a_trace)
        async for event in runner.run_async(
            user_id="m2",
            session_id="phase_b",
            new_message=types.Content(
                role="user", parts=[types.Part(text=prompt)]),
        ):
            for part in (event.content.parts if event.content else []) or []:
                if part.function_call:
                    calls += 1
                    trace.write(f"--- FunctionCall #{calls}: "
                                f"{part.function_call.name} ---")
                    trace.write(str(dict(part.function_call.args or {})))
                    trace.write()
                elif part.function_response:
                    responses += 1
                    trace.write(f"--- FunctionResponse #{responses} ---")
                    trace.write(str(part.function_response.response))
                    trace.write()
                elif part.text and part.text.strip():
                    model_text.append(part.text.strip())
                    trace.write(f"--- Model text ({event.author}) ---")
                    trace.write(part.text.strip())
                    trace.write()

        raw = "\n".join(model_text)
        if raw.strip():
            try:
                proposal = parse_treatment_proposal(raw)
            except Exception as exc:  # pydantic gives useful detail here.
                parse_error = str(exc)

        if proposal is not None:
            grounding_errors = validate_grounded_proposal(
                proposal, phase_a_trace)
            PROPOSAL_PATH.write_text(
                proposal.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )

        checks = [
            (calls == 0, "No tool calls were made", f"{calls} calls"),
            (responses == 0, "No tool responses were received",
             f"{responses} responses"),
            (bool(raw.strip()), "Model returned text",
             f"{len(raw)} characters"),
            (proposal is not None, "Output parses as TreatmentProposal",
             "parsed" if proposal is not None else parse_error or "no output"),
            (
                proposal is not None and proposal.variant == "grounded",
                "Proposal variant is grounded",
                proposal.variant if proposal is not None else "no proposal",
            ),
            (
                proposal is not None and not grounding_errors,
                "Proposal evidence is grounded in Phase A trace",
                "no proposal" if proposal is None else
                ("ok" if not grounding_errors else "; ".join(grounding_errors)),
            ),
        ]

        trace.write("=== Phase B DoD ===")
        for ok, name, detail in checks:
            trace.write(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

        if proposal is not None:
            trace.write()
            trace.write("--- Validated proposal JSON ---")
            trace.write(proposal.model_dump_json(indent=2))
            trace.write(f"\nproposal_path: {PROPOSAL_PATH}")

        passed = all(ok for ok, _, _ in checks)
        trace.write()
        trace.write("result: PASS -- grounded TreatmentProposal created."
                    if passed else
                    "result: FAIL -- see failed checks above.")
        return 0 if passed else 1
    finally:
        trace.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
