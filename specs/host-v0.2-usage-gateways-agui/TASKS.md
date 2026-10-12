# prismal-server — Task Breakdown (Host v0.2: Usage, Gateways, AG-UI)

| Field | Value |
|---|---|
| **Status** | `READY` |
| **Version** | 1.0 |
| **Date** | 2026-10-04 |
| **Author** | Ernesto Crespo |
| **Companion** | [`PLAN.md`](./PLAN.md) · [`ARCHITECTURE.md`](./ARCHITECTURE.md) · [`SPEC.md`](./SPEC.md) |

Status legend: `TODO` · `WIP` · `DONE` · `BLOCKED`. Every task is test-first (TDD). Tasks map to
`SPEC-RH2-*` IDs (prefix omitted in the SPEC column).

## Prerequisites — verify before build

Engine `prismal-ai` 3.17.0 released with TKE1, GW, PRT (PRT5 usage wire + AG-UI translator, PRT2
dual-stack handler) and **HRS** (`prismal/specs/composition-root/DELTA-2026-10-host-read-surface.md`,
the engine-owned host read surface). Every **[verify]** item is closed by an RH2-00 task — see [`VERIFY-RH2-00.md`](./VERIFY-RH2-00.md) (verified on `prismal-ai` 3.19.0; 3.17.0 was never published to PyPI).

## Phase 0 — Verify engine surfaces

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RH2-00-01 | Bump pin `prismal-ai>=3.17,<4`; record the current four guard entry points | 0.2 d | engine 3.17 | NFR-001/002 | `DONE` |
| RH2-00-02 | Verify `prismal.events.agui` symbols and `get_run_summary` / `to_usage_wire` signatures | 0.2 d | 00-01 | USG-001, AGU-001 | `DONE` |
| RH2-00-03 | Confirm engine 3.17 ships HRS (HRS1-01…09 `DONE`): `build_runtime(config_source=)`, `budget_status`, `quota_windows`, `audit_page`/`verify_audit_chain`, `decisions_report`, `routing_report`, `a2a_routes`, plus handler `headers=`/`org_id=`; copy the HRS §3 table into the guard allowlist. Any missing item ⇒ dependent RH2 task `BLOCKED` (no host fallback logic) | 0.3 d | 00-01, engine HRS | TEN-001, BUD-*, ADM-*, A2A-*, NFR-002 | `DONE` |
| RH2-00-04 | Verify AG-UI SSE encoding and A2A v1 header names against engine `ADR-PRT-001` **[verify]** | 0.2 d | 00-01 | AGU-002, A2A-001 | `DONE` |
| RH2-00-05 | Vendor engine `tests/fixtures/protocols/usage_wire.json` as the shared contract fixture | 0.1 d | 00-02 | USG-002 | `DONE` |

Phase 0 outcome: all HRS surfaces present ⇒ no RH2 task `BLOCKED`. Record: [`VERIFY-RH2-00.md`](./VERIFY-RH2-00.md).

## Phase 1 — Tenant config & gateways

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RH2-01-01 | `config.py`: SPEC §10 settings, validation | 0.3 d | 00-01 | — | `DONE` |
| RH2-01-02 | `tenancy/secrets.py`: `TenantSecretsPort`, file + env backends, `SecretStr` | 0.5 d | 01-01 | TEN-003 | `DONE` |
| RH2-01-03 | `tenancy/config_source.py`: `TenantConfigSource` + prefix allowlist | 0.6 d | 01-02 | TEN-001/002 | `DONE` |
| RH2-01-04 | Registry passes `config_source`; mtime-based rebuild after in-flight turns | 0.5 d | 01-03 | TEN-001/004 | `DONE` |
| RH2-01-05 | Tests: two tenants, different gateways; forbidden key; secret canary redaction | 0.4 d | 01-04 | TEN-* | `DONE` |

Phase 1 decisions (2026-10-12):
- `tenant.yaml` = flat map of engine `Settings` field names → scalars (lists/maps JSON-encoded);
  emitted to the engine as `PRISMAL_<KEY>`. Missing file ⇒ tenant uses process config.
- Validation is **eager**, before `build_runtime`: the engine's `ChainedConfigSource` swallows a
  failing sub-source, which would silently fall back to process settings (not fail-closed).
- Secret keys (`*_api_key`, `*_token`) are refused inline in `tenant.yaml`; they come only from the
  secrets backend and must also match the prefix allowlist. Empty secret ⇒ refused.
- File backend: `<host_secrets_dir>/<org_id>/<key>`, or `<host_tenant_config_dir>/<org_id>/secrets/<key>`
  when `host_secrets_dir` is unset. Env backend: `PRISMAL_SERVER_SECRET__<ORG>__<KEY>` (read-only).
