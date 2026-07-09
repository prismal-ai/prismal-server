"""``AuthBackend`` protocol and shared credential parsing (RHB-04-01).

A backend resolves an inbound request to an engine ``AgentIdentity`` (or ``None``
when the request is unauthenticated). Per SPEC-RHB-AUT-001 ``resolve`` MUST NOT
raise for an unauthenticated request — it returns ``None`` so the caller decides
policy (public vs. strict).

The host owns only *where the credential comes from on the wire*; the identity
*mechanism* stays engine-side.

Seam: identity — ``prismal.identity`` (``AgentIdentity`` / ``OidcIdentityProvider``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prismal.identity import AgentIdentity


@runtime_checkable
class AuthBackend(Protocol):
    """Resolve a request to an ``AgentIdentity`` or ``None`` (never raising)."""

    async def resolve(self, request: Any) -> AgentIdentity | None: ...


def extract_bearer_token(request: Any) -> str | None:
    """Return the ``Authorization: Bearer <token>`` value, or ``None``.

    Tolerant by construction (SPEC-RHB-AUT-001): a missing header, a non-Bearer
    scheme, or an empty token all yield ``None`` rather than raising.
    """
    header: str | None = request.headers.get("Authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token
