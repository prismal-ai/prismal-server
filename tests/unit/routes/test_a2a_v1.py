"""A2A v1.0 dual-stack through the host (RH2-04-01..04, SPEC-RH2-A2A-001..004)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from prismal.a2a.server import A2AServerHandler
from prismal.composition.host_surface import A2ARouteSpec
from prismal.identity import DID, AgentIdentity

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry


class RecordingHandler:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def handle_rpc(self, request: dict[str, Any], **kw: Any) -> dict[str, Any]:
        self.calls.append({"kind": "rpc", "request": request, **kw})
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}

    async def stream_rpc(
        self, request: dict[str, Any], **kw: Any
    ) -> AsyncIterator[str]:
        self.calls.append({"kind": "stream", "request": request, **kw})
        yield 'data: {"ok":true}\n\n'


class IdentityBackend:
    """Resolves ``Authorization: Bearer <org>`` to an identity of that org."""

    async def resolve(self, request: Any) -> Any:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        org = auth.removeprefix("Bearer ")
        return AgentIdentity(did=DID(f"did:key:z{org}"), agent_name=org, org_id=org)

    async def authenticate(self, request: Any) -> Any:
        return await self.resolve(request)


def _runtime(handler: Any, org_id: str | None, *, tenant_scoped: bool = True) -> Any:
    rpc = f"/{org_id}" if org_id else ""
    spec = A2ARouteSpec(
        card_path=rpc + "/.well-known/agent-card.json",
        rpc_path=rpc,
        tenant_scoped=bool(org_id) and tenant_scoped,
    )
    return SimpleNamespace(
        a2a_handler=handler,
        tool_provider=None,
        settings=SimpleNamespace(a2a_protocol_version="0.3", a2a_accept_legacy=True),
        a2a_routes=lambda **_: spec,
    )


def _app(handler: Any, **settings: Any) -> tuple[Any, list[dict[str, Any]]]:
    cards: list[dict[str, Any]] = []
    scoped = settings.pop("_scoped", True)

    def card_builder(*, org_id: str | None, **kw: Any) -> dict[str, Any]:
        cards.append({"org_id": org_id, **kw})
        return {"name": "prismal", "org": org_id, "version": kw.get("protocol_version")}

    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        return _runtime(handler, org_id, tenant_scoped=scoped)

    host = HostSettings(**settings)
    app = create_app(
        settings=host,
        registry=RuntimeRegistry(host, builder=builder),
        agent_card_builder=card_builder,
        auth_backend=IdentityBackend(),
        a2a_engine_settings=SimpleNamespace(
            a2a_base_url=None, a2a_protocol_version="0.3", a2a_accept_legacy=True
        ),
    )
    return app, cards


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _rpc(method: str, **params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


# ── RH2-04-01: headers + org_id forwarding ─────────────────────────────────


async def test_forwards_version_header_and_org_but_not_credentials() -> None:
    handler = RecordingHandler()
    app, _ = _app(handler)
    async with _client(app) as c:
        await c.post(
            "/a2a",
            json=_rpc("ListTasks"),
            headers={
                "A2A-Version": "1.0",
                "Authorization": "Bearer acme",
                "Cookie": "session=secret",
            },
        )
    call = handler.calls[0]
    assert call["org_id"] == "acme"
    headers = {k.lower(): v for k, v in call["headers"].items()}
    assert headers["a2a-version"] == "1.0"
    assert "authorization" not in headers and "cookie" not in headers


async def test_query_version_becomes_header() -> None:
    handler = RecordingHandler()
    app, _ = _app(handler)
    async with _client(app) as c:
        await c.post("/a2a", params={"A2A-Version": "1.0"}, json=_rpc("GetTask"))
    headers = {k.lower(): v for k, v in handler.calls[0]["headers"].items()}
    assert headers["a2a-version"] == "1.0"


@pytest.mark.parametrize(
    ("method", "kind"),
    [
        ("SendStreamingMessage", "stream"),
        ("message/stream", "stream"),
        ("SendMessage", "rpc"),
        ("ListTasks", "rpc"),
        ("message/send", "rpc"),
    ],
)
async def test_streaming_methods_come_from_engine(method: str, kind: str) -> None:
    handler = RecordingHandler()
    app, _ = _app(handler)
    async with _client(app) as c:
        resp = await c.post("/a2a", json=_rpc(method))
    assert handler.calls[0]["kind"] == kind
    if kind == "stream":
        assert resp.headers["content-type"].startswith("text/event-stream")


# ── RH2-04-02: versioned card ──────────────────────────────────────────────


async def test_card_defaults_to_engine_version() -> None:
    app, cards = _app(RecordingHandler())
    async with _client(app) as c:
        resp = await c.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    assert cards[-1]["protocol_version"] is None


@pytest.mark.parametrize("version", ["0.3", "1.0"])
async def test_card_protocol_version_param(version: str) -> None:
    app, cards = _app(RecordingHandler())
    async with _client(app) as c:
        resp = await c.get(
            "/.well-known/agent-card.json", params={"protocolVersion": version}
        )
    assert resp.status_code == 200
    assert cards[-1]["protocol_version"] == version


async def test_card_invalid_version_is_422() -> None:
    app, _ = _app(RecordingHandler())
    async with _client(app) as c:
        resp = await c.get(
            "/.well-known/agent-card.json", params={"protocolVersion": "2.0"}
        )
    assert resp.status_code == 422


async def test_other_stack_card_needs_accept_legacy() -> None:
    handler = RecordingHandler()
    app, _ = _app(handler)
    app.state.a2a_engine_settings = SimpleNamespace(
        a2a_base_url=None, a2a_protocol_version="1.0", a2a_accept_legacy=False
    )
    async with _client(app) as c:
        other = await c.get(
            "/.well-known/agent-card.json", params={"protocolVersion": "0.3"}
        )
        same = await c.get(
            "/.well-known/agent-card.json", params={"protocolVersion": "1.0"}
        )
    assert other.status_code == 404 and same.status_code == 200


# ── RH2-04-02/03: tenant-scoped routes ─────────────────────────────────────


async def test_tenant_rpc_route_scoped_to_identity_org() -> None:
    handler = RecordingHandler()
    app, _ = _app(handler)
    async with _client(app) as c:
        resp = await c.post(
            "/acme", json=_rpc("ListTasks"), headers={"Authorization": "Bearer acme"}
        )
    assert resp.status_code == 200
    assert handler.calls[0]["org_id"] == "acme"


@pytest.mark.parametrize("auth", [{"Authorization": "Bearer beta"}, {}])
async def test_tenant_mismatch_is_403_without_handler(auth: dict[str, str]) -> None:
    """EARS A2A-003."""
    handler = RecordingHandler()
    app, _ = _app(handler)
    async with _client(app) as c:
        resp = await c.post("/acme", json=_rpc("ListTasks"), headers=auth)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden_tenant"
    assert handler.calls == []


async def test_tenant_route_must_match_engine_route_spec() -> None:
    handler = RecordingHandler()
    app, _ = _app(handler, _scoped=False)
    async with _client(app) as c:
        resp = await c.post(
            "/acme", json=_rpc("ListTasks"), headers={"Authorization": "Bearer acme"}
        )
    assert resp.status_code == 404 and handler.calls == []


async def test_tenant_card_route() -> None:
    app, cards = _app(RecordingHandler())
    async with _client(app) as c:
        resp = await c.get("/acme/.well-known/agent-card.json")
    assert resp.status_code == 200 and cards[-1]["org_id"] == "acme"


async def test_existing_routes_win_over_tenant_routes() -> None:
    handler = RecordingHandler()
    app, _ = _app(handler)
    async with _client(app) as c:
        resp = await c.post("/threads")
    assert "thread_id" in resp.json() and handler.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/a2a"),
        ("POST", "/acme"),
        ("GET", "/.well-known/agent-card.json"),
        ("GET", "/acme/.well-known/agent-card.json"),
    ],
)
async def test_all_a2a_routes_404_when_disabled(method: str, path: str) -> None:
    """A2A-004: v0.1 404 holds for both stacks and the tenant routes."""
    app, _ = _app(RecordingHandler(), a2a_enabled=False)
    async with _client(app) as c:
        resp = await c.request(
            method,
            path,
            json=_rpc("ListTasks"),
            headers={"Authorization": "Bearer acme"},
        )
    assert resp.status_code == 404


# ── RH2-04-04: smoke against the real engine handler ──────────────────────


class EchoGraph:
    async def ainvoke(self, state: Any, config: Any = None) -> Any:
        return {"messages": [AIMessage(content="pong")]}


def _real_app() -> Any:
    from prismal.a2a import get_settings

    handler = A2AServerHandler(EchoGraph(), settings=get_settings())

    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        return _runtime(handler, org_id)

    host = HostSettings()
    return create_app(
        settings=host,
        registry=RuntimeRegistry(host, builder=builder),
        auth_backend=IdentityBackend(),
    )


async def test_v1_and_v03_clients_with_list_tasks_through_host() -> None:
    app = _real_app()
    v1 = {"A2A-Version": "1.0"}
    send_v1 = _rpc(
        "SendMessage",
        message={"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "ping"}]},
    )
    send_v03 = _rpc(
        "message/send",
        message={
            "messageId": "m2",
            "role": "user",
            "parts": [{"kind": "text", "text": "hi"}],
        },
    )
    async with _client(app) as c:
        r1 = await c.post(
            "/acme", json=send_v1, headers={**v1, "Authorization": "Bearer acme"}
        )
        r2 = await c.post(
            "/a2a", json=send_v03, headers={"Authorization": "Bearer acme"}
        )
        acme = await c.post(
            "/acme",
            json=_rpc("ListTasks"),
            headers={**v1, "Authorization": "Bearer acme"},
        )
        beta = await c.post(
            "/beta",
            json=_rpc("ListTasks"),
            headers={**v1, "Authorization": "Bearer beta"},
        )
    assert r1.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert r2.json()["result"]["status"] == "completed"
    acme_ids = {t["id"] for t in acme.json()["result"]["tasks"]}
    assert r1.json()["result"]["task"]["id"] in acme_ids
    assert beta.json()["result"]["tasks"] == []  # tenant isolation in the engine


async def test_unknown_single_segment_paths_stay_404() -> None:
    app, _ = _app(RecordingHandler())
    async with _client(app) as c:
        for method in ("GET", "PUT", "DELETE"):
            resp = await c.request(method, "/whatever")
            assert resp.status_code == 404, method
