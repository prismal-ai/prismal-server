"""Contract: AG-UI gate reads the real engine flag (RH2-03, opt-in).

The released engine ships ``agui_events_enabled=False`` by default, so an AG-UI
request against a real runtime must be refused with 406 before any graph work.

Run with: ``uv run pytest -m contract``.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.contract


async def test_agui_refused_when_engine_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prismal_server.app import create_app
    from prismal_server.config import HostSettings
    from prismal_server.deps import RuntimeRegistry

    monkeypatch.delenv("PRISMAL_AGUI_EVENTS_ENABLED", raising=False)

    async def graph_factory(*, tool_provider: object = None) -> object:
        raise AssertionError("graph must not run when AG-UI is refused")

    settings = HostSettings()
    registry = RuntimeRegistry(settings)
    app = create_app(settings=settings, registry=registry, graph_factory=graph_factory)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post(
                "/threads/t-1/messages",
                params={"protocol": "ag-ui"},
                json={"content": "hi"},
            )
    finally:
        await registry.aclose_all()
    assert resp.status_code == 406
    assert resp.json()["error"]["code"] == "protocol_not_enabled"
