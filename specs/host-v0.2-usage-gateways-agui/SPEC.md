# prismal-server — Technical Specification (Host v0.2: Usage, Gateways, AG-UI)

| Field | Value |
|---|---|
| **Status** | `READY` |
| **Version** | 1.0 |
| **Date** | 2026-10-04 |
| **Author** | Ernesto Crespo |
| **Companion** | [`PLAN.md`](./PLAN.md) · [`ARCHITECTURE.md`](./ARCHITECTURE.md) · [`TASKS.md`](./TASKS.md) |

Requirement IDs are `SPEC-RH2-<AREA>-NNN`; priority per RFC 2119. Acceptance criteria are EARS.
v0.1 requirements (`SPEC-RHB-*`) stay in force unless explicitly MODIFIED here.
**[verify]** items are closed in [`VERIFY-RH2-00.md`](./VERIFY-RH2-00.md) (engine ADR-PRT-001): the
A2A v1 list method is `ListTasks` (read every `tasks/list` below as `ListTasks`), the version header
is `A2A-Version` (missing ⇒ 0.3), and the AG-UI SSE encoding is confirmed as data-only frames. Wire objects in §3–§4
are normative for `prismal-sdk` (`SPEC-CS2-*`) — same names, same types, same order.

---

## 1. Engine boundary

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RH2-NFR-001 | The engine dependency MUST be pinned `prismal-ai>=3.17,<4`. | `MUST` |
| SPEC-RH2-NFR-002 | The import guard MUST allow exactly the engine **public host read surface** of `SPEC-HRS-SUR-001` (`prismal/specs/composition-root/DELTA-2026-10-host-read-surface.md` §3: `prismal.composition` build functions, `get_async_compiled_graph`, `prismal.a2a` card inputs, `prismal.events.agui`, `prismal.monitoring.get_run_summary`, `prismal.composition.host_surface` view types). Any other `prismal` import fails CI; all other engine reads go through `RuntimeContext` methods (HRS §4). | `MUST` |
| SPEC-RH2-NFR-003 | The host MUST NOT price, meter, classify budgets, select models or translate graph chunks; it only calls engine functions/objects and persists or sums their returned records. | `MUST` |

**EARS:** WHEN a module under `src/` imports a `prismal` symbol outside the allowlist, THE SYSTEM SHALL
fail the `test_import_guard` CI job.

## 2. Tenant configuration & gateways

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RH2-TEN-001 | WHERE `host_tenant_config_dir` is set, the host MUST build a per-tenant `TenantConfigSource` (structurally a `ConfigSourcePort`: `load() -> Mapping[str, str \| SecretStr]`) from `<dir>/<org_id>/tenant.yaml` plus secrets from `TenantSecretsPort`, and pass it to `build_runtime(org_id=…, config_source=…)` (`SPEC-HRS-CMP-001`). | `MUST` |
| SPEC-RH2-TEN-002 | Only keys with allowed prefixes MUST load (`llm_gateway_`, `openrouter_`, `opencode_`, `omniroute_`, `decision`, `jev_`, `laya_`, `adaptive_routing_`, `routing_`, `plan_cache_`, `budget_`, `agui_`, `a2a_protocol_version`); any other key MUST fail that tenant's load with `tenant_config_invalid` (fail-closed). | `MUST` |
| SPEC-RH2-TEN-003 | Secrets (`*_api_key`, `*_token`) MUST be `SecretStr`, resolved from `TenantSecretsPort` (`file` or `env` backend), never written to `os.environ`, logs, error bodies or OpenAPI examples. | `MUST` |
| SPEC-RH2-TEN-004 | WHEN a tenant's `tenant.yaml` or secret file mtime changes, THE SYSTEM SHALL close that tenant's `RuntimeContext` after in-flight turns finish and rebuild it on next use. | `SHOULD` |
| SPEC-RH2-TEN-005 | Trust model: `tenant.yaml` and tenant secrets are **operator-authored** (host vault owned by the operator; tenants never edit them). Budget limits, gateway quotas and decision/routing modes set there are operator policy per tenant. The key set is an exact allowlist (defense in depth, see `tenancy/config_source.py::ALLOWED_KEYS`) that never admits endpoints/URLs, operator-key fallback, host paths, process resource sizing or security/privacy/compliance floors. Tenant self-service config is out of scope for 0.2. | `MUST` |

