#!/usr/bin/env bash
# Run an ADK agent script in a clean interpreter env.
#
# Generalises run_m0_roundtrip.sh, which hardcoded its one script. The two
# environment constraints are the reason this wrapper exists at all, and they
# are the same for every agent:
#
# 1. mcp-clickhouse pins an older `mcp` than google-adk needs, so it must NOT
#    be installed alongside the ADK client. app/mcp.py launches the MCP server
#    as its own `uv run --with mcp-clickhouse` subprocess instead. Adding
#    --with mcp-clickhouse here breaks the ADK import outright.
# 2. An active conda env plus PYTHONPATH makes uv resolve into miniconda's
#    site-packages, where a stale `mcp` shadows the one uv installs. Unsetting
#    them forces a real ephemeral env.
#
# Usage:
#   ./scripts/run_agent.sh scripts/run_m2_recombine_phase_a.py [args...]

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <agent-script.py> [args...]" >&2
  exit 2
fi

export PATH="${HOME}/.local/bin:${PATH}"

env -u PYTHONPATH -u PYTHONHOME -u CONDA_PREFIX -u CONDA_DEFAULT_ENV \
  uv run --python 3.13 --with google-adk --with 'mcp<2' \
  "$@"
