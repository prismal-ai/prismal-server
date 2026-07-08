"""Tests for the SSE bridge (RHB-02-01/04/05, SPEC-RHB-CHT-002/003/004/006)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from prismal_server.streaming import (
    default_translate,
    sse_comment,
    sse_event,
    sse_stream,
)


class FakeMsg:
    """Duck-typed LangChain message chunk."""

    def __init__(
        self, content: str = "", tool_call_chunks: list[dict[str, Any]] | None = None
    ) -> None:
        self.content = content
        self.tool_call_chunks = tool_call_chunks or []


# --- framing -----------------------------------------------------------------
def test_sse_event_frames_named_json() -> None:
    frame = sse_event("token", {"delta": "Hola"})
    assert frame == 'event: token\ndata: {"delta": "Hola"}\n\n'


def test_sse_comment_is_heartbeat() -> None:
    assert sse_comment("heartbeat") == ": heartbeat\n\n"


# --- translation (duck-typed, no langchain import) ---------------------------
def test_translate_message_tuple_to_token() -> None:
    events = default_translate((FakeMsg(content="Hi"), {"langgraph_node": "agent"}))
    assert events == [("token", {"delta": "Hi"})]


def test_translate_empty_content_yields_nothing() -> None:
    assert default_translate((FakeMsg(content=""), {})) == []


def test_translate_tool_call_chunk() -> None:
    msg = FakeMsg(tool_call_chunks=[{"name": "web_search", "args": '{"q":"x"}'}])
    events = default_translate((msg, {}))
    assert ("tool_call", {"name": "web_search", "args": '{"q":"x"}'}) in events


# --- the bridge --------------------------------------------------------------
async def _collect(agen: AsyncIterator[str]) -> list[str]:
    return [frame async for frame in agen]


async def _gen(*msgs: FakeMsg) -> AsyncIterator[Any]:
    for m in msgs:
        yield (m, {})


async def test_stream_emits_tokens_then_done() -> None:
    frames = await _collect(
        sse_stream(
            _gen(FakeMsg("Ho"), FakeMsg("la")),
            thread_id="t-1",
            heartbeat_s=100,
        )
    )
    joined = "".join(frames)
    assert 'event: token\ndata: {"delta": "Ho"}\n\n' in joined
    assert 'event: token\ndata: {"delta": "la"}\n\n' in joined
    # terminal done carries the thread id
    assert frames[-1].startswith("event: done\n")
    done_payload = json.loads(frames[-1].split("data: ", 1)[1])
    assert done_payload["thread_id"] == "t-1"
    assert done_payload["finish_reason"] == "stop"


async def test_heartbeat_emitted_while_idle() -> None:
    async def slow() -> AsyncIterator[Any]:
        await asyncio.sleep(0.05)
        yield (FakeMsg("done"), {})

    frames = await _collect(sse_stream(slow(), thread_id="t", heartbeat_s=0.01))
    assert any(f.startswith(": heartbeat") for f in frames)


async def test_midstream_error_becomes_terminal_error_event() -> None:
    async def boom() -> AsyncIterator[Any]:
        yield (FakeMsg("partial"), {})
        raise RuntimeError("model exploded at /secret")

    frames = await _collect(sse_stream(boom(), thread_id="t", heartbeat_s=100))
    # token first, then a terminal error, and NO done afterwards
    assert frames[-1].startswith("event: error\n")
    assert not any(f.startswith("event: done") for f in frames)
    payload = json.loads(frames[-1].split("data: ", 1)[1])
    assert payload["code"] == "internal_error"
    assert "secret" not in payload["message"]  # opaque 500


async def test_client_disconnect_cancels_underlying_stream() -> None:
    """Closing the SSE generator must cancel the graph astream (SPEC-RHB-CHT-004)."""
    state = {"closed": False}

    async def endless() -> AsyncIterator[Any]:
        try:
            while True:
                yield (FakeMsg("tok"), {})
                await asyncio.sleep(0.01)
        finally:
            state["closed"] = True

    agen = sse_stream(endless(), thread_id="t", heartbeat_s=100)
    first = await agen.__anext__()
    assert first.startswith("event: token")
    await agen.aclose()  # simulate client disconnect
    # give the cancelled producer a tick to run its finally
    await asyncio.sleep(0.02)
    assert state["closed"] is True
