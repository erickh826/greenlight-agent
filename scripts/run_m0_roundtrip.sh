#!/usr/bin/env bash
# Run the M0 ADK <-> MCP <-> Gemini round trip in a clean interpreter env.
#
# Two things this wrapper exists for:
#
# 1. mcp-clickhouse pins an older `mcp` than google-adk needs, so it must NOT
#    be installed alongside the ADK client. The MCP server is launched as its
#    own `uv run --with mcp-clickhouse` subprocess by the script instead.
# 2. An active conda env plus PYTHONPATH makes uv resolve into miniconda's
#    site-packages, where a stale `mcp` shadows the one uv installs. Unsetting
#    them forces a real ephemeral env.
#
# Usage:
#   ./scripts/run_m0_roundtrip.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PATH="${HOME}/.local/bin:${PATH}"

env -u PYTHONPATH -u PYTHONHOME -u CONDA_PREFIX -u CONDA_DEFAULT_ENV \
  uv run --python 3.13 --with google-adk --with 'mcp<2' \
  scripts/m0_adk_roundtrip.py
