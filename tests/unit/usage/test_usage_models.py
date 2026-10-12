"""Usage wire + report models (RH2-02-01, SPEC-RH2-USG-002)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from prismal_server.usage.models import USAGE_KEYS, UsageRecord, UsageWire, rfc3339

FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "protocols" / "usage_wire.json"
)


@pytest.fixture(scope="module")
def example() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))["example"]
    return data


def test_model_keys_match_fixture_order() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert USAGE_KEYS == tuple(k["name"] for k in fixture["keys"])


def test_round_trip_preserves_values_and_order(example: dict[str, Any]) -> None:
    wire = UsageWire.model_validate(example)
    assert wire.model_dump() == example
    assert tuple(wire.model_dump()) == USAGE_KEYS


def test_engine_wire_validates() -> None:
    from prismal.budget.types import Usage
    from prismal.monitoring.observability_types import RunSummary

    summary = RunSummary(
        run_id="t.turn1",
        session_id="t",
        agent_name="a",
        visited_nodes=[],
        spans=[],
        tool_calls=[],
        usage=Usage(),
        latency_ms=0.0,
    )
    assert UsageWire.model_validate(summary.to_usage_wire()).calls == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"extra_key": 1},
        {"prompt_tokens": -1},
        {"cost_source": "free"},
        {"estimated": "maybe"},
    ],
)
def test_invalid_wire_is_rejected(
    example: dict[str, Any], patch: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        UsageWire.model_validate({**example, **patch})


def test_rfc3339_is_utc_z_seconds() -> None:
    dt = datetime(2026, 10, 4, 11, 59, 58, 123456, tzinfo=UTC)
    assert rfc3339(dt) == "2026-10-04T11:59:58Z"


def test_record_serializes_finished_at_as_rfc3339(example: dict[str, Any]) -> None:
    record = UsageRecord(
        org_id="acme",
        thread_id="t-42",
        turn_id="01J0000000000000000000000",
        finished_at=datetime(2026, 10, 4, 11, 59, 58, tzinfo=UTC),
        finish_reason="stop",
        usage=UsageWire.model_validate(example),
    )
    assert record.model_dump(mode="json")["finished_at"] == "2026-10-04T11:59:58Z"
