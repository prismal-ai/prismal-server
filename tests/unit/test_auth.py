"""Tests for the authentication seam and tenant resolution (RHB-04-*).

Covers SPEC-RHB-AUT-001..006 (backend contract, NoAuth/Bearer/OIDC, strict 401,
config-driven selection) and SPEC-RHB-TEN-001 (org_id resolution order).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from prismal_server.app import create_app
from prismal_server.auth import (
    BearerTokenBackend,
    NoAuthBackend,
    OidcAuthBackend,
    build_auth_backend,
    resolve_org_id,
)
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry


def _request(headers: dict[str, str] | None = None) -> Any:
    """A minimal stand-in for a Starlette ``Request`` (only ``.headers`` used)."""
    return SimpleNamespace(headers=headers or {})


# --- AUT-002: NoAuthBackend -------------------------------------------------


async def test_noauth_returns_fixed_configured_identity() -> None:
    backend = NoAuthBackend(did="did:key:zLocalDev", agent_name="local-dev")
    identity = await backend.resolve(_request())
    assert identity is not None
    assert identity.did == "did:key:zLocalDev"
    assert identity.agent_name == "local-dev"


# --- AUT-003: BearerTokenBackend --------------------------------------------


async def test_bearer_maps_known_token_to_identity() -> None:
    backend = BearerTokenBackend(
        static_tokens={"tok-123": SecretStr("did:key:zAcme")},
        agent_name="svc",
    )
    identity = await backend.resolve(_request({"Authorization": "Bearer tok-123"}))
    assert identity is not None
    assert identity.did == "did:key:zAcme"
    assert identity.agent_name == "svc"


async def test_bearer_unknown_token_resolves_to_none() -> None:
    backend = BearerTokenBackend(static_tokens={"tok-123": SecretStr("did:x")})
    assert await backend.resolve(_request({"Authorization": "Bearer nope"})) is None


async def test_bearer_missing_or_malformed_header_never_raises() -> None:
    backend = BearerTokenBackend(static_tokens={"tok-123": SecretStr("did:x")})
    # AUT-001: an unauthenticated request returns None, never raises.
    assert await backend.resolve(_request()) is None
    assert await backend.resolve(_request({"Authorization": "Basic abc"})) is None
    assert await backend.resolve(_request({"Authorization": "Bearer"})) is None


# --- AUT-004: OidcAuthBackend delegates to the engine provider --------------


class _FakeProvider:
    """Stand-in for ``prismal.identity.OidcIdentityProvider`` (sync verify/resolve)."""

    def __init__(self, *, valid: set[str], raises: bool = False) -> None:
        self._valid = valid
        self._raises = raises

    def verify(self, did: str) -> bool:
        if self._raises:
            raise RuntimeError("provider boom")
        return did in self._valid

    def resolve(self, did: str) -> Any:
        from prismal.identity import DID, AgentIdentity

        return AgentIdentity(did=DID(did), agent_name="oidc", org_id="acme")


async def test_oidc_delegates_verify_and_resolve() -> None:
    provider = _FakeProvider(valid={"did:web:example.com"})
    backend = OidcAuthBackend(provider=provider)
    identity = await backend.resolve(
        _request({"Authorization": "Bearer did:web:example.com"})
    )
    assert identity is not None
    assert identity.did == "did:web:example.com"
    assert identity.org_id == "acme"


async def test_oidc_invalid_token_resolves_to_none() -> None:
    backend = OidcAuthBackend(provider=_FakeProvider(valid=set()))
    got = await backend.resolve(_request({"Authorization": "Bearer did:web:bad"}))
    assert got is None


async def test_oidc_provider_error_never_raises() -> None:
    """AUT-001: even a provider that raises must yield None, not propagate."""
    backend = OidcAuthBackend(provider=_FakeProvider(valid=set(), raises=True))
    assert await backend.resolve(_request({"Authorization": "Bearer x"})) is None


# --- AUT-006: config-driven backend selection -------------------------------


def test_build_auth_backend_selects_by_config() -> None:
    assert isinstance(
        build_auth_backend(HostSettings(host_auth_backend="none")), NoAuthBackend
    )
    assert isinstance(
        build_auth_backend(HostSettings(host_auth_backend="bearer")),
        BearerTokenBackend,
    )
    assert isinstance(
        build_auth_backend(HostSettings(host_auth_backend="oidc")), OidcAuthBackend
    )


# --- TEN-001: org_id resolution order ---------------------------------------


def test_resolve_org_id_prefers_identity_over_header() -> None:
    from prismal.identity import DID, AgentIdentity

    ident = AgentIdentity(did=DID("did:x"), agent_name="a", org_id="from-identity")
    req = _request({"X-Org-Id": "from-header"})
    assert resolve_org_id(req, ident) == "from-identity"


def test_resolve_org_id_falls_back_to_header_then_none() -> None:
    from prismal.identity import DID, AgentIdentity

    ident_no_org = AgentIdentity(did=DID("did:x"), agent_name="a")
    assert resolve_org_id(_request({"X-Org-Id": "acme"}), ident_no_org) == "acme"
    assert resolve_org_id(_request({"X-Org-Id": "acme"}), None) == "acme"
    assert resolve_org_id(_request(), None) is None


# --- AUT-005: strict mode → 401 on protected routes -------------------------


class _NullBackend:
    """Always-unauthenticated backend (resolves to None)."""

    async def resolve(self, request: Any) -> Any:
        return None


def _fake_registry() -> RuntimeRegistry:
    async def builder(*, org_id: str | None) -> Any:
        return type("RT", (), {"tool_provider": object(), "org_id": org_id})()

    return RuntimeRegistry(HostSettings(), builder=builder)


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_strict_unauthenticated_gets_401_on_threads() -> None:
    app = create_app(
        registry=_fake_registry(),
        settings=HostSettings(host_auth_strict=True),
        auth_backend=_NullBackend(),
    )
    async with _client(app) as c:
        resp = await c.post("/threads/t/messages", json={"content": "hi"})
    assert resp.status_code == 401


async def test_health_stays_public_under_strict() -> None:
    """AUT-005: health is public even in strict mode."""
    app = create_app(
        registry=_fake_registry(),
        settings=HostSettings(host_auth_strict=True),
        auth_backend=_NullBackend(),
    )
    async with _client(app) as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200


async def test_nonstrict_unauthenticated_is_allowed() -> None:
    """Without strict mode, an unauthenticated request proceeds (org_id=None)."""

    class FakeGraph:
        def astream(self, inp, config, *, stream_mode=None):  # type: ignore[no-untyped-def]
            async def _gen() -> Any:
                for _ in ():
                    yield None

            return _gen()

    async def graph_factory(*, tool_provider: Any = None) -> Any:
        return FakeGraph()

    app = create_app(
        registry=_fake_registry(),
        settings=HostSettings(host_auth_strict=False),
        auth_backend=_NullBackend(),
        graph_factory=graph_factory,
    )
    async with _client(app) as c:
        resp = await c.post("/threads/t/messages", json={"content": "hi"})
    assert resp.status_code == 200


async def test_identity_org_id_threaded_into_graph_config() -> None:
    """A bearer identity carrying no org still lets X-Org-Id select the tenant,
    and the resolved org reaches config.configurable.org_id (TEN-001, THR-003)."""
    calls: list[dict[str, Any]] = []

    class FakeGraph:
        def astream(self, inp, config, *, stream_mode=None):  # type: ignore[no-untyped-def]
            calls.append({"config": config})

            async def _gen() -> Any:
                for _ in ():
                    yield None

            return _gen()

    async def graph_factory(*, tool_provider: Any = None) -> Any:
        return FakeGraph()

    backend = BearerTokenBackend(
        static_tokens={"tok": SecretStr("did:key:zAcme")}, agent_name="svc"
    )
    app = create_app(
        registry=_fake_registry(),
        settings=HostSettings(host_auth_backend="bearer"),
        auth_backend=backend,
        graph_factory=graph_factory,
    )
    async with _client(app) as c:
        await c.post(
            "/threads/t/messages",
            json={"content": "hi"},
            headers={"Authorization": "Bearer tok", "X-Org-Id": "acme"},
        )
    assert calls[0]["config"]["configurable"]["org_id"] == "acme"
