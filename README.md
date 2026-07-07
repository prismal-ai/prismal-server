# prismal-server

Reference **host** for the [`prismal`](https://github.com/prismal-ai/prismal)
agent engine — a thin FastAPI/ASGI process that exposes prismal over
**REST · SSE** (WebSocket to follow) plus the inbound **A2A** endpoints, so
non-Python clients and the front-end ecosystem can build against it.

`prismal` is a pure library (no web server by design). `prismal-server` is the
missing process that binds a port, boots the engine once, and serves it — while
**reimplementing none of its logic**.

## Status

🌱 **Seed / design stage.** The full SDD lives in
[`specs/reference-host-bootstrap/`](./specs/reference-host-bootstrap/):

| Doc | What it covers |
|---|---|
| [`PLAN.md`](./specs/reference-host-bootstrap/PLAN.md) | PRD: problem, goals, scope, milestones |
| [`ARCHITECTURE.md`](./specs/reference-host-bootstrap/ARCHITECTURE.md) | C4 context/containers, module layout, data flow, ADRs |
| [`SPEC.md`](./specs/reference-host-bootstrap/SPEC.md) | `SPEC-RHB-*` requirements: endpoints, contracts, errors, auth, config, SDK |
| [`TASKS.md`](./specs/reference-host-bootstrap/TASKS.md) | Test-first task breakdown `RHB-*` across milestones M1–M5 |

Implementation follows the tasks in order. **Phase 0 (repo scaffold, RHB-00-*)
is done**: `pyproject.toml`, the `prismal_server` package skeleton, the test
harness (`tests/conftest.py`), and CI are in place; the route/behaviour phases
(M1+) are next.

## The contract with the engine

The host uses **exactly four** entry points from `prismal` and nothing else
(see [`CLAUDE.md`](./CLAUDE.md) for the rule + review checklist):

- `build_runtime(...)` → `RuntimeContext` (all ports, coordinated `aclose()`)
- `get_async_compiled_graph(...)` → the graph to `astream()`
- `A2AServerHandler` + `build_agent_card(...)` → inbound A2A
- `prismal.identity` `IdentityPort` / `OidcIdentityProvider` → request → identity

## Planned surface (v0.1)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` · `/readyz` | Liveness / readiness |
| `POST` | `/threads` | Mint a new `thread_id` |
| `POST` | `/threads/{id}/messages` | Stream a chat turn (SSE: `token`/`tool_call`/`state`/`done`/`error`) |
| `GET` | `/.well-known/agent-card.json` | A2A Agent Card |
| `POST` | `/a2a` | A2A JSON-RPC / SSE (mounts `A2AServerHandler`) |
| `GET` | `/openapi.json` | OpenAPI (for `prismal-sdk` generation) |

## Related repos

- [`prismal-ai/prismal`](https://github.com/prismal-ai/prismal) — the engine (pip dependency, pinned `>=3.10,<4`).
- [`prismal-ai/prismal-sdk`](https://github.com/prismal-ai/prismal-sdk) — thin client over this host's wire contract.

## Quickstart (once scaffolded)

```bash
uv pip install -e ".[dev]"
uvicorn prismal_server.app:app --reload
curl localhost:8000/healthz
```

## License

See [`LICENSE`](./LICENSE).