- `org_id` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` (path-traversal guard).
- Reload: mtime fingerprint (tenant.yaml + secret files) checked on each registry lookup; turns
  hold a `Lease`, so a stale or LRU-evicted runtime closes after its last in-flight turn.
- Counter `tenant_config_reloads_total` is logged only for now (no host metrics backend yet) — RH2-07.
- Security review fixes (post-merge, two rounds): the prefix allowlist became an **exact key
  allowlist** (`ALLOWED_KEYS` in `tenancy/config_source.py`, every key also under a SPEC prefix).
  Endpoints/URLs, operator-key fallback, host paths, process-wide resource sizing and
  security/privacy/compliance floors (ZDR, retention/ToS deny-lists, risk floor, HITL and
  calibration gates, remote Ollama, CVE version floors) are refused; engine keys added later are
  refused by default. With tenant config enabled, tenant selection fails closed: `X-Org-Id` ≠
  identity `org_id` ⇒ `403 forbidden_tenant`; header without identity org ⇒ honoured only in dev
  no-auth outside `production`, else `403` (never a silent fallback to the process tenant). Env
  secrets backend accepts only `^[a-z0-9]+(-[a-z0-9]+)*$` org ids (injective mapping).
- Trust model (SPEC-RH2-TEN-005, decided 2026-10-12): tenant config is **operator-authored**. A third
  security review flagged that `budget_*`, `decision_modes` and `opencode_go_monthly_cap_usd` let
  "a tenant" relax cost governance; under this model they are operator per-tenant policy and are
  accepted by design. Self-service tenant config would need a tighten-only layer (future work).

## Phase 2 — Usage & budget

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RH2-02-01 | `usage/models.py` (12-key `UsageWire`, reports, `QuotaWindow`) + fixture contract test | 0.3 d | 00-05 | USG-002 | `TODO` |
| RH2-02-02 | `done.usage` from `get_run_summary(...).to_usage_wire()`; `null` fail-open + counter | 0.4 d | 02-01 | USG-001 | `TODO` |
| RH2-02-03 | `usage/store.py`: memory + SQLite stores, retention, append on `done` | 0.7 d | 02-02 | USE-001 | `TODO` |
| RH2-02-04 | `GET /v1/usage`: window, filter, whole-window totals/by_model, cursor paging | 0.6 d | 02-03 | USE-002/003 | `TODO` |
| RH2-02-05 | `GET /v1/budget`: limits, engine status or `unknown`, quota windows (fail-open) | 0.6 d | 00-03 | BUD-001/002 | `TODO` |

## Phase 3 — AG-UI

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RH2-03-01 | Negotiation (Accept profile / `?protocol=`) and `406 protocol_not_enabled` | 0.3 d | 00-02 | AGU-001/003 | `TODO` |
| RH2-03-02 | `streaming.agui_bridge`: start/translate/finish/error, data-only frames, heartbeat, cancel | 0.7 d | 03-01 | AGU-002/004 | `TODO` |
| RH2-03-03 | `POST /v1/agui` `RunAgentInput` adapter | 0.4 d | 03-02 | AGU-005 | `TODO` |
| RH2-03-04 | Tests: engine golden events framed; `RUN_ERROR`; usage before `RUN_FINISHED`; disconnect | 0.4 d | 03-02 | AGU-* | `TODO` |

## Phase 4 — A2A v1.0

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RH2-04-01 | Forward headers + `org_id` to `handle_rpc` / `stream_rpc` | 0.3 d | 00-03 | A2A-001 | `TODO` |
| RH2-04-02 | Versioned card (`?protocolVersion=`), tenant-scoped routes | 0.5 d | 04-01 | A2A-002/003 | `TODO` |
| RH2-04-03 | Path-tenant mismatch `403`; v0.1 strict/404 behaviours on both stacks | 0.3 d | 04-02 | A2A-003/004 | `TODO` |
| RH2-04-04 | Tests: v0.3 + v1 client smoke, `tasks/list` through the host | 0.4 d | 04-03 | A2A-* | `TODO` |

## Phase 5 — WebSocket

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RH2-05-01 | `routes/ws.py`: handshake auth, `Origin` check, tenant | 0.5 d | 01-04 | WSK-001 | `TODO` |
| RH2-05-02 | Frame protocol, one turn at a time, cancel, close codes | 0.7 d | 05-01, 03-02 | WSK-002/003 | `TODO` |
| RH2-05-03 | Heartbeat, frame-size limit, send backpressure | 0.3 d | 05-02 | WSK-003 | `TODO` |
| RH2-05-04 | Tests (native + AG-UI over WS, 4409/4400/1009/1008) | 0.4 d | 05-03 | WSK-* | `TODO` |

## Phase 6 — Fake-LLM mode & admin

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RH2-06-01 | `host_fake_llm_mode`, production refusal, `X-Prismal-Fake-LLM` header | 0.3 d | 01-01 | FAK-001/003 | `TODO` |
| RH2-06-02 | `fake/runtime.py` deterministic script (or engine fake model) | 0.5 d | 06-01 | FAK-002 | `TODO` |
| RH2-06-03 | Admin gate + `GET /v1/admin/tenants` | 0.4 d | 01-04 | ADM-001/002 | `TODO` |
| RH2-06-04 | `GET /v1/admin/audit` paging + chain status | 0.6 d | 06-03, 00-03 | ADM-003 | `TODO` |
| RH2-06-05 | Tests: script determinism, production refusal, admin 404/403/cross-tenant | 0.3 d | 06-04 | FAK-*, ADM-* | `TODO` |
| RH2-06-06 | `GET /v1/admin/decisions` from `decisions_report()` (+ tests: disabled, null ratio, clamped window) | 0.4 d | 06-03, 00-03 | ADM-004 | `TODO` |
| RH2-06-07 | `GET /v1/admin/routing` from `routing_report()` (+ tests: disabled, 403, field mapping) | 0.4 d | 06-03, 00-03 | ADM-005 | `TODO` |

## Phase 7 — Hardening & release

| ID | Task | Est. | Dep | SPEC | Status |
|---|---|---|---|---|---|
| RH2-07-01 | Import guard: allowlist = HRS §3 public host read surface, nothing else | 0.2 d | all | NFR-002/003 | `TODO` |
| RH2-07-02 | `contract` suite vs released engine in fake mode (native, AG-UI, A2A v1, usage) | 0.4 d | 06-02 | all | `TODO` |
| RH2-07-03 | OpenAPI examples (no secrets) + docs: usage, AG-UI, WS, admin, tenant config | 0.6 d | all | — | `TODO` |
| RH2-07-04 | CHANGELOG, tag `v0.2.0`, GHCR image | 0.3 d | 07-02 | — | `TODO` |

## Test inventory & traceability

| SPEC area | Tasks | Tests |
|---|---|---|
| NFR-001..003 | 00-01, 07-01 | `tests/unit/test_import_guard.py` |
| TEN-001..004 | 01-01..05 | `tests/unit/tenancy/test_{config_source,secrets,reload}.py` |
| USG-001/002 | 00-05, 02-01, 02-02 | `tests/unit/usage/test_usage_wire_contract.py`, `tests/unit/test_streaming_done_usage.py` |
| USE-001..003 | 02-03, 02-04 | `tests/unit/usage/test_{store,aggregate}.py`, `tests/unit/routes/test_usage.py` |
| BUD-001/002 | 02-05 | `tests/unit/routes/test_budget.py` |
| AGU-001..005 | 03-01..04 | `tests/unit/test_streaming_agui.py`, `tests/unit/routes/test_agui_input.py` |
| A2A-001..004 | 04-01..04 | `tests/unit/routes/test_a2a_v1.py` |
| WSK-001..003 | 05-01..04 | `tests/unit/routes/test_ws.py` |
| FAK-001..003 | 06-01, 06-02, 06-05 | `tests/unit/fake/test_fake_runtime.py` |
| ADM-001..005 | 06-03..07 | `tests/unit/routes/test_admin.py`, `tests/unit/routes/test_admin_reports.py` |
| all (live engine) | 07-02 | `tests/contract/test_v02_contract.py` (`contract` marker) |

## Risk register

| Risk | Mitigation |
|---|---|
| HRS slips from 3.17 | RH2-00-03 marks dependent tasks `BLOCKED`; release waits — host never re-implements engine reads |
| Usage key drift between repos | Vendored engine fixture; contract test in host and SDK |
| SQLite store contention under load | Batched writes; Postgres adapter tracked as follow-up |
| WS proxies drop idle connections | App-level heartbeat at `sse_heartbeat_s` |

## Definition of Done (v0.2)

- Every MUST in SPEC has ≥ 1 task and ≥ 1 test; import guard green with the extended allowlist.
- `done.usage` matches the shared fixture byte-for-byte in key order; `/v1/usage` and `/v1/budget`
  match SPEC §4 examples in shape.
- AG-UI stream validated by a reference AG-UI client; native stream unchanged except `usage`.
- `prismal-sdk` contract suite (CSB-05-01 / CS2-05-01) green against `host_fake_llm_mode=true`.
- No tenant secret in logs/bodies (canary test); admin endpoints off by default.

## Estimate roll-up

RH2-0 1.0 d · RH2-1 2.3 d · RH2-2 2.6 d · RH2-3 1.8 d · RH2-4 1.5 d · RH2-5 1.9 d · RH2-6 2.9 d ·
RH2-7 1.5 d → **~15.5 person-days**.

---

## Change History

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-10-04 | Ernesto Crespo | Initial task breakdown for host v0.2 |
| 1.1 | 2026-10-12 | Ernesto Crespo | Phase 0 `DONE` (engine 3.19.0 verified; see VERIFY-RH2-00.md) |
| 1.2 | 2026-10-12 | Ernesto Crespo | Phase 1 `DONE` (tenant config & gateways) |
