"""Contract-suite teardown.

The engine's ``get_async_compiled_graph`` caches a module-level graph whose
SQLite checkpointer runs on a **non-daemon** ``aiosqlite`` worker thread, and
the engine exposes no way to close it. Without this, the interpreter waits on
that thread forever after the last test (the contract job never ends). The
engine follow-up is tracked in specs/host-v0.3-agui-resume/ENGINE-PROMPT.md.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def _stop_engine_graph_checkpointer() -> Iterator[None]:
    yield
    from prismal.agents import graph as engine_graph

    compiled = getattr(engine_graph, "_async_graph", None)
    conn = getattr(getattr(compiled, "checkpointer", None), "conn", None)
    stop = getattr(conn, "stop", None)
    if callable(stop):
        stop()  # sync: closes the sqlite connection and ends the worker thread
