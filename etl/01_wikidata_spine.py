#!/usr/bin/env python3
"""Build the Wikidata film spine.

Queries the Wikidata SPARQL endpoint one release year at a time -- a single
whole-range query times out -- and writes one row per film with the fields the
rest of the pipeline joins on.

Currency is the trap here. P2142 (box office) and P2130 (cost) carry units, and
a film with a JPY box office against a USD budget produces a meaningless ROI.
Both amounts are therefore read through their full statement nodes so the unit
QID comes back with the value, and anything not in USD is dropped.

Usage:
    ./scripts/run_etl.sh etl/01_wikidata_spine.py --since-year 2015 --limit 50
    ./scripts/run_etl.sh etl/01_wikidata_spine.py --since-year 2000 --no-require-budget

Output: data/films_spine.parquet
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "data" / "films_spine.parquet"

ENDPOINT = "https://query.wikidata.org/sparql"

# Wikidata asks every client to identify itself; the default urllib/requests
# agent is blocked outright.
USER_AGENT = (
    "greenlight-agent/0.1 (https://github.com/erickh826/greenlight-agent) "
    "python-requests"
)

USD = "Q4917"

# One year per request. `?fetch` pulls the amount and its unit together so the
# currency filter below is actually enforceable.
QUERY = """
SELECT ?film ?filmLabel ?year ?enwiki ?revenue ?revenueUnit ?budget ?budgetUnit
       (GROUP_CONCAT(DISTINCT ?genreLabel; separator="|") AS ?genres)
WHERE {
  ?film wdt:P31/wdt:P279* wd:Q11424 ;
        wdt:P577 ?date .
  FILTER(YEAR(?date) = %(year)d)

  ?film p:P2142 ?revStmt .
  ?revStmt psv:P2142 ?revNode .
  ?revNode wikibase:quantityAmount ?revenue ;
           wikibase:quantityUnit ?revenueUnit .
  %(budget_block)s

  ?article schema:about ?film ;
           schema:isPartOf <https://en.wikipedia.org/> ;
           schema:name ?enwiki .

  OPTIONAL {
    ?film wdt:P136 ?genre .
    ?genre rdfs:label ?genreLabel .
    FILTER(LANG(?genreLabel) = "en")
  }

  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
  BIND(%(year)d AS ?year)
}
GROUP BY ?film ?filmLabel ?year ?enwiki ?revenue ?revenueUnit ?budget ?budgetUnit
"""

BUDGET_REQUIRED = """
  ?film p:P2130 ?budStmt .
  ?budStmt psv:P2130 ?budNode .
  ?budNode wikibase:quantityAmount ?budget ;
           wikibase:quantityUnit ?budgetUnit .
"""

BUDGET_OPTIONAL = """
  OPTIONAL {
    ?film p:P2130 ?budStmt .
    ?budStmt psv:P2130 ?budNode .
    ?budNode wikibase:quantityAmount ?budget ;
             wikibase:quantityUnit ?budgetUnit .
  }
