"""Authentication seam.

The host owns only *where the credential comes from on the wire*; the identity
*mechanism* is engine-side. Backends implement ``AuthBackend`` and resolve a
request to an ``AgentIdentity`` (or ``None``). Filled in Phase 4 (RHB-04-*).
"""
