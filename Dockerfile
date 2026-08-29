# Greenlight -- single Cloud Run container, two Python environments.
#
# The two environments are not tidiness, they are a hard constraint. The ADK
# client needs a newer `mcp` than mcp-clickhouse pins, and installing both into
# one interpreter breaks the ADK import outright -- see app/mcp.py. So the
# server gets /opt/mcp-env and the app gets /opt/app-env, and they never see
# each other's site-packages.
#
# mcp-clickhouse is installed at build time rather than resolved on first use.
# The development path runs `uv run --with mcp-clickhouse`, which downloads the
# package when the agent first needs it; doing that here would put a package
# fetch on the cold-start path of a demo that already pays ~25 seconds for
# ClickHouse to wake up. MCP_SERVER_CMD points at the installed binary instead.

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --- the MCP server, isolated ------------------------------------------------
FROM base AS mcpenv
RUN python -m venv /opt/mcp-env \
 && /opt/mcp-env/bin/pip install --no-cache-dir mcp-clickhouse

# --- the application ---------------------------------------------------------
FROM base AS appenv
RUN python -m venv /opt/app-env \
 && /opt/app-env/bin/pip install --no-cache-dir \
      "google-adk" \
      "mcp<2" \
      "fastapi" \
      "uvicorn[standard]" \
      "google-genai" \
      "google-cloud-storage" \
      "pydantic"

# --- runtime -----------------------------------------------------------------
FROM base AS runtime

COPY --from=mcpenv /opt/mcp-env /opt/mcp-env
COPY --from=appenv /opt/app-env /opt/app-env

# Non-root. The MCP subprocess inherits this user, so nothing in the container
# runs privileged -- and the ClickHouse connection is read-only besides
# (CLICKHOUSE_ALLOW_WRITE_ACCESS=false, set explicitly in app/mcp.py).
RUN useradd --create-home --uid 1001 greenlight
WORKDIR /app
COPY --chown=greenlight:greenlight app/ ./app/
COPY --chown=greenlight:greenlight etl/vocab.py ./etl/vocab.py
COPY --chown=greenlight:greenlight sql/ ./sql/
COPY --chown=greenlight:greenlight web/ ./web/
USER greenlight

# app/prompts.py reads sql/ at startup and etl/vocab.py is the vocabulary
# source of truth, which is why both are copied rather than inlined: a schema
# change reaches the agent's system instruction with no second edit.
ENV PATH="/opt/app-env/bin:${PATH}" \
    PYTHONPATH=/app \
    MCP_SERVER_CMD=/opt/mcp-env/bin/mcp-clickhouse \
    PORT=8080

EXPOSE 8080

# Liveness only, and cheap on purpose. A probe that waits on the MCP warm-up
# fails against a 25-second ClickHouse cold start, kills the container, and
# restarts it into the same cold start. /ready does the warm-up; nothing
# automated should call it.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os;\
urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\").read()"

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
