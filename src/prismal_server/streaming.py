"""Server-Sent Events bridge (RHB-02-01).

Turns a graph ``astream(...)`` async iterator into an SSE byte stream: named
``token``/``tool_call``/``state`` events, a heartbeat comment while idle, a
terminal ``done`` (or ``error``) event, and prompt cancellation of the
underlying graph work when the client disconnects (SPEC-RHB-CHT-002/003/004/006).

v0.2: ``done`` carries the engine's 12-key ``usage`` (or ``null``, fail-open —
SPEC-RH2-USG-001); a mid-stream ``BudgetExceeded`` becomes a terminal ``error``
with ``scope``/``limit``/``consumed`` and the partial ``usage`` (SPEC-RH2 §11).
:class:`ChunkPump` lets the route peek the first item, so a budget refusal
before the first token is answered as HTTP 402 instead of a stream.

Chunks are translated by duck typing — the host never imports langchain message
types; it reads ``.content`` and ``.tool_call_chunks`` off whatever the graph
yields.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

from prismal_server import metrics
from prismal_server.errors import map_exception
from prismal_server.usage.models import UsageWire

logger = logging.getLogger(__name__)

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


# Queue items: ("chunk", chunk) | ("error", exc) | ("end", None).
PumpItem = tuple[str, Any]
UsageProvider = Callable[[], UsageWire | None]
DoneHook = Callable[[str, UsageWire | None], Awaitable[None]]


class ChunkPump:
    """Drains a graph ``astream`` iterator into a queue on a background task.

    The heartbeat timer then never cancels an in-flight graph step, and
    :meth:`prime` can look at the first item before any response is sent.
    """

    def __init__(self, chunks: AsyncIterator[Any]) -> None:
        self._queue: asyncio.Queue[PumpItem] = asyncio.Queue()
        self._head: PumpItem | None = None
        self._task = asyncio.create_task(self._produce(chunks))

    async def _produce(self, chunks: AsyncIterator[Any]) -> None:
        try:
            async for chunk in chunks:
                await self._queue.put(("chunk", chunk))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # mid-stream engine failure (SPEC-RHB-CHT-006)
            await self._queue.put(("error", exc))
        else:
            await self._queue.put(("end", None))

    async def prime(self, wait_s: float) -> PumpItem | None:
        """Wait up to ``wait_s`` for the first item and keep it for :meth:`get`."""
        if self._head is None:
            try:
                self._head = await asyncio.wait_for(self._queue.get(), timeout=wait_s)
            except TimeoutError:
                return None
        return self._head

    async def get(self, wait_s: float) -> PumpItem:
        """Next item; raises ``TimeoutError`` after ``wait_s`` (heartbeat)."""
        if self._head is not None:
            item, self._head = self._head, None
            return item
        return await asyncio.wait_for(self._queue.get(), timeout=wait_s)

    async def aclose(self) -> None:
        """Cancel the producer, closing the underlying graph stream."""
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


def _safe_usage(usage_for: UsageProvider | None) -> UsageWire | None:
    if usage_for is None:
        return None
    try:
        return usage_for()
    except Exception:  # telemetry is fail-open (DD-RH2-002)
        logger.warning("usage lookup failed", exc_info=True)
        return None


async def sse_stream(
    chunks: AsyncIterator[Any] | ChunkPump,
    *,
    thread_id: str,
    heartbeat_s: float = 15.0,
    translate: Callable[[Any], Iterable[SSEEvent]] = default_translate,
    usage_for: UsageProvider | None = None,
    on_done: DoneHook | None = None,
) -> AsyncIterator[str]:
    """Bridge a graph ``astream`` iterator (or a primed pump) to SSE frames.

    On client disconnect the outer generator is closed, and the ``finally``
    cancels the producer — which closes the graph stream (SPEC-RHB-CHT-004).
    """
    pump = chunks if isinstance(chunks, ChunkPump) else ChunkPump(chunks)
    try:
        while True:
            try:
                kind, value = await pump.get(wait_s=heartbeat_s)
            except TimeoutError:
                yield sse_comment("heartbeat")
                continue

            if kind == "chunk":
                for event, payload in translate(value):
                    yield sse_event(event, payload)
            elif kind == "error":
                mapped = map_exception(value)
                error: dict[str, Any] = {
                    "code": mapped.code,
                    "message": mapped.message,
                    **mapped.extra,
                }
                if mapped.code == "budget_exceeded":
                    partial = _safe_usage(usage_for)
                    error["usage"] = partial.model_dump() if partial else None
                yield sse_event("error", error)
                return
            else:  # "end"
                break

        finish_reason = "stop"
        usage = _safe_usage(usage_for)
        if usage is None:
            metrics.increment("usage_missing_total")
        if on_done is not None:
            try:
                await on_done(finish_reason, usage)
            except Exception:  # persistence must not break the stream
                logger.warning(
                    "usage record failed for thread_id=%s", thread_id, exc_info=True
                )
        yield sse_event(
            "done",
            {
                "thread_id": thread_id,
                "finish_reason": finish_reason,
                "usage": usage.model_dump() if usage else None,
            },
        )
    finally:
        await pump.aclose()
