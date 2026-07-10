"""Contract smoke tests against the released engine (RHB-05-02, opt-in).

Marked ``contract`` and deselected from the default unit run (which stays free of
live provider I/O, SPEC-RHB-NFR-003). This suite exercises the host against a
**real** ``prismal`` composition to catch host/engine drift before deploy
(ADR-005):

1. A real ``build_runtime()`` composes every port and ``aclose()``s cleanly.
2. The Agent Card endpoint serves a real ``build_agent_card(...)`` through the app.
3. A full chat turn round-trips ``get_async_compiled_graph().astream()`` over SSE.

The chat turn needs a live LLM, so it is **skipped** unless a provider credential
is present in the environment — CI runs the rest with no secrets, and a developer
with a key gets the full end-to-end assertion.

Run with: ``uv run pytest -m contract``.
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.contract

# Provider credentials that let the default engine graph run a real turn. If none
# is set, the chat-turn contract test skips rather than failing on a missing key.
_LLM_KEY_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "PRISMAL_ANTHROPIC_API_KEY",
    "PRISMAL_OPENAI_API_KEY",
)


def _has_llm_credential() -> bool:
    return any(os.environ.get(name) for name in _LLM_KEY_ENV)


def test_engine_seams_importable() -> None:
    """The four host-facing seams import from the released ``prismal`` package."""
    from prismal.a2a.card import build_agent_card
    from prismal.a2a.server import A2AServerHandler
    from prismal.agents.graph import get_async_compiled_graph
    from prismal.composition.runtime import build_runtime

    assert callable(build_runtime)
    assert callable(get_async_compiled_graph)
    assert callable(build_agent_card)
    assert A2AServerHandler is not None


async def test_real_runtime_composes_and_closes() -> None:
    """``build_runtime()`` composes a real ``RuntimeContext`` and closes cleanly."""
    from prismal.composition.runtime import build_runtime

    runtime = await build_runtime(org_id=None)
    try:
        # The composed runtime carries the ports the host relies on.
        assert hasattr(runtime, "aclose")
        assert hasattr(runtime, "a2a_handler")  # None unless engine A2A is enabled
    finally:
        await runtime.aclose()


async def test_agent_card_served_against_real_engine() -> None:
    """``GET /.well-known/agent-card.json`` serves a real, valid A2A card."""
    from prismal_server.app import create_app

    app = create_app()  # real card builder + registry (no fakes injected)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    card = resp.json()
    # A2A v0.3.x wire form (camelCase): a name + protocol version at minimum.
    assert card.get("name")
    assert card.get("protocolVersion")


async def test_openapi_served_against_real_engine() -> None:
    """The real app still exposes a valid OpenAPI document (SDK-004)."""
    from prismal_server.app import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/openapi.json")

    assert resp.status_code == 200
    doc = resp.json()
    assert "/threads/{thread_id}/messages" in doc["paths"]


@pytest.mark.skipif(
    not _has_llm_credential(),
    reason="no LLM provider credential in env; chat-turn contract needs a live model",
)
async def test_real_chat_turn_streams_over_sse() -> None:
    """A full chat turn round-trips the real graph's ``astream`` over SSE.

    Boots the app with **no** injected fakes so the request drives the real
    composition + ``get_async_compiled_graph`` end to end, then asserts the SSE
    stream terminates with a ``done`` event (CHT-001/002).
    """
    from prismal_server.app import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", timeout=60.0
    ) as c:
        created = await c.post("/threads")
        thread_id = created.json()["thread_id"]
        resp = await c.post(
            f"/threads/{thread_id}/messages",
            json={"content": "Say hello in one short word."},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    # Terminal event present and no mid-stream engine failure surfaced.
    assert "event: done" in body
    assert "event: error" not in body
