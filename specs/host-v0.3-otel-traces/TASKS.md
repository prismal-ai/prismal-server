# Host v0.3 — Per-request OTel traces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit one OTLP root span per agent request (SSE, AG-UI, WebSocket turn, message-sending A2A RPC), carrying tokens, outcome and duration, so agentlens shows them per run.

**Architecture:** A new `prismal_server/tracing.py` owns a host-only `TracerProvider` (never the global one) and a small `TurnSpan` wrapper. The shared turn path (`run_turn` / `open_turn` / `native_events` / `agui_events`), the WebSocket turn and `a2a._dispatch` open a `TurnSpan`, make it current while the graph pump task / A2A handler is created (so engine spans nest by context), and finish it from the terminal event.

**Tech Stack:** Python 3.13, FastAPI, `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http` 1.43, pytest (`InMemorySpanExporter`).

**Spec:** [`DESIGN.md`](./DESIGN.md) (approved 2026-11-02).

**Branch / PR:** `feat/rh3-1-otel-traces` from `develop`, one PR → `develop`, merge on green CI (same phase workflow as RH2).

## Global Constraints

- Off by default: `host_otel_enabled=false`; disabled ⇒ no `TracerProvider`, no exporter constructed, no spans.
- No `prismal` import added except `METHOD_SEND_MESSAGE` / `LEGACY_METHOD_SEND` from `prismal.a2a.v1.constants` (guard `HOST_EXCEPTIONS`).
- Never put message content, tool args, exception messages/stacks or secrets on a span; status description = error **code** only.
- Tokens only from the turn's `UsageWire` (`done.usage` / budget partial usage); `null` usage ⇒ usage attributes omitted, never zero.
- Do not call `trace.set_tracer_provider` (the engine owns the global provider).
- Fully async: provider shutdown runs in `asyncio.to_thread`, bounded (`asyncio.wait_for`, 10 s); export never blocks a request.
- `host_otel_headers` are `dict[str, SecretStr]`, never logged or returned.
- Engine pin unchanged (`prismal-ai>=3.17,<4`); new explicit deps `opentelemetry-sdk>=1.43,<2`, `opentelemetry-exporter-otlp-proto-http>=1.43,<2`.
- Every task: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -m "not contract"` green before commit.

## Review Focus

1. **Collector down / slow** — requests complete normally and lifespan shutdown returns within the bound (Task 1 tests `test_export_failure_never_breaks_a_turn`, `test_shutdown_is_bounded_when_exporter_hangs`).
2. **Client disconnects mid-SSE** — span ends exactly once, status `UNSET`, `prismal.outcome=cancelled`, not counted as an error (Task 2 `test_disconnect_marks_cancelled`).
3. **Concurrent turns** — two simultaneous turns get distinct trace ids and never swap `gen_ai.conversation.id` / usage (Task 2 `test_concurrent_turns_do_not_bleed`).
4. **Secret / content canary** — a canary in `host_otel_headers` and in the user message never appears in any exported span or log record (Task 2 `test_no_content_or_secret_in_spans`).
5. **Disabled tracing is inert** — default settings create no provider and the existing suite is unchanged (Task 1 `test_disabled_creates_nothing`).

---

### Task 1: Settings, `tracing.py` core and app lifecycle

**Files:**
- Modify: `pyproject.toml` (deps), `src/prismal_server/config.py` (settings), `src/prismal_server/app.py` (state + lifespan + `create_app(tracing=)`)
- Create: `src/prismal_server/tracing.py`
- Test: `tests/unit/test_tracing.py`

**Interfaces:**
- Produces (`prismal_server.tracing`):
  - `Transport = Literal["sse", "ws", "a2a"]`
  - `def conversation_id(org_id: str | None, local_id: str | None) -> str | None` — `None` if `local_id` is `None`; `local_id` when `org_id` is `None`; else `f"{org_id}/{local_id}"`.
  - `class Tracing`: `__init__(self, settings: HostSettings, *, exporter: SpanExporter | None = None)`; `enabled: bool` (true iff `settings.host_otel_enabled` or `exporter` given); `turn(self, *, route: str, transport: Transport, protocol: str, a2a_method: str | None = None) -> TurnSpan`; `async aclose(self) -> None`. Injected exporter ⇒ `SimpleSpanProcessor`; otherwise `BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers={k: v.get_secret_value()}, timeout=5))`.
  - `class TurnSpan`: `activate(self) -> AbstractContextManager[None]` (makes the span current, does not end it); `set_tenant(self, org_id: str | None, conversation: str | None) -> None`; `record_usage(self, usage: UsageWire | None) -> None`; `complete(self, finish_reason: str | None = None) -> None`; `fail(self, code: str) -> None`; `end(self) -> None` (idempotent; if neither `complete` nor `fail` was called ⇒ status UNSET + `prismal.outcome=cancelled`). Disabled `Tracing` returns a `TurnSpan` over a non-recording span (same API, no-op).
  - `app.state.tracing: Tracing`; `create_app(..., tracing: Tracing | None = None)`; lifespan `finally` calls `await tracing.aclose()`.
- Settings (exact): `host_otel_enabled: bool = False`, `host_otel_endpoint: str = "http://localhost:4318"`, `host_otel_headers: dict[str, SecretStr] = {}`, `host_otel_service_name: str = "prismal-server"`, `host_otel_agent_name: str = "prismal"`.
- Span: name `f"invoke_agent {host_otel_agent_name}"`, kind `SERVER`; start attributes `gen_ai.operation.name="invoke_agent"`, `gen_ai.agent.name`, `http.route`, `prismal.transport`, `prismal.protocol`, `prismal.a2a.method` (when given). Resource: `service.name`, `service.version=__version__`, `deployment.environment.name=host_environment`.
- Usage mapping in `record_usage`: `gen_ai.usage.input_tokens←prompt_tokens`, `gen_ai.usage.output_tokens←completion_tokens`, `gen_ai.usage.cache_read.input_tokens←cache_read_tokens`, `gen_ai.usage.cache_write.input_tokens←cache_write_tokens`, `gen_ai.usage.reasoning.output_tokens←reasoning_tokens`, `prismal.usage.calls`, `prismal.usage.cost_usd`, `prismal.usage.cost_source`, `prismal.usage.estimated`, `gen_ai.response.model←served_model` and `prismal.gateway←gateway` (each only when not `None`).
- Outcome: `complete` ⇒ status OK (+ `prismal.finish_reason`); `fail(code)` ⇒ status ERROR with description `code`, `error.type=code`.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_tracing.py` (fixture: `Tracing(HostSettings(), exporter=InMemorySpanExporter())`):
  - `test_disabled_creates_nothing` — `Tracing(HostSettings())` ⇒ `enabled is False`, `turn(...)` usable, no `TracerProvider` built (assert `tracing._provider is None`), `OTLPSpanExporter` never constructed (monkeypatch it to raise).
  - `test_completed_turn_span_shape` — `turn(route="/threads/{thread_id}/messages", transport="sse", protocol="native")`, `set_tenant("acme", conversation_id("acme","t-1"))`, `record_usage(<fixture UsageWire>)`, `complete("stop")`, `end()` ⇒ one span, name `"invoke_agent prismal"`, kind SERVER, status OK, attributes exactly per the mapping (`gen_ai.conversation.id == "acme/t-1"`, `gen_ai.usage.input_tokens == 1200`, …), resource `service.name == "prismal-server"`.
  - `test_null_usage_sets_no_usage_attributes` — `record_usage(None)` ⇒ no key starting with `gen_ai.usage.` or `prismal.usage.`.
  - `test_fail_sets_error_code_only` — `fail("budget_exceeded")` ⇒ status ERROR, description `"budget_exceeded"`, `error.type == "budget_exceeded"`, no events recorded.
  - `test_end_without_outcome_is_cancelled` and `test_end_is_idempotent` (second `end()` exports nothing more).
  - `test_conversation_id` — `(None,"t")→"t"`, `("acme","t")→"acme/t"`, `("acme",None)→None`.
  - `test_child_span_nests_under_activated_turn` — inside `activate()`, a span from an **independent** `TracerProvider` (`provider2.get_tracer("x").start_as_current_span("child")`) has the turn's trace id and parent span id.
  - `test_export_failure_never_breaks_a_turn` — exporter whose `export` raises ⇒ `complete`/`end` don't raise.
  - `test_shutdown_is_bounded_when_exporter_hangs` — exporter whose `shutdown` sleeps 30 s; patch the bound to 0.2 s ⇒ `await tracing.aclose()` returns in < 2 s.
  - `test_global_provider_untouched` — `trace.get_tracer_provider()` is the same object before and after building an enabled `Tracing`.
  - `test_lifespan_closes_tracing` — `create_app(tracing=spy)` under `lifespan_context` ⇒ `spy.aclose` awaited once.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_tracing.py -q` — Expected: FAIL (`ModuleNotFoundError: prismal_server.tracing`).
- [ ] **Step 3: Implement** settings, deps, `tracing.py` and the app wiring per the Interfaces block (`uv pip install -e ".[dev]"` after editing `pyproject.toml`).
- [ ] **Step 4: Run** `uv run pytest -q -m "not contract"` — Expected: all pass (existing suite unchanged).
- [ ] **Step 5: Commit** — `git commit -m "Add host OTel tracing core (RH3-01-01)"`.

### Task 2: Spans on HTTP turns (native SSE + AG-UI)

**Files:**
- Modify: `src/prismal_server/streaming.py` (`native_events`, `agui_events`, `sse_stream`, `agui_stream` gain `span: TurnSpan | None = None`), `src/prismal_server/routes/threads.py` (`Turn` carries `span`; `open_turn(..., span: TurnSpan | None = None)`; `run_turn`)
- Test: `tests/unit/test_tracing_turns.py`

**Interfaces:**
- Consumes: Task 1 `Tracing.turn`, `TurnSpan.*`, `conversation_id`.
- Produces: `open_turn(state, *, org_id, thread_id, role, content, agui, run_id=None, pump_maxsize=0, span: TurnSpan | None = None) -> Turn`; `Turn.span: TurnSpan | None`; the generators finish the span: on `"error"` ⇒ `span.record_usage(partial)` (budget only) then `span.fail(mapped.code)`; on `"end"` ⇒ `span.record_usage(usage)` then `span.complete(finish_reason)`; `finally` ⇒ `span.end()`.
- `run_turn` behaviour: after `authenticate` (401 stays untraced), `span = state.tracing.turn(route=<route template>, transport="sse", protocol="ag-ui" if agui else "native")`; `resolve_org_id` → `span.set_tenant(org_id, conversation_id(org_id, thread_id))`; `open_turn` is awaited inside `with span.activate():` (the `ChunkPump` task copies that context). Any exception before the `StreamingResponse` is returned (`forbidden_tenant`, `invalid_thread_id`, `protocol_not_enabled`, primed `budget_exceeded`, …) ⇒ `span.fail(map_exception(exc).code)`, `span.end()`, re-raise. `POST /v1/agui` passes `route="/v1/agui"`; its `invalid_input` raised before `run_turn` is traced by opening the span in the route first (pass it into `run_turn(span=...)`).

- [ ] **Step 1: Write failing tests** (app built with `create_app(tracing=Tracing(settings, exporter=mem))`, fake graph from `prismal_server.fake.FakeGraph` and `build_test_runtime` registry as in `tests/unit/fake/test_fake_mode.py`):
  - `test_sse_turn_span_has_usage_and_ok` — one span, OK, `gen_ai.usage.input_tokens == 10`, `gen_ai.usage.output_tokens == 5`, `gen_ai.response.model == "fake/echo"`, `prismal.finish_reason == "stop"`, `http.route == "/threads/{thread_id}/messages"`.
  - `test_agui_turn_span_protocol` — `?protocol=ag-ui` and `POST /v1/agui` each ⇒ `prismal.protocol == "ag-ui"`, OK, usage set.
  - `test_midstream_error_span` — content `/error` ⇒ ERROR, `error.type == "internal_error"`.
  - `test_pre_stream_refusals_are_traced` — parametrize: `402 budget_exceeded` (graph raising the engine `BudgetExceeded` name first), `406 protocol_not_enabled`, `422 invalid_thread_id` (`"12:x"` single-tenant), `403 forbidden_tenant`, `422 invalid_input` (`/v1/agui` without user text) ⇒ one span each, ERROR, matching `error.type`.
  - `test_disconnect_marks_cancelled` — close the response after the first token ⇒ span status UNSET, `prismal.outcome == "cancelled"`, exactly one span.
  - `test_concurrent_turns_do_not_bleed` — two turns (`t-a`, `t-b`) via `asyncio.gather` ⇒ two spans, distinct trace ids, each `gen_ai.conversation.id` equals its own thread.
  - `test_no_content_or_secret_in_spans` — `host_otel_headers={"X-Agentlens-Key": "CANARY-SECRET"}`, message `"CANARY-CONTENT"` ⇒ neither string in `repr` of any exported span's attributes/events/status nor in `caplog.text`.
  - `test_engine_span_nests_under_turn` — graph that opens `start_as_current_span("engine.child")` from a separate provider inside `astream` ⇒ child shares the root's trace id, parent = root.
  - `test_untraced_routes` — `/healthz`, `/readyz`, `/v1/usage`, `/v1/budget` ⇒ zero spans.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_tracing_turns.py -q` — Expected: FAIL (no spans exported).
