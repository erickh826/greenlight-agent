#!/usr/bin/env python3
"""Eyeball a handful of interest curves before trusting the derived columns.

The derived columns are three numbers standing in for a 4,000-day series. That
compression hides the two failure modes that matter here:

  1. A title that resolved to the wrong article. `enwiki_title` comes from a
     Wikidata sitelink, so a disambiguation slip lands us on a novel, a band, or
     a person -- and the series still looks perfectly well-formed.
  2. A single news spike dominating the window (a death, a remake, a meme). The
     median is meant to be immune to that; interest_p95_daily is meant to show
     it. Seeing both on the same picture is the check.

Prints a monthly-mean sparkline per film so the shape is visible in a terminal.

Usage:
    ./scripts/run_etl.sh scripts/spotcheck_curves.py
    ./scripts/run_etl.sh scripts/spotcheck_curves.py --n 8 --seed 7
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ATTENTION = ROOT / "data" / "attention.parquet"
ENRICHED = ROOT / "data" / "films_enriched.parquet"

BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: np.ndarray) -> str:
    """Log-scaled, because interest spans three orders of magnitude."""
    v = np.log1p(values.astype(float))
    lo, hi = v.min(), v.max()
    if hi - lo < 1e-9:
        return BLOCKS[0] * len(v)
    idx = ((v - lo) / (hi - lo) * (len(BLOCKS) - 1)).round().astype(int)
    return "".join(BLOCKS[i] for i in idx)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=5, help="how many films to sample")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--film-id", action="append",
                    help="inspect a specific QID (repeatable)")
    args = ap.parse_args()

    for p in (ATTENTION, ENRICHED):
        if not p.exists():
            sys.exit(f"ERROR: {p} not found. Run etl/03_pageviews.py first.")

    films = pd.read_parquet(ENRICHED)
    att = pd.read_parquet(ATTENTION)
    att["date"] = pd.to_datetime(att["date"])

    have = films[films["interest_median_daily"].notna()]

    if args.film_id:
        picked = have[have["film_id"].isin(args.film_id)]
    else:
        # Stratify across the interest range rather than sampling uniformly --
        # a uniform draw from a long-tailed distribution returns five obscure
        # films and tells us nothing about the top of the scale.
        ranked = have.sort_values("interest_median_daily")
        cuts = np.linspace(0, len(ranked) - 1, args.n).round().astype(int)
        picked = ranked.iloc[cuts]

    print(f"=== 曲線抽查（{len(picked)} 部）===")
    print("每格 = 一個月的日均瀏覽（對數尺度）；量測窗 2015-07 至今\n")

    for film in picked.itertuples(index=False):
        series = att[att["film_id"] == film.film_id].sort_values("date")
        if series.empty:
            print(f"  {film.title!r}: attention.parquet 裡沒有列 — 不正常")
            continue

        monthly = (series.set_index("date")["views"]
                         .resample("MS").mean().dropna())
        span = f"{monthly.index[0]:%Y-%m}–{monthly.index[-1]:%Y-%m}"
        ratio = film.interest_p95_daily / max(film.interest_median_daily, 1)

        print(f"{film.title}  ({film.release_year}, {film.release_bucket})")
        print(f"  enwiki: {film.enwiki_title}")
        print(f"  {sparkline(monthly.to_numpy())}  {span}")
        print(f"  中位 {film.interest_median_daily:>7,.0f}/日   "
              f"p95 {film.interest_p95_daily:>8,.0f}   "
              f"p95/中位 {ratio:>6.1f}x")
        print(f"  趨勢 {film.interest_trend_slope:+.2f}/年   "
              f"cohort 百分位 {film.interest_cohort_pct:.2f}   "
              f"量測延遲 {film.years_to_measurement} 年")

        # A spike this sharp is either a real news event or a wrong article.
        # Naming the day makes it a two-second manual check instead of a guess.
        top = series.nlargest(1, "views").iloc[0]
        print(f"  最高單日 {top['views']:,} 於 {top['date']:%Y-%m-%d}"
              + ("   ← 尖峰遠高於基線，值得看一眼那天發生什麼"
                 if ratio > 20 else ""))
        print()

    print("要看的東西：")
    print("  - 曲線大致平坦或緩降 = 正常的長尾存續；沒有上映峰值是預期內的")
    print("  - 條目名明顯不是這部電影 = 消歧義錯誤，該片要從資料集剔除")
    print("  - p95/中位 > 20x = 單一新聞事件主導，中位數仍可用但要留意")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
