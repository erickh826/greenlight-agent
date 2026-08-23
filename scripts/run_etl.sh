#!/usr/bin/env bash
# Run an ETL script in a clean interpreter environment.
#
# Same conda/PYTHONPATH trap as run_m0_roundtrip.sh: an active miniconda prefix
# makes uv resolve into its site-packages instead of the ephemeral env it just
# built, so the --with packages never reach sys.path. Unset them.
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
  uv run --python 3.13 \
    --with pandas --with pyarrow --with requests \
    "$@"
