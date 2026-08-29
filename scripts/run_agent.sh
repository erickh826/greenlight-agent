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
#    them stops conda hijacking sys.prefix.
# 3. Unsetting is not enough on its own, which cost an hour on 8/29. uv still
#    picks miniconda's python off PATH for `--python 3.13`, and when that
#    interpreter already satisfies every --with requirement it skips building
#    the overlay and runs there directly. miniconda base has google-adk 2.7.1
#    and mcp 1.15.0; `mcp<2` is satisfied by 1.15.0, so uv reused it, and ADK
#    2.7.1 then failed on `from mcp import SamplingCapability`. The version
#    constraint was met and the combination was still broken.
#    --python-preference only-managed makes uv use its own CPython, so the
#    environment is the one this line describes rather than whatever happens to
#    be installed next to it.
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
  uv run --python-preference only-managed --python 3.13 \
    --with google-adk --with 'mcp<2' \
    --with google-genai --with google-cloud-texttospeech \
    --with google-cloud-storage \
  "$@"
