# CLAUDE.md — prismal-server

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this repo is

`prismal-server` is the **reference host** for the `prismal` engine: a thin
FastAPI/ASGI process that boots the engine once and serves it over REST/SSE
(and later WS), plus the inbound A2A endpoints. It is the counterpart to the
engine's "framework only, no host" design.

The full design lives in
[`specs/reference-host-bootstrap/`](./specs/reference-host-bootstrap/)
(`PLAN.md` · `ARCHITECTURE.md` · `SPEC.md` · `TASKS.md`). Read those before
implementing.

## The one hard rule — orchestrate, do not reimplement

> **contract / logic → engine (`prismal/`); serving HTTP, authenticating,
> rendering, persisting config → host (this repo).**

The host has **exactly four** allowed seams into the engine. Adding any agent,
RAG, tool, memory, security, or policy logic here is a bug:

| Seam | Engine symbol |
|---|---|
| Composition | `prismal.composition.runtime.build_runtime(...)` → `RuntimeContext` |
| Execution | `prismal.agents.graph.get_async_compiled_graph(...)` |
| Inbound A2A | `prismal.a2a.server.A2AServerHandler`, `prismal.a2a.card.build_agent_card(...)` |
| Identity | `prismal.identity` (`IdentityPort` / `OidcIdentityProvider`) |

## Review checklist (enforce on every PR)

- [ ] No import from `prismal` other than the four seams above (there is an
      AST import-guard test — keep it green).
- [ ] No prompt built from user input host-side (there are none; the engine owns
      prompting). Never f-string request content into anything engine-bound
      except as graph input.
- [ ] Every `RuntimeContext` created is `aclose()`d on shutdown.
- [ ] Fully async; no blocking calls on the event loop.
- [ ] Secrets are `SecretStr`; no secret/credential in any response body.
- [ ] New behaviour is **test-first** (TDD): failing test → minimal code → green.
- [ ] Unit tests use `build_test_runtime` fakes (no live provider I/O); engine
      integration goes in the opt-in `contract` suite.
- [ ] The engine dep stays pinned `prismal>=3.10,<4`.

## Common commands (once scaffolded — see TASKS RHB-00-01)

```bash
uv pip install -e ".[dev]"
uv run pytest                      # unit
uv run pytest -m contract          # against a released engine (opt-in)
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uvicorn prismal_server.app:app --reload   # local run
```

## Boundary examples

- Need RAG results in a response? **No** — the graph already does RAG; stream the
  graph. Do not call `prismal.rag.*`.
- Need to sanitize A2A input? **No** — `A2AServerHandler` L1-sanitizes and audits.
  The host owns transport + auth only.
- Need per-tenant isolation? Use `build_runtime(org_id=...)`; do not build vector
  stores or checkpointers directly.
