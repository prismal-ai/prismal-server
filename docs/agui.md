# AG-UI stream mode

The host can stream a chat turn as **AG-UI 1.0** events, so any AG-UI client
can drive a prismal run. The events come from the engine's own translator
(`prismal.events.agui`). The host frames them and translates nothing itself.

## Enabling

Both gates must be on, otherwise the request gets `406 protocol_not_enabled`
before any graph work:

| Gate | Default |
|---|---|
| Host: `PRISMAL_SERVER_HOST_AGUI_ENABLED` | `true` |
| Engine: `PRISMAL_AGUI_EVENTS_ENABLED` (or the tenant's `agui_events_enabled`) | `false` |

The engine flags `agui_include_usage`, `agui_include_state_deltas` and
`agui_include_metadata` are read from the tenant's engine settings.

## Two ways in

**Same thread route, negotiated.** `POST /threads/{id}/messages` with either
`?protocol=ag-ui` or `Accept: text/event-stream; profile=ag-ui`. `?protocol=` takes
precedence, and an unknown value is `422`.

```bash
curl -N -X POST 'localhost:8000/threads/t-42/messages?protocol=ag-ui' \
  -H 'content-type: application/json' -d '{"content":"Hello!"}'
```

**`POST /v1/agui` with a `RunAgentInput`.**

```bash
curl -N -X POST localhost:8000/v1/agui -H 'content-type: application/json' -d '{
  "threadId":"t-42","runId":"r-1",
  "messages":[{"id":"m1","role":"user","content":"Hello!"}],
  "tools":[],"context":[],"state":{},"forwardedProps":{}}'
```

Only the **last `user` message's text** becomes the turn input. History lives in
the engine checkpoint and is not replayed. Text parts of `InputContent[]` are
joined; a request with no user text is `422 invalid_input`. Client `tools` are
ignored (and logged) in 0.2.

## Wire format

Each frame is `data: <event JSON>\n\n` with camelCase keys and no `event:` line.
A typical run:

```
data: {"type":"RUN_STARTED","threadId":"t-42","runId":"r-1"}
data: {"type":"TEXT_MESSAGE_START","messageId":"m-1","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m-1","delta":"Hello"}
data: {"type":"TEXT_MESSAGE_END","messageId":"m-1"}
data: {"type":"CUSTOM","name":"prismal.usage","value":{…12-key usage…}}
data: {"type":"RUN_FINISHED","threadId":"t-42","runId":"r-1","usage":[…]}
```

- `RUN_STARTED.threadId` is the client's thread id, and `input` is not echoed back.
- Usage arrives before `RUN_FINISHED`, both as `CUSTOM prismal.usage` and as
  `RUN_FINISHED.usage[]`.
- A failure ends with `RUN_ERROR` (`code`, `message`).
- Heartbeats are SSE comments, and a client disconnect cancels the run.

AG-UI turns follow the native turn path: the same org-scoped engine thread, the
same `402` before the first token, and usage persisted on success.

Over WebSocket, set `"protocol":"ag-ui"` on the message frame. See
[websocket](./websocket.md).
