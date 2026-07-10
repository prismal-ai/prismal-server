"""Tests for the app factory + lifespan (RHB-01-03, SPEC-RHB-LIF-001/002/003)."""

from __future__ import annotations

from fastapi import FastAPI

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry


class SpyRuntime:
    def __init__(self, org_id: str | None) -> None:
        self.org_id = org_id
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _spy_registry() -> tuple[RuntimeRegistry, list[SpyRuntime]]:
    built: list[SpyRuntime] = []

    async def builder(*, org_id: str | None):  # type: ignore[no-untyped-def]
        rt = SpyRuntime(org_id)
        built.append(rt)
        return rt

    return RuntimeRegistry(HostSettings(), builder=builder), built


def test_create_app_returns_fastapi_and_exposes_injected_registry() -> None:
    registry, _ = _spy_registry()
    app = create_app(registry=registry)
    assert isinstance(app, FastAPI)
    # Registry is available eagerly (SPEC-RHB-LIF-003 — injectable for tests).
    assert app.state.registry is registry


def test_create_app_builds_default_registry_when_none_injected() -> None:
    app = create_app()
    assert isinstance(app.state.registry, RuntimeRegistry)


async def test_lifespan_closes_all_runtimes_on_shutdown() -> None:
    registry, built = _spy_registry()
    await registry.get(None)
    await registry.get("acme")
    app = create_app(registry=registry)

    async with app.router.lifespan_context(app):
        pass  # startup → shutdown

    assert built and all(rt.closed for rt in built)
