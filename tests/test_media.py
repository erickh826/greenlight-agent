"""Tests for the storyboard plan, checked before anything is generated.

Media is the only irreversible spend in the pipeline. Every test here is a way
that three perfectly good images could be three images of the wrong film, or of
a film with words baked into the frame -- both of which cost the same as getting
it right and are only visible after the money is gone.

No Imagen, no Cloud TTS, no GCS. The plan is a separate artefact precisely so
this can be checked for free.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app.config import SCENE_COUNT  # noqa: E402
from app.contracts import (  # noqa: E402
    ScenePlan, StoryboardPlan, TreatmentProposal)
from app.media import (  # noqa: E402
    HOUSE_STYLE, compose_image_prompt, estimate_duration_sec,
    lettering_requests, restates_house_style, validate_storyboard_plan)

PROPOSAL = TreatmentProposal(
    variant="grounded",
    title="The Unveiling",
    logline="An auditor finds the ledger that explains her father's silence.",
    motif_tags=["hidden_conspiracy", "loss_of_innocence"],
    character_archetypes=["authority_figure", "caregiver"],
    act_structure="classic_three_act",
    rationale="The pairing carries a 5.68 median ROI over 13 films.",
    evidence=[],
)


def scene(i: int, **over) -> ScenePlan:
    base = dict(
        scene_index=i,
        description=f"Beat {i}: she opens another drawer.",
        image_prompt=f"A woman alone in a records room, beat {i}, dust in "
                     "low window light",
        narration=f"Some things are filed away for a reason. Beat {i}.",
    )
    return ScenePlan(**{**base, **over})


def plan(scenes=None, **over) -> StoryboardPlan:
    base = dict(
        proposal_title=PROPOSAL.title,
        variant=PROPOSAL.variant,
        style="Cold municipal interiors, one warm lamp per frame.",
        scenes=scenes if scenes is not None
        else [scene(i) for i in range(SCENE_COUNT)],
    )
    return StoryboardPlan(**{**base, **over})


# --- a good plan passes -----------------------------------------------------

def test_a_faithful_plan_validates():
    assert validate_storyboard_plan(plan(), PROPOSAL) == []


def test_scene_plans_carry_everything_a_scene_asset_needs():
    """Description for the viewer, a prompt for Imagen, a line for TTS."""
    p = plan()
    assert len(p.scenes) == SCENE_COUNT
    for s in p.scenes:
        assert s.description and s.image_prompt and s.narration
        assert estimate_duration_sec(s.narration) > 0


# --- it must still be the film that was approved ----------------------------

def test_a_renamed_treatment_is_rejected():
    errors = validate_storyboard_plan(plan(proposal_title="The Reveal"),
                                      PROPOSAL)
    assert any("approved proposal is" in e for e in errors)


def test_a_switched_variant_is_rejected():
    errors = validate_storyboard_plan(plan(variant="wildcard"), PROPOSAL)
    assert any("approved was" in e for e in errors)


def test_introducing_a_motif_nobody_approved_is_rejected():
    """The person picked a proposal, not a theme the storyboard liked better."""
    drifted = plan(scenes=[
        scene(0), scene(1),
        scene(2, description="She burns the ledger: `revenge` at last."),
    ])
    errors = validate_storyboard_plan(drifted, PROPOSAL)
    assert any("does not carry" in e and "revenge" in e for e in errors)


def test_the_proposals_own_vocabulary_is_fine():
    kept = plan(scenes=[
        scene(0, description="The `hidden_conspiracy` surfaces in a footnote."),
        scene(1), scene(2),
    ])
    assert validate_storyboard_plan(kept, PROPOSAL) == []


def test_ordinary_english_is_not_mistaken_for_a_tag():
    """"Survival" in a sentence is prose, not a claim about the tag set."""
    prose = plan(scenes=[
        scene(0, narration="It was survival, and she knew it."),
        scene(1), scene(2),
    ])
    assert validate_storyboard_plan(prose, PROPOSAL) == []


# --- structure the player depends on ----------------------------------------

def test_wrong_scene_count_is_rejected():
    errors = validate_storyboard_plan(plan(scenes=[scene(0), scene(1)]),
                                      PROPOSAL)
    assert any(f"expected {SCENE_COUNT}" in e for e in errors)


def test_duplicate_scene_indices_are_rejected():
    errors = validate_storyboard_plan(
        plan(scenes=[scene(0), scene(0), scene(2)]), PROPOSAL)
    assert any("exactly once" in e for e in errors)


def test_scenes_out_of_playback_order_are_rejected():
    errors = validate_storyboard_plan(
        plan(scenes=[scene(0), scene(2), scene(1)]), PROPOSAL)
    assert any("out of playback order" in e for e in errors)


def test_an_empty_style_is_rejected():
    errors = validate_storyboard_plan(plan(style="   "), PROPOSAL)
    assert any("read as one pitch" in e for e in errors)


# --- lettering in the frame -------------------------------------------------

def test_a_prompt_asking_for_a_title_card_is_rejected():
    """Generated typography spells nothing and reads as fake instantly."""
    lettered = plan(scenes=[
        scene(0, image_prompt="A title card reading DAY ONE over a dark room"),
        scene(1), scene(2),
    ])
    errors = validate_storyboard_plan(lettered, PROPOSAL)
    assert any("rendered text" in e for e in errors)


def test_quoted_lettering_is_caught():
    assert lettering_requests('a door with a plaque that reads "ARCHIVE"')
    assert lettering_requests('a banner spelling out the words "GO HOME"')


def test_a_negated_mention_is_compliance_not_a_request():
    """The house style is a list of negations, and so are good prompts.

    The first live run flagged all three scenes because the check ran against
    the composed prompt, which carries the house style's own "No text, no
    captions, no logos, no watermarks".
    """
    assert lettering_requests(HOUSE_STYLE) == []
    assert lettering_requests("a quiet street, no captions, no watermark") == []
    assert lettering_requests("an empty wall without a logo") == []
    # ... but a positive request in the same prompt is still caught.
    assert lettering_requests(
        "no watermark, but a title card reading THE END") == ["title card"]


def test_objects_that_merely_could_carry_text_are_not_flagged():
    """A newspaper on a table is a prop, not a request for typography."""
    assert lettering_requests(
        "a newspaper folded on a kitchen table, a road sign in fog, "
        "an open book beside a lamp") == []


# --- prompt composition -----------------------------------------------------

def test_the_subject_leads_and_the_register_trails():
    """Imagen weights the opening; the moment goes first, modifiers after."""
    p = plan()
    composed = compose_image_prompt(p, p.scenes[0])
    assert composed.startswith(p.scenes[0].image_prompt.strip())
    assert composed.endswith(HOUSE_STYLE)
    assert p.style.strip() in composed


def test_every_scene_shares_the_same_register():
    p = plan()
    prompts = [compose_image_prompt(p, s) for s in p.scenes]
    assert all(x.endswith(f"{p.style.strip()} {HOUSE_STYLE}") for x in prompts)


# --- the register must not be said three times ------------------------------

def test_a_style_that_restates_the_house_style_is_rejected():
    """What the first live run actually did.

    The model set style to the house style almost verbatim while the scene
    prompts restated it again, so the composed prompt opened with the same film
    stock vocabulary three times before reaching the subject.
    """
    echoed = plan(style=HOUSE_STYLE)
    errors = validate_storyboard_plan(echoed, PROPOSAL)
    assert any("restates the house style" in e for e in errors)


def test_a_film_specific_style_is_accepted():
    assert not restates_house_style(
        "Rain-soaked municipal Belfast, 1994, sodium streetlight on wet brick.")
    assert validate_storyboard_plan(
        plan(style="Rain-soaked municipal Belfast, 1994, sodium streetlight."),
        PROPOSAL) == []


def test_an_empty_style_is_not_called_a_restatement():
    assert not restates_house_style("   ")


def test_duration_never_collapses_to_nothing():
    """A short line still needs a beat to sit on screen."""
    assert estimate_duration_sec("Go.") >= 3.0
    assert estimate_duration_sec(" ".join(["word"] * 50)) > 15
