# WebSocket transport

`GET /ws/threads/{thread_id}` runs chat turns over one long-lived connection.
It is **off by default**: the route does not exist unless
`PRISMAL_SERVER_HOST_WS_ENABLED=true`.

| Setting | Default | Meaning |
|---|---|---|
| `PRISMAL_SERVER_HOST_WS_ENABLED` | `false` | Mount the route |
| `PRISMAL_SERVER_HOST_WS_MAX_MESSAGE_BYTES` | `1048576` | Largest client frame (`1009` beyond) |
| `PRISMAL_SERVER_SSE_HEARTBEAT_S` | `15` | Server heartbeat interval |

## Handshake

The handshake is the only auth point; credentials go in headers (`Authorization`,
`X-Org-Id`) exactly as for HTTP. Any refusal closes the connection with `1008`
before it is accepted:

- an `Origin` header that is sent but not in `cors_origins` (`"*"` allows any;
  non-browser clients that send no `Origin` are allowed);
- strict mode (`host_auth_strict`) without a credential;
- a tenant that fails fail-closed resolution (an `X-Org-Id` can never select
  another tenant).

Browser ticket auth is out of scope for 0.2.

## Frames

Client → server (JSON text frames):

```json
{"type":"message","content":"Hello!","protocol":"native"}
{"type":"cancel"}
{"type":"ping"}
```

`protocol` is `native` (default) or `ag-ui`. `role` and `metadata` are optional.

Server → client:

```json
{"event":"token","data":{"delta":"Hel"}}
{"event":"done","data":{"thread_id":"t-42","finish_reason":"stop","usage":{…}}}
{"event":"agui","data":{"type":"RUN_STARTED", …}}
{"event":"heartbeat"}
{"event":"pong"}
```

`data` is byte-for-byte the payload of the matching SSE frame (`token`,
`tool_call`, `state`, `done` and `error`, or the engine's AG-UI event).

## Rules

- **One turn at a time.** A `message` while a turn is running closes with `4409`.
  A turn counts as finished once its terminal frame (`done`/`error` or
  `RUN_FINISHED`/`RUN_ERROR`) has been sent.
- `cancel` stops the running turn and is ignored when no turn is running.
- A malformed frame closes with `4400`, and an oversized frame closes with `1009`.
- A turn that cannot start or that fails (`budget_exceeded`, `protocol_not_enabled`,
  `invalid_thread_id`, …) produces an `error` frame carrying the SSE error body,
  and **the connection stays open**. WebSocket has no status code that could carry
  a `402`.
- A disconnect, close or server shutdown cancels the running turn and releases
  its runtime lease.

The in-process metric `ws_sessions_total{close_code}` counts sessions by close code.
