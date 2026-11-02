# Changelog

All notable changes to `prismal-server`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/).

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

[0.2.0]: https://github.com/prismal-ai/prismal-server/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/prismal-ai/prismal-server/releases/tag/v0.1.0
