#!/usr/bin/env python3
"""M3 Task 1: turn an approved proposal into three checked storyboard beats.

One no-tools model call plus validation. Nothing is generated here -- no Imagen,
no Cloud TTS, no GCS -- and that is the point: the plan is a separate artefact
so it can be checked against the approved proposal before any of that is spent.

Reads the approved proposal from a greenlight run result (or a bare proposal
JSON) and writes docs/m3-storyboard-plan.json plus a trace.

Usage:
    ./scripts/run_agent.sh scripts/run_m3_storyboard.py
    ./scripts/run_agent.sh scripts/run_m3_storyboard.py --variant wildcard
    ./scripts/run_agent.sh scripts/run_m3_storyboard.py \
        --proposal docs/m2-grounded-proposal.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import SCENE_COUNT  # noqa: E402
from app.contracts import GreenlightRunResult, TreatmentProposal  # noqa: E402
from app.env import load_env, redact  # noqa: E402
from app.events import Event  # noqa: E402
from app.media import (  # noqa: E402
    HOUSE_STYLE, compose_image_prompt, estimate_duration_sec,
    lettering_requests, restates_house_style, validate_storyboard_plan)
from app.pipeline import plan_storyboard  # noqa: E402

RUN_PATH = ROOT / "docs" / "m2-greenlight-run.json"
TRACE_PATH = ROOT / "docs" / "m3-storyboard-trace.log"
PLAN_PATH = ROOT / "docs" / "m3-storyboard-plan.json"


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

    def emit(self, event: Event) -> None:
        kind = event["type"]
        if kind == "agent_start":
            self.write(f"\n=== {event.get('agent')} ===")
        elif kind == "agent_retry":
            self.write(f"    ~~ retry {event.get('retry')}: "
                       f"{event.get('message')}")
        elif kind == "stage_failed":
            self.write(f"!! {event.get('agent')}: {event.get('message')}")
        elif kind == "agent_output":
            self.write(f"--- {event.get('agent')} output "
                       f"({len(event.get('message', ''))} chars) ---")


def load_proposal(path: Path, variant: str) -> TreatmentProposal:
    """Accepts a greenlight run result or a bare TreatmentProposal."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "outcomes" not in raw:
        return TreatmentProposal.model_validate(raw)

    result = GreenlightRunResult.model_validate(raw)
    for outcome in result.outcomes:
        if outcome.variant == variant and outcome.proposal is not None:
            return outcome.proposal
    raise SystemExit(f"{path} has no {variant} proposal")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--proposal", default=str(RUN_PATH))
    ap.add_argument("--variant", default="grounded",
                    choices=["grounded", "wildcard"])
    ap.add_argument("--model", help="overrides MODEL_FAST from .env")
    args = ap.parse_args()

    load_env()
    model = args.model or os.environ.get("MODEL_FAST") or "gemini-2.5-flash"
    proposal = load_proposal(Path(args.proposal), args.variant)

    trace = Trace(TRACE_PATH)
    try:
        trace.write("=== M3 Task 1: storyboard planning ===")
        trace.write(f"timestamp: {datetime.now(timezone.utc).isoformat()}")
        trace.write(f"model:     {model}")
        trace.write(f"proposal:  {proposal.title} ({proposal.variant})")
        trace.write(f"source:    {args.proposal}")
        trace.write("tools:     disabled")
        trace.write("media:     none generated -- this is the plan only")

        plan, errors, run = await plan_storyboard(model, proposal, trace.emit)

        if plan is not None:
            PLAN_PATH.write_text(plan.model_dump_json(indent=2) + "\n",
                                 encoding="utf-8")
            trace.write("\n--- StoryboardPlan ---")
            trace.write(plan.model_dump_json(indent=2))
            trace.write("\n--- Composed Imagen prompts (not sent) ---")
            for scene in plan.scenes:
                trace.write(f"[{scene.scene_index}] "
                            f"{compose_image_prompt(plan, scene)}")
                trace.write(f"    narration ~{estimate_duration_sec(scene.narration):.1f}s: "
                            f"{scene.narration}")

        # The model's own prompt, not the composed one: the house style is a
        # list of negations ("No text, no captions...") and checking the
        # composed string flags our own instruction. It did, on the first run,
        # for all three scenes.
        lettering = [] if plan is None else [
            (s.scene_index, lettering_requests(s.image_prompt))
            for s in plan.scenes]
        lettering = [(i, hits) for i, hits in lettering if hits]

        checks = [
            (run.calls == 0, "規劃階段沒有 tool call", f"{run.calls} 次"),
            (plan is not None, "輸出 parse 成 StoryboardPlan",
             "parsed" if plan else "; ".join(errors) or "no output"),
            (plan is not None and not errors,
             "plan 通過對已核准 proposal 的驗證",
             "ok" if plan is not None and not errors
             else "; ".join(errors) or "no plan"),
            (plan is not None and len(plan.scenes) == SCENE_COUNT,
             f"正好 {SCENE_COUNT} 個場景",
             f"{len(plan.scenes) if plan else 0} 個"),
            (plan is not None and plan.proposal_title == proposal.title
             and plan.variant == proposal.variant,
             "title 與 variant 沒有被改動",
             f"{plan.proposal_title!r} / {plan.variant}" if plan else "no plan"),
            (not lettering, "沒有任何 image_prompt 要求畫面內文字",
             "無" if not lettering else str(lettering)),
            (plan is not None and all(
                s.description.strip() and s.image_prompt.strip()
                and s.narration.strip() for s in plan.scenes),
             "每個場景都有 description / image_prompt / narration",
             "齊全" if plan else "no plan"),
            (plan is not None and all(
                compose_image_prompt(plan, s).endswith(HOUSE_STYLE)
                for s in plan.scenes),
             "三個 prompt 共用同一個 house style",
             "一致" if plan else "no plan"),
            (plan is not None and not restates_house_style(plan.style),
             "style 是這部片自己的樣子，不是把 house style 再講一次",
             plan.style[:90] if plan else "no plan"),
        ]

        trace.write("\n=== Storyboard DoD ===")
        for ok, name, detail in checks:
            trace.write(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

        # Belt and braces: the validator is what the pipeline trusts, so run it
        # once more against the plan as written to disk.
        if plan is not None:
            trace.write(f"\nre-validated from file: "
                        f"{validate_storyboard_plan(plan, proposal) or 'ok'}")
            trace.write(f"plan_path: {PLAN_PATH}")

        passed = all(ok for ok, _, _ in checks)
        trace.write()
        trace.write("result: PASS -- 三個場景通過驗證，尚未產生任何媒體。"
                    if passed else "result: FAIL -- 見上方未通過項目。")
        return 0 if passed else 1
    finally:
        trace.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
