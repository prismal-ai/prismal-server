"""Runtime registry and FastAPI dependencies (RHB-01-02, RH2-01-04).

``RuntimeRegistry`` keeps one ``RuntimeContext`` per ``org_id`` in an in-process
cache, built lazily via the engine's ``build_runtime`` (the Composition seam) and
released via ``aclose_all()`` on shutdown. Concurrent first-use for the same
``org_id`` builds exactly once (single-flight, SPEC-RHB-LIF-004); each distinct
tenant gets its own isolated runtime (SPEC-RHB-TEN-002).

Engine settings are never built host-side. WHERE ``host_tenant_config_dir`` is
set, a tenant with a ``tenant.yaml`` gets a validated ``TenantConfigSource``
passed as ``build_runtime(config_source=…)`` (SPEC-RH2-TEN-001); otherwise the
engine reads its process ``ConfigSourcePort`` as before.

Turns hold a :class:`Lease` on their runtime. A runtime leaving the cache —
because its tenant config changed (SPEC-RH2-TEN-004) or LRU eviction — is
*retired*: closed immediately when idle, otherwise when its last lease is
released, so in-flight turns are never cut off.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

from prismal.agents.graph import get_async_compiled_graph
from prismal.composition.runtime import build_runtime

from prismal_server.config import HostSettings
from prismal_server.tenancy.config_source import (
    TenantConfigInvalid,
    TenantConfigProvider,
    TenantConfigSource,
    TenantFingerprint,
)
from prismal_server.tenancy.secrets import build_secrets

logger = logging.getLogger(__name__)


class RuntimeBuilder(Protocol):
    """Builds a runtime for a tenant. Defaults to the engine ``build_runtime``."""

    def __call__(
        self, *, org_id: str | None, config_source: TenantConfigSource | None = None
    ) -> Awaitable[Any]: ...


class GraphFactory(Protocol):
    """Returns a compiled graph. Defaults to the engine ``get_async_compiled_graph``.

    Injectable so route tests supply a fake graph and never touch a live LLM.
    """

    def __call__(self, *, tool_provider: Any = None) -> Awaitable[Any]: ...


class AgentCardBuilder(Protocol):
    """Builds the A2A Agent Card dict for a tenant (camelCase wire form).

    Injectable so route tests supply a fake and never import the engine card.
    """

    def __call__(self, *, org_id: str | None) -> dict[str, Any]: ...


async def _default_builder(
    *, org_id: str | None, config_source: TenantConfigSource | None = None
) -> Any:
    """Compose a real ``RuntimeContext`` for ``org_id`` via the engine seams.

    A compiled graph is passed so ``build_runtime`` can wire the inbound A2A
    handler (``RuntimeContext.a2a_handler``) when the engine has
    ``a2a_inbound_enabled``; ``get_async_compiled_graph`` is a cached singleton,
    so this stays cheap for non-A2A runtimes too.
    """
    graph = await get_async_compiled_graph()
    return await build_runtime(org_id=org_id, graph=graph, config_source=config_source)


@dataclass(eq=False)
class _Entry:
    """A built runtime plus the bookkeeping that decides when it may close."""

    org_id: str | None
    runtime: Any
    fingerprint: TenantFingerprint | None
    leases: int = 0
    retired: bool = False


class Lease:
    """A turn's hold on a tenant runtime; ``release()`` exactly once (idempotent)."""

    def __init__(self, registry: RuntimeRegistry, entry: _Entry) -> None:
        self._registry = registry
        self._entry: _Entry | None = entry
        self.runtime: Any = entry.runtime

    async def release(self) -> None:
        entry, self._entry = self._entry, None
        if entry is not None:
            await self._registry._release(entry)


async def release_after[T](lease: Lease, stream: AsyncIterator[T]) -> AsyncIterator[T]:
    """Yield ``stream`` and release ``lease`` when it ends, fails or is cancelled."""
    try:
        async for item in stream:
            yield item
    finally:
        await lease.release()


