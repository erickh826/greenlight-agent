# Greenlight Agent

Agentic Cinema hackathon entry — **ClickHouse track**.

A three-layer agent system that aggregates abstract structural motifs across a provable dataset of historical films in ClickHouse, produces evidence-backed treatment proposals grounded in historical analogues, and generates pitch-ready storyboards.

## Core Features & Agents

1. **RecombineAgent**: Autonomously queries ClickHouse via `mcp-clickhouse` to identify high-potential motif and character archetype combinations, generating grounded and wildcard treatment proposals.
2. **Analogue / Evidence Scoring Agent (`PredictAgent`)**: Evaluates proposals by querying ClickHouse for historical analogues, outputting **historical-analogue evidence** (ROI distribution, archetype performance, and sustained-interest percentiles) with full query citations rather than ungrounded "box office predictions".
3. **StoryboardAgent**: Transforms the user-approved treatment into a checked
   three-beat plan, then into pitch assets — 16:9 frames and narration, both
   generated on Vertex AI and served from Cloud Storage.

Every runtime figure comes from a query the browser shows verbatim, with the row
count and the latency it took. **The score is a historical-analogue score, not a
box-office forecast**, and nothing in the interface implies otherwise.

## Dataset & Metrics Scale

- **Dataset**: **1,238 films** released 1990–2014, each carrying a USD budget, a
  USD worldwide gross, and a matched CMU plot summary. Every figure is
  measured, not estimated — the funnel that produced this number is documented
  in [docs/M1_DATA_FINDINGS.md](docs/M1_DATA_FINDINGS.md). The 2014 ceiling is
  a property of the source corpus rather than a filter: the CMU Movie Summary
  Corpus was published in 2013 and holds 70 films from 2013, 4 from 2014, and
  none after.
- **Attention data (`film_attention`)**: **4,937,204 rows** of daily pageviews
  from the Wikimedia Pageviews API (2015-07-01 to present, up to 4,074 days per
  article), covering 1,238 of 1,238 films.
- **What the attention data measures.** The pageviews API begins in July 2015,
  which is 1 to 25 years after each film in this dataset opened (median 12). So
  these columns describe *cultural persistence* — how much a film was still
  being looked up years later — and not opening-weekend reaction. There is no
  release peak in this window and nothing decays from a premiere.
- **A correction we are keeping visible.** The design assumed that lag would
  make raw counts incomparable across release years, and a 25-film sample
  appeared to confirm it (r = +0.272). Over all 1,238 films it is **r = −0.009**:
  how popular a film is swamps how long ago it came out, and the 25-film sample
  was a non-random slice. `interest_cohort_pct` is therefore a scale
  normalisation giving `attention_score` a bounded 0–1 input — not a lag
  correction. The full measurement is in
  [docs/M1_DATA_FINDINGS.md](docs/M1_DATA_FINDINGS.md) §1.

## Data Governance & Attribution

> [!NOTE]
> **Data Governance Notice & Legal Disclaimer**: The following represents an engineering analysis of data provenance and governance architecture, not formal legal advice. Terms of all data sources should be re-verified prior to production deployment or submission.

| Data Source | Content | License / Terms | Governance & Usage |
|---|---|---|---|
| **Wikidata SPARQL** | Film QID, budget, box office revenue, genres, release year | **CC0** | Primary structured spine in ClickHouse. |
| **Wikimedia Pageviews API** | Daily Wikipedia article pageviews (2015-07 to present) | **CC0 / Wikimedia API Terms** | Sustained attention & interest proxy in `film_attention`. |
| **CMU Movie Summary Corpus** | Film plot summaries & Freebase/Wikipedia metadata | **CC BY-SA 3.0** (derived from Wikipedia) | **Ephemeral ETL only**. Raw plot text is processed locally by Gemini Flash to extract high-level abstract motifs. **Raw summaries are never committed to the repository nor stored in ClickHouse**. |

## Architecture

```
[Browser]  POST /run · GET /events/{id} (SSE) · POST /approve/{id}
    │
┌───▼──────── Cloud Run, one instance ──────────────────────────────────┐
│  FastAPI (app/main.py)                                                │
│    ├─ RunStore          run state machine, in memory                  │
│    ├─ InProcessEventBus SSE fan-out, replayed to late subscribers     │
│    ├─ ANALYSIS_SLOT     one analysis at a time; the approval gate     │
│    │                    holds no slot, so a visitor who walks away    │
│    │                    does not lock the demo                        │
│    └─ MEDIA_SLOT        one storyboard render at a time               │
│                                                                       │
│  app/pipeline.py — the run, in order                                  │
│    RecombineAgent phase A   tools on, queries ClickHouse itself       │
│    RecombineAgent phase B   tools off, one grounded + one wildcard    │
│    PredictAgent             tools on, retrieves analogues             │
│    app/scoring.py           the score, computed in Python             │
│    ── approval gate ──                                                │
│    StoryboardAgent          three beats, validated before spending    │
│    app/media.py             frames + narration → Cloud Storage        │
│                                                                       │
│  /opt/app-env  google-adk + mcp<2        /opt/mcp-env  mcp-clickhouse │
└──────┬──────────────────────────────────────────┬────────────────────┘
       │ stdio MCP                                 │
  ClickHouse Cloud                          Vertex AI + GCS
```

