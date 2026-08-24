-- films: one row per film, the spine everything else hangs off.
--
-- This file is the source of truth for the schema. app/prompts.py reads it at
-- startup and quotes it into the agent's system instruction, so the comments
-- here are what the model sees -- keep them accurate and keep them short.
--
-- Dataset boundary (measured, see docs/M1_DATA_FINDINGS.md):
--   1,238 films, released 1990-2014, each with a USD budget, a USD box office
--   figure, and a matched CMU plot summary. The upper bound is not a choice:
--   the CMU corpus stops in 2012 (70 films in 2013, 4 in 2014, none after).

CREATE TABLE IF NOT EXISTS films
(
    film_id              String,              -- Wikidata QID, e.g. 'Q25188'
    enwiki_title         String,              -- Wikipedia article name, WITH its
                                              -- disambiguation suffix. Differs from
                                              -- `title` for 47% of rows and is the
                                              -- only key the pageviews API accepts.
    title                String,              -- plain title; the key CMU matched on
    release_year        UInt16,
    release_bucket      LowCardinality(String), -- '1990-1994' … '2010-2014'
    genres              Array(LowCardinality(String)),

    budget_usd          Nullable(UInt64),     -- USD only; other currencies dropped
    revenue_usd         Nullable(UInt64),     -- worldwide gross, USD only
    roi                 Nullable(Float32) MATERIALIZED
                            if(budget_usd > 0, revenue_usd / budget_usd, NULL),

    -- Derived by Gemini during ETL from the controlled vocabulary in
    -- etl/vocab.py. Free-form values here would break every aggregate below.
    motif_tags           Array(LowCardinality(String)),   -- 3-6 of MOTIFS
    act_structure        LowCardinality(String),          -- 1 of ACT_STRUCTURES
    character_archetypes Array(LowCardinality(String)),   -- 2-4 of ARCHETYPES
    tone_axis            Float32,             -- -1 bleak … +1 warm
    conflict_scale       LowCardinality(String),  -- personal|communal|existential

    -- Wikipedia interest proxy, derived from film_attention.
    --
    -- READ THIS BEFORE USING THESE COLUMNS. The pageviews API begins
    -- 2015-07-01 and every film here was released before it -- between 1 and 25
    -- years before, median 12. These columns therefore measure how much people
    -- still looked a film up years after it came out. They are NOT a release
    -- reaction, there is no opening peak in this window, and nothing here
    -- decays from a premiere.
    interest_median_daily Nullable(UInt32),   -- typical daily views in-window
    interest_p95_daily    Nullable(UInt32),   -- how large its spikes get
    interest_trend_slope  Nullable(Float32),  -- in-window linear trend; >0 rising
    interest_cohort_pct   Nullable(Float32),  -- 0-1 percentile within release_bucket.
                                              -- THE ONLY COLUMN COMPARABLE ACROSS
                                              -- RELEASE YEARS. A 2014 film and a
                                              -- 1990 film were measured 24 years
                                              -- apart, so their raw counts are not
                                              -- on the same scale; their percentiles
                                              -- within same-aged peers are.
    years_to_measurement  UInt8,              -- release_year → 2015, so 1-25
    attention_kind        LowCardinality(String) DEFAULT 'sustained_interest',
                                              -- constant today: every film predates
                                              -- the window. Present so the semantics
                                              -- are visible in the schema, and so a
                                              -- post-2015 plot source could later be
                                              -- marked 'release_window' beside it.

    ingested_at          DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (release_bucket, film_id);
