"""Thread / chat streaming routes (RHB-02-02/03/04).

``POST /threads`` mints an opaque server-generated ``thread_id`` (THR-002).
``POST /threads/{thread_id}/messages`` resolves the tenant runtime, obtains the
compiled graph via the Execution seam, and streams ``graph.astream(...)`` as
``text/event-stream`` (CHT-001, THR-001/003). User content crosses a trust
boundary and is passed **only** as graph input — never interpolated into a prompt
(CHT-005); the graph owns its own security layers.

Seam: execution — ``get_async_compiled_graph`` (injected via ``app.state``).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from prismal.events.agui import AgUiContext
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from prismal_server import openapi_examples as ex
from prismal_server.auth import authenticate, resolve_org_id
from prismal_server.deps import Lease, release_after
from prismal_server.errors import is_budget_exceeded
from prismal_server.streaming import (
    STREAM_PROTOCOL_HEADER,
    ChunkPump,
    DoneHook,
    agui_events,
    agui_stream,
    native_events,
    sse_stream,
)
from prismal_server.tracing import (
    TurnSpan,
    conversation_id,
    fail_span,
    route_template,
)
from prismal_server.usage.models import UsageRecord, UsageWire
from prismal_server.usage.store import new_turn_id
from prismal_server.usage.summary import TurnUsageProbe

router = APIRouter(tags=["threads"])


# Shape of an org-scoped engine key prefix (`<len>:`). A single-tenant raw id
# with this shape could alias another org's key, so it is refused.
_SCOPED_KEY_RE = re.compile(r"^\d+:")


class InvalidThreadId(Exception):
    """422 ``invalid_thread_id``: a raw id shaped like an org-scoped engine key."""

    def __init__(self) -> None:
        super().__init__("thread_id must not start with '<digits>:'")


def engine_thread_id(org_id: str | None, thread_id: str) -> str:
    """The engine-side thread key: the client ``thread_id`` scoped to its org.

    The engine keys conversation state and run summaries by thread id alone,
    process-wide, and clients choose their own ids — so two tenants using the
    same id would share history and usage. The length prefix makes the mapping
    injective (``("a:b", "c")`` ≠ ``("a", "b:c")``). Single-tenant (``None``)
    keeps the raw id for continuity, so a raw id that *looks* scoped
    (``<digits>:…``) is refused — otherwise it could alias another org's key.
    Clients only ever see their own ``thread_id``.
    """
    if org_id is not None:
        return f"{len(org_id)}:{org_id}:{thread_id}"
    if _SCOPED_KEY_RE.match(thread_id):
        raise InvalidThreadId()
    return thread_id


class MessageIn(BaseModel):
    """A chat turn's request body (SPEC.md §3.2)."""

    content: str = Field(min_length=1)
    role: str = "user"
    metadata: dict[str, Any] = {}


@router.post("/threads")
async def create_thread() -> dict[str, str]:
    """Mint a new opaque thread id (clients may also choose their own)."""
    return {"thread_id": uuid4().hex}


Protocol = Literal["prismal", "ag-ui"]
_AGUI_STREAM_MODES = ["messages", "updates", "custom"]


class UnsupportedResume(Exception):
    """422 ``unsupported_resume``: a ``cancelled`` entry (engine semantics pending)."""

    def __init__(self) -> None:
        super().__init__("cancelled resume entries are not supported yet")


class NoPendingInterrupt(Exception):
    """409 ``no_pending_interrupt``: the thread has nothing to resume."""

    def __init__(self) -> None:
        super().__init__("the thread has no pending interrupt to resume")


class ResumeInProgress(Exception):
    """409 ``resume_in_progress``: another resume of this thread is running."""

    def __init__(self) -> None:
        super().__init__("a resume of this thread is already in progress")


class InvalidResume(Exception):
    """422 ``invalid_resume``: ids are not exactly the pending interrupts."""

    def __init__(self) -> None:
        super().__init__("resume must answer exactly the pending interrupts")


