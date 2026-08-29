"""Tests for running the two variant chains at the same time.

The variants share nothing but the Phase A transcript, and running them one
after the other was 40% of a 307-second run. Doing them together is worth about
seventy seconds and introduces exactly two ways to be wrong, both tested here:
the results could come back in completion order instead of requested order, and
one branch failing could take the other down.

No Gemini and no ClickHouse. The stages are replaced with fakes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app import pipeline  # noqa: E402
from app.contracts import PredictionScore, TreatmentProposal  # noqa: E402
from app.query_run import QueryRun  # noqa: E402


def proposal(variant: str) -> TreatmentProposal:
    return TreatmentProposal(
        variant=variant, title=f"{variant} title",
        logline="A film about a film.",
        motif_tags=["hidden_conspiracy", "loss_of_innocence"],
        character_archetypes=["authority_figure", "caregiver"],
        act_structure="classic_three_act", rationale="because", evidence=[])


def score(variant: str) -> PredictionScore:
    return PredictionScore(proposal_title=f"{variant} title",
                           confidence="high", evidence=[], caveats=[])


def stage_run(label: str, variant: str = "") -> QueryRun:
    run = QueryRun()
    run.label, run.variant, run.elapsed_sec = label, variant, 1.0
    return run


@pytest.fixture
def fakes(monkeypatch):
    """Replace the three model stages. `order` records real interleaving."""
    order: list[str] = []

    async def fake_phase_a(model, toolset, prompt, emit):
        return stage_run("recombine_a")

    monkeypatch.setattr(pipeline, "recombine_phase_a", fake_phase_a)
    return order


def run(variants, order, *, slow=(), fail_b=(), raise_score=()):
    async def fake_phase_b(model, variant, transcript, emit):
        order.append(f"b:{variant}:start")
        # Yield enough times that a sequential implementation cannot interleave
        # while a concurrent one must.
        for _ in range(6 if variant in slow else 1):
            await asyncio.sleep(0)
        order.append(f"b:{variant}:end")
        if variant in fail_b:
            return None, [f"{variant} rejected"], stage_run("b", variant)
        return proposal(variant), [], stage_run(f"recombine_b_{variant}",
                                                variant)

    async def fake_score(model, toolset, request, emit):
        variant = request.proposal.variant
        order.append(f"s:{variant}")
        await asyncio.sleep(0)
        if variant in raise_score:
            raise RuntimeError(f"{variant} scoring exploded")
        return pipeline.ScoringOutcome(
            score=score(variant), query_run=stage_run("q", variant),
            bundle=None, parse_error=None, evidence_errors=[],
            converge_calls=0, converge_run=stage_run("c", variant))

    import unittest.mock as mock
    with mock.patch.object(pipeline, "recombine_phase_b", fake_phase_b), \
         mock.patch.object(pipeline, "score_proposal", fake_score):
        return asyncio.run(pipeline.run_greenlight(
            "fake-model", object(), lambda e: None,
            prompt="p", variants=variants, run_id="r"))


def test_the_two_variants_actually_overlap(fakes):
    """Both Phase B stages start before either finishes."""
    result, _, _ = run(["grounded", "wildcard"], fakes, slow=("grounded",))
    starts = [i for i, e in enumerate(fakes) if e.endswith(":start")]
    first_end = next(i for i, e in enumerate(fakes) if e.endswith(":end"))
    assert max(starts) < first_end, f"ran sequentially: {fakes}"
    assert len(result.outcomes) == 2


def test_outcomes_follow_the_requested_order_not_completion_order(fakes):
    """grounded is asked for first, so grounded is the left-hand card.

    Without this the proposal cards swap sides between runs and the result
    document is unstable, purely because one branch happened to finish first.
    """
    result, _, _ = run(["grounded", "wildcard"], fakes, slow=("grounded",))
    assert [o.variant for o in result.outcomes] == ["grounded", "wildcard"]
    assert fakes.index("b:wildcard:end") < fakes.index("b:grounded:end")


def test_a_rejected_proposal_does_not_stop_the_other_variant(fakes):
    result, _, _ = run(["grounded", "wildcard"], fakes, fail_b=("grounded",))
    by_variant = {o.variant: o for o in result.outcomes}
    assert by_variant["grounded"].score is None
    assert by_variant["grounded"].validation_errors == ["grounded rejected"]
    assert by_variant["wildcard"].score is not None


def test_a_raising_branch_does_not_stop_the_other_variant(fakes):
    """gather() must not let one exception cancel the sibling."""
    result, _, _ = run(["grounded", "wildcard"], fakes,
                       raise_score=("wildcard",))
    by_variant = {o.variant: o for o in result.outcomes}
    assert by_variant["wildcard"].score is None
    assert "exploded" in by_variant["wildcard"].validation_errors[0]
    assert by_variant["grounded"].score is not None
    assert [o.variant for o in result.outcomes] == ["grounded", "wildcard"]


def test_stage_timings_are_recorded_for_every_stage(fakes):
    result, _, _ = run(["grounded", "wildcard"], fakes)
    labels = [s.label for s in result.stages]
    assert "recombine_a" in labels
    assert all(s.elapsed_sec >= 0 for s in result.stages)
    # Phase A plus three stages per variant.
    assert len(result.stages) == 1 + 3 * 2
