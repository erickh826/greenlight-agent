"""Tests for the event bus, which the browser demo depends on entirely.

Every failure here is invisible from the CLI: it publishes to the bus and never
subscribes to it, so a bug in delivery shows up for the first time as a browser
that stops updating halfway through a run.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app.events import (  # noqa: E402
    TERMINAL_EVENTS, InProcessEventBus, make_event)


def drain(bus: InProcessEventBus, run_id: str, limit: int = 100) -> list:
    """Collect from a subscription until it ends.

    Bounded by a timeout on purpose: the failure mode being tested for is a
    stream that never closes, and a test that hangs reports that as a hung
    suite rather than as a failure.
    """
    async def go():
        out = []
        async for event in bus.subscribe(run_id):
            out.append(event)
            if len(out) >= limit:
                break
        return out
    return asyncio.run(asyncio.wait_for(go(), timeout=5))


def test_recoverable_events_do_not_end_the_stream():
    """The bug this whole file exists for.

    The pipeline retries a rejected Phase B proposal and continues past a failed
    variant. Both were published as "error", and "error" ends a subscription --
    so the first retry closed every browser's stream while the CLI ran to
    completion, and nothing in the M2 acceptance run could see it.
    """
    assert "agent_retry" not in TERMINAL_EVENTS
    assert "stage_failed" not in TERMINAL_EVENTS
    assert "tool_error" not in TERMINAL_EVENTS
    assert "awaiting_approval" not in TERMINAL_EVENTS

    bus = InProcessEventBus()
    for kind in ("agent_start", "agent_retry", "stage_failed", "tool_error",
                 "awaiting_approval", "done"):
        bus.publish("r", make_event(kind))
    bus.close("r")

    assert [e["type"] for e in drain(bus, "r")] == [
        "agent_start", "agent_retry", "stage_failed", "tool_error",
        "awaiting_approval", "done"]


def test_terminal_event_ends_the_stream():
    bus = InProcessEventBus()
    bus.publish("r", make_event("agent_start"))
    bus.publish("r", make_event("done"))
    bus.publish("r", make_event("agent_output", message="after the end"))
    bus.close("r")
    assert [e["type"] for e in drain(bus, "r")] == ["agent_start", "done"]


def test_two_subscribers_both_get_the_backlog():
    bus = InProcessEventBus()
    bus.publish("r", make_event("tool_call", args={"query": "SELECT 1"}))
    bus.publish("r", make_event("done"))
    bus.close("r")
    first, second = drain(bus, "r"), drain(bus, "r")
    assert first == second
    assert first[0]["args"]["query"] == "SELECT 1"


def test_replay_survives_more_history_than_the_queue_holds():
    """Subscribing must not raise because the writer was busy.

    The first version put every past event into a bounded queue before yielding
    any of them, so a long run made subscribe() throw QueueFull -- the reader
    paying for the writer's volume.
    """
    bus = InProcessEventBus()
    for i in range(bus.MAX_QUEUED + 500):
        bus.publish("r", make_event("agent_output", message=str(i)))
    bus.publish("r", make_event("done"))
    bus.close("r")

    events = drain(bus, "r", limit=bus.MAX_HISTORY + 10)
    assert events[-1]["type"] == "done"
    assert len(events) <= bus.MAX_HISTORY


def test_history_is_capped_but_keeps_the_most_recent():
    bus = InProcessEventBus()
    for i in range(bus.MAX_HISTORY + 50):
        bus.publish("r", make_event("agent_output", message=str(i)))
    kept = [e["message"] for e in bus._history["r"]]
    assert len(kept) == bus.MAX_HISTORY
    assert kept[-1] == str(bus.MAX_HISTORY + 49)


def test_subscribing_after_the_run_finished_replays_and_ends():
    """Reloading the page on a finished run shows the trace, then closes."""
    bus = InProcessEventBus()
    bus.publish("r", make_event("agent_start"))
    bus.close("r")                       # closed without a terminal event
    assert [e["type"] for e in drain(bus, "r")] == ["agent_start"]


def test_discard_forgets_the_run():
    """Checked directly, not through subscribe().

    After a discard the run is unknown, and an unknown run is indistinguishable
    from one that has not published yet -- so subscribing waits, correctly. The
    guard against subscribing to a swept run is the 404 in app/main.py, not the
    bus.
    """
    bus = InProcessEventBus()
    bus.publish("r", make_event("agent_start"))
    bus.close("r")
    bus.discard("r")
    assert "r" not in bus._history
    assert "r" not in bus._closed


def test_publish_never_blocks_on_a_subscriber_that_stopped_reading():
    """An agent must not stall because a browser tab went to sleep.

    publish() is synchronous and drops into a full queue rather than waiting,
    so a reader that walked away costs that reader events and costs the run
    nothing.
    """
    bus = InProcessEventBus()

    async def go():
        reading = asyncio.Event()

        async def slow_reader():
            async for _ in bus.subscribe("r"):
                reading.set()
                await asyncio.sleep(30)      # never comes back for more

        task = asyncio.create_task(slow_reader())
        bus.publish("r", make_event("agent_start"))
        await asyncio.wait_for(reading.wait(), timeout=2)

        for i in range(bus.MAX_QUEUED + 200):
            bus.publish("r", make_event("agent_output", message=str(i)))

        task.cancel()
        return len(bus._history["r"])

    assert asyncio.run(asyncio.wait_for(go(), timeout=5)) == bus.MAX_HISTORY
