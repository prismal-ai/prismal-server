"""Runtime registry and FastAPI dependencies (RHB-01-02).

``RuntimeRegistry`` keeps one ``RuntimeContext`` per ``org_id`` in an in-process
cache, built lazily via the engine's ``build_runtime`` (the Composition seam) and
released via ``aclose_all()`` on shutdown. Concurrent first-use for the same
``org_id`` builds exactly once (single-flight, SPEC-RHB-LIF-004); each distinct
tenant gets its own isolated runtime (SPEC-RHB-TEN-002).

The host passes only ``org_id`` to ``build_runtime`` — never engine settings, which
the engine reads through its own ``ConfigSourcePort`` (SPEC-RHB-CFG-001).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, Protocol

from prismal.composition.runtime import build_runtime

from prismal_server.config import HostSettings

logger = logging.getLogger(__name__)


class RuntimeBuilder(Protocol):
    """Builds a runtime for a tenant. Defaults to the engine ``build_runtime``."""

    def __call__(self, *, org_id: str | None) -> Awaitable[Any]: ...


class GraphFactory(Protocol):
    """Returns a compiled graph. Defaults to the engine ``get_async_compiled_graph``.

    Injectable so route tests supply a fake graph and never touch a live LLM.
    """

    def __call__(self, *, tool_provider: Any = None) -> Awaitable[Any]: ...


async def _default_builder(*, org_id: str | None) -> Any:
    """Compose a real ``RuntimeContext`` for ``org_id`` via the engine seam."""
    return await build_runtime(org_id=org_id)


class RuntimeRegistry:
    """Per-``org_id`` cache of engine runtimes with coordinated teardown."""

    def __init__(
        self, settings: HostSettings, *, builder: RuntimeBuilder | None = None
    ) -> None:
        self._settings = settings
        self._builder: RuntimeBuilder = builder or _default_builder
        self._runtimes: dict[str | None, Any] = {}
        self._lock = asyncio.Lock()

    async def get(self, org_id: str | None = None) -> Any:
        """Return the tenant runtime, building and caching it on first use."""
        cached = self._runtimes.get(org_id)
        if cached is not None:
            return cached
        async with self._lock:
            # Double-checked: another racer may have built it while we waited.
            cached = self._runtimes.get(org_id)
            if cached is not None:
                return cached
            runtime = await self._builder(org_id=org_id)
            self._runtimes[org_id] = runtime
            return runtime

    async def aclose_all(self) -> None:
        """Close every cached runtime, swallowing per-tenant errors.

        One tenant failing to close never blocks the rest (SPEC-RHB-LIF-002).
        """
        runtimes = list(self._runtimes.items())
        self._runtimes.clear()
        for org_id, runtime in runtimes:
            try:
                await runtime.aclose()
            except Exception:  # defensive: teardown must not raise
                logger.warning(
                    "runtime teardown failed for org_id=%s", org_id, exc_info=True
                )
