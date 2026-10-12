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

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from prismal_server.auth import authenticate, resolve_org_id
from prismal_server.deps import release_after
from prismal_server.errors import is_budget_exceeded
from prismal_server.streaming import ChunkPump, sse_stream
from prismal_server.usage.models import UsageRecord, UsageWire
from prismal_server.usage.store import new_turn_id
from prismal_server.usage.summary import TurnUsageProbe

router = APIRouter(tags=["threads"])


def engine_thread_id(org_id: str | None, thread_id: str) -> str:
    """The engine-side thread key: the client ``thread_id`` scoped to its org.

    The engine keys conversation state and run summaries by thread id alone,
    process-wide, and clients choose their own ids — so two tenants using the
    same id would share history and usage. The length prefix makes the mapping
    injective (``("a:b", "c")`` ≠ ``("a", "b:c")``). Single-tenant (``None``)
    keeps the raw id. Clients only ever see their own ``thread_id``.
    """
    return thread_id if org_id is None else f"{len(org_id)}:{org_id}:{thread_id}"


class MessageIn(BaseModel):
    """A chat turn's request body (SPEC.md §3.2)."""

    content: str = Field(min_length=1)
    role: str = "user"
    metadata: dict[str, Any] = {}


@router.post("/threads")
async def create_thread() -> dict[str, str]:
    """Mint a new opaque thread id (clients may also choose their own)."""
    return {"thread_id": uuid4().hex}


@router.post("/threads/{thread_id}/messages")
async def post_message(
    thread_id: str, body: MessageIn, request: Request
) -> StreamingResponse:
    """Stream a chat turn for ``thread_id`` as Server-Sent Events."""
    # Resolve identity (401 in strict mode) then the tenant (TEN-001, AUT-005).
    identity = await authenticate(request)
    org_id = resolve_org_id(request, identity)

    settings = request.app.state.settings
    store = request.app.state.usage_store
    engine_thread = engine_thread_id(org_id, thread_id)
    probe = TurnUsageProbe(engine_thread, request.app.state.run_summary)

    async def record_usage(finish_reason: str, usage: UsageWire | None) -> None:
        # SPEC-RH2-USE-001: one record per `done` with non-null usage.
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
        graph_factory = request.app.state.graph_factory
        graph = await graph_factory(
            tool_provider=getattr(lease.runtime, "tool_provider", None)
        )
        config = {"configurable": {"thread_id": engine_thread, "org_id": org_id}}
        graph_input = {"messages": [{"role": body.role, "content": body.content}]}
        pump = ChunkPump(graph.astream(graph_input, config, stream_mode="messages"))
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

    return StreamingResponse(
        release_after(
            lease,
            sse_stream(
                pump,
                thread_id=thread_id,
                heartbeat_s=settings.sse_heartbeat_s,
                usage_for=probe.usage,
                on_done=record_usage,
            ),
        ),
        media_type="text/event-stream",
        # Idempotent backstop if the body is never iterated.
        background=BackgroundTask(lease.release),
    )
