# Greenlight Agent

Agentic Cinema hackathon entry — **ClickHouse track**.

A three-layer agent system that aggregates abstract structural motifs across a provable dataset of historical films in ClickHouse, produces evidence-backed treatment proposals grounded in historical analogues, and generates pitch-ready storyboards.

## Core Features & Agents

1. **RecombineAgent**: Autonomously queries ClickHouse via `mcp-clickhouse` to identify high-potential motif and character archetype combinations, generating grounded and wildcard treatment proposals.
2. **Analogue / Evidence Scoring Agent (`PredictAgent`)**: Evaluates proposals by querying ClickHouse for historical analogues, outputting **historical-analogue evidence** (ROI distribution, archetype performance, and sustained-interest percentiles) with full query citations rather than ungrounded "box office predictions".
3. **StoryboardAgent**: Transforms the user-approved treatment into dynamic multi-scene pitch assets (visuals via Imagen and narration via Cloud TTS).

## Dataset & Metrics Scale

- **Dataset**: **1,238 films** released 1990–2014, each carrying a USD budget, a
  USD worldwide gross, and a matched CMU plot summary. Every figure is
  measured, not estimated — the funnel that produced this number is documented
  in [docs/M1_DATA_FINDINGS.md](docs/M1_DATA_FINDINGS.md). The 2014 ceiling is
  a property of the source corpus rather than a filter: the CMU Movie Summary
  Corpus was published in 2013 and holds 70 films from 2013, 4 from 2014, and
  none after.
- **Attention data (`film_attention`)**: ~5.04 million rows of daily pageviews
  from the Wikimedia Pageviews API (2015-07-01 to present, ~4,071 days per
  article).
- **What the attention data measures.** The pageviews API begins in July 2015,
  which is 1 to 25 years after each film in this dataset opened (median 12). So
  these columns describe *cultural persistence* — how much a film was still
  being looked up years later — and not opening-weekend reaction. There is no
  release peak in this window and nothing decays from a premiere. Only
  `interest_cohort_pct`, a percentile within a five-year release cohort, is
  comparable across release years; raw view counts are not, because a 2014 film
  and a 1990 film were measured 24 years apart.

## Data Governance & Attribution

> [!NOTE]
> **Data Governance Notice & Legal Disclaimer**: The following represents an engineering analysis of data provenance and governance architecture, not formal legal advice. Terms of all data sources should be re-verified prior to production deployment or submission.

| Data Source | Content | License / Terms | Governance & Usage |
|---|---|---|---|
| **Wikidata SPARQL** | Film QID, budget, box office revenue, genres, release year | **CC0** | Primary structured spine in ClickHouse. |
| **Wikimedia Pageviews API** | Daily Wikipedia article pageviews (2015-07 to present) | **CC0 / Wikimedia API Terms** | Sustained attention & interest proxy in `film_attention`. |
| **CMU Movie Summary Corpus** | Film plot summaries & Freebase/Wikipedia metadata | **CC BY-SA 3.0** (derived from Wikipedia) | **Ephemeral ETL only**. Raw plot text is processed locally by Gemini Flash to extract high-level abstract motifs. **Raw summaries are never committed to the repository nor stored in ClickHouse**. |

## Status

M0 complete — repository scaffold and MCP round-trip verified.

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

