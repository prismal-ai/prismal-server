# prismal-server — Architecture (Reference Host Bootstrap)

| Field | Value |
|---|---|
| **Status** | `DRAFT` |
| **Version** | 1.0 |
| **Date** | 2026-07-07 |
| **Companion** | [`PLAN.md`](./PLAN.md) · [`SPEC.md`](./SPEC.md) · [`TASKS.md`](./TASKS.md) |

---

## 1. Guiding principle — orchestrate, do not reimplement

> **contract / logic → engine (`prismal/`); serving HTTP, authenticating,
> rendering, persisting config → host (`prismal-server/`).**

The host is a thin ASGI process. It has exactly **four** allowed seams into the
engine and adds **no** agent, RAG, tool, memory, or policy logic of its own:

| Seam | Engine symbol | Used for |
|---|---|---|
| Composition | `prismal.composition.runtime.build_runtime(...)` → `RuntimeContext` | Boot all ports once per tenant; `aclose()` on shutdown |
| Execution | `prismal.agents.graph.get_async_compiled_graph(...)` | `astream()` a turn |
| Inbound A2A | `prismal.a2a.server.A2AServerHandler`, `prismal.a2a.card.build_agent_card(...)` | Mount `/a2a` + Agent Card |
| Identity | `prismal.identity` `IdentityPort` / `OidcIdentityProvider` | Resolve a request → `AgentIdentity` |

Any pull toward a fifth seam (reaching into `prismal.rag`, `prismal.security`,
`prismal.mcp`, …) is a design smell — see the `CLAUDE.md` review checklist.

---

## 2. C4 — Context

```
                       ┌───────────────────────────────────────┐
   Browser / Mobile ──▶│           prismal-sdk (thin)          │
   CLI / Service       └───────────────────┬───────────────────┘
                                           │ REST + SSE (/ WS)
                                           ▼
   A2A peer ──/a2a/──────────────▶ ┌──────────────────────────┐
   (JSON-RPC/SSE)                  │      prismal-server      │   this repo
   /.well-known/agent-card.json ──▶│  (FastAPI ASGI process)  │
                                   └────────────┬─────────────┘
                                                │ 4 entry points only
                                                ▼
                                   ┌──────────────────────────┐
                                   │      prismal (engine)    │   pip dep
                                   │ build_runtime · graph ·  │   >=3.10,<4
                                   │ A2AServerHandler · IdPort│
                                   └──────────────────────────┘
```

---

## 3. C4 — Container / module layout

`src/` layout, single distributable package `prismal_server`:

```
prismal-server/
├── pyproject.toml                 # deps: prismal>=3.10,<4, fastapi, uvicorn[standard]
├── CLAUDE.md                      # boundary rule + review checklist
├── src/prismal_server/
│   ├── __init__.py                # __version__
│   ├── app.py                     # create_app() factory + lifespan; `app` ASGI instance
│   ├── config.py                  # HostSettings (pydantic-settings): bind host/port, cors, auth mode
│   ├── deps.py                    # RuntimeRegistry (per-org RuntimeContext cache) + FastAPI deps
│   ├── streaming.py               # SSE helpers: event framing, heartbeat, graph-astream → SSE bridge
│   ├── errors.py                  # HTTP error model + engine-exception → HTTP mapping
│   ├── auth/
│   │   ├── base.py                # AuthBackend Protocol (request → AgentIdentity | None)
│   │   ├── noauth.py              # NoAuthBackend (dev default): fixed local identity
│   │   ├── bearer.py              # BearerTokenBackend: static/JWT bearer → AgentIdentity
│   │   └── oidc.py                # OidcAuthBackend: delegates to engine OidcIdentityProvider
│   └── routes/
│       ├── health.py              # GET /healthz, GET /readyz
│       ├── threads.py             # POST /threads/{id}/messages (SSE), POST /threads
│       └── a2a.py                 # GET /.well-known/agent-card.json, POST /a2a  (mounts A2AServerHandler)
└── tests/
    ├── conftest.py                # ASGI httpx client; build_test_runtime() fixtures
    ├── unit/                      # routes, streaming, auth, deps (fakes; no live engine I/O)
    └── contract/                  # smoke tests vs a released prismal (marked, opt-in)
```

**Why a `RuntimeRegistry` and not a single global runtime:** Fase Z ships a
*factory* (not a singleton), so the vector store is always carried inside a
`RuntimeContext`. The host therefore keeps one `RuntimeContext` **per `org_id`**
in an in-process registry, built lazily on first use and closed on shutdown. In
single-tenant mode there is exactly one entry (`org_id = None`).

---

## 4. Data flow

### 4.1 Streaming chat turn (RF-RHB-002)

```
POST /threads/{thread_id}/messages  {content, ...}
  │
  ├─ auth: AuthBackend.resolve(request) ─────────────▶ AgentIdentity (or 401 in strict)
  ├─ tenant: org_id = identity.org_id or header ─────▶ RuntimeRegistry.get(org_id)
  │        └─ (miss) build_runtime(settings, org_id=org_id) → RuntimeContext (cached)
  ├─ graph = get_async_compiled_graph(...)            (LRU-cached by the engine)
  ├─ config = {configurable: {thread_id, org_id, ...}}   ← session ↔ thread mapping
  └─ SSE stream:
        async for chunk in graph.astream(input, config, stream_mode="messages"):
            yield sse_event("token" | "tool_call" | "state", chunk)
        yield sse_event("done", summary)
     (client disconnect → cancel the astream task; heartbeat every N s)
```

