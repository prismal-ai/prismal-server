# Inbound A2A

The host exposes the standard **A2A v0.3.x** surface so generic A2A clients
interoperate — no prismal-specific extension. It owns **transport + auth only**:
every JSON-RPC call is passed through unchanged to the engine's
`A2AServerHandler`, which already L1-sanitizes (`InputSanitizer`) and audits every
in/out task. The host never re-sanitizes or re-audits.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/.well-known/agent-card.json` | The Agent Card, `build_agent_card(...)` serialized camelCase, cached per `(settings, org_id)`. |
| `POST` | `/a2a` | JSON-RPC: `message/send`, `tasks/get`, `tasks/cancel` (non-stream) and `message/stream` (SSE). |

Both routes return **`404`** when `PRISMAL_SERVER_A2A_ENABLED=false` (feature
absent, not an error).

## Agent Card

```bash
curl localhost:8000/.well-known/agent-card.json
```

Returns the camelCase A2A wire form (`name`, `protocolVersion`, capabilities, …).
The card is cached per tenant; pass `X-Org-Id` to fetch a tenant-scoped card.

## JSON-RPC call

```bash
curl -X POST localhost:8000/a2a \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{...}}'
```

The host resolves the tenant, adapts the [identity](./auth.md) to the engine's
`AuthContext`, and delegates to `handler.handle_rpc(payload, auth_ctx=...)`. The
raw JSON-RPC reaches the handler verbatim.

## Streaming

A `message/stream` request is delegated to `handler.stream_rpc(...)` and returned
as `text/event-stream`, emitting the handler's `data:` lines (artifacts, status
updates) until completion.

## Auth & strict mode

The host **does not** reject A2A calls itself (no `401`). It resolves whatever
credential is on the wire (see [auth.md](./auth.md)) and hands the `AuthContext`
(or `None`) to the handler. When the engine is in A2A strict mode, an
unauthenticated call is rejected by the handler as JSON-RPC `-32001`, which the
host relays unchanged.

If the engine's inbound A2A is not composed (`a2a_handler is None`, e.g. the
engine's `a2a_inbound_enabled` is off), `POST /a2a` returns an informative
JSON-RPC error rather than a `500`.
