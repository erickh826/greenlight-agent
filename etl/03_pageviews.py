#!/usr/bin/env python3
"""Fetch Wikipedia pageviews and derive the interest columns.

What these numbers are, and are not
-----------------------------------
The Wikimedia pageviews API begins 2015-07-01. Every film in this dataset was
released between 1990 and 2014, so the measurement window opens between 1 and 25
years after release (median 12). There is no opening peak in this data and
nothing decays from a premiere.

So the derived columns describe how much a film was still looked up, years
later, and are named accordingly. The previous plan's `pageview_peak` and
`pageview_decay_days` are gone -- see docs/M1_DATA_FINDINGS.md §1.

The measurement lag also makes raw counts incomparable across release years: a
2014 film was measured one year out and still carries some release afterglow, a
1990 film twenty-five years out carries none. `interest_cohort_pct` exists to
fix that -- it is a percentile within a five-year release cohort, and it is the
only one of these columns safe to compare across the dataset.

Usage:
    ./scripts/run_etl.sh etl/03_pageviews.py --limit 20      # smoke test
    ./scripts/run_etl.sh etl/03_pageviews.py                 # full run

Outputs:
    data/attention.parquet        one row per film per day
    data/films_enriched.parquet   the spine plus the interest columns
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "etl"))
from vocab import MEASUREMENT_START_YEAR, release_bucket, years_to_measurement  # noqa: E402

ENDPOINT = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
            "en.wikipedia/all-access/user/{title}/daily/{start}/{end}")

USER_AGENT = ("greenlight-agent/0.1 "
              "(https://github.com/erickh826/greenlight-agent) python-requests")

# The API's first day of data. Not a choice.
WINDOW_START = "20150701"

# Wikimedia asks for courtesy limits rather than publishing a hard one; 5/s is
# the ceiling SYSTEM_SPEC §4.3 commits to.
MAX_REQ_PER_SEC = 5.0


def fetch_one(session: requests.Session, enwiki_title: str, end: str,
              retries: int = 3) -> list[tuple[str, int]] | None:
    """One article, whole window, one call.

    Returns None for a 404 -- an article the API has no data for is a normal
    outcome (renamed or redirected page), not a failure worth stopping over.
    """
    # The API wants underscores and a path-encoded title; a slash in a film
    # title becomes a path segment otherwise.
    title = quote(enwiki_title.replace(" ", "_"), safe="")
    url = ENDPOINT.format(title=title, start=WINDOW_START, end=end)

    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                time.sleep(wait)
                continue
            r.raise_for_status()
            return [(i["timestamp"][:8], i["views"]) for i in r.json()["items"]]
        except requests.RequestException:
            if attempt == retries:
                return None
            time.sleep(2 ** attempt)
    return None


def derive(views: np.ndarray) -> dict[str, float]:
    """Summarise one film's daily series.

    Deliberately robust statistics: a single news event (an actor dying, a
    remake announcement) can put a spike orders of magnitude above the baseline,
    and a mean would follow it.
    """
    return {
        "interest_median_daily": float(np.median(views)),
        "interest_p95_daily": float(np.percentile(views, 95)),
        # Slope of a least-squares line over the series, normalised by the
        # median so films of very different popularity are on one scale.
        # Positive means rising interest across the window.
        "interest_trend_slope": _trend(views),
    }


def _trend(views: np.ndarray) -> float:
    if len(views) < 30:
        return 0.0
    x = np.arange(len(views), dtype=float)
    slope = np.polyfit(x, views.astype(float), 1)[0]
    baseline = float(np.median(views))
    if baseline <= 0:
        return 0.0
    # Per-year change as a fraction of the typical day.
    return float(slope * 365.0 / baseline)


def add_cohort_percentiles(films: pd.DataFrame) -> pd.DataFrame:
    """Rank each film against same-era peers.

    Done after every film is fetched because a percentile needs the whole
    cohort's distribution. Ranking on interest_median_daily, the robust
    baseline, rather than the spike height.
    """
    films["interest_cohort_pct"] = (
        films.groupby("release_bucket")["interest_median_daily"]
             .rank(pct=True, method="average")
    )
    return films


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--films", type=Path,
                    default=ROOT / "data" / "films_with_plots.parquet")
    ap.add_argument("--limit", type=int,
                    help="only fetch the first N films (smoke test)")
    ap.add_argument("--out-attention", type=Path,
                    default=ROOT / "data" / "attention.parquet")
    ap.add_argument("--out-films", type=Path,
                    default=ROOT / "data" / "films_enriched.parquet")
    args = ap.parse_args()

    if not args.films.exists():
        sys.exit(f"ERROR: {args.films} not found. Run 02_cmu_join.py first.")

    films = pd.read_parquet(args.films)
    if args.limit:
        films = films.head(args.limit).copy()

    end = datetime.now(timezone.utc).strftime("%Y%m%d")
    print(f"pageviews: {len(films)} films, window {WINDOW_START}-{end}")
    print(f"measurement starts {MEASUREMENT_START_YEAR}; every film predates it")
    print()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    rows: list[dict] = []
    derived: dict[str, dict[str, float]] = {}
    missing: list[str] = []
    min_interval = 1.0 / MAX_REQ_PER_SEC

    for i, film in enumerate(films.itertuples(index=False), 1):
        started = time.monotonic()
        series = fetch_one(session, film.enwiki_title, end)

        if series:
            for day, views in series:
                rows.append({"film_id": film.film_id,
                             "date": date(int(day[:4]), int(day[4:6]),
                                          int(day[6:8])),
                             "views": views})
            derived[film.film_id] = derive(
                np.array([v for _, v in series], dtype=np.int64))
        else:
            missing.append(film.enwiki_title)

        if i % 50 == 0 or i == len(films):
            print(f"  {i}/{len(films)}  rows={len(rows):,}  "
                  f"missing={len(missing)}")

        elapsed = time.monotonic() - started
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    if not rows:
        print("\nNo pageview data at all -- check enwiki_title values.",
              file=sys.stderr)
        return 1

    attention = pd.DataFrame(rows)
    args.out_attention.parent.mkdir(parents=True, exist_ok=True)
    attention.to_parquet(args.out_attention, index=False)

    # Attach the derived columns, then the cohort percentile that needs them all.
    films["release_bucket"] = films["release_year"].map(release_bucket)
    films["years_to_measurement"] = films["release_year"].map(years_to_measurement)
    films["attention_kind"] = "sustained_interest"
    for col in ("interest_median_daily", "interest_p95_daily",
                "interest_trend_slope"):
        films[col] = films["film_id"].map(
            lambda fid, c=col: derived.get(fid, {}).get(c))
    films = add_cohort_percentiles(films)
    films.to_parquet(args.out_films, index=False)

    have = films["interest_median_daily"].notna().sum()
    print()
    print("=== attention ===")
    print(f"rows:                     {len(attention):,}")
    print(f"films with data:          {have}/{len(films)} "
          f"({have / len(films):.0%})")
    print(f"articles with no data:    {len(missing)}"
          + (f" (e.g. {missing[0]!r})" if missing else ""))
    print()
    print(f"median daily views:       "
          f"{films['interest_median_daily'].median():.0f}")
    print(f"measurement lag (years):  "
          f"{films['years_to_measurement'].min()}-"
          f"{films['years_to_measurement'].max()}")
    print()
    print("cohort percentile spread (should be ~uniform within each bucket):")
    for bucket, grp in films.groupby("release_bucket"):
        pct = grp["interest_cohort_pct"].dropna()
        if len(pct):
            print(f"  {bucket}  n={len(pct):>4}  "
                  f"p25={pct.quantile(.25):.2f} "
                  f"p50={pct.median():.2f} p75={pct.quantile(.75):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
