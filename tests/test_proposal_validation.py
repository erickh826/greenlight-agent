"""Tests for grounding a Phase B proposal against a Phase A trace."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.contracts import TreatmentProposal  # noqa: E402
from app.proposal_validation import (  # noqa: E402
    parse_treatment_proposal,
    validate_grounded_proposal,
)

MOTIF_QUERY = """
SELECT motif_a, motif_b,
       countMerge(sample_count) AS n_roi,
       quantileMerge(0.5)(roi_median) AS roi_median,
       countMerge(interest_sample_count) AS n_interest,
       quantileMerge(0.5)(interest_pct_median) AS interest_median
FROM mv_motif_pair_stats
GROUP BY motif_a, motif_b
HAVING n_roi >= 8
ORDER BY roi_median DESC
LIMIT 5;
"""

ARCHETYPE_QUERY = """
SELECT archetype,
       countMerge(sample_count) AS n_roi,
       quantileMerge(0.5)(roi_median) AS roi_median,
       countMerge(interest_sample_count) AS n_interest,
       quantileMerge(0.5)(interest_pct_median) AS interest_median
FROM mv_archetype_performance
GROUP BY archetype
HAVING n_roi >= 8
ORDER BY roi_median DESC
LIMIT 5;
"""

TRACE = f"""
--- FunctionCall #1: run_query ---
{MOTIF_QUERY}
--- FunctionResponse #1 ---
{{"rows": [["hidden_conspiracy", "loss_of_innocence", 13, 5.685, 13, 0.741]]}}

--- FunctionCall #2: run_query ---
{ARCHETYPE_QUERY}
--- FunctionResponse #2 ---
{{"rows": [["authority_figure", 25, 3.510, 22, 0.418],
           ["mentor", 370, 2.644, 356, 0.564]]}}
"""


def proposal_payload(**overrides):
    payload = {
        "variant": "grounded",
        "title": "The Glass Civic",
        "logline": (
            "A mentor and an authority figure expose a hidden conspiracy "
            "before it consumes a protected witness."
        ),
        "motif_tags": ["hidden_conspiracy", "loss_of_innocence"],
        "character_archetypes": ["mentor", "authority_figure"],
        "act_structure": "classic_three_act",
        "rationale": (
            "`hidden_conspiracy` plus `loss_of_innocence` had the strongest "
            "motif-pair ROI, while `authority_figure` and `mentor` were both "
            "supported broad archetypes."
        ),
        "evidence": [
            {
                "claim": (
                    "`hidden_conspiracy` with `loss_of_innocence` reached "
                    "5.685 median ROI."
                ),
                "sql_query": MOTIF_QUERY,
                "sample_count": 13,
                "source_view": "mv_motif_pair_stats",
                "value": 5.685,
            },
            {
                "claim": "`authority_figure` reached 3.510 median ROI.",
                "sql_query": ARCHETYPE_QUERY,
                "sample_count": 25,
                "source_view": "mv_archetype_performance",
                "value": 3.510,
            },
        ],
    }
    payload.update(overrides)
    return payload


def build_proposal(**overrides) -> TreatmentProposal:
    return TreatmentProposal.model_validate(proposal_payload(**overrides))


def test_fenced_model_json_parses_as_treatment_proposal():
    raw = "```json\n" + json.dumps(proposal_payload()) + "\n```"
    parsed = parse_treatment_proposal(raw)
    assert parsed.variant == "grounded"
    assert parsed.title == "The Glass Civic"


def test_grounded_proposal_passes_validation():
    assert validate_grounded_proposal(build_proposal(), TRACE) == []


def test_evidence_query_must_come_from_phase_a_trace():
    evidence = proposal_payload()["evidence"]
    evidence[0] = {
        **evidence[0],
        "sql_query": MOTIF_QUERY.replace("LIMIT 5", "LIMIT 10"),
    }
    errors = validate_grounded_proposal(build_proposal(evidence=evidence), TRACE)
    assert any("not copied from Phase A" in err for err in errors)


def test_cited_vocab_must_appear_in_phase_a_trace():
    proposal = build_proposal(
        motif_tags=["impossible_heist", "loss_of_innocence"])
    errors = validate_grounded_proposal(proposal, TRACE)
    assert any("impossible_heist" in err for err in errors)


def test_evidence_under_sample_floor_fails_validation():
    evidence = proposal_payload()["evidence"]
    evidence[0] = {**evidence[0], "sample_count": 7}
    errors = validate_grounded_proposal(build_proposal(evidence=evidence), TRACE)
    assert any("below 8" in err for err in errors)
