"""Per-request OTel traces for agentlens (RH3-01, specs/host-v0.3-otel-traces).

One root span per agent request (SSE / AG-UI / WebSocket turn, message-sending
A2A RPC): an OTel GenAI *agent* span (``invoke_agent``) carrying the turn's
usage, outcome and duration. agentlens uses an agent span's usage only when the
trace has no LLM spans, so tokens are never double-counted once the engine
emits GenAI LLM spans.

The host owns its ``TracerProvider`` and never sets the global one (the engine's
``OTelManager`` does). Parent/child linking goes through the OTel context, so
engine spans started while a turn span is current nest under it.

Never on a span: message content, tool arguments, exception messages, secrets.
Tokens come only from the turn's ``UsageWire`` (engine numbers).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Iterator
from typing import Any, Literal

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import SpanKind, Status, StatusCode

from prismal_server import __version__
from prismal_server.config import HostSettings
from prismal_server.errors import map_exception
from prismal_server.usage.models import UsageWire

logger = logging.getLogger(__name__)

Transport = Literal["sse", "ws", "a2a"]

SHUTDOWN_TIMEOUT_S = 10.0
EXPORT_TIMEOUT_S = 5

_OUTCOME_OK = "ok"
_OUTCOME_FAILED = "failed"


def conversation_id(org_id: str | None, local_id: str | None) -> str | None:
    """agentlens session key: tenants never share one (``"{org}/{id}"``)."""
    if local_id is None:
        return None
    return local_id if org_id is None else f"{org_id}/{local_id}"


def route_template(request: Any) -> str:
    """The matched route's path template (``/threads/{thread_id}/messages``)."""
    path = getattr(request.scope.get("route"), "path", None)
    return path if isinstance(path, str) else str(request.url.path)


def fail_span(span: TurnSpan, exc: BaseException) -> None:
    """End ``span`` for an exception: the host error code, or cancelled."""
    if isinstance(exc, Exception):
        span.fail(map_exception(exc).code)
    span.end()


