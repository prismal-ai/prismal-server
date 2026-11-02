"""Spans on HTTP chat turns: native SSE and AG-UI (RH3-01-02)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.fake import FakeGraph
from prismal_server.streaming import sse_stream
from prismal_server.tracing import Tracing

MESSAGES = "/threads/{thread_id}/messages"


class BudgetExceeded(Exception):  # mapped by class name, like the engine's
    pass


def _app(mem: InMemorySpanExporter, **kw: Any) -> Any:
    settings = HostSettings(host_fake_llm_mode=True, **kw.pop("settings", {}))
    return create_app(settings=settings, tracing=Tracing(settings, exporter=mem), **kw)


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _spans(mem: InMemorySpanExporter) -> list[ReadableSpan]:
    return list(mem.get_finished_spans())


def _one(mem: InMemorySpanExporter) -> ReadableSpan:
    spans = _spans(mem)
    assert len(spans) == 1, [s.name for s in spans]
    return spans[0]


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


@pytest.fixture
def mem() -> InMemorySpanExporter:
    return InMemorySpanExporter()


async def test_sse_turn_span_has_usage_and_ok(mem: InMemorySpanExporter) -> None:
    async with _client(_app(mem)) as c:
        resp = await c.post("/threads/t-1/messages", json={"content": "hello"})
    assert resp.status_code == 200
    s = _one(mem)
    a = _attrs(s)
    assert s.status.status_code == StatusCode.OK
    assert a["gen_ai.usage.input_tokens"] == 10
    assert a["gen_ai.usage.output_tokens"] == 5
    assert a["gen_ai.response.model"] == "fake/echo"
    assert a["prismal.finish_reason"] == "stop"
    assert a["http.route"] == MESSAGES
    assert a["prismal.transport"] == "sse"
    assert a["prismal.protocol"] == "native"
    assert a["gen_ai.conversation.id"] == "t-1"


@pytest.fixture
def agui_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    from prismal.a2a import get_settings

    monkeypatch.setenv("PRISMAL_AGUI_EVENTS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_agui_turn_span_protocol(mem: InMemorySpanExporter, agui_on: Any) -> None:
    run_input = {
        "threadId": "t-2",
        "runId": "r-1",
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
        "tools": [],
        "context": [],
        "state": {},
        "forwardedProps": {},
    }
    async with _client(_app(mem)) as c:
        r1 = await c.post(
            "/threads/t-2/messages", params={"protocol": "ag-ui"}, json={"content": "x"}
        )
        r2 = await c.post("/v1/agui", json=run_input)
    assert r1.status_code == r2.status_code == 200
    spans = _spans(mem)
    assert len(spans) == 2
    assert [_attrs(s)["http.route"] for s in spans] == [MESSAGES, "/v1/agui"]
    for s in spans:
        assert _attrs(s)["prismal.protocol"] == "ag-ui"
        assert s.status.status_code == StatusCode.OK
        assert _attrs(s)["gen_ai.usage.input_tokens"] == 10


async def test_midstream_error_span(mem: InMemorySpanExporter) -> None:
    async with _client(_app(mem)) as c:
        await c.post("/threads/t-3/messages", json={"content": "/error"})
    s = _one(mem)
    assert s.status.status_code == StatusCode.ERROR
    assert _attrs(s)["error.type"] == "internal_error"


class _BudgetGraph:
    def astream(self, *a: Any, **k: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            raise BudgetExceeded("cap")
            yield  # pragma: no cover

        return gen()


async def _budget_factory(*, tool_provider: Any = None) -> Any:
    return _BudgetGraph()


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("budget", 402, "budget_exceeded"),
        ("agui_off", 406, "protocol_not_enabled"),
        ("thread_id", 422, "invalid_thread_id"),
        ("tenant", 403, "forbidden_tenant"),
        ("agui_input", 422, "invalid_input"),
    ],
)
async def test_pre_stream_refusals_are_traced(
    mem: InMemorySpanExporter,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    status: int,
    code: str,
) -> None:
    from prismal.a2a import get_settings

    monkeypatch.delenv("PRISMAL_AGUI_EVENTS_ENABLED", raising=False)
    get_settings.cache_clear()
    kw: dict[str, Any] = {}
    path, params, body, headers = "/threads/t/messages", {}, {"content": "x"}, {}
    if case == "budget":
        kw["graph_factory"] = _budget_factory
    elif case == "agui_off":
        params = {"protocol": "ag-ui"}
    elif case == "thread_id":
        path = "/threads/12:x/messages"
    elif case == "tenant":
        kw["settings"] = {
            "host_tenant_config_dir": tmp_path,
            "host_auth_backend": "bearer",
            "bearer_static_tokens": {"tok": "did:key:zCaller"},
        }
        headers = {"Authorization": "Bearer tok", "X-Org-Id": "acme"}
    else:
        path = "/v1/agui"
        body = {
            "threadId": "t",
            "runId": "r",
            "messages": [{"id": "m", "role": "assistant", "content": "no user"}],
            "tools": [],
            "context": [],
            "state": {},
            "forwardedProps": {},
        }
    async with _client(_app(mem, **kw)) as c:
        resp = await c.post(path, params=params, json=body, headers=headers)
    get_settings.cache_clear()
    assert resp.status_code == status
    s = _one(mem)
    assert s.status.status_code == StatusCode.ERROR
    assert _attrs(s)["error.type"] == code
    assert s.status.description == code  # never "Type: message"
    assert not s.events  # no exception event (message/stack can carry content)


async def test_disconnect_marks_cancelled(mem: InMemorySpanExporter) -> None:
    tracing = Tracing(HostSettings(), exporter=mem)
    span = tracing.turn(route=MESSAGES, transport="sse", protocol="native")

    async def chunks() -> AsyncIterator[Any]:
        yield FakeGraph._chunk(content="hi", tagged=False, meta={})
        await asyncio.sleep(60)

    body = sse_stream(chunks(), thread_id="t", heartbeat_s=30, span=span)
    first = await body.__anext__()
    assert "hi" in first
    await body.aclose()  # what Starlette does when the client goes away
    s = _one(mem)
    assert s.status.status_code == StatusCode.UNSET
    assert _attrs(s)["prismal.outcome"] == "cancelled"


async def test_concurrent_turns_do_not_bleed(mem: InMemorySpanExporter) -> None:
    async with _client(_app(mem)) as c:
        await asyncio.gather(
            c.post("/threads/t-a/messages", json={"content": "a"}),
            c.post("/threads/t-b/messages", json={"content": "b"}),
        )
    spans = _spans(mem)
    assert len(spans) == 2
    assert len({s.context.trace_id for s in spans}) == 2
    assert {_attrs(s)["gen_ai.conversation.id"] for s in spans} == {"t-a", "t-b"}


async def test_no_content_or_secret_in_spans(
    mem: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app = _app(
        mem, settings={"host_otel_headers": {"X-Agentlens-Key": "CANARY-SECRET"}}
    )
    async with _client(app) as c:
        await c.post("/threads/t-c/messages", json={"content": "CANARY-CONTENT"})
        await c.post("/threads/t-c/messages", json={"content": "/error"})
    dumped = repr([(s.attributes, s.events, s.status, s.resource) for s in _spans(mem)])
    for canary in ("CANARY-SECRET", "CANARY-CONTENT"):
        assert canary not in dumped
        assert canary not in caplog.text


class _NestingGraph:
    def __init__(self, provider: TracerProvider) -> None:
        self._tracer = provider.get_tracer("engine")

    def astream(self, *a: Any, **k: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            with self._tracer.start_as_current_span("engine.child"):
                yield FakeGraph._chunk(content="x", tagged=False, meta={})

        return gen()


async def test_engine_span_nests_under_turn(mem: InMemorySpanExporter) -> None:
    engine_mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(engine_mem))

    async def factory(*, tool_provider: Any = None) -> Any:
        return _NestingGraph(provider)

    async with _client(_app(mem, graph_factory=factory)) as c:
        await c.post("/threads/t-n/messages", json={"content": "x"})
    root = _one(mem)
    (child,) = engine_mem.get_finished_spans()
    assert child.context.trace_id == root.context.trace_id
    assert child.parent is not None and child.parent.span_id == root.context.span_id


async def test_untraced_routes(mem: InMemorySpanExporter) -> None:
    async with _client(_app(mem)) as c:
        for path in ("/healthz", "/readyz", "/v1/usage", "/v1/budget"):
            await c.get(path)
    assert _spans(mem) == []


async def test_exception_message_never_exported(mem: InMemorySpanExporter) -> None:
    async def factory(*, tool_provider: Any = None) -> Any:
        raise RuntimeError("could not connect postgresql://u:CANARY-PW@db/x")

    async with _client(_app(mem, graph_factory=factory)) as c:
        resp = await c.post("/threads/t-x/messages", json={"content": "x"})
    assert resp.status_code == 500
    s = _one(mem)
    assert s.status.status_code == StatusCode.ERROR
    assert _attrs(s)["error.type"] == "internal_error"
    assert not s.events
    assert "CANARY-PW" not in repr((s.attributes, s.events, s.status))


async def test_host_side_stream_error_is_error_not_cancelled(
    mem: InMemorySpanExporter,
) -> None:
    tracing = Tracing(HostSettings(), exporter=mem)
    span = tracing.turn(route=MESSAGES, transport="sse", protocol="native")

    async def chunks() -> AsyncIterator[Any]:
        yield FakeGraph._chunk(content="hi", tagged=False, meta={})

    def boom(chunk: Any) -> Any:
        raise KeyError("translate bug")

    body = sse_stream(chunks(), thread_id="t", translate=boom, span=span)
    with pytest.raises(KeyError):
        async for _ in body:
            pass
    s = _one(mem)
    assert s.status.status_code == StatusCode.ERROR
    assert _attrs(s)["error.type"] == "internal_error"
    assert "prismal.outcome" not in _attrs(s)


async def test_runtime_build_does_not_inherit_turn_span(
    mem: InMemorySpanExporter,
) -> None:
    from opentelemetry import trace

    app = _app(mem)
    registry = app.state.registry
    real_acquire = registry.acquire
    seen: list[bool] = []

    async def acquire(org_id: Any) -> Any:
        # Engine background tasks started while building a runtime (quota
        # poller) copy this context; it must not hold the request's span.
        seen.append(trace.get_current_span().get_span_context().is_valid)
        return await real_acquire(org_id)

    registry.acquire = acquire
    async with _client(app) as c:
        await c.post("/threads/t-r/messages", json={"content": "x"})
    assert seen == [False]
