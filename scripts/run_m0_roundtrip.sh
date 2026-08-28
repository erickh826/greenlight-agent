#!/usr/bin/env bash
# Run the M0 ADK <-> MCP <-> Gemini round trip.
#
# The environment constraints this needs are shared with every other agent
# script and live in run_agent.sh. This file stays because the M0 evidence
# entry point is named in docs/M0_SETUP.md and MILESTONES.md.
#
# Usage:
#   ./scripts/run_m0_roundtrip.sh

set -euo pipefail
exec "$(dirname "$0")/run_agent.sh" scripts/m0_adk_roundtrip.py "$@"
