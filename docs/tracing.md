# Per-request traces (OpenTelemetry → agentlens)

The host can export **one OpenTelemetry trace per agent request** over OTLP/HTTP,
for example to agentlens or any other OTLP collector. Each trace
answers three questions for one request: how many tokens it consumed, whether it
failed (and with which host error code), and how long it took.

Design: [`specs/host-v0.3-otel-traces/DESIGN.md`](../specs/host-v0.3-otel-traces/DESIGN.md).

Tracing is **off by default**. When it is off the host uses a no-op tracer and
exports nothing.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `PRISMAL_SERVER_HOST_OTEL_ENABLED` | `false` | Turn export on |
| `PRISMAL_SERVER_HOST_OTEL_ENDPOINT` | `http://localhost:4318` | OTLP/HTTP base URL; the host posts to `{endpoint}/v1/traces` |
| `PRISMAL_SERVER_HOST_OTEL_HEADERS` | `{}` | Extra export headers, as JSON. Values are secrets (`SecretStr`) and are never logged or put on a span |
| `PRISMAL_SERVER_HOST_OTEL_SERVICE_NAME` | `prismal-server` | Resource `service.name` |
| `PRISMAL_SERVER_HOST_OTEL_AGENT_NAME` | `prismal` | `gen_ai.agent.name`; the span is named `invoke_agent {agent_name}` |

The resource also carries `service.version` (the host version) and
`deployment.environment.name` (`PRISMAL_SERVER_HOST_ENVIRONMENT`).

### agentlens example

```bash
PRISMAL_SERVER_HOST_OTEL_ENABLED=true
PRISMAL_SERVER_HOST_OTEL_ENDPOINT=http://agentlens:8000    # host appends /v1/traces
PRISMAL_SERVER_HOST_OTEL_HEADERS='{"X-Agentlens-Key":"<project key>"}'
```

All tenants export to the same agentlens project. Tenants are told apart by the
`prismal.org_id` attribute and by the org prefix on `gen_ai.conversation.id`.

Export is batched in a background thread with a 5 s timeout per request. A dead
or slow collector never fails or delays a request. Shutdown flushes for at most
10 s and then gives up.

## What is traced

| Request | Traced |
|---|---|
| `POST /threads/{thread_id}/messages` (native SSE and `?protocol=ag-ui`) | ✅ one span per turn |
| `POST /v1/agui` | ✅ one span per run |
| `WS /ws/threads/{thread_id}` | ✅ one span **per turn** (not per connection) |
| `POST /a2a` (and tenant A2A routes) with `SendMessage`, `SendStreamingMessage`, `message/send`, `message/stream` | ✅ one span per RPC |
| A2A task management (`GetTask`, `ListTasks`, `CancelTask`, `tasks/*`, …) | ❌ |
| `/healthz`, `/readyz`, `/v1/usage`, `/v1/budget`, admin, Agent Card | ❌ |
| Requests FastAPI rejects before the handler runs (body validation `422`), auth `401`, refused WebSocket handshakes | ❌ |

Refusals made **inside** the handler are traced as errors, for example
`402 budget_exceeded`, `403 forbidden_tenant`, `406 protocol_not_enabled`, and
`422 invalid_thread_id` / `invalid_input`.

## The root span

Kind `SERVER`, named `invoke_agent {agent_name}` (an OTel GenAI *agent* span).

| Attribute | Value |
|---|---|
| `gen_ai.operation.name` | `invoke_agent` |
| `gen_ai.agent.name` | `PRISMAL_SERVER_HOST_OTEL_AGENT_NAME` |
| `gen_ai.conversation.id` | `thread_id`, or `"{org_id}/{thread_id}"` when an org is resolved. For A2A it is the message `contextId` with the same prefix, and it is omitted when absent |
| `prismal.org_id` | the resolved org, when there is one |
| `prismal.transport` | `sse` · `ws` · `a2a` |
| `prismal.protocol` | `native` · `ag-ui` · `a2a-0.3` · `a2a-1.0` (for A2A, taken from the method family) |
| `prismal.a2a.method` | the JSON-RPC method (A2A only) |
| `http.route` | the route template, e.g. `/threads/{thread_id}/messages` |
| `prismal.finish_reason` | `done.finish_reason`, when there is one |
| `gen_ai.response.model` | `usage.served_model`, when not null |
| `gen_ai.usage.input_tokens` | `usage.prompt_tokens` |
| `gen_ai.usage.output_tokens` | `usage.completion_tokens` |
| `gen_ai.usage.cache_read.input_tokens` | `usage.cache_read_tokens` |
| `gen_ai.usage.cache_write.input_tokens` | `usage.cache_write_tokens` |
| `gen_ai.usage.reasoning.output_tokens` | `usage.reasoning_tokens` |
| `prismal.usage.calls` · `prismal.usage.cost_usd` · `prismal.usage.cost_source` · `prismal.usage.estimated` · `prismal.gateway` | the rest of the usage wire (see [usage](./usage.md)) |

Usage attributes come only from the turn's `done.usage`, which holds the engine's
numbers, or from the partial usage on a mid-stream `budget_exceeded`. When usage is
`null` they are left out, never written as zero.

**A2A runs carry no tokens.** The A2A handler returns no usage wire, so A2A spans
show duration and outcome only.

### Outcome

| Case | Span |
|---|---|
| Turn completes (`done`, `RUN_FINISHED`, A2A result) | status `OK` |
| Host error before or during the stream | status `ERROR`, `error.type` = host error code (e.g. `budget_exceeded`, `forbidden_tenant`, `protocol_not_enabled`, `internal_error`); the status description is the code, never the message |
| A2A JSON-RPC error response (including streamed error frames) | status `ERROR`, `error.type` = `jsonrpc.<code>` (e.g. `jsonrpc.-32001`) |
| Client disconnect, WS close, or `cancel` frame mid-turn | status `UNSET`, `prismal.outcome=cancelled` |
| Unhandled exception | status `ERROR`, `error.type` = the mapped host code (`internal_error`, the same code the client sees) |

**Duration.** The span starts when the handler accepts the request and ends when the
terminal event is sent, not when the handler returns the `StreamingResponse`.

## Engine spans

The engine's own OTel (`PRISMAL_OTEL_ENABLED`, configured by the engine and not by
this host) uses the global tracer provider. The host never sets the global
provider. Engine spans started during a turn become children of the host's root
span through the OTel context, so they land in the same trace.

agentlens reads token usage from an agent span **only when the trace has no LLM
spans**. Once the engine emits GenAI LLM spans, their usage takes over and nothing
is counted twice.

## What is never exported

The following never go on a span, a span event, or a log line: message content,
tool arguments, exception messages or stacks, and secrets (including the
`HOST_OTEL_HEADERS` values). Tokens come from the engine's usage numbers. The
host never estimates them.
