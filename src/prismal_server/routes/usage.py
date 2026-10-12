"""Usage and budget read endpoints (RH2-02-04/05, SPEC-RH2-USE-002/003, BUD-001/002).

``GET /v1/usage`` reads the host's own per-turn records (always scoped to the
auth-resolved org) and sums them field-wise over the whole window.
``GET /v1/budget`` serializes the engine's ``RuntimeContext.budget_status()``
and ``quota_windows()`` field-for-field — status, breach and remaining are never
computed host-side (DD-RH2-004).
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from prismal_server import metrics
from prismal_server.auth import authenticate, resolve_org_id
from prismal_server.usage.aggregate import by_model, cache_hit_ratio, total
from prismal_server.usage.models import (
    BudgetReport,
    QuotaWindow,
    TurnUsage,
    UsageRecord,
    UsageReport,
)

router = APIRouter(prefix="/v1", tags=["usage"])
logger = logging.getLogger(__name__)

DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=31)


class InvalidWindow(Exception):
    """400 ``invalid_window``: unparsable, offset-less, inverted or > 31 days."""


class InvalidCursor(Exception):
    """400 ``invalid_cursor``: not a cursor issued by this endpoint."""


def _parse_ts(raw: str | None, name: str) -> datetime | None:
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise InvalidWindow(f"`{name}` is not an RFC 3339 timestamp") from None
    if value.tzinfo is None:
        raise InvalidWindow(f"`{name}` must carry a UTC offset")
    return value.astimezone(UTC)


def _window(since_raw: str | None, until_raw: str | None) -> tuple[datetime, datetime]:
    until = _parse_ts(until_raw, "until") or datetime.now(UTC)
    since = _parse_ts(since_raw, "since") or until - DEFAULT_WINDOW
    if since > until:
        raise InvalidWindow("`since` is after `until`")
    if until - since > MAX_WINDOW:
        raise InvalidWindow("window exceeds 31 days")
    return since, until


def _key(record: UsageRecord) -> tuple[str, str]:
    return (record.finished_at.isoformat(), record.turn_id)


def _encode_cursor(record: UsageRecord) -> str:
    raw = json.dumps(list(_key(record))).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at, turn_id = json.loads(raw)
        datetime.fromisoformat(at)
    except (binascii.Error, ValueError, TypeError):
        raise InvalidCursor("cursor is invalid") from None
    if not isinstance(turn_id, str):
        raise InvalidCursor("cursor is invalid")
    return (at, turn_id)


@router.get("/usage")
async def get_usage(
    request: Request,
    thread_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Per-turn usage of the caller's org, with whole-window totals."""
    identity = await authenticate(request)
    org_id = resolve_org_id(request, identity)
    start, end = _window(since, until)
    after = _decode_cursor(cursor) if cursor else None

    records = await request.app.state.usage_store.query(
        org_id=org_id, thread_id=thread_id, since=start, until=end
    )
    usages = [r.usage for r in records]
    totals = total(usages)

    # Records are newest first; the cursor is the last key already returned.
    remaining = [r for r in records if after is None or _key(r) < after]
    page = remaining[:limit]
    next_cursor = _encode_cursor(page[-1]) if len(remaining) > limit else None

    report = UsageReport(
        org_id=org_id,
        thread_id=thread_id,
        since=start,
        until=end,
        totals=totals,
        cache_hit_ratio=cache_hit_ratio(totals),
        by_model=by_model(usages),
        turns=[
            TurnUsage(
                thread_id=r.thread_id,
                turn_id=r.turn_id,
                finished_at=r.finished_at,
                finish_reason=r.finish_reason,
                usage=r.usage,
            )
            for r in page
        ],
        next_cursor=next_cursor,
    )
    return report.model_dump(mode="json")


@router.get("/budget")
async def get_budget(request: Request, thread_id: str | None = None) -> dict[str, Any]:
    """The org's budget status and gateway quota windows, as the engine reports them."""
    identity = await authenticate(request)
    org_id = resolve_org_id(request, identity)
    runtime = await request.app.state.registry.get(org_id)

    view = runtime.budget_status(thread_id=thread_id)
    try:
        windows = await runtime.quota_windows()
    except Exception:  # fail-open: the engine already omits failed gateways
        logger.warning(
            "quota window lookup failed for org_id=%s", org_id, exc_info=True
        )
        metrics.increment("quota_lookup_errors_total", gateway="*")
        windows = []

    report = BudgetReport(
        org_id=org_id,
        **dataclasses.asdict(view),
        gateways=[QuotaWindow(**dataclasses.asdict(w)) for w in windows],
        as_of=datetime.now(UTC),
    )
    return report.model_dump(mode="json")
