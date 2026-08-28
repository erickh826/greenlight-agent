"""RecombineAgent -- finds structural combinations the data supports.

Phase A is autonomous database use: the agent decides its own queries, runs them
through MCP, and reports what came back in prose. Structured convergence to
TreatmentProposal is Phase B, deliberately separate -- a response_schema forces
an answer, and asking for one while the agent is still gathering evidence
produces a well-formed proposal with nothing behind it.

The instruction is the analyst preamble from app/prompts.py plus the task below.
It is not the place to repeat the schema or the vocabulary; those are already in
the preamble, read from sql/ and etl/vocab.py so they cannot drift.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.genai import types

from app.config import MIN_SAMPLE_SIZE, WILDCARD_TEMPERATURE
from app.contracts import TreatmentProposal
from app.prompts import analyst_system_instruction

PHASE_A_TASK = f"""YOUR TASK

Find narrative combinations -- motif pairs, character archetypes, or an
archetype within an era -- that the historical record actually supports, and
report what you found.

Work in this order. It matters, and skipping to the end produces confident
claims resting on four films:

1. Query a broad aggregate first, with no era filter. Rank by sample count and
   ROI, and see what the whole dataset says.
2. Query a second surface, so the finding does not rest on one view. Use
   mv_archetype_performance, mv_motif_pair_stats and films.
3. Only then narrow -- by era, budget, or a specific combination -- and only if
   the broad result had samples to spare. Check the count after narrowing; if it
   fell below {MIN_SAMPLE_SIZE}, say so and stay with the broader finding.

You must run queries before drawing any conclusion. Do not answer from general
knowledge about film: the claim being made is that these numbers came from this
database, and an unsourced assertion breaks it even when it happens to be true.

Report, in prose:

- two or three candidate combinations worth developing;
- for each, the figures you retrieved and the sample count behind them, with
  interest figures cited against interest_sample_count, never sample_count;
- what the evidence does not support -- a combination you looked at and dropped
  for thin samples is a useful thing to state, not a failure to hide.

Name a dropped combination ONLY if it appeared in a query you ran this session
or in a result you received back. Do not write that something "showed a high ROI
in initial searches" unless a result in this session shows it. A hedged sentence
about a combination you did not query is still an invented finding, and it is
harder to spot than an overclaim because it sounds careful.

Use only vocabulary terms that appear in the controlled lists. A term outside
them matches nothing, so a proposal built on one is empty however good it
sounds."""

PHASE_B_GROUNDED_TASK = f"""YOUR TASK

Turn the Phase A transcript the user provides into exactly one grounded
TreatmentProposal.

Tools are disabled in this phase. Do not ask to query the database and do not
claim you ran a query. The only evidence you may use is the SQL and result rows
already present in the Phase A transcript.

Use this policy:

1. Pick one proposal whose motif_tags, character_archetypes and rationale are
   supported by terms that appeared in a Phase A query or result.
2. Set variant to "grounded". The wildcard branch is a separate call
   and is not your concern here.
3. Every evidence item must point back to the Phase A transcript:
   - copy the SQL query verbatim into sql_query;
   - set source_view to the table or materialized view read by that query;
   - set sample_count to the count behind the cited figure, using
     interest_sample_count for interest figures and sample_count for ROI;
   - keep only evidence at or above the {MIN_SAMPLE_SIZE}-sample floor.
4. Write a film treatment idea, not a data report. The logline and rationale may
   be creative, but the cited tags, archetypes, counts and values must be
   grounded in the transcript. The logline must be one sentence and no more
   than 200 characters.

If the transcript is too thin for a field, choose a broader supported field from
the transcript. Do not invent a near-synonym, a new vocabulary term, a later
release era, or an unqueried combination just to make the premise sound better.
"""


PHASE_B_WILDCARD_TASK = f"""YOUR TASK

Turn the Phase A transcript the user provides into exactly one wildcard
TreatmentProposal.

The wildcard exists as a control. The grounded proposal takes what the data
rewards; this one deliberately takes a combination the transcript gives no
support for, so that the scoring can be seen to distinguish them. If the
wildcard scored as well as the grounded proposal every time, the score would be
measuring nothing.

Tools are disabled. Do not claim you ran a query.

Use this policy:

1. Set variant to "wildcard".
2. Choose motif_tags and character_archetypes from the controlled vocabulary
   that the Phase A transcript did NOT show performing well -- a pair it never
   returned, or one it returned with thin samples or a weak figure. Say which,
   in the rationale.
3. Be honest in the evidence list rather than generous. Cite only figures the
   transcript actually contains, copying sql_query verbatim, and cite the ones
   that argue AGAINST this premise if that is what the transcript holds. An
   empty evidence list is a valid and honest answer for a combination the
   transcript never measured; an invented supporting figure is not.
4. The premise may be strange. The vocabulary may not: a term outside the
   controlled lists matches no row, so a wildcard built on one is not a risky
   bet, it is an empty one.
5. The logline must be one sentence and no more than 200 characters.

Write it as a film someone would actually want to see. "Unsupported by the data"
is a statement about the historical record, not permission to write something
incoherent."""


def build_recombine_phase_a_agent(model: str, toolset) -> Agent:
    """Phase A: tools on, no response schema, prose out.

    The toolset's lifetime belongs to the caller -- see app/mcp.py.
    """
    return Agent(
        name="recombine_phase_a",
        model=model,
        instruction=f"{analyst_system_instruction()}\n\n{PHASE_A_TASK}",
        tools=[toolset],
    )


def build_recombine_phase_b_grounded_agent(model: str) -> Agent:
    """Phase B: no tools, schema-constrained grounded proposal out."""
    return Agent(
        name="recombine_phase_b_grounded",
        model=model,
        instruction=(
            f"{analyst_system_instruction()}\n\n{PHASE_B_GROUNDED_TASK}"
        ),
        tools=[],
        output_schema=TreatmentProposal,
        generate_content_config=types.GenerateContentConfig(temperature=0.2),
    )


def build_recombine_phase_b_wildcard_agent(model: str) -> Agent:
    """Phase B, the control branch: same schema, deliberately unsupported.

    Temperature is WILDCARD_TEMPERATURE rather than the grounded branch's 0.2.
    The grounded proposal wants the most defensible reading of the evidence and
    a low temperature gets it; this one wants a combination the evidence does
    not point at, and asking for that at 0.2 tends to produce the second-best
    grounded answer instead of a genuine outlier.
    """
    return Agent(
        name="recombine_phase_b_wildcard",
        model=model,
        instruction=(
            f"{analyst_system_instruction()}\n\n{PHASE_B_WILDCARD_TASK}"
        ),
        tools=[],
        output_schema=TreatmentProposal,
        generate_content_config=types.GenerateContentConfig(
            temperature=WILDCARD_TEMPERATURE),
    )


__all__ = [
    "build_recombine_phase_a_agent",
    "build_recombine_phase_b_grounded_agent",
    "build_recombine_phase_b_wildcard_agent",
    "PHASE_A_TASK",
    "PHASE_B_GROUNDED_TASK",
    "PHASE_B_WILDCARD_TASK",
]
