# prismal-server — Design (Host v0.3: per-request traces for agentlens)

| Field | Value |
|---|---|
| **Status** | `APPROVED` |
| **Version** | 1.0 |
| **Date** | 2026-11-02 |
| **Author** | Ernesto Crespo |
| **Integration** | Cross-project map #2 "OTel → agentlens" (`reflex-agentlens/docs/integration-prompts.md`) |

## 1. Intent

The operator wants the **agentlens dashboard** to answer three questions about
prismal-server traffic:

1. **How many tokens each request consumes.**
2. **How many errors there are**, and which kind.
3. **How long each request takes.**

agentlens already answers exactly these per **run**, and a run is an OTLP **trace**.
It sums tokens from GenAI spans, flags error spans, takes the duration from the root
span, and groups runs into sessions by `gen_ai.conversation.id`. What is missing is
a **root span per request** from the host. This design adds one.

## 2. Findings that shape the design

- **agentlens** (`reflex-agentlens`) ingests OTLP/HTTP `POST /v1/traces` (and
  `/v1/metrics`). It authenticates with `X-Agentlens-Key` (or `Authorization: Bearer`).
  Its dashboard charts only GenAI token metrics, so host counters exported as metrics
  would be stored but never shown. **Traces, not metrics, are the right signal.**
- agentlens computes run tokens from `llm` spans. **An agent span's own usage**
  (`gen_ai.operation.name=invoke_agent` + `gen_ai.usage.*`) **is used only when the
  trace has no LLM spans** (`ingest/normalize/llm.py`, DD-012). So a host agent span
  carrying the turn's usage gives correct totals today, and stays correct without
  double counting once the engine emits LLM spans.
- The **engine emits no `gen_ai.*` spans yet**. Its LLM span is
  `provider.get_llm`, opened when the model is constructed, with no tokens. The engine
  prompt I2-A (GenAI semconv on every span) is not implemented. When the engine's OTel
  is enabled, its spans use `start_as_current_span`, so they nest under whatever span is
  current in the context.
- The host already holds the turn's usage: `done.usage` is the engine's
  `get_run_summary(...).to_usage_wire()`. Copying those numbers onto a span is
  "persist the engine's record", not metering (SPEC-RH2-NFR-003).
- `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` 1.43 are already
  installed, as transitive engine dependencies.

## 3. Goals, non-goals, anti-goals

**Goals**
- One root span per **agent request**: native SSE turn, AG-UI turn (both of today's
  entry points: `?protocol=ag-ui` / `Accept` profile and `POST /v1/agui`), each WebSocket
  turn, and each **message-sending** A2A RPC on `POST /a2a` or a tenant route
  (`SendMessage`, `SendStreamingMessage`, legacy `message/send`, `message/stream`).
- On it: tokens (from `done.usage`), outcome (ok, or an error with the host error code),
  and wall-clock duration from accepting the request to sending its terminal event.
