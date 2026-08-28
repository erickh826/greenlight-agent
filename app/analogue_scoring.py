"""Turn retrieved analogue evidence into a PredictionScore, and check it first.

Two jobs, and the order matters. Validation asks whether each figure was really
returned by a query the agent ran this session. Scoring then combines only what
survived, using app/scoring.py -- so the number at the end is arithmetic over
rows that exist, not a summary of what the model believed.

The check that does the most work here is the numeric one. Every value and every
sample_count in the bundle must appear in a result payload from this run.
Grounding SQL alone is not enough: an agent can copy a real query verbatim and
attach a figure the query never returned, and that reads exactly like a correct
citation. Comparing the numbers themselves closes it.

What this cannot check: whether the number came from the row the claim says it
did. A median from a different row of the same result set would pass. That would
be a mis-citation rather than an invention, and catching it would mean parsing
every result shape mcp-clickhouse can return -- worth doing if it ever shows up,
not worth guessing at now.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.config import MIN_SAMPLE_SIZE
from app.contracts import (
    AnalogueEvidence, AnalogueEvidenceBundle, PredictionScore)
from app.guardrails import inspect, violations
from app.proposal_validation import (
    canonical_sql, primary_source, source_names)
from app.scoring import score_from_evidence

NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

# How close a cited figure must be to one that came back. Not exact equality:
# the instruction says copy the value verbatim, but a model that writes 5.685
# for 5.6850433349609375 has cited the right number, and failing that would
# train the next fix in the wrong direction. At this tolerance two genuinely
# different medians would have to agree to four significant figures to be
# confused for each other.
VALUE_TOLERANCE = 1e-3

# Markers that a query computes its interest figure over the measurement-floor
# subset, and therefore carries its own smaller row count.
INTEREST_COUNT_MARKERS = ("interest_sample_count", "countif(has_interest_signal)",
                          "count_if(has_interest_signal)")
INTEREST_VALUE_MARKERS = ("interest_pct_median", "interest_cohort_pct")
ROI_VALUE_MARKERS = ("roi",)


def result_numbers(payloads: Iterable[str]) -> list[float]:
    """Every number that came back from the database this session."""
    found: list[float] = []
    for payload in payloads:
        for token in NUMBER.findall(payload):
            try:
                found.append(float(token))
            except ValueError:  # pragma: no cover -- regex already constrains
                continue
    return found


def seen(value: float, numbers: Iterable[float],
         tolerance: float = VALUE_TOLERANCE) -> bool:
    """Whether a figure matches one the database returned, within tolerance."""
    allowed = max(tolerance, abs(value) * tolerance)
    return any(abs(candidate - value) <= allowed for candidate in numbers)


def seen_exactly(value: int, numbers: Iterable[float]) -> bool:
    """Whether an integer came back exactly.

    Sample counts get no tolerance. A count is a number of rows, so 13 and 14
    are different facts, and the relative tolerance that makes sense for a
    median would let a two-digit count drift by six.
    """
    return any(candidate == value for candidate in numbers)


def resolve_bundle(bundle: AnalogueEvidenceBundle,
                   queries: list[str]) -> tuple[list[AnalogueEvidence],
                                                list[str]]:
    """Attach the real SQL to each cited figure. (evidence, errors).

    The model cites a query by its number in the transcript; the text comes from
    `queries`, which is what the agent actually sent. So sql_query is verbatim
    by construction rather than by instruction, and source_view is derived from
    that SQL instead of being a second thing the model could get wrong.

    An index outside the transcript is the one failure left, and it is an error
    rather than a silent drop: a figure attributed to a query that does not
    exist is exactly the citation this pipeline refuses to publish.
    """
    evidence: list[AnalogueEvidence] = []
    errors: list[str] = []

    for position, draft in enumerate(bundle.evidence, start=1):
        if not 1 <= draft.query_index <= len(queries):
            errors.append(
                f"evidence[{position}] cites QUERY {draft.query_index}, but "
                f"this run made {len(queries)} successful queries")
            continue
        sql = queries[draft.query_index - 1]
        evidence.append(AnalogueEvidence(
            claim=draft.claim,
            sql_query=sql,
            sample_count=draft.sample_count,
            source_view=primary_source(sql),
            value=draft.value,
            metric=draft.metric,
        ))
    return evidence, errors


def validate_analogue_evidence(
    evidence: list[AnalogueEvidence],
    queries: Iterable[str],
    response_payloads: Iterable[str],
) -> list[str]:
    """Errors that make an evidence item unusable. Empty means it checks out.

    An item below the sample floor is not an error -- app/scoring.py drops it
    and says so in the caveats, which is the honest handling. An item whose
    figure nobody retrieved is a different thing entirely, and it fails.
    """
    errors: list[str] = []
    ran = canonical_sql("\n".join(queries))
    numbers = result_numbers(response_payloads)

    if not evidence:
        return ["bundle contains no evidence items"]

    for index, item in enumerate(evidence, start=1):
        prefix = f"evidence[{index}] ({item.metric})"
        sql = item.sql_query.strip()
        if not sql:
            errors.append(f"{prefix} has an empty sql_query")
            continue

        if canonical_sql(sql) not in ran:
            errors.append(f"{prefix} cites SQL that was not run this session")

        source = item.source_view.strip().split(".")[-1].lower()
        if source and source not in source_names(sql):
            errors.append(
                f"{prefix} source_view {item.source_view!r} is not read by "
                "sql_query")

        bad = violations(inspect(sql))
        if bad:
            errors.append(f"{prefix} cites guarded SQL: "
                          + ", ".join(f.rule for f in bad))

        low = canonical_sql(sql).replace(" ", "")
        if item.metric == "attention":
            if not any(m in low for m in INTEREST_COUNT_MARKERS):
                errors.append(
                    f"{prefix} is an interest figure but its query never "
                    "computes interest_sample_count / "
                    "countIf(has_interest_signal); a count taken from that "
                    "query is the ROI row count and overstates the evidence")
            if not any(m in low for m in INTEREST_VALUE_MARKERS):
                errors.append(
                    f"{prefix} is labelled attention but its query reads no "
                    "interest column")
            if not 0.0 <= item.value <= 1.0:
                errors.append(
                    f"{prefix} value {item.value} is outside 0-1; "
                    "interest_cohort_pct is a percentile, not a percentage")
        else:
            if not any(m in low for m in ROI_VALUE_MARKERS):
                errors.append(
                    f"{prefix} is labelled commercial but its query reads no "
                    "roi column")
            if item.value < 0:
                errors.append(f"{prefix} value {item.value} is a negative ROI")

        if not seen(item.value, numbers):
            errors.append(
                f"{prefix} value {item.value} appears in no result returned "
                "this session")
        if not seen_exactly(item.sample_count, numbers):
            errors.append(
                f"{prefix} sample_count {item.sample_count} appears in no "
                "result returned this session")

    return errors


def partition(evidence: list[AnalogueEvidence]
              ) -> tuple[list[AnalogueEvidence], list[AnalogueEvidence]]:
    """(commercial, attention), in the order app/scoring.py expects them."""
    return ([e for e in evidence if e.metric == "commercial"],
            [e for e in evidence if e.metric == "attention"])


def score_bundle(bundle: AnalogueEvidenceBundle,
                 evidence: list[AnalogueEvidence],
                 extra_caveats: Iterable[str] = ()) -> PredictionScore:
    """The only place a PredictionScore is built.

    Takes the resolved evidence rather than reading it off the bundle: what the
    bundle holds is the model's citations, and what gets scored is those
    citations after resolve_bundle has attached the SQL that was really run.

    Whatever the model said about how well this proposal would do is not an
    input here. The inputs are the evidence list and SCORING_WEIGHTS.
    """
    roi_items, interest_items = partition(evidence)
    commercial, attention, composite, confidence, caveats = score_from_evidence(
        list(roi_items), list(interest_items))

    return PredictionScore(
        proposal_title=bundle.proposal_title,
        commercial_score=commercial,
        attention_score=attention,
        composite=composite,
        confidence=confidence,
        evidence=list(evidence),
        caveats=[*extra_caveats, *caveats, *bundle.caveats],
    )


def insufficient_evidence(proposal_title: str,
                          reason: str,
                          evidence: list[AnalogueEvidence] | None = None,
                          extra_caveats: Iterable[str] = ()) -> PredictionScore:
    """The output when the query path could not produce a comparable set.

    All three scores are None rather than 0.0, for the reason spelled out in
    app/scoring.compute_composite: zero is a position on the scale, and using it
    for "we found nothing" charges the proposal for the absence.
    """
    return PredictionScore(
        proposal_title=proposal_title,
        commercial_score=None,
        attention_score=None,
        composite=None,
        confidence="insufficient_evidence",
        evidence=list(evidence or []),
        caveats=[*extra_caveats, reason],
    )


def comparison_caveats(budget_band: str | None,
                       release_bucket: str | None) -> list[str]:
    """What the caller chose, stated so the score is read against it."""
    caveats = []
    if budget_band:
        caveats.append(
            f"Analogues were drawn from the {budget_band} budget band. The "
            "proposal has no budget of its own; this band was assumed for the "
            "comparison.")
    if release_bucket:
        caveats.append(
            f"Analogues were narrowed to films released {release_bucket}.")
    else:
        caveats.append(
            "Analogues span 1990-2014 with no era filter. The dataset ends at "
            "2014 because the plot corpus does, not because later films were "
            "excluded.")
    return caveats


__all__ = [
    "result_numbers", "seen", "seen_exactly", "resolve_bundle",
    "validate_analogue_evidence", "partition",
    "score_bundle", "insufficient_evidence", "comparison_caveats",
    "VALUE_TOLERANCE", "MIN_SAMPLE_SIZE",
]
