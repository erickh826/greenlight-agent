"""Pydantic contracts for agent output and the SSE stream.

The vocabulary enums are generated from etl/vocab.py rather than restated, so a
term added there reaches the response schema, the prompt and the database
without four edits that can drift apart.

EvidenceItem stays domain-agnostic on purpose -- it is a claim, the query that
produced it, and how many rows stood behind it. The film-specific shape lives in
TreatmentProposal and PredictionScore, and is deliberately not generalised: this
project has one domain.
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "etl"))
from vocab import (  # noqa: E402
    ACT_STRUCTURES,
    ARCHETYPES,
    CONFLICT_SCALES,
    MOTIFS,
    RELEASE_BUCKETS,
)

# Gemini's response_schema accepts a str-valued Enum as a closed set, which is
# what keeps the model from inventing near-synonyms.
Motif = Enum("Motif", {v: v for v in MOTIFS}, type=str)
Archetype = Enum("Archetype", {v: v for v in ARCHETYPES}, type=str)
ActStructure = Enum("ActStructure", {v: v for v in ACT_STRUCTURES}, type=str)
ConflictScale = Enum("ConflictScale", {v: v for v in CONFLICT_SCALES}, type=str)
ReleaseBucket = Enum("ReleaseBucket", {v.replace("-", "_"): v
                                       for v in RELEASE_BUCKETS}, type=str)


class EvidenceItem(BaseModel):
    """One number, and everything needed to check it.

    sample_count is not decoration: app/scoring.py refuses to fold an item
    below MIN_SAMPLE_SIZE into a score, and the frontend shows it beside the
    claim so a viewer can see how thin a figure is.
    """

    claim: str = Field(description="What the number says, in one sentence")
    sql_query: str = Field(description="The query that produced it, verbatim")
    sample_count: int = Field(ge=0)
    source_view: str = Field(description="Which table or view it came from")
    value: float = Field(description="The figure itself, so scoring can "
                                     "recompute without parsing `claim`")


class FilmMotifs(BaseModel):
    """ETL output: what Gemini extracts from one plot summary.

    Only abstract structure leaves this step. No plot text, no dialogue, no
    scene order -- see SYSTEM_SPEC §4.6 on the CMU corpus licence.
    """

    motif_tags: list[Motif] = Field(min_length=3, max_length=6)
    act_structure: ActStructure
    character_archetypes: list[Archetype] = Field(min_length=2, max_length=4)
    tone_axis: float = Field(ge=-1.0, le=1.0,
                             description="-1 bleak … +1 warm")
    conflict_scale: ConflictScale

    @field_validator("motif_tags", "character_archetypes")
    @classmethod
    def _no_repeats(cls, v: list) -> list:
        if len(set(v)) != len(v):
            raise ValueError("duplicate entries")
        return v


class TreatmentProposal(BaseModel):
    variant: Literal["grounded", "wildcard"]
    title: str
    logline: str = Field(max_length=200)
    motif_tags: list[Motif] = Field(min_length=2, max_length=6)
    character_archetypes: list[Archetype] = Field(min_length=2, max_length=4)
    act_structure: ActStructure
    rationale: str = Field(description="Why this combination")
    evidence: list[EvidenceItem]


class PredictionScore(BaseModel):
    """Analogue / evidence scoring -- historical comparables, not a forecast.

    `composite` must equal app/scoring.compute_composite() over `evidence`. It
    is recomputed on the way out rather than trusted from the model, because
    "the score is explainable" only means something if the arithmetic is
    reproducible from the listed evidence.

    All three scores are nullable, and the distinction is load-bearing: None is
    "we found nothing to compare against", 0.0 is "the comparables were as bad
    as anything in the dataset". Folding the first into the second is a silent
    penalty for missing data -- see app/scoring.compute_composite.
    """

    proposal_title: str
    commercial_score: float | None = Field(
        default=None, ge=0, le=100,
        description="From the ROI distribution of historical analogues. None "
                    "means no comparable set met the sample floor -- N/A, not "
                    "a low result.")
    attention_score: float | None = Field(
        default=None, ge=0, le=100,
        description="From interest_cohort_pct of historical analogues, over "
                    "films above the measurement floor only. This is sustained "
                    "post-2015 lookup interest, not opening-weekend attention "
                    "-- no film in the dataset has the latter. None means N/A, "
                    "not low interest.")
    composite: float | None = Field(
        default=None, ge=0, le=100,
        description="Weighted blend of whichever sub-scores exist, with the "
                    "remaining weights renormalised. None when neither does.")
    confidence: Literal["high", "medium", "low", "insufficient_evidence"]
    evidence: list[EvidenceItem]
    caveats: list[str] = Field(
        default_factory=list,
        description="Stated limits of this comparison, e.g. thin sample, era "
                    "mismatch, interest measured years after release")


class SceneAsset(BaseModel):
    scene_index: int = Field(ge=0)
    description: str
    image_url: str
    audio_url: str
    duration_sec: float = Field(gt=0)


__all__ = [
    "Motif", "Archetype", "ActStructure", "ConflictScale", "ReleaseBucket",
    "EvidenceItem", "FilmMotifs", "TreatmentProposal", "PredictionScore",
    "SceneAsset",
]
