"""Contract: usage/budget endpoints over a real engine runtime (RH2-02, opt-in).

Run with: ``uv run pytest -m contract``.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.contract


async def test_budget_and_usage_against_real_runtime() -> None:
    from prismal_server.app import create_app
    from prismal_server.config import HostSettings
    from prismal_server.deps import RuntimeRegistry

    settings = HostSettings()
    registry = RuntimeRegistry(settings)
    app = create_app(settings=settings, registry=registry)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            budget = await c.get("/v1/budget", headers={"X-Org-Id": "acme"})
            usage = await c.get("/v1/usage", headers={"X-Org-Id": "acme"})
    finally:
        await registry.aclose_all()
    assert budget.status_code == 200
    assert budget.json()["status"] in {
        "within", "soft_exceeded", "hard_exceeded", "disabled", "unknown",
    }  # fmt: skip
    assert usage.status_code == 200 and usage.json()["turns"] == []


def test_run_summary_unknown_thread_is_none() -> None:
    from prismal_server.usage.summary import TurnUsageProbe, default_run_summary

    probe = TurnUsageProbe("never-seen-thread", default_run_summary)
    assert probe.usage() is None