- [ ] **Step 3: Implement** the Interfaces block.
- [ ] **Step 4: Run** `uv run pytest -q -m "not contract"` — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "Trace HTTP chat turns (RH3-01-02)"`.

### Task 3: Spans on WebSocket turns

**Files:**
- Modify: `src/prismal_server/routes/ws.py` (`_stream_turn`)
- Test: `tests/unit/routes/test_ws_tracing.py`

**Interfaces:**
- Consumes: Task 1, Task 2 `open_turn(..., span=)`.
- Behaviour: per `message` frame, `span = state.tracing.turn(route="/ws/threads/{thread_id}", transport="ws", protocol=frame.protocol)`, `set_tenant(self._org_id, conversation_id(self._org_id, self._thread_id))`; `open_turn` inside `span.activate()`; `open_turn` failure ⇒ `span.fail(code)`, `span.end()` before `_send_error`; the generators finish the span (Task 2); `cancel` frame / disconnect ⇒ cancelled via `end()`.

- [ ] **Step 1: Write failing tests**: `test_ws_turn_one_span_per_turn` (two turns on one connection ⇒ two spans, same `gen_ai.conversation.id`, distinct trace ids, `prismal.transport == "ws"`); `test_ws_cancel_frame_marks_cancelled`; `test_ws_turn_start_failure_is_error` (`protocol":"ag-ui"` with engine flag off ⇒ ERROR `protocol_not_enabled`); `test_ws_handshake_refusal_not_traced` (bad `Origin` ⇒ zero spans).
- [ ] **Step 2: Run** `uv run pytest tests/unit/routes/test_ws_tracing.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q -m "not contract"` — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "Trace WebSocket turns (RH3-01-03)"`.

### Task 4: Spans on message-sending A2A RPCs

**Files:**
- Modify: `src/prismal_server/routes/a2a.py` (`_dispatch`), `tests/unit/test_import_guard.py` (`HOST_EXCEPTIONS["prismal.a2a.v1.constants"]` += `METHOD_SEND_MESSAGE`, `LEGACY_METHOD_SEND`)
- Test: `tests/unit/routes/test_a2a_tracing.py`

**Interfaces:**
- Consumes: Task 1.
- `RUN_METHODS = {METHOD_SEND_MESSAGE, METHOD_SEND_STREAMING_MESSAGE}` → protocol `"a2a-1.0"`; `{LEGACY_METHOD_SEND, LEGACY_METHOD_STREAM}` → `"a2a-0.3"`. Any other method ⇒ no span.
- Span: `route` = the matched route template (`/a2a` or `{base}/{tenant_id}`), `transport="a2a"`, `a2a_method=payload["method"]`, `set_tenant(org_id, conversation_id(org_id, params.message.contextId))` (context id read defensively; absent ⇒ `None`). Handler call / stream creation inside `span.activate()`.
- Outcome: non-stream result with `"error"` ⇒ `fail(f"jsonrpc.{code}")`, else `complete()`; handler exception ⇒ `fail(type(exc).__name__)` then re-raise; stream: wrap `stream_rpc(...)` in an async generator that inspects each `data:` line (JSON-decode, ignore failures) — first JSON-RPC `error` member ⇒ `fail(f"jsonrpc.{code}")`; normal exhaustion ⇒ `complete()` unless failed; `finally` ⇒ `end()`. No usage attributes on A2A spans.

- [ ] **Step 1: Write failing tests** (fake `a2a_handler` like `tests/unit/routes/test_a2a_v1.py`): `test_send_message_is_one_span` (v1 ⇒ `prismal.protocol == "a2a-1.0"`, `prismal.a2a.method == "SendMessage"`, OK, no `gen_ai.usage.*`); `test_legacy_send_protocol` (`message/send` ⇒ `"a2a-0.3"`); `test_task_management_methods_not_traced` (parametrize `GetTask`, `ListTasks`, `CancelTask`, `tasks/get` ⇒ zero spans); `test_jsonrpc_error_marks_span` (handler returns `{"error":{"code":-32001,…}}` ⇒ ERROR, `error.type == "jsonrpc.-32001"`); `test_streaming_send_span` (stream with an error frame ⇒ ERROR; clean stream ⇒ OK); `test_a2a_conversation_is_context_id` (`contextId "c-9"`, org `acme` ⇒ `"acme/c-9"`).
- [ ] **Step 2: Run** `uv run pytest tests/unit/routes/test_a2a_tracing.py tests/unit/test_import_guard.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q -m "not contract"` — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "Trace message-sending A2A RPCs (RH3-01-04)"`.

