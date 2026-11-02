"""``GET /v1/budget`` (RH2-02-05, SPEC-RH2-BUD-001/002)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from httpx import ASGITransport, AsyncClient
from prismal.composition.host_surface import (
    BudgetAmounts,
    BudgetStatusView,
    QuotaWindowView,
)

from prismal_server import metrics
from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry

AS_OF = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

WITHIN = BudgetStatusView(
    enabled=True,
    scope="tenant",
    status="within",
    breached_dimension=None,
    soft_ratio=0.8,
    hard_cap=True,
    limits=BudgetAmounts(tokens=2_000_000, cost_usd=50.0, calls=0, wall_clock_s=0.0),
    consumed=BudgetAmounts(
        tokens=61_100, cost_usd=0.412345, calls=140, wall_clock_s=312.5
    ),
    remaining=BudgetAmounts(
        tokens=1_938_900, cost_usd=49.587655, calls=None, wall_clock_s=None
    ),
)
WINDOWS = [
    QuotaWindowView(
        gateway="openrouter",
        window="credit",
        unit="usd",
        limit=50.0,
        used=8.8,
        remaining=41.2,
        resets_at=None,
        source="api",
        as_of=AS_OF,
    ),
    QuotaWindowView(
        gateway="opencode_go",
        window="5h",
        unit="usd_equivalent",
        limit=2.0,
        used=0.6,
        remaining=1.4,
        resets_at=datetime(2026, 10, 4, 15, 10, tzinfo=UTC),
        source="ledger",
        as_of=AS_OF,
    ),
]


class FakeRuntime:
    def __init__(self, view: BudgetStatusView, windows: Any) -> None:
        self.view = view
        self.windows = windows
        self.thread_ids: list[str | None] = []

    def budget_status(self, *, thread_id: str | None = None) -> BudgetStatusView:
        self.thread_ids.append(thread_id)
        return self.view

    async def quota_windows(self) -> list[QuotaWindowView]:
        if isinstance(self.windows, Exception):
            raise self.windows
        return list(self.windows)


def _app(runtime: FakeRuntime) -> Any:
    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        return runtime

    settings = HostSettings()
    return create_app(
        settings=settings, registry=RuntimeRegistry(settings, builder=builder)
    )


async def _get(app: Any, **params: Any) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get("/v1/budget", params=params, headers={"X-Org-Id": "acme"})


async def test_serializes_engine_views_field_for_field() -> None:
    resp = await _get(_app(FakeRuntime(WITHIN, WINDOWS)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_id"] == "acme"
    assert {
        k: body[k] for k in ("enabled", "scope", "status", "breached_dimension")
    } == {
        "enabled": True,
        "scope": "tenant",
        "status": "within",
        "breached_dimension": None,
    }
    assert body["limits"] == {
        "tokens": 2000000,
        "cost_usd": 50.0,
        "calls": 0,
        "wall_clock_s": 0.0,
    }
    assert body["remaining"]["calls"] is None
    assert body["gateways"][0] == {
        "gateway": "openrouter",
        "window": "credit",
        "unit": "usd",
        "limit": 50.0,
        "used": 8.8,
        "remaining": 41.2,
        "resets_at": None,
        "source": "api",
        "as_of": "2026-10-04T12:00:00Z",
    }
    assert body["gateways"][1]["resets_at"] == "2026-10-04T15:10:00Z"
    assert body["as_of"].endswith("Z")


async def test_unknown_status_returned_unchanged() -> None:
    """EARS BUD-001: ``unknown`` passes through with HTTP 200."""
    unknown = BudgetStatusView(
        enabled=True,
        scope="turn",
        status="unknown",
        breached_dimension=None,
        soft_ratio=0.8,
        hard_cap=True,
        limits=WITHIN.limits,
        consumed=None,
        remaining=None,
    )
    body = (await _get(_app(FakeRuntime(unknown, [])))).json()
    assert body["status"] == "unknown" and body["consumed"] is None


async def test_disabled_budget() -> None:
    disabled = BudgetStatusView(
        enabled=False,
        scope="turn",
        status="disabled",
        breached_dimension=None,
        soft_ratio=None,
        hard_cap=None,
        limits=None,
        consumed=None,
        remaining=None,
    )
    body = (await _get(_app(FakeRuntime(disabled, [])))).json()
    assert (
        body["enabled"] is False and body["limits"] is None and body["gateways"] == []
    )


async def test_thread_id_is_forwarded() -> None:
    runtime = FakeRuntime(WITHIN, [])
    await _get(_app(runtime), thread_id="t-42")
    await _get(_app(runtime))
    assert runtime.thread_ids == ["t-42", None]


async def test_quota_failure_is_fail_open() -> None:
    before = metrics.get("quota_lookup_errors_total", gateway="*")
    body = (await _get(_app(FakeRuntime(WITHIN, RuntimeError("down"))))).json()
    assert body["gateways"] == [] and body["status"] == "within"
    assert metrics.get("quota_lookup_errors_total", gateway="*") == before + 1


async def test_real_test_runtime_budget() -> None:
    """Against the engine's own build_test_runtime (budget disabled by default)."""
    from prismal.composition import build_test_runtime

    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        return build_test_runtime(org_id=org_id)

    settings = HostSettings()
    registry = RuntimeRegistry(settings, builder=builder)
    app = create_app(settings=settings, registry=registry)
    body = (await _get(app)).json()
    await registry.aclose_all()
    assert body["status"] == "disabled" and body["gateways"] == []
