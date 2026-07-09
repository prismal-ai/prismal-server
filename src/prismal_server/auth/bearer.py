"""``BearerTokenBackend`` (RHB-04-03).

Resolves an ``Authorization: Bearer <token>`` credential to an ``AgentIdentity``
via a static token→DID map (SPEC-RHB-AUT-003). The configured tokens are
``SecretStr`` so a raw token never surfaces in logs or responses; an unknown or
absent token resolves to ``None`` (never raises, SPEC-RHB-AUT-001).

The identity carries no ``org_id`` — tenant selection falls back to the
``X-Org-Id`` header per SPEC-RHB-TEN-001.

Seam: identity — ``prismal.identity.AgentIdentity``.
"""

from __future__ import annotations

from typing import Any

from prismal.identity import DID, AgentIdentity
from pydantic import SecretStr


class BearerTokenBackend:
    """Resolve a static bearer token to a fixed-DID identity."""

    def __init__(
        self,
        *,
        static_tokens: dict[str, SecretStr],
        agent_name: str = "bearer-client",
    ) -> None:
        self._static_tokens = static_tokens
        self._agent_name = agent_name

    async def resolve(self, request: Any) -> AgentIdentity | None:
        from prismal_server.auth.base import extract_bearer_token

        token = extract_bearer_token(request)
        if token is None:
            return None
        did = self._static_tokens.get(token)
        if did is None:
            return None
        return AgentIdentity(
            did=DID(did.get_secret_value()), agent_name=self._agent_name
        )
