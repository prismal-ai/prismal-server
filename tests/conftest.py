"""Shared test fixtures.

Two building blocks the rest of the suite composes on (RHB-00-03):

* ``fake_runtime`` — a deterministic, fully I/O-free ``RuntimeContext`` from the
  engine's own ``build_test_runtime()`` (fake tool provider, fake vector store,
  in-module embeddings/checkpointer/audit). This keeps the unit suite off any
  live LLM/provider I/O (SPEC-RHB-NFR-003).
* ``client`` — an ASGI ``httpx.AsyncClient`` bound to the app under test via
  ``ASGITransport`` (no sockets), so routes are exercised in-process.

The ``app`` fixture calls ``create_app()`` once it exists (Phase 1, RHB-01-03,
where the fake registry is injected per SPEC-RHB-LIF-003) and falls back to a
bare ``FastAPI`` app until then, so the scaffold's plumbing is testable now.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def fake_runtime():  # type: ignore[no-untyped-def]
    """A deterministic ``RuntimeContext`` backed entirely by engine fakes.

    ``build_test_runtime()`` does no I/O and its ``aclose()`` is a no-op, so
    tests may use it freely without a live engine composition.
    """
    from prismal.composition import build_test_runtime

    return build_test_runtime()


@pytest.fixture
def app() -> FastAPI:
    """The ASGI app under test.

    Prefers ``prismal_server.app.create_app()`` (Phase 1 onward); until that
    factory lands the module is a stub, so we fall back to a bare app to keep
    the ASGI/client plumbing exercisable during the scaffold phase.
    """
    try:
        from prismal_server.app import create_app  # type: ignore[attr-defined]
    except ImportError:
        create_app = None  # type: ignore[assignment]

    if callable(create_app):
        return create_app()
    return FastAPI(title="prismal-server (scaffold)")


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An in-process ASGI client for the app under test."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
