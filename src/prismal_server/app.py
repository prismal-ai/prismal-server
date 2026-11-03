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

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from prismal.agents.graph import aclose_async_compiled_graph, get_async_compiled_graph

from prismal_server import __version__, openapi_examples
from prismal_server.auth import AuthBackend, build_auth_backend
from prismal_server.config import HostSettings, get_settings
from prismal_server.deps import (
    AgentCardBuilder,
    GraphFactory,
    RuntimeRegistry,
)
from prismal_server.errors import register_exception_handlers
from prismal_server.fake import (
    FakeCheckpoint,
    FakeGraph,
    FakeHeaderMiddleware,
    FakeSummaries,
    fake_builder,
)
from prismal_server.routes import a2a, admin, agui, health, threads, usage, ws
from prismal_server.tracing import Tracing
from prismal_server.usage.store import UsageStore, build_usage_store
from prismal_server.usage.summary import RunSummaryLookup, default_run_summary

logger = logging.getLogger(__name__)

GraphCloser = Callable[[], Awaitable[None]]


async def _default_graph_closer() -> None:
    """Close the engine's cached graph checkpointers (execution seam, H-320).

    Its SQLite checkpointer runs on a non-daemon thread; left open, the
    process cannot exit after shutdown. prismal-ai >= 3.22.1 closes the
    connections before dropping its cached graph, so the host holds nothing.
    """
    await aclose_async_compiled_graph()


async def _default_graph_factory(*, tool_provider: Any = None) -> Any:
    """Obtain the compiled graph via the Execution seam."""
    return await get_async_compiled_graph(tool_provider=tool_provider)


def _default_agent_card_builder(
    *,
    org_id: str | None = None,
    protocol_version: str | None = None,
    settings: Any = None,
) -> dict[str, Any]:
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
        settings or get_engine_settings(),
        DEFAULT_CAPABILITY_MAP,
        org_id=org_id,
        protocol_version=protocol_version,
    )
    return cast("dict[str, Any]", card.model_dump(by_alias=True))


def _default_a2a_engine_settings() -> Any:
    """Process engine settings for A2A routing (base URL, version, legacy)."""
    from prismal.a2a import get_settings as get_engine_settings  # noqa: PLC0415

    return get_engine_settings()


def create_app(
    *,
    registry: RuntimeRegistry | None = None,
    settings: HostSettings | None = None,
    graph_factory: GraphFactory | None = None,
    agent_card_builder: AgentCardBuilder | None = None,
    auth_backend: AuthBackend | None = None,
    usage_store: UsageStore | None = None,
    run_summary: RunSummaryLookup | None = None,
    a2a_engine_settings: Any = None,
    tracing: Tracing | None = None,
    graph_closer: GraphCloser | None = None,
) -> FastAPI:
    """Build the FastAPI app, optionally with injected seams (for tests)."""
    settings = settings or get_settings()
    # Fake mode never builds the engine graph, so nothing to close by default.
    closer = graph_closer or (
        None if settings.host_fake_llm_mode else _default_graph_closer
    )
    if settings.host_fake_llm_mode:
        # SPEC-RH2-FAK-001: engine test runtimes + the deterministic script.
        summaries = FakeSummaries()
        checkpoint = FakeCheckpoint()

        async def fake_graph_factory(*, tool_provider: Any = None) -> Any:
            return FakeGraph(summaries, checkpoint)

        registry = registry or RuntimeRegistry(settings, builder=fake_builder)
        graph_factory = graph_factory or fake_graph_factory
        run_summary = run_summary or summaries
    registry = registry or RuntimeRegistry(settings)
    graph_factory = graph_factory or _default_graph_factory
    agent_card_builder = agent_card_builder or _default_agent_card_builder
    auth_backend = auth_backend or build_auth_backend(settings)
    usage_store = usage_store or build_usage_store(settings)
    run_summary = run_summary or default_run_summary
    a2a_engine_settings = a2a_engine_settings or _default_a2a_engine_settings()
    tracing = tracing or Tracing(settings)  # RH3-01: no-op unless enabled

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: registry is already on app.state (set below). Warming the
        # default tenant is optional (SPEC-RHB-LIF-001 MAY) and left off so boot
        # stays resilient; /readyz composes on demand.
        try:
            yield
        finally:
            # Shutdown: release every tenant runtime (SPEC-RHB-LIF-002), then
            # the engine's cached graph — after the runtimes, which close their
            # own checkpointers first (H-320).
            await registry.aclose_all()
            if closer is not None:
                try:
                    await closer()
                except Exception as exc:  # shutdown must complete; no message
                    logger.warning("engine graph close failed: %s", type(exc).__name__)
            await usage_store.aclose()
            await tracing.aclose()  # bounded flush (RH3-01)

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
    app.state.auth_backend = auth_backend
    app.state.usage_store = usage_store
    app.state.run_summary = run_summary
    app.state.a2a_engine_settings = a2a_engine_settings
    app.state.tracing = tracing
    # Engine threads with an AG-UI resume in flight (RH3-02, DESIGN §3.3).
    app.state.resume_claims = set()

    register_exception_handlers(app)
    openapi_examples.install(app)  # RH2-07-03: examples served verbatim
    if settings.host_fake_llm_mode:
        app.add_middleware(FakeHeaderMiddleware)  # SPEC-RH2-FAK-003
    app.include_router(health.router)
    app.include_router(threads.router)
    app.include_router(a2a.router)
    app.include_router(usage.router)
    app.include_router(agui.router)
    app.include_router(admin.router)  # 404 unless host_admin_enabled (ADM-001)
    if settings.host_ws_enabled:  # SPEC-RH2-WSK-001
        app.include_router(ws.router)
    # Tenant-scoped A2A routes match `{base}/{tenant}`: registered last so every
    # fixed route above wins (SPEC-RH2-A2A-003).
    app.include_router(
        a2a.tenant_router(
            a2a.base_path(getattr(a2a_engine_settings, "a2a_base_url", None))
        )
    )
    return app


app = create_app()