## 3. Usage in the `done` event (MODIFIES SPEC-RHB-CHT-002)

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RH2-USG-001 | The `done` event `data` MUST be `{"thread_id", "finish_reason", "usage"}` where `usage` is `get_run_summary(thread_id).to_usage_wire()` or `null` when no summary exists (fail-open; never an `error` event). | `MUST` |
| SPEC-RH2-USG-002 | The `usage` object MUST contain exactly these 12 keys in this order. | `MUST` |

| Key | Type | Meaning |
|---|---|---|
| `prompt_tokens` | int | All input tokens, incl. cached |
| `completion_tokens` | int | All output tokens, incl. reasoning |
| `cache_read_tokens` | int | Subset of prompt served from cache |
| `cache_write_tokens` | int | Subset of prompt written to cache |
| `reasoning_tokens` | int | Subset of completion |
| `total_tokens` | int | prompt + completion |
| `calls` | int | LLM calls in the turn |
| `cost_usd` | float | USD, 6 decimals |
| `cost_source` | string | `gateway`\|`litellm`\|`table`\|`subscription`\|`none`\|`mixed` |
| `estimated` | bool | false only if all calls priced by gateway/litellm |
| `served_model` | string\|null | Model of the last call |
| `gateway` | string\|null | Gateway name; null = direct provider |

```
event: done
data: {"thread_id":"t-42","finish_reason":"stop","usage":{"prompt_tokens":1200,"completion_tokens":340,"cache_read_tokens":900,"cache_write_tokens":0,"reasoning_tokens":0,"total_tokens":1540,"calls":3,"cost_usd":0.004213,"cost_source":"gateway","estimated":false,"served_model":"anthropic/claude-sonnet-4.5","gateway":"openrouter"}}
```

**EARS:** WHEN a turn completes and the engine summary is unavailable, THE SYSTEM SHALL emit `done`
with `"usage": null` and increment `prismal_server.usage_missing_total`.

## 4. Usage & budget endpoints

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RH2-USE-001 | WHEN `done` is emitted with non-null usage, THE SYSTEM SHALL append a `UsageRecord(org_id, thread_id, turn_id, finished_at, finish_reason, usage)` to the `UsageStore` (`memory` \| `sqlite`, retention `host_usage_retention_days`). | `MUST` |
| SPEC-RH2-USE-002 | `GET /v1/usage?thread_id=&since=&until=&limit=&cursor=` MUST return the caller's org only; `since` defaults to now−24 h, `until` to now (RFC 3339 UTC); `limit` 1–500 (default 100). | `MUST` |
| SPEC-RH2-USE-003 | `totals` and `by_model` MUST be computed over the whole window (not the page) by field-wise sum; `estimated` = OR; `cost_source` = common value or `mixed`; `totals.served_model`/`gateway` = `null`. `cache_hit_ratio` = `cache_read_tokens / prompt_tokens` rounded to 4 decimals (0.0 when prompt is 0). | `MUST` |
| SPEC-RH2-BUD-001 | `GET /v1/budget?thread_id=` MUST serialize `RuntimeContext.budget_status(thread_id=)` (`SPEC-HRS-BUD-001`) field-for-field; `status`, `breached_dimension` and `remaining` are never computed host-side. | `MUST` |
| SPEC-RH2-BUD-002 | `gateways` MUST be `await RuntimeContext.quota_windows()` (`SPEC-HRS-QTA-001`) serialized field-for-field; failed gateways are already omitted by the engine. | `MUST` |

