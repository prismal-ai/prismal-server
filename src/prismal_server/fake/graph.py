"""The deterministic fake graph and its run summaries (SPEC-RH2-FAK-002)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from prismal.composition.runtime import build_test_runtime

ECHO_PREFIX = "echo: "
ECHO_MAX_CHARS = 200
TOKEN_CHARS = 16

FAKE_USAGE: dict[str, Any] = {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
    "total_tokens": 15,
    "calls": 1,
    "cost_usd": 0.0,
    "cost_source": "none",
    "estimated": True,
    "served_model": "fake/echo",
    "gateway": None,
}


class FakeScriptError(Exception):
    """The scripted ``/error`` failure (maps to ``internal_error``)."""

    def __init__(self) -> None:
        super().__init__("fake error")


async def fake_builder(*, org_id: str | None, config_source: Any = None) -> Any:
    """Runtime builder for fake mode: the engine's I/O-free test runtime."""
    return build_test_runtime(org_id=org_id)


@dataclass(frozen=True)
class FakeSummary:
    """Duck-types the engine ``RunSummary`` the usage probe and AG-UI read."""

    run_id: str

    def to_usage_wire(self) -> dict[str, Any]:
        return dict(FAKE_USAGE)


@dataclass
class FakeSummaries:
    """Latest run summary per engine thread key (``run_summary`` lookup)."""

    _latest: dict[str, FakeSummary] = field(default_factory=dict)
    _runs: int = 0

    def record(self, thread_id: str) -> None:
        self._runs += 1
        self._latest[thread_id] = FakeSummary(run_id=f"fake-{self._runs}")

    def __call__(self, thread_id: str) -> FakeSummary | None:
        return self._latest.get(thread_id)


@dataclass
class _NativeMessage:
    """A native-mode chunk: ``tool_call`` args stay an object (FAK-002)."""

    content: str = ""
    tool_call_chunks: list[dict[str, Any]] = field(default_factory=list)


def _last_user_text(graph_input: dict[str, Any]) -> str:
    messages = graph_input.get("messages") or []
    content = messages[-1].get("content", "") if messages else ""
    return content if isinstance(content, str) else ""


class FakeGraph:
    """Scripted stand-in for the compiled graph.

    ``/tool <q>`` ⇒ one ``fake_tool`` call; ``/error`` ⇒ ``FakeScriptError``;
    anything else ⇒ ``"echo: " + content[:200]`` in ≤ 16-char chunks. A list
    ``stream_mode`` (AG-UI) yields ``("messages", (chunk, meta))`` with real
    message chunks, which the engine's AG-UI translator requires.
    """

    def __init__(self, summaries: FakeSummaries | None = None) -> None:
        self.summaries = summaries or FakeSummaries()

    def astream(
        self, graph_input: Any, config: Any, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        return self._run(graph_input, config, tagged=isinstance(stream_mode, list))

    async def _run(
        self, graph_input: Any, config: Any, *, tagged: bool
    ) -> AsyncIterator[Any]:
        thread_id = config["configurable"]["thread_id"]
        text = _last_user_text(graph_input)
        meta = {"langgraph_node": "agent"}
        if text.strip() == "/error":
            raise FakeScriptError()
        if text.startswith("/tool "):
            query = text.removeprefix("/tool ")
            yield self._chunk(tool_query=query, tagged=tagged, meta=meta)
        else:
            reply = ECHO_PREFIX + text[:ECHO_MAX_CHARS]
            for i in range(0, len(reply), TOKEN_CHARS):
                yield self._chunk(
                    content=reply[i : i + TOKEN_CHARS], tagged=tagged, meta=meta
                )
        self.summaries.record(thread_id)

    @staticmethod
    def _chunk(
        *,
        content: str = "",
        tool_query: str | None = None,
        tagged: bool,
        meta: dict[str, Any],
    ) -> Any:
        if not tagged:
            calls = (
                [{"name": "fake_tool", "args": {"q": tool_query}}]
                if tool_query is not None
                else []
            )
            return (_NativeMessage(content=content, tool_call_chunks=calls), meta)
        from langchain_core.messages import AIMessageChunk  # noqa: PLC0415

        if tool_query is None:
            message = AIMessageChunk(content=content, id="fake-msg")
        else:
            message = AIMessageChunk(
                content="",
                id="fake-msg",
                tool_call_chunks=[
                    {
                        "name": "fake_tool",
                        "args": json.dumps({"q": tool_query}),
                        "id": "fake-call",
                        "index": 0,
                    }
                ],
            )
        return ("messages", (message, meta))
