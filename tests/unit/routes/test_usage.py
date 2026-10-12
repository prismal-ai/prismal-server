"""``GET /v1/usage`` (RH2-02-04, SPEC-RH2-USE-002/003)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry
from prismal_server.usage.models import UsageRecord, UsageWire
from prismal_server.usage.store import MemoryUsageStore, new_turn_id

NOW = datetime.now(UTC).replace(microsecond=0)


def _wire(**over: Any) -> UsageWire:
    base: dict[str, Any] = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cache_read_tokens": 75,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 110,
        "calls": 1,
        "cost_usd": 0.001,
        "cost_source": "gateway",
        "estimated": False,
        "served_model": "m/a",
        "gateway": "openrouter",
    }
    return UsageWire.model_validate({**base, **over})


async def _seed(store: MemoryUsageStore) -> None:
    for i, (org, thread, wire) in enumerate(
        [
            ("acme", "t1", _wire()),
            (
                "acme",
                "t2",
                _wire(
                    served_model="m/b",
                    gateway=None,
                    cost_source="table",
                    estimated=True,
                ),
            ),
            ("acme", "t1", _wire()),
            ("beta", "t9", _wire(cost_usd=9.0)),
        ]
    ):
        at = NOW - timedelta(minutes=10 * (i + 1))
        await store.append(
            UsageRecord(
                org_id=org,
                thread_id=thread,
                turn_id=new_turn_id(at),
                finished_at=at,
                finish_reason="stop",
                usage=wire,
            )
        )
    old = NOW - timedelta(days=2)
    await store.append(
        UsageRecord(
            org_id="acme",
            thread_id="t1",
            turn_id=new_turn_id(old),
            finished_at=old,
            finish_reason="stop",
            usage=_wire(),
        )
    )


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    store = MemoryUsageStore(retention_days=90)
    await _seed(store)

    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        return object()

    settings = HostSettings()
    app = create_app(
        settings=settings,
        registry=RuntimeRegistry(settings, builder=builder),
        usage_store=store,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _get(c: AsyncClient, org: str, **params: Any) -> Any:
    return await c.get("/v1/usage", params=params, headers={"X-Org-Id": org})


async def test_default_window_is_last_24h_and_org_scoped(client: AsyncClient) -> None:
    resp = await _get(client, "acme")
    assert resp.status_code == 200
    body = resp.json()
    assert list(body) == [
        "org_id", "thread_id", "since", "until", "totals",
        "cache_hit_ratio", "by_model", "turns", "next_cursor",
    ]  # fmt: skip
    assert body["org_id"] == "acme" and body["thread_id"] is None
    assert len(body["turns"]) == 3  # the 2-day-old record is outside
    assert body["since"].endswith("Z") and body["until"].endswith("Z")


async def test_other_org_sees_none_of_acme(client: AsyncClient) -> None:
    """EARS USE-002."""
    body = (await _get(client, "beta")).json()
    assert [t["thread_id"] for t in body["turns"]] == ["t9"]
    assert body["totals"]["cost_usd"] == 9.0


async def test_totals_and_by_model_cover_whole_window_not_page(
    client: AsyncClient,
) -> None:
    body = (await _get(client, "acme", limit=1)).json()
    assert len(body["turns"]) == 1 and body["next_cursor"]
    totals = body["totals"]
    assert totals["calls"] == 3 and totals["prompt_tokens"] == 300
    assert totals["cost_usd"] == 0.003
    assert totals["cost_source"] == "mixed" and totals["estimated"] is True
    assert totals["served_model"] is None and totals["gateway"] is None
    assert body["cache_hit_ratio"] == 0.75
    assert {(r["served_model"], r["gateway"]) for r in body["by_model"]} == {
        ("m/a", "openrouter"),
        ("m/b", None),
    }


async def test_cursor_pages_through_every_turn_once(client: AsyncClient) -> None:
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = (await _get(client, "acme", **params)).json()
        seen += [t["turn_id"] for t in body["turns"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 3 and len(set(seen)) == 3
    assert seen == sorted(seen, reverse=True)  # newest first


async def test_thread_filter(client: AsyncClient) -> None:
    body = (await _get(client, "acme", thread_id="t2")).json()
    assert body["thread_id"] == "t2"
    assert [t["thread_id"] for t in body["turns"]] == ["t2"]


async def test_explicit_window(client: AsyncClient) -> None:
    since = (NOW - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    body = (await _get(client, "acme", since=since)).json()
    assert len(body["turns"]) == 4


@pytest.mark.parametrize(
    "params",
    [
        {"since": "yesterday"},
        {"since": "2026-10-04T12:00:00"},  # no offset
        {"since": "2026-10-04T12:00:00Z", "until": "2026-10-03T12:00:00Z"},
        {"since": "2026-08-01T00:00:00Z", "until": "2026-10-01T00:00:00Z"},  # > 31 d
    ],
)
async def test_invalid_window_is_400(
    client: AsyncClient, params: dict[str, str]
) -> None:
    resp = await _get(client, "acme", **params)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_window"


async def test_invalid_cursor_is_400(client: AsyncClient) -> None:
    resp = await _get(client, "acme", cursor="not-a-cursor")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.parametrize("limit", [0, 501])
async def test_limit_bounds(client: AsyncClient, limit: int) -> None:
    resp = await _get(client, "acme", limit=limit)
    assert resp.status_code == 422


# ── Security review: reads are scoped to the authenticated org ────────────


async def _bearer_app(**settings: Any) -> Any:
    store = MemoryUsageStore(retention_days=90)
    await _seed(store)

    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        return object()

    s = HostSettings(
        host_auth_backend="bearer",
        bearer_static_tokens={"tok": "did:key:zCaller"},
        **settings,
    )
    return create_app(
        settings=s, registry=RuntimeRegistry(s, builder=builder), usage_store=store
    )


async def test_header_cannot_select_another_org_for_reads() -> None:
    app = await _bearer_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(
            "/v1/usage", headers={"Authorization": "Bearer tok", "X-Org-Id": "acme"}
        )
        budget = await c.get(
            "/v1/budget", headers={"Authorization": "Bearer tok", "X-Org-Id": "acme"}
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden_tenant"
    assert budget.status_code == 403


async def test_reads_without_header_use_identity_org() -> None:
    app = await _bearer_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/v1/usage", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    assert resp.json()["org_id"] is None and resp.json()["turns"] == []
