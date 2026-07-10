"""``OidcAuthBackend`` (RHB-04-04).

Delegates token validation and identity resolution to the engine's
``OidcIdentityProvider`` — the host performs no token crypto (SPEC-RHB-AUT-004).
The bearer value is treated as the caller's DID; the engine provider verifies it
and resolves the backing ``AgentIdentity``. Any provider error (or an
unverifiable / unknown DID) resolves to ``None`` rather than raising
(SPEC-RHB-AUT-001).

The synchronous provider calls run in a worker thread so DID resolution never
blocks the event loop.

Seam: identity — ``prismal.identity.OidcIdentityProvider``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from prismal.identity import DID, AgentIdentity, OidcIdentityProvider

logger = logging.getLogger(__name__)


class OidcAuthBackend:
    """Resolve a bearer DID via the engine OIDC identity provider."""

    def __init__(self, *, provider: Any | None = None) -> None:
        # Constructed lazily by the caller/factory; ``provider`` is injectable so
        # tests supply a fake and never do real DID resolution.
        self._provider = provider if provider is not None else OidcIdentityProvider()

    async def resolve(self, request: Any) -> AgentIdentity | None:
        from prismal_server.auth.base import extract_bearer_token

        token = extract_bearer_token(request)
        if token is None:
            return None
        did = DID(token)
        try:
            verified = await asyncio.to_thread(self._provider.verify, did)
            if not verified:
                return None
            return await asyncio.to_thread(self._provider.resolve, did)
        except Exception:  # never propagate — an auth failure is just ``None``
            logger.warning("OIDC identity resolution failed", exc_info=True)
            return None
