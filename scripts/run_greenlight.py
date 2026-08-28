#!/usr/bin/env python3
"""End-to-end CLI: one prompt in, two scored proposals out.

Runs the whole M2 path in a single invocation -- RecombineAgent queries
ClickHouse, both variants converge to a TreatmentProposal against the same
transcript, and PredictAgent scores each against historical analogues -- and
leaves three artefacts:

    docs/m2-greenlight-run.json     the result document (proposals + scores)
    docs/m2-greenlight-events.jsonl the structured event stream, one JSON per
                                    line, the same events the SSE endpoint will
                                    forward
    docs/m2-greenlight-trace.log    the human-readable trace, SQL verbatim

The events go through app/events.InProcessEventBus rather than straight to the
file, so this exercises the transport the HTTP layer will subscribe to instead
of a parallel path that happens to work here.

Usage:
    ./scripts/run_agent.sh scripts/run_greenlight.py
    ./scripts/run_agent.sh scripts/run_greenlight.py --variants grounded
    ./scripts/run_agent.sh scripts/run_greenlight.py --prompt "..."
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

from app.analogue_scoring import partition  # noqa: E402
from app.config import BUDGET_BANDS, MIN_SAMPLE_SIZE, PROPOSAL_VARIANTS  # noqa: E402
from app.contracts import GreenlightRunResult, PredictionScore  # noqa: E402
from app.env import load_env, redact  # noqa: E402
from app.events import Event, InProcessEventBus  # noqa: E402
from app.mcp import build_clickhouse_tools, warm_up  # noqa: E402
from app.pipeline import (  # noqa: E402
    DEFAULT_PROMPT, RECOMBINE_REQUIRED_SURFACES, recombine_surfaces_seen,
    run_greenlight)
from app.scoring import score_from_evidence  # noqa: E402
from app.state import RunState, RunStore  # noqa: E402

RESULT_PATH = ROOT / "docs" / "m2-greenlight-run.json"
EVENTS_PATH = ROOT / "docs" / "m2-greenlight-events.jsonl"
TRACE_PATH = ROOT / "docs" / "m2-greenlight-trace.log"

# Event types a complete run must produce. Checked rather than assumed: an SSE
# client that never receives tool_call has no way to show the query, which is
# the one thing this project asks a viewer to look at.
REQUIRED_EVENTS = ("agent_start", "tool_call", "tool_result", "agent_output",
                   "done")


class Recorder:
    """Fans one event out to the bus, the JSONL log and the human trace."""

    def __init__(self, run_id: str, bus: InProcessEventBus):
        self.run_id = run_id
        self.bus = bus
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.events: list[Event] = []
        self.jsonl = EVENTS_PATH.open("w", encoding="utf-8")
        self.trace = TRACE_PATH.open("w", encoding="utf-8")

    def write(self, line: str = "") -> None:
        line = redact(str(line))
        print(line)
        self.trace.write(line + "\n")
        self.trace.flush()

    def emit(self, event: Event) -> None:
        self.bus.publish(self.run_id, event)
        self.events.append(event)
        self.jsonl.write(redact(json.dumps(event, default=str)) + "\n")
        self.jsonl.flush()
        self._render(event)

    def _render(self, event: Event) -> None:
        kind = event["type"]
        agent = event.get("agent", "?")
        if kind == "agent_start":
            self.write(f"\n=== {agent} ===")
        elif kind == "tool_call":
            sql = (event.get("args") or {}).get("query")
            self.write(f"--- {agent} → {event.get('tool')} ---")
            self.write(sql.strip() if sql
                       else json.dumps(event.get("args"), indent=2))
        elif kind == "tool_result":
            cols = (event.get("payload") or {}).get("columns") or []
            self.write(f"    {event.get('rows', 0)} rows in "
                       f"{event.get('elapsed_ms', 0):.0f} ms"
                       + (f"  [{', '.join(cols)}]" if cols else ""))
            for row in event.get("preview") or []:
                self.write("      " + " | ".join(row))
        elif kind == "tool_error":
            self.write(f"    !! ERROR (retry {event.get('retry', 0)}): "
                       f"{str(event.get('error'))[:400]}")
        elif kind == "agent_output":
            self.write(f"--- {agent} says ---")
            self.write(event.get("message", ""))
        elif kind == "error":
            self.write(f"!! {agent}: {event.get('message')}")
        elif kind == "done":
            self.write("\n=== run complete ===")

    def close(self) -> None:
        self.jsonl.close()
        self.trace.close()


def recomputes(score: PredictionScore) -> bool:
    """Whether the score follows from the evidence printed beside it."""
    roi, interest = partition(score.evidence)
    return score_from_evidence(roi, interest)[:3] == (
        score.commercial_score, score.attention_score, score.composite)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--variants", nargs="+", default=list(PROPOSAL_VARIANTS),
                    choices=list(PROPOSAL_VARIANTS))
    ap.add_argument("--budget-band", choices=[*BUDGET_BANDS, "none"],
                    default="mid")
    ap.add_argument("--release-bucket", default=None)
    ap.add_argument("--model", help="overrides MODEL_FAST from .env")
    args = ap.parse_args()

    load_env()
    model = args.model or os.environ.get("MODEL_FAST") or "gemini-2.5-flash"

    store = RunStore()
    run = store.create(prompt=args.prompt)
    bus = InProcessEventBus()
    rec = Recorder(run.run_id, bus)
    toolset = build_clickhouse_tools()
    warm_failures: list[str] = []

    try:
        rec.write("=== Greenlight: end-to-end run ===")
        rec.write(f"run_id:         {run.run_id}")
        rec.write(f"timestamp:      {datetime.now(timezone.utc).isoformat()}")
        rec.write(f"model:          {model}")
        rec.write(f"variants:       {', '.join(args.variants)}")
        rec.write(f"budget band:    {args.budget_band}")
        rec.write(f"release bucket: {args.release_bucket or 'none (all eras)'}")
        rec.write(f"clickhouse:     {os.environ['CLICKHOUSE_HOST']}:"
                  f"{os.environ.get('CLICKHOUSE_PORT', '8443')} (TLS)")
        rec.write(f"prompt:         {args.prompt}")

        rec.write("\n--- warm-up (pre-flight) ---")
        for label, ms, ok in await warm_up(toolset):
            rec.write(f"    {'ok  ' if ok else 'FAIL'} {ms:8.1f} ms  {label}")
            if not ok:
                warm_failures.append(label)

        result, phase_a, scorings = await run_greenlight(
            model, toolset, rec.emit,
            prompt=args.prompt,
            variants=args.variants,
            budget_band=None if args.budget_band == "none" else args.budget_band,
            release_bucket=args.release_bucket,
            run_id=run.run_id,
        )

        RESULT_PATH.write_text(result.model_dump_json(indent=2) + "\n",
                               encoding="utf-8")

        # Scoring is done and the gate is next: the user picks a variant. The
        # agents do not await that click -- see app/state.py.
        run.proposals = [o.proposal.model_dump(mode="json")
                         for o in result.outcomes if o.proposal]
        run.scores = [o.score.model_dump(mode="json")
                      for o in result.outcomes if o.score]
        if all(o.score is not None for o in result.outcomes):
            run.transition(RunState.AWAITING_APPROVAL)
        else:
            run.fail("at least one variant produced no score")

        # --- DoD ------------------------------------------------------------
        published = GreenlightRunResult.model_validate_json(
            RESULT_PATH.read_text(encoding="utf-8"))
        scored = [o for o in published.outcomes if o.score]
        seen_types = {e["type"] for e in rec.events}
        phase_a_surfaces = sorted(recombine_surfaces_seen(phase_a))
        missing_phase_a_surfaces = [
            surface for surface in RECOMBINE_REQUIRED_SURFACES
            if surface not in phase_a_surfaces]
        tool_calls = [e for e in rec.events if e["type"] == "tool_call"]
        with_sql = [e for e in tool_calls
                    if (e.get("args") or {}).get("query", "").strip()]
        usable = {o.variant: [ev for ev in o.score.evidence
                              if ev.sample_count >= MIN_SAMPLE_SIZE]
                  for o in scored}

        checks = [
            (not warm_failures, "warm-up 全部成功",
             "3/3 通過" if not warm_failures
             else f"失敗：{', '.join(warm_failures)}"),
            (phase_a.calls >= 2, "Phase A 自主查詢 ClickHouse",
             f"{phase_a.calls} 次 tool call"),
            (not missing_phase_a_surfaces,
             "Phase A handoff 包含 grounded proposal 必需的 evidence surface",
             f"已查到 {phase_a_surfaces}"
             if not missing_phase_a_surfaces
             else f"缺少 {missing_phase_a_surfaces}，已查到 {phase_a_surfaces}"),
            (len(published.outcomes) == len(args.variants),
             f"要求的 {len(args.variants)} 個方案都有結果",
             f"{[o.variant for o in published.outcomes]}"),
            (all(o.proposal is not None for o in published.outcomes),
             "每個方案都產出 TreatmentProposal",
             "; ".join(f"{o.variant}: "
                       + ("ok" if o.proposal and not o.validation_errors
                          else "; ".join(o.validation_errors) or "no proposal")
                       for o in published.outcomes)),
            (all(not o.validation_errors for o in published.outcomes),
             "沒有 grounding / wildcard 驗證錯誤",
             "無" if all(not o.validation_errors for o in published.outcomes)
             else "; ".join(e for o in published.outcomes
                            for e in o.validation_errors)),
            (len(scored) == len(published.outcomes),
             "每個方案都有 PredictionScore",
             f"{len(scored)}/{len(published.outcomes)}"),
            (all(recomputes(o.score) for o in scored),
             "每個 composite 都可由寫出的 JSON 內 evidence 重算",
             "; ".join(f"{o.variant}={o.score.composite}" for o in scored)),
            (all(usable[o.variant]
                 or o.score.confidence == "insufficient_evidence"
                 for o in scored),
             f"evidence 過 {MIN_SAMPLE_SIZE} 樣本門檻，否則走 insufficient_evidence",
             "; ".join(f"{o.variant}: {len(usable[o.variant])} 筆 "
                       f"({o.score.confidence})" for o in scored)),
            (bool(tool_calls) and len(with_sql) == len(tool_calls),
             "每個 tool_call 事件都帶 SQL 原文",
             f"{len(with_sql)}/{len(tool_calls)}"),
            (set(REQUIRED_EVENTS) <= seen_types,
             "結構化事件流包含 SSE 需要的所有型別",
             f"{sorted(seen_types)}"),
            (published.guardrail_blocks == 0
             or all(o.score for o in published.outcomes),
             "護欄攔截沒有讓整條流程失敗",
             f"攔下 {published.guardrail_blocks} 個查詢、"
             f"SQL 錯誤 {published.sql_errors} 次"),
            (run.state is RunState.AWAITING_APPROVAL,
             "run 狀態機停在核准閘門（agent 不等待點擊）",
             run.state.value),
        ]

        rec.write("\n=== Root orchestration DoD ===")
        for ok, name, detail in checks:
            rec.write(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

        rec.write()
        rec.write(f"tool calls: {published.tool_calls}  "
                  f"sql errors: {published.sql_errors}  "
                  f"guardrail blocks: {published.guardrail_blocks}  "
                  f"elapsed: {published.elapsed_sec:.1f}s")
        for o in published.outcomes:
            if o.proposal and o.score:
                rec.write(f"  {o.variant:<9} {o.proposal.title!r} "
                          f"composite={o.score.composite} "
                          f"({o.score.confidence}, "
                          f"{len(o.score.evidence)} evidence)")
        rec.write(f"\nresult:  {RESULT_PATH}")
        rec.write(f"events:  {EVENTS_PATH}  ({len(rec.events)} events)")

        passed = all(ok for ok, _, _ in checks)
        rec.write()
        rec.write("result: PASS -- 一次執行輸出雙方案 JSON 與完整 tool call trace。"
                  if passed else "result: FAIL -- 見上方未通過項目。")
        return 0 if passed else 1
    finally:
        bus.close(run.run_id)
        rec.close()
        await toolset.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
