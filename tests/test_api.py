"""Tests for the browser-facing API.

No Gemini, no ClickHouse, no media. app.main imports the agent stack inside the
background task rather than at module scope, so the endpoints, the admission
control and the approval gate can all be exercised against a fake analysis that
publishes the same events the real one does.

What is deliberately covered: the parts that only break in a browser. A run that
returns before its analysis finishes, a stream that has to stay open across the
approval gate, and a second visitor arriving mid-run.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app import main  # noqa: E402
from app.contracts import SceneAsset  # noqa: E402
from app.events import make_event  # noqa: E402
from app.state import RunState  # noqa: E402

PROPOSALS = [
    {"variant": "grounded", "title": "The Unveiling", "logline": "A film.",
     "motif_tags": ["hidden_conspiracy", "loss_of_innocence"],
     "character_archetypes": ["mentor", "authority_figure"],
     "act_structure": "classic_three_act", "rationale": "because",
     "evidence": []},
    {"variant": "wildcard", "title": "Echoes", "logline": "Another film.",
     "motif_tags": ["revenge", "survival"],
     "character_archetypes": ["orphan", "outcast"],
     "act_structure": "classic_three_act", "rationale": "why not",
     "evidence": []},
]
SCORES = [
    {"proposal_title": "The Unveiling", "commercial_score": 55.8,
     "attention_score": 58.0, "composite": 56.7, "confidence": "high",
     "evidence": [], "caveats": []},
    {"proposal_title": "Echoes", "commercial_score": 46.7,
     "attention_score": 49.7, "composite": 47.9, "confidence": "high",
     "evidence": [], "caveats": []},
]
SCENES = [
    {"scene_index": 0, "description": "A records room.",
     "image_url": "https://storage.example/runs/r/scene_0/image.jpg",
     "audio_url": "https://storage.example/runs/r/scene_0/narration.wav",
     "duration_sec": 3.2},
    {"scene_index": 1, "description": "A hallway.",
     "image_url": "https://storage.example/runs/r/scene_1/image.jpg",
     "audio_url": "https://storage.example/runs/r/scene_1/narration.wav",
     "duration_sec": 4.0},
    {"scene_index": 2, "description": "A street.",
     "image_url": "https://storage.example/runs/r/scene_2/image.jpg",
     "audio_url": "https://storage.example/runs/r/scene_2/narration.wav",
     "duration_sec": 3.7},
]


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """A fresh store, bus and admission slot per test."""
    from app.events import InProcessEventBus
    from app.state import RunStore

    monkeypatch.setattr(main, "store", RunStore())
    monkeypatch.setattr(main, "bus", InProcessEventBus())
    monkeypatch.setattr(main, "ANALYSIS_SLOT", asyncio.Semaphore(1))
    monkeypatch.setattr(main, "MEDIA_SLOT", asyncio.Semaphore(1))
    main._recent_runs.clear()
    yield


def fake_analysis(*, hold: threading.Event | None = None,
                  fail: bool = False):
    """Stand-in for _analyse: same events, same state moves, no model calls."""
    async def run(run_id: str, prompt: str) -> None:
        store, bus = main.store, main.bus
        run_obj = store.require(run_id)
        try:
            async with main.ANALYSIS_SLOT:
                bus.publish(run_id, make_event("agent_start",
                                               agent="recombine_phase_a"))
                bus.publish(run_id, make_event(
                    "tool_call", agent="recombine_phase_a", tool="run_query",
                    args={"query": "SELECT countMerge(sample_count) "
                                   "FROM mv_archetype_performance"}))
                bus.publish(run_id, make_event("tool_result", rows=25,
                                               elapsed_ms=212.0))
                # A retry mid-run: the browser must survive this.
                bus.publish(run_id, make_event("agent_retry", retry=1,
                                               message="logline too long"))
                # threading.Event, polled: the test thread and the app's
                # event loop are different threads, and set() on an
                # asyncio.Event from outside its loop does not wake the waiter.
                while hold is not None and not hold.is_set():
                    await asyncio.sleep(0.01)
                if fail:
                    raise RuntimeError("analysis blew up")
            run_obj.proposals = PROPOSALS
            run_obj.scores = SCORES
            run_obj.transition(RunState.AWAITING_APPROVAL)
            bus.publish(run_id, make_event("awaiting_approval", agent="root",
                                           proposals=PROPOSALS, scores=SCORES))
        except Exception as exc:
            run_obj.fail(str(exc))
            bus.publish(run_id, make_event("error", agent="root",
                                           error=str(exc)))
            bus.close(run_id)
    return run


def fake_media(*, hold: threading.Event | None = None,
               fail: bool = False):
    """Stand-in for _render_approved_variant: same state moves, no spend."""
    async def run(run_id: str, variant: str) -> None:
        store, bus = main.store, main.bus
        run_obj = store.require(run_id)
        try:
            async with main.MEDIA_SLOT:
                bus.publish(run_id, make_event("agent_start", agent="media"))
                while hold is not None and not hold.is_set():
                    await asyncio.sleep(0.01)
                if fail:
                    raise RuntimeError("media blew up")
                run_obj.scenes = SCENES
                bus.publish(run_id, make_event("media_ready", agent="media",
                                               scenes=run_obj.scenes))
                run_obj.transition(RunState.DONE)
                bus.publish(run_id, make_event("done", agent="root"))
                bus.close(run_id)
        except Exception as exc:
            run_obj.fail(str(exc))
            bus.publish(run_id, make_event("error", agent="media",
                                           error=str(exc)))
            bus.close(run_id)
    return run


def start(client: TestClient, prompt: str = "") -> str:
    res = client.post("/run", json={"prompt": prompt})
    assert res.status_code == 200, res.text
    return res.json()["run_id"]


def wait_for(client: TestClient, run_id: str, state: str,
             tries: int = 100) -> dict:
    for _ in range(tries):
        body = client.get(f"/runs/{run_id}").json()
        if body["state"] == state:
            return body
        time.sleep(0.02)
    raise AssertionError(f"run stayed at {body['state']}, wanted {state}")


# --- the happy path ---------------------------------------------------------

def test_run_returns_immediately_then_reaches_the_gate(monkeypatch):
    monkeypatch.setattr(main, "_analyse", fake_analysis())
    with TestClient(main.app) as client:
        run_id = start(client)
        body = wait_for(client, run_id, "awaiting_approval")
        assert [p["variant"] for p in body["proposals"]] == ["grounded",
                                                             "wildcard"]


def test_stream_carries_sql_verbatim_across_retry_and_gate(monkeypatch):
    """Read the whole stream of a finished run.

    Approving first means the stream terminates on its own, which is also the
    point being tested: a retry mid-run and the approval gate are both things
    the connection has to survive, and only `done` ends it.
    """
    monkeypatch.setattr(main, "_analyse", fake_analysis())
    monkeypatch.setattr(main, "_render_approved_variant", fake_media())
    with TestClient(main.app) as client:
        run_id = start(client)
        wait_for(client, run_id, "awaiting_approval")
        client.post(f"/approve/{run_id}", json={"variant": "grounded"})
        wait_for(client, run_id, "done")

        with client.stream("GET", f"/events/{run_id}") as res:
            assert res.headers["x-accel-buffering"] == "no"
            kinds, sql = [], None
            for line in res.iter_lines():
                if line.startswith("event: "):
                    kinds.append(line.removeprefix("event: "))
                elif '"query"' in line and sql is None:
                    sql = line

        assert kinds.index("agent_retry") < kinds.index("awaiting_approval")
        assert kinds.index("media_ready") < kinds.index("done")
        assert kinds.index("awaiting_approval") < kinds.index("done")
        assert kinds[-1] == "done"
        assert "mv_archetype_performance" in sql


def test_approve_starts_media_in_the_background(monkeypatch):
    hold = threading.Event()
    monkeypatch.setattr(main, "_analyse", fake_analysis())
    monkeypatch.setattr(main, "_render_approved_variant",
                        fake_media(hold=hold))
    with TestClient(main.app) as client:
        run_id = start(client)
        wait_for(client, run_id, "awaiting_approval")
        res = client.post(f"/approve/{run_id}", json={"variant": "wildcard"})
        assert res.status_code == 200
        assert res.json()["approved_variant"] == "wildcard"
        assert res.json()["state"] == "storyboard"
        assert client.get(f"/runs/{run_id}").json()["state"] == "storyboard"

        hold.set()
        body = wait_for(client, run_id, "done")
        assert body["approved_variant"] == "wildcard"
        assert body["scenes"] == SCENES


def test_media_failure_ends_the_stream_with_an_error(monkeypatch):
    monkeypatch.setattr(main, "_analyse", fake_analysis())
    monkeypatch.setattr(main, "_render_approved_variant", fake_media(fail=True))
    with TestClient(main.app) as client:
        run_id = start(client)
        wait_for(client, run_id, "awaiting_approval")
        res = client.post(f"/approve/{run_id}", json={"variant": "grounded"})
        assert res.status_code == 200

        body = wait_for(client, run_id, "error")
        assert "media blew up" in body["error"]


def test_rendered_media_is_stored_before_media_ready(monkeypatch):
    from app.events import InProcessEventBus
    import app.media as media_module

    async def fake_plan_storyboard(model, proposal, emit):
        return types.SimpleNamespace(scenes=[]), [], types.SimpleNamespace()

    async def fake_render_media(run_id, plan, *, progress=None):
        progress and progress("scene 1/3: image")
        return [SceneAsset.model_validate(s) for s in SCENES]

    fake_pipeline = types.ModuleType("app.pipeline")
    fake_pipeline.plan_storyboard = fake_plan_storyboard
    monkeypatch.setitem(sys.modules, "app.pipeline", fake_pipeline)
    monkeypatch.setattr(media_module, "render_storyboard_media",
                        fake_render_media)

    class RecordingBus(InProcessEventBus):
        def __init__(self):
            super().__init__()
            self.media_ready_state = None
            self.media_ready_scenes = None

        def publish(self, run_id, event):
            if event["type"] == "media_ready":
                run = main.store.require(run_id)
                self.media_ready_state = run.state.value
                self.media_ready_scenes = list(run.scenes)
            super().publish(run_id, event)

    recording_bus = RecordingBus()
    monkeypatch.setattr(main, "bus", recording_bus)

    run = main.store.create(prompt="")
    run.proposals = PROPOSALS
    run.transition(RunState.AWAITING_APPROVAL)
    run.approved_variant = "grounded"
    run.transition(RunState.STORYBOARD)

    asyncio.run(main._render_approved_variant(run.run_id, "grounded"))

    stored = main.store.require(run.run_id)
    assert stored.scenes == SCENES
    assert stored.state is RunState.DONE
    assert recording_bus.media_ready_state == "storyboard"
    assert recording_bus.media_ready_scenes == SCENES


# --- admission control ------------------------------------------------------

def test_second_run_while_one_is_analysing_is_refused(monkeypatch):
    hold = threading.Event()
    monkeypatch.setattr(main, "_analyse", fake_analysis(hold=hold))
    with TestClient(main.app) as client:
        run_id = start(client)
        for _ in range(200):
            if main.ANALYSIS_SLOT.locked():
                break
            time.sleep(0.02)
        assert main.ANALYSIS_SLOT.locked()

        res = client.post("/run", json={"prompt": ""})
        assert res.status_code == 409
        assert "one at a time" in res.json()["detail"]

        hold.set()
        wait_for(client, run_id, "awaiting_approval")


def test_the_approval_gate_does_not_hold_the_slot(monkeypatch):
    """The decision that keeps a public demo usable.

    A visitor who reaches the gate and walks away must not lock everyone else
    out. The slot covers the analysis -- the Gemini loop and the ClickHouse
    queries -- and is released the moment proposals exist.
    """
    monkeypatch.setattr(main, "_analyse", fake_analysis())
    with TestClient(main.app) as client:
        first = start(client)
        wait_for(client, first, "awaiting_approval")
        assert not main.ANALYSIS_SLOT.locked()

        second = start(client)           # nobody approved the first
        wait_for(client, second, "awaiting_approval")
        assert client.get(f"/runs/{first}").json()["state"] == \
            "awaiting_approval"


def test_real_analysis_publishes_gate_after_state_transition(monkeypatch):
    """The SSE gate must not race ahead of the RunStore state.

    A fast client can receive `awaiting_approval` and immediately call
    /approve. The event therefore has to be published only after the run is
    actually approvable.
    """
    from app.events import InProcessEventBus

    class Dumpable:
        def __init__(self, value):
            self.value = value

        def model_dump(self, mode="json"):
            return self.value

    class FakeToolset:
        async def close(self):
            pass

    async def fake_warm_up(toolset):
        return []

    async def fake_run_greenlight(model, toolset, emit, *, prompt, run_id):
        emit(make_event("tool_call", agent="predict", tool="run_query",
                        args={"query": "SELECT 1"}))
        emit(make_event("awaiting_approval", agent="root",
                        proposals=[{"stale": True}],
                        scores=[{"stale": True}]))
        outcomes = [
            types.SimpleNamespace(proposal=Dumpable(PROPOSALS[0]),
                                  score=Dumpable(SCORES[0])),
            types.SimpleNamespace(proposal=Dumpable(PROPOSALS[1]),
                                  score=Dumpable(SCORES[1])),
        ]
        return types.SimpleNamespace(outcomes=outcomes), None, None

    fake_mcp = types.ModuleType("app.mcp")
    fake_mcp.build_clickhouse_tools = FakeToolset
    fake_mcp.warm_up = fake_warm_up
    fake_pipeline = types.ModuleType("app.pipeline")
    fake_pipeline.DEFAULT_PROMPT = "default prompt"
    fake_pipeline.run_greenlight = fake_run_greenlight
    monkeypatch.setitem(sys.modules, "app.mcp", fake_mcp)
    monkeypatch.setitem(sys.modules, "app.pipeline", fake_pipeline)

    class RecordingBus(InProcessEventBus):
        def __init__(self):
            super().__init__()
            self.published = []

        def publish(self, run_id, event):
            run = main.store.get(run_id)
            self.published.append((event["type"], run.state.value))
            super().publish(run_id, event)

    recording_bus = RecordingBus()
    monkeypatch.setattr(main, "bus", recording_bus)

    class FakeTask:
        def cancel(self):
            pass

    def no_background_task(coro):
        coro.close()
        return FakeTask()

    monkeypatch.setattr(main.asyncio, "create_task", no_background_task)

    run = main.store.create(prompt="")
    asyncio.run(main._analyse(run.run_id, ""))

    stored = main.store.require(run.run_id)
    assert stored.state is RunState.AWAITING_APPROVAL
    assert stored.proposals == PROPOSALS
    assert stored.scores == SCORES
    assert ("awaiting_approval", "awaiting_approval") in \
        recording_bus.published
    assert ("awaiting_approval", "running") not in recording_bus.published


def test_rate_limit_is_per_address(monkeypatch):
    monkeypatch.setattr(main, "_analyse", fake_analysis())
    with TestClient(main.app) as client:
        for _ in range(main.RATE_LIMIT_RUNS):
            run_id = start(client)
            wait_for(client, run_id, "awaiting_approval")
        assert client.post("/run", json={"prompt": ""}).status_code == 429
        # A different address is unaffected.
        assert client.post("/run", json={"prompt": ""},
                           headers={"x-forwarded-for": "203.0.113.9"}
                           ).status_code == 200


def test_prompt_length_is_bounded():
    with TestClient(main.app) as client:
        res = client.post("/run",
                          json={"prompt": "x" * (main.MAX_PROMPT_CHARS + 1)})
        assert res.status_code == 422


# --- failure paths ----------------------------------------------------------

def test_failed_analysis_ends_the_stream_with_an_error(monkeypatch):
    monkeypatch.setattr(main, "_analyse", fake_analysis(fail=True))
    with TestClient(main.app) as client:
        run_id = start(client)
        body = wait_for(client, run_id, "error")
        assert "blew up" in body["error"]
        assert not main.ANALYSIS_SLOT.locked()


def test_approving_before_the_gate_is_rejected(monkeypatch):
    hold = threading.Event()
    monkeypatch.setattr(main, "_analyse", fake_analysis(hold=hold))
    with TestClient(main.app) as client:
        run_id = start(client)
        res = client.post(f"/approve/{run_id}", json={"variant": "grounded"})
        assert res.status_code == 409
        hold.set()
        wait_for(client, run_id, "awaiting_approval")


def test_approving_a_variant_that_was_not_produced(monkeypatch):
    monkeypatch.setattr(main, "_analyse", fake_analysis())
    with TestClient(main.app) as client:
        run_id = start(client)
        wait_for(client, run_id, "awaiting_approval")
        main.store.require(run_id).proposals = [PROPOSALS[0]]
        res = client.post(f"/approve/{run_id}", json={"variant": "wildcard"})
        assert res.status_code == 400


def test_unknown_run_is_404():
    with TestClient(main.app) as client:
        assert client.get("/events/nope").status_code == 404
        assert client.get("/runs/nope").status_code == 404
        assert client.post("/approve/nope",
                           json={"variant": "grounded"}).status_code == 404


def test_health_does_not_touch_the_database():
    """A startup probe must not wait on a 32-second ClickHouse cold start."""
    with TestClient(main.app) as client:
        assert client.get("/health").json()["ok"] is True
