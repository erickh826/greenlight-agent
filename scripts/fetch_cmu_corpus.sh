#!/usr/bin/env bash
# Download and extract the CMU Movie Summary Corpus.
#
# The corpus is CC BY-SA 3.0 (derived from Wikipedia), so it is fetched at ETL
# time and never committed. Plot text is read locally to derive abstract motifs;
# the raw summaries do not enter the repository or ClickHouse.
#
# Source: https://www.cs.cmu.edu/~ark/personas/
#
# Usage:
#   ./scripts/fetch_cmu_corpus.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/data"
URL="https://www.cs.cmu.edu/~ark/personas/data/MovieSummaries.tar.gz"
TARBALL="$DEST/MovieSummaries.tar.gz"

mkdir -p "$DEST"

if [[ -f "$DEST/MovieSummaries/plot_summaries.txt" ]]; then
  echo "Corpus already extracted at $DEST/MovieSummaries/ — nothing to do."
  exit 0
fi

if [[ ! -f "$TARBALL" ]]; then
  echo "Downloading CMU Movie Summary Corpus (~46MB)..."
  curl -sSL --fail --max-time 600 -o "$TARBALL" "$URL"
fi

echo "Extracting..."
tar xzf "$TARBALL" -C "$DEST"

plots=$(wc -l < "$DEST/MovieSummaries/plot_summaries.txt" | tr -d ' ')
meta=$(wc -l < "$DEST/MovieSummaries/movie.metadata.tsv" | tr -d ' ')
echo "Done: $meta metadata rows, $plots plot summaries."
echo
echo "Note: this corpus stops in 2012 — 2013 has 70 films, 2014 has 4, and"
echo "nothing after. See docs/M1_DATA_FINDINGS.md before choosing a year range."
