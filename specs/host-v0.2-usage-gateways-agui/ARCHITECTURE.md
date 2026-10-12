# prismal-server — Architecture (Host v0.2: Usage, Gateways, AG-UI)

| Field | Value |
|---|---|
| **Status** | `READY` |
| **Version** | 1.0 |
| **Date** | 2026-10-04 |
| **Author** | Ernesto Crespo |
| **Companion** | [`PLAN.md`](./PLAN.md) · [`SPEC.md`](./SPEC.md) · [`TASKS.md`](./TASKS.md) |

---

## 1. Context

v0.1 layout: `app.py` (factory + lifespan), `deps.py` (`RuntimeRegistry`, LRU per `org_id`),
`streaming.py` (SSE framing, heartbeat, `astream` bridge with cancellation), `routes/{health,threads,a2a}.py`,
`auth/{base,noauth,bearer,oidc}.py`, `errors.py`, `config.py` (`HostSettings`). The AST import guard
allows four engine entry points. v0.2 adds modules around these seams; none of the v0.1 seams change shape.

## 2. Feasibility

- **Tenant config:** the engine's `ConfigSourcePort` is a structural `Protocol` (`load()`), so the
  host implements it without importing it. `build_runtime(org_id=…)` is already called by the
  registry; v0.2 passes `config_source=` (`SPEC-HRS-CMP-001`).
- **Usage:** PRT5 adds `prismal.monitoring.get_run_summary(session_id)` and
  `RunSummary.to_usage_wire()`; the host calls them after the stream ends. `thread_id` is the engine
  `session_id`/`thread_id` (SPEC-RHB-THR-001).
- **AG-UI:** the engine translator is pure and sync; the existing SSE bridge only needs a second
  framing mode and `stream_mode=["messages","updates","custom"]` on `astream`.
- **A2A v1:** dual-stack lives in `A2AServerHandler` (PRT2); the host forwards headers + `org_id`.
- **Budget/quota/audit/reports/A2A routes:** read through the engine-owned host read surface
  (HRS delta to the composition root): `RuntimeContext.budget_status()`, `quota_windows()`,
  `audit_page()`, `verify_audit_chain()`, `decisions_report()`, `routing_report()`, `a2a_routes()` —
  method calls on the object `build_runtime` returns, no new imports.

## 3. Modules

| Module | New/ext | Purpose |
|---|---|---|
| `tenancy/config_source.py` | new | `TenantConfigSource` (allowlisted keys, `SecretStr`), mtime watcher |
| `tenancy/secrets.py` | new | `TenantSecretsPort`, `FileSecrets`, `EnvSecrets` |
| `deps.py` | ext | registry passes `config_source`; fake-mode builder; rebuild-on-change |
| `usage/models.py` | new | Pydantic `UsageWire` (12 keys), `UsageRecord`, `UsageReport`, `BudgetReport`, `QuotaWindow` |
| `usage/store.py` | new | `UsageStore` Protocol, `MemoryUsageStore`, `SqliteUsageStore` (aiosqlite) |
| `usage/aggregate.py` | new | field-wise sums, `cache_hit_ratio`, `mixed` resolution (no pricing) |
| `routes/usage.py` | new | `GET /v1/usage`, `GET /v1/budget` |
| `streaming.py` | ext | native mode + `done.usage`; AG-UI mode (`agui_bridge`) |
| `routes/threads.py` | ext | protocol negotiation (Accept profile / `?protocol=`) |
| `routes/agui.py` | new | `POST /v1/agui` (`RunAgentInput` adapter) |
| `routes/a2a.py` | ext | headers + `org_id` forwarding, versioned card, tenant routes |
| `routes/ws.py` | new | `GET /ws/threads/{id}` |
| `fake/runtime.py` | new | deterministic script (`FakeGraph`) used only in fake mode |
| `routes/admin.py` | new | `/v1/admin/{tenants,audit,decisions,routing}` |
| `config.py` | ext | `host_*` settings of SPEC §10 |

## 4. Data flows

### 4.1 Native turn with usage

```
POST /threads/{id}/messages ─► auth ─► org_id ─► RuntimeRegistry.get(org_id)
      │                                   (TenantConfigSource → build_runtime(org_id, config_source))
      ▼
graph.astream(...) ─► streaming.native_bridge ─► token / tool_call / state frames
      │ end
      ▼
summary = get_run_summary(thread_id)          (engine)
usage   = summary.to_usage_wire() if summary else None
emit `done {thread_id, finish_reason, usage}` ─► UsageStore.append(record)   (if usage)
```

### 4.2 AG-UI turn

```
Accept: text/event-stream; profile=ag-ui   |  ?protocol=ag-ui
      ▼
ctx = AgUiContext(run_id, thread_id, …)   ─► frames(start_run(ctx))
for chunk in graph.astream(..., stream_mode=["messages","updates","custom"]):
      frames(to_agui_events(chunk, ctx))                      # data-only SSE frames
on success: frames(finish_run(ctx, summary=get_run_summary(thread_id)))   # prismal.usage → RUN_FINISHED
on error:   frames(error_run(ctx, code, message)) → close
```

