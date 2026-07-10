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


def resolve_org_id(request: Any, identity: AgentIdentity | None) -> str | None:
    """Resolve the tenant ``org_id`` (SPEC-RHB-TEN-001).

    Order: the identity's ``org_id`` if present, else the ``X-Org-Id`` header,
    else ``None`` (single-tenant).
    """
    if identity is not None and identity.org_id:
        org_id: str = identity.org_id
        return org_id
    header: str | None = request.headers.get("X-Org-Id")
    return header
