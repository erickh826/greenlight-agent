#!/usr/bin/env bash
# Serve the demo locally, in the same clean interpreter the agents need.
#
# Same two environment constraints as run_agent.sh, and they apply here for the
# same reason -- this process launches the MCP subprocess itself:
#
# 1. mcp-clickhouse pins an older `mcp` than google-adk needs, so it must NOT be
#    installed alongside the ADK client. app/mcp.py runs the server as its own
#    `uv run --with mcp-clickhouse` subprocess.
# 2. uv must not resolve to miniconda's python: unsetting CONDA_PREFIX is not
#    enough, because uv still picks it off PATH and skips building an overlay
#    when it already satisfies every --with. See run_agent.sh.
#
# Usage:
#   ./scripts/serve.sh                 # http://127.0.0.1:8080
#   PORT=9000 ./scripts/serve.sh
#   ./scripts/serve.sh --reload

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PATH="${HOME}/.local/bin:${PATH}"
PORT="${PORT:-8080}"

env -u PYTHONPATH -u PYTHONHOME -u CONDA_PREFIX -u CONDA_DEFAULT_ENV \
  uv run --python-preference only-managed --python 3.13 \
    --with google-adk --with 'mcp<2' \
    --with fastapi --with 'uvicorn[standard]' \
  uvicorn app.main:app --host 0.0.0.0 --port "$PORT" "$@"
