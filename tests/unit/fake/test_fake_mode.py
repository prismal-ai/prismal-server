"""Fake-LLM mode (RH2-06-01/02/05, SPEC-RH2-FAK-001..003)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.testclient import TestClient

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.fake import FAKE_HEADER, FAKE_USAGE, FakeGraph, fake_builder

FAKE_WIRE = {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
    "total_tokens": 15,
    "calls": 1,
    "cost_usd": 0.0,
    "cost_source": "none",
    "estimated": True,
    "served_model": "fake/echo",
    "gateway": None,
}


def _app(**settings: Any) -> Any:
    return create_app(settings=HostSettings(host_fake_llm_mode=True, **settings))


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


# ── FAK-001: production refusal + test runtime ─────────────────────────────


def test_fake_mode_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="host_fake_llm_mode"):
        HostSettings(host_fake_llm_mode=True, host_environment="production")


@pytest.mark.parametrize("env", ["development", "staging"])
def test_fake_mode_allowed_outside_production(env: str) -> None:
    assert HostSettings(host_fake_llm_mode=True, host_environment=env)


async def test_fake_builder_uses_engine_test_runtime() -> None:
    runtime = await fake_builder(org_id="acme", config_source=object())
    try:
        assert runtime.org_id == "acme"
        assert type(runtime).__name__ == "RuntimeContext"
    finally:
        await runtime.aclose()


# ── FAK-002: the deterministic script ──────────────────────────────────────


async def test_echo_is_streamed_in_16_char_tokens_with_fixed_usage() -> None:
    content = "hello fake world, " * 20  # > 200 chars
    async with _client(_app()) as c:
        resp = await c.post("/threads/t1/messages", json={"content": content})
    events = _events(resp.text)
    tokens = [d["delta"] for e, d in events if e == "token"]
    assert "".join(tokens) == "echo: " + content[:200]
    assert all(0 < len(t) <= 16 for t in tokens)
    name, done = events[-1]
    assert name == "done" and done["usage"] == FAKE_WIRE
    assert done["usage"] == FAKE_USAGE


async def test_tool_command_emits_one_tool_call() -> None:
    async with _client(_app()) as c:
        resp = await c.post("/threads/t1/messages", json={"content": "/tool weather"})
    events = _events(resp.text)
    calls = [d for e, d in events if e == "tool_call"]
    assert calls == [{"name": "fake_tool", "args": {"q": "weather"}}]
    assert events[-1][0] == "done"


async def test_error_command_is_terminal_error() -> None:
    async with _client(_app()) as c:
        resp = await c.post("/threads/t1/messages", json={"content": "/error"})
    events = _events(resp.text)
    assert events[-1] == (
        "error",
        {"code": "internal_error", "message": "fake error"},
    )
    assert not any(e == "done" for e, _ in events)


async def test_script_is_deterministic_across_turns_and_threads() -> None:
    async with _client(_app()) as c:
        runs = [
            (await c.post(f"/threads/{t}/messages", json={"content": "same"})).text
            for t in ("a", "a", "b")
        ]
    tokens = [[d for e, d in _events(r) if e == "token"] for r in runs]
    assert tokens[0] == tokens[1] == tokens[2]


async def test_usage_is_recorded_per_turn() -> None:
    async with _client(_app()) as c:
        for _ in range(2):
            await c.post("/threads/t1/messages", json={"content": "hi"})
        usage = (await c.get("/v1/usage")).json()
    assert len(usage["turns"]) == 2
    assert usage["totals"]["total_tokens"] == 30


def test_graph_tags_chunks_for_list_stream_modes() -> None:
    """AG-UI asks for several stream modes: chunks are then ``(mode, payload)``."""
    import asyncio

    async def collect() -> list[Any]:
        graph = FakeGraph()
        cfg = {"configurable": {"thread_id": "t"}}
        inp = {"messages": [{"role": "user", "content": "hi"}]}
        return [c async for c in graph.astream(inp, cfg, stream_mode=["messages"])]

    chunks = asyncio.run(collect())
    assert chunks and all(c[0] == "messages" for c in chunks)


# ── FAK-003: the marker header ─────────────────────────────────────────────


async def test_every_http_response_carries_fake_header() -> None:
    async with _client(_app()) as c:
        responses = [
            await c.get("/healthz"),
            await c.get("/readyz"),
            await c.post("/threads/t1/messages", json={"content": "hi"}),
            await c.post("/threads/t1/messages", json={}),  # 422 too
            await c.get("/nope"),
        ]
    assert all(r.headers.get(FAKE_HEADER) == "1" for r in responses)
    assert responses[1].json() == {"status": "ready"}


async def test_header_absent_outside_fake_mode() -> None:
    app = create_app(settings=HostSettings())
    async with _client(app) as c:
        resp = await c.get("/healthz")
    assert FAKE_HEADER not in resp.headers


def test_websocket_handshake_carries_fake_header() -> None:
    app = _app(host_ws_enabled=True)
    with TestClient(app) as c, c.websocket_connect("/ws/threads/t1") as ws:
        assert (FAKE_HEADER.lower().encode(), b"1") in (ws.extra_headers or [])
        ws.send_json({"type": "message", "content": "hi"})
        frames = []
        while not frames or frames[-1]["event"] != "done":
            frames.append(ws.receive_json())
    assert "".join(f["data"]["delta"] for f in frames if f["event"] == "token") == (
        "echo: hi"
    )
