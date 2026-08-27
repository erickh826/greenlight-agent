"""System instructions, assembled from the schema files rather than transcribed.

The DDL in sql/ is the source of truth. Copying it into a string here would let
the two drift, and the failure mode is the worst kind: the agent writes a
perfectly reasonable query against a column that no longer exists, retries
twice, and reports insufficient_evidence. Reading the files at startup means a
schema change reaches the model with no second edit.

The vocabulary comes from etl/vocab.py for the same reason.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from app.config import MIN_INTEREST_SIGNAL, MIN_SAMPLE_SIZE

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"

sys.path.insert(0, str(ROOT / "etl"))
from vocab import (  # noqa: E402
    ACT_STRUCTURES, ARCHETYPES, CONFLICT_SCALES, MOTIFS, RELEASE_BUCKETS,
    as_sql_list,
)


@lru_cache(maxsize=1)
def schema_ddl() -> str:
    """The full DDL, in file order."""
    files = sorted(SQL_DIR.glob("*.sql"))
    if not files:
        raise FileNotFoundError(
            f"no .sql files in {SQL_DIR}; the schema is the source of truth "
            "for the system instruction and cannot be skipped"
        )
    return "\n\n".join(f.read_text(encoding="utf-8") for f in files)


def vocabulary_block() -> str:
    return f"""CONTROLLED VOCABULARY -- these are closed sets. Any value outside
them will match nothing, because the database only ever contains these.

motif_tags ({len(MOTIFS)}):        {as_sql_list(MOTIFS)}
character_archetypes ({len(ARCHETYPES)}): {as_sql_list(ARCHETYPES)}
act_structure ({len(ACT_STRUCTURES)}):    {as_sql_list(ACT_STRUCTURES)}
conflict_scale ({len(CONFLICT_SCALES)}):   {as_sql_list(CONFLICT_SCALES)}
release_bucket ({len(RELEASE_BUCKETS)}):   {as_sql_list(RELEASE_BUCKETS)}"""


DATASET_BOUNDARY = f"""DATASET BOUNDARY -- state these limits rather than
working around them.

1,238 films released 1990-2014. Every one has a USD budget, a USD worldwide
gross, and a plot summary. The 2014 ceiling is a property of the source corpus,
not a filter: nothing later exists to query.

The interest_* columns are NOT release-window attention. Wikipedia pageview data
begins 2015-07-01, which is between 1 and 25 years after each film opened
(median 12). They measure how much a film was still looked up years later.
Never describe them as opening reaction, buzz at launch, or decay from a
premiere.

Use `interest_cohort_pct` for any comparison between films. It is a percentile
within a five-year release cohort, so it is bounded 0-1 and unit-free.
`interest_median_daily` is a raw daily count spanning roughly 13-40x within a
single cohort, so a difference in it says more about how famous a film is than
about anything a comparison would be trying to establish.

Release year is not a confound here, and do not present it as one: measured over
all 1,238 films, measurement lag against raw daily median is r = -0.009."""


QUERY_GUIDANCE = f"""QUERYING

The views are AggregatingMergeTree. Read -State columns through the matching
-Merge function; selecting them raw returns binary state, not numbers:

    SELECT archetype,
           countMerge(sample_count)          AS n,
           quantileMerge(0.5)(roi_median)    AS roi_median
    FROM mv_archetype_performance
    WHERE archetype = 'antihero'
    GROUP BY archetype

Array membership uses has():

    SELECT count() FROM films WHERE has(motif_tags, 'revenge')

Sample size gates everything. Below {MIN_SAMPLE_SIZE} rows, report
insufficient_evidence instead of a figure:

    SELECT motif_a, motif_b,
           countMerge(sample_count)       AS n,
           quantileMerge(0.5)(roi_median) AS roi_median
    FROM mv_motif_pair_stats
    WHERE motif_a = 'redemption' AND motif_b = 'revenge'
    GROUP BY motif_a, motif_b
    HAVING n >= {MIN_SAMPLE_SIZE}

Order of narrowing matters on a dataset this size. Query the broad aggregate
first, check the count, and only add a release_bucket or budget filter if what
came back can afford to be split. Filtering first is how a query that had
30 samples returns 2.

The two counts are not interchangeable. `sample_count` covers the ROI figures;
`interest_sample_count` covers the interest figures, which are aggregated only
over films above the measurement floor ({MIN_INTEREST_SIGNAL} daily views).
It is the smaller of the two. When you report an interest figure, its
sample_count must be `interest_sample_count`:

    SELECT archetype,
           countMerge(sample_count)                     AS n_roi,
           countMerge(interest_sample_count)            AS n_interest,
           quantileMerge(0.5)(roi_median)               AS roi_median,
           quantileMerge(0.5)(interest_pct_median)      AS interest_median
    FROM mv_archetype_performance
    WHERE archetype = 'antihero'
    GROUP BY archetype

If `n_interest` is below {MIN_SAMPLE_SIZE} while `n_roi` is not, report the ROI
figure and omit the interest one. Do not report an attention score of zero: a
missing figure is N/A, and scoring drops the dimension and reweights rather than
counting the absence against the proposal. Querying `films` directly for
interest requires `WHERE has_interest_signal` yourself; the views apply it for
you."""


def analyst_system_instruction() -> str:
    """Shared preamble for every agent that touches the database."""
    return f"""You analyse a ClickHouse database of historical films to find
which structural combinations have performed well, and you support every number
with the query that produced it.

{DATASET_BOUNDARY}

SCHEMA (authoritative -- read the comments, they carry the semantics):

{schema_ddl()}

{vocabulary_block()}

{QUERY_GUIDANCE}

You decide what to query. Do not ask which query to run; choose one, run it,
and report what came back -- including when what came back is too thin to
support a claim."""


__all__ = [
    "schema_ddl", "vocabulary_block", "analyst_system_instruction",
    "DATASET_BOUNDARY", "QUERY_GUIDANCE",
]
