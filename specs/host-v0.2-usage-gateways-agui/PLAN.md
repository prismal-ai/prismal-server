# prismal-server — Plan (Host v0.2: Usage, Gateways, AG-UI)

| Field | Value |
|---|---|
| **Author** | Ernesto Crespo |
| **Status** | `READY` |
| **Version** | 1.0 |
| **Date** | 2026-10-04 |
| **Target version** | `0.2.0` |
| **Prefix** | `RH2` (requirements `SPEC-RH2-<AREA>-NNN`, tasks `RH2-0x-yy`) |
| **Companion** | [`SPEC.md`](./SPEC.md) · [`ARCHITECTURE.md`](./ARCHITECTURE.md) · [`TASKS.md`](./TASKS.md) |
| **Depends on** | `prismal-ai>=3.17,<4` — TKE1 (usage fields), GW (gateways, quota), PRT (A2A v1, AG-UI translator, usage wire), HRS (host read surface), DEC/AMR (report sources) |

---

## 1. Executive summary

`prismal-server` v0.1.0 is a thin FastAPI host: SSE chat (`token`/`tool_call`/`state`/`done`/`error`),
inbound A2A v0.3, pluggable auth (none/bearer/OIDC), a per-`org_id` `RuntimeRegistry` (LRU), and an
AST import guard allowing only four engine entry points. v0.2 exposes what the 2026-10 engine wave
adds — per-tenant gateways, cache-aware usage and cost, AG-UI 1.0, A2A v1.0 — and closes two
operational gaps: a deterministic **fake-LLM mode** so `prismal-sdk` contract tests (CSB-05-01,
`BLOCKED` because the available host answers `internal_error` without LLM credentials) can run, and
read-only **admin endpoints** that unblock dashboard DSH-FUTURE-03/04.

Hard boundary (unchanged): the host owns transport, auth, tenancy, persistence of returned records
and framing. It never prices, meters, classifies budgets, routes models or translates graph chunks —
those come from the engine.

## 2. Context & problem

- `done` carries only `{thread_id, finish_reason}`; clients cannot show tokens, cache hits or cost.
- No per-tenant gateway/decision/routing configuration path: every tenant shares the process settings.
- Custom SSE only; AG-UI clients (CopilotKit et al.) cannot attach; sub-agent progress is invisible.
- A2A v0.3 only; v1.0 peers (released 2026-03-12 **[verify]**) cannot call the host.
- WebSocket transport was deferred in v0.1 (ADR-002 fast-follow).
- SDK contract suite blocked by missing credentials; dashboard budget/audit viewers blocked by
  missing read endpoints.

## 3. Requirements (RF)

| ID | Requirement | Prio |
|---|---|---|
| RF-RH2-001 | Per-tenant gateway + decision + routing config injected via `ConfigSourcePort` from a host vault | `MUST` |
| RF-RH2-002 | `done` SSE event carries the 12-key `usage` object defined by the engine (`SPEC-PRT-USG-001`) | `MUST` |
| RF-RH2-003 | `GET /v1/usage` and `GET /v1/budget` per org | `MUST` |
| RF-RH2-004 | AG-UI stream mode on `POST /threads/{id}/messages` via engine `to_agui_events` | `MUST` |
| RF-RH2-005 | A2A v1.0 dual-stack routes incl. `tasks/list` | `MUST` |
| RF-RH2-006 | WebSocket transport `GET /ws/threads/{id}` | `SHOULD` |
| RF-RH2-007 | `host_fake_llm_mode` for credential-free contract tests | `MUST` |
| RF-RH2-008 | Admin read endpoints `GET /v1/admin/{audit,tenants,decisions,routing}` behind an admin scope | `SHOULD` |
| RF-RH2-009 | Import guard = engine public host read surface (`SPEC-HRS-SUR-001`), nothing else | `MUST` |

## 4. Goals & success metrics

| Goal | Metric | Target |
|---|---|---|
| Usage visibility | `done` events with non-null `usage` when engine summary exists | 100 % |
| Wire agreement | Server/SDK/engine usage fixture contract test | green in all three repos |
| AG-UI | Golden AG-UI stream parsed by a reference AG-UI client | 100 % events accepted |
| Contract tests | SDK `contract` suite against `host_fake_llm_mode=true` | green, 0 credentials |
| Isolation | Cross-tenant leakage in usage/budget/admin/tool-cache tests | 0 |
| Overhead | Added p95 latency per streamed event (AG-UI vs native) | ≤ 1 ms |

## 5. Scope

**In:** the nine RFs above; `HostSettings` additions; OpenAPI update; docs; release `0.2.0`.
**Out:** engine changes (tracked in engine specs); WebSocket browser tickets; AG-UI HITL resume
endpoint (needs engine `Command(resume=…)` contract — v0.3); writes to admin data; billing/invoicing;
an `AuthorizationPrompter` for outbound A2A PKCE (outbound PKCE peers fail closed in 0.2).

## 6. Phases (milestones)

| Phase | Focus | Est. |
|---|---|---|
| RH2-0 | Verify engine 3.17 surfaces; pin `prismal-ai>=3.17,<4` | 1.0 d |
| RH2-1 | Tenant config & gateways (vault → `ConfigSourcePort` → `build_runtime(org_id)`) | 2.3 d |
| RH2-2 | Usage in `done`; `/v1/usage`; `/v1/budget` | 2.6 d |
| RH2-3 | AG-UI stream mode | 1.8 d |
| RH2-4 | A2A v1.0 dual-stack routes | 1.5 d |
| RH2-5 | WebSocket transport | 1.9 d |
| RH2-6 | Fake-LLM mode + admin endpoints (audit, tenants, decisions, routing) | 2.9 d |
| RH2-7 | Guard, contract suite, docs, release 0.2.0 | 1.5 d |
| **Total** | | **~15.5 d** |

Critical path: RH2-0 → RH2-1 → RH2-2 → RH2-7. RH2-6 (fake mode) should land early because it unblocks
the SDK; RH2-3/4/5 parallelise after RH2-0.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Engine read surface (HRS) slips from 3.17 | RH2-00-03 gates on HRS; dependent tasks `BLOCKED`, never re-implemented host-side |
| Fake mode reaches production | Startup refuses `host_fake_llm_mode` when `host_environment="production"`; every response carries `X-Prismal-Fake-LLM: 1` |
| Tenant config overrides security settings | Key allowlist by prefix; unknown keys rejected at load (fail-closed) |
| Admin endpoints leak cross-tenant data | Admin scope + per-tenant filter by default; `org_id=*` only with `prismal:admin:global` |
| AG-UI / A2A v1 names change | All names come from the engine; host has no protocol constants of its own |

## 8. References

Engine: `prismal/specs/protocol-upgrades-2026/`, `token-efficiency/`, `llm-gateway-providers/`.
Host v0.1: `specs/reference-host-bootstrap/`. SDK: `prismal-sdk/specs/client-v0.2-usage-agui/`.
AG-UI 1.0 — https://www.copilotkit.ai/blog/ag-ui-1.0 · A2A — https://github.com/a2aproject/A2A/releases

---

## Change History

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-10-04 | Ernesto Crespo | Initial plan for host v0.2 |
