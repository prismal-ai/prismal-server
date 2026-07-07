# prismal-server — Reference Host (Reference Host Bootstrap)

## Strategic Plan / Product Requirements Document (PLAN)

| Field | Value |
|---|---|
| **Author** | Ernesto Crespo |
| **Status** | `DRAFT` (this repo's own PRD; expanded from the `prismal-ai` seed) |
| **Version** | 1.0 |
| **Date** | 2026-07-07 |
| **Phase** | RHB (Reference Host Bootstrap) |
| **Repository** | `prismal-ai/prismal-server` (this repo) |
| **Engine dependency** | `prismal >= 3.10, < 4` (the published engine; see `docs/composition-root.md` in that repo) |
| **Reviewers** | Tech Lead, AI Architect |
| **Priority** | P0 (production-unblocking) |
| **Related** | Engine seed `prismal/specs/reference-host-bootstrap/PLAN.md`; engine `specs/{composition-root,a2a-interop,agent-identity-governance}/`; sibling repo `prismal-ai/prismal-sdk` |

---

## 1. Executive Summary

`prismal` (the engine) is a pure library: it composes agents, RAG, tools, memory,
identity, budget, and A2A behind clean hexagonal ports, but it **ships no web
server, dashboard, or CLI** by design. This repository, `prismal-server`, is the
**minimal viable host** that binds a port, boots the engine once, and serves it
over **REST / SSE (and later WS)** — plus the inbound **A2A** endpoints — so that
non-Python clients and the front-end ecosystem (`prismal-dashboard`,
`prismal-tui`, `prismal-webchat`, `prismal-chatbot`) can finally exist.

The engine side of this contract is **already complete**: `build_runtime()`
composes every port a host needs; `get_async_compiled_graph()` is the streaming
entry point; `A2AServerHandler` / `build_agent_card()` implement inbound A2A;
`IdentityPort` / `OidcIdentityProvider` implement auth resolution. The host's only
job is to **compose and serve** — never to reimplement engine logic.

This PLAN is the PRD for that host. `ARCHITECTURE.md`, `SPEC.md`, and `TASKS.md`
in this same folder refine it into a buildable, test-first plan.

---

## 2. Context and Problem

- Without a host, `prismal` runs only inside a Python process that embeds it.
  There is no network surface, no auth boundary, no session→`thread_id` mapping,
  and therefore no dashboard/TUI/web-chat/chatbot and no *reachable* A2A agent.
- The engine already exposes exactly four host-facing entry points, and nothing
  more is needed from it:
  - `prismal.composition.runtime.build_runtime(...)` → `RuntimeContext` (ports +
    coordinated `aclose()`).
  - `prismal.agents.graph.get_async_compiled_graph(...)` → the compiled graph to
    `astream()` from.
  - `prismal.a2a.server.A2AServerHandler` + `prismal.a2a.card.build_agent_card(...)`
    → inbound A2A JSON-RPC/SSE and the Agent Card.
  - `prismal.identity` `IdentityPort` / `OidcIdentityProvider` → resolve a request
    to an `AgentIdentity`.
- The gap is purely the **process** that wires these behind HTTP. That process
  cannot live in the engine repo (its hard boundary rule), so it lives here.

---

## 3. Target Users

- **Application developers** calling prismal over HTTP/SSE/WS from a non-Python
  client (web, mobile, another service).
- **Platform / SRE** who want one deployable, health-checked, horizontally
  scalable process instead of hand-rolling a host.
- **`prismal-dashboard` / `prismal-tui` / `prismal-webchat` / `prismal-chatbot`
  maintainers**, blocked until server + SDK exist.
- **A2A ecosystem peers** who need a live `/.well-known/agent-card.json` to
  discover and call a running prismal agent.

---

## 4. Goals and Success Metrics

| Goal | Metric | Target |
|---|---|---|
| The host boots | `uvicorn prismal_server.app:app` serves `GET /healthz` | Boots from a fresh clone + `build_runtime()` |
| Streaming chat over the engine | `POST /threads/{id}/messages` streams tokens/tool-calls via SSE | Round-trips a full turn through `get_async_compiled_graph().astream()` |
| A2A reachable | `/.well-known/agent-card.json` + `/a2a` mounted over `A2AServerHandler` | Passes an external A2A client smoke test |
| Session/tenant mapping | HTTP session ↔ `thread_id` / `org_id` consistent with `build_runtime(org_id=...)` | 1:1, documented |
| Auth is a pluggable seam | A request resolves to an `AgentIdentity` via `IdentityPort` | Default no-auth (dev) + bearer/OIDC backends |
| Engine untouched | Zero changes required inside `prismal/` | Only the four entry points are used |

---

## 5. Scope

### In scope (v0.1 — the minimal viable host)
- `GET /healthz` and `GET /readyz` health/readiness checks.
- One streaming chat endpoint (`POST /threads/{id}/messages`, SSE) mapping one
  HTTP session to one `thread_id` and streaming the compiled graph.
- The inbound A2A endpoints mounted over `A2AServerHandler` when `a2a_enabled`.
- A pluggable auth seam resolving a request → `AgentIdentity` (default: no-auth
  dev backend; bearer + OIDC backends reuse the engine's providers).
- Per-tenant `org_id` resolution → `build_runtime(org_id=...)`, one cached
  `RuntimeContext` per org, torn down on shutdown.
- Contract/smoke tests against a **released** `prismal` version.

### Out of scope (deferred)
- WebSocket transport (SSE first; WS is a fast-follow, specced but `SHOULD`).
- `prismal-dashboard`, `prismal-tui`, `prismal-webchat`, `prismal-chatbot` — each
  is its own repo, blocked on this host + the SDK.
- Auth/IdP **product** decisions (Entra/Okta wiring) beyond exposing the seam —
  the engine's `OidcIdentityProvider` already implements the mechanism.
- Deployment topology (containers, k8s, autoscaling) — a separate ops concern,
  sketched in `ARCHITECTURE.md` §Deployment but not built here.
- The `prismal-sdk` client itself — it lives in the sibling `prismal-sdk` repo;
  this repo only **defines the wire contract** it must satisfy (`SPEC.md` §SDK).

---

## 6. Functional Requirements (summary; refined in `SPEC.md`)

| ID | Requirement | Priority |
|---|---|---|
| RF-RHB-001 | Boot `build_runtime()` once at startup (lifespan); `RuntimeContext.aclose()` on shutdown | `MUST` |
| RF-RHB-002 | A streaming endpoint maps one HTTP session → one `thread_id` and streams `get_async_compiled_graph().astream(...)` | `MUST` |
| RF-RHB-003 | `/.well-known/agent-card.json` + `/a2a` mounted over `A2AServerHandler` when `a2a_enabled` | `MUST` |
| RF-RHB-004 | Auth is a pluggable seam resolving to an `AgentIdentity` via `IdentityPort` | `SHOULD` |
| RF-RHB-005 | Per-tenant `org_id` → `build_runtime(org_id=...)` collection isolation | `SHOULD` |
| RF-RHB-006 | The wire contract is sufficient for a thin `prismal-sdk` with no business logic | `SHOULD` |
| RF-RHB-007 | No engine logic duplicated — the host only composes and serves | `MUST` |
| RF-RHB-008 | Health/readiness endpoints report engine composition status | `MUST` |

---

## 7. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Scope creep — building dashboard/TUI features into the server | Hold the line at RF-RHB-001…008; each front-end is its own repo |
| Reinventing engine logic in the host | The four entry points are the **only** allowed seams — enforced by the `CLAUDE.md` review checklist |
| Engine/host drift (engine changes break the host silently) | Pin `prismal` (`>=3.10,<4`); a contract/smoke test runs against the released engine in CI |
| Streaming back-pressure / disconnects | SSE with explicit heartbeat + cancellation wired to graph cancellation (`SPEC.md` §Streaming) |
| Secret leakage over the wire | Reuse engine security: `InputSanitizer` on inbound A2A, `SecretStr` in config, never echo credentials; auth backends return `AgentIdentity` (no secret) |

---

## 8. Dependencies

- `prismal >= 3.10, < 4` — the engine (published to PyPI). Entry points:
  `build_runtime`, `get_async_compiled_graph`, `A2AServerHandler`,
  `build_agent_card`, `IdentityPort`/`OidcIdentityProvider`.
- `fastapi` + `uvicorn[standard]` — HTTP framework + ASGI server (see ADR-001).
- `httpx` (tests) — ASGI transport for contract/smoke tests.
- Sibling `prismal-ai/prismal-sdk` — consumes this repo's wire contract; not a
  build dependency of the server.

---

## 9. Milestones

| Milestone | Content | Exit criterion |
|---|---|---|
| M0 — Seed | This SDD set (`PLAN`/`ARCHITECTURE`/`SPEC`/`TASKS`) + repo `CLAUDE.md` | Reviewed & merged |
| M1 — Skeleton boots | `app.py` factory + lifespan + `/healthz` + config | `uvicorn` boots, health green |
| M2 — Streaming chat | `POST /threads/{id}/messages` SSE over the graph | A turn round-trips end-to-end |
| M3 — A2A reachable | Agent Card + `/a2a` mounted | External A2A client smoke passes |
| M4 — Auth + tenant | Auth seam + `org_id` → per-tenant runtime | Two orgs isolated; identity resolved |
| M5 — Hardening | Contract test vs released engine, docs, container | CI green; image publishes |

---

## Change History

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-07-07 | Ernesto Crespo | Ported + expanded from the `prismal-ai` seed PRD into this repo's own PLAN |