### 4.3 Usage and budget reads

```
GET /v1/usage  ─► UsageStore.query(org_id, thread_id, since, until, cursor, limit)
               ─► aggregate.totals / by_model (whole window) + page of turns
GET /v1/budget ─► ctx = registry.get(org_id)
               ├─ limits/consumed/remaining/status  ← ctx.budget_status(thread_id)   (HRS-BUD-001)
               └─ gateways[]                        ← await ctx.quota_windows()       (HRS-QTA-001)
GET /v1/admin/decisions|routing ─► admin scope ─► ctx.decisions_report() | ctx.routing_report()
```

### 4.4 WebSocket

```
GET /ws/threads/{id} ─► handshake auth ─► accept
loop: recv frame ─► message → run turn (native or AG-UI) → send {"event":…, "data":…} frames
                   cancel  → cancel astream task → {"event":"error","data":{"code":"cancelled"}}
                   ping    → {"event":"pong"}
timer: {"event":"heartbeat"} every sse_heartbeat_s
```

## 5. Design decisions

| ID | Decision | Rationale |
|---|---|---|
| DD-RH2-001 | Usage is an additive key on `done`, never a new event | v0.1 SDKs ignore unknown keys; no breaking change |
| DD-RH2-002 | `usage: null` instead of an error when the summary is missing | Fail-open on telemetry/cost |
| DD-RH2-003 | Host persists per-turn usage records; sums only | Engine owns pricing/metering; history must survive engine run registries |
| DD-RH2-004 | Budget, quota, audit chain, decision and routing reports come only from HRS methods | Never re-implement `BudgetGuard.check`, chain hashing or savings math |
| DD-RH2-005 | Tenant config allowlist by key prefix | A tenant must not relax security settings (fail-closed) |
| DD-RH2-006 | AG-UI selected per request; native stays default | Existing clients unaffected |
| DD-RH2-007 | WS frames reuse SSE event names and payloads | One parser model in the SDK |
| DD-RH2-008 | Fake mode is host-side, production-guarded, labelled by header | Unblocks CSB-05-01 without credentials; cannot be mistaken for real output |
| DD-RH2-009 | Admin endpoints off by default, scope-gated, tenant-filtered | Least privilege for DSH-FUTURE-03/04 |
| DD-RH2-010 | Import guard = HRS §3 table | One engine-owned, versioned allowlist |

Fake mode prefers an engine-provided fake chat model if `build_test_runtime` exposes one;
otherwise `fake/runtime.py::FakeGraph` yields engine-shaped `astream` chunks so the real native and
AG-UI bridges (incl. `to_agui_events`) are exercised.

## 6. Error mapping additions

| Code | HTTP | Raised by |
|---|---|---|
| `tenant_config_invalid` | 500 (opaque) | `TenantConfigSource` load |
| `protocol_not_enabled` | 406 | AG-UI negotiation |
| `invalid_window` | 400 | `since > until`, unparsable timestamps, window > 31 d |
| `forbidden_tenant` | 403 | path tenant ≠ identity tenant |
| `admin_disabled` | 404 | admin routes with `host_admin_enabled=false` |

## 7. Security

- Tenant secrets: `SecretStr`, never mirrored to `os.environ`; secret files must be mode `0600` (warn otherwise).
- Tenant config trust model (SPEC-RH2-TEN-005): `tenant.yaml` and secrets are operator-authored;
  per-tenant budgets/quotas/modes are operator policy. Exact key allowlist as defense in depth;
  tenant selection fails closed (`403 forbidden_tenant`) when tenant config is enabled.
- Usage/budget/admin queries always filtered by the auth-resolved `org_id`.
- AG-UI `STATE_*` content is produced by the engine's allowlisted state view; host adds nothing.
- WS: auth only at handshake; `Origin` checked against `cors_origins`; frame size limit.
- Fake mode: startup refusal in production + response header.

## 8. Observability

Counters `prismal_server.usage_missing_total`, `prismal_server.usage_records_total{store}`,
`prismal_server.agui_streams_total`, `prismal_server.ws_sessions_total{close_code}`,
`prismal_server.tenant_config_reloads_total{outcome}`, `prismal_server.quota_lookup_errors_total{gateway}`.

## 9. Testing

Unit (no network, `build_test_runtime` fakes): config allowlist, secrets redaction canary, usage
fixture contract (vendored engine `usage_wire.json`), aggregation math, budget fallbacks, AG-UI
framing against engine golden events, WS protocol/close codes, fake script determinism, admin gating.
`contract` marker: real `prismal-ai` 3.17 runtime in fake mode — one native turn, one AG-UI turn,
one A2A v1 `tasks/list`, `/v1/usage` after the turn.

## 10. Open questions

1. AG-UI HITL resume endpoint (`POST /threads/{id}/resume`) — needs engine resume contract (v0.3).
2. WS auth for browsers (short-lived ticket) — deferred; header-only in 0.2.
3. Multi-worker deployments need `SqliteUsageStore` on shared storage or Postgres — Postgres adapter later.
