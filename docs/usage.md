# Usage & budget

Every number here comes from the engine. The host never prices, meters or
classifies a budget (SPEC-RH2-NFR-003): it forwards the engine's run summary,
stores what it gets, and sums those records.

## `done.usage`

A completed chat turn ends with a `done` event whose `usage` is the engine's
`get_run_summary(...).to_usage_wire()`. It has exactly these 12 keys, in this order (shared
contract fixture: `tests/fixtures/protocols/usage_wire.json`):

```
event: done
data: {"thread_id":"t-42","finish_reason":"stop","usage":{"prompt_tokens":1200,"completion_tokens":340,"cache_read_tokens":900,"cache_write_tokens":0,"reasoning_tokens":0,"total_tokens":1540,"calls":3,"cost_usd":0.004213,"cost_source":"gateway","estimated":false,"served_model":"anthropic/claude-sonnet-4.5","gateway":"openrouter"}}
```

`usage` is `null` when the engine has no summary for the turn. In that case the
stream still completes, and the in-process counter `usage_missing_total` is
incremented. A turn that made no LLM call never re-reports the previous turn.

## Persistence

Each `done` with non-null usage appends one record per turn to the usage store:

| Setting | Default | Meaning |
|---|---|---|
| `PRISMAL_SERVER_HOST_USAGE_STORE` | `memory` | `memory` (per process) \| `sqlite` |
| `PRISMAL_SERVER_HOST_USAGE_DB_PATH` | `usage.db` | SQLite file (`sqlite` store) |
| `PRISMAL_SERVER_HOST_USAGE_RETENTION_DAYS` | `90` | Records older than this are dropped |

A mid-stream `budget_exceeded` carries the partial usage in its `error` event,
but nothing is persisted for it. A2A turns are not recorded in 0.2.

## `GET /v1/usage`

```bash
curl 'localhost:8000/v1/usage?since=2026-10-03T12:00:00Z&limit=50'
```

| Query | Default | Notes |
|---|---|---|
| `thread_id` | — | Filter to one thread |
| `since` / `until` | now−24 h / now | RFC 3339 with offset; window ≤ 31 d |
| `limit` | `100` | 1–500 turns per page |
| `cursor` | — | `next_cursor` from the previous page |

The response contains:

- `totals`: a field-wise sum over the **whole window**, not just the page.
  `estimated` is the OR of all records; `cost_source` is their common value or `mixed`.
- `cache_hit_ratio`: `cache_read_tokens / prompt_tokens`.
- `by_model`: per-model totals.
- `turns`: newest first.
- `next_cursor`: an opaque cursor for the next page.

Errors: `400 invalid_window`, `400 invalid_cursor`.

## `GET /v1/budget`

Serializes the engine's `RuntimeContext.budget_status(thread_id=)` field for
field, plus `await quota_windows()` as `gateways`. The host never computes
`status`, `remaining` or `breached_dimension`. With the budget disabled the response has
`"status":"disabled"`, and `consumed` is still reported. Gateway windows that fail
are omitted by the engine; see SPEC-RH2 §4 for the complete shape.

## `402 budget_exceeded`

If the tenant's hard cap or a gateway quota window is already exhausted when a
turn starts, the request is refused **before any stream** with:

```json
{"error":{"code":"budget_exceeded","message":"…","scope":"tenant","limit":{…},"consumed":{…}}}
```

This is a change from v0.1, which answered `429`. When the budget runs out
mid-stream, the turn ends with a terminal `error` event that has the same `code`
and the partial `usage`.

## Tenant scoping

Usage and budget reads resolve the org **from the identity** and fail closed. An
`X-Org-Id` that differs from the identity's org is `403 forbidden_tenant`. A bare
header is honoured only with `host_auth_backend=none` outside `production`. See
[tenancy](./tenancy.md).
