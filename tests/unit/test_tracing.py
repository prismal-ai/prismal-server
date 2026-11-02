"""Host OTel tracing core (RH3-01-01, specs/host-v0.3-otel-traces/DESIGN.md §5)."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode

from prismal_server import tracing as tracing_mod
from prismal_server.config import HostSettings
from prismal_server.tracing import Tracing, conversation_id
from prismal_server.usage.models import UsageWire

USAGE = UsageWire(
    prompt_tokens=1200,
    completion_tokens=340,
    cache_read_tokens=900,
    cache_write_tokens=7,
    reasoning_tokens=3,
    total_tokens=1540,
    calls=3,
    cost_usd=0.004213,
    cost_source="gateway",
    estimated=False,
    served_model="anthropic/claude-sonnet-4.5",
    gateway="openrouter",
)


@pytest.fixture
def mem() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracing(mem: InMemorySpanExporter) -> Tracing:
    return Tracing(HostSettings(), exporter=mem)


def _one(mem: InMemorySpanExporter) -> ReadableSpan:
    spans = mem.get_finished_spans()
    assert len(spans) == 1, spans
    return spans[0]


def _sse_turn(tracing: Tracing) -> Any:
    return tracing.turn(
        route="/threads/{thread_id}/messages", transport="sse", protocol="native"
    )


def test_disabled_creates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("exporter must not be built when disabled")

    monkeypatch.setattr(tracing_mod, "OTLPSpanExporter", boom)
    t = Tracing(HostSettings())
    assert t.enabled is False
    assert t._provider is None
    span = _sse_turn(t)
    span.set_tenant("acme", "acme/t")
    span.record_usage(USAGE)
    span.complete("stop")
    span.end()


def test_completed_turn_span_shape(tracing: Tracing, mem: InMemorySpanExporter) -> None:
    span = _sse_turn(tracing)
    span.set_tenant("acme", conversation_id("acme", "t-1"))
    span.record_usage(USAGE)
    span.complete("stop")
    span.end()
    s = _one(mem)
    assert s.name == "invoke_agent prismal"
    assert s.kind == SpanKind.SERVER
    assert s.status.status_code == StatusCode.OK
    assert dict(s.attributes or {}) == {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": "prismal",
        "http.route": "/threads/{thread_id}/messages",
        "prismal.transport": "sse",
        "prismal.protocol": "native",
        "prismal.org_id": "acme",
        "gen_ai.conversation.id": "acme/t-1",
        "gen_ai.usage.input_tokens": 1200,
        "gen_ai.usage.output_tokens": 340,
        "gen_ai.usage.cache_read.input_tokens": 900,
        "gen_ai.usage.cache_write.input_tokens": 7,
        "gen_ai.usage.reasoning.output_tokens": 3,
        "prismal.usage.calls": 3,
        "prismal.usage.cost_usd": 0.004213,
        "prismal.usage.cost_source": "gateway",
        "prismal.usage.estimated": False,
        "gen_ai.response.model": "anthropic/claude-sonnet-4.5",
        "prismal.gateway": "openrouter",
        "prismal.finish_reason": "stop",
    }
    assert s.resource.attributes["service.name"] == "prismal-server"
    assert s.resource.attributes["deployment.environment.name"] == "development"
    assert "service.version" in s.resource.attributes


def test_a2a_method_attribute(tracing: Tracing, mem: InMemorySpanExporter) -> None:
    span = tracing.turn(
        route="/a2a", transport="a2a", protocol="a2a-1.0", a2a_method="SendMessage"
    )
    span.end()
    assert _one(mem).attributes["prismal.a2a.method"] == "SendMessage"  # type: ignore[index]


def test_null_usage_sets_no_usage_attributes(
    tracing: Tracing, mem: InMemorySpanExporter
) -> None:
    span = _sse_turn(tracing)
    span.record_usage(None)
    span.complete("stop")
    span.end()
    keys = set(_one(mem).attributes or {})
    assert not {k for k in keys if k.startswith(("gen_ai.usage.", "prismal.usage."))}
    assert "gen_ai.response.model" not in keys


def test_fail_sets_error_code_only(tracing: Tracing, mem: InMemorySpanExporter) -> None:
    span = _sse_turn(tracing)
    span.fail("budget_exceeded")
    span.end()
    s = _one(mem)
    assert s.status.status_code == StatusCode.ERROR
    assert s.status.description == "budget_exceeded"
    assert s.attributes["error.type"] == "budget_exceeded"  # type: ignore[index]
    assert not s.events


def test_end_without_outcome_is_cancelled(
    tracing: Tracing, mem: InMemorySpanExporter
) -> None:
    span = _sse_turn(tracing)
    span.end()
    s = _one(mem)
    assert s.status.status_code == StatusCode.UNSET
    assert s.attributes["prismal.outcome"] == "cancelled"  # type: ignore[index]


def test_end_is_idempotent(tracing: Tracing, mem: InMemorySpanExporter) -> None:
    span = _sse_turn(tracing)
    span.complete()
    span.end()
    span.end()
    span.fail("late")  # after end: ignored
    assert _one(mem).status.status_code == StatusCode.OK


def test_conversation_id() -> None:
    assert conversation_id(None, "t") == "t"
    assert conversation_id("acme", "t") == "acme/t"
    assert conversation_id("acme", None) is None


def test_child_span_nests_under_activated_turn(
    tracing: Tracing, mem: InMemorySpanExporter
) -> None:
    other = InMemorySpanExporter()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider2 = TracerProvider()
    provider2.add_span_processor(SimpleSpanProcessor(other))
    span = _sse_turn(tracing)
    with span.activate():
        with provider2.get_tracer("engine").start_as_current_span("child"):
            pass
    span.complete()
    span.end()
    root = _one(mem)
    (child,) = other.get_finished_spans()
    assert child.context.trace_id == root.context.trace_id
    assert child.parent is not None
    assert child.parent.span_id == root.context.span_id


class _Raising(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise RuntimeError("collector down")

    def shutdown(self) -> None:
        return None


def test_export_failure_never_breaks_a_turn() -> None:
    t = Tracing(HostSettings(), exporter=_Raising())
    span = _sse_turn(t)
    span.complete()
    span.end()


class _Hanging(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        time.sleep(30)


async def test_shutdown_is_bounded_when_exporter_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracing_mod, "SHUTDOWN_TIMEOUT_S", 0.2)
    t = Tracing(HostSettings(), exporter=_Hanging())
    started = time.monotonic()
    await t.aclose()
    assert time.monotonic() - started < 2


def test_global_provider_untouched() -> None:
    before = trace.get_tracer_provider()
    Tracing(HostSettings(host_otel_enabled=True))
    assert trace.get_tracer_provider() is before


def test_enabled_builds_otlp_exporter_with_secret_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class FakeOTLP(_Raising):
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(tracing_mod, "OTLPSpanExporter", FakeOTLP)
    Tracing(
        HostSettings(
            host_otel_enabled=True,
            host_otel_endpoint="http://lens:4318",
            host_otel_headers={"X-Agentlens-Key": "k1"},  # type: ignore[dict-item]
        )
    )
    assert seen["endpoint"] == "http://lens:4318/v1/traces"
    assert seen["headers"] == {"X-Agentlens-Key": "k1"}


async def test_lifespan_closes_tracing() -> None:
    from prismal_server.app import create_app

    calls: list[str] = []

    class Spy(Tracing):
        async def aclose(self) -> None:
            calls.append("aclose")

    app = create_app(settings=HostSettings(), tracing=Spy(HostSettings()))
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.tracing, Spy)
    assert calls == ["aclose"]
