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

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from prismal.events.agui import AgUiContext
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from prismal_server.auth import authenticate, resolve_org_id
from prismal_server.deps import release_after
from prismal_server.errors import is_budget_exceeded
from prismal_server.streaming import (
    STREAM_PROTOCOL_HEADER,
    ChunkPump,
    agui_stream,
    sse_stream,
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


async def run_turn(
    request: Request,
    *,
    thread_id: str,
    role: str,
    content: str,
    agui: bool,
    run_id: str | None = None,
) -> StreamingResponse:
    """Run one chat turn and stream it natively or as AG-UI.

    Shared by ``POST /threads/{id}/messages`` and ``POST /v1/agui``: tenant
    resolution, org-scoped engine thread key, runtime lease, AG-UI gate (406
    before any graph work), 402 on a pre-token budget refusal, usage record.
    User content is passed **only** as graph input (CHT-005).
    """
    # Resolve identity (401 in strict mode) then the tenant (TEN-001, AUT-005).
    identity = await authenticate(request)
    org_id = resolve_org_id(request, identity)

    settings = request.app.state.settings
    store = request.app.state.usage_store
    engine_thread = engine_thread_id(org_id, thread_id)
    probe = TurnUsageProbe(engine_thread, request.app.state.run_summary)

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
    lease = await request.app.state.registry.acquire(org_id)
    pump: ChunkPump | None = None
    try:
        engine_settings = _engine_settings(lease.runtime)
        if agui and not (
            settings.host_agui_enabled
            and getattr(engine_settings, "agui_events_enabled", False)
        ):
            raise ProtocolNotEnabled()  # SPEC-RH2-AGU-003: before streaming
        graph_factory = request.app.state.graph_factory
        graph = await graph_factory(
            tool_provider=getattr(lease.runtime, "tool_provider", None)
        )
        config = {"configurable": {"thread_id": engine_thread, "org_id": org_id}}
        graph_input = {"messages": [{"role": role, "content": content}]}
        stream_mode: Any = _AGUI_STREAM_MODES if agui else "messages"
        pump = ChunkPump(graph.astream(graph_input, config, stream_mode=stream_mode))
        # Peek the first item: a budget refusal before the first token is an
        # HTTP 402, not a stream (SPEC-RH2 §11). Bounded by the heartbeat so a
        # slow first token never delays the response headers indefinitely.
        first = await pump.prime(wait_s=settings.sse_heartbeat_s)
        if first is not None and first[0] == "error" and is_budget_exceeded(first[1]):
            raise first[1]
    except BaseException:
        if pump is not None:
            await pump.aclose()
        await lease.release()
        raise

    body: AsyncIterator[str]
    headers: dict[str, str] = {}
    if agui:
        ctx = AgUiContext(
            run_id=run_id or uuid4().hex,
            thread_id=thread_id,  # client-facing id, never the engine key
            include_state_deltas=bool(
                getattr(engine_settings, "agui_include_state_deltas", True)
            ),
            include_usage=bool(getattr(engine_settings, "agui_include_usage", True)),
            include_metadata=bool(
                getattr(engine_settings, "agui_include_metadata", True)
            ),
        )
        body = agui_stream(
            pump,
            ctx=ctx,
            heartbeat_s=settings.sse_heartbeat_s,
            summary_for=probe.summary,
            on_done=record_usage,
        )
        headers[STREAM_PROTOCOL_HEADER] = "ag-ui"
    else:
        body = sse_stream(
            pump,
            thread_id=thread_id,
            heartbeat_s=settings.sse_heartbeat_s,
            usage_for=probe.usage,
            on_done=record_usage,
        )

    return StreamingResponse(
        release_after(lease, body),
        media_type="text/event-stream",
        headers=headers,
        # Idempotent backstop if the body is never iterated.
        background=BackgroundTask(lease.release),
    )


@router.post("/threads/{thread_id}/messages")
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
