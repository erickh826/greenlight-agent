"""StoryboardAgent -- the approved proposal, as three shots.

No tools. It reads one TreatmentProposal and returns a StoryboardPlan; it does
not query, does not re-score, and does not get to reconsider the premise. By
this point a person has looked at two proposals and picked one, and quietly
drifting to a better idea would throw away the only human decision in the run.

The plan is deliberately a separate artefact from the media. Imagen and Cloud
TTS are the irreversible, billable step, and what is worth guarding against is
not an ugly frame -- it is three good frames of the wrong film. A plan can be
checked against the approved proposal for nothing.

One thing this agent must not do: retell the plot of any historical analogue.
The comparable films exist in this pipeline as ROI and interest percentiles, and
the CMU plot summaries they were derived from are licensed for the ETL step
only, never for output. See SYSTEM_SPEC §4.6.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.genai import types

from app.config import SCENE_COUNT, SCHEMA_STAGE_THINKING_BUDGET
from app.contracts import StoryboardPlan, TreatmentProposal
from app.media import HOUSE_STYLE

STORYBOARD_TASK = f"""YOUR TASK

You are given one approved film treatment. Turn it into exactly {SCENE_COUNT}
storyboard beats: an opening image, a turn, and a final image.

This is the pitch reel for the treatment you were given. It is not an
opportunity to improve it. Keep proposal_title and variant exactly as supplied,
and keep the premise, the motifs and the archetypes the proposal names. If you
think a different idea would be stronger, that judgement already happened -- a
person compared two proposals and chose this one.

For each beat:

- description: what happens, in one or two sentences, for a viewer to read
  beside the frame. Present tense.
- image_prompt: what is visible in this one moment, and nothing else. Subject,
  setting, time of day, weather, who is where, what they are doing. Describe a
  single moment, not a sequence -- "a man stands in a flooded lobby, water to
  his knees" works; "he walks in, then turns" does not, because it is two
  images. Do NOT restate film stock, grain, depth of field, colour grading or
  lighting vocabulary: the house style is appended to every prompt already, and
  repeating it pushes the actual subject to the back. Never ask for text,
  titles, captions, subtitles, logos, watermarks or numbers in the frame;
  generated lettering is the single most reliable way to make an image look
  fake. Do not name a real person, actor, brand or existing film.
- narration: what a voice says over the beat. One or two sentences, spoken
  register, under 320 characters. Write for the ear -- it will be read aloud, so
  no parentheses, no lists, no stage directions.

Set style to what is specific to THIS film's look -- its locations, palette,
era, weather, the texture of its world. One line, shared by all three beats. Do
not repeat the house style back: it is added to every prompt automatically, and
a style field that restates it is a wasted field.

Do not describe the plot of any real film. The historical comparables in this
project are numbers -- ROI and interest percentiles -- and their plot summaries
are not yours to retell. Write this treatment's scenes.

Do not introduce motif or archetype terms the proposal does not already carry.
The vocabulary is closed and the proposal's own tags are the ones this pitch is
selling."""


def build_storyboard_agent(model: str) -> Agent:
    """No tools, StoryboardPlan out.

    Temperature sits above the grounded proposal's 0.2: this step is writing
    images rather than defending figures, and the constraint that matters here
    is enforced by validation against the approved proposal, not by keeping the
    model timid.
    """
    return Agent(
        name="storyboard",
        model=model,
        instruction=(
            f"You write pitch reels for film treatments.\n\n"
            f"HOUSE STYLE (every image must sit inside it):\n{HOUSE_STYLE}\n\n"
            f"{STORYBOARD_TASK}"
        ),
        tools=[],
        output_schema=StoryboardPlan,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.7,
            thinking_config=types.ThinkingConfig(
                thinking_budget=SCHEMA_STAGE_THINKING_BUDGET),
        ),
    )


def storyboard_prompt(proposal: TreatmentProposal) -> str:
    """The approved treatment, as the storyboard agent receives it."""
    return f"""Storyboard this approved treatment.

TITLE:      {proposal.title}
VARIANT:    {proposal.variant}
LOGLINE:    {proposal.logline}
MOTIFS:     {", ".join(m.value for m in proposal.motif_tags)}
ARCHETYPES: {", ".join(a.value for a in proposal.character_archetypes)}
STRUCTURE:  {proposal.act_structure.value}

WHY THIS ONE WAS CHOSEN
{proposal.rationale}

Return exactly {SCENE_COUNT} beats, scene_index 0 to {SCENE_COUNT - 1}, in
playback order. proposal_title must be exactly "{proposal.title}" and variant
must be "{proposal.variant}"."""


__all__ = ["build_storyboard_agent", "storyboard_prompt", "STORYBOARD_TASK"]
