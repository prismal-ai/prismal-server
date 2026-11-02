"""``done.usage`` and budget errors on the native stream (RH2-02-02, USG-001, §11)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from prismal_server import metrics
from prismal_server.streaming import ChunkPump, sse_stream
from prismal_server.usage.models import USAGE_KEYS, UsageWire

WIRE = UsageWire.model_validate(
    {
        "prompt_tokens": 1200,
        "completion_tokens": 340,
        "cache_read_tokens": 900,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 1540,
        "calls": 3,
        "cost_usd": 0.004213,
        "cost_source": "gateway",
        "estimated": False,
        "served_model": "anthropic/claude-sonnet-4.5",
        "gateway": "openrouter",
    }
)


class Msg:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_call_chunks: list[Any] = []


class BudgetExceeded(Exception):
    def __init__(self) -> None:
        self.dimension, self.used, self.limit, self.scope = "tokens", 2001, 2000, "turn"
        super().__init__("Budget exceeded on tokens (turn): 2001 >= 2000")


def _events(frames: list[str]) -> list[tuple[str, Any]]:
    out = []
    for f in frames:
        if f.startswith("event: "):
            head, data = f.split("\n", 1)
            out.append((head[len("event: ") :], json.loads(data[len("data: ") :])))
    return out


async def _collect(agen: AsyncIterator[str]) -> list[str]:
    return [f async for f in agen]


async def _two_tokens() -> AsyncIterator[Any]:
    yield (Msg("he"), {})
    yield (Msg("llo"), {})


async def test_done_carries_usage_in_key_order() -> None:
    done_calls: list[tuple[str, Any]] = []

    async def on_done(reason: str, usage: UsageWire | None) -> None:
        done_calls.append((reason, usage))

    frames = await _collect(
        sse_stream(
            _two_tokens(),
            thread_id="t-42",
            heartbeat_s=100,
            usage_for=lambda: WIRE,
            on_done=on_done,
        )
    )
    name, data = _events(frames)[-1]
    assert name == "done"
    assert list(data) == ["thread_id", "finish_reason", "usage"]
    assert data["thread_id"] == "t-42" and data["finish_reason"] == "stop"
    assert tuple(data["usage"]) == USAGE_KEYS
    assert data["usage"] == WIRE.model_dump()
    assert done_calls == [("stop", WIRE)]


async def test_missing_summary_is_null_and_counted() -> None:
    """EARS USG-001: fail-open ``usage: null`` + counter, never an error."""
    before = metrics.get("usage_missing_total")
    frames = await _collect(
        sse_stream(
            _two_tokens(), thread_id="t", heartbeat_s=100, usage_for=lambda: None
        )
    )
    name, data = _events(frames)[-1]
    assert name == "done" and data["usage"] is None
    assert metrics.get("usage_missing_total") == before + 1


async def test_usage_provider_failure_is_fail_open() -> None:
    def broken() -> UsageWire | None:
        raise RuntimeError("telemetry down")

    frames = await _collect(
        sse_stream(_two_tokens(), thread_id="t", heartbeat_s=100, usage_for=broken)
    )
    assert _events(frames)[-1] == (
        "done",
        {"thread_id": "t", "finish_reason": "stop", "usage": None},
    )


async def test_on_done_failure_does_not_break_stream() -> None:
    async def failing(reason: str, usage: UsageWire | None) -> None:
        raise RuntimeError("store down")

    frames = await _collect(
        sse_stream(
            _two_tokens(),
            thread_id="t",
            heartbeat_s=100,
            usage_for=lambda: WIRE,
            on_done=failing,
        )
    )
    assert _events(frames)[-1][0] == "done"


async def test_midstream_budget_error_carries_partial_usage() -> None:
    async def chunks() -> AsyncIterator[Any]:
        yield (Msg("partial"), {})
        raise BudgetExceeded()

    frames = await _collect(
        sse_stream(chunks(), thread_id="t", heartbeat_s=100, usage_for=lambda: WIRE)
    )
    name, data = _events(frames)[-1]
    assert name == "error"
    assert data["code"] == "budget_exceeded"
    assert (
        data["scope"] == "turn" and data["limit"] == 2000 and data["consumed"] == 2001
    )
    assert data["usage"] == WIRE.model_dump()


async def test_pump_prime_exposes_first_item_without_consuming() -> None:
    pump = ChunkPump(_two_tokens())
    first = await pump.prime(wait_s=1.0)
    assert first is not None and first[0] == "chunk"
    frames = await _collect(sse_stream(pump, thread_id="t", heartbeat_s=100))
    assert [e for e in _events(frames) if e[0] == "token"] == [
        ("token", {"delta": "he"}),
        ("token", {"delta": "llo"}),
    ]


async def test_pump_prime_surfaces_early_error() -> None:
    async def chunks() -> AsyncIterator[Any]:
        raise BudgetExceeded()
        yield  # pragma: no cover

    pump = ChunkPump(chunks())
    first = await pump.prime(wait_s=1.0)
    await pump.aclose()
    assert first is not None and first[0] == "error"
    assert type(first[1]).__name__ == "BudgetExceeded"
