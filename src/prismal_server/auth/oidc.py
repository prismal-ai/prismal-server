"""``OidcAuthBackend``.

Filled in Phase 4 (RHB-04-04): delegates token validation + identity minting to
the engine's ``OidcIdentityProvider`` — no token crypto in the host
(SPEC-RHB-AUT-004).

Seam: identity — ``prismal.identity.OidcIdentityProvider``.
"""
