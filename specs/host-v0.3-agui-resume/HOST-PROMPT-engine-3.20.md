# Host prompt H-320 · Adopt prismal-ai 3.20 and close the engine graph at shutdown

The engine side is already done: prismal commit `23a35c0` ("fix(graph): close async
checkpointers so processes can exit"), released in **prismal-ai 3.20.0** on PyPI, adds
`prismal.agents.graph.aclose_async_compiled_graph()`. This prompt is the host side.

```
You are working in prismal-server (FastAPI reference host for the prismal engine).
Read CLAUDE.md (the four seams, the review checklist and the engine pin),
src/prismal_server/app.py (create_app, lifespan), src/prismal_server/deps.py
(_default_builder, RuntimeRegistry.aclose_all), tests/unit/test_import_guard.py
(the allowlist, HOST_EXCEPTIONS) and tests/contract/conftest.py (the current
workaround).

Problem: prismal.agents.graph.get_async_compiled_graph() caches a module-level graph.
Its SQLite checkpointer is an aiosqlite connection running on a NON-daemon worker
thread. prismal-server's _default_builder calls it, so after uvicorn's lifespan
shutdown the interpreter waits on that thread forever. A container then needs
SIGKILL, and the contract CI job hung until tests/contract/conftest.py stopped the
connection by hand.

Engine fix, already released in prismal-ai 3.20.0:
`async def aclose_async_compiled_graph() -> None` in prismal.agents.graph. It closes
every checkpointer that build_checkpointer() opened (sqlite connections plus
postgres context managers) and resets the cached graph. It is idempotent and logs
failures without raising.

Scope (TDD: failing test, then minimal code, then green):
1. Engine pin: raise the floor to `prismal-ai>=3.20,<4` in pyproject.toml, and
   update CLAUDE.md's checklist line ("The engine dep stays pinned …") and README
   to match. Run `uv lock --upgrade-package prismal-ai` locally (uv.lock stays
   untracked) and confirm `uv pip show prismal-ai` reports 3.20.x.
2. Seam: `aclose_async_compiled_graph` belongs to the execution seam
   (prismal.agents.graph). Add it to the import-guard allowlist next to
   get_async_compiled_graph, with a comment citing this prompt. No other new engine
   import.
3. Lifespan: in app.py's lifespan `finally`, call
   `await aclose_async_compiled_graph()` AFTER `registry.aclose_all()` (live
   runtimes close their own checkpointers first) and before or alongside
   `tracing.aclose()`. Never let it raise: wrap it in try/except with a warning
   log. Skip it in fake-LLM mode, which never builds the engine graph.
   Make it injectable for tests, the way graph_factory is (e.g. a
   `graph_closer` parameter of create_app, defaulting to the engine function).
4. Tests:
   - unit: the lifespan calls the injected closer exactly once, after
     registry.aclose_all, and a raising closer still lets shutdown finish;
   - unit: fake mode does not call it;
   - contract: delete the session fixture in tests/contract/conftest.py (or the
     whole file) and prove the suite exits by itself. Add a contract test that
     runs `python -c "<build app with the real registry, run its lifespan
     startup+shutdown, exit>"` in a subprocess with a 60 s timeout and asserts
     returncode 0 (it hangs without the fix).
5. Docs: CHANGELOG `[Unreleased]` → Fixed ("process exits cleanly on shutdown:
   the engine graph's checkpointer is closed; requires prismal-ai 3.20"), and
   remove the "Separate engine issue" section from
   specs/host-v0.3-agui-resume/ENGINE-PROMPT.md (replace it with a one-line
   pointer to this prompt).

Acceptance (EARS):
- WHEN the host's lifespan shuts down THE SYSTEM SHALL close the engine graph's
  checkpointer connections, so the process exits without SIGKILL.
- IF closing the engine graph fails THEN shutdown SHALL complete and log a
  warning (no secret or message content in the log).
- THE contract suite SHALL exit on its own after its last test.

Out of scope: any change in the engine repo; the other ENGINE-PROMPT items
(client tools, cancel semantics, occurrence-unique interrupt ids, atomic resume).
```
