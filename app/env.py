"""Read `.env` into the process environment.

A tiny loader rather than python-dotenv, because the only consumer that must
never fail to start is the M0 evidence script, and a dependency it does not need
is a dependency that can break the one artefact the submission cannot lose.

`setdefault`, not assignment: a value already exported in the shell wins, so a
one-off `M0_MODEL=... ./scripts/run_m0_roundtrip.sh` works without editing the
file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The connection host identifies the cluster, so SYSTEM_SPEC §8.2 scans git
# history for it alongside the password. Anything logged must be filtered
# through redact().
SECRET_ENV = ("CLICKHOUSE_HOST", "CLICKHOUSE_PASSWORD")


def load_env(path: Path | None = None) -> None:
    env_file = path or ROOT / ".env"
    if not env_file.exists():
        sys.exit("ERROR: .env not found. Copy .env.example and fill it in.")
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def redact(text: str) -> str:
    """Strip credentials so a committed log carries no secrets."""
    for name in SECRET_ENV:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, f"<{name}>")
    return text


__all__ = ["load_env", "redact", "SECRET_ENV", "ROOT"]
