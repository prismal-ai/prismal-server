# prismal-server — Technical Specification (Reference Host Bootstrap)

| Field | Value |
|---|---|
| **Status** | `DRAFT` |
| **Version** | 1.0 |
| **Date** | 2026-07-07 |
| **Companion** | [`PLAN.md`](./PLAN.md) · [`ARCHITECTURE.md`](./ARCHITECTURE.md) · [`TASKS.md`](./TASKS.md) |

Requirement IDs are `SPEC-RHB-<AREA>-NNN`. Priority follows RFC 2119
(`MUST`/`SHOULD`/`MAY`). Each maps back to a `RF-RHB-*` in `PLAN.md`.

---

## 1. Runtime composition & lifespan

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-LIF-001 | On ASGI **startup**, the app MUST construct a `RuntimeRegistry(settings)` and MAY warm the default-tenant (`org_id=None`) `RuntimeContext` via `build_runtime(settings)`. | `MUST` |
| SPEC-RHB-LIF-002 | On ASGI **shutdown**, the app MUST call `aclose()` on every cached `RuntimeContext` (MCP disconnect, checkpointer close, stores released), swallowing per-tenant errors so one failure never blocks the rest. | `MUST` |
| SPEC-RHB-LIF-003 | The app MUST be built by a `create_app() -> FastAPI` factory; the module-level `app` is `create_app()`. Tests MUST be able to build an app with an injected fake registry (`build_test_runtime`). | `MUST` |
| SPEC-RHB-LIF-004 | `RuntimeRegistry.get(org_id)` MUST build the `RuntimeContext` lazily on first use and cache it; concurrent first-use for the same `org_id` MUST NOT build twice (single-flight lock). | `MUST` |

## 2. Health & readiness

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-HLT-001 | `GET /healthz` MUST return `200 {"status":"ok"}` whenever the process is up (liveness), independent of engine state. | `MUST` |
| SPEC-RHB-HLT-002 | `GET /readyz` MUST return `200 {"status":"ready"}` only when the default `RuntimeContext` composed successfully; otherwise `503 {"status":"not_ready","reason":...}`. | `MUST` |
| SPEC-RHB-HLT-003 | Neither endpoint MAY leak configuration values or secrets; `reason` is a stable non-sensitive code. | `MUST` |

## 3. Thread / chat streaming

### 3.1 Session ↔ thread model

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-THR-001 | A `thread_id` (path segment) MUST map 1:1 to the engine graph `thread_id` (passed as `config.configurable.thread_id`). Reusing a `thread_id` MUST continue the same conversation via the engine checkpointer. | `MUST` |
| SPEC-RHB-THR-002 | `POST /threads` MAY mint a new opaque `thread_id` (server-generated UUID) and return it; clients MAY also choose their own `thread_id` and `POST /threads/{id}/messages` directly. | `SHOULD` |
| SPEC-RHB-THR-003 | The resolved `org_id` MUST be threaded into `config.configurable.org_id` and MUST select the tenant `RuntimeContext`. | `MUST` |

### 3.2 `POST /threads/{thread_id}/messages` (SSE)

Request body:
```json
{ "content": "string (required)", "role": "user", "metadata": { } }
```

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-CHT-001 | The endpoint MUST resolve identity (§5), select the tenant runtime (§1), obtain the graph via `get_async_compiled_graph(...)`, and stream `graph.astream(input, config)` as **`text/event-stream`**. | `MUST` |
| SPEC-RHB-CHT-002 | SSE events MUST use named types: `token` (incremental assistant text), `tool_call` (tool invocation surfaced by the graph), `state` (optional node/state transition), `done` (terminal, carries a final summary), `error` (terminal failure). Each `data:` payload is JSON. | `MUST` |
| SPEC-RHB-CHT-003 | The server MUST emit a heartbeat comment (`:\n\n`) at a configurable interval (default 15 s) to keep intermediaries from closing idle streams. | `SHOULD` |
| SPEC-RHB-CHT-004 | On client disconnect, the server MUST cancel the underlying `astream` task promptly (no orphaned graph work). | `MUST` |
| SPEC-RHB-CHT-005 | Request `content` crosses a trust boundary; the graph's own security layers apply. The host MUST NOT f-string user content into any prompt (there is none host-side) and MUST pass it only as graph input. | `MUST` |
| SPEC-RHB-CHT-006 | A mid-stream engine failure MUST be delivered as a terminal `error` event (`{"code":...,"message":...}`) followed by stream close; the HTTP status stays `200` (headers already sent). | `MUST` |

