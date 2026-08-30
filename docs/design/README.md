# Greenlight UI redesign — screens for review

Six mockups answering `suggestion/Greenlight UI UX Review.md`. **Static hi-fi,
not implemented.** Nothing in `web/index.html` has changed.

Live canvas (pan/zoom, PNG/PDF export):
https://claude.ai/code/artifact/9a0efd61-55f0-4c52-b338-a5f48afad888

| Screen | File | What it is |
|---|---|---|
| 01 Brief | `screens/brief.png` | Warm editorial entry — `#F7F6F2`, the only light screen |
| 02 Live analysis | `screens/analysis.png` | Stepper with findings, replacing the raw log panel |
| 03 Treatments | `screens/main.png` | **The decision screen.** Recommendation strip + two cards |
| 04 Storyboard | `screens/storyboard.png` | Three-beat sequence, caption in the flow |
| P0 card | `screens/insufficientevidence.png` | The insufficient-evidence state |
| Mobile | `screens/treatmentsmobile.png` | Treatments at 390px |

Sources are the `.dc.html` files beside this README — plain HTML with inline
styles, no build step. `canvas.json` is the canvas layout only.

## What the review asked for, and what these do about it

**P0 · the `N/A` card.** The shipped card showed three `N/A` scores, `confidence
—`, `0 evidence items`, and a `storyboard below ↓` CTA at once, which reads as a
crash rather than an honest limit. `insufficientevidence.png` drops the score
block entirely: no `N/A`, no empty bar, no zero. It states that no score is
shown, why (3 films, under the 8-film floor), and that this is *not a low score
because nothing was measured*. The CTA reads "Continue on creative grounds →" so
it cannot be read as data approving it, and "Show the 0 figures and their SQL"
becomes "Why no comparable set was found".

**P0 · scores without meaning.** `54.2` alone told a first-time reader nothing.
Every score now carries a median tick on its bar, the sentence "Above the median
of this run's 22-film analogue set (47.0)", and an evidence count. Above both
cards a recommendation strip names which treatment to pursue and why, in the
project's existing vocabulary — "analogue set", "commercial-resilience signal" —
never "likely to earn".

**P0 · the hidden caption.** The caption now sits in the document flow inside
the same bordered block as the frame, so no fixed player can cover it.

**P1 · narrative DNA.** Cards read "A hidden conspiracy surfacing through a loss
of innocence, carried by an authority figure and the caregiver who protects her"
instead of `hidden_conspiracy · loss_of_innocence · authority_figure`. **This is
the one item with no implementation behind it** — see Open questions.

**P1 · the brief screen.** "Find historical analogues →" replaces "Run
analysis"; the placeholder is a real example brief; three starting-point chips;
the default prompt demoted to "Use sample brief". A right-hand column shows the
five stages of a run, so the value is legible before anything is typed.

**P2 · the shell.** Warm `#F7F6F2` for the brief only; `#0A0A0A` production
stage from analysis onward. Amber `#FFB84D` is reserved for the recommendation
and the running step — nothing else uses it. Grounded/wildcard colour appears
only as a 2px top hairline, a label and the score bar; the cards are not tinted.

## Where these deviate from the review

- **Typography.** The review says Inter or system sans. These use **Instrument
  Serif** for creative language (headlines, treatment titles, narration), system
  sans for UI, IBM Plex Mono for evidence. A serif reads as film development
  rather than another product dashboard, and it separates the creative layer
  from the evidence layer without another colour. Swap it if you disagree — it
  is two `font-family` declarations per file.
- **Two screens, not one, for the evidence states.** The review folds the
  insufficient-evidence case into the main comparison example. These keep the
  both-scored case as the main screen, because that is the common path, and give
  the insufficient state its own artboard.

## What is real and what is drafted

- **Real:** the storyboard frames (a live run, bucket `runs/a94f9da825e6`), the
  scores `54.2` / `45.9` (from `docs/m2-greenlight-run.json`), the SQL shown in
  the analysis stepper, `29.5 s database time`, `21 queries`, the 8-film floor,
  and the `5.68x across 13 films` / `3.51x across 25` figures.
- **Drafted:** all titles, loglines, narration and the `47.0` median. The median
  is plausible but was not computed — if it goes into the product it has to come
  from the run.

## Open questions for whoever picks this up

1. **Narrative DNA needs a source.** Turning `hidden_conspiracy` into readable
   prose needs either a hand-written mapping for all 55 vocabulary terms, or a
   model call per proposal. A mapping is deterministic, free and testable; a
   model call reads better and is another thing to validate. Not decided here.
2. **The benchmark median has to be computed.** `app/scoring.py` does not
   currently produce a median for the run's analogue set. It is a small addition
   to `score_from_evidence`, but it is a real change, not a UI change.
3. **The analysis stepper needs stage names in the event stream.** Today
   `agent_start` carries an agent name (`predict_analogue_query`), not a
   human-readable stage. Either the frontend maps agent names to labels, or the
   pipeline emits a label.
4. **"Directional evidence" vs "Evidence-backed" needs a rule.** These mockups
   show both, but nothing defines the threshold between them.

## Regenerating

Sources here are the working files. The canvas is re-seeded from them with the
`/design` skill; the PNGs are rendered by lifting the markup out of the `<x-dc>`
wrapper and screenshotting at frame size (2x).
