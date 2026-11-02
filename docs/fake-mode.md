# Fake-LLM mode

`PRISMAL_SERVER_HOST_FAKE_LLM_MODE=true` serves chat from a **deterministic
script** with no LLM provider and no credential. It exists for contract tests in
SDKs and front-ends (for example the `prismal-sdk` contract suite).

```bash
PRISMAL_SERVER_HOST_FAKE_LLM_MODE=true uvicorn prismal_server.app:app
```

- Runtimes come from the engine's `build_test_runtime()`, which does no I/O and
  composes no gateways. `tenant.yaml` is ignored in this mode.
- **Refused in production.** With `host_environment=production`, the settings fail
  validation and the process does not start.
- Every response carries `X-Prismal-Fake-LLM: 1`. That covers HTTP and SSE
  responses and the WebSocket accept. `/readyz` still answers `{"status":"ready"}`.

## Script

| Input `content` | Result |
|---|---|
| `/tool <q>` | one `tool_call` `{"name":"fake_tool","args":{"q":"<q>"}}` |
| `/error` | terminal `error` `{"code":"internal_error","message":"fake error"}` |
| anything else | `"echo: " + content[:200]`, streamed as `token` chunks of at most 16 chars |

Every successful turn ends with `done.usage` set to:

```json
{"prompt_tokens":10,"completion_tokens":5,"cache_read_tokens":0,"cache_write_tokens":0,
 "reasoning_tokens":0,"total_tokens":15,"calls":1,"cost_usd":0.0,"cost_source":"none",
 "estimated":true,"served_model":"fake/echo","gateway":null}
```

Each turn is recorded in the usage store, so `/v1/usage` sums them. The AG-UI mode
(engine `PRISMAL_AGUI_EVENTS_ENABLED=true`) runs the script through the engine's
real AG-UI translator.

Inbound A2A is not composed in fake mode (the engine test runtime has no A2A
handler), so `POST /a2a` returns the "not enabled" JSON-RPC error. The host's
contract suite instead covers A2A v1 by wrapping the released engine's
`A2AServerHandler` around a scripted graph (`tests/contract/test_v02_contract.py`).
