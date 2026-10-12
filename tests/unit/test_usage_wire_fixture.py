"""Shared usage-wire contract fixture (RH2-00-05, SPEC-RH2-USG-002).

``tests/fixtures/protocols/usage_wire.json`` is vendored verbatim from the
engine (``prismal/tests/fixtures/protocols/usage_wire.json``) and is the same
file the SDK vendors (``SPEC-CS2-USG-001``). It pins the 12 keys of the
``done.usage`` object, in order. These tests keep the fixture, the SPEC table
and the installed engine's ``RunSummary.to_usage_wire()`` in agreement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = _FIXTURES / "protocols" / "usage_wire.json"

# SPEC-RH2-USG-002 table, in normative order.
SPEC_KEYS: tuple[str, ...] = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "calls",
    "cost_usd",
    "cost_source",
    "estimated",
    "served_model",
    "gateway",
)


@pytest.fixture(scope="module")
def wire() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def test_fixture_keys_match_spec_order(wire: dict[str, Any]) -> None:
    assert tuple(k["name"] for k in wire["keys"]) == SPEC_KEYS


def test_fixture_example_matches_key_order(wire: dict[str, Any]) -> None:
    assert tuple(wire["example"]) == SPEC_KEYS


def test_fixture_cost_source_enum(wire: dict[str, Any]) -> None:
    (cost_source,) = (k for k in wire["keys"] if k["name"] == "cost_source")
    assert set(cost_source["enum"]) == {
        "gateway",
        "litellm",
        "table",
        "subscription",
        "none",
        "mixed",
    }


def test_engine_to_usage_wire_matches_fixture(wire: dict[str, Any]) -> None:
    """The installed engine emits exactly the fixture's keys, in order."""
    from prismal.budget.types import Usage
    from prismal.monitoring.observability_types import RunSummary

    summary = RunSummary(
        run_id="r-1",
        session_id="t-42",
        agent_name="supervisor",
        visited_nodes=[],
        spans=[],
        tool_calls=[],
        usage=Usage(),
        latency_ms=0.0,
    )
    out = summary.to_usage_wire()
    assert tuple(out) == SPEC_KEYS
    # Empty run: nullable keys are null, not "".
    assert out["served_model"] is None and out["gateway"] is None
