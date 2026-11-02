"""Usage stores (RH2-02-03, SPEC-RH2-USE-001)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from prismal_server.config import HostSettings
from prismal_server.usage.models import UsageRecord, UsageWire
from prismal_server.usage.store import (
    MemoryUsageStore,
    SqliteUsageStore,
    UsageStore,
    build_usage_store,
    new_turn_id,
)

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
WIRE = UsageWire.model_validate(
    {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 15,
        "calls": 1,
        "cost_usd": 0.0,
        "cost_source": "none",
        "estimated": True,
        "served_model": "fake/echo",
        "gateway": None,
    }
)


def _rec(org: str | None, thread: str, at: datetime) -> UsageRecord:
    return UsageRecord(
        org_id=org,
        thread_id=thread,
        turn_id=new_turn_id(at),
        finished_at=at,
        finish_reason="stop",
        usage=WIRE,
    )


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: Any, tmp_path: Path) -> AsyncIterator[UsageStore]:
    s: UsageStore
    if request.param == "memory":
        s = MemoryUsageStore(retention_days=90, clock=lambda: NOW)
    else:
        s = SqliteUsageStore(
            tmp_path / "usage.db", retention_days=90, clock=lambda: NOW
        )
    yield s
    await s.aclose()


async def test_query_filters_org_thread_and_window(store: UsageStore) -> None:
    await store.append(_rec("acme", "t1", NOW - timedelta(hours=1)))
    await store.append(_rec("acme", "t2", NOW - timedelta(hours=2)))
    await store.append(_rec("beta", "t1", NOW - timedelta(hours=1)))
    await store.append(_rec("acme", "t1", NOW - timedelta(days=3)))
    await store.append(_rec(None, "t1", NOW - timedelta(hours=1)))

    since, until = NOW - timedelta(days=1), NOW
    acme = await store.query(org_id="acme", thread_id=None, since=since, until=until)
    assert [r.thread_id for r in acme] == ["t1", "t2"]  # newest first
    assert all(r.org_id == "acme" for r in acme)
    only_t1 = await store.query(org_id="acme", thread_id="t1", since=since, until=until)
    assert len(only_t1) == 1
    beta = await store.query(org_id="beta", thread_id=None, since=since, until=until)
    assert [r.org_id for r in beta] == ["beta"]
    default = await store.query(org_id=None, thread_id=None, since=since, until=until)
    assert [r.org_id for r in default] == [None]


async def test_record_round_trips(store: UsageStore) -> None:
    rec = _rec("acme", "t1", NOW - timedelta(minutes=1))
    await store.append(rec)
    (got,) = await store.query(
        org_id="acme", thread_id=None, since=NOW - timedelta(hours=1), until=NOW
    )
    assert got == rec


async def test_retention_purges_old_records(store: UsageStore) -> None:
    await store.append(_rec("acme", "old", NOW - timedelta(days=91)))
    await store.append(_rec("acme", "new", NOW - timedelta(days=1)))
    # Append purges opportunistically; an explicit purge is idempotent.
    await store.purge()
    assert await store.purge() == 0
    rows = await store.query(
        org_id="acme", thread_id=None, since=NOW - timedelta(days=365), until=NOW
    )
    assert [r.thread_id for r in rows] == ["new"]


async def test_sqlite_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    first = SqliteUsageStore(path, retention_days=90, clock=lambda: NOW)
    await first.append(_rec("acme", "t1", NOW - timedelta(minutes=1)))
    await first.aclose()
    second = SqliteUsageStore(path, retention_days=90, clock=lambda: NOW)
    rows = await second.query(
        org_id="acme", thread_id=None, since=NOW - timedelta(hours=1), until=NOW
    )
    await second.aclose()
    assert len(rows) == 1


def test_turn_ids_sort_by_time() -> None:
    a = new_turn_id(NOW)
    b = new_turn_id(NOW + timedelta(milliseconds=1))
    assert len(a) == 26 and a < b


def test_build_usage_store(tmp_path: Path) -> None:
    assert isinstance(build_usage_store(HostSettings()), MemoryUsageStore)
    sqlite = build_usage_store(
        HostSettings(host_usage_store="sqlite", host_usage_db_path=tmp_path / "u.db")
    )
    assert isinstance(sqlite, SqliteUsageStore)
