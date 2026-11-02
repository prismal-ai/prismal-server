"""The lifespan closes the engine's cached graph (H-320, prismal-ai 3.20)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

import prismal_server.app as app_module
from prismal_server.app import create_app
from prismal_server.config import HostSettings


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


class StubRegistry:
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec

    async def aclose_all(self) -> None:
        self.rec.calls.append("registry")


class StubStore:
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec

    async def aclose(self) -> None:
        self.rec.calls.append("usage_store")


async def _run_lifespan(app: Any) -> None:
    async with app.router.lifespan_context(app):
        pass


def _app(rec: Recorder, **kw: Any) -> Any:
    return create_app(
        settings=kw.pop("settings", HostSettings()),
        registry=StubRegistry(rec),  # type: ignore[arg-type]
        usage_store=StubStore(rec),  # type: ignore[arg-type]
        **kw,
    )


async def test_closer_runs_once_after_registry() -> None:
    rec = Recorder()

    async def closer() -> None:
        rec.calls.append("graph")

    await _run_lifespan(_app(rec, graph_closer=closer))
    assert rec.calls == ["registry", "graph", "usage_store"]


async def test_failing_closer_does_not_break_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rec = Recorder()

    async def closer() -> None:
        raise RuntimeError("sqlite://u:CANARY@x")

    caplog.set_level(logging.WARNING)
    await _run_lifespan(_app(rec, graph_closer=closer))
    assert rec.calls == ["registry", "usage_store"]
    assert "engine graph close failed" in caplog.text
    assert "CANARY" not in caplog.text


async def test_default_closer_is_the_engine_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder()

    async def engine_close() -> None:
        rec.calls.append("engine")

    monkeypatch.setattr(app_module, "aclose_async_compiled_graph", engine_close)
    await _run_lifespan(_app(rec))
    assert rec.calls == ["registry", "engine", "usage_store"]


async def test_fake_mode_skips_engine_close(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()

    async def engine_close() -> None:
        rec.calls.append("engine")

    monkeypatch.setattr(app_module, "aclose_async_compiled_graph", engine_close)
    await _run_lifespan(_app(rec, settings=HostSettings(host_fake_llm_mode=True)))
    assert "engine" not in rec.calls


async def test_engine_graph_held_alive_during_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3.20.0 drops its cached graph before closing; the host must hold it."""
    import weakref

    from prismal_server import deps

    class Graph:
        pass

    graph = Graph()
    ref = weakref.ref(graph)
    deps.hold_engine_graph(graph)
    del graph
    alive: list[bool] = []

    async def engine_close() -> None:
        alive.append(ref() is not None)

    monkeypatch.setattr(app_module, "aclose_async_compiled_graph", engine_close)
    rec = Recorder()
    await _run_lifespan(_app(rec))
    assert alive == [True]
    assert deps.ENGINE_GRAPHS == []
    assert ref() is None  # released after the close