Two Python environments in one image is a constraint, not tidiness: the ADK
needs a newer `mcp` than `mcp-clickhouse` pins, and one interpreter holding both
breaks the ADK import outright.

**Google-only.** Gemini for reasoning, `gemini-2.5-flash-image` for frames,
`gemini-2.5-flash-preview-tts` for narration. Not Imagen and not Cloud
Text-to-Speech: every Imagen publisher model returns 404 on this project, and
the Cloud TTS API is disabled behind a Service Usage API that is also disabled.
Both replacements sit on the Vertex surface the agents already authenticate
against — see `app/media.py`.

## Running it

```bash
# Locally, against ClickHouse Cloud and Vertex AI.
./scripts/serve.sh                       # http://127.0.0.1:8080

# The whole pipeline on the command line, no browser.
./scripts/run_agent.sh scripts/run_greenlight.py

# Tests: no Gemini, no ClickHouse, no media, no billing.
./scripts/run_etl.sh -m pytest tests -q
```

`GET /health` is liveness only and deliberately cheap. `GET /ready` runs the MCP
warm-up and reports what it cost — never put it behind a startup probe, because
the ClickHouse cold path has been measured at 25 seconds and a probe that waits
on it kills the container and restarts it into the same cold start.

## Deploying

```bash
PROJECT=$(gcloud config get-value project)
REGION=us-central1

# The runtime service account reads the secrets, calls Vertex and writes the
# asset bucket. Without these the container starts and every run fails.
SA=$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com
for ROLE in secretmanager.secretAccessor aiplatform.user storage.objectAdmin; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$SA" --role="roles/$ROLE" --condition=None
done

gcloud artifacts repositories create greenlight \
  --repository-format=docker --location=$REGION

gcloud builds submit --tag \
  $REGION-docker.pkg.dev/$PROJECT/greenlight/greenlight:latest

gcloud run deploy greenlight \
  --image=$REGION-docker.pkg.dev/$PROJECT/greenlight/greenlight:latest \
  --region=$REGION --allow-unauthenticated \
  --min-instances=1 --max-instances=1 \
  --no-cpu-throttling --timeout=900 --concurrency=10 \
  --memory=2Gi --cpu=2 \
  --set-env-vars=GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION,GCS_BUCKET=greenlight-agent-demo,GCS_PUBLIC_ASSETS=true,CLICKHOUSE_USER=default,CLICKHOUSE_PORT=8443,CLICKHOUSE_SECURE=true,CLICKHOUSE_DATABASE=default,MODEL_FAST=gemini-2.5-flash,MODEL_IMAGE=gemini-2.5-flash-image,MODEL_TTS=gemini-2.5-flash-preview-tts,MODEL_TTS_VOICE=Charon \
  --set-secrets=CLICKHOUSE_HOST=clickhouse-host:latest,CLICKHOUSE_PASSWORD=clickhouse-password:latest
```

`--timeout=900` is about the SSE stream, not about slow requests. A full run
measured 335 seconds when this was first deployed, and at the specified 300 the
connection was cut before `media_ready` -- the browser recovers, because
`EventSource` reconnects and the bus replays history to a new subscriber, but
the primary path should not depend on the backstop. A run is now about **168
seconds** (roughly 115 to the two scored proposals, the rest to the storyboard),
so 900 is headroom rather than necessity, and is kept as headroom.

`--min-instances=1 --max-instances=1` is not a cost setting. `RunStore` and
`InProcessEventBus` live in one process's memory, so a second instance would
serve `/events` for runs it has never heard of and the stream would hang open
producing nothing — no error, no disconnect.

Credentials never enter the image: `.env` is in `.dockerignore`, and Cloud Run
injects the two secrets from Secret Manager.

## Status

**Live: https://greenlight-277057547230.us-central1.run.app**

M0–M3 complete. The full path — analysis, two scored proposals, approval,
storyboard — has been verified inside the production container against live
ClickHouse, Vertex AI and Cloud Storage.

## Quick start (M0)

```bash
# 1. Copy env template and fill in ClickHouse Cloud credentials
cp .env.example .env

# 2. Test MCP server connectivity
./scripts/test_mcp_clickhouse.sh

# 3. Run full ADK <-> MCP <-> Gemini round trip
./scripts/run_m0_roundtrip.sh
```

## Building the dataset (M1)

```bash
# 1. Wikidata spine — films with both a USD budget and a USD gross
./scripts/run_etl.sh etl/01_wikidata_spine.py --since-year 1990 --until-year 2014

# 2. CMU corpus (CC BY-SA 3.0; fetched at ETL time, never committed)
./scripts/fetch_cmu_corpus.sh

# 3. Join on normalised title + release year within +/-1
./scripts/run_etl.sh etl/02_cmu_join.py --spine data/films_spine.parquet

# 4. Pageviews and the derived interest columns
./scripts/run_etl.sh etl/03_pageviews.py

# Check the result
./scripts/run_etl.sh scripts/qa_m1_data.py
```

Every stage takes `--limit` for a thin-slice run.

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Hackathon

- **Event**: [Agentic Cinema: The Blockbuster Hackathon](https://agentic-cinema.devpost.com/)
- **Track**: ClickHouse
- **Platform**: Gemini Enterprise Agent Platform

