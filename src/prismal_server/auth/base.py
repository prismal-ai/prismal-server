"""``AuthBackend`` protocol.

Filled in Phase 4 (RHB-04-01): ``async resolve(request) -> AgentIdentity | None``
that MUST NOT raise for an unauthenticated request (returns ``None``)
(SPEC-RHB-AUT-001).

Seam: identity — ``prismal.identity`` (``IdentityPort`` / ``AgentIdentity``).
"""
