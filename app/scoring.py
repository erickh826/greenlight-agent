"""Composite scoring, as arithmetic over evidence.

The claim this project makes is that a score is not a black box: every figure
carries the query that produced it, and the composite is those figures combined
by a stated rule. That claim holds only if the composite is actually computed
here rather than asserted by the model, so the agent's number is discarded and
this runs in its place.

Pure functions, no I/O, no model calls -- which is also what makes the
"recompute and compare" test in the verification steps possible.
"""

from __future__ import annotations

from statistics import median

from app.config import MIN_INTEREST_SIGNAL, MIN_SAMPLE_SIZE, SCORING_WEIGHTS
from app.contracts import EvidenceItem

# A film that made back its budget sits at ROI 1.0. The measured median across
# the dataset is 2.4, and the p75 is 3.66; anchoring the top of the scale at 5x
# keeps the interesting range spread out instead of compressing everything into
# the bottom decile because one film returned 12,890x.
ROI_SCALE_CEILING = 5.0


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def roi_to_score(roi: float) -> float:
    """Map an ROI onto 0-100, linear up to ROI_SCALE_CEILING then flat."""
    return _clamp(roi / ROI_SCALE_CEILING * 100.0)


def cohort_pct_to_score(pct: float) -> float:
    """interest_cohort_pct is already 0-1 within a release cohort."""
    return _clamp(pct * 100.0)


def usable(evidence: list[EvidenceItem]) -> list[EvidenceItem]:
    """Evidence thin enough to be noise is not evidence."""
    return [e for e in evidence if e.sample_count >= MIN_SAMPLE_SIZE]


def compute_composite(commercial: float | None,
                      attention: float | None) -> float | None:
    """Weighted blend over the dimensions that have evidence.

    A missing dimension is dropped and the remaining weights renormalised, not
    passed in as zero. Zero is a position on the scale -- it says the
    comparables performed as badly as anything in the dataset -- and the earlier
    version used it for "we found nothing", then attached a caveat saying the
    score was "0, not low" while the arithmetic went on treating it as low. A
    film with no usable interest comparables was losing 40 points of composite
    for the absence.

    Returns None when neither dimension has evidence; there is no number to
    give, and 0.0 would be the same lie one level up.
    """
    present = {name: score for name, score
               in (("commercial", commercial), ("attention", attention))
               if score is not None}
    if not present:
        return None

    total_weight = sum(SCORING_WEIGHTS[name] for name in present)
    return _clamp(
        sum(score * SCORING_WEIGHTS[name] for name, score in present.items())
        / total_weight
    )


def score_from_evidence(
    roi_evidence: list[EvidenceItem],
    interest_evidence: list[EvidenceItem],
) -> tuple[float | None, float | None, float | None, str, list[str]]:
    """Derive both sub-scores and the composite from evidence alone.

    Returns (commercial, attention, composite, confidence, caveats). Any of the
    three is None when nothing backed it -- N/A, not zero. See compute_composite.

    Confidence tracks how much survived the sample floor, not how sure the model
    sounded. With nothing left on either side the caller must emit
    insufficient_evidence rather than a number, because a score with no rows
    behind it is exactly the black box this design exists to avoid.

    One limit worth stating plainly: the low-signal exclusion (films under
    MIN_INTEREST_SIGNAL daily views, whose cohort percentile is a precise-looking
    ranking of noise) happens in SQL, via the has_interest_signal column that
    sql/003 filters the interest aggregates on. This function sees only
    sample_count, so it cannot verify the agent used interest_sample_count
    rather than the ROI row count. app/prompts.py instructs it to; that
    instruction is the enforcement.
    """
    caveats: list[str] = []

    roi_ok = usable(roi_evidence)
    interest_ok = usable(interest_evidence)

    dropped = (len(roi_evidence) - len(roi_ok)) + \
              (len(interest_evidence) - len(interest_ok))
    if dropped:
        caveats.append(
            f"{dropped} evidence item(s) discarded for fewer than "
            f"{MIN_SAMPLE_SIZE} samples"
        )

    if not roi_ok and not interest_ok:
        return None, None, None, "insufficient_evidence", caveats + [
            "No comparable set met the sample floor; no score was computed."
        ]

    commercial = median(roi_to_score(e.value) for e in roi_ok) if roi_ok else None
    attention = (median(cohort_pct_to_score(e.value) for e in interest_ok)
                 if interest_ok else None)

    if commercial is None:
        caveats.append(
            f"No ROI comparables met the {MIN_SAMPLE_SIZE}-sample floor. "
            "Commercial score is N/A and the composite is the attention score "
            "alone -- it is not a low commercial result."
        )
    if attention is None:
        caveats.append(
            f"No interest comparables met the {MIN_SAMPLE_SIZE}-sample floor, "
            f"after films under {MIN_INTEREST_SIGNAL} daily views were excluded "
            "as below the measurement floor. Attention score is N/A and the "
            "composite is the commercial score alone -- it is not low interest."
        )
    else:
        # Worth restating on every score: this is the one thing about the
        # attention figure a reader is most likely to misread.
        caveats.append(
            "Attention reflects sustained Wikipedia lookups from 2015 onward, "
            "measured 1-25 years after release. It is not opening-weekend "
            "attention."
        )

    total = len(roi_ok) + len(interest_ok)
    confidence = "high" if total >= 6 else "medium" if total >= 3 else "low"
    # One dimension carrying the whole composite is at most medium confidence,
    # however many rows stood behind it.
    if commercial is None or attention is None:
        confidence = "medium" if confidence == "high" else confidence

    return (commercial, attention,
            compute_composite(commercial, attention), confidence, caveats)


__all__ = [
    "roi_to_score", "cohort_pct_to_score", "usable", "compute_composite",
    "score_from_evidence", "ROI_SCALE_CEILING",
]
