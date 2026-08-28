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

from app.config import MIN_SAMPLE_SIZE
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


__all__ = ["build_recombine_phase_a_agent", "PHASE_A_TASK"]
