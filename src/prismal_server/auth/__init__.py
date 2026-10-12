"""Authentication seam (RHB-04-*).

The host owns only *where the credential comes from on the wire*; the identity
*mechanism* is engine-side. Backends implement :class:`AuthBackend` and resolve a
request to an engine ``AgentIdentity`` (or ``None``). The backend is selected by
``host_auth_backend`` (``none`` | ``bearer`` | ``oidc``) via
:func:`build_auth_backend`, pluggable without touching the routes
(SPEC-RHB-AUT-006).

:func:`authenticate` applies the host's strict-mode policy (SPEC-RHB-AUT-005) and
:func:`resolve_org_id` resolves the tenant (SPEC-RHB-TEN-001).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from prismal_server.auth.base import AuthBackend, extract_bearer_token
from prismal_server.auth.bearer import BearerTokenBackend
from prismal_server.auth.noauth import NoAuthBackend
from prismal_server.auth.oidc import OidcAuthBackend
from prismal_server.config import get_settings

if TYPE_CHECKING:
    from prismal.identity import AgentIdentity

    from prismal_server.config import HostSettings

__all__ = [
    "AuthBackend",
    "BearerTokenBackend",
    "NoAuthBackend",
    "OidcAuthBackend",
    "authenticate",
    "build_auth_backend",
    "extract_bearer_token",
    "resolve_org_id",
]


def build_auth_backend(settings: HostSettings) -> AuthBackend:
    """Select the auth backend by config (SPEC-RHB-AUT-006).

    The engine ``OidcIdentityProvider`` is constructed only when the ``oidc``
    backend is chosen, so ``none``/``bearer`` hosts pay no OIDC setup cost.
    """
    if settings.host_auth_backend == "bearer":
        return BearerTokenBackend(
            static_tokens=settings.bearer_static_tokens,
            agent_name=settings.dev_identity_agent_name,
        )
    if settings.host_auth_backend == "oidc":
        return OidcAuthBackend()
    return NoAuthBackend(
        did=settings.dev_identity_did,
        agent_name=settings.dev_identity_agent_name,
    )


async def authenticate(request: Request) -> AgentIdentity | None:
    """Resolve the request identity, enforcing strict mode on protected routes.

    Returns the resolved ``AgentIdentity`` (or ``None`` in non-strict mode). When
    ``host_auth_strict`` and the request is unauthenticated, raises ``401``
    (SPEC-RHB-AUT-005). Health routes never call this, so they stay public.
    """
    backend: AuthBackend = request.app.state.auth_backend
    identity = await backend.resolve(request)
    if identity is None and request.app.state.settings.host_auth_strict:
        raise HTTPException(status_code=401, detail="authentication required")
    return identity


class ForbiddenTenant(Exception):
    """The request names a tenant it may not use (HTTP 403 ``forbidden_tenant``)."""

    def __init__(self) -> None:
        super().__init__("the requested tenant is not available to this caller")


def resolve_org_id(
    request: Any, identity: AgentIdentity | None, *, for_read: bool = False
) -> str | None:
    """Resolve the tenant ``org_id`` (SPEC-RHB-TEN-001).

    Order: the identity's ``org_id`` if present, else the ``X-Org-Id`` header,
    else ``None`` (single-tenant).

    WHERE per-tenant config is enabled (``host_tenant_config_dir``), tenants
    carry their own gateway credentials, so tenant selection fails closed:

    * an ``X-Org-Id`` that differs from the identity's ``org_id`` ⇒
      :class:`ForbiddenTenant`;
    * an ``X-Org-Id`` without an identity ``org_id`` is honoured only in local
      development (``host_auth_backend="none"`` outside ``production``);
      otherwise :class:`ForbiddenTenant` — never a silent fallback to the
      process (operator) tenant.

    ``for_read=True`` (usage, budget, admin reads) applies those fail-closed
    rules even without tenant config: reading another org's records must never
    be possible by just sending a header.
    """
    header: str | None = request.headers.get("X-Org-Id")
    identity_org: str | None = identity.org_id if identity is not None else None
    state = getattr(getattr(request, "app", None), "state", None)
    settings = getattr(state, "settings", None) or get_settings()

    if settings.host_tenant_config_dir is None and not for_read:
        # v0.1 behaviour (SPEC-RHB-TEN-001).
        return identity_org or header

    if identity_org:
        if header is not None and header != identity_org:
            raise ForbiddenTenant()
        return identity_org
    if header is None:
        return None
    if (
        settings.host_auth_backend == "none"
        and settings.host_environment != "production"
    ):
        return header
    raise ForbiddenTenant()
