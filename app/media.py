"""Storyboard plans, checked before anything is generated from them.

Media is the one irreversible, billable step in this pipeline. Everything up to
the approval gate can be re-run for the cost of a few ClickHouse queries; three
Imagen frames and three Cloud TTS clips cannot be un-spent. So the plan is
validated against the approved proposal first, for nothing, and what that
catches is not an ugly frame -- it is three good frames of the wrong film.

Generation itself (Imagen, Cloud TTS, GCS) lands in Task 2. This module holds
the parts that can be checked without spending anything, and they are checkable
precisely because the plan is a separate artefact from the assets.
"""

from __future__ import annotations

import re

from app.config import SCENE_COUNT
from app.contracts import ScenePlan, StoryboardPlan, TreatmentProposal
from app.guardrails import unsupported_terms

# Hard-coded rather than model-chosen, and the same for every run. Three images
# generated from three independent prompts look like three films; a shared
# register is what makes them read as one pitch. It also keeps the demo's look
# stable across runs, so a judge comparing two runs sees the ideas differ rather
# than the art direction.
HOUSE_STYLE = (
    "Cinematic 35mm film still, anamorphic widescreen, shallow depth of field, "
    "naturalistic motivated lighting with deep shadows, muted desaturated "
    "palette with one warm accent, fine film grain. No text, no captions, no "
    "logos, no watermarks."
)

# Phrases that ask a diffusion model for lettering. Generated text is the single
# most reliable way to make an image look fake -- it comes out as convincing
# typography spelling nothing -- and the instruction not to is worth a check,
# because it is easy to write "a title card reading DAY ONE" without noticing
# that is a request for letters.
#
# Kept narrow on purpose. "sign", "book" and "newspaper" are legitimate objects
# in a frame; only an explicit request for rendered text is a finding.
LETTERING_PATTERNS = (
    r"\btitle card\b", r"\bcaption(s|ed)?\b", r"\bsubtitle", r"\bwatermark",
    r"\blogo\b", r"\btext overlay\b", r"\boverlaid text\b",
    r"\bthe words?\s+[\"']", r"\breading\s+[\"']", r"\bthat reads?\s+[\"']",
    r"\bspell(s|ing)?\s+out\b",
)

# Cloud TTS Chirp 3 HD lands near 150 words per minute in a narration register.
# Used only as a fallback: Task 2 reads the real length off the synthesised
# audio, because a scene whose image changes before its line finishes is the
# kind of thing nobody notices in code and everybody notices on stage.
WORDS_PER_SECOND = 2.5
MIN_SCENE_SEC = 3.0


def compose_image_prompt(plan: StoryboardPlan, scene: ScenePlan) -> str:
    """One frame's prompt: the moment, then this film's look, then the house.

    Subject first. Imagen reads natural language and weights the opening most,
    so leading with the register put four clauses of film-stock vocabulary in
    front of the thing the frame is actually of. The shared style trails it,
    which is where a modifier belongs and still guarantees all three frames
    carry the same one.
    """
    return (f"{scene.image_prompt.strip()} {plan.style.strip()} "
            f"{HOUSE_STYLE}")


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3}


HOUSE_STYLE_WORDS = _content_words(HOUSE_STYLE)
RESTATEMENT_RATIO = 0.7


def restates_house_style(style: str) -> bool:
    """Whether a plan's style is the house style said again.

    On the first live run the model set style to the house style almost
    verbatim, and the scene prompts restated it a third time -- so the composed
    prompt opened with the same register three times before reaching the
    subject. The style field is for what is specific to this film; a
    restatement is a wasted field, and worth a retry rather than a shrug.
    """
    words = _content_words(style)
    if not words:
        return False
    return len(words & HOUSE_STYLE_WORDS) / len(words) >= RESTATEMENT_RATIO


def estimate_duration_sec(narration: str) -> float:
    """Fallback scene length from the narration, when no audio exists yet."""
    words = len(narration.split())
    return max(MIN_SCENE_SEC, words / WORDS_PER_SECOND)


# "no captions", "without a watermark" -- a negated mention is compliance, not a
# request. The house style is itself written as a list of negations, so a check
# blind to this flags our own instruction; it did, on the first live run, for
# all three scenes at once.
NEGATION = re.compile(r"(?:\bno\b|\bnot\b|\bwithout\b|\bavoid(?:ing)?\b|"
                      r"\bfree of\b|\bnever\b)[\s\w,]{0,24}$")