`GET /v1/usage` → `200`:
```json
{"org_id":"acme","thread_id":null,"since":"2026-10-03T12:00:00Z","until":"2026-10-04T12:00:00Z",
 "totals":{"prompt_tokens":52000,"completion_tokens":9100,"cache_read_tokens":39000,"cache_write_tokens":4000,"reasoning_tokens":1200,"total_tokens":61100,"calls":140,"cost_usd":0.412345,"cost_source":"mixed","estimated":true,"served_model":null,"gateway":null},
 "cache_hit_ratio":0.75,
 "by_model":[{"prompt_tokens":40000,"completion_tokens":7000,"cache_read_tokens":32000,"cache_write_tokens":3000,"reasoning_tokens":1200,"total_tokens":47000,"calls":90,"cost_usd":0.39,"cost_source":"gateway","estimated":false,"served_model":"anthropic/claude-sonnet-4.5","gateway":"openrouter"}],
 "turns":[{"thread_id":"t-42","turn_id":"01J…","finished_at":"2026-10-04T11:59:58Z","finish_reason":"stop","usage":{"…":"12-key usage object"}}],
 "next_cursor":null}
```

`GET /v1/budget` → `200`:
```json
{"org_id":"acme","enabled":true,"scope":"tenant","status":"within","breached_dimension":null,
 "soft_ratio":0.8,"hard_cap":true,
 "limits":{"tokens":2000000,"cost_usd":50.0,"calls":0,"wall_clock_s":0.0},
 "consumed":{"tokens":61100,"cost_usd":0.412345,"calls":140,"wall_clock_s":312.5},
 "remaining":{"tokens":1938900,"cost_usd":49.587655,"calls":null,"wall_clock_s":null},
 "gateways":[{"gateway":"openrouter","window":"credit","unit":"usd","limit":50.0,"used":8.8,"remaining":41.2,"resets_at":null,"source":"api","as_of":"2026-10-04T12:00:00Z"},
             {"gateway":"opencode_go","window":"5h","unit":"usd_equivalent","limit":2.0,"used":0.6,"remaining":1.4,"resets_at":"2026-10-04T15:10:00Z","source":"ledger","as_of":"2026-10-04T12:00:00Z"}],
 "as_of":"2026-10-04T12:00:00Z"}
```
Enums: `status` ∈ `within|soft_exceeded|hard_exceeded|disabled|unknown`; `scope` ∈ `turn|session|tenant`;
`breached_dimension` ∈ `tokens|cost_usd|calls|wall_clock_s|null`; a limit of `0` means unlimited and its
`remaining` is `null`; `window` ∈ `credit|5h|daily|weekly|monthly`; `unit` ∈ `usd|usd_equivalent|requests|tokens`;
`source` ∈ `api|ledger`; timestamps RFC 3339 UTC. With `budget_enabled=false`: `enabled:false`,
`status:"disabled"`, and `limits`, `remaining`, `soft_ratio`, `hard_cap` are `null`; `consumed` is still reported. WHERE scope is `turn`/`session` and no `thread_id` is given,
`consumed` is `null` and `status` is `"unknown"`.

## 5. AG-UI stream mode

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RH2-AGU-001 | WHEN `POST /threads/{id}/messages` carries `Accept: text/event-stream; profile=ag-ui` or `?protocol=ag-ui`, THE SYSTEM SHALL stream AG-UI events produced only by engine `start_run` / `to_agui_events` / `finish_run` / `error_run`. | `MUST` |
| SPEC-RH2-AGU-002 | Each AG-UI event MUST be one SSE frame `data: <json>\n\n` with no `event:` line (AG-UI SSE encoding — verified, ADR-PRT-001 §2.3); heartbeat comments continue; response header `X-Prismal-Stream-Protocol: ag-ui`. | `MUST` |
| SPEC-RH2-AGU-003 | IF the tenant's engine `agui_events_enabled` is false or `host_agui_enabled` is false, THEN THE SYSTEM SHALL answer `406 {"error":{"code":"protocol_not_enabled",…}}` before streaming. | `MUST` |
| SPEC-RH2-AGU-004 | A mid-stream failure MUST be delivered as the engine `error_run` events (`RUN_ERROR`, `code`/`message` identical to the native `error` event) followed by close; usage MUST arrive as `CUSTOM` `prismal.usage` (12-key object) before `RUN_FINISHED`. | `MUST` |
| SPEC-RH2-AGU-005 | `POST /v1/agui` SHOULD accept an AG-UI `RunAgentInput` (`threadId`, `runId`, `messages`, …); the last `user` message content becomes the turn input; client-declared `tools` are ignored and logged in 0.2. | `SHOULD` |

