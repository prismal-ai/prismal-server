"""``NoAuthBackend`` — dev default (RHB-04-02).

Returns a fixed local ``AgentIdentity`` (configurable DID / agent_name) for every
request, so a fresh clone boots usable with no credentials (SPEC-RHB-AUT-002).
This is a development convenience; production hosts select ``bearer`` or ``oidc``.

Seam: identity — ``prismal.identity.AgentIdentity``.
"""

from __future__ import annotations

from typing import Any

from prismal.identity import DID, AgentIdentity


class NoAuthBackend:
    """Resolve every request to one fixed, configured identity."""

    def __init__(self, *, did: str, agent_name: str) -> None:
        self._identity = AgentIdentity(did=DID(did), agent_name=agent_name)

    async def resolve(self, request: Any) -> AgentIdentity | None:
        return self._identity
