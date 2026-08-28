"""Tests for analogue evidence validation and programmatic scoring.

The claim being defended is narrow and checkable: every number in a
PredictionScore came out of a query this run actually made, and the composite is
arithmetic over those numbers rather than something the model asserted. Each
test below is one way that claim has a hole in it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app import scoring  # noqa: E402
from app.analogue_scoring import (  # noqa: E402
    comparison_caveats, insufficient_evidence, partition, resolve_bundle,
    result_numbers, score_bundle, seen, seen_exactly,
    validate_analogue_evidence)
from app.config import BUDGET_BANDS, MIN_SAMPLE_SIZE  # noqa: E402
from app.contracts import (  # noqa: E402
    AnalogueEvidence, AnalogueEvidenceBundle, AnalogueEvidenceDraft,
    PredictionScore)

ROI_SQL = (
    "SELECT archetype, countMerge(sample_count) AS n_roi, "
    "quantileMerge(0.5)(roi_median) AS roi_median, "
    "countMerge(interest_sample_count) AS n_interest, "
    "quantileMerge(0.5)(interest_pct_median) AS interest_median "
    "FROM mv_archetype_performance GROUP BY archetype HAVING n_roi >= 8"
)

# The same shape without the interest count: legal SQL, and the one an interest
# figure must not be cited against.
ROI_ONLY_SQL = (
    "SELECT archetype, countMerge(sample_count) AS n_roi, "
    "quantileMerge(0.5)(roi_median) AS roi_median "
    "FROM mv_archetype_performance GROUP BY archetype HAVING n_roi >= 8"
)

PAYLOAD = """{
  "content": [{"text": "archetype,n_roi,roi_median,n_interest,interest_median
authority_figure,25,3.5104405879974365,22,0.4175257682800293
caregiver,151,2.9679999351501465,142,0.4530743509531021"}],
  "isError": false
}"""


def ev(metric: str, value: float, n: int, sql: str = ROI_SQL,
       view: str = "mv_archetype_performance") -> AnalogueEvidence:
    return AnalogueEvidence(claim=f"{metric} figure {value}", sql_query=sql,
                            sample_count=n, source_view=view, value=value,
                            metric=metric)


def validate(items, sql=ROI_SQL, payload=PAYLOAD):
    return validate_analogue_evidence(items, [sql], [payload])


# --- the numbers have to have come back -------------------------------------

def test_evidence_matching_the_result_validates():
    items = [ev("commercial", 3.5104405879974365, 25),
             ev("attention", 0.4175257682800293, 22)]
    assert validate(items) == []


def test_rounded_value_still_validates():
    """Copying 3.51 for 3.5104405879974365 is a correct citation."""
    assert validate([ev("commercial", 3.51, 25)]) == []


def test_invented_value_fails():
    errors = validate([ev("commercial", 7.4, 25)])
    assert any("appears in no result" in e for e in errors)


def test_invented_sample_count_fails():
    errors = validate([ev("commercial", 3.5104405879974365, 47)])
    assert any("sample_count 47" in e for e in errors)


def test_sample_count_gets_no_tolerance():
    """25 and 26 are different facts, however close the floats are."""
    errors = validate([ev("commercial", 3.5104405879974365, 26)])
    assert any("sample_count 26" in e for e in errors)


def test_sql_not_run_this_session_fails():
    stray = "SELECT count() FROM films WHERE has(motif_tags, 'revenge')"
    errors = validate([ev("commercial", 3.51, 25, sql=stray,
                          view="films")])
    assert any("was not run this session" in e for e in errors)


def test_source_view_must_be_read_by_the_query():
    errors = validate([ev("commercial", 3.51, 25, view="mv_motif_pair_stats")])
    assert any("is not read by" in e for e in errors)


def test_guarded_sql_is_rejected_even_when_it_returned_rows():
    scan = "SELECT avg(views) FROM film_attention"
    errors = validate_analogue_evidence(
        [ev("commercial", 3.51, 25, sql=scan, view="film_attention")],
        [scan], [PAYLOAD])
    assert any("unbounded_attention_scan" in e for e in errors)


# --- the two counts are not interchangeable ---------------------------------

def test_interest_evidence_cited_against_a_roi_only_query_fails():
    errors = validate_analogue_evidence(
        [ev("attention", 0.4175257682800293, 25, sql=ROI_ONLY_SQL)],
        [ROI_ONLY_SQL], [PAYLOAD])
    assert any("interest_sample_count" in e for e in errors)


def test_interest_evidence_from_films_may_use_countif():
    sql = ("SELECT count() AS n_roi, quantile(0.5)(roi) AS roi_median, "
           "countIf(has_interest_signal) AS n_interest, "
           "quantileIf(0.5)(interest_cohort_pct, has_interest_signal) "
           "AS interest_median FROM films WHERE roi IS NOT NULL")
    payload = '{"text": "34,2.1,29,0.51", "isError": false}'
    items = [ev("attention", 0.51, 29, sql=sql, view="films")]
    assert validate_analogue_evidence(items, [sql], [payload]) == []


def test_attention_value_outside_zero_to_one_fails():
    """41.75 is a percentage; interest_cohort_pct is a percentile."""
    errors = validate_analogue_evidence(
        [ev("attention", 41.75, 22)], [ROI_SQL],
        [PAYLOAD + '\n{"v": 41.75}'])
    assert any("outside 0-1" in e for e in errors)


def test_commercial_evidence_needs_a_roi_column():
    sql = ("SELECT film_id, countIf(has_interest_signal) AS n_interest, "
           "quantileIf(0.5)(interest_cohort_pct, has_interest_signal) AS m "
           "FROM films GROUP BY film_id")
    errors = validate_analogue_evidence(
        [ev("commercial", 0.45, 142, sql=sql, view="films")], [sql], [PAYLOAD])
    assert any("reads no roi column" in e for e in errors)


def test_empty_bundle_fails():
    assert validate_analogue_evidence([], [ROI_SQL], [PAYLOAD])


# --- scoring is arithmetic over what survived -------------------------------

def draft(metric: str, value: float, n: int,
          query_index: int = 1) -> AnalogueEvidenceDraft:
    return AnalogueEvidenceDraft(claim=f"{metric} figure {value}",
                                 query_index=query_index, sample_count=n,
                                 value=value, metric=metric)


def bundle(drafts, caveats=()) -> AnalogueEvidenceBundle:
    return AnalogueEvidenceBundle(proposal_title="The Unveiling",
                                  evidence=list(drafts),
                                  caveats=list(caveats))


def scored(drafts, queries=(ROI_SQL,), caveats=(), extra=()):
    """resolve -> score, the order the pipeline uses."""
    b = bundle(drafts, caveats)
    evidence, errors = resolve_bundle(b, list(queries))
    assert errors == []
    return score_bundle(b, evidence, extra_caveats=extra)


# --- citation by index, not by transcription --------------------------------

def test_resolve_attaches_the_sql_that_was_actually_run():
    """The model never types the SQL, so it cannot paraphrase it.

    This is the failure that motivated query_index: a convergence pass copied a
    real query but wrapped one WHERE clause in its own parentheses, and the
    grounding check rejected a figure standing on 133 films.
    """
    evidence, errors = resolve_bundle(
        bundle([draft("commercial", 3.51, 25, query_index=2)]),
        ["SELECT 1", ROI_SQL])
    assert errors == []
    assert evidence[0].sql_query == ROI_SQL
    assert evidence[0].source_view == "mv_archetype_performance"


def test_source_view_is_derived_not_asked_for():
    films_sql = ("SELECT count() AS n_roi, quantile(0.5)(roi) AS roi_median "
                 "FROM films WHERE roi IS NOT NULL")
    evidence, _ = resolve_bundle(bundle([draft("commercial", 2.4, 30)]),
                                 [films_sql])
    assert evidence[0].source_view == "films"


def test_index_outside_the_transcript_is_a_failed_citation():
    evidence, errors = resolve_bundle(
        bundle([draft("commercial", 3.51, 25, query_index=7)]), [ROI_SQL])
    assert evidence == []
    assert any("QUERY 7" in e for e in errors)


def test_composite_recomputes_from_the_evidence_it_lists():
    score = scored([draft("commercial", 2.4, 30), draft("attention", 0.62, 30)])
    roi, interest = partition(score.evidence)
    expected = scoring.score_from_evidence(roi, interest)[:3]
    assert (score.commercial_score, score.attention_score,
            score.composite) == expected


def test_below_floor_evidence_is_excluded_not_rejected():
    score = scored([draft("commercial", 2.4, 30),
                    draft("attention", 0.9, MIN_SAMPLE_SIZE - 1)])
    assert score.attention_score is None
    assert len(score.evidence) == 2        # the thin one is still shown
    assert any("discarded" in c for c in score.caveats)


def test_missing_attention_is_none_not_zero():
    score = scored([draft("commercial", 2.4, 30)])
    assert score.attention_score is None
    assert score.composite == scoring.roi_to_score(2.4)
    assert score.confidence != "high"


def test_no_usable_evidence_is_insufficient_evidence():
    score = scored([draft("commercial", 2.4, 1)])
    assert score.confidence == "insufficient_evidence"
    assert (score.commercial_score, score.attention_score,
            score.composite) == (None, None, None)


def test_model_caveats_are_appended_not_substituted():
    score = scored([draft("commercial", 2.4, 30)],
                   caveats=["Analogues skew pre-2000."],
                   extra=["Budget band: mid."])
    assert score.caveats[0] == "Budget band: mid."
    assert "Analogues skew pre-2000." in score.caveats
    assert any("N/A" in c for c in score.caveats)


def test_insufficient_evidence_keeps_the_evidence_it_saw():
    thin = [ev("commercial", 2.4, 2)]
    score = insufficient_evidence("The Unveiling", "retry limit reached", thin)
    assert score.confidence == "insufficient_evidence"
    assert score.composite is None
    assert score.evidence == thin


def test_comparison_caveats_state_the_assumed_band():
    caveats = comparison_caveats("mid", None)
    assert any("mid budget band" in c for c in caveats)
    assert any("1990-2014" in c for c in caveats)


def test_metric_survives_a_prediction_score_round_trip():
    """The published JSON must be enough to recompute the composite from.

    PredictionScore.evidence was list[EvidenceItem] first. Pydantic serialises
    by the declared type, so `metric` was dropped on the way out: the score file
    listed twelve evidence items with no way to tell which fed which dimension,
    and the composite could not be reproduced from the artefact that exists to
    make it reproducible. The in-memory check passed the whole time.
    """
    score = scored([draft("commercial", 2.4, 30),
                    draft("attention", 0.62, 30)])
    reloaded = PredictionScore.model_validate_json(score.model_dump_json())

    assert [e.metric for e in reloaded.evidence] == ["commercial", "attention"]
    roi, interest = partition(reloaded.evidence)
    assert scoring.score_from_evidence(roi, interest)[:3] == (
        reloaded.commercial_score, reloaded.attention_score,
        reloaded.composite)


# --- helpers ----------------------------------------------------------------

def test_result_numbers_reads_figures_out_of_a_payload():
    numbers = result_numbers([PAYLOAD])
    assert 3.5104405879974365 in numbers
    assert 151 in numbers


def test_seen_and_seen_exactly_differ_on_integers():
    assert seen(3.51, [3.5104405879974365])
    assert not seen_exactly(26, [25.0, 151.0])
    assert seen_exactly(25, [25.0, 151.0])


def test_budget_bands_partition_the_range_without_gaps():
    """Every band names budget_usd, and the boundaries line up."""
    assert set(BUDGET_BANDS) == {"micro", "low", "mid", "high"}
    for predicate in BUDGET_BANDS.values():
        assert "budget_usd" in predicate
    for edge in ("5000000", "20000000", "80000000"):
        assert sum(edge in p for p in BUDGET_BANDS.values()) == 2
