# Engine prompt E-CT · AG-UI client (frontend) tools

To run in the engine repo (`prismal`). The host side
([DESIGN.md](./DESIGN.md)) is already in place: `POST /v1/agui` resumes interrupted
threads with `Command(resume={id: payload})`.

```
You are working in prismal (the engine). Read prismal/events/agui.py (AG-UI
translator, RUN_FINISHED.outcome interrupt), prismal/agents/graph.py
(get_async_compiled_graph, tool_provider binding), prismal/agents/tool_registry.py
and prismal/agents/subgraphs/gates.py (the existing interrupt()/Command(resume)
pattern).

Goal: let an AG-UI frontend declare its own tools for one run. The model can call
them; the engine never executes them. A call suspends the run, and the frontend's
result resumes it. The host (prismal-server) must stay a pure transport: it passes
the client tools and results through and holds no tool logic.

Scope:
1. Input: get_async_compiled_graph(...) or the run config accepts
   `client_tools: list[{name, description, parameters (JSON Schema)}]`
   (the AG-UI `Tool` shape) for one run, e.g. under
   config["configurable"]["client_tools"]. These schemas are bound to the model for
   that run only. They are never added to the shared registry, never visible to
   other threads or tenants, and are validated: name pattern, schema size cap, and
   no collision with server tools (a server tool with the same name wins and the
   collision is audited).
2. Suspension: when the model calls a client tool, the engine emits the
   TOOL_CALL_START/ARGS/END events as usual and then calls
   interrupt({"reason": "client_tool", "tool_call_id": <id>, "tool_name": <name>,
   "args": <args>}). The translator already maps this to
   RUN_FINISHED.outcome.interrupts[] with `toolCallId`. Parallel client tool calls
   become one interrupt each.
3. Resume: Command(resume={interrupt_id: <result>}) where <result> is the tool's
   output (any JSON). The engine appends it as the ToolMessage for that
   tool_call_id and continues the loop. A malformed result becomes a tool error
   message, not a crash.
4. Cancel: define what a `cancelled` AG-UI resume means for any interrupt (client
   tool and HITL gate alike), and expose it as a documented resume value the host
   can forward without interpreting it (e.g. a sentinel
   `{"__agui_status__": "cancelled"}`). For a client tool, the call becomes a tool
   error "cancelled by user"; for a HITL gate, `reject`.
5. Security: client tool args and results are untrusted input. The L1 sanitizer
   and the ActionInterceptor audit apply to them like any tool result. Budgets
   count the model calls around them.
6. Atomic resume and occurrence-unique ids:
   (a) make interrupt ids unique per occurrence, for example a hash of task id,
       interrupt index and checkpoint id. Use the same id in the translator's
       RUN_FINISHED.outcome and in aget_state().interrupts. Today LangGraph reuses
       one id for every interrupt() of a node invocation (the CUA approval loop), and
       the host works around it with a checkpoint fence ("<id>.<checkpoint_id>").
   (b) make resuming an interrupt a compare-and-consume in the checkpointer, so a
       second Command(resume) for an already-consumed interrupt fails instead of
       re-running the node. The host's per-process guard does not cover multiple
       replicas.
7. Tests: a fake-model run that calls a client tool, interrupts, resumes with a
   result, and finishes. Also: collision, oversize schema, cancel.

Acceptance (EARS):
- WHEN the model calls a client tool THE SYSTEM SHALL end the run with
  RUN_FINISHED.outcome.type = "interrupt" and one interrupt per call carrying
  its toolCallId, and SHALL NOT execute the tool server-side.
- WHEN resumed with a result for every pending client-tool interrupt THE SYSTEM
  SHALL continue the same thread with those results as tool messages.
- IF a client tool name collides with a server tool THEN the server tool SHALL
  win and the collision SHALL be audited.

Out of scope: host changes (prismal-server forwards `tools` after release),
WebSocket transport.
```

## Host follow-up once the engine ships

- `routes/agui.py`: forward `RunAgentInput.tools` as `client_tools`. Today they are
  ignored and logged.
- Forward `cancelled` resume entries as the engine's documented value. They are
  refused with `422 unsupported_resume` until then.
- Extend the fixture with a client tool round-trip.

## Cached graph checkpointer (resolved in the engine)

`get_async_compiled_graph()` kept a non-daemon `aiosqlite` thread open, so the
process could not exit. prismal-ai 3.20.0 added `aclose_async_compiled_graph()`
(commit `23a35c0`); the host adopted it in
[HOST-PROMPT-engine-3.20.md](./HOST-PROMPT-engine-3.20.md) (H-320, done). That
close hangs on its own in 3.20.0; the follow-up fix is
[ENGINE-PROMPT-graph-close.md](./ENGINE-PROMPT-graph-close.md).
