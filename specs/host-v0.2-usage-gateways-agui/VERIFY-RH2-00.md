# prismal-server — Phase RH2-0 verification record (engine surfaces)

| Field | Value |
|---|---|
| **Status** | `DONE` |
| **Date** | 2026-10-12 |
| **Tasks** | RH2-00-01 … RH2-00-05 |
| **Engine verified** | `prismal-ai` **3.19.0** (PyPI wheel). 3.17.0 was never published to PyPI (releases jump 3.12.0 → 3.18.0); the pin `>=3.17,<4` resolves to 3.19.0. |
| **Executable record** | `tests/unit/test_engine_surface.py`, `tests/unit/test_usage_wire_fixture.py`, `tests/unit/test_import_guard.py` |

Every **[verify]** item in `SPEC.md` / `ARCHITECTURE.md` is closed here. Where the verified fact
differs from the SPEC text, this record (and the engine's `docs/adr/ADR-PRT-001-protocol-pins.md`)
is authoritative; the SPEC keeps its text with a pointer to this file.

## RH2-00-01 — Pin and current guard

- `pyproject.toml`: `prismal-ai>=3.10.2,<4` → `prismal-ai>=3.17,<4` (SPEC-RH2-NFR-001).
- Guard entry points before v0.2 (v0.1, RHB-05-01): `prismal.composition`, `prismal.agents.graph`,
  `prismal.a2a`, `prismal.identity` (prefix match). Unit (122) and contract (4 + 1 skipped for no
  credentials) suites are green on 3.19.0 with no host code change.

## RH2-00-02 — Usage and AG-UI symbols

| Symbol | Verified signature |
|---|---|
| `prismal.monitoring.get_run_summary` | `(session_id: str, *, turn: Literal["last","all"]="last") -> RunSummary \| None` |
| `RunSummary.to_usage_wire` | `() -> dict[str, Any]` — the 12 keys of SPEC-RH2-USG-002, in order; empty `served_model`/`gateway` ⇒ `None` |
| `prismal.events.agui.AgUiContext` | `(run_id, thread_id, include_state_deltas=True, include_usage=True, include_metadata=True, parent_run_id=None, state_keys=…, …)` |
| `start_run` | `(ctx, *, initial_state=None, input=None) -> list[AgUiEvent]` |
| `to_agui_events` | `(chunk, ctx) -> list[AgUiEvent]` (pure, sync) |
| `finish_run` | `(ctx, *, summary: RunSummary \| None = None, result=None) -> list[AgUiEvent]` |
| `error_run` | `(ctx, *, code: str, message: str, summary=None) -> list[AgUiEvent]` |
| `AGUI_PROTOCOL_VERSION` | `"1.0"` |

## RH2-00-03 — Host read surface (HRS) present

All HRS1 items ship in 3.19.0:

| HRS | Verified |
|---|---|
| SPEC-HRS-CMP-001 | `build_runtime(..., *, org_id=None, config_source: ConfigSourcePort \| None = None, ...)` |
| SPEC-HRS-BUD-001 | `RuntimeContext.budget_status(*, thread_id=None) -> BudgetStatusView` |
| SPEC-HRS-QTA-001 | `async RuntimeContext.quota_windows() -> list[QuotaWindowView]` |
| SPEC-HRS-AUD-001/002 | `audit_page(*, page=1, page_size=50, since=None)`, `verify_audit_chain(*, max_records=10000)` |
| SPEC-HRS-DEC-001 | `decisions_report(*, since=None, until=None) -> DecisionsReportView` |
| SPEC-HRS-RTE-001 | `routing_report(*, since=None, until=None) -> RoutingReportView` |
| SPEC-HRS-A2A-001 | `a2a_routes(*, protocol_version: Literal["0.3","1.0"] \| None = None) -> A2ARouteSpec` |
| PRT2 handler | `A2AServerHandler.handle_rpc/stream_rpc(request, *, auth_ctx=None, headers=None, org_id=None)` |
| Card | `build_agent_card(settings, registry, *, org_id=None, did=None, protocol_version=None)` |
| View types | all 12 of HRS §4 in `prismal.composition.host_surface` |

**No dependent RH2 task is `BLOCKED`.**

Guard allowlist (SPEC-RH2-NFR-002) now = HRS §3:

| Row | Module | Guard rule |
|---|---|---|
| 1 | `prismal.composition` (incl. `host_surface`, row 6) | prefix (unchanged) |
| 2 | `prismal.agents.graph` | prefix (unchanged) |
| 3 | `prismal.a2a` | prefix (unchanged) |
| 4 | `prismal.events.agui` | **symbol-restricted**: the six names above, `from … import` only |
| 5 | `prismal.monitoring` | **symbol-restricted**: `get_run_summary` only |
| — | `prismal.identity` | prefix — **kept**: used by `auth/*` (v0.1 Identity seam) and HRS §3 forbids silently removing an existing entry point. Open item for RH2-07-01: ask the engine to add it as an HRS row, or keep it as a documented host exception. |

## RH2-00-04 — Wire encodings and names (amends SPEC)

| SPEC item | Verified (ADR-PRT-001) | SPEC impact |
|---|---|---|
| AGU-002 AG-UI SSE encoding **[verify]** | `data: <camelCase JSON>\n\n`, no `event:` line | Confirmed as written |
| AGU-004 usage in AG-UI | `CUSTOM` `prismal.usage` (12-key wire) **and** native `RUN_FINISHED.usage[]` (`TokenUsage[]`); both produced by `finish_run(summary=…)` | Host passes `summary`; adds nothing |
| A2A v1 version header **[verify]** | `A2A-Version: <Major.Minor>` (also `?A2A-Version=`); **missing/empty ⇒ 0.3**; otherwise PascalCase method family ⇒ 1.0 | Host forwards headers verbatim (A2A-001) |
| A2A v1 list method (SPEC/PLAN say `tasks/list`) | **`ListTasks`** (v1 methods are PascalCase: `SendMessage`, `GetTask`, `ListTasks`, `CancelTask`, …) | Read `tasks/list` in RF-RH2-005, A2A-001 and RH2-04-04 as `ListTasks` |
| A2A v1 tenant mismatch | engine answers JSON-RPC **-32001** and audits `a2a.inbound.tenant_mismatch`; the host-side `403` of A2A-003 applies only to the *path* tenant vs auth `org_id` check before the handler | Unchanged; both layers hold |
| A2A content type | `application/a2a+json` preferred on HTTP | Note for RH2-04 |
| A2A version string | `"1.0"` (not `"1.0.0"`) | Matches `?protocolVersion=0.3\|1.0` |

## RH2-00-05 — Shared usage fixture

`tests/fixtures/protocols/usage_wire.json` vendored verbatim from engine
`tests/fixtures/protocols/usage_wire.json` (engine commit `3af4e02`, 2026-10-06, `since: 3.17.0`).
`tests/unit/test_usage_wire_fixture.py` checks fixture ⇔ SPEC table ⇔ installed
`RunSummary.to_usage_wire()` key order.

## Notes for later phases

- `build_test_runtime()` has **no** `config_source` parameter — fake mode (RH2-06) cannot combine
  with per-tenant vault config; fake runtimes ignore `host_tenant_config_dir`.
- No engine fake chat model is exposed through the allowed surface ⇒ RH2-06-02 uses the host
  `fake/runtime.py::FakeGraph` path (ARCHITECTURE §5 fallback).
- Engine AG-UI golden streams live in `prismal/tests/fixtures/protocols/agui/`; vendor them in RH2-03-04.
