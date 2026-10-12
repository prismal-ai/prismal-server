"""AG-UI negotiation and ``POST /v1/agui`` (RH2-03-01/03, SPEC-RH2-AGU-001/003/005)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry
from prismal_server.usage.store import MemoryUsageStore

WIRE = {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
    "total_tokens": 15,
    "calls": 1,
    "cost_usd": 0.0001,
    "cost_source": "gateway",
    "estimated": False,
    "served_model": "m/a",
    "gateway": "openrouter",
}


class Summary:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    def to_usage_wire(self) -> dict[str, Any]:
        return dict(WIRE)


def _engine_settings(enabled: bool = True) -> Any:
    return SimpleNamespace(
        agui_events_enabled=enabled,
        agui_include_state_deltas=True,
        agui_include_usage=True,
        agui_include_metadata=True,
    )


class Harness:
    def __init__(self, *, host_agui: bool = True, engine_agui: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.summaries: dict[str, Summary] = {}
        self.store = MemoryUsageStore(retention_days=90)
        self.fail_midstream = False
        harness = self

        async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
            return SimpleNamespace(
                tool_provider=None, settings=_engine_settings(engine_agui)
            )

        class Graph:
            def astream(self, inp: Any, config: Any, *, stream_mode: Any = None) -> Any:
                harness.calls.append(
                    {"input": inp, "config": config, "mode": stream_mode}
                )
                thread = config["configurable"]["thread_id"]

                tagged = isinstance(stream_mode, list)  # multi-mode ⇒ (mode, data)

                async def gen() -> AsyncIterator[Any]:
                    harness.summaries[thread] = Summary(f"{thread}.turn1")
                    msg = AIMessageChunk(content="Hello", id="msg-1")
                    item = (msg, {"langgraph_node": "agent"})
                    yield ("messages", item) if tagged else item
                    if harness.fail_midstream:
                        raise RuntimeError("boom")
                    if tagged:
                        yield ("updates", {"agent": {"current_agent": "agent"}})

                return gen()

        async def graph_factory(*, tool_provider: Any = None) -> Graph:
            return Graph()

        settings = HostSettings(host_agui_enabled=host_agui, sse_heartbeat_s=1)
        self.app = create_app(
            settings=settings,
            registry=RuntimeRegistry(settings, builder=builder),
            graph_factory=graph_factory,
            usage_store=self.store,
            run_summary=lambda t: self.summaries.get(t),
        )

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://t")


def _events(text: str) -> list[dict[str, Any]]:
    assert "event:" not in text
    return [
        json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")
    ]


@pytest.mark.parametrize(
    ("params", "headers"),
    [
        ({"protocol": "ag-ui"}, {}),
        ({}, {"Accept": "text/event-stream; profile=ag-ui"}),
        ({}, {"Accept": 'text/event-stream;profile="ag-ui"'}),
    ],
)
async def test_negotiates_agui(params: dict[str, str], headers: dict[str, str]) -> None:
    h = Harness()
    async with h.client() as c:
        resp = await c.post(
            "/threads/t-1/messages",
            params=params,
            json={"content": "hi"},
            headers=headers,
        )
    assert resp.status_code == 200
    assert resp.headers["x-prismal-stream-protocol"] == "ag-ui"
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _events(resp.text)
    assert events[0]["type"] == "RUN_STARTED" and events[0]["threadId"] == "t-1"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [e.get("name") for e in events if e["type"] == "CUSTOM"] == ["prismal.usage"]
    assert h.calls[0]["mode"] == ["messages", "updates", "custom"]


async def test_default_stays_native() -> None:
    h = Harness()
    async with h.client() as c:
        resp = await c.post("/threads/t-1/messages", json={"content": "hi"})
    assert "x-prismal-stream-protocol" not in resp.headers
    assert "event: token" in resp.text and "event: done" in resp.text
    assert h.calls[0]["mode"] == "messages"


@pytest.mark.parametrize(("host_agui", "engine_agui"), [(False, True), (True, False)])
async def test_disabled_is_406_before_streaming(
    host_agui: bool, engine_agui: bool
) -> None:
    h = Harness(host_agui=host_agui, engine_agui=engine_agui)
    async with h.client() as c:
        resp = await c.post(
            "/threads/t-1/messages",
            params={"protocol": "ag-ui"},
            json={"content": "hi"},
        )
    assert resp.status_code == 406
    assert resp.json()["error"]["code"] == "protocol_not_enabled"
    assert h.calls == []


async def test_unknown_protocol_is_422() -> None:
    h = Harness()
    async with h.client() as c:
        resp = await c.post(
            "/threads/t-1/messages",
            params={"protocol": "graphql"},
            json={"content": "hi"},
        )
    assert resp.status_code == 422


async def test_midstream_error_is_run_error_with_native_code() -> None:
    h = Harness()
    h.fail_midstream = True
    async with h.client() as c:
        resp = await c.post(
            "/threads/t-1/messages",
            params={"protocol": "ag-ui"},
            json={"content": "hi"},
        )
    last = _events(resp.text)[-1]
    assert last["type"] == "RUN_ERROR"
    assert (
        last["code"] == "internal_error" and last["message"] == "Internal Server Error"
    )


async def test_agui_turn_records_usage_and_scopes_thread() -> None:
    h = Harness()
    async with h.client() as c:
        await c.post(
            "/threads/t-1/messages",
            params={"protocol": "ag-ui"},
            json={"content": "hi"},
            headers={"X-Org-Id": "acme"},
        )
    assert h.calls[0]["config"]["configurable"]["thread_id"] == "4:acme:t-1"
    now = datetime.now(UTC)
    rows = await h.store.query(
        org_id="acme", thread_id=None, since=now - timedelta(minutes=1), until=now
    )
    assert len(rows) == 1 and rows[0].thread_id == "t-1"


# ── POST /v1/agui (RunAgentInput) ─────────────────────────────────────────


def _run_input(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "threadId": "t-7",
        "runId": "run-42",
        "messages": [
            {"id": "m1", "role": "user", "content": "first"},
            {"id": "m2", "role": "assistant", "content": "ok"},
            {"id": "m3", "role": "user", "content": "second"},
        ],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    body.update(over)
    return body


async def test_run_agent_input_uses_last_user_message() -> None:
    h = Harness()
    async with h.client() as c:
        resp = await c.post("/v1/agui", json=_run_input())
    assert resp.status_code == 200
    assert resp.headers["x-prismal-stream-protocol"] == "ag-ui"
    assert h.calls[0]["input"] == {"messages": [{"role": "user", "content": "second"}]}
    started = _events(resp.text)[0]
    assert started["runId"] == "run-42" and started["threadId"] == "t-7"


async def test_run_agent_input_text_parts_are_joined() -> None:
    h = Harness()
    content = [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]
    async with h.client() as c:
        await c.post(
            "/v1/agui",
            json=_run_input(messages=[{"id": "m", "role": "user", "content": content}]),
        )
    assert h.calls[0]["input"]["messages"][0]["content"] == "hello world"


async def test_client_tools_are_ignored_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = Harness()
    tools = [{"name": "client_tool", "description": "x", "parameters": {}}]
    with caplog.at_level(logging.INFO):
        async with h.client() as c:
            resp = await c.post("/v1/agui", json=_run_input(tools=tools))
    assert resp.status_code == 200
    assert "client_tool" in caplog.text
    assert "tools" not in h.calls[0]["input"]


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"id": "m", "role": "assistant", "content": "x"}],
        [{"id": "m", "role": "user", "content": ""}],
    ],
)
async def test_run_agent_input_without_user_text_is_422(messages: list[Any]) -> None:
    h = Harness()
    async with h.client() as c:
        resp = await c.post("/v1/agui", json=_run_input(messages=messages))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] in {"invalid_input", "validation_error"}
    assert h.calls == []


async def test_v1_agui_disabled_is_406() -> None:
    h = Harness(host_agui=False)
    async with h.client() as c:
        resp = await c.post("/v1/agui", json=_run_input())
    assert resp.status_code == 406
