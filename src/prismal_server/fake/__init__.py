"""Fake-LLM mode (RH2-06-01/02, SPEC-RH2-FAK-001..003).

WHERE ``host_fake_llm_mode`` is on, runtimes come from the engine's
``build_test_runtime()`` (no I/O, no gateways) and chat is served by
:class:`FakeGraph`, a deterministic script, so SDKs and front-ends can be
tested end to end with no LLM credential. Refused in production
(``HostSettings`` validation); every response carries ``X-Prismal-Fake-LLM: 1``.

The engine ships no fake chat model, so the script lives here — it only stands
in for the graph and never touches engine logic. ``build_test_runtime`` takes no
``config_source``, so ``tenant.yaml`` is ignored in fake mode.

Seam: composition — ``build_test_runtime`` (``prismal.composition.runtime``).
"""

from __future__ import annotations

from prismal_server.fake.graph import (
    FAKE_USAGE,
    FakeGraph,
    FakeScriptError,
    FakeSummaries,
    fake_builder,
)
from prismal_server.fake.middleware import FAKE_HEADER, FakeHeaderMiddleware

__all__ = [
    "FAKE_HEADER",
    "FAKE_USAGE",
    "FakeGraph",
    "FakeHeaderMiddleware",
    "FakeScriptError",
    "FakeSummaries",
    "fake_builder",
]