Default (no header/param) remains the v0.1 native SSE set — byte-for-byte except the additive `usage` key.

## 6. A2A v1.0 dual-stack

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RH2-A2A-001 | `POST /a2a` MUST pass request headers and the auth-resolved `org_id` to `a2a_handler.handle_rpc(..., headers=, org_id=)` / `stream_rpc(...)`; version negotiation, `tasks/list` and tenant checks live in the engine. | `MUST` |
| SPEC-RH2-A2A-002 | `GET /.well-known/agent-card.json` MUST serve the card for the engine's `a2a_protocol_version`; `?protocolVersion=0.3\|1.0` MAY request the other stack while `a2a_accept_legacy` is true. | `MUST` |
| SPEC-RH2-A2A-003 | The host MUST mount tenant-scoped A2A routes from `RuntimeContext.a2a_routes()` (`SPEC-HRS-A2A-001`), which match the card's interface URLs; IF the path tenant differs from the auth-resolved `org_id`, THEN THE SYSTEM SHALL return `403`. | `MUST` |
| SPEC-RH2-A2A-004 | v0.1 behaviours SPEC-RHB-A2A-003/004/005 (no re-sanitize, strict `-32001`, `404` when disabled) MUST hold for both stacks. | `MUST` |

## 7. WebSocket transport

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RH2-WSK-001 | WHERE `host_ws_enabled`, `GET /ws/threads/{id}` MUST upgrade after auth on the handshake (`Authorization` header; strict mode ⇒ close `1008`). | `SHOULD` |
| SPEC-RH2-WSK-002 | Client frames: `{"type":"message","content","role","metadata","protocol":"prismal"\|"ag-ui"}`, `{"type":"cancel"}`, `{"type":"ping"}`. Server frames: `{"event":<name>,"data":<object>}` with `name` ∈ native SSE names (`token`, `tool_call`, `state`, `done`, `error`) and identical `data`, or `"agui"` with an AG-UI event as `data`; plus `{"event":"heartbeat"}` and `{"event":"pong"}`. | `SHOULD` |
| SPEC-RH2-WSK-003 | One turn at a time per connection (`message` during a turn ⇒ close `4409`); `cancel` stops the `astream` task and yields `error` `{"code":"cancelled"}`; malformed frame ⇒ close `4400`; frame > `host_ws_max_message_bytes` ⇒ close `1009`. | `SHOULD` |

## 8. Fake-LLM mode

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RH2-FAK-001 | WHERE `host_fake_llm_mode` is true, the registry MUST build runtimes with `build_test_runtime()` and serve chat from a deterministic script; IF `host_environment="production"`, THEN startup MUST fail. | `MUST` |
| SPEC-RH2-FAK-002 | Script: `/tool <q>` ⇒ one `tool_call {"name":"fake_tool","args":{"q":<q>}}`; `/error` ⇒ terminal `error {"code":"internal_error","message":"fake error"}`; otherwise reply text `"echo: " + content[:200]` streamed in `token` chunks of ≤ 16 chars; `done.usage` = `{"prompt_tokens":10,"completion_tokens":5,"cache_read_tokens":0,"cache_write_tokens":0,"reasoning_tokens":0,"total_tokens":15,"calls":1,"cost_usd":0.0,"cost_source":"none","estimated":true,"served_model":"fake/echo","gateway":null}`. | `MUST` |
| SPEC-RH2-FAK-003 | Every response (HTTP, SSE, WS handshake) MUST carry `X-Prismal-Fake-LLM: 1`; `/readyz` stays `{"status":"ready"}`. | `MUST` |

## 9. Admin read endpoints

