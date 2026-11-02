"""Admin read endpoints (RH2-06-03/04/06/07, SPEC-RH2-ADM-001..005).

``/v1/admin/*`` is ``404`` unless ``host_admin_enabled``; otherwise the caller's
identity must hold ``host_admin_scope`` or gets ``403`` before any engine call.

Tenant targeting is fail-closed and identity-only (``X-Org-Id`` is ignored): an
admin reads its own org; any other ``org_id`` — and ``*`` or an identity
without an org, which mean the engine's process-wide view — needs
``<host_admin_scope>:global``.

Every figure comes from a ``RuntimeContext`` HRS method (``audit_page``,
``verify_audit_chain``, ``decisions_report``, ``routing_report``) serialized
field for field — the host never recomputes chain hashes, agreement or savings
(DD-RH2-004).
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from prismal_server.auth import authenticate
from prismal_server.routes.usage import InvalidWindow, parse_ts
from prismal_server.usage.models import rfc3339

router = APIRouter(prefix="/v1/admin", tags=["admin"])

ALL_ORGS = "*"
DEFAULT_REPORT_WINDOW = timedelta(days=7)


class AdminForbidden(Exception):
    """403 ``forbidden``: missing admin scope, or a tenant outside the grant."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


@dataclasses.dataclass(frozen=True)
class AdminCaller:
    org_id: str | None
    is_global: bool

    def target(self, requested: str | None) -> tuple[str | None, str | None]:
        """``(runtime org, response org_id)`` for a requested ``org_id``.

        ``None``/``*`` runtime org is the engine's all-tenant view.
        """
        if requested is None:
            requested = self.org_id
        if requested is not None and requested == self.org_id:
            return requested, requested
        if not self.is_global:
            raise AdminForbidden("this org requires the global admin scope")
        if requested in (None, ALL_ORGS):
            return None, ALL_ORGS
        return requested, requested


def _has_scope(identity: Any, required: str) -> bool:
    return any(s.matches("read", required) for s in getattr(identity, "scopes", ()))


async def admin_caller(request: Request) -> AdminCaller:
    """The ADM-001 gate: 404 when disabled, 403 without the admin scope."""
    settings = request.app.state.settings
    if not settings.host_admin_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    identity = await authenticate(request)  # 401 in strict mode
    scope = settings.host_admin_scope
    if identity is None or not _has_scope(identity, scope):
        raise AdminForbidden("the admin scope is required")
    return AdminCaller(
        org_id=identity.org_id, is_global=_has_scope(identity, f"{scope}:global")
    )


Caller = Annotated[AdminCaller, Depends(admin_caller)]


def _jsonable(value: Any) -> Any:
    """Engine view dataclasses → JSON (RFC 3339 ``Z`` datetimes)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, datetime):
        return rfc3339(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def _report_window(since: str | None, until: str | None) -> tuple[datetime, datetime]:
    end = parse_ts(until, "until") or datetime.now(UTC)
    start = parse_ts(since, "since") or end - DEFAULT_REPORT_WINDOW
    if start > end:
        raise InvalidWindow("`since` is after `until`")
    return start, end


@router.get("/tenants")
async def list_tenants(request: Request, caller: Caller) -> dict[str, Any]:
    """Live and configured tenants; a non-global admin sees only its own org."""
    registry = request.app.state.registry
    process = request.app.state.a2a_engine_settings
    rows = []
    for info in await registry.tenants():
        if not caller.is_global and (
            info.org_id is None or info.org_id != caller.org_id
        ):
            continue
        runtime = info.runtime
        engine = getattr(runtime, "settings", None) or process
        gateways = getattr(runtime, "gateways", None)
        rows.append(
            {
                "org_id": info.org_id,
                "live": info.live,
                "created_at": rfc3339(info.created_at) if info.created_at else None,
                "last_used_at": (
                    rfc3339(info.last_used_at) if info.last_used_at else None
                ),
                "gateways": list(gateways.names()) if gateways is not None else [],
                "a2a_protocol_version": getattr(engine, "a2a_protocol_version", None),
                "config_source": "vault" if info.configured else "process",
            }
        )
    return {"tenants": rows, "max_tenants": registry.max_tenants}


@router.get("/audit")
async def audit(
    request: Request,
    caller: Caller,
    org_id: str | None = None,
    since: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """A newest-first audit page plus the hash-chain status (ADM-003)."""
    target, _ = caller.target(org_id)
    start = parse_ts(since, "since")
    lease = await request.app.state.registry.acquire(target)
    try:
        runtime = lease.runtime
        # Both read the audit file: keep that I/O off the event loop.
        view = await asyncio.to_thread(
            runtime.audit_page, page=page, page_size=page_size, since=start
        )
        chain = await asyncio.to_thread(runtime.verify_audit_chain)
    finally:
        await lease.release()
    return {**_jsonable(view), "chain": _jsonable(chain)}


@router.get("/decisions")
async def decisions(
    request: Request,
    caller: Caller,
    org_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """The engine's DEC report per integration (ADM-004)."""
    target, shown = caller.target(org_id)
    start, end = _report_window(since, until)
    lease = await request.app.state.registry.acquire(target)
    try:
        view = lease.runtime.decisions_report(since=start, until=end)
    finally:
        await lease.release()
    return {
        "org_id": shown,
        "enabled": view.enabled,
        "window": {"since": rfc3339(start), "until": rfc3339(end)},
        "integrations": _jsonable(view.integrations),
        "as_of": rfc3339(datetime.now(UTC)),
    }


@router.get("/routing")
async def routing(
    request: Request,
    caller: Caller,
    org_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """The engine's AMR report per routing policy (ADM-005)."""
    target, shown = caller.target(org_id)
    start, end = _report_window(since, until)
    lease = await request.app.state.registry.acquire(target)
    try:
        view = lease.runtime.routing_report(since=start, until=end)
    finally:
        await lease.release()
    return {
        "org_id": shown,
        "enabled": view.enabled,
        "mode": view.mode,
        "window": {"since": rfc3339(start), "until": rfc3339(end)},
        "policies": _jsonable(view.policies),
        "as_of": rfc3339(datetime.now(UTC)),
    }
