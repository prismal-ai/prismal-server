"""HTTP surface of tenant config (RH2-01-05, EARS TEN-002/TEN-003)."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry

CANARY = "sk-canary-7f3a9e"


class _Graph:
    async def astream(self, *_: Any, **__: Any) -> AsyncIterator[Any]:
        yield ("hi", {})


async def _graph_factory(*, tool_provider: Any = None) -> _Graph:
    return _Graph()


@pytest.fixture
def tenant_root(tmp_path: Path) -> Path:
    (tmp_path / "acme" / "secrets").mkdir(parents=True)
    (tmp_path / "acme" / "tenant.yaml").write_text(
        "openrouter_enabled: true\nsecurity_strict: false\n", encoding="utf-8"
    )
    secret = tmp_path / "acme" / "secrets" / "openrouter_api_key"
    secret.write_text(CANARY, encoding="utf-8")
    os.chmod(secret, 0o600)
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "tenant.yaml").write_text(
        "routing_policy: composite\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
async def client(tenant_root: Path) -> AsyncIterator[AsyncClient]:
    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        return type("RT", (), {"tool_provider": None, "org_id": org_id})()

    settings = HostSettings(host_tenant_config_dir=tenant_root)
    app = create_app(
        settings=settings,
        registry=RuntimeRegistry(settings, builder=builder),
        graph_factory=_graph_factory,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_refused_tenant_gets_opaque_500_and_no_secret_leaks(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        resp = await client.post(
            "/threads/t-1/messages",
            json={"content": "hello"},
            headers={"X-Org-Id": "acme"},
        )
    assert resp.status_code == 500
    assert resp.json() == {
        "error": {
            "code": "tenant_config_invalid",
            "message": "tenant configuration is invalid",
        }
    }
    assert CANARY not in resp.text
    assert CANARY not in caplog.text
    assert "security_strict" in caplog.text  # reason is logged (names only)


async def test_other_tenant_keeps_serving(client: AsyncClient) -> None:
    resp = await client.post(
        "/threads/t-1/messages",
        json={"content": "hello"},
        headers={"X-Org-Id": "beta"},
    )
    assert resp.status_code == 200
    assert "event: done" in resp.text


async def test_config_change_mid_turn_closes_old_runtime_after_stream(
    tmp_path: Path,
) -> None:
    """TEN-004 end-to-end: a turn's runtime outlives a mid-stream config change."""
    path = tmp_path / "acme" / "tenant.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("routing_policy: composite\n", encoding="utf-8")
    runtimes: list[Any] = []
    observed: dict[str, bool] = {}

    class RT:
        def __init__(self) -> None:
            self.tool_provider = None
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        runtimes.append(RT())
        return runtimes[-1]

    settings = HostSettings(host_tenant_config_dir=tmp_path)
    registry = RuntimeRegistry(settings, builder=builder)

    class ReloadingGraph:
        async def astream(self, *_: Any, **__: Any) -> AsyncIterator[Any]:
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            await registry.get("acme")  # another request triggers the rebuild
            observed["old_closed_mid_turn"] = runtimes[0].closed
            yield ("hi", {})

    async def graph_factory(*, tool_provider: Any = None) -> ReloadingGraph:
        return ReloadingGraph()

    app = create_app(settings=settings, registry=registry, graph_factory=graph_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/threads/t-1/messages", json={"content": "x"}, headers={"X-Org-Id": "acme"}
        )
    assert resp.status_code == 200 and "event: done" in resp.text
    assert len(runtimes) == 2
    assert observed["old_closed_mid_turn"] is False
    assert runtimes[0].closed is True  # released when the stream ended
    assert runtimes[1].closed is False
