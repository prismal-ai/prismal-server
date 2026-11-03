# Changelog

All notable changes to `prismal-server`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/).

## [Unreleased]

## [0.3.0] — 2026-11-02

Host v0.3: per-request traces, AG-UI interrupt resume and engine 3.22.1. Specs:
[`specs/host-v0.3-otel-traces/`](./specs/host-v0.3-otel-traces/),
[`specs/host-v0.3-agui-resume/`](./specs/host-v0.3-agui-resume/).
Engine pin: `prismal-ai>=3.22.1,<4` (verified on 3.22.1).

### Added

- **Per-request OpenTelemetry traces** (RH3-01, spec
  [`specs/host-v0.3-otel-traces/`](./specs/host-v0.3-otel-traces/)). Off by default
  (`PRISMAL_SERVER_HOST_OTEL_ENABLED`). Each SSE, AG-UI and WebSocket turn and each
  message-sending A2A RPC exports one GenAI `invoke_agent` root span over OTLP/HTTP.
  The span carries token usage from `done.usage`, the outcome (`error.type` holds the
  host error code) and the duration up to the terminal event. Engine spans nest under
  it. No content and no secrets are exported. See [`docs/tracing.md`](./docs/tracing.md).
- Explicit dependencies `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http`
  (`>=1.43,<2`; both were already resolved transitively).

- **AG-UI interrupt resume** (RH3-02, spec
  [`specs/host-v0.3-agui-resume/`](./specs/host-v0.3-agui-resume/)). `POST /v1/agui`
  accepts AG-UI 1.0 `RunAgentInput.resume` (`ResumeEntry[]`) and resumes the thread's
  pending interrupts with `Command(resume={interruptId: payload})`.
  - Interrupt ids are opaque, `"<engine id>.<checkpoint_id>"`. LangGraph reuses one id
    across a node's interrupts, so the checkpoint suffix stops a retried answer from
    approving the next prompt.
  - Resume runs are not cancelled on client disconnect. The approved step finishes in
    the background.
  - New refusals: `409 no_pending_interrupt` (this includes retries), `409 resume_in_progress`,
    `422 invalid_resume`, `422 unsupported_resume` (`cancelled`).
  - Fake mode gains a `/interrupt <message>` script.
  - A replayable fixture is published at `fixtures/agui/interrupt-resume.json`.
  - Frontend tools await the engine (`ENGINE-PROMPT.md`).
- Explicit dependency `langgraph>=1.0,<2` (already resolved through the engine).
- **Tenant key `decision_ollama_timeout_ms_by_tier`** (H-322, prismal-ai 3.22). The
  per-sub-tier (`fast` | `accurate` → ms) sibling of the already tenant-tunable
  `decision_ollama_timeout_ms` joins `ALLOWED_KEYS`. JSON-encode the map in
  `tenant.yaml`; the engine rejects unknown tiers and non-positive budgets
  (`500 tenant_config_invalid`).

### Fixed

- **The process exits cleanly on shutdown** (H-320). The lifespan now closes the
  engine's cached graph checkpointer with `aclose_async_compiled_graph()` after the
  tenant runtimes, so no non-daemon `aiosqlite` thread keeps the interpreter alive
  (no more SIGKILL). A failing close is logged as a warning and shutdown still
  completes; fake-LLM mode skips it. The opt-in contract suite therefore exits on its
  own, and its session-end workaround (`tests/contract/conftest.py`) is removed.
  - prismal-ai 3.20.0–3.22.0 dropped the cached graph *before* closing its
    connections, so they were garbage-collected mid-close and the close hung. 3.22.1
    closes first, so the host's interim graph hold (`ENGINE_GRAPHS` /
    `hold_engine_graph`) is removed (H-3221).

### Changed

- **Engine floor raised to `prismal-ai>=3.22.1,<4`** (H-320, H-322, H-3221). 3.20 adds
  `aclose_async_compiled_graph` (execution seam). 3.22 adds the per-tier Ollama
  budget above, hardens the engine's typed-decision prompt-injection screen and
  raises transitive floors (`litellm!=1.107.0,>=1.100.4`, `mcp>=1.30,<2`,
  `pypdf>=6.21.4`, `setuptools>=84.1`, `unstructured>=0.24.1`). 3.22.1 fixes the
  graph-close ordering (above). No seam changed.
- `POST /v1/agui` validates the run input after authentication. In strict auth mode, an
  unauthenticated request with an invalid body now gets `401` instead of `422`.

## [0.2.0] — 2026-11-02

Host v0.2: usage, gateways and AG-UI. Spec:
[`specs/host-v0.2-usage-gateways-agui/`](./specs/host-v0.2-usage-gateways-agui/).
Engine pin: `prismal-ai>=3.17,<4` (verified on 3.19.0; 3.17.0 was never published).

### ⚠️ Upgrade notes

- **Multi-tenant thread continuity resets.** The engine now receives an
  org-scoped thread key `f"{len(org)}:{org}:{thread_id}"` (SPEC-RH2-THR-001), so
  conversation state and run summaries can no longer cross tenants. In
  multi-tenant 0.1 deployments, threads started before the upgrade do not resume.
  Clients still see their own `thread_id`. Single-tenant deployments keep raw ids,
  but a raw id shaped `^\d+:` is now refused with `422 invalid_thread_id`.
