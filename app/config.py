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

# --- Generation shape -------------------------------------------------------
SCENE_COUNT: Final[int] = 3
PROPOSAL_VARIANTS: Final[tuple[str, str]] = ("grounded", "wildcard")

# The wildcard branch is one extra call at high temperature, not a second
# pipeline: it asks for a combination the data argues against, as a control on
# whether the scoring only ever rewards safe choices.
WILDCARD_TEMPERATURE: Final[float] = 1.5

# --- Reliability ------------------------------------------------------------
# A failing query is handed back to the model with the error text verbatim. On
# the third failure the agent stops with insufficient_evidence rather than
# burning turns.
SQL_RETRY_LIMIT: Final[int] = 2

# Non-functional targets from SYSTEM_SPEC §11, enforced rather than aspirational.
QUERY_TIMEOUT_SEC: Final[int] = 30
RUN_TIMEOUT_SEC: Final[int] = 300


def composite_weights_sum_to_one() -> bool:
    return abs(sum(SCORING_WEIGHTS.values()) - 1.0) < 1e-9


assert composite_weights_sum_to_one(), (
    f"SCORING_WEIGHTS must sum to 1.0, got {sum(SCORING_WEIGHTS.values())}"
)

__all__ = [
    "SCORING_WEIGHTS", "MIN_SAMPLE_SIZE", "SCENE_COUNT", "PROPOSAL_VARIANTS",
    "WILDCARD_TEMPERATURE", "SQL_RETRY_LIMIT", "QUERY_TIMEOUT_SEC",
    "RUN_TIMEOUT_SEC",
]
