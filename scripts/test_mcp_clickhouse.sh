#!/usr/bin/env bash
# Test mcp-clickhouse connectivity by listing databases via the MCP protocol.
#
# Prerequisites:
#   - uv installed (https://docs.astral.sh/uv/)
#   - .env file with ClickHouse Cloud credentials
#
# Usage:
#   ./scripts/test_mcp_clickhouse.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found. Copy .env.example and fill in ClickHouse credentials:"
  echo "  cp .env.example .env"
  exit 1
fi

# shellcheck disable=SC1091
set -a
source .env
set +a

for var in CLICKHOUSE_HOST CLICKHOUSE_USER CLICKHOUSE_PASSWORD; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: $var is empty in .env"
    exit 1
  fi
done

export CLICKHOUSE_PORT="${CLICKHOUSE_PORT:-8443}"
export CLICKHOUSE_SECURE="${CLICKHOUSE_SECURE:-true}"
export CLICKHOUSE_DATABASE="${CLICKHOUSE_DATABASE:-default}"

echo "Connecting to ${CLICKHOUSE_HOST}:${CLICKHOUSE_PORT} as ${CLICKHOUSE_USER}..."

export PATH="${HOME}/.local/bin:${PATH}"

# Send MCP initialize + tools/call to list databases
python3 - <<'PY'
import json
import os
import subprocess
import sys

env = {**os.environ}
proc = subprocess.Popen(
    ["uv", "run", "--with", "mcp-clickhouse", "mcp-clickhouse"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env=env,
)

def send(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()

def recv():
    line = proc.stdout.readline()
    if not line:
        err = proc.stderr.read()
        print(err, file=sys.stderr)
        sys.exit(1)
    return json.loads(line)

send({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "m0-test", "version": "0.1.0"},
    },
})
init_resp = recv()
print("initialize:", json.dumps(init_resp, indent=2))

send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

send({
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {},
})
tools_resp = recv()
print("tools:", json.dumps(tools_resp, indent=2))

send({
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
        "name": "run_select_query",
        "arguments": {"query": "SHOW DATABASES"},
    },
})
query_resp = recv()
print("SHOW DATABASES:", json.dumps(query_resp, indent=2))

proc.terminate()
proc.wait(timeout=5)

if "error" in query_resp:
    sys.exit(1)

print("\n✓ MCP server connected and listed databases successfully.")
PY
