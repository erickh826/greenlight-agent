#!/usr/bin/env python3
"""M0 evidence: one full Gemini -> mcp-clickhouse -> Gemini round trip.

Mounts the official mcp-clickhouse MCP server as an ADK MCPToolset, hands it to
Gemini, and lets the model decide its own query. Every FunctionCall and
FunctionResponse is written to the trace log so the runtime MCP path is visible.

Usage:
    uv run --python 3.13 --with google-adk --with mcp-clickhouse \
        scripts/m0_adk_roundtrip.py

Requires .env with ClickHouse credentials and Google auth (either
GOOGLE_API_KEY, or GOOGLE_GENAI_USE_VERTEXAI=true with GOOGLE_CLOUD_PROJECT).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.genai import types
from mcp import StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent
TRACE_PATH = ROOT / "docs" / "m0-mcp-trace.log"

# The connection host is treated as a secret (spec 8.2 scans history for it).
SECRET_ENV = ("CLICKHOUSE_HOST", "CLICKHOUSE_PASSWORD")


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit("ERROR: .env not found. Copy .env.example and fill it in.")
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def redact(text: str) -> str:
    """Strip credentials so the committed trace carries no secrets."""
    for name in SECRET_ENV:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, f"<{name}>")
    return text


class Trace:
    """Writes to stdout and the trace log at once."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("w", encoding="utf-8")

    def write(self, line: str = "") -> None:
        line = redact(line)
        print(line)
        self.handle.write(line + "\n")
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


def build_toolset() -> MCPToolset:
    """Mount mcp-clickhouse over stdio. This is the only runtime DB path."""
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="uv",
                args=["run", "--with", "mcp-clickhouse", "--python", "3.13",
                      "mcp-clickhouse"],
                env={
                    "CLICKHOUSE_HOST": os.environ["CLICKHOUSE_HOST"],
                    "CLICKHOUSE_PORT": os.environ.get("CLICKHOUSE_PORT", "8443"),
                    "CLICKHOUSE_USER": os.environ["CLICKHOUSE_USER"],
                    "CLICKHOUSE_PASSWORD": os.environ["CLICKHOUSE_PASSWORD"],
                    "CLICKHOUSE_SECURE": os.environ.get("CLICKHOUSE_SECURE", "true"),
                    "CLICKHOUSE_DATABASE": os.environ.get("CLICKHOUSE_DATABASE",
                                                          "default"),
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                },
            ),
            timeout=120,
        )
    )


async def main() -> int:
    load_env()
    model = os.environ.get("M0_MODEL") or os.environ.get("MODEL_FAST") \
        or "gemini-2.5-flash"

    toolset = build_toolset()
    agent = Agent(
        name="m0_probe",
        model=model,
        instruction=(
            "You are verifying a ClickHouse connection. You have read-only "
            "ClickHouse tools available. Decide for yourself which query "
            "proves the database is reachable and answering, run it, then "
            "state the result you got back in one sentence."
        ),
        tools=[toolset],
    )

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="m0", user_id="m0", session_id="m0"
    )
    runner = Runner(app_name="m0", agent=agent, session_service=session_service)

    trace = Trace(TRACE_PATH)
    calls = responses = 0
    try:
        trace.write("=== M0 MCP round-trip trace ===")
        trace.write(f"timestamp:  {datetime.now(timezone.utc).isoformat()}")
        trace.write(f"model:      {model}")
        trace.write(f"mcp server: mcp-clickhouse (stdio subprocess)")
        trace.write(f"clickhouse: {os.environ['CLICKHOUSE_HOST']}:"
                    f"{os.environ.get('CLICKHOUSE_PORT', '8443')} (TLS)")
        trace.write()

        tools = await toolset.get_tools()
        trace.write(f"tools discovered via MCP: {[t.name for t in tools]}")
        trace.write()

        prompt = ("Check that the ClickHouse database is reachable and "
                  "responding, then tell me what you found.")
        trace.write(f"USER PROMPT: {prompt}")
        trace.write()

        async for event in runner.run_async(
            user_id="m0",
            session_id="m0",
            new_message=types.Content(role="user",
                                      parts=[types.Part(text=prompt)]),
        ):
            for part in (event.content.parts if event.content else []) or []:
                if part.function_call:
                    calls += 1
                    trace.write(f"--- FunctionCall #{calls} "
                                f"(from {event.author}) ---")
                    trace.write(f"tool: {part.function_call.name}")
                    trace.write("args: " + json.dumps(
                        dict(part.function_call.args or {}), indent=2))
                    trace.write()
                elif part.function_response:
                    responses += 1
                    trace.write(f"--- FunctionResponse #{responses} "
                                f"(tool: {part.function_response.name}) ---")
                    trace.write(json.dumps(
                        part.function_response.response, indent=2,
                        default=str))
                    trace.write()
                elif part.text and part.text.strip():
                    trace.write(f"--- Model text ({event.author}) ---")
                    trace.write(part.text.strip())
                    trace.write()

        trace.write("=== summary ===")
        trace.write(f"function calls:     {calls}")
        trace.write(f"function responses: {responses}")
        ok = calls > 0 and responses > 0
        trace.write("result: PASS -- Gemini chose a query and received a "
                    "ClickHouse response over MCP." if ok else
                    "result: FAIL -- no complete tool round trip occurred.")
        return 0 if ok else 1
    finally:
        trace.close()
        await toolset.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
