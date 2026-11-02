# Integration prompts — prismal-server

Part of the cross-project integration map (2026-10-31): #1 AG-UI prismal → agentkit,
#2 OTel → agentlens, **#3 ACP/A2A Umbral ⇄ prismal**, #4 JSON-RPC Umbral → xtermjs,
#5 shared MCP host. This host touches #1 and #3.

Counterparts: `reflex-agentkit/docs/integration-prompts.md`, `umbral/docs/integration-prompts.md`.

---

## Prompt I1-B · AG-UI 1.0 SSE endpoint

```
You are working in prismal-server (FastAPI/ASGI reference host for the prismal
engine). Read README.md, specs/reference-host-bootstrap/SPEC.md, docs/auth.md and,
in the engine, prismal/events/agui.py (the pure astream → AG-UI translator,
SPEC-PRT-AGU-001/002) and prismal/events/subagent.py.

Goal: expose the engine's AG-UI translator over HTTP so any AG-UI client (the
first one being reflex-agentkit) can drive a prismal run.

Scope:
1. `POST /threads/{thread_id}/agui`: body is an AG-UI `RunAgentInput`
   (threadId, runId, messages, tools, context, state, forwardedProps). Response
   is `text/event-stream`; each SSE `data:` line is one AG-UI event as the
   translator emits it, in order, ending with RUN_FINISHED or RUN_ERROR.
   Reuse the thread/session plumbing of `POST /threads/{id}/messages`; do not
   re-implement streaming.
2. Client-side tools: `tools` from the input are registered for the run as
   client tools (the engine's deferred tool mechanism); a tool call the engine
   cannot execute server-side is emitted as TOOL_CALL_* and the run ends with
   the interrupt outcome, exactly as prismal/events/agui.py documents.
3. Resume: a second POST to the same thread with the tool results in `messages`
   (role `tool`) continues the run. Idempotency through runId.
4. Auth and tenancy: same middleware as the other routes; `include_metadata`
   query flag gated by a scope, since CUSTOM prismal.routing/decision reveal
   routing internals.
5. OpenAPI: document the endpoint and the SSE event schema so prismal-sdk can
   generate its client (it already models AG-UI 1.0 streaming in v0.2).
6. Tests: contract test in fake-LLM mode that records a run (text, tool call,
   interrupt, resume, usage) as a fixture file; publish that fixture in the
   repo so reflex-agentkit can replay it.

Acceptance (EARS):
- THE SYSTEM SHALL emit exactly the 31 AG-UI 1.0 event types and no others.
- WHEN the translator reports a translation error THE SYSTEM SHALL still end
  the stream with RUN_ERROR and increment prismal.agui_translation_errors_total.
- IF the client disconnects THEN the run SHALL be cancelled within 1 s and the
  cancellation audited.

Out of scope: WebSocket AG-UI transport; UI.
```

## Prompt I3-B · prismal as an ACP agent for Umbral

```
You are working in prismal-server. Read Umbral's docs/spikes/2026-10-acp-opencode.md
and specs/prd/umbral-f2.md §REQ-ACP-* (in the umbral repository) to learn how Umbral
launches, mediates and cancels an ACP agent, and the Agent Client Protocol spec
(agentclientprotocol.com) plus its Python SDK.

Goal: a `prismal-acp` entry point (console script) that speaks ACP over stdio so
Umbral can run a prismal agent as one more thread, with Umbral mediating
permissions, files and terminals.

Scope:
1. `prismal_server/acp/`: initialize, session/new, session/prompt, session/cancel;
   stream agent output as ACP session updates; map prismal tool calls that touch
   the filesystem or shell to ACP permission requests (fs/read_text_file,
   fs/write_text_file, terminal/*), so that Umbral's policy — not prismal's
   ActionInterceptor alone — is the gate when running inside Umbral.
2. Permission layering: write a short design note deciding what happens when
   both prismal's ActionInterceptor and Umbral's policy apply. Recommended:
   inside ACP, ActionInterceptor runs in "audit-only" mode and defers the
   decision to the client; outside ACP, unchanged. Make this a settings flag.
3. Agent manifest for Umbral's third-party agent detection (T-F2-10): name,
   command, capabilities, egress_visibility.
4. Reuse the same runtime composition as the HTTP host (build_runtime()); no
   engine logic in the ACP layer.
5. Tests: a scripted ACP client fixture that runs initialize → session/new →
   prompt → one permission round-trip → cancel, in fake-LLM mode.

Acceptance (EARS):
- WHEN Umbral denies a permission request THE SYSTEM SHALL abort that tool call
  and continue the turn without executing it.
- WHILE running under ACP THE SYSTEM SHALL emit no network egress that the
  manifest does not declare.

Out of scope: A2A (already served at /a2a); remote ACP over SSH.
```