### 4.2 Inbound A2A (RF-RHB-003)

```
GET /.well-known/agent-card.json
  └─ build_agent_card(settings, registry, org_id=…, did=…) → JSON (cached per org)

POST /a2a   (JSON-RPC: message/send | tasks/get | tasks/cancel)
  ├─ AuthContext gate (strict → 401 / JSON-RPC -32001)
  ├─ handler = RuntimeContext.a2a_handler         (engine-built; graph + sanitizer + audit)
  ├─ non-stream: await handler.handle_rpc(payload) → JSON-RPC result
  └─ stream:     async for line in handler.stream_rpc(payload): yield  SSE `data:` line
```

The host does **not** sanitize or audit A2A content itself — `A2AServerHandler`
already L1-sanitizes (`InputSanitizer`) and audits every in/out task. The host
owns only transport + auth in front.

### 4.3 Lifespan (RF-RHB-001)

```
startup:  settings = get_settings(); registry = RuntimeRegistry(settings)
          (optionally warm the default-tenant RuntimeContext)
shutdown: await registry.aclose_all()   # each RuntimeContext.aclose(): MCP disconnect,
                                          # checkpointer close, built stores released
```

---

## 5. Architecture Decision Records

### ADR-001 — FastAPI + Uvicorn
**Decision:** FastAPI on Uvicorn (`uvicorn[standard]`).
**Rationale:** Starlette (FastAPI's core) is already a transitive dependency of
the engine; `A2AServerHandler` speaks JSON-RPC + SSE which map cleanly to
Starlette `StreamingResponse`; FastAPI gives typed request/response models and
dependency injection for the auth/tenant seams with minimal boilerplate.
**Alternatives:** raw Starlette (less ergonomics), aiohttp (weaker typing/DI),
Litestar (smaller ecosystem). **Status:** Accepted.

### ADR-002 — SSE first, WebSocket deferred
**Decision:** Ship SSE for the streaming chat turn; WS is specced `SHOULD` and
deferred to a fast-follow. **Rationale:** the graph produces a unidirectional
token/tool-call stream per turn; SSE matches that exactly, is trivially
proxy/CDN-friendly, and the A2A handler already emits SSE. WS adds bidirectional
complexity only needed for interactive multi-turn sockets. **Status:** Accepted.

### ADR-003 — Per-`org_id` RuntimeContext registry, no global runtime
**Decision:** Keep one `RuntimeContext` per `org_id` in an in-process registry,
built lazily via `build_runtime(org_id=...)`. **Rationale:** Fase Z is
factory-based (no vector-store singleton), so tenants must not share a runtime;
`build_runtime` already yields per-tenant collection isolation
(`collection_for(base, org_id)`). **Status:** Accepted.

### ADR-004 — Auth as a `Protocol` seam, identity from the engine
**Decision:** Define an `AuthBackend` Protocol (`resolve(request) -> AgentIdentity
| None`); provide `NoAuth` (dev), `Bearer`, and `Oidc` backends; the OIDC backend
delegates token→identity to the engine's `OidcIdentityProvider`. **Rationale:**
identity *mechanism* is engine-side (Phase IDN); the host only owns *where the
credential comes from on the wire*. **Status:** Accepted.

### ADR-005 — Pin the engine, contract-test against the release
**Decision:** Depend on `prismal >= 3.10, < 4`; a `contract` test suite runs
against the installed released engine in CI. **Rationale:** the host is coupled
to four engine entry points; a smoke test catches drift before deploy.
**Status:** Accepted.

---

## 6. Error handling

- A single `errors.py` maps engine exceptions to HTTP:
  `RuntimeCompositionError` → 503 (readiness), `PolicyDenied` → 403,
  `BudgetExceeded` → 402/429 (configurable), `A2AAgentUnavailable` → 502,
  validation → 422, unknown → 500 (opaque body; details only in logs).
- Streaming errors mid-turn are emitted as a terminal SSE `error` event, then the
  stream closes — the HTTP status is already `200` by then, so the event carries
  the failure (documented in `SPEC.md` §Streaming error model).
- No secret or raw credential ever appears in a response body; the engine's
  `SecretStr` config and `AgentIdentity` (secret-free) are the guard.

---

## 7. Observability

The host reuses the engine's telemetry rather than adding its own: it starts a
request span, binds `thread_id`/`org_id`/route, and lets the engine's
`OTelManager` / `ObservabilityPort` emit run-level spans. Health endpoints expose
liveness (process up) and readiness (default `RuntimeContext` composed). Metrics
are scraped from the engine's existing counters; the host adds only HTTP-level
request counters/histograms.

---

## 8. Deployment (sketch — not built in v0.1)

Single stateless container: `uvicorn prismal_server.app:app --host 0.0.0.0
--port 8000`, `N` replicas behind a load balancer. Checkpointer/vector-store
state is external (SQLite→Postgres, Chroma→server backend via engine extras) so
replicas are horizontally scalable. Liveness = `/healthz`, readiness = `/readyz`.
Config via env (`prismal`'s `ConfigSourcePort` + `HostSettings`). Details are a
separate ops concern; this section only fixes the shape.

---

## Change History

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-07-07 | Ernesto Crespo | Initial architecture for the reference host |
