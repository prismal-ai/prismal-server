# AG-UI Interrupt Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `POST /v1/agui` resumes a thread's pending LangGraph interrupts from AG-UI `RunAgentInput.resume`.

**Architecture:** `routes/agui.py` parses `resume`. `routes/threads.run_turn`/`open_turn` accept an
optional resume map. `open_turn` validates it against `graph.aget_state(config).interrupts` and feeds
`Command(resume=…)` as graph input. Fake mode scripts an interrupt.

**Tech Stack:** FastAPI, LangGraph `Command`/`Interrupt`, the engine AG-UI translator, OTel span attributes.

**Spec:** `specs/host-v0.3-agui-resume/DESIGN.md`

Branch `feat/rh3-2-agui-resume`.

## Global Constraints

- Engine pin unchanged: `prismal-ai>=3.17,<4`. Add `langgraph>=1.0,<2` as an explicit dep.
- No new `prismal` import (the import-guard test must stay green).
- The host never reads or rewrites resume payloads (DESIGN §2 anti-goal).
- Error codes, verbatim: `unsupported_resume` (422), `no_pending_interrupt` (409), `invalid_resume` (422).
- Span attributes, verbatim: `prismal.agui.resume`, `prismal.agui.resume_count`.

## Review Focus

1. Retried resume (same `runId`/entries): it must be `409`, never a second resume (Task 2 `test_resume_retry_is_409`).
2. Resume on another tenant's thread with the same `thread_id`: the engine thread key is org-scoped, so it must be `409` (Task 2 `test_resume_is_tenant_scoped`).
3. A `resume` array plus a `user` message: resume wins and the message is not sent (Task 2 `test_resume_ignores_messages`).
4. A refusal must release the runtime lease and fail the span with the code (Task 2 `test_resume_refusals` checks the span and lease release).
5. The v0.2 path without `resume` must be unchanged (the existing AG-UI tests stay green).

---

### Task 1: Fake-mode interrupts

**Files:** Modify `src/prismal_server/fake/graph.py`. Test `tests/unit/test_fake_interrupt.py`.

**Produces:** `FakeGraph.aget_state(config) -> SimpleNamespace(interrupts=tuple[Interrupt, ...])`.
`/interrupt <msg>` yields `("updates", {"__interrupt__": (Interrupt(value={"reason": "input_required", "message": msg}, id=f"fake-int-{n}"),)})`
(tagged mode only; native mode yields nothing for it) and stores it as pending for the thread.
A `Command` input clears the pending interrupts and echoes `"resumed: " + json.dumps(resume, sort_keys=True)` in chunks.

- [ ] Test: interrupt, then `aget_state` returns 1 interrupt with id `fake-int-1`. A `Command(resume={"fake-int-1": {"ok": True}})` yields text `resumed: {"fake-int-1": {"ok": true}}` and leaves no pending interrupts. Run it and see it FAIL.
- [ ] Implement it, then see it PASS. Commit "Fake-mode interrupts for AG-UI resume (RH3-02-01)".

### Task 2: Resume path

**Files:** Modify `routes/agui.py`, `routes/threads.py`, `errors.py`, `pyproject.toml`. Test `tests/unit/routes/test_agui_resume.py`.

**Consumes:** Task 1. **Produces:** `ResumeEntry` model; `open_turn(..., resume: dict[str, Any] | None = None)`;
exceptions `UnsupportedResume`, `NoPendingInterrupt`, `InvalidResume`, mapped in `_NAME_MAP`.

- [ ] Tests (fake mode, AG-UI engine flag on):
  - happy path (`RUN_FINISHED.outcome.type == "interrupt"` with id `fake-int-1`, then resume → text `resumed:` and `RUN_FINISHED` success with usage);
  - `test_resume_refusals` (cancelled → 422 `unsupported_resume`; nothing pending → 409; unknown id → 422 `invalid_resume`; duplicate id → 422);
  - `test_resume_retry_is_409`;
  - `test_resume_is_tenant_scoped`;
  - `test_resume_ignores_messages`;
  - span attrs `prismal.agui.resume`/`_count`.

  Run them and see them FAIL.
- [ ] Implement it. In `open_turn`, the validation goes after `graph_factory` and before `ChunkPump`, inside the existing span activation and inside the lease try. Run and see PASS, then run the full unit suite. Commit "Resume interrupted AG-UI runs (RH3-02-02)".

### Task 3: Fixture, OpenAPI, docs

**Files:** Create `fixtures/agui/interrupt-resume.json` and `tests/contract/test_agui_resume_fixture.py`. Modify `openapi_examples.py` (a resume example on `/v1/agui`), `docs/agui.md`, `CHANGELOG.md`, this file (statuses).

- [ ] Contract test: run the two requests in fake mode, normalise (drop `timestamp`), and compare with the fixture. Regenerate when `PRISMAL_SERVER_UPDATE_FIXTURES=1`. Generate once, read it, then commit.
- [ ] Docs, then the full gate (ruff, format, mypy, unit, contract). Commit "AG-UI resume fixture and docs (RH3-02-03)".

## Status

| ID | Task | Status |
|---|---|---|
| RH3-02-01 | Fake-mode interrupts | `TODO` |
| RH3-02-02 | Resume path | `TODO` |
| RH3-02-03 | Fixture, OpenAPI, docs | `TODO` |
