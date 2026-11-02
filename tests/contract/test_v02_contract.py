"""Contract: host v0.2 surfaces vs the released engine, fake-LLM mode (RH2-07-02).

No provider credential is needed: runtimes are the engine's own
``build_test_runtime()`` and chat comes from the deterministic fake script
(SPEC-RH2-FAK-*). What is real here is every engine piece the host orchestrates:
the AG-UI translator, the run-summary/usage wire, the Agent Card builder and the
inbound A2A v1 handler.

Run with: ``uv run pytest -m contract``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.contract

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "protocols" / "usage_wire.json"
FIXTURE_KEYS = [k["name"] for k in json.loads(_FIXTURE.read_text())["keys"]]


def _sse(body: str) -> list[tuple[str | None, Any]]:
    """``(event name | None, parsed data)`` per SSE frame; heartbeats dropped."""
    frames: list[tuple[str | None, Any]] = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = json.loads(line.removeprefix("data:").strip())
        if data is not None and name != "heartbeat":
            frames.append((name, data))
    return frames


@pytest.fixture
def engine_settings_cache() -> Iterator[None]:
    """Process engine settings re-read from env, then restored for other tests."""
    from prismal.a2a import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def fake_client(
    monkeypatch: pytest.MonkeyPatch, engine_settings_cache: None
) -> AsyncIterator[AsyncClient]:
    from prismal_server.app import create_app
    from prismal_server.config import HostSettings

    monkeypatch.setenv("PRISMAL_AGUI_EVENTS_ENABLED", "true")
    settings = HostSettings(host_fake_llm_mode=True, host_environment="development")
    app = create_app(settings=settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        yield client


# ── native SSE ───────────────────────────────────────────────────────────────


async def test_native_turn_is_scripted_with_fixture_shaped_usage(
    fake_client: AsyncClient,
) -> None:
    resp = await fake_client.post(
        "/threads/c-1/messages", json={"content": "hello contract suite"}
    )
    assert resp.status_code == 200
    assert resp.headers["x-prismal-fake-llm"] == "1"
    frames = _sse(resp.text)
    tokens = [d["delta"] for n, d in frames if n == "token"]
    assert "".join(tokens) == "echo: hello contract suite"
    assert all(len(t) <= 16 for t in tokens)
    name, done = frames[-1]
    assert name == "done" and done["thread_id"] == "c-1"
    assert list(done["usage"]) == FIXTURE_KEYS  # SPEC-RH2-USG-002 key order
    assert done["usage"]["served_model"] == "fake/echo"


async def test_native_tool_and_error_scripts(fake_client: AsyncClient) -> None:
    tool = _sse(
        (
            await fake_client.post("/threads/c-2/messages", json={"content": "/tool q"})
        ).text
    )
    calls = [d for n, d in tool if n == "tool_call"]
    assert len(calls) == 1
    assert (calls[0]["name"], calls[0]["args"]) == ("fake_tool", {"q": "q"})
    error = _sse(
        (
            await fake_client.post("/threads/c-3/messages", json={"content": "/error"})
        ).text
    )
    assert error[-1] == ("error", {"code": "internal_error", "message": "fake error"})


# ── AG-UI ────────────────────────────────────────────────────────────────────


async def test_agui_stream_is_framed_from_the_engine_translator(
    fake_client: AsyncClient,
) -> None:
    resp = await fake_client.post(
        "/threads/c-4/messages", params={"protocol": "ag-ui"}, json={"content": "hi"}
    )
    assert resp.status_code == 200
    events = [d for n, d in _sse(resp.text)]
    types = [e["type"] for e in events]
    assert types[0] == "RUN_STARTED" and types[-1] == "RUN_FINISHED"
    assert events[0]["threadId"] == "c-4"
    text = "".join(e.get("delta", "") for e in events if e["type"].startswith("TEXT"))
    assert text == "echo: hi"
    usage = [
        e for e in events if e["type"] == "CUSTOM" and e["name"] == "prismal.usage"
    ]
    assert usage and list(usage[0]["value"]) == FIXTURE_KEYS


async def test_agui_run_agent_input(fake_client: AsyncClient) -> None:
    resp = await fake_client.post(
        "/v1/agui",
        json={
            "threadId": "c-5",
            "runId": "r-1",
            "messages": [{"id": "m1", "role": "user", "content": "ping"}],
            "tools": [],
            "context": [],
            "state": {},
            "forwardedProps": {},
        },
    )
    assert resp.status_code == 200
    events = [d for _, d in _sse(resp.text)]
    assert events[-1]["type"] == "RUN_FINISHED"
    assert "echo: ping" in "".join(e.get("delta", "") for e in events)


# ── usage ────────────────────────────────────────────────────────────────────


async def test_usage_sums_recorded_fake_turns(fake_client: AsyncClient) -> None:
    for i in range(2):
        await fake_client.post(f"/threads/u-{i}/messages", json={"content": "x"})
    body = (await fake_client.get("/v1/usage")).json()
    assert body["totals"]["total_tokens"] == 30
    assert body["totals"]["calls"] == 2
    assert {t["thread_id"] for t in body["turns"]} == {"u-0", "u-1"}


# ── A2A v1.0 through the released engine handler ─────────────────────────────


class _ScriptedA2AGraph:
    """``ainvoke`` stand-in: the fake echo, as the A2A handler consumes it."""

    async def ainvoke(self, state: dict[str, Any], config: Any) -> dict[str, Any]:
        from langchain_core.messages import AIMessage

        text = state["messages"][-1].content
        return {"messages": [*state["messages"], AIMessage(content=f"echo: {text}")]}


class _NullAudit:
    def log_event(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest_asyncio.fixture
async def a2a_client(engine_settings_cache: None) -> AsyncIterator[AsyncClient]:
    from prismal.a2a import get_settings
    from prismal.a2a.server import A2AServerHandler
    from prismal.composition import build_test_runtime

    from prismal_server.app import create_app
    from prismal_server.config import HostSettings
    from prismal_server.deps import RuntimeRegistry

    engine = get_settings().model_copy(
        update={
            "a2a_enabled": True,
            "a2a_inbound_enabled": True,
            "a2a_strict": False,
            "a2a_protocol_version": "1.0",
            "a2a_accept_legacy": True,
        }
    )
    handler = A2AServerHandler(_ScriptedA2AGraph(), settings=engine, audit=_NullAudit())

    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        runtime = build_test_runtime(org_id=org_id, settings=engine)
        runtime.a2a_handler = handler
        return runtime

    settings = HostSettings(a2a_enabled=True, host_fake_llm_mode=True)
    registry = RuntimeRegistry(settings, builder=builder)
    app = create_app(settings=settings, registry=registry, a2a_engine_settings=engine)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        yield client


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


async def test_a2a_v1_card_is_versioned(a2a_client: AsyncClient) -> None:
    card = await a2a_client.get(
        "/.well-known/agent-card.json", params={"protocolVersion": "1.0"}
    )
    assert card.status_code == 200
    assert card.json()["supportedInterfaces"][0]["protocolVersion"] == "1.0"


async def test_a2a_v1_send_then_list_tasks(a2a_client: AsyncClient) -> None:
    v1 = {"A2A-Version": "1.0"}
    sent = await a2a_client.post(
        "/a2a",
        headers=v1,
        json=_rpc(
            "SendMessage",
            {
                "message": {
                    "messageId": "m-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "hello a2a"}],
                }
            },
        ),
    )
    assert sent.status_code == 200
    task = sent.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"  # v1 enum spelling
    assert task["artifacts"][0]["parts"] == [{"text": "echo: hello a2a"}]

    listed = await a2a_client.post("/a2a", headers=v1, json=_rpc("ListTasks"))
    assert listed.status_code == 200
    assert [t["id"] for t in listed.json()["result"]["tasks"]] == [task["id"]]
