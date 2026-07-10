"""Tests for RuntimeRegistry (RHB-01-02, SPEC-RHB-LIF-004, TEN-002)."""

from __future__ import annotations

import asyncio

from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry


class FakeRuntime:
    """A minimal RuntimeContext stand-in: records close, can fail on close."""

    def __init__(self, org_id: str | None, *, fail_close: bool = False) -> None:
        self.org_id = org_id
        self.closed = False
        self._fail_close = fail_close

    async def aclose(self) -> None:
        if self._fail_close:
            raise RuntimeError(f"close boom for {self.org_id}")
        self.closed = True


def make_registry(*, settings: HostSettings | None = None, **kw):  # type: ignore[no-untyped-def]
    calls: list[str | None] = []

    async def builder(*, org_id: str | None):  # type: ignore[no-untyped-def]
        calls.append(org_id)
        await asyncio.sleep(0)  # yield, so concurrent callers can interleave
        return FakeRuntime(org_id, **kw)

    return RuntimeRegistry(settings or HostSettings(), builder=builder), calls


async def test_get_builds_lazily_and_caches() -> None:
    registry, calls = make_registry()
    first = await registry.get("acme")
    second = await registry.get("acme")
    assert first is second
    assert calls == ["acme"]  # built exactly once


async def test_distinct_orgs_get_distinct_runtimes() -> None:
    registry, calls = make_registry()
    a = await registry.get("acme")
    b = await registry.get(None)
    assert a is not b
    assert a.org_id == "acme"
    assert b.org_id is None
    assert calls == ["acme", None]


async def test_concurrent_first_use_builds_once() -> None:
    """Single-flight: two racers for the same org build only one runtime."""
    registry, calls = make_registry()
    r1, r2 = await asyncio.gather(registry.get("acme"), registry.get("acme"))
    assert r1 is r2
    assert calls == ["acme"]


async def test_aclose_all_closes_every_runtime() -> None:
    registry, _ = make_registry()
    a = await registry.get("acme")
    b = await registry.get("globex")
    await registry.aclose_all()
    assert a.closed and b.closed


async def test_aclose_all_swallows_per_tenant_errors() -> None:
    """One tenant's close failing must not block the others (SPEC-RHB-LIF-002)."""
    registry, _ = make_registry(fail_close=True)
    await registry.get("acme")
    await registry.get("globex")
    # Must not raise despite every aclose() raising.
    await registry.aclose_all()


async def test_host_max_tenants_lru_closes_overflow() -> None:
    """The live-tenant cap LRU-closes the least-recently-used runtime (TEN-003)."""
    registry, _ = make_registry(settings=HostSettings(host_max_tenants=2))
    a = await registry.get("a")
    b = await registry.get("b")
    # Touch "a" so "b" becomes least-recently-used.
    await registry.get("a")
    c = await registry.get("c")  # overflow → evict LRU ("b")
    assert b.closed is True
    assert a.closed is False and c.closed is False
    # The evicted tenant rebuilds a fresh runtime on next use.
    b2 = await registry.get("b")
    assert b2 is not b


async def test_host_max_tenants_zero_is_unbounded() -> None:
    """A non-positive cap disables eviction (keeps every tenant live)."""
    registry, _ = make_registry(settings=HostSettings(host_max_tenants=0))
    runtimes = [await registry.get(f"org-{i}") for i in range(5)]
    assert all(not r.closed for r in runtimes)
