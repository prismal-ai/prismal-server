"""Tests for thread/chat routes (RHB-02-02/03/04/05, THR-*, CHT-*)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from httpx import ASGITransport, AsyncClient

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry


class FakeMsg:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_call_chunks: list[dict[str, Any]] = []


def _fake_registry() -> RuntimeRegistry:
    async def builder(*, org_id: str | None) -> Any:
        return type("RT", (), {"tool_provider": object(), "org_id": org_id})()

    return RuntimeRegistry(HostSettings(), builder=builder)


def _app_with_graph(astream_impl):  # type: ignore[no-untyped-def]
    """Build an app whose graph records calls and streams via astream_impl."""
    calls: list[dict[str, Any]] = []

    class FakeGraph:
        def astream(self, inp, config, *, stream_mode=None):  # type: ignore[no-untyped-def]
            calls.append({"input": inp, "config": config, "stream_mode": stream_mode})
            return astream_impl()

    async def graph_factory(*, tool_provider: Any = None) -> Any:
        return FakeGraph()

    app = create_app(registry=_fake_registry(), graph_factory=graph_factory)
    return app, calls


def _client(app):  # type: ignore[no-untyped-def]
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_create_thread_mints_unique_ids() -> None:
    app, _ = _app_with_graph(lambda: _empty())
    async with _client(app) as c:
        a = await c.post("/threads")
        b = await c.post("/threads")
    assert a.status_code == 200
    id_a = a.json()["thread_id"]
    id_b = b.json()["thread_id"]
    assert id_a and id_b and id_a != id_b


async def _empty() -> AsyncIterator[Any]:
    for _ in ():
        yield None


async def _two_tokens() -> AsyncIterator[Any]:
    yield (FakeMsg("Ho"), {})
    yield (FakeMsg("la"), {})


async def test_post_message_streams_sse_tokens_and_done() -> None:
    app, _ = _app_with_graph(_two_tokens)
    async with _client(app) as c:
        resp = await c.post("/threads/t-42/messages", json={"content": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert 'event: token\ndata: {"delta": "Ho"}' in body
    assert "event: done" in body
    assert '"thread_id": "t-42"' in body


async def test_thread_and_org_are_threaded_into_config() -> None:
    """thread_id (path) + org_id (X-Org-Id) reach config.configurable (THR-003)."""
    app, calls = _app_with_graph(_two_tokens)
    async with _client(app) as c:
        await c.post(
            "/threads/t-9/messages",
            json={"content": "hi"},
            headers={"X-Org-Id": "acme"},
        )
    cfg = calls[0]["config"]["configurable"]
    assert cfg["thread_id"] == "t-9"
    assert cfg["org_id"] == "acme"
    # user content is passed only as graph input, never interpolated (CHT-005)
    assert calls[0]["input"]["messages"][0]["content"] == "hi"


async def test_missing_content_is_422() -> None:
    app, _ = _app_with_graph(_two_tokens)
    async with _client(app) as c:
        resp = await c.post("/threads/t/messages", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_midstream_failure_emits_terminal_error_event() -> None:
    async def boom() -> AsyncIterator[Any]:
        yield (FakeMsg("partial"), {})
        raise RuntimeError("kaboom /secret")

    app, _ = _app_with_graph(boom)
    async with _client(app) as c:
        resp = await c.post("/threads/t/messages", json={"content": "hi"})
    # status is already 200; failure rides a terminal error event (CHT-006)
    assert resp.status_code == 200
    assert "event: error" in resp.text
    assert "secret" not in resp.text


async def test_client_disconnect_cancels_graph_stream() -> None:
    """A real ASGI ``http.disconnect`` cancels the underlying astream (RHB-02-05).

    Driven at the ASGI layer because httpx's ASGITransport never emits
    ``http.disconnect``; here we send one after the first body chunk and assert
    the graph stream's ``finally`` runs.
    """
    state = {"closed": False}

    async def endless() -> AsyncIterator[Any]:
        try:
            while True:
                yield (FakeMsg("tok"), {})
                await asyncio.sleep(0.01)
        finally:
            state["closed"] = True

    app, _ = _app_with_graph(endless)

    body = b'{"content":"hi"}'
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/threads/t/messages",
        "raw_path": b"/threads/t/messages",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "server": ("test", 80),
        "client": ("test", 12345),
        "scheme": "http",
    }
    disconnect = asyncio.Event()
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        # Once the first streamed chunk is sent, simulate the client hanging up.
        if message["type"] == "http.response.body" and message.get("body"):
            disconnect.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    assert state["closed"] is True
