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


class AnalogueEvidence(EvidenceItem):
    """An EvidenceItem that also says which score it feeds.

    The metric is not cosmetic. commercial reads an ROI figure against the ROI
    row count; attention reads interest_cohort_pct against interest_sample_count,
    which is the smaller of the two because interest is aggregated only over
    films above the measurement floor. Getting the pairing wrong produces a
    number that looks entirely reasonable and is backed by fewer rows than it
    claims, so app/analogue_scoring.py checks the pairing against the SQL rather
    than taking the label's word for it.
    """

    metric: Literal["commercial", "attention"]


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

    evidence is AnalogueEvidence rather than EvidenceItem so that `metric`
    survives serialisation. Pydantic serialises by the declared type, so with
    the base class here the written JSON lost the commercial/attention label --
    which left a file whose composite could not be recomputed from its own
    contents, the one property this class exists to have.

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
    evidence: list[AnalogueEvidence]
    caveats: list[str] = Field(
        default_factory=list,
        description="Stated limits of this comparison, e.g. thin sample, era "
                    "mismatch, interest measured years after release")


# Analogue retrieval bands. These are filters for finding comparable films, not
# claims about what the proposal would actually cost to make -- a proposal has
# no budget, so one is chosen for it and stated.
BudgetBand = Literal["micro", "low", "mid", "high"]


class AnalogueScoringRequest(BaseModel):
    """What PredictAgent is asked to score, and under which comparison.

    TreatmentProposal carries no budget and no target era, because neither is a
    property of an idea. They are properties of the comparison being drawn, so
    they live here: the caller picks the analogue set, and the caveats on the
    resulting score say which one was picked.

    release_bucket defaults to None on purpose. Narrowing by era is the step
    that empties a cell -- see sql/003 on mv_motif_pair_stats -- so it is opt-in
    and applied last, after the broad result has shown it can afford the split.
    """

    proposal: TreatmentProposal
    budget_band: BudgetBand | None = "mid"
    target_release_bucket: ReleaseBucket | None = None


class AnalogueEvidenceDraft(BaseModel):
    """One figure the convergence step claims, citing the query by number.

    Deliberately has no sql_query field. The first version asked the model to
    copy the query verbatim out of the transcript, and it mostly did -- then on
    one run it reformatted a WHERE clause, wrapping `budget_usd >= 20000000 AND
    budget_usd < 80000000` in its own parentheses. Semantically identical,
    textually different, so the grounding check rejected it and a score with
    133 real films behind it collapsed to insufficient_evidence.

    Transcription is not a job for a model. The transcript numbers its queries;
    the model says which one produced the figure, and app/analogue_scoring.py
    substitutes the text. A query it never types is a query it cannot
    paraphrase, and the citation becomes exact rather than nearly right.
    """

    claim: str = Field(description="What the number says, in one sentence")
    query_index: int = Field(
        ge=1, description="Which QUERY n in the transcript produced it")
    sample_count: int = Field(ge=0)
    value: float = Field(description="The figure itself, copied exactly")
    metric: Literal["commercial", "attention"]


class AnalogueEvidenceBundle(BaseModel):
    """The convergence step's output: evidence only, deliberately no scores.

    PredictAgent never emits commercial_score, attention_score or composite.
    Those are computed in app/scoring.py from this list, which is the only way
    "the score is explainable" survives contact with a judge who recomputes it.
    """

    proposal_title: str
    evidence: list[AnalogueEvidenceDraft]
    caveats: list[str] = Field(
        default_factory=list,
        description="Limits of this comparison in prose. No numbers that are "
                    "not already in an evidence item.")


class VariantOutcome(BaseModel):
    """One proposal and what scoring made of it.

    proposal and score are both nullable and independently so. A variant whose
    Phase B output failed validation has neither; a variant that produced a
    proposal the database could not find comparables for has a proposal and a
    score of confidence insufficient_evidence. Collapsing the two into one
    "failed" flag would hide which of the pipeline's halves went wrong.
    """

    variant: Literal["grounded", "wildcard"]
    proposal: TreatmentProposal | None = None
    score: PredictionScore | None = None
    validation_errors: list[str] = Field(default_factory=list)


class StageTiming(BaseModel):
    """What one pipeline stage cost.

    Recorded because the first attempt to make the run faster had to start by
    finding out where the time went, and the answer was not where it looked:
    ClickHouse was 7% of a 307-second run and the other 93% was model latency
    and stages queued behind each other. A timing that is not in the result
    document is a timing nobody checks after the next change.
    """

    label: str
    variant: str = ""
    elapsed_sec: float
    tool_calls: int = 0
    db_ms: float = 0.0


class GreenlightRunResult(BaseModel):
    """One end-to-end run: the document the CLI writes and the API returns.

    Deliberately carries the counters as well as the output. "Gemini queried
    ClickHouse at runtime" is the claim the whole project rests on, and a result
    file that shows only the finished proposals cannot distinguish a real run
    from a cached one.
    """

    run_id: str
    prompt: str
    model: str
    started_at: float
    finished_at: float
    outcomes: list[VariantOutcome]
    stages: list[StageTiming] = Field(default_factory=list)
    tool_calls: int = 0
    sql_errors: int = 0
    guardrail_blocks: int = 0

    @property
    def elapsed_sec(self) -> float:
        return self.finished_at - self.started_at


class ScenePlan(BaseModel):
    """One storyboard beat, before any pixel or sample exists.

    Three fields rather than one, because they are read by three different
    things and merging them makes all three worse: `description` is what a
    viewer reads beside the frame, `image_prompt` is what Imagen receives, and
    `narration` is what Cloud TTS speaks. A single blob would put camera
    directions into the narration and prose into the image prompt.

    Splitting them also puts the expensive calls behind a checkable artefact.
    A storyboard plan can be validated against the approved proposal for free;
    an image cannot.
    """

    scene_index: int = Field(ge=0, description="0-based, in playback order")
    description: str = Field(
        max_length=300,
        description="What happens in this beat, for the viewer to read")
    image_prompt: str = Field(
        max_length=600,
        description="Visual content only -- subject, setting, light, framing. "
                    "No title cards, captions, letters or logos.")
    narration: str = Field(
        max_length=320,
        description="What the voice says over this beat. One or two sentences.")


class StoryboardPlan(BaseModel):
    """The full plan for one approved proposal.

    Carries the title and variant so the plan can be checked against what was
    actually approved. Media generation is the one irreversible, billable step
    in this pipeline, and the thing worth guarding against is not a bad image --
    it is three good images of the wrong film.
    """

    proposal_title: str
    variant: Literal["grounded", "wildcard"]
    style: str = Field(
        max_length=300,
        description="One visual register shared by every scene, so the three "
                    "frames read as one pitch rather than three films.")
    scenes: list[ScenePlan]


class SceneAsset(BaseModel):
    scene_index: int = Field(ge=0)
    description: str
    image_url: str
    audio_url: str
    duration_sec: float = Field(gt=0)


__all__ = [
    "Motif", "Archetype", "ActStructure", "ConflictScale", "ReleaseBucket",
    "EvidenceItem", "FilmMotifs", "TreatmentProposal", "PredictionScore",
    "BudgetBand", "AnalogueScoringRequest", "AnalogueEvidence",
    "AnalogueEvidenceDraft", "AnalogueEvidenceBundle",
    "VariantOutcome", "StageTiming", "GreenlightRunResult",
    "ScenePlan", "StoryboardPlan", "SceneAsset",
]
