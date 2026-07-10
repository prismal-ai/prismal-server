# prismal-server

Reference **host** for the [`prismal`](https://github.com/prismal-ai/prismal)
agent engine — a thin FastAPI/ASGI process that exposes prismal over
**REST · SSE** (WebSocket to follow) plus the inbound **A2A** endpoints, so
non-Python clients and the front-end ecosystem can build against it.

`prismal` is a pure library (no web server by design). `prismal-server` is the
missing process that binds a port, boots the engine once, and serves it — while
**reimplementing none of its logic**.

## Status

**v0.1 — reference host bootstrap complete.** The streaming chat surface, inbound
A2A, the auth/tenancy seams, health/readiness, and the release plumbing (image,
contract suite, import guard) are in place. The full SDD lives under
[`specs/reference-host-bootstrap/`](./specs/reference-host-bootstrap/)
([`PLAN`](./specs/reference-host-bootstrap/PLAN.md) ·
[`ARCHITECTURE`](./specs/reference-host-bootstrap/ARCHITECTURE.md) ·
[`SPEC`](./specs/reference-host-bootstrap/SPEC.md) ·
[`TASKS`](./specs/reference-host-bootstrap/TASKS.md)).

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

## HTTP surface (v0.1)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` · `/readyz` | Liveness / readiness |
| `POST` | `/threads` | Mint a new `thread_id` |
| `POST` | `/threads/{id}/messages` | Stream a chat turn (SSE: `token`/`tool_call`/`state`/`done`/`error`) |
| `GET` | `/.well-known/agent-card.json` | A2A Agent Card |
| `POST` | `/a2a` | A2A JSON-RPC / SSE (delegates to `A2AServerHandler`) |
| `GET` | `/openapi.json` | OpenAPI (for `prismal-sdk` generation) |

## Docs

| Doc | Covers |
|---|---|
| [`docs/running.md`](./docs/running.md) | Local run, Docker/compose, config, health/readiness |
| [`docs/auth.md`](./docs/auth.md) | The auth seam: `none` / `bearer` / `oidc`, strict mode |
| [`docs/tenancy.md`](./docs/tenancy.md) | `org_id` resolution, per-tenant runtimes, the LRU cap |
| [`docs/a2a.md`](./docs/a2a.md) | Inbound A2A: Agent Card + JSON-RPC/SSE |

## The contract with the engine

The host uses **exactly four** entry points from `prismal` and nothing else — a
static import-guard test enforces it (`tests/unit/test_import_guard.py`), and the
rule + review checklist live in [`CLAUDE.md`](./CLAUDE.md):

- `build_runtime(...)` → `RuntimeContext` (all ports, coordinated `aclose()`)
- `get_async_compiled_graph(...)` → the graph to `astream()`
- `A2AServerHandler` + `build_agent_card(...)` → inbound A2A
- `prismal.identity` `IdentityPort` / `OidcIdentityProvider` → request → identity

## Development

```bash
uv pip install -e ".[dev]"
uv run pytest                      # unit suite (no live provider I/O)
uv run pytest -m contract          # opt-in: against a released engine
uv run ruff check . && uv run ruff format --check .
uv run mypy src
```

The unit suite runs fully offline against engine fakes (`build_test_runtime`); the
`contract` suite boots a real `build_runtime()`, serves a real Agent Card, and —
when an LLM provider credential is present in the environment — round-trips a full
chat turn over SSE.

## Related repos

- [`prismal-ai/prismal`](https://github.com/prismal-ai/prismal) — the engine (pip dependency `prismal-ai`, pinned `>=3.10,<4`).
- [`prismal-ai/prismal-sdk`](https://github.com/prismal-ai/prismal-sdk) — thin client generated against this host's wire contract (`/openapi.json` + the A2A surface).

## License

See [`LICENSE`](./LICENSE).
