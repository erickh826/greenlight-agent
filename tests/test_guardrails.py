"""Tests for the query guardrails.

Both directions matter. A rule that never fires is useless, and a rule that
fires on the good query the prompt asks for is worse than useless -- it would
fail Phase A on correct behaviour, and the fix would be to delete the rule.
So each rule is tested against the violation it targets AND against the query
app/prompts.py holds up as the right one.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app.guardrails import (  # noqa: E402
    SEVERITY_VIOLATION, SEVERITY_WARNING, inspect, is_error_response,
    unsupported_terms, violations)


def rules(sql: str) -> set[str]:
    return {f.rule for f in inspect(sql)}


# --- the queries the prompt recommends must stay clean ----------------------

GOOD_ARCHETYPE = """
    SELECT archetype,
           countMerge(sample_count)                AS n_roi,
           countMerge(interest_sample_count)       AS n_interest,
           quantileMerge(0.5)(roi_median)          AS roi_median,
           quantileMerge(0.5)(interest_pct_median) AS interest_median
    FROM mv_archetype_performance
    GROUP BY archetype
    HAVING n_roi >= 8
"""

GOOD_PAIR = """
    SELECT motif_a, motif_b,
           countMerge(sample_count)       AS n,
           quantileMerge(0.5)(roi_median) AS roi_median
    FROM mv_motif_pair_stats
    GROUP BY motif_a, motif_b
    HAVING n >= 8
    ORDER BY roi_median DESC
"""

GOOD_NARROW = """
    SELECT count() AS n, quantile(0.5)(roi) AS roi_median
    FROM films
    WHERE has(character_archetypes, 'antihero')
      AND has(character_archetypes, 'mentor')
      AND roi IS NOT NULL
