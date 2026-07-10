"""Server-Sent Events bridge (RHB-02-01).

Turns a graph ``astream(...)`` async iterator into an SSE byte stream: named
``token``/``tool_call``/``state`` events, a heartbeat comment while idle, a
terminal ``done`` (or ``error``) event, and prompt cancellation of the
underlying graph work when the client disconnects (SPEC-RHB-CHT-002/003/004/006).

Chunks are translated by duck typing — the host never imports langchain message
types; it reads ``.content`` and ``.tool_call_chunks`` off whatever the graph
yields.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

from prismal_server.errors import map_exception

# A translated SSE event: (event name, JSON-serializable payload).
SSEEvent = tuple[str, dict[str, Any]]


def sse_event(event: str, data: Any) -> str:
    """Frame a named SSE event with a JSON ``data:`` payload."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def sse_comment(text: str) -> str:
    """Frame an SSE comment line (used for heartbeats)."""
    return f": {text}\n\n"


def _message_of(chunk: Any) -> Any:
    """Unwrap ``(message, metadata)`` from ``stream_mode="messages"``."""
    if isinstance(chunk, tuple | list) and len(chunk) == 2:
        return chunk[0]
    return chunk


def default_translate(chunk: Any) -> list[SSEEvent]:
    """Translate one graph chunk into zero or more SSE events."""
    message = _message_of(chunk)
    events: list[SSEEvent] = []

    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        events.append(("token", {"delta": content}))

    for tc in getattr(message, "tool_call_chunks", None) or []:
        name = tc.get("name") if isinstance(tc, dict) else None
        if name:
            events.append(("tool_call", {"name": name, "args": tc.get("args", "")}))

    return events


async def sse_stream(
    chunks: AsyncIterator[Any],
    *,
    thread_id: str,
    heartbeat_s: float = 15.0,
    translate: Callable[[Any], Iterable[SSEEvent]] = default_translate,
) -> AsyncIterator[str]:
    """Bridge a graph ``astream`` iterator to SSE frames.

    A background producer drains ``chunks`` into a queue so the heartbeat timer
    never cancels an in-flight graph step. On client disconnect the outer
    generator is closed, and the ``finally`` cancels the producer — which closes
    the underlying graph stream (SPEC-RHB-CHT-004).
    """
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def producer() -> None:
        try:
            async for chunk in chunks:
                await queue.put(("chunk", chunk))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # mid-stream engine failure (SPEC-RHB-CHT-006)
            await queue.put(("error", exc))
        else:
            await queue.put(("end", None))

    task = asyncio.create_task(producer())
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
            except TimeoutError:
                yield sse_comment("heartbeat")
                continue

            if kind == "chunk":
                for event, payload in translate(value):
                    yield sse_event(event, payload)
            elif kind == "error":
                mapped = map_exception(value)
                yield sse_event(
                    "error", {"code": mapped.code, "message": mapped.message}
                )
                return
            else:  # "end"
                break

        yield sse_event("done", {"thread_id": thread_id, "finish_reason": "stop"})
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
