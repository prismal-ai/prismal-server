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

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from prismal_server.streaming import sse_stream

router = APIRouter(tags=["threads"])


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
    # Tenant resolution: Phase 4 adds identity; for now honour X-Org-Id (TEN-001).
    org_id = request.headers.get("X-Org-Id")

    registry = request.app.state.registry
    runtime = await registry.get(org_id)
    graph_factory = request.app.state.graph_factory
    graph = await graph_factory(tool_provider=getattr(runtime, "tool_provider", None))

    config = {"configurable": {"thread_id": thread_id, "org_id": org_id}}
    graph_input = {"messages": [{"role": body.role, "content": body.content}]}
    chunks = graph.astream(graph_input, config, stream_mode="messages")

    heartbeat_s = request.app.state.settings.sse_heartbeat_s
    return StreamingResponse(
        sse_stream(chunks, thread_id=thread_id, heartbeat_s=heartbeat_s),
        media_type="text/event-stream",
    )