class RuntimeRegistry:
    """Per-``org_id`` cache of engine runtimes with coordinated teardown.

    The cache is bounded to ``host_max_tenants`` live runtimes (LRU-retiring the
    least-recently-used tenant on overflow, SPEC-RHB-TEN-003); a non-positive cap
    disables eviction (unbounded).
    """

    def __init__(
        self,
        settings: HostSettings,
        *,
        builder: RuntimeBuilder | None = None,
        tenant_config: TenantConfigProvider | None = None,
    ) -> None:
        self._settings = settings
        self._builder: RuntimeBuilder = builder or _default_builder
        if tenant_config is None and settings.host_tenant_config_dir is not None:
            tenant_config = TenantConfigProvider(
                settings.host_tenant_config_dir, build_secrets(settings)
            )
        self._tenant_config = tenant_config
        self._entries: OrderedDict[str | None, _Entry] = OrderedDict()
        self._retiring: set[_Entry] = set()
        self._max_tenants = settings.host_max_tenants
        self._lock = asyncio.Lock()

    async def get(self, org_id: str | None = None) -> Any:
        """Return the tenant runtime, building and caching it on first use.

        For a whole turn prefer :meth:`acquire`, so a config change or eviction
        cannot close the runtime mid-turn.
        """
        return (await self._current(org_id)).runtime

    async def acquire(self, org_id: str | None = None) -> Lease:
        """Lease the tenant runtime for the duration of a turn."""
        entry = await self._current(org_id)
        entry.leases += 1
        return Lease(self, entry)

    async def _fingerprint(self, org_id: str | None) -> TenantFingerprint | None:
        if self._tenant_config is None or org_id is None:
            return None
        return await asyncio.to_thread(self._tenant_config.fingerprint, org_id)

    async def _current(self, org_id: str | None) -> _Entry:
        cached = self._entries.get(org_id)
        if cached is not None and await self._fingerprint(org_id) == cached.fingerprint:
            self._entries.move_to_end(org_id)  # mark most-recently-used
            return cached
        async with self._lock:
            # Double-checked: another racer may have rebuilt it while we waited.
            fingerprint = await self._fingerprint(org_id)
            cached = self._entries.get(org_id)
            if cached is not None:
                if cached.fingerprint == fingerprint:
                    self._entries.move_to_end(org_id)
                    return cached
                logger.info("tenant config changed; rebuilding org_id=%s", org_id)
                del self._entries[org_id]
                await self._retire(cached)
            entry = await self._build(org_id, fingerprint)
            self._entries[org_id] = entry
            await self._evict_overflow()
            return entry

    async def _build(
        self, org_id: str | None, fingerprint: TenantFingerprint | None
    ) -> _Entry:
        source: TenantConfigSource | None = None
        if self._tenant_config is not None and org_id is not None:
            try:
                source = await asyncio.to_thread(self._tenant_config.load, org_id)
            except TenantConfigInvalid as exc:
                # Key names only — never values (SPEC-RH2-TEN-003).
                logger.warning(
                    "tenant config refused org_id=%s reason=%s", org_id, exc.reason
                )
                raise
        runtime = await self._builder(org_id=org_id, config_source=source)
        return _Entry(org_id, runtime, fingerprint)

    async def _retire(self, entry: _Entry) -> None:
        """Close ``entry`` now if idle, else when its last lease is released."""
        entry.retired = True
        if entry.leases > 0:
            self._retiring.add(entry)
        else:
            await self._close(entry)

    async def _release(self, entry: _Entry) -> None:
        entry.leases -= 1
        if entry.retired and entry.leases <= 0 and entry in self._retiring:
            self._retiring.discard(entry)
            await self._close(entry)

    async def _close(self, entry: _Entry) -> None:
        try:
            await entry.runtime.aclose()
        except Exception:  # teardown must not raise
            logger.warning(
                "runtime teardown failed for org_id=%s", entry.org_id, exc_info=True
            )

    async def _evict_overflow(self) -> None:
        """LRU-retire runtimes above the cap. Called under ``self._lock``."""
        if self._max_tenants <= 0:
            return
        while len(self._entries) > self._max_tenants:
            _, evicted = self._entries.popitem(last=False)
            await self._retire(evicted)

    async def aclose_all(self) -> None:
        """Close every cached and retiring runtime, swallowing per-tenant errors.

        One tenant failing to close never blocks the rest (SPEC-RHB-LIF-002).
        """
        entries = [*self._entries.values(), *self._retiring]
        self._entries.clear()
        self._retiring.clear()
        for entry in entries:
            entry.retired = True
            await self._close(entry)