| ID | Requirement | Prio |
|---|---|---|
| SPEC-RH2-ADM-001 | WHERE `host_admin_enabled` is false (default) admin routes MUST return `404`; otherwise the identity MUST hold scope `host_admin_scope` (default `prismal:admin`) or get `403`. | `SHOULD` |
| SPEC-RH2-ADM-002 | `GET /v1/admin/tenants` → `{"tenants":[{"org_id","live","created_at","last_used_at","gateways":[…],"a2a_protocol_version","config_source":"vault"\|"process"}],"max_tenants":int}`; no secret values. | `SHOULD` |
| SPEC-RH2-ADM-003 | `GET /v1/admin/audit?org_id=&since=&page=&page_size=` (1-based, `page_size` ≤ 200, default 50) → `{"page","page_size","total","entries":[{"seq","ts","event","actor","org_id","hash","prev_hash","fields"}],"chain":{"status":"valid"\|"broken"\|"unverified","checked":int,"first_broken_seq":int\|null}}` from `RuntimeContext.audit_page()` + `verify_audit_chain()` (`SPEC-HRS-AUD-001/002`). `org_id=*` requires `prismal:admin:global`. | `SHOULD` |
| SPEC-RH2-ADM-004 | `GET /v1/admin/decisions?org_id=&since=&until=` MUST serialize `RuntimeContext.decisions_report()` (`SPEC-HRS-DEC-001`) as below; defaults `since`=now−7 d, `until`=now. | `SHOULD` |
| SPEC-RH2-ADM-005 | `GET /v1/admin/routing?org_id=&since=&until=` MUST serialize `RuntimeContext.routing_report()` (`SPEC-HRS-RTE-001`) as below; savings are the engine's frontier-counterfactual estimate, never recomputed. | `SHOULD` |

`GET /v1/admin/decisions` → `200`:
```json
{"org_id":"acme","enabled":true,"window":{"since":"2026-09-27T12:00:00Z","until":"2026-10-04T12:00:00Z"},
 "integrations":[{"integration":"routing","mode":"shadow","samples":2412,"agreement_ratio":0.974,
   "accepted":1830,"escalated":402,"abstained":180,"llm_calls_avoided":1830,
   "window":{"since":"2026-09-30T08:15:00Z","until":"2026-10-04T12:00:00Z"}}],
 "as_of":"2026-10-04T12:00:00Z"}
```
`GET /v1/admin/routing` → `200`:
```json
{"org_id":"acme","enabled":true,"mode":"shadow","window":{"since":"…","until":"…"},
 "policies":[{"policy":"composite","decisions":5120,"decisions_by_tier":{"small":3900,"medium":1000,"frontier":220},
   "escalations":140,"escalations_by_reason":{"inadequate":90,"structured_invalid":40,"tool_errors":10},
   "plan_cache_hits":610,"estimated_savings_usd":12.43,"window":{"since":"…","until":"…"}}],
 "as_of":"2026-10-04T12:00:00Z"}
```
`mode` ∈ `off|shadow|enforce` (decisions) / `shadow|enforce` (routing); `agreement_ratio` is `null` when
`samples` is 0; row `window.since` may be later than requested (engine clamps to retained stats).
Disabled features return `200` with `"enabled":false` and empty `integrations`/`policies`.

## 10. Configuration (`HostSettings`, prefix `PRISMAL_SERVER_`)

| Key | Default | Meaning |
|---|---|---|
| `host_environment` | `development` | `development`\|`staging`\|`production` |
| `host_tenant_config_dir` | `None` | Per-tenant config root |
| `host_secrets_backend` / `host_secrets_dir` | `file` / `None` | `file`\|`env` tenant secrets |
| `host_usage_store` / `host_usage_db_path` / `host_usage_retention_days` | `memory` / `usage.db` / `90` | Usage persistence |
| `host_agui_enabled` | `true` | Host-side AG-UI gate (engine flag also required) |
| `host_ws_enabled` / `host_ws_max_message_bytes` | `false` / `1048576` | WebSocket |
| `host_fake_llm_mode` | `false` | Deterministic fake runtime |
| `host_admin_enabled` / `host_admin_scope` | `false` / `prismal:admin` | Admin endpoints |

