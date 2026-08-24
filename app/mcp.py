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


__all__ = ["build_clickhouse_tools", "CONNECT_TIMEOUT_SEC"]
