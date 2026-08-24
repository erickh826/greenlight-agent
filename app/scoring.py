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

from app.config import MIN_SAMPLE_SIZE, SCORING_WEIGHTS
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


def compute_composite(commercial: float, attention: float) -> float:
    """Weighted blend. The single definition of the composite."""
    return _clamp(
        commercial * SCORING_WEIGHTS["commercial"]
        + attention * SCORING_WEIGHTS["attention"]
    )


def score_from_evidence(
    roi_evidence: list[EvidenceItem],
    interest_evidence: list[EvidenceItem],
) -> tuple[float, float, float, str, list[str]]:
    """Derive both sub-scores and the composite from evidence alone.

    Returns (commercial, attention, composite, confidence, caveats).

    Confidence tracks how much survived the sample floor, not how sure the
    model sounded. With nothing left on either side the caller must emit
    insufficient_evidence rather than a number, because a score with no rows
    behind it is exactly the black box this design exists to avoid.
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
        return 0.0, 0.0, 0.0, "insufficient_evidence", caveats + [
            "No comparable set met the sample floor; no score was computed."
        ]

    commercial = median(roi_to_score(e.value) for e in roi_ok) if roi_ok else 0.0
    attention = (median(cohort_pct_to_score(e.value) for e in interest_ok)
                 if interest_ok else 0.0)

    if not roi_ok:
        caveats.append("No usable ROI comparables; commercial score is 0, "
                       "not low.")
    if not interest_ok:
        caveats.append("No usable interest comparables; attention score is 0, "
                       "not low.")
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

    return (commercial, attention,
            compute_composite(commercial, attention), confidence, caveats)


__all__ = [
    "roi_to_score", "cohort_pct_to_score", "usable", "compute_composite",
    "score_from_evidence", "ROI_SCALE_CEILING",
]
