# prismal-server — Task Breakdown (Reference Host Bootstrap)

| Field | Value |
|---|---|
| **Status** | `DRAFT` |
| **Version** | 1.0 |
| **Date** | 2026-07-07 |
| **Companion** | [`PLAN.md`](./PLAN.md) · [`ARCHITECTURE.md`](./ARCHITECTURE.md) · [`SPEC.md`](./SPEC.md) |

Status legend: `TODO` · `WIP` · `DONE` · `BLOCKED`. Every task is **test-first
(TDD)** — write the failing test, watch it fail, minimal code to green. Tasks map
to `SPEC-RHB-*` IDs and the `M0…M5` milestones in `PLAN.md`.

---

## Phase 0 — Repo scaffold (M1 prep)

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RHB-00-01 | `pyproject.toml`: `src` layout, `prismal>=3.10,<4`, `fastapi`, `uvicorn[standard]`; dev extras `pytest`, `pytest-asyncio`, `httpx`, `ruff`, `mypy` | 0.3 d | — | NFR-004 | `DONE` |
| RHB-00-02 | Package skeleton `src/prismal_server/__init__.py` (`__version__`), empty modules per `ARCHITECTURE.md` §3 | 0.2 d | 00-01 | — | `DONE` |
| RHB-00-03 | `tests/conftest.py`: ASGI `httpx.AsyncClient` fixture + `build_test_runtime()` fake registry fixture | 0.3 d | 00-02 | NFR-003 | `DONE` |
| RHB-00-04 | CI (`.github/workflows/ci.yml`): ruff + mypy + pytest (unit); a separate opt-in `contract` job | 0.3 d | 00-01 | NFR-003 | `DONE` |

## Phase 1 — Skeleton boots (M1)

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RHB-01-01 | `config.py`: `HostSettings` (pydantic-settings, `PRISMAL_SERVER_` prefix, `SecretStr` for secrets) | 0.3 d | 00-02 | CFG-001/002 | `DONE` |
| RHB-01-02 | `deps.py`: `RuntimeRegistry` — lazy per-`org_id` `build_runtime`, single-flight lock, `aclose_all()` | 0.6 d | 01-01 | LIF-004, TEN-002 | `DONE` |
| RHB-01-03 | `app.py`: `create_app()` factory + lifespan (startup registry, shutdown `aclose_all`) | 0.4 d | 01-02 | LIF-001/002/003 | `DONE` |
| RHB-01-04 | `routes/health.py`: `GET /healthz` (liveness), `GET /readyz` (default runtime composed) | 0.3 d | 01-03 | HLT-001/002/003 | `DONE` |
| RHB-01-05 | `errors.py`: engine-exception → HTTP mapping + JSON error body; register exception handlers | 0.4 d | 01-03 | ERR-001/002/003 | `DONE` |

## Phase 2 — Streaming chat (M2)

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RHB-02-01 | `streaming.py`: SSE framing helper (`event:`/`data:`), heartbeat generator, `astream → SSE` bridge with cancellation | 0.6 d | 01-03 | CHT-002/003/004 | `DONE` |
| RHB-02-02 | `routes/threads.py`: `POST /threads` mints opaque `thread_id` | 0.2 d | 02-01 | THR-002 | `DONE` |
| RHB-02-03 | `routes/threads.py`: `POST /threads/{id}/messages` → resolve runtime + `get_async_compiled_graph` + `astream` as `text/event-stream` | 0.7 d | 02-01 | CHT-001, THR-001/003 | `DONE` |
| RHB-02-04 | Mid-stream failure → terminal `error` SSE event (status already 200) | 0.3 d | 02-03 | CHT-006 | `DONE` |
| RHB-02-05 | Client-disconnect cancels the `astream` task (no orphaned work) — test with a slow fake graph | 0.4 d | 02-03 | CHT-004 | `DONE` |

## Phase 3 — A2A reachable (M3)

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RHB-03-01 | `routes/a2a.py`: `GET /.well-known/agent-card.json` via `build_agent_card(...)`, cached per `(settings,org)` | 0.4 d | 01-03 | A2A-001 | `DONE` |
| RHB-03-02 | `routes/a2a.py`: `POST /a2a` → `RuntimeContext.a2a_handler.handle_rpc` (non-stream) | 0.4 d | 03-01 | A2A-002 | `DONE` |
| RHB-03-03 | `POST /a2a` SSE-streaming path via `stream_rpc` | 0.4 d | 03-02 | A2A-002 | `DONE` |
| RHB-03-04 | Gating: `a2a_enabled=false` → `404`; `a2a_strict` unauth → JSON-RPC `-32001` | 0.3 d | 03-02 | A2A-004/005 | `DONE` |
| RHB-03-05 | Assert host does not re-sanitize/re-audit (handler owns it) — verified by the import-guard + a behavioural test | 0.2 d | 03-02 | A2A-003 | `DONE` |

