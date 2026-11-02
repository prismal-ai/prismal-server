# prismal-server

Reference **host** for the [`prismal`](https://github.com/prismal-ai/prismal)
agent engine — a thin FastAPI/ASGI process that exposes prismal over
**REST · SSE · WebSocket** (opt-in, `host_ws_enabled`) plus the inbound **A2A** endpoints, so
non-Python clients and the front-end ecosystem can build against it.

`prismal` is a pure library (no web server by design). `prismal-server` is the
missing process that binds a port, boots the engine once, and serves it — while
**reimplementing none of its logic**.

## Status

**v0.2: usage, gateways and AG-UI.** On top of the v0.1 host (streaming chat,
inbound A2A, auth/tenancy seams, health, release plumbing), v0.2 adds per-tenant
engine config and gateways, `done.usage` plus `/v1/usage` and `/v1/budget`, AG-UI 1.0
streaming, the A2A v1.0 dual stack, an opt-in WebSocket transport, a deterministic
fake-LLM mode and read-only admin endpoints. Specs:
[`specs/reference-host-bootstrap/`](./specs/reference-host-bootstrap/) (v0.1) and
[`specs/host-v0.2-usage-gateways-agui/`](./specs/host-v0.2-usage-gateways-agui/)
(v0.2). Release notes are in [`CHANGELOG.md`](./CHANGELOG.md).

## Quickstart

```bash
uv pip install -e ".[dev]"
uvicorn prismal_server.app:app --reload
curl localhost:8000/healthz          # {"status":"ok"}
curl localhost:8000/readyz           # {"status":"ready"} once the runtime composes
```

Stream a chat turn (SSE):

```bash
tid=$(curl -sX POST localhost:8000/threads | python -c 'import sys,json;print(json.load(sys.stdin)["thread_id"])')
curl -N -X POST "localhost:8000/threads/$tid/messages" \
  -H 'content-type: application/json' \
  -d '{"content":"Hello!"}'
```

Or with Docker:

```bash
docker compose up --build
curl localhost:8000/healthz
```

> The image bundles the engine, which pulls heavy ML deps (embeddings, vector
> store) — the first build is large and slow. See
> [`docs/running.md`](./docs/running.md).

## HTTP surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` · `/readyz` | Liveness / readiness |
| `POST` | `/threads` | Mint a new `thread_id` |
| `POST` | `/threads/{id}/messages` | Stream a chat turn (SSE: `token`/`tool_call`/`state`/`done`/`error`; `?protocol=ag-ui` for AG-UI) |
| `POST` | `/v1/agui` | AG-UI `RunAgentInput` → AG-UI event stream |
| `GET` | `/ws/threads/{id}` | WebSocket chat (opt-in, `host_ws_enabled`) |
| `GET` | `/v1/usage` · `/v1/budget` | Per-turn usage history · budget status and gateway quotas |
| `GET` | `/v1/admin/{tenants,audit,decisions,routing}` | Admin read endpoints (opt-in, admin scope) |
| `GET` | `/.well-known/agent-card.json` | A2A Agent Card (`?protocolVersion=0.3\|1.0`) |
| `POST` | `/a2a` · `{base}/{tenant}` | A2A JSON-RPC / SSE, v0.3 and v1.0 (delegates to `A2AServerHandler`) |
| `GET` | `/openapi.json` | OpenAPI with examples (for `prismal-sdk` generation) |

## Docs

| Doc | Covers |
|---|---|
| [`docs/running.md`](./docs/running.md) | Local run, Docker/compose, config, health/readiness |
| [`docs/auth.md`](./docs/auth.md) | The auth seam: `none` / `bearer` / `oidc`, strict mode |
| [`docs/tenancy.md`](./docs/tenancy.md) | `org_id` resolution (fail-closed), per-tenant runtimes, `tenant.yaml` and secrets |
| [`docs/usage.md`](./docs/usage.md) | `done.usage`, `/v1/usage`, `/v1/budget`, `402 budget_exceeded` |
| [`docs/agui.md`](./docs/agui.md) | AG-UI stream mode and `POST /v1/agui` |
| [`docs/websocket.md`](./docs/websocket.md) | WebSocket frames, close codes, handshake auth |
| [`docs/a2a.md`](./docs/a2a.md) | Inbound A2A: Agent Card, v0.3/v1.0 dual stack, tenant routes |
| [`docs/admin.md`](./docs/admin.md) | Admin read endpoints and their scopes |
| [`docs/fake-mode.md`](./docs/fake-mode.md) | Deterministic fake-LLM mode for contract tests |

## The contract with the engine

The host calls the engine only through its **public host read surface** (HRS §3)
plus the v0.1 entry points. Everything else it reads is a `RuntimeContext` method.
A static import-guard test enforces an exact per-module symbol allowlist
(`tests/unit/test_import_guard.py`); the rule and the review checklist live in
[`CLAUDE.md`](./CLAUDE.md).

- `build_runtime(...)` / `build_test_runtime(...)` → `RuntimeContext` (all ports, coordinated `aclose()`)
- `get_async_compiled_graph(...)` → the graph to `astream()`
- `A2AServerHandler` + `build_agent_card(...)` → inbound A2A
- `prismal.identity` `IdentityPort` / `OidcIdentityProvider` → request → identity
- `prismal.events.agui` (AG-UI translator) and `prismal.monitoring.get_run_summary` (usage wire)

## Development

```bash
uv pip install -e ".[dev]"
uv run pytest                      # unit suite (no live provider I/O)
uv run pytest -m contract          # opt-in: against a released engine
uv run ruff check . && uv run ruff format --check .
uv run mypy src
```

The unit suite runs fully offline against engine fakes (`build_test_runtime`); the
`contract` suite boots a real `build_runtime()`, serves a real Agent Card, runs the
v0.2 surfaces (native, AG-UI, usage, A2A v1) against the released engine in fake-LLM
mode, and, when an LLM provider credential is present in the environment,
round-trips a full chat turn over SSE.

## Related repos

- [`prismal-ai/prismal`](https://github.com/prismal-ai/prismal) — the engine (pip dependency `prismal-ai`, pinned `>=3.17,<4`).
- [`prismal-ai/prismal-sdk`](https://github.com/prismal-ai/prismal-sdk) — thin client generated against this host's wire contract (`/openapi.json` + the A2A surface).

## License

See [`LICENSE`](./LICENSE).
