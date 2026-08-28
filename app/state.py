"""Run lifecycle as an explicit state machine.

The approval gate is a state this run sits in, not a coroutine blocked on a
click. An agent that awaited the user would hold its turn open for however long
someone takes to decide, tie the run's survival to one HTTP connection, and make
"did they approve" unanswerable from anywhere but that stack frame.

So: agents run to completion and stop. The API layer moves the run to
AWAITING_APPROVAL, and POST /approve/{run_id} transitions it onward.

Storage is an in-memory dict, per SYSTEM_SPEC §6.3 -- a restart loses in-flight
runs, which is acceptable for a demo. The same single-instance constraint as
app/events.py applies: this state is not shared between Cloud Run instances.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RunState(str, Enum):
    RUNNING = "running"                      # Recombine → Predict
    AWAITING_APPROVAL = "awaiting_approval"  # gate; user picks a variant
    STORYBOARD = "storyboard"                # approved; media generating
    DONE = "done"
    ERROR = "error"


# Everything else is rejected, so an out-of-order /approve cannot resurrect a
# finished run or skip the gate.
_ALLOWED: dict[RunState, frozenset[RunState]] = {
    RunState.RUNNING: frozenset({RunState.AWAITING_APPROVAL, RunState.ERROR}),
    RunState.AWAITING_APPROVAL: frozenset({RunState.STORYBOARD, RunState.ERROR}),
    RunState.STORYBOARD: frozenset({RunState.DONE, RunState.ERROR}),
    RunState.DONE: frozenset(),
    RunState.ERROR: frozenset(),
}


class InvalidTransition(RuntimeError):
    pass


@dataclass
class Run:
    run_id: str
    state: RunState = RunState.RUNNING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    prompt: str = ""
    proposals: list[dict[str, Any]] = field(default_factory=list)
    scores: list[dict[str, Any]] = field(default_factory=list)
    approved_variant: str | None = None
    scenes: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def transition(self, to: RunState) -> None:
        if to not in _ALLOWED[self.state]:
            raise InvalidTransition(
                f"run {self.run_id}: {self.state.value} → {to.value} "
                f"is not allowed (permitted: "
                f"{', '.join(s.value for s in _ALLOWED[self.state]) or 'none'})"
            )
        self.state = to
        self.updated_at = time.time()

    def fail(self, message: str) -> None:
        """Terminal states stay terminal; a late failure does not overwrite."""
        if self.state in (RunState.DONE, RunState.ERROR):
            return
        self.error = message
        self.state = RunState.ERROR
        self.updated_at = time.time()


class RunStore:
    """In-memory run registry."""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    def create(self, prompt: str = "") -> Run:
        run = Run(run_id=uuid.uuid4().hex[:12], prompt=prompt)
        self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def require(self, run_id: str) -> Run:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        return run

    def sweep(self, max_age_sec: float = 3600) -> int:
        """Drop finished runs older than max_age_sec. Returns how many went."""
        cutoff = time.time() - max_age_sec
        stale = [rid for rid, r in self._runs.items()
                 if r.state in (RunState.DONE, RunState.ERROR)
                 and r.updated_at < cutoff]
        for rid in stale:
            del self._runs[rid]
        return len(stale)


__all__ = ["RunState", "Run", "RunStore", "InvalidTransition"]
