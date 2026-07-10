"""Scaffold smoke tests (Phase 0).

These assert the repository plumbing is sound — the package imports, the ASGI
test client works, and the engine's fake runtime composes with no I/O — without
depending on any route or app behaviour that later phases add.
"""

from __future__ import annotations

import prismal_server


def test_version_is_exposed() -> None:
    assert isinstance(prismal_server.__version__, str)
    assert prismal_server.__version__


async def test_asgi_client_roundtrips(client) -> None:  # type: ignore[no-untyped-def]
    """The in-process ASGI client reaches the app (unknown path → 404)."""
    resp = await client.get("/__scaffold_unknown__")
    assert resp.status_code == 404


async def test_fake_runtime_composes_without_io(fake_runtime) -> None:  # type: ignore[no-untyped-def]
    """The engine's fake runtime composes and closes cleanly (no live I/O)."""
    assert fake_runtime is not None
    # aclose() is a no-op for the fake runtime but must be awaitable.
    await fake_runtime.aclose()
