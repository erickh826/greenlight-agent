# Greenlight Agent

Agentic Cinema hackathon entry — **ClickHouse track**.

A three-layer agent system that aggregates abstract structural motifs across historical films in ClickHouse, produces evidence-backed treatment proposals, and generates pitch-ready storyboards.

## Status

M0 in progress — repo scaffold and MCP connectivity.

## Quick start (M0)

```bash
# 1. Copy env template and fill in ClickHouse Cloud credentials
cp .env.example .env

# 2. Test MCP server connectivity
./scripts/test_mcp_clickhouse.sh
```

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Hackathon

- **Event**: [Agentic Cinema: The Blockbuster Hackathon](https://agentic-cinema.devpost.com/)
- **Track**: ClickHouse
- **Platform**: Gemini Enterprise Agent Platform
