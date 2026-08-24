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
    "awaiting_approval", "media_ready", "done", "error",
]


class Event(TypedDict, total=False):
    type: EventType
    ts: float
    agent: str
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


class InProcessEventBus:
    """Fan-out queue per run. First implementation; see the module note.

    Publishing is deliberately non-blocking and synchronous so an agent never
    stalls on a slow or absent reader.
    """

    # Bounds the memory one run can hold if nobody is reading. A run producing
    # more than this many unread events has a bigger problem than the drop.
    MAX_QUEUED = 1000

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[Event | None]]] = \
            defaultdict(list)
        self._history: dict[str, list[Event]] = defaultdict(list)

    def publish(self, run_id: str, event: Event) -> None:
        # Kept so a viewer attaching mid-run sees what already happened rather
        # than joining a stream in progress.
        self._history[run_id].append(event)
        for q in self._subscribers[run_id]:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, run_id: str) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=self.MAX_QUEUED)
        for past in self._history[run_id]:
            q.put_nowait(past)
        self._subscribers[run_id].append(q)
        try:
            while True:
                event = await q.get()
                if event is None:      # close() sentinel
                    return
                yield event
                if event["type"] in ("done", "error"):
                    return
        finally:
            self._subscribers[run_id].remove(q)

    def close(self, run_id: str) -> None:
        for q in self._subscribers[run_id]:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._history.pop(run_id, None)


__all__ = ["Event", "EventType", "EventBus", "InProcessEventBus", "make_event"]