class ResumeEntry(BaseModel):
    """An AG-UI 1.0 ``ResumeEntry`` (the payload is graph input, never read)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    interrupt_id: str = Field(alias="interruptId", min_length=1)
    status: Literal["resolved", "cancelled"]
    payload: Any = None


async def _resume_input(
    graph: Any,
    config: dict[str, Any],
    resume: list[ResumeEntry],
    *,
    claims: set[str],
    lease: Lease,
) -> Command[Any]:
    """``Command(resume={id: payload})`` after checking the engine checkpoint.

    The pending set comes only from the engine (``aget_state``); a retried
    resume finds its interrupts consumed and is ``409`` (DESIGN §3.3). A
    concurrent resume of the same engine thread is refused while the first
    holds its claim, which is freed with the turn's runtime lease (in-process;
    replicas need sticky routing per thread).
    """
    if any(entry.status == "cancelled" for entry in resume):
        raise UnsupportedResume()
    key = config["configurable"]["thread_id"]
    if key in claims:  # check-and-add with no await between: atomic per loop
        raise ResumeInProgress()
    claims.add(key)
    lease.on_release(lambda: claims.discard(key))
    snapshot = await graph.aget_state(config)
    pending = {intr.id for intr in getattr(snapshot, "interrupts", ())}
    if not pending:
        raise NoPendingInterrupt()
    ids = [entry.interrupt_id for entry in resume]
    if len(ids) != len(set(ids)) or set(ids) != pending:
        raise InvalidResume()
    return Command(resume={entry.interrupt_id: entry.payload for entry in resume})


class ProtocolNotEnabled(Exception):
    """406 ``protocol_not_enabled``: AG-UI is off on the host or the tenant engine."""

    def __init__(self) -> None:
        super().__init__("the AG-UI stream protocol is not enabled")


def wants_agui(request: Request, protocol: Protocol | None) -> bool:
    """``?protocol=`` wins; else ``Accept: text/event-stream; profile=ag-ui``."""
    if protocol is not None:
        return protocol == "ag-ui"
    for media_range in request.headers.get("accept", "").split(","):
        for param in media_range.split(";")[1:]:
            key, _, value = param.partition("=")
            if key.strip().lower() == "profile" and value.strip().strip('"') == "ag-ui":
                return True
    return False


def _engine_settings(runtime: Any) -> Any:
    """The tenant runtime's effective engine settings (process settings if unset)."""
    settings = getattr(runtime, "settings", None)
    if settings is None:
        from prismal.a2a import get_settings  # noqa: PLC0415 - A2A seam re-export

        settings = get_settings()
    return settings


class Turn:
    """One running chat turn, independent of the transport (SSE or WebSocket).

    Holds the runtime lease and the graph pump; :meth:`events` yields the
    native ``(name, data)`` or engine AG-UI events (``None`` = idle heartbeat)
    and :meth:`aclose` is the idempotent cleanup.
    """

    def __init__(
        self,
        *,
        lease: Lease,
        pump: ChunkPump,
        thread_id: str,
        engine_settings: Any,
        probe: TurnUsageProbe,
        record_usage: DoneHook,
        agui: bool,
        run_id: str | None,
        heartbeat_s: float,
        span: TurnSpan | None = None,
    ) -> None:
        self.span = span
        self.lease = lease
        self.pump = pump
        self.thread_id = thread_id
        self.agui = agui
        self._engine_settings = engine_settings
        self._probe = probe
        self._record_usage = record_usage
        self._run_id = run_id
        self._heartbeat_s = heartbeat_s

    def events(self) -> AsyncGenerator[Any]:
        if not self.agui:
            return native_events(
                self.pump,
                thread_id=self.thread_id,
                heartbeat_s=self._heartbeat_s,
                usage_for=self._probe.usage,
                on_done=self._record_usage,
                span=self.span,
            )
        return agui_events(
            self.pump,
            ctx=self._agui_context(),
            heartbeat_s=self._heartbeat_s,
            summary_for=self._probe.summary,
            on_done=self._record_usage,
            span=self.span,
        )

    def sse_body(self) -> AsyncIterator[str]:
        if not self.agui:
            return sse_stream(
                self.pump,
                thread_id=self.thread_id,
                heartbeat_s=self._heartbeat_s,
                usage_for=self._probe.usage,
                on_done=self._record_usage,
                span=self.span,
            )
        return agui_stream(
            self.pump,
            ctx=self._agui_context(),
            heartbeat_s=self._heartbeat_s,
            summary_for=self._probe.summary,
            on_done=self._record_usage,
            span=self.span,
        )

    def _agui_context(self) -> AgUiContext:
        engine = self._engine_settings
        return AgUiContext(
            run_id=self._run_id or uuid4().hex,
            thread_id=self.thread_id,  # client-facing id, never the engine key
            include_state_deltas=bool(
                getattr(engine, "agui_include_state_deltas", True)
            ),
            include_usage=bool(getattr(engine, "agui_include_usage", True)),
            include_metadata=bool(getattr(engine, "agui_include_metadata", True)),
        )

    async def aclose(self) -> None:
        if self.span is not None:
            self.span.end()  # no-op when the stream already ended it
        await self.pump.aclose()
        await self.lease.release()

    async def release(self) -> None:
        """Response backstop: end the span and free the lease (idempotent)."""
        if self.span is not None:
            self.span.end()
        await self.lease.release()


