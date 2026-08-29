"""Event transport between the agents and the SSE endpoint.

Agents publish to a bus; the HTTP layer subscribes. Nothing yields SSE frames
from inside an agent, so an agent can run without anyone watching, two viewers
can attach to one run, and swapping the in-process queue for Redis pub/sub later
touches only this file.

The event shapes mirror SYSTEM_SPEC §6.2 and are not up for redesign: tool_call
carrying the SQL verbatim and tool_result carrying rows and elapsed_ms are how a
judge verifies the database is really being queried at runtime.

DEPLOYMENT CONSTRAINT: InProcessEventBus lives in one process's memory. On Cloud
Run with autoscaling, a subscriber can land on a different instance from the
publisher, and the stream then hangs open producing nothing -- no error, no
disconnect. Deploy with --min-instances=1 --max-instances=1, or move to a shared
bus first.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, AsyncIterator, Literal, Protocol, TypedDict

EventType = Literal[
    "agent_start", "tool_call", "tool_result", "tool_error", "agent_output",
    "agent_retry", "stage_failed", "awaiting_approval", "media_ready",
    "done", "error",
]

# Receiving one of these ends a subscription. Everything else is progress, and
# the distinction is not cosmetic: the pipeline retries a rejected Phase B
# proposal and carries on past a failed variant, and publishing those as
# "error" closed every browser's stream mid-run while the CLI -- which does not
# subscribe to its own bus -- ran happily to completion. A recoverable problem
# is agent_retry (another attempt follows) or stage_failed (this unit gave up,
# the run continues without it); "error" means the run itself is over.
TERMINAL_EVENTS: tuple[EventType, ...] = ("done", "error")


class Event(TypedDict, total=False):
    type: EventType
    ts: float
    run_id: str
    agent: str
    variant: str            # which proposal this stage belongs to, if any
    tool: str
    args: dict[str, Any]      # tool_call: MUST include the SQL text
    rows: int                 # tool_result
    elapsed_ms: float         # tool_result
    preview: list[list[str]]  # tool_result: first few rows, for the UI
    error: str
    retry: int
    payload: dict[str, Any]
    proposals: list[dict[str, Any]]
    scores: list[dict[str, Any]]
    scenes: list[dict[str, Any]]
    message: str


def make_event(type_: EventType, **fields: Any) -> Event:
    """Stamp an event. Callers never set ts themselves."""
    return Event(type=type_, ts=time.time(), **fields)


class EventBus(Protocol):
    def publish(self, run_id: str, event: Event) -> None: ...

    def subscribe(self, run_id: str) -> AsyncIterator[Event]: ...

    def close(self, run_id: str) -> None: ...

    def discard(self, run_id: str) -> None: ...


class InProcessEventBus:
    """Fan-out queue per run. First implementation; see the module note.

    Publishing is deliberately non-blocking and synchronous so an agent never
    stalls on a slow or absent reader.
    """

    # Bounds the memory one run can hold if nobody is reading. A run producing
    # more than this many unread events has a bigger problem than the drop.
    MAX_QUEUED = 1000

    # Replay is capped below the queue so a late subscriber can always take the
    # whole backlog without filling its queue on the first line. A full M2 run
    # produces 86 events; the cap is for a pathological run, not a normal one.
    MAX_HISTORY = 800

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[Event | None]]] = \
            defaultdict(list)
        self._history: dict[str, list[Event]] = defaultdict(list)
        self._closed: set[str] = set()

    def publish(self, run_id: str, event: Event) -> None:
        # Kept so a viewer attaching mid-run sees what already happened rather
        # than joining a stream in progress.
        history = self._history[run_id]
        history.append(event)
        if len(history) > self.MAX_HISTORY:
            del history[:len(history) - self.MAX_HISTORY]
        for q in self._subscribers[run_id]:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, run_id: str) -> AsyncIterator[Event]:
        """Replay what has happened, then follow the run.

        Replay never raises. The first version called put_nowait for every past
        event against a bounded queue, so a run with more history than
        MAX_QUEUED made subscribing throw QueueFull -- the failure landing on
        the reader, for something the writer did.
        """
        q: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=self.MAX_QUEUED)
        backlog = list(self._history[run_id])
        self._subscribers[run_id].append(q)
        try:
            for past in backlog:
                yield past
                if past["type"] in TERMINAL_EVENTS:
                    return
            # A run that finished before anyone attached still ends the stream:
            # the browser gets the whole trace and a closed connection rather
            # than a complete trace and a socket that never closes.
            if run_id in self._closed:
                return
            while True:
                event = await q.get()
                if event is None:      # close() sentinel
                    return
                yield event
                if event["type"] in TERMINAL_EVENTS:
                    return
        finally:
            if q in self._subscribers[run_id]:
                self._subscribers[run_id].remove(q)

    def close(self, run_id: str) -> None:
        """End live subscriptions. History stays, so a reload still replays."""
        self._closed.add(run_id)
        for q in self._subscribers[run_id]:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def discard(self, run_id: str) -> None:
        """Drop a run's history for good. Called when the run is swept.

        Pair this with removing the run from the RunStore. Once discarded the
        run is simply unknown here, and subscribing to an unknown run waits --
        which is right for a run that has not published yet and wrong for one
        that has been forgotten. app/main.py answers 404 before it gets here.
        """
        self._history.pop(run_id, None)
        self._subscribers.pop(run_id, None)
        self._closed.discard(run_id)


__all__ = ["Event", "EventType", "EventBus", "InProcessEventBus",
           "make_event", "TERMINAL_EVENTS"]