Example stream:
```
: heartbeat

event: token
data: {"delta":"Hola"}

event: tool_call
data: {"name":"web_search","args":{"q":"..."}}

event: done
data: {"thread_id":"...","finish_reason":"stop"}
```

## 4. Inbound A2A

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-A2A-001 | When `settings.a2a_enabled`, `GET /.well-known/agent-card.json` MUST return `build_agent_card(settings, registry, org_id=…, did=…)` serialized with `by_alias=True` (camelCase wire form); the result MUST be cached per `(settings fingerprint, org_id)`. | `MUST` |
| SPEC-RHB-A2A-002 | `POST /a2a` MUST delegate JSON-RPC (`message/send`, `tasks/get`, `tasks/cancel`) to `RuntimeContext.a2a_handler.handle_rpc(...)`; SSE-streaming requests MUST use `stream_rpc(...)` and emit `data:` lines. | `MUST` |
| SPEC-RHB-A2A-003 | The host MUST NOT re-sanitize or re-audit A2A content — `A2AServerHandler` already L1-sanitizes and audits. The host owns transport + auth only. | `MUST` |
| SPEC-RHB-A2A-004 | When `settings.a2a_strict`, unauthenticated `/a2a` calls MUST be rejected (JSON-RPC error `-32001`); the Agent Card endpoint MAY stay public. | `MUST` |
| SPEC-RHB-A2A-005 | When `a2a_enabled` is false, both routes MUST return `404` (feature absent), not `500`. | `MUST` |

## 5. Authentication seam

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-AUT-001 | An `AuthBackend` Protocol MUST define `async resolve(request) -> AgentIdentity | None`; it MUST NOT raise for an unauthenticated request (returns `None`). | `MUST` |
| SPEC-RHB-AUT-002 | The default backend MUST be `NoAuthBackend` (dev), returning a fixed local `AgentIdentity` (configurable DID/agent_name), so a fresh clone boots usable. | `MUST` |
| SPEC-RHB-AUT-003 | `BearerTokenBackend` MUST resolve a `Authorization: Bearer` token to an `AgentIdentity` (static map or JWT claims → DID/org_id/scopes). | `SHOULD` |
| SPEC-RHB-AUT-004 | `OidcAuthBackend` MUST delegate token validation + identity minting to the engine's `OidcIdentityProvider` (no token crypto in the host). | `SHOULD` |
| SPEC-RHB-AUT-005 | When `settings.host_auth_strict`, a request that resolves to `None` MUST get `401` on chat/thread routes (health stays public). | `MUST` |
| SPEC-RHB-AUT-006 | The selected backend MUST be chosen by config (`host_auth_backend ∈ {none, bearer, oidc}`), pluggable without code changes to routes. | `MUST` |

## 6. Multi-tenancy

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-TEN-001 | `org_id` MUST be resolved as: `AgentIdentity.org_id` if present, else an `X-Org-Id` header, else `None` (single-tenant). | `MUST` |
| SPEC-RHB-TEN-002 | Each distinct `org_id` MUST map to its own `RuntimeContext` via `build_runtime(org_id=org_id)`; parallel tenants MUST stay isolated (engine `collection_for(base, org_id)`). | `MUST` |
| SPEC-RHB-TEN-003 | The registry MUST bound the number of live tenant runtimes (`host_max_tenants`, LRU-close on overflow) to cap resource use. | `SHOULD` |

## 7. Configuration

`HostSettings` (pydantic-settings, prefix `PRISMAL_SERVER_`) governs **host**
concerns only; engine settings stay in the engine's `Settings` / `ConfigSourcePort`.

