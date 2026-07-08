"""Tests for health/readiness routes (RHB-01-04, SPEC-RHB-HLT-001/002/003)."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry


async def test_healthz_is_ok(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_ready_when_default_runtime_composes(client: AsyncClient) -> None:
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


async def test_readyz_not_ready_when_composition_fails() -> None:
    class RuntimeCompositionError(Exception): ...

    async def failing_builder(*, org_id: str | None):  # type: ignore[no-untyped-def]
        raise RuntimeCompositionError("chroma unreachable at 10.0.0.5:8000")

    registry = RuntimeRegistry(HostSettings(), builder=failing_builder)
    app = create_app(registry=registry)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    # Reason is a stable, non-sensitive code — never the raw error (HLT-003).
    assert body["reason"] == "runtime_composition_error"
    assert "10.0.0.5" not in resp.text


async def test_healthz_is_public_and_leaks_nothing(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert set(resp.json().keys()) == {"status"}