# A negation only covers its own clause. Without this, "no watermark, but a
# title card reading THE END" reads as negated all the way to the end, because
# the "no" is still inside the lookback window.
CLAUSE_BREAK = re.compile(r"[.;:]|\bbut\b|\bhowever\b|\byet\b|\bthough\b")


def _is_negated(prefix: str) -> bool:
    """Whether the clause immediately before a match negates it."""
    return bool(NEGATION.search(CLAUSE_BREAK.split(prefix)[-1]))


def lettering_requests(text: str) -> list[str]:
    """Phrases in an image prompt that ask for rendered text.

    Negated mentions are ignored. What is left is a positive request for
    letters, which is worth catching because generated typography spells
    nothing and gives the image away instantly.
    """
    low = text.lower()
    found = set()
    for pattern in LETTERING_PATTERNS:
        for m in re.finditer(pattern, low):
            if _is_negated(low[:m.start()]):
                continue
            found.add(m.group(0).strip())
    return sorted(found)


def validate_storyboard_plan(plan: StoryboardPlan,
                             proposal: TreatmentProposal) -> list[str]:
    """Errors that make a plan unsafe to spend money on. Empty means go.

    The checks fall into two kinds. Structural ones -- the count, the indices --
    are about whether the player can show it. The rest are about whether it is
    still the film that was approved: a storyboard that renames the treatment,
    switches variant or introduces a motif nobody chose has stopped being a
    pitch for the thing the person picked.
    """
    errors: list[str] = []

    if plan.proposal_title.strip() != proposal.title.strip():
        errors.append(
            f"plan is titled {plan.proposal_title!r} but the approved proposal "
            f"is {proposal.title!r}")
    if plan.variant != proposal.variant:
        errors.append(
            f"plan says variant {plan.variant!r}, approved was "
            f"{proposal.variant!r}")
    if not plan.style.strip():
        errors.append("plan has no shared style; the three frames would not "
                      "read as one pitch")
    elif restates_house_style(plan.style):
        errors.append(
            "plan style restates the house style instead of adding this "
            "film's own look; say what is specific to this treatment -- "
            "its locations, palette, era, weather")

    if len(plan.scenes) != SCENE_COUNT:
        errors.append(f"plan has {len(plan.scenes)} scenes, expected "
                      f"{SCENE_COUNT}")
    indices = [s.scene_index for s in plan.scenes]
    if sorted(indices) != list(range(len(plan.scenes))):
        errors.append(f"scene_index values {indices} are not 0.."
                      f"{len(plan.scenes) - 1} exactly once")
    elif indices != sorted(indices):
        errors.append(f"scenes are out of playback order: {indices}")

    for scene in plan.scenes:
        prefix = f"scene[{scene.scene_index}]"
        if not scene.description.strip():
            errors.append(f"{prefix} has no description")
        if not scene.image_prompt.strip():
            errors.append(f"{prefix} has no image_prompt")
        if not scene.narration.strip():
            errors.append(f"{prefix} has no narration")

        lettering = lettering_requests(scene.image_prompt)
        if lettering:
            errors.append(
                f"{prefix} image_prompt asks for rendered text "
                f"({', '.join(lettering)}); generated lettering spells nothing "
                "and reads as fake")

    # The proposal's own tags are what this pitch is selling. A storyboard that
    # brings in a motif the proposal does not carry is describing a different
    # film -- and the terms are closed vocabulary, so this is exact rather than
    # a judgement about theme.
    approved_terms = " ".join(
        [*(m.value for m in proposal.motif_tags),
         *(a.value for a in proposal.character_archetypes),
         proposal.act_structure.value])
    plan_text = "\n".join(
        [plan.style]
        + [f"{s.description}\n{s.image_prompt}\n{s.narration}"
           for s in plan.scenes])
    invented = unsupported_terms(plan_text, approved_terms)
    if invented:
        errors.append(
            "storyboard introduces vocabulary the approved proposal does not "
            "carry: " + ", ".join(invented))

    return errors


__all__ = ["compose_image_prompt", "estimate_duration_sec",
           "lettering_requests", "validate_storyboard_plan",
           "LETTERING_PATTERNS", "WORDS_PER_SECOND", "MIN_SCENE_SEC"]
