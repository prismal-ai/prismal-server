# Host v0.3 — AG-UI interrupt resume (RH3-02)

Status: **APPROVED v1.0** (2026-11-02). The user chose to split I1-B: the host part
lands now, and client tools wait for the engine ([ENGINE-PROMPT.md](./ENGINE-PROMPT.md)).

## 1. Intent

The target case: an AG-UI frontend (reflex-agentkit first) runs an agent, the agent
pauses for something only the frontend can give (an approval today; the result of a
frontend-owned tool once the engine supports client tools), and the frontend then
**resumes the same run**.

The engine already pauses. A LangGraph `interrupt()` (the HITL gates, the CUA agent)
ends the AG-UI stream with
`RUN_FINISHED.outcome = {"type": "interrupt", "interrupts": [{id, reason, message?, toolCallId?, metadata}]}`
(engine `prismal.events.agui`). What is missing is the way back: the host has no way
to resume an interrupted thread. This phase adds it, using the AG-UI 1.0 contract.

**Why the split.** Frontend tools need engine work: the frontend's tool schemas must be
bound to the model, and a call to one of them must suspend the run. Neither exists in
prismal 3.19/3.20, and the host may not add tool logic (CLAUDE.md hard rule). The
engine contract is written up in `ENGINE-PROMPT.md`. When it ships, the host forwards
`tools` and returns `role: "tool"` results through the same resume path built here.

## 2. Goals / non-goals / anti-goals

**Goals**
- `POST /v1/agui` accepts AG-UI `RunAgentInput.resume: ResumeEntry[]` and resumes the
  thread's pending interrupts. The resumed run streams AG-UI like any other run and
  carries usage, tracing and the usage record.
- Every refusal (nothing to resume, unknown or missing ids) happens before streaming,
  with a stable error code.
- A retried resume (same `runId`, same entries) never resumes twice.
- A recorded fake-mode fixture (text → interrupt → resume → text + usage) that
  reflex-agentkit can replay.

**Non-goals (this phase)**
- Client-declared `tools` (engine first; they are still ignored and logged).
- Resume over WebSocket or native `POST /threads/{id}/messages`.
- `cancelled` resume entries (§4.3).
- The deprecated `forwardedProps.command.resume` form.
- Re-gating `include_metadata` by scope (kept as is from RH2, `agui_include_metadata`
  per tenant).
- The `POST /threads/{id}/agui` alias from the original I1-B prompt (`POST /v1/agui`
  already serves `RunAgentInput`).

**Anti-goals**
- The host must not interpret resume payloads. It never maps a payload to
  approve/reject or reads its keys; the payload is graph input.
- The host must not keep its own record of interrupts. The engine checkpoint is the
  only source of truth.

## 3. Contract

### 3.1 Input

```jsonc
{
  "threadId": "t-1",
  "runId": "r-2",
  "messages": [],
  "resume": [
    {"interruptId": "int-abc", "status": "resolved", "payload": {"action": "approve"}}
  ]
}
```

`ResumeEntry` = `{interruptId: str (min 1), status: "resolved" | "cancelled", payload?: any, metadata?: object}`.

When `resume` is non-empty:
- `messages` is not read and needs no `user` message;
- the graph input is `langgraph.types.Command(resume={interruptId: payload, ...})`. LangGraph
  supports this id-keyed map for one or several interrupts. `payload` defaults to `null`.

When `resume` is absent or empty, behaviour is exactly as in v0.2.

### 3.2 Validation (inside the turn span, before streaming)

The host reads the pending interrupts from the engine checkpoint with
`await graph.aget_state(config)` (same compiled graph, same `config`), then checks:

| Case | Response |
|---|---|
| Any entry has `status: "cancelled"` | `422 unsupported_resume` |
| Another resume of the same thread is still running (this process) | `409 resume_in_progress` |
| The thread has no pending interrupt | `409 no_pending_interrupt` |
| The ids are not exactly the pending set (unknown, missing or duplicate) | `422 invalid_resume` |
| AG-UI disabled | `406 protocol_not_enabled` (unchanged) |

"Exactly the pending set" follows the AG-UI client's `buildResumeArray`: every open
interrupt is answered, and no unknown id appears.

### 3.3 Idempotency

A retried resume finds the interrupts it answered already consumed in the checkpoint
and gets `409 no_pending_interrupt`. If the resumed run interrupts again, the new
interrupts have new ids, so the old entries are `invalid_resume`. This holds across
replicas because it rests only on the checkpoint. The host keeps no per-`runId` state.

**Concurrent resumes.** Two resumes of the same thread arriving together could both
pass the checkpoint check, and an approved action would then run twice. The host holds
an in-process claim per engine thread from validation until the turn's runtime lease is
released (stream end, error, refusal or the response backstop). A second resume
meanwhile gets `409 resume_in_progress`. This was raised by the security review; a
`checkpoint_id` pin was rejected because LangGraph forks from the given checkpoint
instead of refusing. **Remaining gap:** the claim is per process, so multi-replica
deployments need sticky routing per thread (or the engine-side atomic resume asked
for in ENGINE-PROMPT.md).

### 3.4 Output

Same AG-UI stream as any run, with `RUN_STARTED.runId = runId`. The resumed run ends
with `RUN_FINISHED` (`success`, or `interrupt` again) or `RUN_ERROR`.

### 3.5 Tracing (RH3-01)

The span gains `prismal.agui.resume = true` and `prismal.agui.resume_count = N`. Refusals
fail the span with the codes above.

## 4. Design decisions

1. **The checkpoint read goes through the execution seam.** `aget_state` is a method of
   the compiled graph from `get_async_compiled_graph`; no new engine import.
   `Command` comes from `langgraph.types`, which is not a `prismal` module, and
   `langgraph` becomes an **explicit** host dependency (`>=1.0,<2`; it is already
   resolved through the engine).
2. **Pending ids come from `StateSnapshot.interrupts`** (`Interrupt.id`), which are the
   same ids the engine translator puts on `RUN_FINISHED.outcome.interrupts[].id`.
3. **`cancelled` is refused (my call).** What cancelling means for a paused node
   (reject? skip? abort the run?) is engine policy. The host cannot pick a resume value
   for it without interpreting payloads (anti-goal). `ENGINE-PROMPT.md` asks the engine
   to define it, and the host will then forward `cancelled` as-is.
4. **Fake mode.** `FakeGraph` gets a `/interrupt <message>` script. It yields an
   `updates` chunk `{"__interrupt__": (Interrupt(value={"reason": "input_required", "message": …}, id=…),)}`
   and remembers the pending interrupt per engine thread. `aget_state` returns
   `interrupts`. A `Command` input clears them and echoes `"resumed: " + json(payload)`.
   It is deterministic, with no engine involvement, and is what the fixture records.

## 5. Fixture

`fixtures/agui/interrupt-resume.json`, committed. It holds two runs on one thread, each
with its `RunAgentInput` and the AG-UI events it produced, in order. `timestamp` fields
are dropped and generated ids are kept deterministic (fake mode). A contract test
regenerates it and compares, so a translator change shows up as a diff. Regenerate with
`PRISMAL_SERVER_UPDATE_FIXTURES=1`.

## 6. Testing

- Unit:
  - Resume happy path (fake graph: interrupt, then resume → `resumed: …` text,
    `RUN_FINISHED` success, usage).
  - One test per refusal row.
  - Retry → `409`.
  - v0.2 behaviour unchanged without `resume`.
  - The `Command` built contains exactly the id → payload map.
  - Span attributes.
- Contract: the fixture round-trip (fake mode).
- Docs: `docs/agui.md` resume section, CHANGELOG.
