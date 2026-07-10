# Multi-tenancy

Each distinct `org_id` gets its **own** `RuntimeContext`, built via the engine's
`build_runtime(org_id=...)`. Tenants stay isolated because the engine derives
per-tenant collections (`collection_for(base, org_id)`); the host never builds
vector stores or checkpointers itself.

## Resolving `org_id`

For every request the host resolves the tenant in this order (SPEC-RHB-TEN-001):

1. `AgentIdentity.org_id` — if the resolved [identity](./auth.md) carries one.
2. The `X-Org-Id` request header.
3. `None` — single-tenant mode.

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

## Notes

- Single-tenant deployments have exactly one entry (`org_id = None`).
- Evicting an LRU tenant only releases resources; its persisted state
  (checkpointer, vector store) is external and is re-attached on next use.
- A high tenant churn with a small cap means frequent rebuilds — size
  `host_max_tenants` to your working set.
