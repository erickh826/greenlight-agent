# M2 PredictAgent Plan: Analogue / Evidence Scoring

Date: 2026-08-29

Goal: build the blocking M2 agent that scores a `TreatmentProposal` by
retrieving historical analogues from ClickHouse, then deriving
`PredictionScore` from evidence. This is not a box-office forecast. It is an
auditable historical-analogue score.

## Decision

`docs/M2_PHASE_A_PLAN.md` is not enough for this work.

That document is still useful as the proof that Gemini can query ClickHouse
through MCP and that a no-tools schema convergence pass works. But it explicitly
puts `PredictAgent` scoring out of Phase A scope, and PredictAgent has different
failure modes:

- it must assemble analogue filters from a proposal, not search for raw ideas;
- it must tolerate SQL errors and retry no more than two times;
- it must emit numbers that Python can recompute from `EvidenceItem`;
- it must return `insufficient_evidence` rather than a confident-looking score
  when comparable sets are too thin.

## Starting Point

Already available:

- `docs/m2-grounded-proposal.json`: one grounded `TreatmentProposal`.
- `app/contracts.py`: `EvidenceItem`, `TreatmentProposal`, `PredictionScore`.
- `app/scoring.py`: pure scoring functions; missing dimensions are `None`, not
  zero.
- `app/prompts.py`: DDL, controlled vocabulary, sample-size guidance, interest
  caveats and ClickHouse `-Merge` examples.
- `app/mcp.py`: isolated `mcp-clickhouse` toolset and `warm_up()`.
- `app/guardrails.py`: live SQL checks for dangerous or misleading query shapes.
- `app/proposal_validation.py`: Phase B grounding checks.

Missing:

- `PredictAgent` factory and task instruction.
- A scoring runner that invokes tools, captures retries, and leaves a trace.
- A small contract for the scoring input, because `TreatmentProposal` has no
  explicit target release bucket or budget band.
- Post-run validation that `PredictionScore` matches `app/scoring.py` output.
- An `insufficient_evidence` regression path.

## Ownership Boundary

The split is load-bearing:

- **Model owns query strategy**: choose which comparable sets to inspect, build
  valid ClickHouse SQL, interpret result rows into evidence candidates.
- **Program owns scoring**: convert accepted evidence into
  `commercial_score`, `attention_score`, `composite`, `confidence`, and
  caveats via `app/scoring.py`.
- **Verifier owns trust**: every evidence item must name the SQL that produced
  it, pass guardrails, meet the sample floor, and use the correct count for the
  metric.

Never trust a score written by the model. If the model emits one, it is treated
as commentary and discarded.

## Runtime Shape

PredictAgent should follow the same two-stage pattern as RecombineAgent:

1. **Query phase**: tools on, no `output_schema`.
   - Input: a proposal JSON plus optional scoring constraints.
   - Gemini calls `run_query` through `mcp-clickhouse`.
   - Runner logs every function call, function response, error, retry and final
     synthesis.
   - Runner stops after the third SQL failure: original attempt plus two
     retries.

2. **Convergence/scoring phase**: no tools.
   - Parse evidence candidates from the query phase into `EvidenceItem`.
   - Validate the evidence against the trace.
   - Compute `PredictionScore` in Python using `app/scoring.score_from_evidence`.
   - Write the final JSON and trace.

Do not combine tool calling and `output_schema` in one model call.

## Scoring Input

Add a small request contract or equivalent runner input:

```python
class AnalogueScoringRequest(BaseModel):
    proposal: TreatmentProposal
    target_release_bucket: ReleaseBucket | None = None
    budget_band: Literal["micro", "low", "mid", "high"] | None = "mid"
```

Default behavior for the demo:

- `budget_band="mid"` if the user gives no budget.
- no release bucket filter at first; release bucket is a narrowing step only if
  the broad set survives the sample floor.
- use the proposal's motif tags and character archetypes as the primary
  analogue signals.

Budget bands should be implemented in SQL as explicit `budget_usd` predicates,
not hidden in prose. Choose conservative fixed bands unless data quantiles prove
more stable:

| Band | Predicate |
|---|---|
| micro | `budget_usd < 5000000` |
| low | `budget_usd >= 5000000 AND budget_usd < 20000000` |
| mid | `budget_usd >= 20000000 AND budget_usd < 80000000` |
| high | `budget_usd >= 80000000` |

These are filters for analogue retrieval, not claims about the proposal's actual
production budget.

## Query Strategy

The agent should assemble queries from broad to narrow:

1. **Broad motif-pair evidence** from `mv_motif_pair_stats`.
   - For every pair inside `proposal.motif_tags`, check ROI and interest.
   - Require `HAVING n_roi >= 8`.
   - Interest evidence uses `n_interest`, not `n_roi`.

2. **Broad archetype evidence** from `mv_archetype_performance`.
   - For every proposal archetype, check ROI and interest.
   - Start without `release_bucket`.
   - Add release bucket only if a target bucket exists and the broad result has
     enough samples.

3. **Film-level analogue set** from `films`.
   - Use `has(motif_tags, ...)` and `has(character_archetypes, ...)`.
   - Start with a relaxed intersection: at least one selected motif pair plus
     one selected archetype.
   - Add `budget_usd` band if the broad count survives.
   - Add `release_bucket` last.
   - Compute:
     - `count() AS n_roi`
     - `quantile(0.5)(roi) AS roi_median`
     - `quantile(0.75)(roi) AS roi_p75`
     - `countIf(has_interest_signal) AS n_interest`
     - `quantileIf(0.5)(interest_cohort_pct, has_interest_signal) AS interest_median`

4. **Optional concrete analogues** from `films`.
   - Return up to five titles for UI explanation.
   - Do not feed individual outliers directly into the score unless the
     aggregate sample floor is met.

