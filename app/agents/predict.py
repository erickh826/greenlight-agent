"""PredictAgent -- scores a proposal against historical analogues.

Named "Analogue / Evidence Scoring Agent" everywhere a viewer can see it,
because "predict" invites the reading this project is careful not to make: no
box-office forecast is produced, and none could be. What is produced is a set of
comparable films drawn from 1,238 titles released 1990-2014, the ROI and
sustained-interest distributions of that set, and a score computed from them by
app/scoring.py.

Same two-stage shape as RecombineAgent, for the same reason. Stage one has tools
and no response schema: the agent decides which comparable sets to look at and
what to do when a set comes back too thin. Stage two has a response schema and
no tools: it turns the transcript into EvidenceItems. A single call with both
would force a schema-shaped answer while the evidence was still being gathered,
and the schema would be filled either way.

Neither stage emits a score. The model chooses what to compare; the arithmetic
happens in Python over what it retrieved.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.genai import types

from app.config import (
    BUDGET_BANDS, MIN_SAMPLE_SIZE, SCHEMA_STAGE_THINKING_BUDGET,
    SQL_RETRY_LIMIT)
from app.contracts import AnalogueEvidenceBundle, AnalogueScoringRequest
from app.prompts import analyst_system_instruction

_BANDS = "\n".join(f"    {name:<6} {predicate}"
                   for name, predicate in BUDGET_BANDS.items())

QUERY_TASK = f"""YOUR TASK

You are given one film treatment proposal. Retrieve the historical films most
comparable to it and report what their record looks like.

This is analogue retrieval, not forecasting. You are not estimating what this
film would earn. You are answering "when this combination of elements has
appeared before, how did those films do", and the answer is only worth as much
as the number of films behind it.

Work broad to narrow, and check the count at every step:

1. Motif pairs from mv_motif_pair_stats, for pairs drawn from the proposal's
   motif_tags. No era filter. Prefer one set-based query for the whole proposal
   motif set instead of one tool call per pair:

       SELECT motif_a, motif_b,
              countMerge(sample_count)       AS n_roi,
              quantileMerge(0.5)(roi_median) AS roi_median,
              countMerge(interest_sample_count) AS n_interest,
              quantileMerge(0.5)(interest_pct_median) AS interest_median
       FROM mv_motif_pair_stats
       WHERE motif_a IN ('…', '…') AND motif_b IN ('…', '…')
       GROUP BY motif_a, motif_b
       HAVING n_roi >= 8
       ORDER BY roi_median DESC, n_roi DESC

2. Archetypes from mv_archetype_performance, for the proposal's
   character_archetypes. No era filter yet. Prefer one query for the archetype
   set instead of one call per archetype:

       SELECT archetype,
              countMerge(sample_count)       AS n_roi,
              quantileMerge(0.5)(roi_median) AS roi_median,
              countMerge(interest_sample_count) AS n_interest,
              quantileMerge(0.5)(interest_pct_median) AS interest_median
       FROM mv_archetype_performance
       WHERE archetype IN ('…', '…')
       GROUP BY archetype
       HAVING n_roi >= 8
       ORDER BY roi_median DESC, n_roi DESC

3. A film-level analogue set from films, matching at least one proposal motif
   and at least one proposal archetype. Start with that relaxed intersection and
   compute it in one query:

       SELECT count()                                    AS n_roi,
              quantile(0.5)(roi)                         AS roi_median,
              quantile(0.75)(roi)                        AS roi_p75,
              countIf(has_interest_signal)               AS n_interest,
              quantileIf(0.5)(interest_cohort_pct, has_interest_signal)
                                                         AS interest_median
       FROM films
       WHERE roi IS NOT NULL
         AND (has(motif_tags, '…') OR has(motif_tags, '…'))
         AND (has(character_archetypes, '…') OR has(character_archetypes, '…'))

4. Only then add the budget band, and only then the release bucket. Add each
   one, look at what happened to n_roi, and abandon the narrowing if it fell
   below {MIN_SAMPLE_SIZE}. A tighter analogue set that no longer has rows in it
   is worse evidence than a looser one that does.
5. Optionally, up to five named comparable titles for the interface to show.
   These illustrate the set; they are not evidence on their own, and one
   spectacular outlier is not an analogue.

The two counts are not interchangeable and this is the mistake to avoid. An ROI
figure is reported against the ROI row count; an interest figure is reported
against interest_sample_count (in the views) or countIf(has_interest_signal) (in
films). Interest aggregates exclude films below the measurement floor, so its
count is always the smaller one, and quoting the larger one beside an interest
median overstates the evidence in a way nothing in the result looks wrong about.

