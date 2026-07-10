# Running prismal-server

The host is a single stateless ASGI process. It boots the engine once per tenant
(lazily) and serves it over REST/SSE + inbound A2A.

## Local (uv)

```bash
uv pip install -e ".[dev]"
uvicorn prismal_server.app:app --reload
```

`prismal_server.app:app` is the module-level ASGI instance built by
`create_app()`. On **startup** nothing is warmed — readiness composes the default
runtime on demand; on **shutdown** every cached `RuntimeContext` is `aclose()`d
(MCP disconnect, checkpointer close, stores released).

## Docker

```bash
docker build -t prismal-server:dev .
docker run -p 8000:8000 -v prismal-data:/app/data prismal-server:dev
```

or via compose (adds a named volume + healthcheck):

```bash
docker compose up --build
```

The image is a two-stage `uv` build on `python:3.13-slim`, running uvicorn as a
non-root user (uid 10001). It bundles the engine, which pulls heavy ML deps
(embeddings model, vector store), so the first build is large and slow. Engine
runtime state (sqlite checkpointer, vector store) is written under `/app/data` —
mount a volume there in production so it survives restarts.

## Configuration

Host-only settings use the `PRISMAL_SERVER_` prefix (pydantic-settings; a local
`.env` is read if present). They govern **host** concerns only — engine settings
(provider keys, models, …) are read separately by the engine's own
`ConfigSourcePort` from the same environment.

| Env var | Default | Meaning |
|---|---|---|
| `PRISMAL_SERVER_HOST` | `0.0.0.0` | Bind address |
| `PRISMAL_SERVER_PORT` | `8000` | Bind port |
| `PRISMAL_SERVER_CORS_ORIGINS` | `[]` | Allowed CORS origins |
| `PRISMAL_SERVER_HOST_AUTH_BACKEND` | `none` | `none` \| `bearer` \| `oidc` — see [auth](./auth.md) |
| `PRISMAL_SERVER_HOST_AUTH_STRICT` | `false` | Reject unauthenticated on protected routes |
| `PRISMAL_SERVER_SSE_HEARTBEAT_S` | `15` | SSE heartbeat interval (seconds) |
| `PRISMAL_SERVER_HOST_MAX_TENANTS` | `32` | Live tenant-runtime cap (LRU) — see [tenancy](./tenancy.md) |
| `PRISMAL_SERVER_A2A_ENABLED` | `true` | Gate the A2A routes (404 when off) — see [a2a](./a2a.md) |
| `PRISMAL_SERVER_DEV_IDENTITY_DID` | `did:key:zLocalDev` | `NoAuthBackend` identity DID |

## Health & readiness

| Endpoint | Meaning |
|---|---|
| `GET /healthz` | **Liveness** — `200 {"status":"ok"}` whenever the process is up, independent of engine state. |
| `GET /readyz` | **Readiness** — `200 {"status":"ready"}` only once the default `RuntimeContext` composes; otherwise `503 {"status":"not_ready","reason":...}` with a stable, non-sensitive reason code. |

Wire liveness probes to `/healthz` and readiness/traffic gating to `/readyz`.
Neither endpoint leaks configuration values or secrets.

## Scaling

The per-process `RuntimeRegistry` is authoritative, so run **one uvicorn worker
per process** and scale out with replicas behind a load balancer rather than with
`--workers`. Checkpointer/vector-store state is external, so replicas are
horizontally scalable.
