"""Spans on WebSocket turns (RH3-01-03)."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from starlette.websockets import WebSocketDisconnect
from test_ws import Endless, World, _msg, _until

from prismal_server.config import HostSettings
from prismal_server.tracing import Tracing


def _traced(world: World) -> InMemorySpanExporter:
    mem = InMemorySpanExporter()
    world.app.state.tracing = Tracing(HostSettings(), exporter=mem)
    return mem


def _attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes or {})


def test_ws_turn_one_span_per_turn() -> None:
    world = World()
    mem = _traced(world)
    with (
        world.client() as c,
        c.websocket_connect(
            "/ws/threads/t1", headers={"Authorization": "Bearer acme"}
        ) as ws,
    ):
        for _ in range(2):
            ws.send_json(_msg())
            _until(ws, "done")
    spans = mem.get_finished_spans()
    assert len(spans) == 2
    assert len({s.context.trace_id for s in spans}) == 2
    for s in spans:
        a = _attrs(s)
        assert s.status.status_code == StatusCode.OK
        assert a["prismal.transport"] == "ws"
        assert a["prismal.protocol"] == "native"
        assert a["http.route"] == "/ws/threads/{thread_id}"
        assert a["gen_ai.conversation.id"] == "acme/t1"
        assert a["gen_ai.usage.input_tokens"] == 10


def test_ws_cancel_frame_marks_cancelled() -> None:
    world = World(Endless())
    mem = _traced(world)
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        ws.send_json(_msg())
        assert ws.receive_json()["event"] == "token"
        ws.send_json({"type": "cancel"})
        _until(ws, "error")
    (s,) = mem.get_finished_spans()
    assert s.status.status_code == StatusCode.UNSET
    assert _attrs(s)["prismal.outcome"] == "cancelled"


def test_ws_turn_start_failure_is_error() -> None:
    world = World()  # engine AG-UI flag off
    mem = _traced(world)
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        ws.send_json(_msg(protocol="ag-ui"))
        assert _until(ws, "error")[-1]["data"]["code"] == "protocol_not_enabled"
    (s,) = mem.get_finished_spans()
    assert s.status.status_code == StatusCode.ERROR
    assert _attrs(s)["error.type"] == "protocol_not_enabled"
    assert _attrs(s)["prismal.protocol"] == "ag-ui"


def test_ws_handshake_refusal_not_traced() -> None:
    world = World(cors_origins=["https://app.example"])
    mem = _traced(world)
    with world.client() as c, pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(
            "/ws/threads/t1", headers={"Origin": "https://evil.example"}
        ):
            pass
    assert mem.get_finished_spans() == ()
