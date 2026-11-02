"""Fake-mode interrupts for AG-UI resume (RH3-02-01)."""

from __future__ import annotations

from typing import Any

from langgraph.types import Command

from prismal_server.fake.graph import FakeGraph

MODES = ["messages", "updates", "custom"]
CONFIG = {"configurable": {"thread_id": "t"}}


async def _collect(graph: FakeGraph, graph_input: Any) -> list[Any]:
    return [c async for c in graph.astream(graph_input, CONFIG, stream_mode=MODES)]


def _text(chunks: list[Any]) -> str:
    return "".join(
        c[1][0].content for c in chunks if c[0] == "messages" and c[1][0].content
    )


async def test_interrupt_then_resume() -> None:
    graph = FakeGraph()
    chunks = await _collect(
        graph, {"messages": [{"role": "user", "content": "/interrupt ok?"}]}
    )
    (update,) = [c for c in chunks if c[0] == "updates"]
    (intr,) = update[1]["__interrupt__"]
    assert intr.id == "fake-int-1"
    assert intr.value == {"reason": "input_required", "message": "ok?"}
    state = await graph.aget_state(CONFIG)
    assert [i.id for i in state.interrupts] == ["fake-int-1"]

    resumed = await _collect(graph, Command(resume={"fake-int-1": {"ok": True}}))
    assert _text(resumed) == 'resumed: {"fake-int-1": {"ok": true}}'
    assert (await graph.aget_state(CONFIG)).interrupts == ()


async def test_no_interrupt_state_is_empty() -> None:
    assert (await FakeGraph().aget_state(CONFIG)).interrupts == ()
