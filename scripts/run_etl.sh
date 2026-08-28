#!/usr/bin/env bash
# Run an ETL script in a clean interpreter environment.
#
# Same conda/PYTHONPATH trap as run_agent.sh: an active miniconda prefix makes
# uv resolve into its site-packages instead of the ephemeral env it just built,
# so the --with packages never reach sys.path. Unset them -- and pin
# --python-preference only-managed, because unsetting alone still lets uv pick
# miniconda's python off PATH and, if that interpreter already satisfies the
# --with list, skip the overlay entirely. See run_agent.sh for the failure that
# found this.
#
# Usage:
#   ./scripts/run_etl.sh etl/01_wikidata_spine.py --since-year 2015 --limit 50

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <etl-script.py> [args...]" >&2
  exit 2
fi

export PATH="${HOME}/.local/bin:${PATH}"

env -u PYTHONPATH -u PYTHONHOME -u CONDA_PREFIX -u CONDA_DEFAULT_ENV \
  uv run --python-preference only-managed --python 3.13 \
    --with pandas --with pyarrow --with requests \
    --with google-genai --with pydantic \
    --with pytest --with clickhouse-connect \
    "$@"
