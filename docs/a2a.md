# Inbound A2A

The host exposes the standard A2A surface, **v0.3.x and v1.0 side by side**, so
generic A2A clients interoperate with no prismal-specific extension. It owns **transport + auth only**:
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

## v1.0 dual stack (v0.2)

The engine chooses the protocol stack per request from the `A2A-Version` header,
or `?A2A-Version=` when the header is absent. A missing header means v0.3. The
host forwards request headers verbatim, except `Authorization`,
`Proxy-Authorization` and `Cookie`, together with the resolved `org_id`.

| | v0.3 | v1.0 |
|---|---|---|
| Methods | `message/send`, `message/stream`, `tasks/get`, `tasks/cancel` | `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask` (PascalCase) |
| Task state | `completed` | `TASK_STATE_COMPLETED` |

```bash
curl -X POST localhost:8000/a2a -H 'A2A-Version: 1.0' -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ListTasks","params":{}}'
```

- **Versioned card:** `GET /.well-known/agent-card.json?protocolVersion=1.0|0.3`.
  Asking for the other stack is `404` unless the engine's `a2a_accept_legacy` is on.
- **Tenant routes:** `POST {base}/{tenant}` and
  `GET {base}/{tenant}/.well-known/agent-card.json`, where `{base}` is the path of
  the engine's `a2a_base_url`. The RPC route requires the auth-resolved org to equal
  the path tenant, otherwise `403 forbidden_tenant` before the handler runs. The
  card route is public. Recommended: set `a2a_base_url` to end in `/a2a`, so
  tenants live at `/a2a/{tenant}`.
- `POST /a2a` resolves the org fail-closed: the engine scopes `ListTasks`/`GetTask`
  by the forwarded `org_id`, so a bare or mismatched `X-Org-Id` is `403`.

## Auth & strict mode

The host **does not** reject A2A calls itself (no `401`). It resolves whatever
credential is on the wire (see [auth.md](./auth.md)) and hands the `AuthContext`
(or `None`) to the handler. When the engine is in A2A strict mode, an
unauthenticated call is rejected by the handler as JSON-RPC `-32001`, which the
host relays unchanged.

If the engine's inbound A2A is not composed (`a2a_handler is None`, e.g. the
engine's `a2a_inbound_enabled` is off), `POST /a2a` returns an informative
JSON-RPC error rather than a `500`.
