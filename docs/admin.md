# Admin read endpoints

These are read-only views for operators and dashboards. Each one serializes an
engine `RuntimeContext` report field for field. The host never recomputes hashes,
agreement ratios or savings.

## Enabling and access

| Setting | Default | Meaning |
|---|---|---|
| `PRISMAL_SERVER_HOST_ADMIN_ENABLED` | `false` | Off ⇒ every `/v1/admin/*` route is `404 admin_disabled` |
| `PRISMAL_SERVER_HOST_ADMIN_SCOPE` | `prismal:admin` | Scope the identity must hold |

With admin enabled, an identity without the scope gets `403 forbidden` before
any engine call. In strict mode, a request with no credential gets `401`.

## Which tenant

Targeting uses **the identity only** and fails closed. `X-Org-Id` is ignored on
admin routes.

| Caller | `org_id` query | Reads |
|---|---|---|
| `prismal:admin`, identity org `acme` | omitted or `acme` | `acme` |
| `prismal:admin` | any other org, or `*` | `403 forbidden` |
| `prismal:admin:global` | `beta` | `beta` |
| `prismal:admin:global` | `*` (or the identity has no org) | the engine's process-wide view |

A non-global admin sees only its own org in `/tenants`.

## Endpoints

| Path | Engine source | Notes |
|---|---|---|
| `GET /v1/admin/tenants` | host registry + `tenant.yaml` files | Live runtimes (`created_at`, `last_used_at`) and configured tenants that are not live; `config_source` is `vault` or `process`; gateway **names** only, never secrets; `max_tenants` |
| `GET /v1/admin/audit?org_id=&since=&page=&page_size=` | `audit_page()` + `verify_audit_chain()` | 1-based paging, `page_size` 1–200 (default 50, otherwise `422`); `chain.status` is `valid`, `broken` or `unverified` |
| `GET /v1/admin/decisions?org_id=&since=&until=` | `decisions_report()` | Default window is the last 7 d; `agreement_ratio` is `null` when there are no samples |
| `GET /v1/admin/routing?org_id=&since=&until=` | `routing_report()` | `estimated_savings_usd` is the engine's frontier-counterfactual estimate |

A disabled feature still answers `200`, with `"enabled":false` and empty
`integrations`/`policies`. A row's `window.since` can be later than the one you
asked for, because the engine clamps it to the stats it still holds. An inverted
or unparsable window is `400 invalid_window`.

Full response shapes are in the OpenAPI document (`/openapi.json`) and in SPEC-RH2 §9.

```bash
curl -H 'Authorization: Bearer <admin token>' \
  'localhost:8000/v1/admin/audit?page_size=20'
```
