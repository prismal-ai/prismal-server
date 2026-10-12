"""Usage record persistence (RH2-02-03, SPEC-RH2-USE-001).

``memory`` (default, per process) or ``sqlite`` (``aiosqlite``; single host or a
shared volume — multi-worker deployments need a server DB, see ARCHITECTURE
§10). Records older than ``host_usage_retention_days`` are purged at most once
a minute on append. Queries return newest first.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import aiosqlite

from prismal_server import metrics
from prismal_server.config import HostSettings
from prismal_server.usage.models import UsageRecord, UsageWire

Clock = Callable[[], datetime]
_PURGE_INTERVAL_S = 60.0
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_turn_id(at: datetime | None = None) -> str:
    """A ULID: 48-bit ms timestamp + 80 random bits, Crockford base32 (sortable)."""
    ms = int((at or _utcnow()).timestamp() * 1000)
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


class UsageStore(Protocol):
    async def append(self, record: UsageRecord) -> None: ...

    async def query(
        self,
        *,
        org_id: str | None,
        thread_id: str | None,
        since: datetime,
        until: datetime,
    ) -> list[UsageRecord]:
        """Records of one org in ``[since, until]``, newest first."""
        ...

    async def purge(self) -> int:
        """Delete records past retention; return how many."""
        ...

    async def aclose(self) -> None: ...


def _sort_key(r: UsageRecord) -> tuple[datetime, str]:
    return (r.finished_at, r.turn_id)


class MemoryUsageStore:
    """Process-local store (lost on restart)."""

    def __init__(self, *, retention_days: int, clock: Clock = _utcnow) -> None:
        self._records: list[UsageRecord] = []
        self._retention = timedelta(days=retention_days)
        self._clock = clock
        self._last_purge = 0.0
        self._lock = asyncio.Lock()

    async def append(self, record: UsageRecord) -> None:
        async with self._lock:
            self._records.append(record)
        metrics.increment("usage_records_total", store="memory")
        if time.monotonic() - self._last_purge > _PURGE_INTERVAL_S:
            await self.purge()

    async def query(
        self,
        *,
        org_id: str | None,
        thread_id: str | None,
        since: datetime,
        until: datetime,
    ) -> list[UsageRecord]:
        async with self._lock:
            rows = [
                r
                for r in self._records
                if r.org_id == org_id
                and (thread_id is None or r.thread_id == thread_id)
                and since <= r.finished_at <= until
            ]
        return sorted(rows, key=_sort_key, reverse=True)

    async def purge(self) -> int:
        cutoff = self._clock() - self._retention
        async with self._lock:
            before = len(self._records)
            self._records = [r for r in self._records if r.finished_at >= cutoff]
            self._last_purge = time.monotonic()
            return before - len(self._records)

    async def aclose(self) -> None:
        return None


def _ts(dt: datetime) -> str:
    # Fixed-width UTC ISO with microseconds: lexicographic order = time order.
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    turn_id       TEXT PRIMARY KEY,
    org_key       TEXT NOT NULL,
    org_id        TEXT,
    thread_id     TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    finish_reason TEXT NOT NULL,
    usage_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_records_org_time
    ON usage_records (org_key, finished_at);
"""


def _org_key(org_id: str | None) -> str:
    # NOT NULL key so `=` matches the single-tenant (None) org too.
    return "" if org_id is None else "o:" + org_id


class SqliteUsageStore:
    """SQLite-backed store via ``aiosqlite`` (non-blocking for the event loop)."""

    def __init__(
        self, path: Path, *, retention_days: int, clock: Clock = _utcnow
    ) -> None:
        self._path = Path(path)
        self._retention = timedelta(days=retention_days)
        self._clock = clock
        self._db: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        self._last_purge = 0.0

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            async with self._init_lock:
                if self._db is None:
                    db = await aiosqlite.connect(self._path)
                    await db.executescript(_SCHEMA)
                    await db.commit()
                    self._db = db
        return self._db

    async def append(self, record: UsageRecord) -> None:
        db = await self._conn()
        await db.execute(
            "INSERT INTO usage_records VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.turn_id,
                _org_key(record.org_id),
                record.org_id,
                record.thread_id,
                _ts(record.finished_at),
                record.finish_reason,
                record.usage.model_dump_json(),
            ),
        )
        await db.commit()
        metrics.increment("usage_records_total", store="sqlite")
        if time.monotonic() - self._last_purge > _PURGE_INTERVAL_S:
            await self.purge()

    async def query(
        self,
        *,
        org_id: str | None,
        thread_id: str | None,
        since: datetime,
        until: datetime,
    ) -> list[UsageRecord]:
        db = await self._conn()
        sql = (
            "SELECT org_id, thread_id, turn_id, finished_at, finish_reason, usage_json "
            "FROM usage_records "
            "WHERE org_key = ? AND finished_at >= ? AND finished_at <= ?"
        )
        params: list[str] = [_org_key(org_id), _ts(since), _ts(until)]
        if thread_id is not None:
            sql += " AND thread_id = ?"
            params.append(thread_id)
        sql += " ORDER BY finished_at DESC, turn_id DESC"
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            UsageRecord(
                org_id=row[0],
                thread_id=row[1],
                turn_id=row[2],
                finished_at=datetime.strptime(row[3], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                    tzinfo=UTC
                ),
                finish_reason=row[4],
                usage=UsageWire.model_validate(json.loads(row[5])),
            )
            for row in rows
        ]

    async def purge(self) -> int:
        db = await self._conn()
        cutoff = _ts(self._clock() - self._retention)
        cursor = await db.execute(
            "DELETE FROM usage_records WHERE finished_at < ?", (cutoff,)
        )
        await db.commit()
        self._last_purge = time.monotonic()
        return cursor.rowcount

    async def aclose(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None


def build_usage_store(settings: HostSettings) -> UsageStore:
    """Select the store from ``HostSettings``."""
    if settings.host_usage_store == "sqlite":
        return SqliteUsageStore(
            settings.host_usage_db_path,
            retention_days=settings.host_usage_retention_days,
        )
    return MemoryUsageStore(retention_days=settings.host_usage_retention_days)
