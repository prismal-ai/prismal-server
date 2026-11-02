"""Per-tenant engine configuration (host v0.2, RH2-01).

The host reads a tenant's ``tenant.yaml`` plus its secrets and hands the result
to the engine as a ``ConfigSourcePort`` (``build_runtime(config_source=…)``).
It validates *which* keys a tenant may set (fail-closed allowlist) but never
interprets their meaning — that is engine configuration.
"""