### Task 5: Wire-level contract, docs and release notes

**Files:**
- Create: `tests/contract/test_otel_contract.py`, `docs/tracing.md`
- Modify: `docs/running.md` (config table rows for the five settings), `README.md` (docs table row), `CHANGELOG.md` (`## [Unreleased]` → Added), this file (statuses)

**Interfaces:**
- Consumes: Tasks 1–4.

- [ ] **Step 1: Write the contract test** (`pytestmark = pytest.mark.contract`): start a stdlib `ThreadingHTTPServer` on `127.0.0.1:0` whose `POST /v1/traces` handler decodes the body with `opentelemetry.proto.collector.trace.v1.trace_service_pb2.ExportTraceServiceRequest` and records it plus the request headers; build the fake-mode app with `HostSettings(host_fake_llm_mode=True, host_otel_enabled=True, host_otel_endpoint=f"http://127.0.0.1:{port}", host_otel_headers={"X-Agentlens-Key": "k-test"})`; run one SSE turn; `await app.state.tracing.aclose()` (flush). Assert: one received span named `invoke_agent prismal`, `gen_ai.usage.input_tokens == 10`, `gen_ai.conversation.id` equals the thread, resource `service.name == "prismal-server"`, request header `x-agentlens-key == "k-test"`.
- [ ] **Step 2: Run** `uv run pytest -m contract tests/contract/test_otel_contract.py -q` — Expected: PASS (Tasks 1–4 done; if FAIL, fix the wiring, not the test).
- [ ] **Step 3: Write `docs/tracing.md`**: purpose (agentlens per-request runs), the five settings with an agentlens example (`PRISMAL_SERVER_HOST_OTEL_ENABLED=true`, `…_ENDPOINT=http://agentlens:8000/otlp` style placeholder, `…_HEADERS='{"X-Agentlens-Key":"<key>"}'`), which requests are traced and which are not, the attribute table and outcome table from DESIGN §5.3, the A2A no-tokens note, engine nesting (`PRISMAL_OTEL_ENABLED`), and "no content is ever exported". Add the config rows, README row and CHANGELOG entry.
- [ ] **Step 4: Run** the full gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -m "not contract" -q && uv run pytest -m contract -q` — Expected: all green (contract: 1 skipped without LLM credential).
- [ ] **Step 5: Commit** — `git commit -m "OTel traces contract test, docs and changelog (RH3-01-05)"`; push, PR → `develop`, merge on green CI.

## Status

| ID | Task | Status |
|---|---|---|
| RH3-01-01 | Settings, tracing core, lifecycle | `TODO` |
| RH3-01-02 | HTTP turns (native + AG-UI) | `TODO` |
| RH3-01-03 | WebSocket turns | `TODO` |
| RH3-01-04 | A2A message-sending RPCs | `TODO` |
| RH3-01-05 | Wire contract, docs, changelog | `TODO` |