"""


def test_recommended_queries_produce_no_findings():
    for sql in (GOOD_ARCHETYPE, GOOD_PAIR, GOOD_NARROW):
        assert inspect(sql) == [], f"false positive on:\n{sql}"


# --- each rule fires on what it targets -------------------------------------

def test_write_attempt_is_a_violation():
    assert "write_attempt" in rules("DROP TABLE films")
    assert "write_attempt" in rules("  insert into films values (1)")


def test_select_named_like_a_verb_is_not_a_write():
    """`updated_at` or a CTE must not trip the write rule."""
    assert "write_attempt" not in rules(
        "SELECT count() FROM films WHERE ingested_at > now() - 1")


def test_removed_columns_are_caught():
    assert "removed_name" in rules("SELECT pageview_peak FROM films")
    assert "removed_name" in rules("SELECT * FROM mv_attention_curve")


def test_unbounded_attention_scan_is_a_violation():
    assert "unbounded_attention_scan" in rules(
        "SELECT film_id, sum(views) FROM film_attention GROUP BY film_id")


def test_attention_scan_pinned_to_a_film_is_allowed():
    assert "unbounded_attention_scan" not in rules(
        "SELECT date, views FROM film_attention WHERE film_id = 'Q25188'")
    assert "unbounded_attention_scan" not in rules(
        "SELECT a.views FROM film_attention a "
        "JOIN films f ON f.film_id = a.film_id WHERE f.release_year = 2010")


def test_interest_figure_without_its_own_count():
    assert "interest_without_its_count" in rules("""
        SELECT archetype, countMerge(sample_count) AS n,
               quantileMerge(0.5)(interest_pct_median) AS interest_median
        FROM mv_archetype_performance GROUP BY archetype
    """)


def test_nested_merge_is_caught():
    assert "nested_merge" in rules(
        "SELECT sum(countMerge(sample_count)) FROM mv_archetype_performance")


def test_raw_aggregate_state_is_caught():
    assert "raw_state_selected" in rules(
        "SELECT archetype, sample_count FROM mv_archetype_performance")


# --- warnings are warnings, not failures ------------------------------------

def test_narrowing_without_a_floor_is_only_a_warning():
    found = inspect("""
        SELECT archetype, countMerge(sample_count) AS n
        FROM mv_archetype_performance
        WHERE release_bucket = '2010-2014'
        GROUP BY archetype
    """)
    assert {f.rule for f in found} == {"narrow_without_floor"}
    assert violations(found) == []
    assert found[0].severity == SEVERITY_WARNING


def test_raw_interest_comparison_is_only_a_warning():
    found = inspect(
        "SELECT title FROM films ORDER BY interest_median_daily DESC LIMIT 5")
    assert {f.rule for f in found} == {"raw_interest_comparison"}
    assert violations(found) == []


def test_cohort_pct_alongside_raw_is_fine():
    assert inspect(
        "SELECT title, interest_cohort_pct FROM films "
        "ORDER BY interest_median_daily DESC LIMIT 5") == []


# --- comment stripping, since the model writes commented SQL ----------------

def test_a_removed_name_inside_a_comment_does_not_fire():
    assert "removed_name" not in rules(
        "-- pageview_peak was removed\nSELECT count() FROM films")


def test_violations_filters_by_severity():
    found = inspect("DROP TABLE films")
    assert all(f.severity == SEVERITY_VIOLATION for f in violations(found))


# --- error detection in tool responses --------------------------------------
# The first version of this check matched neither payload, so the Phase A trace
# reported zero errors whatever happened.

SUCCESS = ('{"content": [{"type": "text", "text": "{\\"rows\\": [[1]]}"}], '
           '"isError": false}')
FAILURE = ('{"content": [{"type": "text", "text": "Code: 47. DB::Exception: '
           'Unknown identifier pageview_peak"}], "isError": true}')


def test_success_payload_is_not_an_error():
    assert is_error_response(SUCCESS) is False


def test_failure_payload_is_an_error():
    assert is_error_response(FAILURE) is True


def test_clickhouse_error_text_without_the_flag_is_caught():
    assert is_error_response(
        '{"content": [{"text": "Code: 60. DB::Exception: Table not found"}]}'
    ) is True


# --- claims must trace back to a query --------------------------------------
# From the first Phase A run: the agent wrote that impossible_heist with
# redemption "showed a very high ROI in initial broad searches" and fell under
# the sample floor. Neither term appears in any query it ran or any result it
# got back. Plausible, hedged, and invented.

def test_term_never_queried_is_unsupported():
    synthesis = ("The combination of `impossible_heist` and `redemption` "
                 "showed a very high ROI in initial broad searches.")
    evidence = "SELECT motif_a FROM mv_motif_pair_stats WHERE motif_a='revenge'"
    assert unsupported_terms(synthesis, evidence) == ["impossible_heist",
                                                      "redemption"]  # both cited


def test_term_present_in_a_result_is_supported():
    synthesis = "The pair `rise_and_fall` and `sacrifice_for_others` looks good."
    evidence = ('{"rows": [["rise_and_fall", "sacrifice_for_others", 9, 3.58]]}')
    assert unsupported_terms(synthesis, evidence) == []


def test_term_present_only_in_the_query_is_supported():
    """Querying it and getting nothing back is still evidence of having looked."""
    synthesis = "I checked `impossible_heist` and it was too thin."
    evidence = "SELECT * FROM mv_motif_pair_stats WHERE motif_a='impossible_heist'"
    assert unsupported_terms(synthesis, evidence) == []


def test_bare_english_words_are_not_treated_as_citations():
    """Half the vocabulary is also ordinary English.

    "a story of redemption" is prose, not a claim about the `redemption` tag,
    and flagging it would mean flagging correct writing. A bare single word
    counts only when marked as a citation; an underscored token always does.
    """
    assert unsupported_terms(
        "The film is about revenge and survival in a broad sense.", "") == []
    assert unsupported_terms("A story of loss and redemption arcs.", "") == []
    assert unsupported_terms("Films tagged `redemption` returned 2.4x.", "") == [
        "redemption"]
    assert unsupported_terms("The rise_and_fall shape did well.", "") == [
        "rise_and_fall"]
