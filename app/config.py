"""Tunables that would otherwise get hardcoded into prompts and three agents.

Nothing here is a secret -- credentials come from the environment via app/mcp.py.
These are the numbers that shape output, kept in one place so changing one does
not mean grepping the prompt text for a literal.
"""

from __future__ import annotations

from typing import Final

# --- Scoring ----------------------------------------------------------------
# composite = commercial * w_commercial + attention * w_attention.
# Computed in app/scoring.py from the evidence list, never taken from whatever
# number the model says out loud.
SCORING_WEIGHTS: Final[dict[str, float]] = {
    "commercial": 0.6,
    "attention": 0.4,
}

# Below this many rows behind a figure, the agent reports insufficient_evidence
# rather than scoring. This is also the floor the materialized views are sized
# against -- see sql/003_materialized_views.sql.
MIN_SAMPLE_SIZE: Final[int] = 8

# Daily-views floor below which a film's interest_cohort_pct is a precise-looking
# ranking of noise. 71 of 1,238 films sit under it and 9 under 5 views/day; at
# one view a day, the percentile comes out as 0.003, which reads as a confident
# "bottom of its cohort" measurement rather than the absence of one.
#
# The raw columns keep these films -- the data is not wrong, it is just below
# resolution. What changes is that sql/001 marks them with has_interest_signal
# and sql/003 excludes them from the interest aggregates, so they are absent
# from the evidence rather than dragging a median down.
#
# Changing this means rebuilding the materialized views: the DDL carries the
# same number, and tests/test_scoring.py asserts the two still agree.
MIN_INTEREST_SIGNAL: Final[int] = 50

# --- Generation shape -------------------------------------------------------
SCENE_COUNT: Final[int] = 3
PROPOSAL_VARIANTS: Final[tuple[str, str]] = ("grounded", "wildcard")

# The wildcard branch is one extra call at high temperature, not a second
# pipeline: it asks for a combination the data argues against, as a control on
# whether the scoring only ever rewards safe choices.
WILDCARD_TEMPERATURE: Final[float] = 1.5

# --- Analogue retrieval ------------------------------------------------------
# Budget bands for finding comparable films. A proposal is an idea and has no
# budget, so one is assumed for the comparison and named in the caveats rather
# than inferred from the logline.
#
# Fixed boundaries, not data quantiles: quantiles move when the corpus changes,
# and a score whose comparison set silently shifts under it is not reproducible.
# The predicates are written out so they reach the agent's instruction as SQL,
# not as prose it has to translate.
BUDGET_BANDS: Final[dict[str, str]] = {
    "micro": "budget_usd < 5000000",
    "low":   "budget_usd >= 5000000 AND budget_usd < 20000000",
    "mid":   "budget_usd >= 20000000 AND budget_usd < 80000000",
    "high":  "budget_usd >= 80000000",
}

# --- Latency ----------------------------------------------------------------
# Three of the no-tools stages -- both Phase B branches and the storyboard --
# turn something that already exists into JSON. They are transcription, not
# reasoning, and gemini-2.5-flash thinks by default: measured over one
# 307-second run the no-tools stages spent 88 seconds between them.
#
# The analogue convergence stage was in this set and has been taken out. It
# looked like transcription and is not: it decides which numbered query
# produced each figure and which count belongs to which metric. Without
# thinking it emitted 26 evidence items citing sample_count 1 and pairing
# interest figures with ROI counts, all of which validation rejected -- a
# faster stage that scored nothing.
#
# etl/04_motif_enrichment.py already does this for the same kind of stage, and
# its comment records the finding that matters here too -- thinking is not extra
# care on the same answer, it is a different answer.
#
# Deliberately NOT applied to the two tools-on agents. Those plan SQL and decide
# what to query next from what came back, which is the part being judged; a few
# seconds is not worth trading for worse queries.
SCHEMA_STAGE_THINKING_BUDGET: Final[int] = 0

# --- Reliability ------------------------------------------------------------
# A failing query is handed back to the model with the error text verbatim. On
# the third failure the agent stops with insufficient_evidence rather than
# burning turns.
SQL_RETRY_LIMIT: Final[int] = 2

# Non-functional targets from SYSTEM_SPEC §11. Recorded, NOT enforced -- nothing
# reads either of these, and the comment here used to claim the opposite, which
# is worse than not stating it: a reader takes it as a guarantee.
#
# What actually bounds a run: Cloud Run's --timeout=900 on the request, MAX_TURNS
# and PREDICT_MAX_TURNS on the agent loops, SQL_RETRY_LIMIT on failures, and
# MEDIA_ATTEMPTS in app/media.py. Wiring these two in would mean cancelling a
# run mid-flight and unwinding the MCP subprocess cleanly; not worth the risk
# before the deadline, and the numbers are stale anyway now that a full run is
# ~168s.
QUERY_TIMEOUT_SEC: Final[int] = 30
RUN_TIMEOUT_SEC: Final[int] = 300


def composite_weights_sum_to_one() -> bool:
    return abs(sum(SCORING_WEIGHTS.values()) - 1.0) < 1e-9


assert composite_weights_sum_to_one(), (
    f"SCORING_WEIGHTS must sum to 1.0, got {sum(SCORING_WEIGHTS.values())}"
)

__all__ = [
    "SCORING_WEIGHTS", "MIN_SAMPLE_SIZE", "SCENE_COUNT", "PROPOSAL_VARIANTS",
    "MIN_INTEREST_SIGNAL", "WILDCARD_TEMPERATURE", "BUDGET_BANDS",
    "SCHEMA_STAGE_THINKING_BUDGET",
    "SQL_RETRY_LIMIT", "QUERY_TIMEOUT_SEC",
    "RUN_TIMEOUT_SEC",
]