"""


def qid(uri: str | None) -> str | None:
    return uri.rsplit("/", 1)[-1] if uri else None


def run_year(year: int, require_budget: bool, timeout: int,
             retries: int = 3) -> list[dict]:
    """Fetch one release year. Returns raw bindings, unfiltered."""
    query = QUERY % {
        "year": year,
        "budget_block": BUDGET_REQUIRED if require_budget else BUDGET_OPTIONAL,
    }
    headers = {"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(ENDPOINT, params={"query": query},
                                headers=headers, timeout=timeout)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 30))
                print(f"    rate limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["results"]["bindings"]
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            wait = 5 * attempt
            print(f"    {type(exc).__name__}, retry {attempt}/{retries} "
                  f"in {wait}s", file=sys.stderr)
            time.sleep(wait)
    return []


def parse(rows: list[dict], year: int) -> tuple[list[dict], dict]:
    """Convert bindings to records, dropping non-USD amounts.

    Also returns the per-year drop counts so the funnel report can show where
    rows are actually lost rather than only the surviving total.
    """
    out: list[dict] = []
    stats = {"raw": len(rows), "non_usd_revenue": 0, "non_usd_budget": 0,
             "zero_amount": 0}

    for r in rows:
        film = qid(r["film"]["value"])
        if qid(r["revenueUnit"]["value"]) != USD:
            stats["non_usd_revenue"] += 1
            continue

        budget = None
        if "budget" in r:
            if qid(r["budgetUnit"]["value"]) != USD:
                stats["non_usd_budget"] += 1
                continue
            budget = float(r["budget"]["value"])

        revenue = float(r["revenue"]["value"])
        if revenue <= 0 or (budget is not None and budget <= 0):
            stats["zero_amount"] += 1
            continue

        genres = r.get("genres", {}).get("value", "")
        out.append({
            "film_id": film,
            "title": r["filmLabel"]["value"],
            "enwiki_title": r["enwiki"]["value"],
            "release_year": year,
            "genres": [g for g in genres.split("|") if g],
            "budget_usd": budget,
            "revenue_usd": revenue,
        })

    stats["kept"] = len(out)
    return out, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since-year", type=int, default=2015,
                    help="earliest release year, inclusive (default: 2015)")
    ap.add_argument("--until-year", type=int,
                    default=datetime.now(timezone.utc).year,
                    help="latest release year, inclusive (default: this year)")
    ap.add_argument("--limit", type=int,
                    help="stop once this many films are collected "
                         "(thin-slice runs; omit for a full pull)")
    ap.add_argument("--no-require-budget", dest="require_budget",
                    action="store_false",
                    help="keep films that have box office but no budget "
                         "(they cannot produce an ROI)")
    ap.add_argument("--timeout", type=int, default=180,
                    help="per-request timeout in seconds (default: 180)")
    ap.add_argument("--sleep", type=float, default=1.5,
                    help="pause between year queries (default: 1.5)")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()

    years = list(range(args.since_year, args.until_year + 1))
    print(f"Wikidata spine: {args.since_year}-{args.until_year} "
          f"({len(years)} years), budget "
          f"{'required' if args.require_budget else 'optional'}"
          + (f", limit {args.limit}" if args.limit else ""))
    print()

    records: list[dict] = []
    totals = {"raw": 0, "non_usd_revenue": 0, "non_usd_budget": 0,
              "zero_amount": 0}

    for year in years:
        rows = run_year(year, args.require_budget, args.timeout)
        parsed, stats = parse(rows, year)
        for k in totals:
            totals[k] += stats[k]
        records.extend(parsed)
        print(f"  {year}: {stats['raw']:>5} raw -> {stats['kept']:>4} kept "
              f"(running total {len(records)})")

        if args.limit and len({r["film_id"] for r in records}) >= args.limit:
            print(f"  reached limit {args.limit}, stopping")
            break
        time.sleep(args.sleep)

    if not records:
        print("\nNo rows. Widen the year range or drop --require-budget.",
              file=sys.stderr)
        return 1

    df = pd.DataFrame(records)

    # A film comes back once per (release date x box office x budget) statement
    # it holds, and both properties are routinely multi-valued:
    #
    #   P577  one release date per country -- 'The City of Lost Children'
    #         appears under six separate years.
    #   P2142 territory subtotals alongside the worldwide gross -- Shrek carries
    #         $4,879 and $268M and $484M and $488M, all tagged USD.
    #
    # Collapsing on the first row would have kept whichever the endpoint
    # happened to return, which is how Shrek ended up with an ROI of 0.00008.
    # Take the premiere (earliest date) and the worldwide figures (largest
    # amounts); territory subtotals are by definition smaller.
    before = len(df)
    df = (df.sort_values("release_year")
            .groupby("film_id", as_index=False)
            .agg(title=("title", "first"),
                 enwiki_title=("enwiki_title", "first"),
                 release_year=("release_year", "min"),
                 genres=("genres", "first"),
                 budget_usd=("budget_usd", "max"),
                 revenue_usd=("revenue_usd", "max")))
    collapsed = before - len(df)

    if args.limit:
        df = df.head(args.limit)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)

    print()
    print("=== spine funnel ===")
    print(f"raw SPARQL bindings:      {totals['raw']}")
    print(f"dropped, revenue not USD: {totals['non_usd_revenue']}")
    print(f"dropped, budget not USD:  {totals['non_usd_budget']}")
    print(f"dropped, zero amount:     {totals['zero_amount']}")
    print(f"collapsed, multi-value rows:{collapsed}")
    print(f"written:                  {len(df)} (unique films)")
    print()
    with_budget = df["budget_usd"].notna().sum()
    print(f"has budget (ROI possible): {with_budget} "
          f"({with_budget / len(df):.0%})")
    print(f"distinct release years:    {df['release_year'].nunique()}")
    try:
        shown = args.out.resolve().relative_to(ROOT)
    except ValueError:
        shown = args.out
    print(f"-> {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
