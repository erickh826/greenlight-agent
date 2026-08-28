# M3 Plan: Media And Frontend Demo Surface

**Branch**: `feature/m3-media-frontend-plan`
**Status**: M2 merged to `main`; this plan starts M3.
**Goal**: turn the verified CLI pipeline into a public browser demo without
changing the evidence rules that made M2 pass.

## Non-Negotiable Constraints

- Runtime analysis must still query ClickHouse through `mcp-clickhouse`; the UI
  must show `run_query` SQL, row counts and latency from SSE events.
- AI services stay Google-only: Gemini, Imagen and Cloud TTS. No fallback may
  call a non-Google AI API.
- Competition deploy stays single-instance Cloud Run:
  `--min-instances=1 --max-instances=1 --no-cpu-throttling --timeout=300`.
- HITL approval is not an agent await and not a distributed queue. It is
  `RunStore` state plus `InProcessEventBus` in one process.
- Do not move to Redis, Workflows, Pub/Sub, Cloud Tasks or MCP sidecar until
  Phase 1 browser demo is already stable.

## Current Inputs From M2

- `app.pipeline.run_greenlight()` produces validated grounded and wildcard
  proposals plus `PredictionScore` objects.
- `app.events.InProcessEventBus` already carries `tool_call`, `tool_result`,
  `agent_output`, `error` and `done` events.
- `app.state.RunStore` already has the required state machine:
  `running -> awaiting_approval -> storyboard -> done`.
- `app.contracts.SceneAsset` exists and should be used for media output.

## Task 0 — Demo API/SSE Shell

This is the next implementation task. It is a blocker for everything visual
because it proves a judge can see the agent work from a browser.

Files:

- `app/main.py`
- `tests/test_api.py`
- `tests/test_events.py`
- `pyproject.toml` or equivalent pinned dependency manifest
- `web/index.html` skeleton, only enough to exercise `/run`, `/events/{id}` and
  `/approve/{id}`

Implementation:

1. Add FastAPI endpoints:
   - `GET /` serves `web/index.html`.
   - `POST /run` creates a run, starts the analysis in a background task and
     returns `{run_id}` immediately.
   - `GET /events/{run_id}` streams SSE with no-buffering headers.
   - `POST /approve/{run_id}` accepts `{"variant": "grounded" | "wildcard"}`.
   - `GET /health` checks process liveness and MCP warm-up path.
2. Add public demo protection:
   - one active `/run` at a time with `asyncio.Semaphore(1)` or equivalent
     `RunStore` admission;
   - basic per-IP rate limit for `/run`;
   - bounded prompt length and a fixed default prompt.
3. Adjust the analysis handoff for API use:
   - the CLI can keep emitting terminal `done`;
   - the API path must emit `awaiting_approval` after proposals/scores and keep
     the run open for `/approve`;
   - do not let an early `done` close SSE before media generation.
4. Harden the event bus:
   - cap replay history per run;
   - replay must not raise if history exceeds queue capacity;
   - terminal run cleanup should call `event_bus.close(run_id)`.

DoD:

- Tests use fakes/mocks for Gemini, ClickHouse and media; no paid APIs in unit
  tests.
- Two subscribers can attach to the same run and replay existing events.
- `/run` returns quickly with a `run_id`; SSE then shows at least one
  `tool_call` and `tool_result` in order.
- When analysis finishes, the API publishes `awaiting_approval` with proposals
  and scores; `POST /approve` moves the run to `storyboard`.
- A second concurrent `/run` receives a clear busy response.

## Task 1 — Storyboard Planning

Files:

- `app/agents/storyboard.py`
- `app/media.py`
- `tests/test_media.py`

Implementation:

- Build a no-tools or low-tools StoryboardAgent that turns the approved
  `TreatmentProposal` into exactly three scene descriptions.
- Keep the scene style prefix deterministic so all three images feel like one
  pitch.
- Keep media generation behind the approval gate only; analysis tests must not
  spend Imagen or TTS budget.

DoD:

- Given a fixed proposal, the storyboard planner returns three scene plans that
  validate into `SceneAsset` inputs.
- Planner output contains no unsupported vocabulary and does not change the
  approved proposal's title, variant or core motifs.

## Task 2 — Imagen, TTS And GCS

Files:

- `app/media.py`
- `.env.example`
- `tests/test_media.py`

Implementation:

- Generate one 16:9 image per scene with Imagen.
- Generate one narration audio clip per scene with Cloud TTS Chirp 3 HD.
- Upload assets to GCS under `runs/{run_id}/scene_{n}/...`.
- Return browser-readable URLs. Prefer signed URLs with a demo-safe TTL unless
  bucket policy is intentionally public for the hackathon.
- On media failure, publish an `error` event and keep the approved proposal
  visible; do not silently fake Google AI output.

DoD:

- A mocked media run returns three `SceneAsset` objects with image URL, audio
  URL and positive duration.
- Real smoke test produces three style-consistent 16:9 images and playable
  audio from GCS.

## Task 3 — Browser Experience

Files:

- `web/index.html`
- `web/styles.css` if needed
- `web/app.js` if splitting becomes cleaner than one file

Implementation:

- Show the primary workflow as the first screen: prompt, run status, SQL/event
  stream, proposal comparison, approve, storyboard playback.
- Evidence stream must expose SQL verbatim, returned row count and elapsed time.
- Proposal cards must show `commercial_score`, `attention_score`, `composite`,
  confidence, sample counts and caveats without implying a box-office forecast.
- Ken Burns playback uses CSS transforms and `audio.timeupdate`; no video
  rendering backend.

DoD:

- Desktop and mobile layouts show no overlapping text or hidden buttons.
- A user can run analysis, watch SQL events, compare two proposals, approve one
  and play three scenes.

## Task 4 — Docker And Cloud Run Phase 1

Files:

- `Dockerfile`
- `.dockerignore`
- `README.md`
- deployment notes in `docs/M4_DEPLOYMENT_PROMPT.md`

Implementation:

- Single Cloud Run container with FastAPI app and isolated MCP environment.
- Preinstall `mcp-clickhouse`; do not download it during cold start.
- Non-root runtime user.
- Secret Manager injects ClickHouse credentials.
- `GET /health` performs MCP `SELECT 1` warm-up through the same path agents use.

DoD:

- Local container starts and serves `/health`.
- Cloud Run public URL passes incognito test:
  run -> SSE SQL trace -> proposals -> approve -> storyboard.
- Repo still has no secrets and no non-Google AI dependency.

## Kill Criteria

- If media generation is not reliable by the end of 9/4, ship visual-only
  storyboard using Google-generated stills and clearly mark missing audio in the
  UI.
- If full frontend is not ready by 9/7, ship a static result page plus one demo
  trigger button, but keep the SQL evidence stream.
- If Cloud Run sidecar work threatens the Phase 1 URL, do not start it.

## Verification Commands

```bash
./scripts/run_etl.sh -m pytest tests -q
python3 -m compileall app scripts etl tests
./scripts/run_agent.sh scripts/run_greenlight.py
```

For API implementation, add a local server smoke command once `app/main.py`
exists:

```bash
uvicorn app.main:app --reload
```
