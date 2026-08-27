#!/usr/bin/env python3
"""Load the M1 parquet output into ClickHouse and build the views.

Order matters and is not adjustable. A ClickHouse materialized view declared
without POPULATE fills only from inserts that arrive after it exists, so the
DDL in sql/003 must run BEFORE the data goes in. Loading first and creating
views second leaves three empty views and no error anywhere -- the same class
of silent failure the arrayJoin bug had.

What deliberately does not travel:

  plot        CMU summary text, CC BY-SA 3.0. It is present in
              films_enriched.parquet because 04 needed it, and it stops here.
              Only the abstract tags derived from it reach the database
              (SYSTEM_SPEC 4.6). The column list below is explicit for this
              reason -- never widen it to "everything in the dataframe".

  roi         MATERIALIZED in sql/001, computed from budget and revenue.
  has_interest_signal
              MATERIALIZED in sql/001. Inserting into either is an error.

The 28 films with no motif labels (plot under 500 characters) are still loaded.
They keep their financial and interest columns, and their empty motif arrays
mean arrayJoin drops them from the motif and archetype views by itself. Leaving
them out of `films` instead would break the film_attention join, which has all
1,238.

Usage:
    ./scripts/run_etl.sh etl/05_load_clickhouse.py --dry-run
    ./scripts/run_etl.sh etl/05_load_clickhouse.py
    ./scripts/run_etl.sh etl/05_load_clickhouse.py --drop   # rebuild from zero
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app.env import load_env  # noqa: E402

# Explicit, and in the table's column order. Anything not named here does not
# reach ClickHouse -- see the module docstring on `plot`.
FILM_COLUMNS = [
    "film_id", "enwiki_title", "title", "release_year", "release_bucket",
    "genres", "budget_usd", "revenue_usd",
    "motif_tags", "act_structure", "character_archetypes", "tone_axis",
    "conflict_scale",
    "interest_median_daily", "interest_p95_daily", "interest_trend_slope",
    "interest_cohort_pct", "years_to_measurement", "attention_kind",
]

ATTENTION_BATCH = 500_000

SQL_FILES = ("sql/001_films.sql", "sql/002_film_attention.sql",
             "sql/003_materialized_views.sql")


def connect():
    import clickhouse_connect
    load_env()
    return clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ.get("CLICKHOUSE_PORT", 8443)),
        username=os.environ["CLICKHOUSE_USER"],
        password=os.environ["CLICKHOUSE_PASSWORD"],
        secure=os.environ.get("CLICKHOUSE_SECURE", "true").lower() == "true",
        database=os.environ.get("CLICKHOUSE_DATABASE", "default"),
        # A 4.9M-row load in 500k batches; the default socket timeout is not
        # generous enough for the largest of them on a cold dev-tier service.
        send_receive_timeout=600,
    )


def statements(path: Path) -> list[str]:
    """Split a .sql file into executable statements.

    Comments are stripped first: the DDL carries long explanatory blocks, and a
    trailing `--` comment on the last line would otherwise swallow the
    semicolon that separates two statements.
    """
    sql = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
    return [s.strip() for s in sql.split(";") if s.strip()]


def build_films(args) -> pd.DataFrame:
    films = pd.read_parquet(args.films)
    motifs = pd.read_parquet(args.motifs)

    df = films.merge(motifs, on="film_id", how="left")
    if len(df) != len(films):
        sys.exit(f"ERROR: merge changed the row count {len(films)} -> {len(df)}; "
                 "films_motifs.parquet has duplicate film_id values.")

    unlabelled = df["act_structure"].isna()
    # Empty arrays rather than NULL: the columns are Array(LowCardinality(String))
    # and not nullable, and an empty array is exactly right here -- arrayJoin
    # yields no rows for it, so these films leave the motif views alone.
    df["motif_tags"] = df["motif_tags"].apply(
        lambda v: list(v) if isinstance(v, (list, np.ndarray)) else [])
    df["character_archetypes"] = df["character_archetypes"].apply(
        lambda v: list(v) if isinstance(v, (list, np.ndarray)) else [])
    df["genres"] = df["genres"].apply(
        lambda v: list(v) if isinstance(v, (list, np.ndarray)) else [])
    df["act_structure"] = df["act_structure"].fillna("")
    df["conflict_scale"] = df["conflict_scale"].fillna("")
    df["tone_axis"] = df["tone_axis"].fillna(0.0).astype("float32")

    # UInt64 columns reject NaN; every film has both, but be explicit rather
    # than discover it as a driver error mid-insert.
    for col in ("budget_usd", "revenue_usd"):
        if df[col].isna().any():
            sys.exit(f"ERROR: {df[col].isna().sum()} rows have a null {col}")
        df[col] = df[col].astype("int64")

    # These are a median and a 95th percentile over an even-length series, so
    # they arrive as .0 or .5 floats. The DDL declares UInt32 -- half a pageview
    # is not a thing -- so round rather than widen the column.
    for col in ("interest_median_daily", "interest_p95_daily"):
        df[col] = df[col].round().astype("Int64")   # nullable integer
    df["years_to_measurement"] = df["years_to_measurement"].astype("int16")
    df["release_year"] = df["release_year"].astype("int32")

    print(f"films:        {len(df):,}")
    print(f"  已標註母題:  {(~unlabelled).sum():,}")
    print(f"  無母題:      {unlabelled.sum()} 部（劇情過短，空陣列，"
          f"自動不進母題 view）")
    return df[FILM_COLUMNS]


def apply_ddl(client, args) -> None:
    """Create tables, then views -- in that order, before any insert."""
    for name in SQL_FILES:
        path = ROOT / name
        for st in statements(path):
            try:
                client.command(st)
            except Exception as exc:
                sys.exit(f"ERROR in {name}:\n{st[:300]}\n-> {exc}")
        print(f"  {name}")


def load(client, films: pd.DataFrame, args) -> None:
    started = time.monotonic()
    client.insert_df("films", films)
    print(f"films 載入 {len(films):,} 列  "
          f"{time.monotonic() - started:.1f}s")

    attention = pd.read_parquet(args.attention)
    attention["date"] = pd.to_datetime(attention["date"]).dt.date
    attention["views"] = attention["views"].astype("int64")

    started = time.monotonic()
    for i in range(0, len(attention), ATTENTION_BATCH):
        batch = attention.iloc[i:i + ATTENTION_BATCH]
        client.insert_df("film_attention", batch)
        print(f"  film_attention {min(i + ATTENTION_BATCH, len(attention)):>9,}"
              f"/{len(attention):,}")
    print(f"film_attention 載入 {len(attention):,} 列  "
          f"{time.monotonic() - started:.1f}s")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--films", type=Path,
                    default=ROOT / "data" / "films_enriched.parquet")
    ap.add_argument("--motifs", type=Path,
                    default=ROOT / "data" / "films_motifs.parquet")
    ap.add_argument("--attention", type=Path,
                    default=ROOT / "data" / "attention.parquet")
    ap.add_argument("--drop", action="store_true",
                    help="drop the tables and views first (full rebuild)")
    ap.add_argument("--dry-run", action="store_true",
                    help="assemble the dataframe and stop, no connection")
    args = ap.parse_args()

    for path in (args.films, args.motifs, args.attention):
        if not path.exists():
            sys.exit(f"ERROR: {path} not found.")

    films = build_films(args)

    assert "plot" not in films.columns, "CMU plot text must never be loaded"

    if args.dry_run:
        print("\n--dry-run: 不連線。前兩列：")
        print(films.head(2).to_string())
        return 0

    client = connect()
    print(f"\nClickHouse {client.server_version}")

    if args.drop:
        for name in ("mv_archetype_performance", "mv_motif_pair_stats",
                     "mv_interest_by_year"):
            client.command(f"DROP VIEW IF EXISTS {name}")
        for name in ("films", "film_attention"):
            client.command(f"DROP TABLE IF EXISTS {name}")
        print("已清空既有資料表與 view")

    existing = client.query(
        "SELECT count() FROM system.tables WHERE database = currentDatabase() "
        "AND name IN ('films','film_attention')").result_rows[0][0]
    if existing:
        rows = client.query("SELECT count() FROM films").result_rows[0][0]
        if rows:
            sys.exit(f"ERROR: films already holds {rows:,} rows. Re-running "
                     "would double them -- pass --drop to rebuild.")

    print("\n建立資料表與 view（必須早於 INSERT，否則 view 會是空的）：")
    apply_ddl(client, args)

    print()
    load(client, films, args)

    print("\n=== 載入後計數 ===")
    for table in ("films", "film_attention", "mv_archetype_performance",
                  "mv_motif_pair_stats", "mv_interest_by_year"):
        n = client.query(f"SELECT count() FROM {table}").result_rows[0][0]
        print(f"  {table:<26} {n:>10,}")

    print("\n接著跑 ./scripts/run_etl.sh scripts/verify_mv.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
