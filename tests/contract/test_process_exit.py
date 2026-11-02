"""Contract: the host process exits on its own after shutdown (H-320, opt-in).

The engine's cached graph keeps a SQLite checkpointer on a non-daemon thread.
The lifespan must close it (``aclose_async_compiled_graph``, prismal-ai >= 3.20),
or the interpreter waits forever after uvicorn's shutdown.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.contract

SCRIPT = textwrap.dedent(
    """
    import asyncio, sys

    from prismal_server.app import create_app
    from prismal_server.config import HostSettings

    async def main() -> None:
        kw = {}
        if "--no-close" in sys.argv:  # reproduce the pre-H-320 hang
            async def noop() -> None:
                return None
            kw["graph_closer"] = noop
        app = create_app(settings=HostSettings(), **kw)
        async with app.router.lifespan_context(app):
            await app.state.registry.get()  # builds runtime + cached engine graph

    asyncio.run(main())
    print("exited-main")
    """
)


def test_process_exits_after_lifespan_shutdown() -> None:
    proc = subprocess.run(  # noqa: S603 - fixed interpreter and inline script
        [sys.executable, "-c", SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "exited-main" in proc.stdout
