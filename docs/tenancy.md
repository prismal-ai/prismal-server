# Multi-tenancy

Each distinct `org_id` gets its **own** `RuntimeContext`, built via the engine's
`build_runtime(org_id=...)`. Tenants stay isolated because the engine derives
per-tenant collections (`collection_for(base, org_id)`); the host never builds
vector stores or checkpointers itself.

## Resolving `org_id`

For every request the host resolves the tenant in this order (SPEC-RHB-TEN-001):

1. `AgentIdentity.org_id`, if the resolved [identity](./auth.md) carries one.
2. The `X-Org-Id` request header.
3. `None`, which means single-tenant mode.

Two rules make this **fail closed** (v0.2):

- If an identity has an org, an `X-Org-Id` that differs from it is
  `403 forbidden_tenant`. A header never switches tenants.
- Reads of tenant data (usage, budget, `POST /a2a`, WebSocket) and chat with tenant
  config enabled accept a bare header (no identity org) **only** with
  `host_auth_backend=none` outside `production`. Otherwise they answer `403`, never a
  silent fallback to the process tenant. Admin routes ignore the header completely
  (see [admin](./admin.md)).

The engine receives an **org-scoped thread key** `f"{len(org)}:{org}:{thread_id}"`,
or the raw id in single-tenant mode, so conversation state and run summaries never
cross tenants. Clients always see their own `thread_id`. In single-tenant mode a raw
id shaped `^\d+:` is refused (`422 invalid_thread_id`) because it could alias a
scoped key.

The resolved `org_id` both selects the tenant runtime **and** is threaded into
`config.configurable.org_id` so the engine graph runs in the right tenant scope.

```bash
curl -N -X POST localhost:8000/threads/t-1/messages \
  -H 'X-Org-Id: acme' \
  -H 'content-type: application/json' \
  -d '{"content":"hi"}'
```

## The runtime registry

`RuntimeRegistry` keeps one `RuntimeContext` per `org_id` in an in-process cache:

- **Lazy** — a tenant's runtime is built on first use, then cached.
- **Single-flight** — concurrent first-use for the same `org_id` builds exactly
  once (an `asyncio.Lock` guards the miss path).
- **Bounded (LRU)** — at most `host_max_tenants` live runtimes; on overflow the
  least-recently-used tenant's runtime is `aclose()`d and evicted. Set
  `PRISMAL_SERVER_HOST_MAX_TENANTS=0` to disable eviction (unbounded).
- **Coordinated teardown** — on shutdown every cached runtime is `aclose()`d;
  one tenant failing to close never blocks the rest.

## Per-tenant engine config (v0.2)

Set `PRISMAL_SERVER_HOST_TENANT_CONFIG_DIR` to give tenants their own engine
settings (gateways, models, budget policy, …):

```
<host_tenant_config_dir>/
  acme/
    tenant.yaml        # flat map: engine Settings field → scalar
    secrets/           # file backend default location
      openrouter_api_key
```

```yaml
# acme/tenant.yaml
openrouter_enabled: true
budget_enabled: true
budget_max_cost_usd: 50
```

- `tenant.yaml` is a flat map of engine `Settings` field names to scalars, with lists
  and maps JSON-encoded. The values reach the engine through
  `build_runtime(config_source=…)` as `PRISMAL_<KEY>`. A tenant without the file
  uses the process config.
- **Exact allowlist.** Only keys in `ALLOWED_KEYS`
  (`src/prismal_server/tenancy/config_source.py`) are accepted. Anything else
  (endpoints/URLs, host paths, process-wide sizing, security, privacy and compliance
  floors, and any engine key added later) is refused with
  `500 tenant_config_invalid` (opaque message). Validation is eager, before the
  runtime is built, so a bad file never falls back silently.
- **Secrets never go in `tenant.yaml`.** `*_api_key` / `*_token` keys come only from
  the secrets backend, wrapped in `SecretStr` and never logged or returned:
  - `file` (default): `<host_secrets_dir>/<org_id>/<key>`, or
    `<host_tenant_config_dir>/<org_id>/secrets/<key>` when `host_secrets_dir` is unset;
  - `env`: `PRISMAL_SERVER_SECRET__<ORG>__<KEY>`. It accepts only org ids matching
    `^[a-z0-9]+(-[a-z0-9]+)*$`.
- `org_id` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, which guards against path traversal.
- **Hot reload.** On each lookup the registry checks an mtime fingerprint covering
  `tenant.yaml` and the secret files. A changed tenant is rebuilt; the old runtime
  closes after its last in-flight turn.
- **Trust model.** Tenant config is **operator-authored** (SPEC-RH2-TEN-005).
  Budget and quota keys are per-tenant operator policy. Self-service tenant config
  would need a tighten-only layer, which does not exist yet.

## Notes

- Single-tenant deployments have exactly one entry (`org_id = None`).
- Evicting an LRU tenant only releases resources; its persisted state
  (checkpointer, vector store) is external and is re-attached on next use.
- A high tenant churn with a small cap means frequent rebuilds — size
  `host_max_tenants` to your working set.
