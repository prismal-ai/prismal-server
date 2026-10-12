"""Inbound A2A routes — v0.3 + v1.0 dual stack (RHB-03-*, RH2-04-*).

``GET /.well-known/agent-card.json`` publishes the Agent Card via
``build_agent_card(...)`` for the engine's ``a2a_protocol_version``;
``?protocolVersion=0.3|1.0`` requests the other stack while ``a2a_accept_legacy``
holds (SPEC-RH2-A2A-002). ``POST /a2a`` delegates JSON-RPC to
``RuntimeContext.a2a_handler`` — ``stream_rpc`` for the engine's streaming
methods, ``handle_rpc`` otherwise — passing the request headers (minus
credentials) and the auth-resolved ``org_id`` (SPEC-RH2-A2A-001). Version
negotiation, ``ListTasks`` and tenant checks live in the engine.

Tenant-scoped routes ``{base}/{tenant}`` (RPC) and
``{base}/{tenant}/.well-known/agent-card.json`` mirror the URLs the card
advertises (``RuntimeContext.a2a_routes()``, SPEC-HRS-A2A-001). A path tenant
that differs from the auth-resolved org is ``403`` without invoking the handler
(SPEC-RH2-A2A-003).

The host owns transport + auth only: it passes the raw JSON-RPC through
unchanged; ``A2AServerHandler`` L1-sanitizes, audits and enforces strict-mode
auth (JSON-RPC ``-32001``) itself. All routes 404 when the host's
``a2a_enabled`` is off (SPEC-RH2-A2A-004).

Seam: inbound A2A — ``build_agent_card`` / ``A2AServerHandler`` (via the engine).
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prismal.a2a import a2a_route_paths
from prismal.a2a.v1.constants import (
    LEGACY_METHOD_STREAM,
    METHOD_SEND_STREAMING_MESSAGE,
    VERSION_HEADER,
)
from starlette.background import BackgroundTask

from prismal_server.auth import ForbiddenTenant, resolve_org_id
from prismal_server.deps import Lease, release_after

router = APIRouter(tags=["a2a"])

# JSON-RPC error emitted when the engine's inbound A2A handler is not composed.
_A2A_UNAVAILABLE = -32000

# The engine streams exactly these methods (its own constants, ADR-PRT-001).
_STREAMING_METHODS = frozenset({METHOD_SEND_STREAMING_MESSAGE, LEGACY_METHOD_STREAM})

# Never forwarded to the engine: it needs the version header, not credentials.
_DROPPED_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})

CardVersion = Literal["0.3", "1.0"]


def base_path(a2a_base_url: str | None) -> str:
    """URL path of the engine's ``a2a_base_url`` (``""`` when unset)."""
    if not a2a_base_url:
        return ""
    return urlparse(a2a_base_url).path.rstrip("/")


def _require_enabled(request: Request) -> None:
    if not request.app.state.settings.a2a_enabled:
        raise HTTPException(status_code=404, detail="A2A is not enabled")


def _to_auth_ctx(identity: Any) -> Any:
    """Adapt a resolved ``AgentIdentity`` to the engine's A2A ``AuthContext``.

    Unauthenticated requests pass ``None`` through so the handler applies its own
    strict-mode gate (JSON-RPC ``-32001``); the host never enforces A2A auth
    itself. ``AuthContext`` is sourced from the A2A seam.
    """
    if identity is None:
        return None
    from prismal.a2a import AuthContext  # noqa: PLC0415 - deferred to the A2A path

    return AuthContext(
        authenticated=True, subject=identity.agent_name, did=identity.did
    )


def _forwarded_headers(request: Request) -> dict[str, str]:
    """Request headers for engine negotiation, minus credentials.

    ``?A2A-Version=`` is honoured as the header when the header is absent.
    """
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _DROPPED_HEADERS
    }
    query_version = request.query_params.get(VERSION_HEADER)
    if query_version is not None and not any(
        k.lower() == VERSION_HEADER.lower() for k in headers
    ):
        headers[VERSION_HEADER] = query_version
    return headers


async def _dispatch(
    request: Request, *, lease: Lease, identity: Any, org_id: str | None
) -> Any:
    """Hand one JSON-RPC payload to the leased runtime's handler."""
    try:
        payload = await request.json()
    except BaseException:
        await lease.release()
        raise
    handler = getattr(lease.runtime, "a2a_handler", None)
    if handler is None:
        await lease.release()
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {
                    "code": _A2A_UNAVAILABLE,
                    "message": "inbound A2A is not enabled on the engine",
                },
            }
        )

    kwargs: dict[str, Any] = {
        "auth_ctx": _to_auth_ctx(identity),
        "headers": _forwarded_headers(request),
        "org_id": org_id,
    }
    if payload.get("method") in _STREAMING_METHODS:
        # The lease spans the stream (SPEC-RH2-TEN-004).
        return StreamingResponse(
            release_after(lease, handler.stream_rpc(payload, **kwargs)),
            media_type="text/event-stream",
            background=BackgroundTask(lease.release),
        )
    try:
        result = await handler.handle_rpc(payload, **kwargs)
    finally:
        await lease.release()
    return JSONResponse(result)