| Key | Default | Meaning |
|---|---|---|
| `host` | `0.0.0.0` | Bind address |
| `port` | `8000` | Bind port |
| `cors_origins` | `[]` | Allowed CORS origins |
| `host_auth_backend` | `none` | `none` \| `bearer` \| `oidc` |
| `host_auth_strict` | `false` | Reject unauthenticated on protected routes |
| `sse_heartbeat_s` | `15` | SSE heartbeat interval |
| `host_max_tenants` | `32` | Live tenant-runtime cap (LRU) |
| `dev_identity_did` | `did:key:zLocalDev` | `NoAuthBackend` identity DID |

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-CFG-001 | `HostSettings` MUST NOT duplicate engine settings; engine config is read by the engine via its own `ConfigSourcePort`. | `MUST` |
| SPEC-RHB-CFG-002 | Secret-bearing host config (e.g. bearer static tokens) MUST be `SecretStr`. | `MUST` |

## 8. Error model

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-ERR-001 | Non-stream errors MUST return a JSON body `{"error":{"code","message"}}` with a mapped HTTP status (see `ARCHITECTURE.md` §6). | `MUST` |
| SPEC-RHB-ERR-002 | `500` responses MUST carry an opaque message; internal detail goes only to logs/telemetry. | `MUST` |
| SPEC-RHB-ERR-003 | Engine exceptions MUST be mapped centrally (`errors.py`), never leaked as raw tracebacks. | `MUST` |

## 9. SDK wire contract (for `prismal-sdk`)

This repo does **not** implement the SDK (it lives in `prismal-ai/prismal-sdk`);
it fixes the contract the SDK must satisfy so the SDK stays business-logic-free.

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-SDK-001 | The chat contract MUST be fully described by: `POST /threads` → `{thread_id}`, and `POST /threads/{id}/messages` → SSE (`token`/`tool_call`/`state`/`done`/`error`). | `SHOULD` |
| SPEC-RHB-SDK-002 | The A2A contract MUST be the standard A2A v0.3.x JSON-RPC/SSE surface (no prismal-specific extension) so generic A2A clients interoperate. | `SHOULD` |
| SPEC-RHB-SDK-003 | Auth MUST be a single `Authorization: Bearer` header (or none in dev); the SDK MUST NOT need any other prismal-specific header beyond optional `X-Org-Id`. | `SHOULD` |
| SPEC-RHB-SDK-004 | An OpenAPI document MUST be served at `/openapi.json` (FastAPI default) so the SDK MAY be generated/verified against it. | `SHOULD` |

## 10. Non-functional

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RHB-NFR-001 | No engine logic duplicated — only the four entry points are imported from `prismal` (enforced by an import-guard test + `CLAUDE.md` checklist). | `MUST` |
| SPEC-RHB-NFR-002 | The server MUST run fully async (no blocking calls on the event loop); the engine's async graph and `aclose()` are awaited. | `MUST` |
| SPEC-RHB-NFR-003 | Unit tests MUST run without live LLM/provider I/O (use `build_test_runtime` fakes); a separate `contract` suite (opt-in marker) exercises a released engine. | `MUST` |
| SPEC-RHB-NFR-004 | The engine dependency MUST be pinned `>=3.10,<4`. | `MUST` |

---

## 11. Traceability (RF → SPEC)

| RF | Covered by |
|---|---|
| RF-RHB-001 | SPEC-RHB-LIF-001/002/003/004 |
| RF-RHB-002 | SPEC-RHB-THR-*, SPEC-RHB-CHT-* |
| RF-RHB-003 | SPEC-RHB-A2A-* |
| RF-RHB-004 | SPEC-RHB-AUT-* |
| RF-RHB-005 | SPEC-RHB-TEN-* |
| RF-RHB-006 | SPEC-RHB-SDK-* |
| RF-RHB-007 | SPEC-RHB-NFR-001 |
| RF-RHB-008 | SPEC-RHB-HLT-* |

---

## Change History

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-07-07 | Ernesto Crespo | Initial technical spec for the reference host |