async def open_turn(
    state: Any,
    *,
    org_id: str | None,
    thread_id: str,
    role: str,
    content: str,
    agui: bool,
    run_id: str | None = None,
    pump_maxsize: int = 0,
    span: TurnSpan | None = None,
    resume: list[ResumeEntry] | None = None,
) -> Turn:
    """Start one chat turn for an already-resolved tenant (``state`` = app.state).

    Org-scoped engine thread key, runtime lease, AG-UI gate
    (``ProtocolNotEnabled`` before any graph work), usage record on ``done``.
    User content is passed **only** as graph input (CHT-005).
    """
    settings = state.settings
    store = state.usage_store
    engine_thread = engine_thread_id(org_id, thread_id)
    probe = TurnUsageProbe(engine_thread, state.run_summary)

    async def record_usage(finish_reason: str, usage: UsageWire | None) -> None:
        # SPEC-RH2-USE-001: one record per finished turn with non-null usage.
        if usage is None:
            return
        await store.append(
            UsageRecord(
                org_id=org_id,
                thread_id=thread_id,
                turn_id=new_turn_id(),
                finished_at=datetime.now(UTC),
                finish_reason=finish_reason,
                usage=usage,
            )
        )

    # The turn leases its runtime so a tenant-config reload or LRU eviction
    # closes it only after the stream ends (SPEC-RH2-TEN-004).
    lease = await state.registry.acquire(org_id)
    try:
        engine_settings = _engine_settings(lease.runtime)
        if agui and not (
            settings.host_agui_enabled
            and getattr(engine_settings, "agui_events_enabled", False)
        ):
            raise ProtocolNotEnabled()  # SPEC-RH2-AGU-003: before streaming
        config = {"configurable": {"thread_id": engine_thread, "org_id": org_id}}
        graph_input: Any = {"messages": [{"role": role, "content": content}]}
        stream_mode: Any = _AGUI_STREAM_MODES if agui else "messages"
        # Only the per-turn graph and its pump task run under the turn span;
        # the runtime lease (which may build a runtime and start long-lived
        # engine tasks) must not inherit it.
        with span.activate() if span is not None else contextlib.nullcontext():
            graph = await state.graph_factory(
                tool_provider=getattr(lease.runtime, "tool_provider", None)
            )
            if resume:  # AG-UI resume (RH3-02): the user content is not sent
                graph_input = await _resume_input(
                    graph,
                    config,
                    resume,
                    claims=state.resume_claims,
                    lease=lease,
                )
            pump = ChunkPump(
                graph.astream(graph_input, config, stream_mode=stream_mode),
                maxsize=pump_maxsize,
            )
    except BaseException:
        await lease.release()
        raise
    return Turn(
        lease=lease,
        pump=pump,
        thread_id=thread_id,
        engine_settings=engine_settings,
        probe=probe,
        record_usage=record_usage,
        agui=agui,
        run_id=run_id,
        heartbeat_s=settings.sse_heartbeat_s,
        span=span,
    )


async def run_turn(
    request: Request,
    *,
    thread_id: str,
    role: str,
    content: str | Callable[[], str],
    agui: bool,
    run_id: str | None = None,
    resume: list[ResumeEntry] | None = None,
) -> StreamingResponse:
    """Run one chat turn over HTTP and stream it natively or as AG-UI.

    Shared by ``POST /threads/{id}/messages`` and ``POST /v1/agui``: auth and
    tenant resolution, then :func:`open_turn`; a budget refusal before the
    first token is HTTP 402. ``content`` may be a callable so input validation
    runs inside the turn's span (``POST /v1/agui``'s ``invalid_input``).

    One root span per turn (RH3-01): opened after authentication (a ``401`` is
    not a run), failed with the host error code on any refusal, and ended by
    the stream when its terminal event is sent.
    """
    # Resolve identity (401 in strict mode) then the tenant (TEN-001, AUT-005).
    identity = await authenticate(request)
    state = request.app.state
    settings = state.settings
    span = state.tracing.turn(
        route=route_template(request),
        transport="sse",
        protocol="ag-ui" if agui else "native",
    )
    try:
        org_id = resolve_org_id(request, identity)
        span.set_tenant(org_id, conversation_id(org_id, thread_id))
        if resume:
            span.set_resume(len(resume))
        text = "" if resume else content() if callable(content) else content
        turn = await open_turn(  # starts the pump task under the span
            state,
            org_id=org_id,
            thread_id=thread_id,
            role=role,
            content=text,
            agui=agui,
            run_id=run_id,
            span=span,
            resume=resume,
        )
    except BaseException as exc:
        fail_span(span, exc)
        raise
    try:
        # Peek the first item: a budget refusal before the first token is an
        # HTTP 402, not a stream (SPEC-RH2 §11). Bounded by the heartbeat so a
        # slow first token never delays the response headers indefinitely.
        first = await turn.pump.prime(wait_s=settings.sse_heartbeat_s)
        if first is not None and first[0] == "error" and is_budget_exceeded(first[1]):
            raise first[1]
    except BaseException as exc:
        fail_span(span, exc)
        await turn.aclose()
        raise

    headers = {STREAM_PROTOCOL_HEADER: "ag-ui"} if agui else {}
    return StreamingResponse(
        release_after(turn.lease, turn.sse_body()),
        media_type="text/event-stream",
        headers=headers,
        # Idempotent backstop if the body is never iterated.
        background=BackgroundTask(turn.release),
    )


@router.post("/threads/{thread_id}/messages", responses=ex.MESSAGES)
async def post_message(
    thread_id: str,
    body: MessageIn,
    request: Request,
    protocol: Protocol | None = None,
) -> StreamingResponse:
    """Stream a chat turn as native SSE (default) or AG-UI (SPEC-RH2-AGU-001)."""
    return await run_turn(
        request,
        thread_id=thread_id,
        role=body.role,
        content=body.content,
        agui=wants_agui(request, protocol),
    )