## 11. Error codes added

`tenant_config_invalid` (500, opaque message), `protocol_not_enabled` (406), `invalid_window` (400),
`forbidden_tenant` (403), `admin_disabled` (404), `budget_exceeded` (402 before a turn starts when the
tenant's hard cap or a gateway quota window is exhausted; mid-stream it is a terminal `error` event with
`code:"budget_exceeded"` and the partial `usage`). SDK maps each (`SPEC-CS2-ERR-001`).

**EARS:** IF the engine raises `BudgetExceeded` before the first token, THEN THE SYSTEM SHALL answer
`402` with `{"error":{"code":"budget_exceeded","scope":…,"limit":…,"consumed":…}}`; WHEN it is
raised mid-stream, THE SYSTEM SHALL emit a terminal `error` event with the same `code` and the partial `usage`.

## 12. Acceptance criteria (EARS)

| SPEC | Criterion |
|---|---|
| TEN-002 | IF `tenant.yaml` contains `security_strict: false`, THEN THE SYSTEM SHALL refuse that tenant with `tenant_config_invalid` and leave other tenants serving |
| TEN-003 | WHEN any request fails, THE SYSTEM SHALL produce logs and bodies containing no tenant secret value (canary-secret test) |
| USE-002 | WHEN org `beta` calls `/v1/usage`, THE SYSTEM SHALL return no `acme` record |
| USE-003 | WHEN 3 records sum to known values, THE SYSTEM SHALL return `totals` equal to the field-wise sum and `cache_hit_ratio` to 4 decimals |
| BUD-001 | WHEN `budget_status()` returns `status:"unknown"` (e.g. `turn` scope without `thread_id`), THE SYSTEM SHALL return it unchanged with HTTP 200 |
| AGU-001 | WHEN `?protocol=ag-ui` is sent, THE SYSTEM SHALL emit `RUN_STARTED` first and `RUN_FINISHED` or `RUN_ERROR` last |
| A2A-003 | IF path tenant `acme` is called with a `beta` identity, THEN THE SYSTEM SHALL return 403 without invoking the handler |
| WSK-003 | WHEN a second `message` frame arrives mid-turn, THE SYSTEM SHALL close with code 4409 |
| FAK-001 | IF `host_fake_llm_mode=true` and `host_environment=production`, THEN THE SYSTEM SHALL exit at startup with a non-zero code |
| ADM-001 | WHILE `host_admin_enabled=false`, THE SYSTEM SHALL answer 404 on every `/v1/admin/*` route |
| ADM-004 | WHEN DEC is disabled for the tenant, THE SYSTEM SHALL return 200 with `enabled:false` and `integrations:[]` |
| ADM-005 | WHEN an identity without `prismal:admin` calls `/v1/admin/routing`, THE SYSTEM SHALL return 403 and not call the engine |

## 13. Traceability (RF → SPEC)

| RF | Covered by |
|---|---|
| RF-RH2-001 | SPEC-RH2-TEN-* |
| RF-RH2-002 | SPEC-RH2-USG-* |
| RF-RH2-003 | SPEC-RH2-USE-*, SPEC-RH2-BUD-* |
| RF-RH2-004 | SPEC-RH2-AGU-* |
| RF-RH2-005 | SPEC-RH2-A2A-* |
| RF-RH2-006 | SPEC-RH2-WSK-* |
| RF-RH2-007 | SPEC-RH2-FAK-* |
| RF-RH2-008 | SPEC-RH2-ADM-* |
| RF-RH2-009 | SPEC-RH2-NFR-* |

---

## Change History

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-10-04 | Ernesto Crespo | Initial technical spec for host v0.2 |
| 1.1 | 2026-10-12 | Ernesto Crespo | [verify] items closed by VERIFY-RH2-00.md (`ListTasks`, `A2A-Version`, AG-UI encoding) |
| 1.2 | 2026-10-12 | Ernesto Crespo | TEN-005 trust model (operator-authored tenant config); exact key allowlist; `forbidden_tenant` on tenant-selection mismatch |
