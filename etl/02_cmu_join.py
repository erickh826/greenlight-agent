#!/usr/bin/env python3
"""Join the Wikidata spine to CMU plot summaries.

CMU keys films by Wikipedia page ID and Freebase ID; Wikidata keys them by QID.
Nothing links the two, so the join runs on a normalised title plus a release
year within +/-1.

Two things the raw data forces:

- Match on `title`, never `enwiki_title`. The Wikipedia article name carries a
  disambiguation suffix ("The Martian (film)") on about half the spine, and CMU
  stores the plain title. `enwiki_title` is still the right key for the
  pageviews API later -- the two columns are not interchangeable.
- CMU release dates come as YYYY-MM-DD, YYYY-MM, bare YYYY, or empty. Only the
  leading year is usable, and rows without one can still match on title alone
  when that title is unique in the corpus.

Usage:
    ./scripts/run_etl.sh etl/02_cmu_join.py --spine data/films_spine_2015.parquet

Output: data/films_with_plots.parquet
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CMU_DIR_DEFAULT = ROOT / "data" / "MovieSummaries"
OUT_DEFAULT = ROOT / "data" / "films_with_plots.parquet"

META_COLS = ["wiki_page_id", "freebase_id", "title", "release_date", "revenue",
             "runtime", "languages", "countries", "genres"]

LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+")
NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Wikipedia disambiguation suffix: "(film)", "(2015 film)", "(2015 American film)"
PAREN_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def normalise(title: str) -> str:
    """Fold a title to its comparable core.

    Unicode is decomposed first so 'JonBenét' and 'JonBenet' agree, then the
    leading article is dropped -- CMU and Wikidata disagree on it often enough
    to matter -- and everything that is not alphanumeric is collapsed.
    """
    t = unicodedata.normalize("NFKD", str(title)).encode("ascii", "ignore").decode()
    t = t.lower().strip()
    t = PAREN_SUFFIX.sub("", t)
    t = LEADING_ARTICLE.sub("", t)
    t = NON_ALNUM.sub(" ", t).strip()
    return re.sub(r"\s+", " ", t)


def cmu_year(raw) -> int | None:
    """CMU dates are inconsistent; only the leading year is dependable."""
    if pd.isna(raw):
        return None
    m = re.match(r"^(\d{4})", str(raw))
    return int(m.group(1)) if m else None


def load_cmu(cmu_dir: Path) -> tuple[pd.DataFrame, dict[str, str]]:
    meta_path = cmu_dir / "movie.metadata.tsv"
    plot_path = cmu_dir / "plot_summaries.txt"
    for p in (meta_path, plot_path):
        if not p.exists():
            sys.exit(f"ERROR: {p} not found. Extract MovieSummaries.tar.gz into "
                     f"{cmu_dir}/ first.")

    meta = pd.read_csv(meta_path, sep="\t", names=META_COLS, dtype=str,
                       quoting=3)  # QUOTE_NONE: titles contain bare quotes
    plots: dict[str, str] = {}
    with plot_path.open(encoding="utf-8") as fh:
        for line in fh:
            page_id, _, text = line.partition("\t")
            if text.strip():
                plots[page_id.strip()] = text.strip()
    return meta, plots


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spine", type=Path,
                    default=ROOT / "data" / "films_spine.parquet")
    ap.add_argument("--cmu-dir", type=Path, default=CMU_DIR_DEFAULT)
    ap.add_argument("--year-tolerance", type=int, default=1,
                    help="allowed release-year difference (default: 1)")
    ap.add_argument("--limit", type=int,
                    help="keep only the first N matched films")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()

    if not args.spine.exists():
        sys.exit(f"ERROR: {args.spine} not found. Run 01_wikidata_spine.py first.")

    spine = pd.read_parquet(args.spine)
    meta, plots = load_cmu(args.cmu_dir)

    print(f"spine:          {len(spine)} films "
          f"({spine.release_year.min()}-{spine.release_year.max()})")
    print(f"CMU metadata:   {len(meta)} rows")
    print(f"CMU plots:      {len(plots)} summaries")
    print()

    # Index CMU by normalised title. Only rows that actually carry a plot are
    # worth indexing -- roughly half the metadata file has none.
    meta = meta[meta.wiki_page_id.isin(plots.keys())].copy()
    meta["norm"] = meta.title.map(normalise)
    meta["year"] = meta.release_date.map(cmu_year)
    print(f"CMU rows with a plot: {len(meta)}")

    index: dict[str, list[tuple[int | None, str]]] = defaultdict(list)
    for norm, year, page_id in zip(meta.norm, meta.year, meta.wiki_page_id):
        index[norm].append((year, page_id))

    rows, stats = [], defaultdict(int)
    for film in spine.itertuples(index=False):
        norm = normalise(film.title)
        candidates = index.get(norm)
        if not candidates:
            stats["no_title_match"] += 1
            continue

        dated = [(y, pid) for y, pid in candidates if y is not None]
        near = [(y, pid) for y, pid in dated
                if abs(y - film.release_year) <= args.year_tolerance]

        if near:
            near.sort(key=lambda c: abs(c[0] - film.release_year))
            page_id = near[0][1]
            stats["matched_year"] += 1
        elif len(candidates) == 1 and not dated:
            # Unique title, CMU has no year at all -- accept it.
            page_id = candidates[0][1]
            stats["matched_undated"] += 1
        elif dated:
            stats["year_too_far"] += 1
            continue
        else:
            stats["ambiguous_undated"] += 1
            continue

        rows.append({
            "film_id": film.film_id,
            "title": film.title,
            "enwiki_title": film.enwiki_title,
            "release_year": film.release_year,
            "genres": list(film.genres),
            "budget_usd": film.budget_usd,
            "revenue_usd": film.revenue_usd,
            "cmu_page_id": page_id,
            "plot": plots[page_id],
        })

    if args.limit:
        rows = rows[:args.limit]

    if not rows:
        print("\nNo matches at all -- check the spine and CMU paths.",
              file=sys.stderr)
        return 1

    df = pd.DataFrame(rows)
    df["roi"] = df["revenue_usd"] / df["budget_usd"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)

    total = len(spine)
    matched = stats["matched_year"] + stats["matched_undated"]
    print()
    print("=== join funnel ===")
    print(f"spine films:              {total}")
    print(f"  matched on title+year:  {stats['matched_year']}")
    print(f"  matched, CMU undated:   {stats['matched_undated']}")
    print(f"  no title match in CMU:  {stats['no_title_match']}")
    print(f"  title hit, year too far:{stats['year_too_far']}")
    print(f"  ambiguous, no year:     {stats['ambiguous_undated']}")
    print(f"match rate:               {matched / total:.1%}")
    print()
    print(f"written:                  {len(df)} films with plot AND ROI")
    print(f"plot length (chars):      median "
          f"{int(df['plot'].str.len().median())}, "
          f"min {df['plot'].str.len().min()}")
    print(f"ROI median:               {df['roi'].median():.2f}")
    try:
        shown = args.out.resolve().relative_to(ROOT)
    except ValueError:
        shown = args.out
    print(f"-> {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