> RHB-03-01 depended on the engine re-exporting `get_settings` + `DEFAULT_CAPABILITY_MAP`
> from `prismal.a2a` (host imports the card inputs only from the A2A seam). Shipped in
> **prismal-ai 3.10.2**; the host pin is bumped and `GET /.well-known/agent-card.json`
> now serves a real card (verified E2E against the released engine).

## Phase 4 — Auth + tenancy (M4)

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RHB-04-01 | `auth/base.py`: `AuthBackend` Protocol (`resolve(request) -> AgentIdentity | None`, never raises) | 0.2 d | 01-03 | AUT-001 | `DONE` |
| RHB-04-02 | `auth/noauth.py`: `NoAuthBackend` dev default (fixed configurable identity) | 0.2 d | 04-01 | AUT-002 | `DONE` |
| RHB-04-03 | `auth/bearer.py`: `BearerTokenBackend` (static map / JWT claims → identity) | 0.4 d | 04-01 | AUT-003 | `DONE` |
| RHB-04-04 | `auth/oidc.py`: `OidcAuthBackend` delegating to engine `OidcIdentityProvider` | 0.4 d | 04-01 | AUT-004 | `DONE` |
| RHB-04-05 | Backend selection by `host_auth_backend`; `host_auth_strict` → `401` on protected routes | 0.3 d | 04-02 | AUT-005/006 | `DONE` |
| RHB-04-06 | Tenant resolution: `identity.org_id` → `X-Org-Id` → `None`; wire into runtime selection + `config.org_id` | 0.4 d | 01-02, 04-01 | TEN-001/002 | `DONE` |
| RHB-04-07 | `host_max_tenants` LRU-close of overflow runtimes | 0.3 d | 04-06 | TEN-003 | `DONE` |

## Phase 5 — Hardening & release (M5)

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RHB-05-01 | Import-guard test: only the four engine entry points are imported from `prismal` (AST scan) | 0.3 d | all | NFR-001 | `DONE` |
| RHB-05-02 | `contract/` suite (opt-in marker): boot a real `build_runtime()` + one chat turn + Agent Card fetch against the released engine | 0.5 d | 02-03, 03-01 | NFR-003 | `DONE` |
| RHB-05-03 | `/openapi.json` sanity + a generated-client smoke (for `prismal-sdk`) | 0.3 d | 02-03 | SDK-004 | `DONE` |
| RHB-05-04 | `Dockerfile` (uvicorn entrypoint) + compose sample; health/readiness wired | 0.4 d | 01-04 | — | `DONE` |
| RHB-05-05 | `README.md` quickstart + `docs/` (run, auth, tenancy, A2A); link the SDK repo | 0.4 d | all | — | `DONE` |
| RHB-05-06 | Tag `v0.1.0`; release workflow (PyPI optional; GHCR image) | 0.3 d | 05-04 | — | `DONE` |

---

## Definition of Done (v0.1)

- `uvicorn prismal_server.app:app` boots from a fresh clone; `/healthz` green,
  `/readyz` green once the default runtime composes.
- A full chat turn round-trips through `get_async_compiled_graph().astream()` over
  SSE; client disconnect cancels the turn.
- `/.well-known/agent-card.json` + `/a2a` work over `A2AServerHandler`; an
  external A2A client smoke passes.
- Auth resolves a request → `AgentIdentity` via the selected backend; two `org_id`s
  are isolated.
- The import-guard test proves no engine logic is duplicated; unit suite runs
  with no live provider I/O; the `contract` suite passes against released
  `prismal`.

## Estimate roll-up

~**11.5 person-days** across M1–M5 (excludes the M0 seed, already done). Phases
are independently shippable; M1→M2→M3 is the critical path, M4 can parallelize
after M1, M5 closes.

---

## Change History

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-07-07 | Ernesto Crespo | Initial task breakdown for the reference host |