Do not state a score, a percentage rating, or a predicted gross. Report figures
and counts. The score is computed afterwards, in code, from the evidence you
retrieved -- a number you write here is discarded, and writing one only makes
the transcript harder to check.

If a comparable set comes back under {MIN_SAMPLE_SIZE} rows, say so and widen.
If every set does, say that too: insufficient evidence is a finding, and it is
the correct output when the data does not contain the comparison being asked
for. A failing query may be corrected at most {SQL_RETRY_LIMIT} times, so read
the error text rather than guessing again."""


CONVERGE_TASK = f"""YOUR TASK

Turn the query transcript the user provides into an AnalogueEvidenceBundle.

Tools are disabled. Every figure must already appear in the transcript; a number
not returned by one of those queries cannot go in the bundle, however reasonable
it looks.

Each query in the transcript is numbered. For each evidence item:

- query_index is that number -- QUERY 3 is query_index 3. You do not copy the
  SQL; it is attached from the transcript by its number, so cite the number and
  nothing else. An index for a query that is not there is a failed citation.
- metric is "commercial" for an ROI figure, "attention" for an interest one;
- value is the figure itself, copied exactly as the result gave it -- do not
  round it, and do not convert it to a 0-100 score;
- sample_count is the count from the SAME result row: the ROI row count for a
  commercial item, interest_sample_count or countIf(has_interest_signal) for an
  attention item;
- claim states in one sentence what the number says, including which comparable
  set it describes.

Keep items at or above the {MIN_SAMPLE_SIZE}-sample floor. Below that, leave the
item out: it will be discarded by the scorer anyway, and a bundle listing
evidence that does not count is a bundle that reads stronger than it is.

Do not output commercial_score, attention_score or composite. They are not
fields of this schema, and they are computed in code from what you list here.

caveats are prose limits on the comparison -- the era the analogues come from,
the budget band assumed, a thin sample, the fact that interest is measured years
after release. Do not put a number in a caveat that is not already in an
evidence item."""


def analogue_prompt(request: AnalogueScoringRequest) -> str:
    """The user message for the query stage: the proposal and its comparison."""
    proposal = request.proposal
    motifs = ", ".join(m.value for m in proposal.motif_tags)
    archetypes = ", ".join(a.value for a in proposal.character_archetypes)

    band = request.budget_band
    band_line = (
        f"{band} -- {BUDGET_BANDS[band]}" if band
        else "none; do not filter on budget_usd"
    )
    bucket_line = (
        request.target_release_bucket.value if request.target_release_bucket
        else "none; do not filter on release_bucket unless the broad set has "
             "samples to spare, and say so if you do"
    )

    return f"""Score this proposal against historical analogues.

TITLE:        {proposal.title}
LOGLINE:      {proposal.logline}
MOTIF TAGS:   {motifs}
ARCHETYPES:   {archetypes}
ACT STRUCTURE: {proposal.act_structure.value}

COMPARISON SET
Budget band:    {band_line}
Release bucket: {bucket_line}

Budget bands available, as SQL predicates on films:
{_BANDS}

Query the database yourself. Start broad, narrow only while the sample count
survives, and report insufficient evidence rather than a figure standing on
fewer than {MIN_SAMPLE_SIZE} films."""


def build_predict_query_agent(model: str, toolset,
                              before_tool_callback=None) -> Agent:
    """Stage one: tools on, no response schema, prose and figures out.

    before_tool_callback is where app/guardrails.py stops being advisory. The
    runner passes one that refuses a query with a hard violation and hands the
    reason back as the tool result, so the model gets a correction instead of a
    result set it should never have had. The toolset's lifetime belongs to the
    caller -- see app/mcp.py.
    """
    return Agent(
        name="predict_analogue_query",
        model=model,
        instruction=f"{analyst_system_instruction()}\n\n{QUERY_TASK}",
        tools=[toolset],
        before_tool_callback=before_tool_callback,
    )


def build_predict_converge_agent(model: str) -> Agent:
    """Stage two: no tools, AnalogueEvidenceBundle out, still no scores."""
    return Agent(
        name="predict_analogue_converge",
        model=model,
        instruction=f"{analyst_system_instruction()}\n\n{CONVERGE_TASK}",
        tools=[],
        output_schema=AnalogueEvidenceBundle,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2,
            thinking_config=types.ThinkingConfig(
                thinking_budget=SCHEMA_STAGE_THINKING_BUDGET),
        ),
    )


__all__ = [
    "build_predict_query_agent",
    "build_predict_converge_agent",
    "analogue_prompt",
    "QUERY_TASK",
    "CONVERGE_TASK",
]