## SQL Error Retry

Use `SQL_RETRY_LIMIT = 2` from `app/config.py`.

Definition:

- attempt 0: first bad query;
- retry 1 and retry 2: model receives the ClickHouse error text verbatim and may
  correct the query;
- after retry 2 fails, stop the PredictAgent run and emit
  `PredictionScore(confidence="insufficient_evidence", scores=None...)`.

The trace must show:

- original failing SQL;
- ClickHouse error text;
- retry count;
- corrected SQL or final insufficient evidence.

Guardrail violations count as failures even if ClickHouse would run the SQL.

## Evidence Mapping

Every accepted evidence item must be classified before scoring:

| Metric | Source | Count field | Value field | Score function |
|---|---|---|---|---|
| commercial | ROI aggregate | `n_roi` / `sample_count` | `roi_median` | `roi_to_score()` |
| attention | sustained interest | `n_interest` / `interest_sample_count` | `interest_median` | `cohort_pct_to_score()` |

Rules:

- evidence below `MIN_SAMPLE_SIZE` is kept in the trace but excluded from
  scoring;
- if no commercial evidence survives, `commercial_score=None`;
- if no attention evidence survives, `attention_score=None`;
- if neither survives, all scores are `None` and `confidence="insufficient_evidence"`;
- if exactly one dimension survives, composite equals that dimension and
  confidence is capped at `medium`.

## Implementation Tasks

### Task 1 — Contract And Fixtures

- Add `AnalogueScoringRequest` if the runner needs structured input.
- Keep `PredictionScore` unchanged unless implementation shows a real missing
  field.
- Add fixture input using `docs/m2-grounded-proposal.json`.

DoD:

- tests confirm scoring request defaults are stable;
- existing `PredictionScore` tests still pass.

### Task 2 — PredictAgent Query Factory

- Add `app/agents/predict.py`.
- Compose `analyst_system_instruction()` with a PredictAgent task instruction.
- Tools on, no `output_schema`.
- Instruction requires broad-to-narrow analogue search and explicit sample
  counts.

DoD:

- factory imports cleanly under `./scripts/run_agent.sh`;
- no global MCP toolset is created at import time.

### Task 3 — Phase A Runner For Scoring

- Add `scripts/run_m2_predict_agent.py`.
- Load proposal JSON and optional `--budget-band`, `--release-bucket`.
- Build MCP toolset, run `warm_up()`, start ADK runner.
- Capture tool calls/responses like `run_m2_recombine_phase_a.py`.
- Apply `app.guardrails.inspect()` live to every SQL.
- Count SQL/tool errors and stop after `SQL_RETRY_LIMIT + 1` failed attempts.

DoD:

- trace shows at least three successful query surfaces:
  `mv_motif_pair_stats`, `mv_archetype_performance`, and `films`;
- no hard guardrail violations;
- no unbounded `film_attention` scan;
- SQL errors, if any, are followed by no more than two retries.

### Task 4 — Evidence Extraction And Validation

- Extract evidence candidates from successful result payloads.
- Validate that every `EvidenceItem.sql_query` appears in the trace.
- Validate `source_view`, metric/count pairing and sample floor.
- Add tests for:
  - ROI evidence uses ROI count;
  - interest evidence uses interest count;
  - below-floor evidence is excluded from scoring;
  - invented vocabulary terms fail.

DoD:

- unit tests cover both passing and failing evidence.

### Task 5 — Programmatic Scoring

- Convert accepted evidence into commercial and attention lists.
- Call `app.scoring.score_from_evidence()`.
- Build `PredictionScore` from the returned tuple.
- Do not use any score the model wrote in prose.

DoD:

- test recomputes `PredictionScore.composite` from evidence and matches;
- missing attention produces `attention_score=None`, not `0.0`;
- no surviving evidence produces `insufficient_evidence`.

### Task 6 — End-To-End Acceptance Trace

- Write `docs/m2-predict-agent-trace.log`.
- Write `docs/m2-prediction-score.json`.
- Update `MILESTONES.md` with command output and caveats.

DoD:

- `python3 -m pytest tests -q` passes;
- `python3 -m compileall app scripts etl tests` passes;
- `./scripts/run_agent.sh scripts/run_m2_predict_agent.py` passes;
- trace shows runtime ClickHouse queries and retry accounting;
- final score is a valid `PredictionScore` with evidence SQL attached.

## Suggested First Prompt

```text
Score this proposal as historical analogues, not as a forecast. Query
ClickHouse yourself. Start broad, then narrow by budget band and release bucket
only if the sample count survives. Use ROI evidence and sustained-interest
evidence separately, and report insufficient evidence rather than guessing.
```

## Risks

- **The proposal has no explicit budget or target era.** Keep budget band and
  release bucket as runner inputs with conservative defaults rather than
  inventing them from the logline.
- **The agent may over-narrow.** Runner should accept broad evidence and reject
  below-floor narrow evidence instead of forcing exact intersections.
- **The model may claim scores.** Ignore model-written scores; compute in Python.
- **Interest count confusion can recur.** Validator must fail an interest
  evidence item that cites `sample_count`/`n_roi` instead of
  `interest_sample_count`/`n_interest`.
- **Retry loops can burn time.** `SQL_RETRY_LIMIT` is hard; third failure becomes
  `insufficient_evidence`.

## Kill Criteria

If the full autonomous query path is not stable by the end of 2026-09-01:

- keep the programmatic scorer;
- replace PredictAgent's autonomous query planning with a small fixed query
  template set;
- preserve MCP runtime calls and evidence SQL in the trace.

If end-to-end root orchestration is not stable by 2026-09-02 evening, cut the
wildcard proposal and score only the grounded proposal.
