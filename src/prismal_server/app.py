"""ASGI application factory and lifespan.

Filled in Phase 1 (RHB-01-03): ``create_app() -> FastAPI`` wires the
``RuntimeRegistry`` into a lifespan (startup builds it, shutdown ``aclose_all``s
it) and mounts the routers; the module-level ``app`` is ``create_app()``.

Seam: composition — ``prismal.composition.runtime.build_runtime`` (via ``deps``).
"""
