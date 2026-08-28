"""The one place a ClickHouse MCP connection is constructed.

Agents call build_clickhouse_tools() and nothing else. No agent module should
import StdioServerParameters or name the transport itself -- when this moves to
a remote SSE MCP server after the event, only this file changes.

Two environment traps live here rather than in the callers:

1. mcp-clickhouse pins an older `mcp` than google-adk needs, so the two cannot
   share an interpreter. The server runs as its own `uv run --with
   mcp-clickhouse` subprocess; the client side needs google-adk and `mcp<2`.
   Installing mcp-clickhouse alongside the ADK breaks the import outright.
2. The subprocess gets an explicit env dict, not os.environ. That keeps a
   caller's PYTHONPATH or conda prefix from resolving the server into the wrong
   site-packages -- the failure mode that made `uv run --with` silently
   ineffective during M0.

See docs/M0_SETUP.md §4.3 for the full history.
"""

from __future__ import annotations

import os

from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from mcp import StdioServerParameters

# Startup timeout for the stdio subprocess. ClickHouse Cloud development tier
# cold-starts, and the first connection after an idle period has been measured
# at 32 seconds; the default is not enough.
CONNECT_TIMEOUT_SEC = 120

REQUIRED_ENV = ("CLICKHOUSE_HOST", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD")


def _server_env() -> dict[str, str]:
    """Exactly what mcp-clickhouse needs, and nothing else."""
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            f"missing ClickHouse credentials: {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in."
        )
    return {
        "CLICKHOUSE_HOST": os.environ["CLICKHOUSE_HOST"],
        "CLICKHOUSE_PORT": os.environ.get("CLICKHOUSE_PORT", "8443"),
        "CLICKHOUSE_USER": os.environ["CLICKHOUSE_USER"],
        "CLICKHOUSE_PASSWORD": os.environ["CLICKHOUSE_PASSWORD"],
        "CLICKHOUSE_SECURE": os.environ.get("CLICKHOUSE_SECURE", "true"),
        "CLICKHOUSE_DATABASE": os.environ.get("CLICKHOUSE_DATABASE", "default"),
        # Write access stays off. Read-only is a competition constraint, and the
        # default is only a default -- setting it explicitly means a change to
        # the server's defaults cannot quietly grant writes.
        "CLICKHOUSE_ALLOW_WRITE_ACCESS": "false",
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }


def build_clickhouse_tools() -> MCPToolset:
    """Mount mcp-clickhouse over stdio.

    This is the only runtime path to the database. Exposes list_databases,
    list_tables and run_query (mcp-clickhouse >= 0.3.0 names it run_query, not
    run_select_query).

    The caller owns the returned toolset's lifetime and must await close() on
    it; the subprocess does not exit on its own.
    """
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="uv",
                args=["run", "--with", "mcp-clickhouse", "--python", "3.13",
                      "mcp-clickhouse"],
                env=_server_env(),
            ),
            timeout=CONNECT_TIMEOUT_SEC,
        )
    )


# Queries run before the agent starts. SELECT 1 wakes the service; the two
# aggregates pull each view's parts into page cache. Kept cheap on purpose --
# this is here to absorb a cold start, not to do work.
WARM_UP_QUERIES = (
    ("connectivity", "SELECT 1"),
    ("mv_archetype_performance",
     "SELECT countMerge(sample_count) AS n FROM mv_archetype_performance "
     "GROUP BY archetype LIMIT 1"),
    ("mv_motif_pair_stats",
     "SELECT countMerge(sample_count) AS n FROM mv_motif_pair_stats "
     "GROUP BY motif_a, motif_b LIMIT 1"),
)


async def warm_up(toolset: MCPToolset) -> list[tuple[str, float, bool]]:
    """Run a few light queries so the agent's first call is not the cold one.

    ClickHouse Cloud development tier idles, and the first query after that has
    been measured at 32 seconds during M0. Paying it here costs a second of
    startup instead of appearing as a stall in the demo, and it doubles as a
    pre-flight: if the tools are broken, this fails before any model tokens are
    spent rather than halfway through a run.

    Goes through the MCP run_query tool rather than a direct client, so what
    gets warmed is the path the agent actually uses.

    Returns (label, elapsed_ms, ok) per query. Never raises -- a warm-up that
    breaks the run it was meant to smooth would be worse than no warm-up.
    """
    import time

    tools = {t.name: t for t in await toolset.get_tools()}
    run_query = tools.get("run_query")
    if run_query is None:
        return [("run_query tool missing", 0.0, False)]

    results: list[tuple[str, float, bool]] = []
    for label, sql in WARM_UP_QUERIES:
        started = time.monotonic()
        try:
            # tool_context is part of the ADK tool protocol and is unused by
            # MCPTool for a call carrying no auth; None keeps this callable
            # outside a Runner.
            await run_query.run_async(args={"query": sql}, tool_context=None)
            ok = True
        except Exception:
            ok = False
        results.append((label, (time.monotonic() - started) * 1000, ok))
    return results


__all__ = ["build_clickhouse_tools", "warm_up", "WARM_UP_QUERIES",
           "CONNECT_TIMEOUT_SEC"]