async def _card(
    request: Request, *, org_id: str | None, version: CardVersion | None
) -> JSONResponse:
    """The Agent Card for ``org_id`` and ``version`` (None ⇒ engine default)."""
    process = request.app.state.a2a_engine_settings
    registry = request.app.state.registry
    tenant_settings = None
    # Only configured tenants (a bounded set) can differ from process settings,
    # so only they get a runtime built for a public card request.
    if await registry.has_tenant_config(org_id):
        tenant_settings = getattr(await registry.get(org_id), "settings", None)
    effective = tenant_settings or process

    if (
        version is not None
        and version != effective.a2a_protocol_version
        and not effective.a2a_accept_legacy
    ):
        raise HTTPException(status_code=404, detail="card version not available")

    builder = request.app.state.agent_card_builder
    if tenant_settings is not None:
        # The engine caches cards per settings fingerprint itself.
        card = builder(
            org_id=org_id, protocol_version=version, settings=tenant_settings
        )
        return JSONResponse(card)
    cache: dict[Any, dict[str, Any]] = request.app.state.agent_card_cache
    key = (org_id, version)
    cached = cache.get(key)
    if cached is None:
        cached = builder(org_id=org_id, protocol_version=version)
        cache[key] = cached
    return JSONResponse(cached)


@router.get("/.well-known/agent-card.json")
async def agent_card(
    request: Request,
    protocolVersion: CardVersion | None = None,  # noqa: N803
) -> JSONResponse:
    """Serve the A2A Agent Card (camelCase wire form), cached per tenant."""
    _require_enabled(request)
    return await _card(
        request, org_id=request.headers.get("X-Org-Id"), version=protocolVersion
    )


@router.post("/a2a")
async def a2a_rpc(request: Request) -> Any:
    """Delegate a JSON-RPC call to the tenant's inbound A2A handler."""
    _require_enabled(request)
    # Resolve identity for tenant selection + AuthContext; the host never rejects
    # here (no 401) — the handler owns A2A strict-mode gating (JSON-RPC -32001).
    identity = await request.app.state.auth_backend.resolve(request)
    # Fail-closed (``for_read``): the engine scopes ListTasks/GetTask by this
    # org_id, so a bare ``X-Org-Id`` must never select another tenant's tasks.
    org_id = resolve_org_id(request, identity, for_read=True)
    lease = await request.app.state.registry.acquire(org_id)
    return await _dispatch(request, lease=lease, identity=identity, org_id=org_id)


def tenant_router(prefix: str) -> APIRouter:
    """Tenant-scoped A2A routes under ``prefix`` (the ``a2a_base_url`` path)."""
    tenant = APIRouter(prefix=prefix, tags=["a2a"])

    @tenant.get("/{tenant_id}/.well-known/agent-card.json")
    async def tenant_agent_card(
        tenant_id: str,
        request: Request,
        protocolVersion: CardVersion | None = None,  # noqa: N803
    ) -> JSONResponse:
        """The tenant's Agent Card at the path the engine advertises."""
        _require_enabled(request)
        card_path, _ = a2a_route_paths(
            request.app.state.a2a_engine_settings, org_id=tenant_id
        )
        if prefix + card_path != request.url.path:
            raise HTTPException(status_code=404, detail="not an A2A card path")
        return await _card(request, org_id=tenant_id, version=protocolVersion)

    @tenant.post("/{tenant_id}")
    async def tenant_a2a_rpc(tenant_id: str, request: Request) -> Any:
        """JSON-RPC for ``tenant_id``; the caller's org must be that tenant."""
        _require_enabled(request)
        identity = await request.app.state.auth_backend.resolve(request)
        org_id = resolve_org_id(request, identity, for_read=True)
        if org_id != tenant_id:
            raise ForbiddenTenant()  # SPEC-RH2-A2A-003: before the handler
        lease = await request.app.state.registry.acquire(tenant_id)
        spec = lease.runtime.a2a_routes()
        if not spec.tenant_scoped or spec.rpc_path != f"/{tenant_id}":
            await lease.release()
            raise HTTPException(status_code=404, detail="not an A2A route")
        return await _dispatch(request, lease=lease, identity=identity, org_id=org_id)

    @tenant.api_route(
        "/{tenant_id}",
        methods=["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def not_a_tenant_route(tenant_id: str) -> None:
        """Unknown paths stay 404 (not 405) although ``{base}/{tenant}`` is POST."""
        raise HTTPException(status_code=404, detail="Not Found")

    return tenant
