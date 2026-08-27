"""Tests for the parts that must not silently drift.

The composite score is the project's claim to not being a black box, and the
vocabulary is the single source four other files derive from. Both fail quietly
when broken -- a wrong composite still looks like a number, and a drifted
vocabulary still produces valid-looking tags that simply never match a row.

    ./scripts/run_etl.sh -m pytest tests/ -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app import scoring  # noqa: E402
from app.config import (  # noqa: E402
    MIN_INTEREST_SIGNAL, MIN_SAMPLE_SIZE, SCORING_WEIGHTS)  # noqa: E402
from app.contracts import Archetype, EvidenceItem, Motif  # noqa: E402
from vocab import (  # noqa: E402
    ARCHETYPES, KNOWN_VOCAB_OVERLAP, MOTIFS, release_bucket,
    years_to_measurement)


def ev(value: float, n: int = 30) -> EvidenceItem:
    return EvidenceItem(claim=f"value {value}", sql_query="SELECT 1",
                        sample_count=n, source_view="mv_test", value=value)


# --- composite is arithmetic, not assertion ---------------------------------

def test_composite_matches_weighted_sum():
    c, a, comp, _, _ = scoring.score_from_evidence([ev(2.4)], [ev(0.62)])
    expected = c * SCORING_WEIGHTS["commercial"] + a * SCORING_WEIGHTS["attention"]
    assert comp == pytest.approx(expected)


def test_composite_recomputable_from_evidence_alone():
    """A reader holding only the evidence list must reach the same number."""
    roi, interest = [ev(2.0), ev(3.0)], [ev(0.5)]
    _, _, comp, _, _ = scoring.score_from_evidence(roi, interest)

    # Redo it by hand: median of mapped ROIs, median of mapped percentiles.
    from statistics import median
    c = median(scoring.roi_to_score(e.value) for e in roi)
    a = median(scoring.cohort_pct_to_score(e.value) for e in interest)
    assert comp == pytest.approx(scoring.compute_composite(c, a))


def test_weights_sum_to_one():
    assert sum(SCORING_WEIGHTS.values()) == pytest.approx(1.0)


# --- the sample floor actually gates ----------------------------------------

def test_thin_evidence_is_discarded():
    assert scoring.usable([ev(2.0, n=MIN_SAMPLE_SIZE - 1)]) == []
    assert len(scoring.usable([ev(2.0, n=MIN_SAMPLE_SIZE)])) == 1


def test_all_thin_evidence_yields_insufficient_not_zero():
    """Nothing above the floor must not quietly become a score of 0."""
    thin = [ev(9.9, n=1)]
    c, a, comp, confidence, _ = scoring.score_from_evidence(thin, thin)
    assert confidence == "insufficient_evidence"
    assert (c, a, comp) == (None, None, None)


# --- a missing dimension is N/A, not zero -----------------------------------
# The bug this guards: attention used to come back as 0.0 when nothing was
# found, and 0.0 * 0.4 still subtracted 40 points of composite. The caveat said
# "0, not low" while the arithmetic said low.

def test_missing_attention_does_not_penalise_composite():
    c, a, comp, _, _ = scoring.score_from_evidence([ev(2.4)], [])
    assert a is None
    assert comp == pytest.approx(c), "composite must be the commercial score alone"


def test_missing_commercial_does_not_penalise_composite():
    c, a, comp, _, _ = scoring.score_from_evidence([], [ev(0.8)])
    assert c is None
    assert comp == pytest.approx(a), "composite must be the attention score alone"


def test_missing_dimension_beats_a_zero_scoring_one():
    """A film with no interest evidence must not rank below a genuinely bad one."""
    _, _, absent, _, _ = scoring.score_from_evidence([ev(2.4)], [])
    _, _, worst, _, _ = scoring.score_from_evidence([ev(2.4)], [ev(0.0)])
    assert absent > worst


def test_low_signal_absence_is_stated_in_caveats():
    _, _, _, _, caveats = scoring.score_from_evidence([ev(2.4)], [])
    joined = " ".join(caveats).lower()
    assert "n/a" in joined and "not low" in joined
    assert str(MIN_INTEREST_SIGNAL) in " ".join(caveats)


def test_single_dimension_caps_confidence_at_medium():
    """Six ROI items is 'high' normally; with no attention it must not be."""
    roi = [ev(2.0, n=30) for _ in range(6)]
    _, _, _, confidence, _ = scoring.score_from_evidence(roi, [])
    assert confidence == "medium"


def test_composite_none_only_when_both_absent():
    assert scoring.compute_composite(None, None) is None
    assert scoring.compute_composite(50.0, None) == pytest.approx(50.0)
    assert scoring.compute_composite(None, 50.0) == pytest.approx(50.0)


# --- the interest floor is one number, written in two places ----------------

def test_ddl_interest_floor_matches_config():
    """sql/001 hardcodes the threshold; a drift here silently changes scoring."""
    ddl = (ROOT / "sql" / "001_films.sql").read_text()
    match = re.search(r"has_interest_signal\s+UInt8\s+MATERIALIZED\s+"
                      r"interest_median_daily\s*>=\s*(\d+)", ddl)
    assert match, "has_interest_signal not found in sql/001_films.sql"
    assert int(match.group(1)) == MIN_INTEREST_SIGNAL


def test_outlier_roi_is_capped_not_unbounded():
    """Paranormal Activity returned 12,890x. It must not blow past the scale."""
    assert scoring.roi_to_score(12890.4) == 100.0
    assert scoring.roi_to_score(0.0) == 0.0


def test_attention_caveat_is_always_stated():
    """The measurement-lag caveat is the thing a reader most easily misreads."""
    _, _, _, _, caveats = scoring.score_from_evidence([ev(2.4)], [ev(0.6)])
    assert any("sustained" in c.lower() for c in caveats)


# --- vocabulary stays the single source -------------------------------------

def test_contract_enums_come_from_vocab():
    assert {m.value for m in Motif} == set(MOTIFS)
    assert {a.value for a in Archetype} == set(ARCHETYPES)


def test_vocab_sizes_match_spec():
    assert len(MOTIFS) == 30
    assert len(ARCHETYPES) == 25


def test_no_new_overlap_between_motifs_and_archetypes():
    """A term on both lists means the same thing twice in a joint aggregate.

    'reluctant_hero' is a known, accepted P1 -- it is a person, so it does not
    belong in MOTIFS, but removing it costs a full relabelling run. Pinning the
    set here keeps that visible while failing on any newly introduced overlap.
    """
    overlap = set(MOTIFS) & set(ARCHETYPES)
    assert overlap == set(KNOWN_VOCAB_OVERLAP), (
        f"vocabulary overlap changed: {overlap ^ set(KNOWN_VOCAB_OVERLAP)}")


@pytest.mark.parametrize("year,bucket,lag", [
    (1990, "1990-1994", 25),
    (1994, "1990-1994", 21),
    (1995, "1995-1999", 20),
    (2014, "2010-2014", 1),
])
def test_release_bucket_and_lag(year, bucket, lag):
    assert release_bucket(year) == bucket
    assert years_to_measurement(year) == lag


def test_out_of_range_year_raises():
    """A film outside 1990-2014 has no cohort to be a percentile within."""
    with pytest.raises(ValueError):
        release_bucket(2015)
    with pytest.raises(ValueError):
        release_bucket(1989)


# --- schema is read, not transcribed ----------------------------------------

def test_system_instruction_reads_the_real_ddl():
    from app.prompts import analyst_system_instruction
    si = analyst_system_instruction()
    for column in ("interest_cohort_pct", "release_bucket",
                   "years_to_measurement"):
        assert column in si, f"{column} missing from the system instruction"
    for gone in ("pageview_peak", "pageview_decay_days"):
        assert gone not in si, f"{gone} still reaching the model"
