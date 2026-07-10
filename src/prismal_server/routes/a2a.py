"""Inbound A2A routes (RHB-03-*).

``GET /.well-known/agent-card.json`` publishes the Agent Card via
``build_agent_card(...)`` (cached per ``(settings, org_id)``). ``POST /a2a``
delegates JSON-RPC to ``RuntimeContext.a2a_handler`` — ``handle_rpc`` for the
non-streaming path and ``stream_rpc`` (SSE ``data:`` lines) for ``message/stream``.

The host owns transport + auth only: it passes the raw JSON-RPC through unchanged.
``A2AServerHandler`` already L1-sanitizes and audits every task, and enforces
strict-mode auth (JSON-RPC ``-32001``) itself (SPEC-RHB-A2A-002/003/004). Both
routes 404 when the host's ``a2a_enabled`` is off (A2A-005).

Seam: inbound A2A — ``build_agent_card`` / ``A2AServerHandler`` (via the engine).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from prismal_server.auth import resolve_org_id

router = APIRouter(tags=["a2a"])

# JSON-RPC error emitted when the engine's inbound A2A handler is not composed.
_A2A_UNAVAILABLE = -32000


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


@router.get("/.well-known/agent-card.json")
async def agent_card(request: Request) -> JSONResponse:
    """Serve the A2A Agent Card (camelCase wire form), cached per tenant."""
    _require_enabled(request)
    org_id = request.headers.get("X-Org-Id")

    cache: dict[str | None, dict[str, Any]] = request.app.state.agent_card_cache
    card = cache.get(org_id)
    if card is None:
        card = request.app.state.agent_card_builder(org_id=org_id)
        cache[org_id] = card
    return JSONResponse(card)


@router.post("/a2a")
async def a2a_rpc(request: Request) -> Any:
    """Delegate a JSON-RPC call to the tenant's inbound A2A handler."""
    _require_enabled(request)
    # Resolve identity for tenant selection + AuthContext; the host never rejects
    # here (no 401) — the handler owns A2A strict-mode gating (JSON-RPC -32001).
    identity = await request.app.state.auth_backend.resolve(request)
    org_id = resolve_org_id(request, identity)
    payload = await request.json()

    runtime = await request.app.state.registry.get(org_id)
    handler = getattr(runtime, "a2a_handler", None)
    if handler is None:
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

    auth_ctx = _to_auth_ctx(identity)

    if payload.get("method") == "message/stream":
        return StreamingResponse(
            handler.stream_rpc(payload, auth_ctx=auth_ctx),
            media_type="text/event-stream",
        )

    result = await handler.handle_rpc(payload, auth_ctx=auth_ctx)
    return JSONResponse(result)
