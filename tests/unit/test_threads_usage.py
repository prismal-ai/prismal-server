"""Turn route ⇄ usage store + 402 (RH2-02-02/03, SPEC-RH2-USE-001, §11)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from httpx import ASGITransport, AsyncClient

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry
from prismal_server.usage.store import MemoryUsageStore

WIRE_DICT = {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
    "total_tokens": 15,
    "calls": 1,
    "cost_usd": 0.0001,
    "cost_source": "gateway",
    "estimated": False,
    "served_model": "m/a",
    "gateway": "openrouter",
}


class Msg:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_call_chunks: list[Any] = []


class Summary:
    def __init__(self, run_id: str, wire: dict[str, Any]) -> None:
        self.run_id = run_id
        self._wire = wire

    def to_usage_wire(self) -> dict[str, Any]:
        return dict(self._wire)


class BudgetExceeded(Exception):
    def __init__(self) -> None:
        self.dimension, self.used, self.limit, self.scope = "cost", 50.2, 50.0, "tenant"
        super().__init__("Budget exceeded on cost (tenant): 50.2 >= 50.0")


def _app(astream: Any, summaries: dict[str, Summary]) -> tuple[Any, MemoryUsageStore]:
    store = MemoryUsageStore(retention_days=90)

    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        return type("RT", (), {"tool_provider": None})()

    class Graph:
        def astream(self, inp: Any, config: Any, *, stream_mode: Any = None) -> Any:
            return astream(config["configurable"]["thread_id"])

    async def graph_factory(*, tool_provider: Any = None) -> Graph:
        return Graph()

    settings = HostSettings(sse_heartbeat_s=1)
    app = create_app(
        settings=settings,
        registry=RuntimeRegistry(settings, builder=builder),
        graph_factory=graph_factory,
        usage_store=store,
        run_summary=lambda thread_id: summaries.get(thread_id),
    )
    return app, store


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_done_usage_is_recorded_per_org() -> None:
    summaries: dict[str, Summary] = {}

    def astream(thread_id: str) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            summaries[thread_id] = Summary(f"{thread_id}.turn1", WIRE_DICT)
            yield (Msg("hi"), {})

        return gen()

    app, store = _app(astream, summaries)
    async with _client(app) as c:
        resp = await c.post(
            "/threads/t-1/messages", json={"content": "x"}, headers={"X-Org-Id": "acme"}
        )
    assert resp.status_code == 200
    assert '"usage": {"prompt_tokens": 10' in resp.text
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    rows = await store.query(
        org_id="acme", thread_id=None, since=now - timedelta(minutes=1), until=now
    )
    assert len(rows) == 1
    assert rows[0].thread_id == "t-1" and rows[0].usage.calls == 1
    assert rows[0].finish_reason == "stop"


async def test_stale_summary_from_previous_turn_is_not_reused() -> None:
    """A turn with no LLM call must not re-report the previous turn's usage."""
    summaries = {"t-1": Summary("t-1.turn1", WIRE_DICT)}

    def astream(thread_id: str) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            yield (Msg("cached answer"), {})

        return gen()

    app, store = _app(astream, summaries)
    async with _client(app) as c:
        resp = await c.post("/threads/t-1/messages", json={"content": "x"})
    assert '"usage": null' in resp.text
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    assert (
        await store.query(
            org_id=None, thread_id=None, since=now - timedelta(minutes=1), until=now
        )
        == []
    )


async def test_budget_exceeded_before_first_token_is_402() -> None:
    """EARS §11: 402 with scope/limit/consumed, no stream started."""

    def astream(thread_id: str) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            raise BudgetExceeded()
            yield  # pragma: no cover

        return gen()

    app, _ = _app(astream, {})
    async with _client(app) as c:
        resp = await c.post("/threads/t-1/messages", json={"content": "x"})
    assert resp.status_code == 402
    assert resp.json() == {
        "error": {
            "code": "budget_exceeded",
            "message": "Budget exceeded on cost (tenant): 50.2 >= 50.0",
            "scope": "tenant",
            "limit": 50.0,
            "consumed": 50.2,
        }
    }


async def test_other_first_chunk_errors_still_stream_as_error_event() -> None:
    """v0.1 behaviour (SPEC-RHB-CHT-006) is unchanged for non-budget errors."""

    def astream(thread_id: str) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            raise RuntimeError("boom")
            yield  # pragma: no cover

        return gen()

    app, _ = _app(astream, {})
    async with _client(app) as c:
        resp = await c.post("/threads/t-1/messages", json={"content": "x"})
    assert resp.status_code == 200
    assert "event: error" in resp.text and "internal_error" in resp.text
