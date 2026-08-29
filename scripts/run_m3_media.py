#!/usr/bin/env python3
"""M3 Task 2: render a checked StoryboardPlan into Google media assets.

This script spends money: three Imagen calls, three Cloud TTS calls and six GCS
uploads. It therefore requires --yes. The unit-test path uses fakes; this is the
manual real smoke before a demo.

Usage:
    ./scripts/run_agent.sh scripts/run_m3_media.py --yes
    ./scripts/run_agent.sh scripts/run_m3_media.py --plan docs/m3-storyboard-plan.json --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import SCENE_COUNT  # noqa: E402
from app.contracts import StoryboardPlan  # noqa: E402
from app.env import load_env, redact  # noqa: E402
from app.media import compose_image_prompt, render_storyboard_media  # noqa: E402

PLAN_PATH = ROOT / "docs" / "m3-storyboard-plan.json"
ASSETS_PATH = ROOT / "docs" / "m3-media-assets.local.json"
TRACE_PATH = ROOT / "docs" / "m3-media-trace.local.log"


def _redact_signed_urls(text: str) -> str:
    return re.sub(r"(https?://\S+?)\?\S+", r"\1?<signed-url>", text)


class Trace:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("w", encoding="utf-8")

    def write(self, line: str = "") -> None:
        line = _redact_signed_urls(redact(str(line)))
        print(line)
        self.handle.write(line + "\n")
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


def load_plan(path: Path) -> StoryboardPlan:
    return StoryboardPlan.model_validate_json(path.read_text(encoding="utf-8"))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", default=str(PLAN_PATH))
    ap.add_argument("--output", default=str(ASSETS_PATH))
    ap.add_argument("--trace", default=str(TRACE_PATH))
    ap.add_argument("--run-id",
                    default="m3-media-" + datetime.now(timezone.utc).strftime(
                        "%Y%m%d%H%M%S"))
    ap.add_argument("--yes", action="store_true",
                    help="actually call Imagen, Cloud TTS and GCS")
    args = ap.parse_args()

    load_env()
    plan = load_plan(Path(args.plan))
    trace = Trace(Path(args.trace))
    try:
        trace.write("=== M3 Task 2: media generation smoke ===")
        trace.write(f"timestamp: {datetime.now(timezone.utc).isoformat()}")
        trace.write(f"run_id:    {args.run_id}")
        trace.write(f"plan:      {args.plan}")
        trace.write(f"output:    {args.output}")

        trace.write("\n--- Prompts ---")
        for scene in plan.scenes:
            trace.write(f"[{scene.scene_index}] "
                        f"{compose_image_prompt(plan, scene)}")

        if not args.yes:
            trace.write("\nresult: DRY RUN -- pass --yes to spend media calls.")
            return 0

        trace.write("\n--- Generation ---")
        assets = await render_storyboard_media(
            args.run_id, plan, progress=trace.write)
        payload = {
            "run_id": args.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "plan_path": args.plan,
            "assets": [a.model_dump(mode="json") for a in assets],
        }
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n",
                                     encoding="utf-8")

        checks = [
            (len(assets) == SCENE_COUNT, f"{SCENE_COUNT} scene assets",
             f"{len(assets)} assets"),
            (all(a.image_url.startswith("http") for a in assets),
             "image URLs are browser-readable", "ok"),
            (all(a.audio_url.startswith("http") for a in assets),
             "audio URLs are browser-readable", "ok"),
            (all(a.duration_sec > 0 for a in assets),
             "audio durations are positive",
             ", ".join(f"{a.duration_sec:.1f}s" for a in assets)),
        ]

        trace.write("\n--- Assets ---")
        for asset in assets:
            trace.write(json.dumps(asset.model_dump(mode="json"),
                                   ensure_ascii=False))

        trace.write("\n=== Media DoD ===")
        for ok, name, detail in checks:
            trace.write(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

        passed = all(ok for ok, _, _ in checks)
        trace.write("\nresult: " + ("PASS" if passed else "FAIL"))
        return 0 if passed else 1
    finally:
        trace.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
