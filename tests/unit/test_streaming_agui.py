"""AG-UI bridge vs the engine golden streams (RH2-03-02/04, SPEC-RH2-AGU-002/004).

The fixtures under ``tests/fixtures/protocols/agui/`` are vendored verbatim from
the engine (``prismal/tests/fixtures/protocols/agui/``). Each one replays a
``graph.astream(stream_mode=["messages","updates","custom"])`` transcript; the
host bridge must frame exactly the engine-produced events, in order, as
data-only SSE frames — the host adds no AG-UI event of its own.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.types import Interrupt
from prismal.events.agui import AgUiContext

from prismal_server.streaming import ChunkPump, agui_stream

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "protocols" / "agui"
_CLASSES: dict[str, Any] = {
    "AIMessageChunk": AIMessageChunk,
    "ToolMessage": ToolMessage,
}


def _decode(obj: Any) -> Any:
    if isinstance(obj, dict):
        cls = obj.get("__class__")
        if cls == "Interrupt":
            return Interrupt(value=_decode(obj["value"]), id=obj["id"])
        if cls in _CLASSES:
            fields = {k: v for k, v in obj.items() if k != "__class__"}
            # The engine serialized with exclude_none: restore the dropped keys.
            for tc in fields.get("tool_call_chunks", []):
                for key in ("name", "args", "id", "index"):
                    tc.setdefault(key, None)
            for tc in fields.get("tool_calls", []):
                tc.setdefault("id", None)
            return _CLASSES[cls](**fields)
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    return obj


def _chunk(item: list[Any]) -> tuple[Any, ...]:
    decoded = _decode(item)
    if len(decoded) == 2:
        mode, payload = decoded
        return (mode, tuple(payload) if mode == "messages" else payload)
    ns, mode, payload = decoded
    return (tuple(ns), mode, tuple(payload) if mode == "messages" else payload)


class _Summary:
    def __init__(self, wire: dict[str, Any]) -> None:
        self._wire = wire

    def to_usage_wire(self) -> dict[str, Any]:
        return dict(self._wire)


class BudgetExceeded(Exception):
    """Named like the engine exception so the host maps it by class name."""


def _frames_to_events(frames: list[str]) -> list[dict[str, Any]]:
    events = []
    for frame in frames:
        assert not frame.startswith("event:"), "AG-UI frames carry no event: line"
        if frame.startswith("data: "):
            events.append(json.loads(frame[len("data: ") :]))
    return events


def _fixture_names() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.json"))


@pytest.mark.parametrize("name", _fixture_names())
async def test_bridge_frames_engine_golden_stream(name: str) -> None:
    fx = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    counter = itertools.count(1_760_000_000_000)
    ctx = AgUiContext(clock=lambda: next(counter), **fx["context"])
    end = fx["end"]

    async def chunks() -> AsyncIterator[Any]:
        for item in fx["input"]:
            yield _chunk(item)
        if end["kind"] == "error":
            assert end["code"] == "budget_exceeded"  # the only error fixture
            raise BudgetExceeded(end["message"])

    wire = fx["summary_usage_wire"]
    frames = [
        f
        async for f in agui_stream(
            ChunkPump(chunks()),
            ctx=ctx,
            heartbeat_s=100,
            initial_state=fx["initial_state"],
            result=end.get("result"),
            summary_for=lambda: _Summary(wire) if wire else None,
        )
    ]
    assert _frames_to_events(frames) == fx["expected"]


async def test_first_run_started_last_terminal() -> None:
    """EARS AGU-001: RUN_STARTED first, RUN_FINISHED or RUN_ERROR last."""
    for name in _fixture_names():
        fx = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        types = [e["type"] for e in fx["expected"]]
        assert types[0] == "RUN_STARTED"
        assert types[-1] in {"RUN_FINISHED", "RUN_ERROR"}


async def test_usage_custom_precedes_run_finished() -> None:
    fx = json.loads((FIXTURES / "react_text.json").read_text(encoding="utf-8"))
    types = [(e["type"], e.get("name")) for e in fx["expected"]]
    assert types[-2] == ("CUSTOM", "prismal.usage") and types[-1][0] == "RUN_FINISHED"


async def test_heartbeat_comments_while_idle() -> None:
    async def slow() -> AsyncIterator[Any]:
        await asyncio.sleep(0.05)
        yield ("updates", {"agent": {"current_agent": "agent"}})

    ctx = AgUiContext(run_id="r", thread_id="t")
    frames = [
        f
        async for f in agui_stream(
            ChunkPump(slow()), ctx=ctx, heartbeat_s=0.01, summary_for=lambda: None
        )
    ]
    assert ": heartbeat\n\n" in frames
    assert _frames_to_events(frames)[-1]["type"] == "RUN_FINISHED"


async def test_on_done_receives_usage_after_success() -> None:
    fx = json.loads((FIXTURES / "react_text.json").read_text(encoding="utf-8"))
    recorded: list[Any] = []

    async def on_done(reason: str, usage: Any) -> None:
        recorded.append((reason, usage))

    async def chunks() -> AsyncIterator[Any]:
        for item in fx["input"]:
            yield _chunk(item)

    ctx = AgUiContext(run_id="r", thread_id="t")
    async for _ in agui_stream(
        ChunkPump(chunks()),
        ctx=ctx,
        heartbeat_s=100,
        summary_for=lambda: _Summary(fx["summary_usage_wire"]),
        on_done=on_done,
    ):
        pass
    assert len(recorded) == 1
    reason, usage = recorded[0]
    assert reason == "stop" and usage.model_dump() == fx["summary_usage_wire"]


async def test_disconnect_cancels_graph_stream() -> None:
    state = {"closed": False}

    async def endless() -> AsyncIterator[Any]:
        try:
            while True:
                msg = AIMessageChunk(content="tok", id="m-1")
                yield ("messages", (msg, {"langgraph_node": "agent"}))
                await asyncio.sleep(0.01)
        finally:
            state["closed"] = True

    ctx = AgUiContext(run_id="r", thread_id="t")
    agen = agui_stream(
        ChunkPump(endless()), ctx=ctx, heartbeat_s=100, summary_for=lambda: None
    )
    # Read until a chunk-derived event: the graph stream is now running.
    while "TEXT_MESSAGE_CONTENT" not in await agen.__anext__():
        pass
    await agen.aclose()  # simulate client disconnect
    await asyncio.sleep(0.02)
    assert state["closed"] is True