- Engine spans (when the engine's OTel is on) nest under it, in the same trace.
- Export over OTLP/HTTP to a configurable endpoint (agentlens), **off by default**.

**Non-goals**
- No host metrics export (`/metrics`, OTLP metrics). The in-process counters stay as
  they are.
- No spans for non-agent routes (`/healthz`, `/readyz`, `/v1/usage`, `/v1/budget`,
  `/v1/admin/*`, the Agent Card), nor for A2A task-management RPCs (`GetTask`,
  `ListTasks`, `CancelTask`, `SubscribeToTask`, push-config and extended-card methods, and
  their legacy aliases). They are not runs: clients poll them, and they would clutter
  the run list and skew the counts.
- No tokens on A2A runs in this version. The engine's A2A path returns raw JSON-RPC
  without a `done.usage`, and finding its run summary would mean copying the handler's
  private thread-key formula. A2A turns were not in `/v1/usage` in 0.2 either. A2A runs
  get tokens once the engine emits GenAI LLM spans (engine I2-A).
- Per-tenant trace destinations. **All tenants go to one operator-wide agentlens
  project** (decided 2026-11-02), filterable by `prismal.org_id`. `tenant.yaml` gains no
  OTel keys.
- No changes to agentlens, and no changes to the engine (I2-A stays the engine's job).
- No trace-context propagation from clients (`traceparent` in) in this version.

**Anti-goals** (failure even if it "works")
- Any prompt, completion, message content, tool argument or secret in a span.
- Tokens counted twice once the engine emits LLM spans.
- Tracing on by default, or a broken or unreachable collector slowing down or failing
  requests.
- Any new `prismal.*` import (the HRS guard stays as it is).

## 4. Decided

| # | Decision |
|---|---|
| D1 | Signal = **traces**; the span kind agentlens reads is a GenAI **agent** span. |
| D2 | Tokens come **only** from `done.usage` (engine numbers), never computed. |
| D3 | Off by default; one host flag turns it on. |
| D4 | No content capture. |
| D5 | One operator-wide agentlens project for every tenant (one endpoint, one key). |

## 5. Design

### 5.1 Settings (`HostSettings`, prefix `PRISMAL_SERVER_`)

| Key | Default | Meaning |
|---|---|---|
| `host_otel_enabled` | `false` | Turn host tracing on |
| `host_otel_endpoint` | `http://localhost:4318` | OTLP/HTTP base URL; spans go to `{endpoint}/v1/traces` |
| `host_otel_headers` | `{}` | `dict[str, SecretStr]`, e.g. `{"X-Agentlens-Key": "…"}`; never logged or returned |
| `host_otel_service_name` | `prismal-server` | `service.name` resource attribute |
| `host_otel_agent_name` | `prismal` | `gen_ai.agent.name` on the root span |

Resource: `service.name`, `service.version` (= `prismal_server.__version__`) and
`deployment.environment.name` (= `host_environment`).

### 5.2 Tracer lifecycle (`prismal_server/tracing.py`)

- At app startup (lifespan), when enabled: build a **host-owned** `TracerProvider` with
  a `BatchSpanProcessor(OTLPSpanExporter(...))`. Keep it on `app.state` and get the
  tracer from it.
  - **Do not set the global provider.** The engine's `OTelManager` sets its own, and the
    two must not fight.
  - Parent/child linking works through the OTel *context* (contextvars), which is
    independent of the provider.
- When disabled, a no-op tracer is used and there is zero export work.
- At shutdown: `provider.shutdown()` in a worker thread (`asyncio.to_thread`), because it
  blocks while flushing. A dead collector must never hang or crash shutdown: bound it
  with the exporter timeout and log on failure.
- Export is asynchronous (the batch processor's background thread). Requests never wait
  on the collector. Exporter errors are logged and dropped.

### 5.3 The root span

Name `invoke_agent {host_otel_agent_name}`, kind `SERVER`.

| Attribute | Value |
|---|---|
| `gen_ai.operation.name` | `invoke_agent` |
| `gen_ai.agent.name` | `host_otel_agent_name` |
| `gen_ai.conversation.id` | `thread_id`, or `"{org_id}/{thread_id}"` when an org is resolved (two tenants with the same `thread_id` must not merge into one agentlens session). For A2A it is the message's `contextId` from the request params, with the same `"{org_id}/"` prefix, and it is omitted when absent |
| `prismal.org_id` | resolved org, when present |
| `prismal.transport` | `sse` · `ws` · `a2a` |
| `prismal.protocol` | `native` · `ag-ui` · `a2a-0.3` · `a2a-1.0`. For A2A it is taken from the **method family**, which is the engine's own negotiation rule (VERIFY-RH2-00): a legacy method name means `a2a-0.3`, and a v1 PascalCase method means `a2a-1.0` |
| `prismal.a2a.method` | the JSON-RPC method (A2A only) |
| `http.route` | route template, e.g. `/threads/{thread_id}/messages` |
| `prismal.finish_reason` | `done.finish_reason`, when there is one |
| `gen_ai.response.model` | `usage.served_model`, when not null |
| `gen_ai.usage.input_tokens` | `usage.prompt_tokens` |
| `gen_ai.usage.output_tokens` | `usage.completion_tokens` |
| `gen_ai.usage.cache_read.input_tokens` | `usage.cache_read_tokens` |
| `gen_ai.usage.cache_write.input_tokens` | `usage.cache_write_tokens` |
| `gen_ai.usage.reasoning.output_tokens` | `usage.reasoning_tokens` |
| `prismal.usage.calls` · `prismal.usage.cost_usd` · `prismal.usage.cost_source` · `prismal.usage.estimated` · `prismal.gateway` | remaining usage-wire keys (namespaced, never a `gen_ai.*` key outside the spec) |

Usage attributes are set only when the terminal event carries usage: a successful
`done`, or the partial usage of a mid-stream `budget_exceeded`. When `usage` is `null`
they are omitted, never written as zero.

**Outcome**

| Case | Span |
|---|---|
| Turn completes (`done`, `RUN_FINISHED`, A2A result) | status `OK` |
| Host error, before or during the stream (`budget_exceeded`, `forbidden_tenant`, `protocol_not_enabled`, `invalid_thread_id`, `invalid_input`, `internal_error`, …) | status `ERROR`, `error.type` = the host error code, status description = the code (never the message) |
| A2A JSON-RPC error response | status `ERROR`, `error.type` = `jsonrpc.<code>` (e.g. `jsonrpc.-32001`) |
| Client disconnect / WS close / cancel frame mid-turn | status `UNSET`, `prismal.outcome=cancelled` |
| Unhandled exception | status `ERROR`, `error.type` = the mapped host code (`internal_error`, the code the client sees), no exception event, message or stack recorded (they can contain content) |

**Duration.** The span starts when the route handler accepts the request, so refusals
inside the handler (402, 403, 406, 422 from host checks) are traced. It ends when the
**terminal event is sent**, not when the handler returns the `StreamingResponse`. For
WebSocket there is one span per turn, from the `message` frame to the terminal frame.
Requests that FastAPI rejects before the handler runs (body validation `422`) and auth
`401`s are not traced.

**Nesting.** Only the per-turn graph construction and its chunk pump task, and the A2A handler
call, are started while the root span is current (never the runtime lease, whose
runtime build may start long-lived engine tasks), so the contextvars copied into the task carry it, and engine
spans become its children. An engine with its OTel off simply contributes no children.

### 5.4 Where it hooks in

One helper, `tracing.turn_span(request_ctx, …)`, is used by the shared turn path. It is
wrapped around `routes/threads.open_turn` plus the `Turn` lifecycle (native and AG-UI,
for SSE and WS alike). In `routes/a2a._dispatch` it is opened only when
`payload["method"]` is one of the four message-sending methods. The method names come
from the engine's `prismal.a2a.v1.constants` (`METHOD_SEND_MESSAGE` and
`LEGACY_METHOD_SEND` join the existing `HOST_EXCEPTIONS` entry in the import guard;
there are no host-side protocol strings). It is not repeated per route.
The SSE generators already know the terminal event, and that is where the span is
finished.

### 5.5 Boundary

No `prismal` import. The host uses the `opentelemetry` API and SDK directly. Add
`opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` as **explicit** host
dependencies; they are already resolved transitively, so this adds nothing new. The
engine pin is unchanged.

## 6. Testing

- **Unit** (`InMemorySpanExporter`, no network):
  - one span per turn on each transport, with the attribute table above;
  - the error code on `402`, `403`, `406`, `422` and a mid-stream error;
  - `cancelled` on disconnect;
  - no span on health, usage and admin routes;
  - usage omitted when `null`;
  - a secret canary in `host_otel_headers` and the message content never appear in
    any span or log;
  - the A2A JSON-RPC error mapping;
  - tracing disabled ⇒ no provider and no export.
- **Nesting**: a fake graph that opens its own child span under the current context ⇒
  the same trace id and the root as parent.
- **Shutdown**: an unreachable endpoint does not hang lifespan shutdown (bounded).
- **Contract** (`-m contract`, fake-LLM mode): a real OTLP/HTTP exporter pointed at an
  in-test receiver that decodes the request. Asserts the root span shape over the wire.
- **Cross-repo check (follow-up, not blocking)**: export one fake-mode trace as OTLP JSON
  into `tests/fixtures/otel/`, so agentlens can replay it in its own contract suite.

## 7. Left to the builder

Module layout inside `tracing.py`; how the terminal-event hook is threaded through the
existing generators; exporter timeout values; exact log event names.

## 8. Calls made (open for review)

From the builder check (2026-11-02):
- Only the message-sending A2A methods are runs (§3).
- A2A runs carry no tokens until engine I2-A; their session is `contextId`.
- A2A protocol label comes from the method family, not from header guessing.
- This lands **before I1-B**, on today's two AG-UI entry points. How an interrupted run
  (waiting on client-tool results) is marked is decided in the I1-B design. The
  proposal there: `OK` with `prismal.outcome=interrupted`, since it is not a failure.
- D5 (one agentlens project for all tenants) was decided by the operator.

Earlier calls:

- Non-agent routes are not traced.
- `gen_ai.conversation.id` = `"{org}/{thread}"` when multi-tenant.
- A host-owned provider, not the global one.
- Cancelled turns are `UNSET` rather than `ERROR`, so client disconnects don't inflate
  the error count.
- Body-validation `422`s and auth `401`s are not traced.
- Release target: host **v0.3.0**, together with I1-B.
