"""Health and readiness routes.

Filled in Phase 1 (RHB-01-04): ``GET /healthz`` (liveness, always ``200`` while
the process is up) and ``GET /readyz`` (``200`` only when the default
``RuntimeContext`` composed, else ``503``). Neither leaks config or secrets
(SPEC-RHB-HLT-001/002/003).
"""
