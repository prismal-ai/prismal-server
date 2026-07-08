"""ASGI application factory and lifespan (RHB-01-03).

``create_app()`` wires the ``RuntimeRegistry`` into the app, registers the
centralized error mapping, and mounts the routers. The lifespan warms nothing by
default (readiness composes on demand) and, on shutdown, ``aclose()``s every
cached ``RuntimeContext`` (SPEC-RHB-LIF-001/002). The registry is injectable so
tests can supply a fake built from ``build_test_runtime`` (SPEC-RHB-LIF-003).

The module-level ``app = create_app()`` is the entry point for
``uvicorn prismal_server.app:app``.

Seam: composition — ``build_runtime`` (via ``deps.RuntimeRegistry``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from prismal.agents.graph import get_async_compiled_graph

from prismal_server import __version__
from prismal_server.config import HostSettings, get_settings
from prismal_server.deps import AgentCardBuilder, GraphFactory, RuntimeRegistry
from prismal_server.errors import register_exception_handlers
from prismal_server.routes import a2a, health, threads


async def _default_graph_factory(*, tool_provider: Any = None) -> Any:
    """Obtain the compiled graph via the Execution seam."""
    return await get_async_compiled_graph(tool_provider=tool_provider)


def _default_agent_card_builder(*, org_id: str | None = None) -> dict[str, Any]:
    """Build the Agent Card via the A2A seam (camelCase wire form).

    Imports are deferred and sourced only from ``prismal.a2a`` (the seam
    re-exports the engine settings + capability map), so the host never reaches
    past the A2A seam and module import stays independent of that re-export.
    """
    from prismal.a2a import (  # noqa: PLC0415 - deferred to keep import off the base path
        DEFAULT_CAPABILITY_MAP,
        build_agent_card,
    )
    from prismal.a2a import (
        get_settings as get_engine_settings,
    )

    card = build_agent_card(
        get_engine_settings(), DEFAULT_CAPABILITY_MAP, org_id=org_id
    )
    return cast("dict[str, Any]", card.model_dump(by_alias=True))


def create_app(
    *,
    registry: RuntimeRegistry | None = None,
    settings: HostSettings | None = None,
    graph_factory: GraphFactory | None = None,
    agent_card_builder: AgentCardBuilder | None = None,
) -> FastAPI:
    """Build the FastAPI app, optionally with injected seams (for tests)."""
    settings = settings or get_settings()
    registry = registry or RuntimeRegistry(settings)
    graph_factory = graph_factory or _default_graph_factory
    agent_card_builder = agent_card_builder or _default_agent_card_builder

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: registry is already on app.state (set below). Warming the
        # default tenant is optional (SPEC-RHB-LIF-001 MAY) and left off so boot
        # stays resilient; /readyz composes on demand.
        try:
            yield
        finally:
            # Shutdown: release every tenant runtime (SPEC-RHB-LIF-002).
            await registry.aclose_all()

    app = FastAPI(
        title="prismal-server",
        version=__version__,
        summary="Reference host for the prismal engine (REST · SSE · A2A).",
        lifespan=lifespan,
    )
    # Eagerly available so ASGI clients that don't run lifespan still resolve it.
    app.state.settings = settings
    app.state.registry = registry
    app.state.graph_factory = graph_factory
    app.state.agent_card_builder = agent_card_builder
    app.state.agent_card_cache = {}

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(threads.router)
    app.include_router(a2a.router)
    return app


app = create_app()
