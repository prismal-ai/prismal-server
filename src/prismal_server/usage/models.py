"""Usage and budget wire models (RH2-02-01, SPEC-RH2-USG-002, SPEC-RH2 §4).

``UsageWire`` is the normative 12-key object shared with the engine
(``SPEC-PRT-USG-001``) and the SDK (``SPEC-CS2-USG-001``); field order is part of
the contract (``tests/fixtures/protocols/usage_wire.json``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

CostSource = Literal["gateway", "litellm", "table", "subscription", "none", "mixed"]


def rfc3339(dt: datetime) -> str:
    """RFC 3339 UTC with second precision and a ``Z`` suffix."""
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


UtcDatetime = Annotated[datetime, PlainSerializer(rfc3339, return_type=str)]

_NonNegInt = Annotated[int, Field(ge=0, strict=True)]


class UsageWire(BaseModel):
    """The 12-key usage object, in normative order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: _NonNegInt
    completion_tokens: _NonNegInt
    cache_read_tokens: _NonNegInt
    cache_write_tokens: _NonNegInt
    reasoning_tokens: _NonNegInt
    total_tokens: _NonNegInt
    calls: _NonNegInt
    cost_usd: Annotated[float, Field(ge=0)]
    cost_source: CostSource
    estimated: Annotated[bool, Field(strict=True)]
    served_model: str | None
    gateway: str | None


USAGE_KEYS: tuple[str, ...] = tuple(UsageWire.model_fields)


class UsageRecord(BaseModel):
    """One finished turn's usage, as persisted (SPEC-RH2-USE-001)."""

    model_config = ConfigDict(frozen=True)

    org_id: str | None
    thread_id: str
    turn_id: str
    finished_at: UtcDatetime
    finish_reason: str
    usage: UsageWire


class TurnUsage(BaseModel):
    """A ``turns[]`` row of ``GET /v1/usage`` (no ``org_id``: implied)."""

    thread_id: str
    turn_id: str
    finished_at: UtcDatetime
    finish_reason: str
    usage: UsageWire


class UsageReport(BaseModel):
    """``GET /v1/usage`` response body."""

    org_id: str | None
    thread_id: str | None
    since: UtcDatetime
    until: UtcDatetime
    totals: UsageWire
    cache_hit_ratio: float
    by_model: list[UsageWire]
    turns: list[TurnUsage]
    next_cursor: str | None


class BudgetAmounts(BaseModel):
    tokens: int | None
    cost_usd: float | None
    calls: int | None
    wall_clock_s: float | None


class QuotaWindow(BaseModel):
    gateway: str
    window: Literal["credit", "5h", "daily", "weekly", "monthly"]
    unit: Literal["usd", "usd_equivalent", "requests", "tokens"]
    limit: float | None
    used: float
    remaining: float | None
    resets_at: UtcDatetime | None
    source: Literal["api", "ledger"]
    as_of: UtcDatetime


class BudgetReport(BaseModel):
    """``GET /v1/budget`` response body — engine views serialized field-for-field."""

    org_id: str | None
    enabled: bool
    scope: str
    status: str
    breached_dimension: str | None
    soft_ratio: float | None
    hard_cap: bool | None
    limits: BudgetAmounts | None
    consumed: BudgetAmounts | None
    remaining: BudgetAmounts | None
    gateways: list[QuotaWindow]
    as_of: UtcDatetime
