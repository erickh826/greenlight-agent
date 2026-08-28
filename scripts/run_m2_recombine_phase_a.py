#!/usr/bin/env python3
"""M2 Phase A: RecombineAgent queries ClickHouse on its own.

Proves the runtime path -- Gemini decides the SQL, mcp-clickhouse runs it,
the result comes back and shapes the next query -- and leaves a trace that can
be read afterwards to see it happen.

Every query the model issues is checked by app/guardrails.py as it goes past,
and the findings are written into the trace beside the query. The prompt asks
the agent to query well; this records whether it did. A violation fails the run,
because the two that matter -- scanning the 4.9M-row attention table, and citing
an interest figure against the ROI sample count -- both produce results that
look perfectly reasonable.

Usage:
    ./scripts/run_agent.sh scripts/run_m2_recombine_phase_a.py
    ./scripts/run_agent.sh scripts/run_m2_recombine_phase_a.py --prompt "..."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.adk.agents import Agent  # noqa: F401  (via the factory)
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.recombine import build_recombine_phase_a_agent  # noqa: E402
from app.config import MIN_SAMPLE_SIZE  # noqa: E402
from app.env import load_env, redact  # noqa: E402
from app.guardrails import (  # noqa: E402
    inspect, is_error_response, unsupported_terms, violations)
from app.mcp import build_clickhouse_tools, warm_up  # noqa: E402

TRACE_PATH = ROOT / "docs" / "m2-recombine-phase-a-trace.log"

DEFAULT_PROMPT = (
    "Find two or three promising narrative recombinations for a mid-budget "
    "original film. Use the database yourself: start broad, cite sample "
    "counts, and do not invent columns or vocabulary terms."
)

# Enough turns for broad -> second surface -> narrow, plus a retry, without
# letting a confused agent loop against a paid API.
MAX_TURNS = 12


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


def extract_sql(args: dict) -> str | None:
    """The query text out of a run_query call, whatever the arg is named."""
    for key in ("query", "sql", "statement"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--model", help="overrides MODEL_FAST from .env")
    args = ap.parse_args()

    load_env()
    model = args.model or os.environ.get("MODEL_FAST") or "gemini-2.5-flash"

    toolset = build_clickhouse_tools()
    agent = build_recombine_phase_a_agent(model, toolset)

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="m2", user_id="m2", session_id="m2")
    runner = Runner(app_name="m2", agent=agent,
                    session_service=session_service)

    trace = Trace(TRACE_PATH)
    calls = 0
    responses = 0
    errors = 0
    queries: list[str] = []
    response_payloads: list[str] = []
    warm_failures: list[str] = []
    all_findings: list[tuple[str, object]] = []
    final_text: list[str] = []

    try:
        trace.write("=== M2 Phase A: RecombineAgent autonomous query ===")
        trace.write(f"timestamp:  {datetime.now(timezone.utc).isoformat()}")
        trace.write(f"model:      {model}")
        trace.write("mcp server: mcp-clickhouse (stdio subprocess, read-only)")
        trace.write(f"clickhouse: {os.environ['CLICKHOUSE_HOST']}:"
                    f"{os.environ.get('CLICKHOUSE_PORT', '8443')} (TLS)")
        trace.write()

        tools = await toolset.get_tools()
        trace.write(f"tools discovered via MCP: {[t.name for t in tools]}")
        trace.write()

        # Absorb the cold start before the model is involved, so a dev-tier
        # wake-up does not show up as the agent stalling mid-demo.
        trace.write("--- warm-up (pre-flight, not part of the agent run) ---")
        for label, ms, ok in await warm_up(toolset):
            trace.write(f"    {'ok  ' if ok else 'FAIL'} {ms:8.1f} ms  {label}")
            if not ok:
                warm_failures.append(label)
        trace.write()

        trace.write(f"USER PROMPT: {args.prompt}")
        trace.write()

        async for event in runner.run_async(
            user_id="m2", session_id="m2",
            new_message=types.Content(
                role="user", parts=[types.Part(text=args.prompt)]),
        ):
            for part in (event.content.parts if event.content else []) or []:
                if part.function_call:
                    calls += 1
                    name = part.function_call.name
                    call_args = dict(part.function_call.args or {})
                    trace.write(f"--- FunctionCall #{calls}: {name} ---")

                    sql = extract_sql(call_args)
                    if sql:
                        queries.append(sql)
                        trace.write(sql.strip())
                        findings = inspect(sql)
                        for f in findings:
                            all_findings.append((f"call #{calls}", f))
                            trace.write(f"    [GUARDRAIL {f.severity}] "
                                        f"{f.rule}: {f.detail}")
                        if not findings:
                            trace.write("    [GUARDRAIL ok]")
                    else:
                        trace.write(json.dumps(call_args, indent=2))
                    trace.write()

                elif part.function_response:
                    responses += 1
                    payload = json.dumps(part.function_response.response,
                                         indent=2, default=str)
                    response_payloads.append(payload)
                    is_error = is_error_response(payload)
                    errors += is_error
                    trace.write(f"--- FunctionResponse #{responses}"
                                f"{' (ERROR)' if is_error else ''} ---")
                    # Long result sets are truncated in the log only; the model
                    # received the whole thing.
                    trace.write(payload if len(payload) <= 4000
                                else payload[:4000] + "\n… [truncated in log]")
                    trace.write()

                elif part.text and part.text.strip():
                    final_text.append(part.text.strip())
                    trace.write(f"--- Model text ({event.author}) ---")
                    trace.write(part.text.strip())
                    trace.write()

            if calls > MAX_TURNS:
                trace.write(f"!! stopping: more than {MAX_TURNS} tool calls")
                break

        # --- DoD ------------------------------------------------------------
        synthesis = "\n".join(final_text)
        mv_queries = [q for q in queries if "mv_" in q.lower()]
        hard = [f for _, f in all_findings if f.severity == "violation"]
        warn = [f for _, f in all_findings if f.severity == "warning"]

        # Every term the agent could legitimately be citing: what it asked for,
        # and what came back.
        evidence_text = "\n".join(queries + response_payloads)
        invented = unsupported_terms(synthesis, evidence_text)

        checks = [
            (not warm_failures, "warm-up 全部成功（冷啟動已在 agent 之前吸收）",
             "3/3 通過" if not warm_failures
             else f"失敗：{', '.join(warm_failures)}"),
            (calls >= 2, "至少 2 次 Gemini 自己決定的 tool call", f"{calls} 次"),
            (responses - errors >= 2, "至少 2 次成功的 ClickHouse 回應",
             f"{responses} 次回應，扣掉 {errors} 次錯誤後 {responses - errors} 次成功"),
            (len(mv_queries) >= 1, "至少一次查詢打到 materialized view",
             f"{len(mv_queries)}/{len(queries)} 個查詢用到 mv_"),
            (bool(synthesis.strip()), "有最終文字綜述",
             f"{len(synthesis)} 字元"),
            ("sample_count" in synthesis or "sample" in synthesis.lower()
             or any("sample_count" in q for q in queries),
             "樣本數出現在查詢或綜述中", "已引用"),
            (not hard, "沒有查詢護欄違規",
             "無" if not hard else
             "; ".join(f"{f.rule}" for f in hard)),
            (not invented, "綜述中的詞彙都有查詢或結果支撐",
             "無憑空出現的詞彙" if not invented else
             f"未被任何查詢或結果支撐：{', '.join(invented)}"),
        ]

        trace.write("=== Phase A DoD ===")
        for ok, name, detail in checks:
            trace.write(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

        trace.write()
        trace.write(f"查詢護欄：{len(queries)} 個查詢，"
                    f"違規 {len(hard)}、警告 {len(warn)}")
        for f in warn:
            trace.write(f"  warning {f.rule}: {f.detail}")

        passed = all(ok for ok, _, _ in checks)
        trace.write()
        trace.write("result: PASS -- Gemini 自主查詢並取得證據。" if passed
                    else "result: FAIL -- 見上方未通過項目。")
        return 0 if passed else 1
    finally:
        trace.close()
        await toolset.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
