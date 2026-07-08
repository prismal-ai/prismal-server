"""Tests for inbound A2A routes (RHB-03-*, SPEC-RHB-A2A-001..005)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from httpx import ASGITransport, AsyncClient

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry


class FakeHandler:
    """Stand-in for RuntimeContext.a2a_handler."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], Any]] = []

    async def handle_rpc(
        self, request: dict[str, Any], *, auth_ctx: Any = None
    ) -> dict[str, Any]:
        self.calls.append((request, auth_ctx))
        # Mimic the engine's strict gate: unauth → JSON-RPC -32001.
        if request.get("method") == "secure/op" and auth_ctx is None:
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32001, "message": "authentication required"},
            }
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {"echo": request.get("params")},
        }

    async def stream_rpc(
        self, request: dict[str, Any], *, auth_ctx: Any = None
    ) -> AsyncIterator[str]:
        self.calls.append((request, auth_ctx))
        yield 'data: {"kind":"artifact","n":1}\n\n'
        yield 'data: {"kind":"status","status":"completed"}\n\n'


def _registry_with_handler(handler: Any) -> RuntimeRegistry:
    async def builder(*, org_id: str | None) -> Any:
        return type("RT", (), {"a2a_handler": handler, "tool_provider": object()})()

    return RuntimeRegistry(HostSettings(), builder=builder)


def _make_app(
    *, handler: Any = None, a2a_enabled: bool = True, card: dict[str, Any] | None = None
):  # type: ignore[no-untyped-def]
    card = card if card is not None else {"name": "prismal", "protocolVersion": "0.3.0"}
    card_calls = {"n": 0}

    def card_builder(*, org_id: str | None) -> dict[str, Any]:
        card_calls["n"] += 1
        return card

    app = create_app(
        settings=HostSettings(a2a_enabled=a2a_enabled),
        registry=_registry_with_handler(handler),
        agent_card_builder=card_builder,
    )
    return app, card_calls


def _client(app):  # type: ignore[no-untyped-def]
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# --- Agent Card (RHB-03-01) --------------------------------------------------
async def test_agent_card_served_and_cached() -> None:
    app, card_calls = _make_app(card={"name": "prismal", "protocolVersion": "0.3.0"})
    async with _client(app) as c:
        r1 = await c.get("/.well-known/agent-card.json")
        r2 = await c.get("/.well-known/agent-card.json")
    assert r1.status_code == 200
    assert r1.json() == {"name": "prismal", "protocolVersion": "0.3.0"}
    # Cached per (settings, org): built once across two identical fetches.
    assert card_calls["n"] == 1
    assert r2.json() == r1.json()


async def test_agent_card_404_when_a2a_disabled() -> None:
    app, _ = _make_app(a2a_enabled=False)
    async with _client(app) as c:
        r = await c.get("/.well-known/agent-card.json")
    assert r.status_code == 404


# --- POST /a2a (RHB-03-02/03/04/05) ------------------------------------------
async def test_post_a2a_delegates_to_handler_unchanged() -> None:
    """Host passes the raw JSON-RPC to the handler verbatim (A2A-003)."""
    handler = FakeHandler()
    app, _ = _make_app(handler=handler)
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {"x": 1},
    }
    async with _client(app) as c:
        r = await c.post("/a2a", json=payload)
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "id": "1", "result": {"echo": {"x": 1}}}
    # The handler received exactly what the client sent — no host sanitize/mutate.
    seen_request, _ = handler.calls[0]
    assert seen_request == payload


async def test_post_a2a_strict_unauth_returns_minus_32001() -> None:
    """The engine handler enforces strict; the host relays its -32001 (A2A-004)."""
    handler = FakeHandler()
    app, _ = _make_app(handler=handler)
    payload = {"jsonrpc": "2.0", "id": "9", "method": "secure/op", "params": {}}
    async with _client(app) as c:
        r = await c.post("/a2a", json=payload)
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32001


async def test_post_a2a_streaming_uses_stream_rpc() -> None:
    handler = FakeHandler()
    app, _ = _make_app(handler=handler)
    payload = {"jsonrpc": "2.0", "id": "2", "method": "message/stream", "params": {}}
    async with _client(app) as c:
        r = await c.post("/a2a", json=payload)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert 'data: {"kind":"artifact","n":1}' in body
    assert '"status":"completed"' in body


async def test_post_a2a_404_when_disabled() -> None:
    app, _ = _make_app(handler=FakeHandler(), a2a_enabled=False)
    async with _client(app) as c:
        r = await c.post("/a2a", json={"jsonrpc": "2.0", "id": "1", "method": "x"})
    assert r.status_code == 404


async def test_post_a2a_handler_absent_returns_jsonrpc_error() -> None:
    """Engine a2a_inbound disabled → handler None → informative JSON-RPC error."""
    app, _ = _make_app(handler=None)
    async with _client(app) as c:
        r = await c.post(
            "/a2a", json={"jsonrpc": "2.0", "id": "7", "method": "message/send"}
        )
    body = r.json()
    assert body["id"] == "7"
    assert body["error"]["code"] != 0
