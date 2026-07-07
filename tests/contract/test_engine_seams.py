"""Contract smoke tests against the released engine (opt-in).

Marked ``contract`` and deselected from the default unit run. This Phase-0 stub
only asserts the four host-facing engine seams import from the released
``prismal`` package; the full contract suite (a real ``build_runtime()``, a chat
turn, an Agent Card fetch) is built in RHB-05-02.

Run with: ``uv run pytest -m contract``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract


def test_engine_seams_importable() -> None:
    from prismal.a2a.card import build_agent_card
    from prismal.a2a.server import A2AServerHandler
    from prismal.agents.graph import get_async_compiled_graph
    from prismal.composition.runtime import build_runtime

    assert callable(build_runtime)
    assert callable(get_async_compiled_graph)
    assert callable(build_agent_card)
    assert A2AServerHandler is not None
