"""Host configuration.

Filled in Phase 1 (RHB-01-01): ``HostSettings`` (pydantic-settings, prefix
``PRISMAL_SERVER_``) governs *host* concerns only — bind host/port, CORS, auth
mode, SSE heartbeat, tenant cap. Secret-bearing values are ``SecretStr``
(SPEC-RHB-CFG-002). Engine settings stay engine-side (SPEC-RHB-CFG-001).
"""
