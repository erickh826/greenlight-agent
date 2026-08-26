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

`interest_cohort_pct` is a percentile within a five-year release cohort. It was
added on the expectation that the measurement lag would make raw counts
incomparable across release years. Over all 1,238 films that turned out to be
false -- lag against the raw daily median is r = -0.009, because how popular a
film is swamps how long ago it came out. The column stays because a bounded 0-1
standing is still what attention_score needs, but it is a scale normalisation,
not a lag correction, and this docstring used to claim otherwise. See
docs/M1_DATA_FINDINGS.md §1.

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


class TransientFailure(Exception):
    """Retries exhausted on an article that may well have data.

    Distinct from a 404 on purpose. The first version returned None for both,
    so a network blip and a genuinely absent article were recorded identically
    -- and the full run silently dropped `Inception`, which has 4,074 days of
    data, into the same bucket as articles that have none. A film missing from
    the dataset for no reason is exactly the kind of thing nobody notices.
    """


def fetch_one(session: requests.Session, enwiki_title: str, end: str,
              retries: int = 3) -> list[tuple[str, int]] | None:
    """One article, whole window, one call.

    Returns None only for a 404 -- an article the API has no data for is a
    normal outcome (renamed or redirected page). Raises TransientFailure when
    retries run out, so the caller can try again rather than record a gap.
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
        except requests.RequestException as exc:
            if attempt == retries:
                raise TransientFailure(f"{enwiki_title}: {exc}") from exc
            time.sleep(2 ** attempt)
    raise TransientFailure(f"{enwiki_title}: retries exhausted")


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


def fetch_all(session: requests.Session, films: pd.DataFrame, end: str,
              rows: list[dict], derived: dict[str, dict[str, float]],
              retries: int = 3) -> tuple[list[str], list[str]]:
    """Fetch every film, appending into `rows` and `derived` as it goes.

    Returns (articles the API has no data for, articles that failed
    transiently). The two lists are kept apart because only the second is worth
    retrying, and only the first is a real property of the dataset.
    """
    no_data: list[str] = []
    transient: list[str] = []
    min_interval = 1.0 / MAX_REQ_PER_SEC

    for i, film in enumerate(films.itertuples(index=False), 1):
        started = time.monotonic()
        try:
            series = fetch_one(session, film.enwiki_title, end, retries=retries)
        except TransientFailure:
            transient.append(film.enwiki_title)
            series = None
        else:
            if series is None:
                no_data.append(film.enwiki_title)

        if series:
            for day, views in series:
                rows.append({"film_id": film.film_id,
                             "date": date(int(day[:4]), int(day[4:6]),
                                          int(day[6:8])),
                             "views": views})
            derived[film.film_id] = derive(
                np.array([v for _, v in series], dtype=np.int64))

        if i % 50 == 0 or i == len(films):
            print(f"  {i}/{len(films)}  rows={len(rows):,}  "
                  f"no_data={len(no_data)}  transient={len(transient)}")

        elapsed = time.monotonic() - started
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    return no_data, transient


def add_cohort_percentiles(films: pd.DataFrame) -> pd.DataFrame:
    """Rank each film against same-era peers.

    Done after every film is fetched because a percentile needs the whole
    cohort's distribution. Ranking on interest_median_daily, the robust
    baseline, rather than the spike height.

    Grouping by cohort rather than ranking globally is now a weaker choice than
    it looked: with no lag effect to remove, it ranks each film against 162-317
    peers instead of 1,238 for no measured benefit. Kept for now because
    release_bucket is the grouping key throughout sql/003 and the two agreeing
    is worth more than the marginal precision.
    """
    films["interest_cohort_pct"] = (
        films.groupby("release_bucket")["interest_median_daily"]
             .rank(pct=True, method="average")
    )
    return films


def repair(films: pd.DataFrame, end: str, args) -> int:
    """Fetch only the films an earlier run left without data, and merge.

    A full pass is forty-five minutes of rate-limited requests; recovering two
    films should not cost that, or it will not get done.
    """
    for path in (args.out_attention, args.out_films):
        if not path.exists():
            sys.exit(f"ERROR: --repair needs {path.name}; run without it first.")

    attention = pd.read_parquet(args.out_attention)
    enriched = pd.read_parquet(args.out_films)

    gaps = enriched[enriched["interest_median_daily"].isna()]
    print(f"repair: {len(gaps)} 部沒有資料\n")
    if gaps.empty:
        print("沒有要補的。")
        return 0

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    rows: list[dict] = []
    derived: dict[str, dict[str, float]] = {}
    no_data, transient = fetch_all(session, gaps, end, rows, derived, retries=6)

    if not rows:
        print(f"\n一部都補不回來。no_data={no_data} transient={transient}")
        return 1

    attention = pd.concat([attention, pd.DataFrame(rows)], ignore_index=True)
    attention.to_parquet(args.out_attention, index=False)

    enriched = enriched.set_index("film_id")
    for film_id, values in derived.items():
        for col, value in values.items():
            enriched.loc[film_id, col] = value
    enriched = enriched.reset_index()

    # The percentile is relative, so adding films shifts everyone in the cohort.
    # Recompute across the whole table rather than assigning the new rows a rank
    # against a distribution they were not part of.
    enriched = add_cohort_percentiles(enriched)
    enriched.to_parquet(args.out_films, index=False)

    have = enriched["interest_median_daily"].notna().sum()
    print(f"\n補回 {len(derived)} 部，新增 {len(rows):,} 列")
    print(f"attention 總列數: {len(attention):,}")
    print(f"有資料的影片:     {have}/{len(enriched)} "
          f"({have / len(enriched):.1%})")
    if no_data:
        print(f"確實沒有資料:     {', '.join(no_data)}")
    if transient:
        print(f"仍然失敗:         {', '.join(transient)}")
    return 0


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
    ap.add_argument("--repair", action="store_true",
                    help="fetch only the films missing from an existing run "
                         "and merge them in, instead of refetching all 1,238")
    args = ap.parse_args()

    if not args.films.exists():
        sys.exit(f"ERROR: {args.films} not found. Run 02_cmu_join.py first.")

    films = pd.read_parquet(args.films)
    if args.limit:
        films = films.head(args.limit).copy()

    end = datetime.now(timezone.utc).strftime("%Y%m%d")

    if args.repair:
        return repair(films, end, args)

    print(f"pageviews: {len(films)} films, window {WINDOW_START}-{end}")
    print(f"measurement starts {MEASUREMENT_START_YEAR}; every film predates it")
    print()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    rows: list[dict] = []
    derived: dict[str, dict[str, float]] = {}

    no_data, transient = fetch_all(session, films, end, rows, derived)

    # A second pass over only the transient failures. They are a handful at
    # most, so a slower, more patient retry costs seconds and recovers films
    # that would otherwise vanish from the dataset without explanation.
    if transient:
        print(f"\n重試 {len(transient)} 部暫時性失敗（非 404）")
        retry = films[films["enwiki_title"].isin(transient)]
        still_no_data, still_failing = fetch_all(session, retry, end, rows,
                                                 derived, retries=6)
        no_data.extend(still_no_data)
        if still_failing:
            print(f"  仍然失敗: {', '.join(still_failing)}")
        else:
            print("  全部補回")

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
    print(f"articles with no data:    {len(no_data)}"
          + (f" ({', '.join(no_data[:3])})" if no_data else ""))
    print()
    print(f"median daily views:       "
          f"{films['interest_median_daily'].median():.0f}")
    print(f"measurement lag (years):  "
          f"{films['years_to_measurement'].min()}-"
          f"{films['years_to_measurement'].max()}")
    print()
    # Not a validation: rank(pct=True) is uniform by construction, so these
    # quartiles land on .25/.50/.75 whatever the input. It is here to show the
    # cohort sizes. The check that can actually fail is the correlation against
    # years_to_measurement, in scripts/qa_m1_data.py section H.
    print("cohort sizes (percentiles are uniform by construction):")
    for bucket, grp in films.groupby("release_bucket"):
        pct = grp["interest_cohort_pct"].dropna()
        if len(pct):
            print(f"  {bucket}  n={len(pct):>4}  "
                  f"p25={pct.quantile(.25):.2f} "
                  f"p50={pct.median():.2f} p75={pct.quantile(.75):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
