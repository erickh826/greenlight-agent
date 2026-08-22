# Greenlight Agent

Agentic Cinema hackathon entry — **ClickHouse track**.

A three-layer agent system that aggregates abstract structural motifs across a provable dataset of historical films in ClickHouse, produces evidence-backed treatment proposals grounded in historical analogues, and generates pitch-ready storyboards.

## Core Features & Agents

1. **RecombineAgent**: Autonomously queries ClickHouse via `mcp-clickhouse` to identify high-potential motif and character archetype combinations, generating grounded and wildcard treatment proposals.
2. **Analogue / Evidence Scoring Agent (`PredictAgent`)**: Evaluates proposals by querying ClickHouse for historical analogues, outputting **historical-analogue evidence** (ROI distribution, archetype performance, and attention metrics) with full query citations rather than ungrounded "box office predictions".
3. **StoryboardAgent**: Transforms the user-approved treatment into dynamic multi-scene pitch assets (visuals via Imagen and narration via Cloud TTS).

## Dataset & Metrics Scale

- **Target Dataset**: ~1,500 verifiable films (2000–present, English-language, complete Wikidata box office & budget figures matched with CMU plot summaries). This focused, provable dataset avoids credibility gaps while demonstrating real-time analytical power.
- **Attention Data (`film_attention`)**: ~6.1 million rows of daily pageview metrics from the Wikimedia Pageviews API (2015-07 to present, ~4,070 days per article).
- **Wikipedia Page-Interest Proxy**: For films released after July 2015, daily pageviews capture release-window attention and decay; for pre-2015 films, pageviews serve as a sustained post-2015 interest proxy rather than a release-window metric.

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

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Hackathon

- **Event**: [Agentic Cinema: The Blockbuster Hackathon](https://agentic-cinema.devpost.com/)
- **Track**: ClickHouse
- **Platform**: Gemini Enterprise Agent Platform

