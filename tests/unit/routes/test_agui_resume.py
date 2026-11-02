"""Resume interrupted AG-UI runs (RH3-02-02, specs/host-v0.3-agui-resume)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.tracing import Tracing


@pytest.fixture(autouse=True)
def agui_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    from prismal.a2a import get_settings

    monkeypatch.setenv("PRISMAL_AGUI_EVENTS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class World:
    def __init__(self) -> None:
        settings = HostSettings(host_fake_llm_mode=True)
        self.mem = InMemorySpanExporter()
        self.app = create_app(
            settings=settings, tracing=Tracing(settings, exporter=self.mem)
        )
        self.leases: list[Any] = []
        registry = self.app.state.registry
        real_acquire = registry.acquire

        async def acquire(org_id: Any = None) -> Any:
            lease = await real_acquire(org_id)
            self.leases.append(lease)
            return lease

        registry.acquire = acquire

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://t")


def _input(
    thread: str = "t-1",
    *,
    text: str | None = None,
    resume: list[dict[str, Any]] | None = None,
    run_id: str = "r-1",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "threadId": thread,
        "runId": run_id,
        "messages": (
            [{"id": "m", "role": "user", "content": text}] if text is not None else []
        ),
        "tools": [],
        "context": [],
        "state": {},
        "forwardedProps": {},
    }
    if resume is not None:
        body["resume"] = resume
    return body


def _events(resp: Any) -> list[dict[str, Any]]:
    return [
        json.loads(line[5:])
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]


def _text(events: list[dict[str, Any]]) -> str:
    return "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")


def _resolve(iid: str = "fake-int-1", payload: Any = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"interruptId": iid, "status": "resolved"}
    if payload is not None:
        entry["payload"] = payload
    return entry


async def _interrupt(c: AsyncClient, thread: str = "t-1", **kw: Any) -> str:
    resp = await c.post("/v1/agui", json=_input(thread, text="/interrupt ok?"), **kw)
    assert resp.status_code == 200
    (finished,) = [e for e in _events(resp) if e["type"] == "RUN_FINISHED"]
    assert finished["outcome"]["type"] == "interrupt"
    (intr,) = finished["outcome"]["interrupts"]
    return str(intr["id"])


async def test_interrupt_then_resume() -> None:
    w = World()
    async with w.client() as c:
        iid = await _interrupt(c)
        assert iid == "fake-int-1"
        resp = await c.post(
            "/v1/agui",
            json=_input(resume=[_resolve(iid, {"action": "approve"})], run_id="r-2"),
        )
    assert resp.status_code == 200
    events = _events(resp)
    assert _text(events) == 'resumed: {"fake-int-1": {"action": "approve"}}'
    (started,) = [e for e in events if e["type"] == "RUN_STARTED"]
    assert started["runId"] == "r-2"
    (finished,) = [e for e in events if e["type"] == "RUN_FINISHED"]
    assert finished["outcome"] == {"type": "success"}
    assert finished["usage"]


@pytest.mark.parametrize(
    ("setup", "resume", "status", "code"),
    [
        (
            True,
            [{"interruptId": "fake-int-1", "status": "cancelled"}],
            422,
            "unsupported_resume",
        ),
        (False, [_resolve()], 409, "no_pending_interrupt"),
        (True, [_resolve("nope")], 422, "invalid_resume"),
        (True, [_resolve(), _resolve()], 422, "invalid_resume"),
    ],
)
async def test_resume_refusals(
    setup: bool, resume: list[dict[str, Any]], status: int, code: str
) -> None:
    w = World()
    async with w.client() as c:
        if setup:
            await _interrupt(c)
        w.mem.clear()
        resp = await c.post("/v1/agui", json=_input(resume=resume, run_id="r-2"))
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code
    assert all(lease._entry is None for lease in w.leases)  # every lease released
    (s,) = w.mem.get_finished_spans()
    assert s.status.status_code == StatusCode.ERROR
    assert dict(s.attributes or {})["error.type"] == code


async def test_resume_retry_is_409() -> None:
    w = World()
    body = _input(resume=[_resolve()], run_id="r-2")
    async with w.client() as c:
        await _interrupt(c)
        first = await c.post("/v1/agui", json=body)
        retry = await c.post("/v1/agui", json=body)
    assert first.status_code == 200
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "no_pending_interrupt"


async def test_resume_is_tenant_scoped() -> None:
    w = World()
    async with w.client() as c:
        await _interrupt(c, headers={"X-Org-Id": "acme"})
        other = await c.post(
            "/v1/agui",
            json=_input(resume=[_resolve()], run_id="r-2"),
            headers={"X-Org-Id": "globex"},
        )
        own = await c.post(
            "/v1/agui",
            json=_input(resume=[_resolve()], run_id="r-3"),
            headers={"X-Org-Id": "acme"},
        )
    assert other.status_code == 409
    assert own.status_code == 200


async def test_resume_ignores_messages() -> None:
    w = World()
    async with w.client() as c:
        await _interrupt(c)
        body = _input(text="hello there", resume=[_resolve()], run_id="r-2")
        resp = await c.post("/v1/agui", json=body)
    assert resp.status_code == 200
    text = _text(_events(resp))
    assert text.startswith("resumed: ")
    assert "hello there" not in text


async def test_resume_span_attributes() -> None:
    w = World()
    async with w.client() as c:
        await _interrupt(c)
        w.mem.clear()
        await c.post("/v1/agui", json=_input(resume=[_resolve()], run_id="r-2"))
    (s,) = w.mem.get_finished_spans()
    a = dict(s.attributes or {})
    assert s.status.status_code == StatusCode.OK
    assert a["prismal.agui.resume"] is True
    assert a["prismal.agui.resume_count"] == 1


async def test_plain_run_has_no_resume_attributes() -> None:
    w = World()
    async with w.client() as c:
        resp = await c.post("/v1/agui", json=_input(text="hi"))
    assert _text(_events(resp)) == "echo: hi"
    (s,) = w.mem.get_finished_spans()
    assert "prismal.agui.resume" not in dict(s.attributes or {})


async def test_concurrent_resume_is_409_in_progress() -> None:
    import asyncio

    from langgraph.types import Command

    from prismal_server.fake import FakeCheckpoint, FakeGraph, FakeSummaries

    gate = asyncio.Event()
    summaries, checkpoint = FakeSummaries(), FakeCheckpoint()

    class SlowResume(FakeGraph):
        async def _run(self, graph_input: Any, config: Any, *, tagged: bool) -> Any:
            if isinstance(graph_input, Command):
                await gate.wait()
            async for chunk in super()._run(graph_input, config, tagged=tagged):
                yield chunk

    async def factory(*, tool_provider: Any = None) -> Any:
        return SlowResume(summaries, checkpoint)

    settings = HostSettings(host_fake_llm_mode=True)
    app = create_app(settings=settings, graph_factory=factory)
    body = _input(resume=[_resolve()], run_id="r-2")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _interrupt(c)
        first = asyncio.create_task(c.post("/v1/agui", json=body))
        for _ in range(100):
            if app.state.resume_claims:
                break
            await asyncio.sleep(0.01)
        second = await c.post("/v1/agui", json=body)
        gate.set()
        done = await first
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "resume_in_progress"
    assert done.status_code == 200
    assert app.state.resume_claims == set()  # claim freed with the lease


async def _open_resume_turn(app: Any) -> Any:
    from prismal_server.routes.threads import ResumeEntry, open_turn

    return await open_turn(
        app.state,
        org_id=None,
        thread_id="t-1",
        role="user",
        content="",
        agui=True,
        resume=[ResumeEntry(interruptId="fake-int-1", status="resolved")],
    )


async def test_backstop_cancels_graph_before_freeing_claim() -> None:
    w = World()
    async with w.client() as c:
        await _interrupt(c)
    turn = await _open_resume_turn(w.app)
    assert w.app.state.resume_claims  # held while the turn is open
    await turn.release()  # the response backstop (body never iterated)
    assert turn.pump._task.done()  # graph cancelled, not left running unclaimed
    assert w.app.state.resume_claims == set()


async def test_aget_state_failure_frees_claim() -> None:
    from prismal_server.fake import FakeGraph

    class NoCheckpointer(FakeGraph):
        async def aget_state(self, config: Any) -> Any:
            raise ValueError("No checkpointer set")

    async def factory(*, tool_provider: Any = None) -> Any:
        return NoCheckpointer()

    mem = InMemorySpanExporter()
    settings = HostSettings(host_fake_llm_mode=True)
    app = create_app(
        settings=settings,
        graph_factory=factory,
        tracing=Tracing(settings, exporter=mem),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/v1/agui", json=_input(resume=[_resolve()]))
    assert resp.status_code == 500
    assert app.state.resume_claims == set()
    (s,) = mem.get_finished_spans()
    assert dict(s.attributes or {})["error.type"] == "internal_error"
