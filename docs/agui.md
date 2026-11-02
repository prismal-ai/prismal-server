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
ignored (and logged); see [Interrupts and resume](#interrupts-and-resume).

## Interrupts and resume

When the agent pauses (a LangGraph `interrupt()`, such as an engine approval gate),
the run ends with `RUN_FINISHED.outcome = {"type": "interrupt", "interrupts": [...]}`.
Each interrupt has an `id`, a `reason`, and an optional `message` / `toolCallId` /
`metadata`. AG-UI 1.0 has no separate INTERRUPT event.

To continue, send another `RunAgentInput` on the same thread with `resume`: one
`ResumeEntry` per open interrupt. This is what `@ag-ui/client`'s `buildResumeArray`
produces.

```bash
curl -N -X POST localhost:8000/v1/agui -H 'content-type: application/json' -d '{
  "threadId":"t-42","runId":"r-2","messages":[],
  "resume":[{"interruptId":"<id from outcome>","status":"resolved","payload":{"action":"approve"}}],
  "tools":[],"context":[],"state":{},"forwardedProps":{}}'
```

- When `resume` is non-empty, `messages` is not read. Each `payload` goes to the
  engine unchanged as `Command(resume={interruptId: payload})`. The host never
  interprets it.
- The resumed run streams like any run: usage, `RUN_FINISHED` (`success`, or
  `interrupt` again), and a usage record.
- Interrupt ids are **opaque**: `"<engine id>.<checkpoint_id>"`. Send them back
  exactly as received. The suffix ties an answer to the checkpoint it was asked at, so
  a retried answer can never approve the *next* prompt of the same step.
- **A resume is not cancelled when the client disconnects.** The approved step runs to
  completion in the background, and its output is discarded. Reconnect and inspect the
  thread instead of re-sending. A re-sent resume gets `409` (`resume_in_progress`
  while the step runs, then `no_pending_interrupt`). Plain runs are still cancelled on
  disconnect.
- Refusals, all before streaming:

  | Code | When |
  |---|---|
  | `409 no_pending_interrupt` | Nothing to resume, or the interrupt was already answered (stale checkpoint suffix). A **retried** resume gets this, so a retry never resumes twice |
  | `409 resume_in_progress` | Another resume of this thread is still running. The guard is per process, so with several replicas route each thread to one replica |
  | `422 invalid_resume` | The ids are not exactly the open interrupts (unknown, missing, duplicate, or missing the checkpoint suffix) |
  | `422 unsupported_resume` | A `cancelled` entry. Cancel semantics are still to be defined by the engine |

- Pending interrupts are read from the engine checkpoint, and they are org-scoped like
  the thread.
- Traces mark resume runs with `prismal.agui.resume` and `prismal.agui.resume_count`.

A recorded run (interrupt → resume, fake mode) is published at
[`fixtures/agui/interrupt-resume.json`](../fixtures/agui/interrupt-resume.json) for
clients to replay. Client-declared **frontend tools** need engine support first (see
[`specs/host-v0.3-agui-resume/ENGINE-PROMPT.md`](../specs/host-v0.3-agui-resume/ENGINE-PROMPT.md)).
Until then they are ignored and logged.

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