class TurnSpan:
    """The root span of one agent request; every method is safe to call twice."""

    def __init__(self, span: trace.Span) -> None:
        self._span = span
        self._outcome: str | None = None
        self._ended = False

    @contextlib.contextmanager
    def activate(self) -> Iterator[None]:
        """Make the span current (tasks created inside inherit it); never ends it."""
        # No exception event / "Type: message" status: messages and stacks
        # can carry content or credentials (DESIGN §3, §5.3).
        with trace.use_span(
            self._span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            yield

    def set_tenant(self, org_id: str | None, conversation: str | None) -> None:
        if org_id is not None:
            self._span.set_attribute("prismal.org_id", org_id)
        if conversation is not None:
            self._span.set_attribute("gen_ai.conversation.id", conversation)

    def record_usage(self, usage: UsageWire | None) -> None:
        if usage is None or self._ended:
            return
        attrs: dict[str, Any] = {
            "gen_ai.usage.input_tokens": usage.prompt_tokens,
            "gen_ai.usage.output_tokens": usage.completion_tokens,
            "gen_ai.usage.cache_read.input_tokens": usage.cache_read_tokens,
            "gen_ai.usage.cache_write.input_tokens": usage.cache_write_tokens,
            "gen_ai.usage.reasoning.output_tokens": usage.reasoning_tokens,
            "prismal.usage.calls": usage.calls,
            "prismal.usage.cost_usd": usage.cost_usd,
            "prismal.usage.cost_source": usage.cost_source,
            "prismal.usage.estimated": usage.estimated,
        }
        if usage.served_model is not None:
            attrs["gen_ai.response.model"] = usage.served_model
        if usage.gateway is not None:
            attrs["prismal.gateway"] = usage.gateway
        self._span.set_attributes(attrs)

    def complete(self, finish_reason: str | None = None) -> None:
        if self._outcome is not None or self._ended:
            return
        self._outcome = _OUTCOME_OK
        if finish_reason is not None:
            self._span.set_attribute("prismal.finish_reason", finish_reason)
        self._span.set_status(Status(StatusCode.OK))

    def fail(self, code: str) -> None:
        """Error outcome: the host error **code** only, never a message or stack."""
        if self._outcome is not None or self._ended:
            return
        self._outcome = _OUTCOME_FAILED
        self._span.set_attribute("error.type", code)
        self._span.set_status(Status(StatusCode.ERROR, code))

    def end(self) -> None:
        if self._ended:
            return
        if self._outcome is None:  # disconnect / cancel: not an error
            self._span.set_attribute("prismal.outcome", "cancelled")
        self._ended = True
        try:
            self._span.end()
        except Exception:  # export problems must never reach a request
            logger.warning("tracing: span end failed", exc_info=True)


class Tracing:
    """The host's tracer; a no-op unless enabled (or given a test exporter)."""

    def __init__(
        self, settings: HostSettings, *, exporter: SpanExporter | None = None
    ) -> None:
        self._agent_name = settings.host_otel_agent_name
        self.enabled = bool(settings.host_otel_enabled or exporter is not None)
        self._provider: TracerProvider | None = None
        if not self.enabled:
            self._tracer: trace.Tracer = trace.NoOpTracer()
            return
        resource = Resource.create(
            {
                "service.name": settings.host_otel_service_name,
                "service.version": __version__,
                "deployment.environment.name": settings.host_environment,
            }
        )
        # Shutdown is the lifespan's job (bounded); no atexit flush that could
        # stall process exit retrying a dead collector.
        provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        if exporter is not None:
            provider.add_span_processor(_SafeSimpleProcessor(exporter))
        else:
            otlp = OTLPSpanExporter(
                endpoint=f"{settings.host_otel_endpoint.rstrip('/')}/v1/traces",
                headers={
                    k: v.get_secret_value()
                    for k, v in settings.host_otel_headers.items()
                },
                timeout=EXPORT_TIMEOUT_S,
            )
            provider.add_span_processor(BatchSpanProcessor(otlp))
        self._provider = provider
        self._tracer = provider.get_tracer("prismal_server", __version__)

    def turn(
        self,
        *,
        route: str,
        transport: Transport,
        protocol: str,
        a2a_method: str | None = None,
    ) -> TurnSpan:
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": self._agent_name,
            "http.route": route,
            "prismal.transport": transport,
            "prismal.protocol": protocol,
        }
        if a2a_method is not None:
            attrs["prismal.a2a.method"] = a2a_method
        span = self._tracer.start_span(
            f"invoke_agent {self._agent_name}", kind=SpanKind.SERVER, attributes=attrs
        )
        return TurnSpan(span)

    async def aclose(self) -> None:
        """Flush and stop export; bounded so a dead collector never hangs shutdown."""
        provider, self._provider = self._provider, None
        if provider is None:
            return
        # A daemon thread, not the default executor: the loop's executor
        # shutdown would otherwise wait on a hung exporter after the timeout.
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()

        def run() -> None:
            try:
                provider.shutdown()
            except Exception:
                logger.warning("tracing: provider shutdown failed", exc_info=True)
            finally:
                with contextlib.suppress(RuntimeError):  # loop already closed
                    loop.call_soon_threadsafe(_resolve, done)

        threading.Thread(target=run, name="otel-shutdown", daemon=True).start()
        try:
            await asyncio.wait_for(done, timeout=SHUTDOWN_TIMEOUT_S)
        except TimeoutError:
            logger.warning("tracing: provider shutdown timed out")


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class _SafeSimpleProcessor(SimpleSpanProcessor):
    """Synchronous export (tests); an exporter error never reaches the caller."""

    def on_end(self, span: Any) -> None:
        try:
            super().on_end(span)
        except Exception:
            logger.warning("tracing: export failed", exc_info=True)
