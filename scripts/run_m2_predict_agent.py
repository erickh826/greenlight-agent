#!/usr/bin/env python3
"""M2: score a proposal against historical analogues, end to end.

Stage one has tools: the agent picks its own comparable sets and queries
ClickHouse through MCP. Stage two has none: it turns that transcript into an
AnalogueEvidenceBundle. Then Python validates every figure against what the
database actually returned and computes the score.

The stages themselves live in app/pipeline.py, because scripts/run_greenlight.py
runs the same two against every variant and two copies of a retry limit is one
copy too many. This script is the single-proposal acceptance harness: it keeps
the DoD block and the trace format.

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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analogue_scoring import partition  # noqa: E402
from app.config import BUDGET_BANDS, MIN_SAMPLE_SIZE, SQL_RETRY_LIMIT  # noqa: E402
from app.contracts import (  # noqa: E402
    AnalogueScoringRequest, PredictionScore, TreatmentProposal)
from app.env import load_env, redact  # noqa: E402
from app.events import Event  # noqa: E402
from app.mcp import build_clickhouse_tools, warm_up  # noqa: E402
from app.pipeline import score_proposal  # noqa: E402
from app.scoring import score_from_evidence  # noqa: E402

PROPOSAL_PATH = ROOT / "docs" / "m2-grounded-proposal.json"
TRACE_PATH = ROOT / "docs" / "m2-predict-agent-trace.log"
SCORE_PATH = ROOT / "docs" / "m2-prediction-score.json"

# The three surfaces the analogue search is supposed to cross. Named here rather
# than inferred from the trace so a run that quietly stopped using one fails.
REQUIRED_SURFACES = ("mv_motif_pair_stats", "mv_archetype_performance", "films")


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
        """Render one pipeline event into the trace."""
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
            self.write(f"    !! ERROR (retry {event.get('retry', 0)} of "
                       f"{SQL_RETRY_LIMIT + 1} attempts): "
                       f"{str(event.get('error'))[:600]}")
        elif kind == "agent_output":
            self.write(f"--- {agent} says ---")
            self.write(event.get("message", ""))
        elif kind == "error":
            self.write(f"!! {agent}: {event.get('message')}")


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

        outcome = await score_proposal(model, toolset, request, trace.emit)
        run = outcome.query_run
        SCORE_PATH.write_text(outcome.score.model_dump_json(indent=2) + "\n",
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
             f"重試次數受控（上限 {run.attempts_allowed} 次連續失敗）",
             f"錯誤 {run.errors} 次、護欄攔下 {len(run.blocked)} 次、"
             f"最後連續失敗 {run.consecutive_failures} 次"),
            (True, "護欄違規在送到 ClickHouse 之前被攔下",
             f"攔下 {len(run.blocked)} 個查詢"
             + (f"：{', '.join(r.rule for _, f in run.blocked for r in f)}"
                if run.blocked else "（本輪沒有違規查詢）")),
            (outcome.converge_calls == 0, "收斂階段沒有 tool call",
             f"{outcome.converge_calls} 次"),
            (outcome.bundle is not None,
             "收斂輸出 parse 成 AnalogueEvidenceBundle",
             "parsed" if outcome.bundle is not None
             else outcome.parse_error or "no output"),
            (outcome.bundle is not None and not outcome.evidence_errors,
             "每個 evidence 的 SQL、count 配對與數值都可回溯本輪結果",
             "ok" if outcome.bundle is not None and not outcome.evidence_errors
             else "; ".join(outcome.evidence_errors) or "no bundle"),
            (bool(used_evidence)
             or published.confidence == "insufficient_evidence",
             f"有通過 {MIN_SAMPLE_SIZE} 樣本門檻的 evidence，否則走 "
             "insufficient_evidence",
             f"{len(used_evidence)} 筆可用，confidence={published.confidence}"),
            (recompute_ok,
             "composite 可由寫出的 JSON 內 evidence 重算得到相同結果",
             f"commercial={published.commercial_score}, "
             f"attention={published.attention_score}, "
             f"composite={published.composite}"),
            (all(e.sql_query.strip() for e in published.evidence)
             if published.evidence
             else published.confidence == "insufficient_evidence",
             "每個數字都附帶產生它的 SQL",
             f"{len(published.evidence)} 筆 evidence"),
        ]

        trace.write("\n=== PredictAgent DoD ===")
        for ok, name, detail in checks:
            trace.write(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

        trace.write()
        trace.write("--- PredictionScore ---")
        trace.write(published.model_dump_json(indent=2))
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
