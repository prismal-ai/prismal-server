"""WebSocket transport (RH2-05-01..04, SPEC-RH2-WSK-001..003)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk
from prismal.identity import DID, AgentIdentity
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from prismal_server import metrics
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


class Msg:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_call_chunks: list[Any] = []


class Summary:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    def to_usage_wire(self) -> dict[str, Any]:
        return dict(WIRE)


class BudgetExceeded(Exception):
    def __init__(self) -> None:
        self.dimension, self.used, self.limit, self.scope = "cost", 50.2, 50.0, "tenant"
        super().__init__("Budget exceeded on cost (tenant): 50.2 >= 50.0")


class IdentityBackend:
    """``Authorization: Bearer <org>`` ⇒ an identity of that org."""

    async def resolve(self, request: Any) -> Any:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        org = auth.removeprefix("Bearer ")
        return AgentIdentity(did=DID(f"did:key:z{org}"), agent_name=org, org_id=org)


class World:
    """App + recorded graph calls; ``stream`` decides what each turn yields."""

    def __init__(self, stream: Any = None, *, agui: bool = False, **settings: Any):
        self.calls: list[dict[str, Any]] = []
        self.summaries: dict[str, Summary] = {}
        self.store = MemoryUsageStore(retention_days=90)
        self.stream = stream or self.two_tokens
        engine = SimpleNamespace(agui_events_enabled=agui)

        async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
            return SimpleNamespace(tool_provider=None, settings=engine)

        world = self

        class Graph:
            def astream(self, inp: Any, config: Any, *, stream_mode: Any = None):  # type: ignore[no-untyped-def]
                world.calls.append(
                    {"input": inp, "config": config, "stream_mode": stream_mode}
                )
                return world.stream(config["configurable"]["thread_id"], stream_mode)

        async def graph_factory(*, tool_provider: Any = None) -> Graph:
            return Graph()

        settings.setdefault("host_ws_enabled", True)
        host = HostSettings(**settings)
        self.registry = RuntimeRegistry(host, builder=builder)
        self.app = create_app(
            settings=host,
            registry=self.registry,
            graph_factory=graph_factory,
            usage_store=self.store,
            run_summary=lambda thread_id: self.summaries.get(thread_id),
            auth_backend=IdentityBackend(),
        )

    async def two_tokens(self, thread_id: str, mode: Any) -> AsyncIterator[Any]:
        self.summaries[thread_id] = Summary(f"{thread_id}.{len(self.calls)}")
        if isinstance(mode, list):
            yield ("messages", (AIMessageChunk(content="Ho", id="m1"), {}))
            yield ("messages", (AIMessageChunk(content="la", id="m1"), {}))
        else:
            yield (Msg("Ho"), {})
            yield (Msg("la"), {})

    def leases(self) -> int:
        return sum(e.leases for e in self.registry._entries.values())

    def client(self) -> TestClient:
        return TestClient(self.app)


def _msg(content: str = "hi", **kw: Any) -> dict[str, Any]:
    return {"type": "message", "content": content, **kw}


def _until(ws: Any, event: str) -> list[dict[str, Any]]:
    """Receive frames up to and including the first ``event`` (heartbeats kept)."""
    frames: list[dict[str, Any]] = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["event"] == event:
            return frames


def _close_code(ws: Any) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        while True:
            ws.receive_json()
    return exc.value.code


# ── RH2-05-01: handshake ───────────────────────────────────────────────────


def test_route_absent_when_disabled() -> None:
    world = World(host_ws_enabled=False)
    with world.client() as c, pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/threads/t1") as ws:
            ws.receive_json()


def test_strict_unauthenticated_handshake_is_1008() -> None:
    world = World(host_auth_strict=True)
    with world.client() as c, pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws/threads/t1"):
            pass
    assert exc.value.code == 1008
    assert world.calls == []


def test_disallowed_origin_is_1008() -> None:
    world = World(cors_origins=["https://app.example"])
    with world.client() as c, pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect(
            "/ws/threads/t1", headers={"Origin": "https://evil.example"}
        ):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("origin", ["https://app.example", None])
def test_allowed_or_absent_origin_connects(origin: str | None) -> None:
    world = World(cors_origins=["https://app.example"])
    headers = {"Origin": origin} if origin else {}
    with (
        world.client() as c,
        c.websocket_connect("/ws/threads/t1", headers=headers) as ws,
    ):
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"event": "pong"}


def test_header_cannot_select_another_tenant() -> None:
    """Tenant selection is fail-closed, as on the A2A and read routes."""
    world = World(host_auth_backend="bearer")
    with world.client() as c, pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect(
            "/ws/threads/t1",
            headers={"Authorization": "Bearer acme", "X-Org-Id": "beta"},
        ):
            pass
    assert exc.value.code == 1008


# ── RH2-05-02: frame protocol ──────────────────────────────────────────────


def test_native_turn_mirrors_sse_events_and_records_usage() -> None:
    world = World(host_auth_backend="bearer")
    with (
        world.client() as c,
        c.websocket_connect(
            "/ws/threads/t1", headers={"Authorization": "Bearer acme"}
        ) as ws,
    ):
        ws.send_json(_msg("hello"))
        frames = [f for f in _until(ws, "done") if f["event"] != "heartbeat"]
    assert frames[:2] == [
        {"event": "token", "data": {"delta": "Ho"}},
        {"event": "token", "data": {"delta": "la"}},
    ]
    done = frames[-1]["data"]
    assert done["thread_id"] == "t1" and done["finish_reason"] == "stop"
    assert done["usage"] == WIRE
    cfg = world.calls[0]["config"]["configurable"]
    assert cfg == {"thread_id": "4:acme:t1", "org_id": "acme"}
    assert world.calls[0]["input"]["messages"][0] == {
        "role": "user",
        "content": "hello",
    }
    now = datetime.now(UTC)
    records = asyncio.run(
        world.store.query(
            org_id="acme",
            thread_id=None,
            since=now - timedelta(minutes=1),
            until=now + timedelta(minutes=1),
        )
    )
    assert len(records) == 1 and records[0].thread_id == "t1"


def test_connection_serves_several_turns() -> None:
    world = World()
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        for _ in range(2):
            ws.send_json(_msg())
            _until(ws, "done")
    assert len(world.calls) == 2


def test_agui_turn_sends_engine_events() -> None:
    world = World(agui=True)
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        ws.send_json(_msg(protocol="ag-ui"))
        frames = []
        while True:
            frame = ws.receive_json()
            if frame["event"] == "heartbeat":
                continue
            frames.append(frame)
            if frame["data"]["type"] in {"RUN_FINISHED", "RUN_ERROR"}:
                break
    assert {f["event"] for f in frames} == {"agui"}
    types = [f["data"]["type"] for f in frames]
    assert types[0] == "RUN_STARTED" and types[-1] == "RUN_FINISHED"
    assert "TEXT_MESSAGE_CONTENT" in types
    assert frames[0]["data"]["threadId"] == "t1"
    assert world.calls[0]["stream_mode"] == ["messages", "updates", "custom"]


def test_agui_not_enabled_is_error_frame_and_connection_stays() -> None:
    world = World(agui=False)
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        ws.send_json(_msg(protocol="ag-ui"))
        frame = ws.receive_json()
        assert frame["event"] == "error"
        assert frame["data"]["code"] == "protocol_not_enabled"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"event": "pong"}
    assert world.calls == []


def test_budget_refusal_is_error_frame_and_connection_stays() -> None:
    async def refused(thread_id: str, mode: Any) -> AsyncIterator[Any]:
        raise BudgetExceeded()
        yield  # pragma: no cover

    world = World(refused)
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        ws.send_json(_msg())
        frame = _until(ws, "error")[-1]
        assert frame["data"]["code"] == "budget_exceeded"
        assert frame["data"]["scope"] == "tenant"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"event": "pong"}


def test_raw_scoped_thread_id_is_error_frame() -> None:
    world = World()
    with world.client() as c, c.websocket_connect("/ws/threads/4:acme:x") as ws:
        ws.send_json(_msg())
        assert ws.receive_json()["data"]["code"] == "invalid_thread_id"


# ── RH2-05-02: one turn at a time, cancel ──────────────────────────────────


class Endless:
    def __init__(self) -> None:
        self.closed = 0

    async def __call__(self, thread_id: str, mode: Any) -> AsyncIterator[Any]:
        try:
            while True:
                yield (Msg("tok"), {})
                await asyncio.sleep(0.01)
        finally:
            self.closed += 1


def test_cancel_stops_turn_and_reports_cancelled() -> None:
    endless = Endless()
    world = World(endless)
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        ws.send_json(_msg())
        assert ws.receive_json()["event"] == "token"
        ws.send_json({"type": "cancel"})
        frame = _until(ws, "error")[-1]
        assert frame["data"]["code"] == "cancelled"
        assert endless.closed == 1
        assert world.leases() == 0  # cancelled turn released its runtime
        # The connection accepts the next turn.
        world.stream = world.two_tokens
        ws.send_json(_msg())
        assert _until(ws, "done")[-1]["event"] == "done"


def test_message_during_turn_closes_4409() -> None:
    endless = Endless()
    world = World(endless)
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        ws.send_json(_msg())
        assert ws.receive_json()["event"] == "token"
        ws.send_json(_msg())
        assert _close_code(ws) == 4409
    assert endless.closed == 1
    assert metrics.get("ws_sessions_total", close_code="4409") >= 1


def test_cancel_without_turn_is_ignored() -> None:
    world = World()
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        ws.send_json({"type": "cancel"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"event": "pong"}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        json.dumps({"type": "nope"}),
        json.dumps({"type": "message"}),
        json.dumps({"type": "message", "content": ""}),
        json.dumps({"type": "message", "content": "x", "protocol": "grpc"}),
        json.dumps({"type": "message", "content": "x", "metadata": "m"}),
    ],
)
def test_malformed_frame_closes_4400(raw: str) -> None:
    world = World()
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        ws.send_text(raw)
        assert _close_code(ws) == 4400
    assert world.calls == []


# ── RH2-05-03: heartbeat + size limit ──────────────────────────────────────


def test_oversized_frame_closes_1009() -> None:
    world = World(host_ws_max_message_bytes=64)
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        ws.send_json(_msg("x" * 100))
        assert _close_code(ws) == 1009
    assert world.calls == []


def test_idle_connection_gets_heartbeats() -> None:
    world = World(sse_heartbeat_s=1)
    with world.client() as c, c.websocket_connect("/ws/threads/t1") as ws:
        assert ws.receive_json() == {"event": "heartbeat"}


def test_disconnect_mid_turn_closes_graph_stream() -> None:
    endless = Endless()
    world = World(endless)
    with world.client() as c:
        with c.websocket_connect("/ws/threads/t1") as ws:
            ws.send_json(_msg())
            assert ws.receive_json()["event"] == "token"
    assert endless.closed == 1
    assert world.leases() == 0
