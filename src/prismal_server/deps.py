"""Runtime registry and FastAPI dependencies.

Filled in Phase 1 (RHB-01-02): ``RuntimeRegistry`` keeps one ``RuntimeContext``
per ``org_id``, built lazily via ``build_runtime(org_id=...)`` behind a
single-flight lock, LRU-bounded by ``host_max_tenants``, and closed via
``aclose_all()`` on shutdown.

Seam: composition — ``prismal.composition.runtime.build_runtime``.
"""
