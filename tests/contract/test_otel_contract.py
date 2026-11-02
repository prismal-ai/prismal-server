"""Contract: host root spans over the OTLP/HTTP wire (RH3-01-05, opt-in).

A real ``OTLPSpanExporter`` (protobuf) posts to an in-test receiver that decodes
``ExportTraceServiceRequest`` exactly as agentlens's ``POST /v1/traces`` does.

Run with: ``uv run pytest -m contract``.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.contract


class _Receiver(BaseHTTPRequestHandler):
    received: list[tuple[dict[str, str], bytes]] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        _Receiver.received.append(
            ({k.lower(): v for k, v in self.headers.items()}, body)
        )
        self.send_response(200)
        self.send_header("content-type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return None


def _value(any_value: Any) -> Any:
    return getattr(any_value, any_value.WhichOneof("value"))


async def test_turn_span_reaches_an_otlp_receiver() -> None:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    from prismal_server.app import create_app
    from prismal_server.config import HostSettings

    _Receiver.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        settings = HostSettings(
            host_fake_llm_mode=True,
            host_otel_enabled=True,
            host_otel_endpoint=f"http://127.0.0.1:{server.server_address[1]}",
            host_otel_headers={"X-Agentlens-Key": "k-test"},  # type: ignore[dict-item]
        )
        app = create_app(settings=settings)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post("/threads/wire-1/messages", json={"content": "hi"})
        assert resp.status_code == 200
        await app.state.tracing.aclose()  # flush the batch processor
    finally:
        server.shutdown()

    assert _Receiver.received, "no OTLP export reached the receiver"
    headers, body = _Receiver.received[0]
    assert headers["x-agentlens-key"] == "k-test"
    assert headers["content-type"] == "application/x-protobuf"
    request = ExportTraceServiceRequest.FromString(body)
    (resource_spans,) = request.resource_spans
    resource = {a.key: _value(a.value) for a in resource_spans.resource.attributes}
    assert resource["service.name"] == "prismal-server"
    spans = [s for scope in resource_spans.scope_spans for s in scope.spans]
    (span,) = spans
    attrs = {a.key: _value(a.value) for a in span.attributes}
    assert span.name == "invoke_agent prismal"
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.usage.input_tokens"] == 10
    assert attrs["gen_ai.usage.output_tokens"] == 5
    assert attrs["gen_ai.conversation.id"] == "wire-1"
    assert span.end_time_unix_nano > span.start_time_unix_nano
