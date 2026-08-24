-- Materialized views: the pre-aggregated surface the agent actually queries.
--
-- These are sized for a 1,238-film dataset, which is small enough that grouping
-- granularity decides whether a query returns evidence or returns nothing. The
-- agent's own floor for acting on a result is 8 samples (app/config.py
-- MIN_SAMPLE_SIZE), so any grouping that routinely lands under it is a view
-- that answers "insufficient_evidence" on stage.
--
-- Reading an AggregatingMergeTree: the -State columns below are merged at read
-- time with the matching -Merge function, e.g.
--     SELECT countMerge(sample_count), quantileMerge(0.5)(roi_median) …
-- Selecting them without -Merge returns opaque binary state, not numbers.


-- ---------------------------------------------------------------------------
-- Archetype performance by era.
--
-- Grouped on release_bucket, not release_year. Per-year buckets over 25 years
-- and 25 archetypes give 625 cells holding ~3,714 (film, archetype) pairs --
-- about 5.9 each, below the floor of 8. Five-year cohorts give 125 cells at
-- ~29.7 each. The bucket labels come from etl/vocab.py RELEASE_BUCKETS.
--
-- Expected query shape: filter on archetype, optionally on release_bucket.
-- Typical cell ~30 samples; a single archetype across all eras ~150.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_archetype_performance
ENGINE = AggregatingMergeTree
ORDER BY (archetype, release_bucket)
AS SELECT
    arrayJoin(character_archetypes) AS archetype,
    release_bucket,
    countState()                        AS sample_count,
    quantileState(0.5)(roi)             AS roi_median,
    quantileState(0.75)(roi)            AS roi_p75,
    avgState(tone_axis)                 AS avg_tone,
    quantileState(0.5)(interest_cohort_pct) AS interest_pct_median
FROM films
WHERE roi IS NOT NULL
GROUP BY archetype, release_bucket;


-- ---------------------------------------------------------------------------
-- Motif co-occurrence.
--
-- SYSTEM_SPEC flagged the original two-arrayJoin form as producing a cartesian
-- product. Tested against ClickHouse 26.2, it does something worse: two
-- arrayJoins over the same array are evaluated in lockstep on a shared index,
-- so ['revenge','redemption','survival'] yields three rows -- (revenge,
-- revenge), (redemption, redemption), (survival, survival) -- not nine. The
-- WHERE motif_a < motif_b that follows then discards all of them, and the view
-- materialises empty without raising anything.
--
-- Build the pair list inside one expression instead: map every element against
-- the whole array, flatten to a single array of tuples, drop self-pairs and
-- mirrored duplicates with p.1 < p.2, then arrayJoin once. Verified to give
-- C(n,2) pairs.
--
-- Expected query shape: filter on motif_a AND motif_b. 30 motifs give 435
-- pairs; at 3-6 motifs per film that is roughly 8-28 samples per pair.
--
-- NOTE FOR THE AGENT: this view has no era dimension on purpose. Adding a year
-- filter on top of a motif pair drops most cells under the evidence floor.
-- Query the pair first, and only narrow by era if the sample count allows.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_motif_pair_stats
ENGINE = AggregatingMergeTree
ORDER BY (motif_a, motif_b)
AS SELECT
    pair.1 AS motif_a,
    pair.2 AS motif_b,
    countState()                            AS sample_count,
    quantileState(0.5)(roi)                 AS roi_median,
    quantileState(0.5)(interest_cohort_pct) AS interest_pct_median
FROM (
    SELECT
        roi,
        interest_cohort_pct,
        arrayJoin(
            arrayFilter(
                p -> p.1 < p.2,
                arrayFlatten(
                    arrayMap(a -> arrayMap(b -> (a, b), motif_tags), motif_tags)
                )
            )
        ) AS pair
    FROM films
    WHERE roi IS NOT NULL
)
GROUP BY motif_a, motif_b;


-- ---------------------------------------------------------------------------
-- Interest trajectory per film, by calendar year.
--
-- Replaces mv_attention_curve, which was ordered on days_since_peak. That
-- column has no meaning for this dataset: the pageview window opens years after
-- every film's release, so there is no opening peak to measure days from, and
-- whatever maximum appears in-window is usually an unrelated news event.
--
-- Calendar year is interpretable without inventing a premiere: it answers "how
-- did lookups for this film move between 2015 and now", which is the question
-- the data can support.
--
-- Expected query shape: filter on film_id, read the whole series.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_interest_by_year
ENGINE = AggregatingMergeTree
ORDER BY (film_id, calendar_year)
AS SELECT
    film_id,
    toYear(date)    AS calendar_year,
    sumState(views) AS total_views,
    avgState(views) AS avg_daily_views
FROM film_attention
GROUP BY film_id, calendar_year;
