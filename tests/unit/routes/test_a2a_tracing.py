"""Spans on message-sending A2A RPCs (RH3-01-04)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from test_a2a_v1 import RecordingHandler, _app, _client, _rpc

from prismal_server.config import HostSettings
from prismal_server.tracing import Tracing

V1 = {"A2A-Version": "1.0"}


def _traced(handler: Any) -> tuple[Any, InMemorySpanExporter]:
    app, _ = _app(handler)
    mem = InMemorySpanExporter()
    app.state.tracing = Tracing(HostSettings(), exporter=mem)
    return app, mem


def _attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes or {})


def _message(context_id: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"messageId": "m", "role": "ROLE_USER", "parts": []}
    if context_id is not None:
        msg["contextId"] = context_id
    return msg


async def test_send_message_is_one_span() -> None:
    app, mem = _traced(RecordingHandler())
    async with _client(app) as c:
        resp = await c.post("/a2a", headers=V1, json=_rpc("SendMessage"))
    assert resp.status_code == 200
    (s,) = mem.get_finished_spans()
    a = _attrs(s)
    assert s.status.status_code == StatusCode.OK
    assert a["prismal.protocol"] == "a2a-1.0"
    assert a["prismal.a2a.method"] == "SendMessage"
    assert a["prismal.transport"] == "a2a"
    assert a["http.route"] == "/a2a"
    assert not [k for k in a if k.startswith("gen_ai.usage.")]


async def test_legacy_send_protocol() -> None:
    app, mem = _traced(RecordingHandler())
    async with _client(app) as c:
        await c.post("/a2a", json=_rpc("message/send"))
    (s,) = mem.get_finished_spans()
    assert _attrs(s)["prismal.protocol"] == "a2a-0.3"


@pytest.mark.parametrize("method", ["GetTask", "ListTasks", "CancelTask", "tasks/get"])
async def test_task_management_methods_not_traced(method: str) -> None:
    app, mem = _traced(RecordingHandler())
    async with _client(app) as c:
        await c.post("/a2a", headers=V1, json=_rpc(method))
    assert mem.get_finished_spans() == ()


class ErrorHandler(RecordingHandler):
    async def handle_rpc(self, request: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "error": {"code": -32001, "message": "unauthenticated"},
        }


async def test_jsonrpc_error_marks_span() -> None:
    app, mem = _traced(ErrorHandler())
    async with _client(app) as c:
        await c.post("/a2a", headers=V1, json=_rpc("SendMessage"))
    (s,) = mem.get_finished_spans()
    assert s.status.status_code == StatusCode.ERROR
    assert _attrs(s)["error.type"] == "jsonrpc.-32001"


async def test_missing_handler_is_error_span() -> None:
    app, mem = _traced(None)
    async with _client(app) as c:
        await c.post("/a2a", headers=V1, json=_rpc("SendMessage"))
    (s,) = mem.get_finished_spans()
    assert _attrs(s)["error.type"] == "jsonrpc.-32000"


class StreamHandler(RecordingHandler):
    def __init__(self, frames: list[str]) -> None:
        super().__init__()
        self.frames = frames

    async def stream_rpc(
        self, request: dict[str, Any], **kw: Any
    ) -> AsyncIterator[str]:
        for frame in self.frames:
            yield frame


@pytest.mark.parametrize(
    ("frames", "status", "error"),
    [
        (['data: {"jsonrpc":"2.0","id":"1","result":{}}\n\n'], StatusCode.OK, None),
        (
            [
                'data: {"jsonrpc":"2.0","id":"1","result":{}}\n\n',
                'data: {"jsonrpc":"2.0","id":"1","error":{"code":-32603}}\n\n',
            ],
            StatusCode.ERROR,
            "jsonrpc.-32603",
        ),
    ],
)
async def test_streaming_send_span(
    frames: list[str], status: StatusCode, error: str | None
) -> None:
    app, mem = _traced(StreamHandler(frames))
    async with _client(app) as c:
        resp = await c.post("/a2a", headers=V1, json=_rpc("SendStreamingMessage"))
    assert resp.status_code == 200
    (s,) = mem.get_finished_spans()
    assert s.status.status_code == status
    assert _attrs(s).get("error.type") == error


async def test_a2a_conversation_is_context_id() -> None:
    app, mem = _traced(RecordingHandler())
    async with _client(app) as c:
        await c.post(
            "/a2a",
            headers={**V1, "Authorization": "Bearer acme"},
            json=_rpc("SendMessage", message=_message("c-9")),
        )
        await c.post("/a2a", headers=V1, json=_rpc("SendMessage", message="bad"))
    first, second = mem.get_finished_spans()
    assert _attrs(first)["gen_ai.conversation.id"] == "acme/c-9"
    assert _attrs(first)["prismal.org_id"] == "acme"
    assert "gen_ai.conversation.id" not in _attrs(second)  # malformed params: omitted
