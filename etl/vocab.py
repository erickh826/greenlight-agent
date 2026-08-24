"""Controlled vocabulary — the single source of truth for every domain term.

Everything downstream imports from here rather than restating the lists:

    etl/04_motif_enrichment.py   the Gemini response_schema enums
    app/contracts.py             the same enums on the API contracts
    app/prompts.py               the legal values quoted into the system instruction
    sql/003_materialized_views.sql   the values named in the view comments

Free-form tagging is what makes this kind of aggregation fail: a model asked for
"themes" produces "redemption", "redemptive arc", "seeking redemption" and
"atonement" as four separate strings, and every GROUP BY over them returns
buckets of one. The lists below are closed on purpose. Adding a term is a
deliberate act that reshapes existing aggregates, so prefer reusing a near
match over extending them.

Sizes follow SYSTEM_SPEC §4.4: 30 motifs, 25 archetypes, 6 act structures.
"""

from __future__ import annotations

from typing import Final

# --- Narrative motifs -------------------------------------------------------
# Assigned 3-6 per film. Phrased as the dramatic situation rather than the
# subject matter, so that films from different genres can share one.
MOTIFS: Final[tuple[str, ...]] = (
    "redemption",
    "revenge",
    "coming_of_age",
    "forbidden_love",
    "survival",
    "betrayal_by_ally",
    "rise_and_fall",
    "fish_out_of_water",
    "impossible_heist",
    "wrongful_accusation",
    "found_family",
    "sacrifice_for_others",
    "corrupted_power",
    "identity_concealed",
    "race_against_time",
    "return_home",
    "mentor_and_protege",
    "unlikely_alliance",
    "obsessive_pursuit",
    "loss_of_innocence",
    "man_versus_system",
    "second_chance",
    "hidden_conspiracy",
    "moral_compromise",
    "isolation_and_madness",
    "generational_conflict",
    "doomed_ambition",
    "reluctant_hero",
    "class_divide",
    "memory_and_truth",
)

# --- Character archetypes ---------------------------------------------------
# Assigned 2-4 per film. Roles in the story's machinery, not personality types.
ARCHETYPES: Final[tuple[str, ...]] = (
    "reluctant_hero",
    "antihero",
    "mentor",
    "trickster",
    "shadow_antagonist",
    "institutional_antagonist",
    "loyal_companion",
    "femme_fatale",
    "innocent",
    "outcast",
    "authority_figure",
    "rebel",
    "caregiver",
    "seeker",
    "everyman",
    "ruler",
    "creator",
    "destroyer",
    "orphan",
    "survivor",
    "double_agent",
    "prodigy",
    "fallen_idol",
    "gatekeeper",
    "witness",
)

# --- Three-act shapes -------------------------------------------------------
# Exactly one per film.
ACT_STRUCTURES: Final[tuple[str, ...]] = (
    "classic_three_act",
    "hero_journey",
    "tragedy_arc",
    "ensemble_parallel",
    "nonlinear_reveal",
    "circular_return",
)

# --- Scale of the central conflict ------------------------------------------
# Exactly one per film. Matches SYSTEM_SPEC §3.1 `conflict_scale`.
CONFLICT_SCALES: Final[tuple[str, ...]] = (
    "personal",
    "communal",
    "existential",
)

# --- Release cohorts --------------------------------------------------------
# One bucketing scheme serves three purposes, and they must agree:
#
#   1. mv_archetype_performance groups by it. Grouping by single year over 1,238
#      films leaves ~5.9 samples per (archetype, year) cell, under the
#      insufficient_evidence floor of 8; five-year buckets give ~29.7.
#   2. interest_cohort_pct is a percentile *within* a bucket. Pageviews start
#      2015-07 while the films span 1990-2014, so a 2014 release was measured
#      one year after opening and a 1990 release twenty-five years after. Raw
#      view counts are not comparable across that gap; a percentile within a
#      cohort of similarly-aged films is.
#   3. The system instruction describes eras to the agent in these terms.
RELEASE_BUCKETS: Final[tuple[str, ...]] = (
    "1990-1994",
    "1995-1999",
    "2000-2004",
    "2005-2009",
    "2010-2014",
)

BUCKET_SPAN: Final[int] = 5
BUCKET_FLOOR: Final[int] = 1990

# The first day the Wikimedia pageviews API has data for. Every film in the
# dataset was released before it -- see docs/M1_DATA_FINDINGS.md §1.
MEASUREMENT_START_YEAR: Final[int] = 2015


def release_bucket(year: int) -> str:
    """Map a release year to its cohort label.

    Raises rather than silently bucketing an out-of-range year: a film outside
    1990-2014 has no cohort to be a percentile within, and quietly assigning it
    to the nearest one would corrupt that cohort's distribution.
    """
    if not BUCKET_FLOOR <= year <= 2014:
        raise ValueError(
            f"release year {year} is outside the dataset range "
            f"{BUCKET_FLOOR}-2014; see docs/M1_DATA_FINDINGS.md"
        )
    start = BUCKET_FLOOR + (year - BUCKET_FLOOR) // BUCKET_SPAN * BUCKET_SPAN
    return f"{start}-{start + BUCKET_SPAN - 1}"


def years_to_measurement(year: int) -> int:
    """How long after release the pageview window opens, in years.

    Ranges 1 (a 2014 release) to 25 (a 1990 release). Carried as a column so
    scoring can condition on it -- a film measured one year out still carries
    some release afterglow, one measured twenty-five years out does not.
    """
    return MEASUREMENT_START_YEAR - year


def as_sql_list(values: tuple[str, ...]) -> str:
    """Render a vocabulary as a SQL/prompt-friendly quoted list."""
    return ", ".join(f"'{v}'" for v in values)


__all__ = [
    "MOTIFS", "ARCHETYPES", "ACT_STRUCTURES", "CONFLICT_SCALES",
    "RELEASE_BUCKETS", "BUCKET_SPAN", "BUCKET_FLOOR", "MEASUREMENT_START_YEAR",
    "release_bucket", "years_to_measurement", "as_sql_list",
]