- **`budget_exceeded` is `402`, no longer `429`.** A budget refusal before the first
  token is now an HTTP `402` whose body adds `scope`, `limit` and `consumed`. Mid-stream
  exhaustion is a terminal `error` event carrying the partial `usage`.
- **Tenant selection fails closed.** An `X-Org-Id` that differs from the identity's
  org is `403 forbidden_tenant`. On tenant-data reads (usage, budget, `POST /a2a`,
  WebSocket) a bare header is honoured only with `host_auth_backend=none` outside
  `production`.
- `POST /a2a` resolves the org the same way, because the engine scopes `ListTasks`
  and `GetTask` by the forwarded `org_id`.

### Added

- **Per-tenant engine config and gateways** (RH2-1). `<host_tenant_config_dir>/<org>/tenant.yaml`
  is checked against an exact key allowlist. Tenant secrets come from a `file` or
  `env` backend as `SecretStr`. Changes hot-reload through an mtime fingerprint, and
  in-flight turns hold a lease. Tenant config is operator-authored (SPEC-RH2-TEN-005).
- **Usage and budget** (RH2-2). `done.usage` carries the engine's 12-key usage wire.
  A usage store (`memory` or `sqlite`, with retention) backs `GET /v1/usage`
  (window totals, per-model totals, cursor paging). `GET /v1/budget` reports engine
  budget status and gateway quota windows.
- **AG-UI 1.0 stream mode** (RH2-3). Request it with `?protocol=ag-ui` or
  `Accept: …; profile=ag-ui`, or post a `RunAgentInput` to `POST /v1/agui`. The events
  come from the engine translator, framed as `data:`-only SSE.
- **A2A v1.0 dual stack** (RH2-4). Request headers and the `org_id` are forwarded to
  the engine handler. The Agent Card is versioned (`?protocolVersion=`), and tenant
  routes live at `{base}/{tenant}`.
- **WebSocket transport** (RH2-5). `GET /ws/threads/{id}` is opt-in (`host_ws_enabled`).
  It supports native and AG-UI frames, one turn at a time, cancel, a heartbeat and
  close codes `4409`, `4400`, `1009` and `1008`.
- **Fake-LLM mode** (RH2-6). `host_fake_llm_mode` serves chat from a deterministic
  script on engine test runtimes and is refused in production. Every response carries
  `X-Prismal-Fake-LLM: 1`.
- **Admin read endpoints** (RH2-6). `GET /v1/admin/{tenants,audit,decisions,routing}`
  are off by default (`404 admin_disabled`) and require `host_admin_scope` (`403`).
  Other orgs and `*` require `<scope>:global`.
- New error codes: `forbidden_tenant` (403), `forbidden` (403, admin),
  `admin_disabled` (404), `protocol_not_enabled` (406), `invalid_window` (400),
  `invalid_cursor` (400), `invalid_thread_id` (422), `invalid_input` (422) and
  `tenant_config_invalid` (500).
- OpenAPI response examples for every v0.2 route, with no secrets. FastAPI would
  strip `null` members from them, so they are re-inserted verbatim.
- Docs: `usage`, `agui`, `websocket`, `admin`, `fake-mode`; `tenancy` and `a2a`
  updated.
- Contract suite `tests/contract/test_v02_contract.py`. It runs native, AG-UI, usage
  and A2A v1 against the released engine in fake mode, with no credentials needed.

### Changed

- The import guard now allows **exactly** the engine's public host read surface
  (HRS §3), as a per-module symbol allowlist. The v0.1 entry points outside HRS §3
  are listed as documented exceptions: `A2AServerHandler`, `AuthContext`,
  `a2a_route_paths`, the A2A v1 wire constants and `prismal.identity`.
  `build_runtime`/`build_test_runtime` are imported from `prismal.composition`,
  not from its `runtime` sub-module.

### Security

- RH2-1: exact tenant-config key allowlist (endpoints, host paths, sizing and
  security/compliance floors are refused); fail-closed tenant selection; injective
  env-secret org ids.
- RH2-2: org-scoped engine thread key; usage and budget reads resolve the org from
  the identity.
- RH2-4: `POST /a2a` no longer lets `X-Org-Id` select another tenant's tasks.

### Known follow-ups

- Host counters (`tenant_config_reloads_total`, `usage_missing_total`,
  `ws_sessions_total`, …) are in-process only, with no exporter yet.
- Engine: promote the documented guard exceptions to HRS rows (a coordinated minor).
- Browser WebSocket tickets, a tighten-only self-service tenant config layer, a
  Postgres usage store, and AG-UI client tools and resume (v0.3).

## [0.1.0]

Reference host bootstrap: streaming chat over SSE, inbound A2A v0.3, the auth
(`none`/`bearer`/`oidc`) and tenancy seams, health and readiness, the Docker
image, the contract suite and the import guard. Spec:
[`specs/reference-host-bootstrap/`](./specs/reference-host-bootstrap/).

[Unreleased]: https://github.com/prismal-ai/prismal-server/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/prismal-ai/prismal-server/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/prismal-ai/prismal-server/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/prismal-ai/prismal-server/releases/tag/v0.1.0
