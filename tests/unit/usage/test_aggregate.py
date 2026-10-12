"""Whole-window usage aggregation (RH2-02-04, SPEC-RH2-USE-003)."""

from __future__ import annotations

from typing import Any

from prismal_server.usage.aggregate import by_model, cache_hit_ratio, total
from prismal_server.usage.models import UsageWire


def _u(**over: Any) -> UsageWire:
    base: dict[str, Any] = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cache_read_tokens": 50,
        "cache_write_tokens": 5,
        "reasoning_tokens": 2,
        "total_tokens": 110,
        "calls": 1,
        "cost_usd": 0.001,
        "cost_source": "gateway",
        "estimated": False,
        "served_model": "m/a",
        "gateway": "openrouter",
    }
    return UsageWire.model_validate({**base, **over})


def test_total_is_fieldwise_sum() -> None:
    """EARS USE-003: 3 records sum to known values."""
    t = total(
        [
            _u(),
            _u(prompt_tokens=200, total_tokens=210, cost_usd=0.0021),
            _u(calls=3, cost_usd=0.0000004),
        ]
    )
    assert t.prompt_tokens == 400
    assert t.completion_tokens == 30
    assert t.cache_read_tokens == 150
    assert t.cache_write_tokens == 15
    assert t.reasoning_tokens == 6
    assert t.total_tokens == 430
    assert t.calls == 5
    assert t.cost_usd == 0.0031  # rounded to 6 decimals
    assert t.cost_source == "gateway"
    assert t.estimated is False
    assert t.served_model is None and t.gateway is None


def test_total_mixed_source_and_estimated_or() -> None:
    t = total([_u(), _u(cost_source="table", estimated=True)])
    assert t.cost_source == "mixed"
    assert t.estimated is True


def test_total_of_nothing() -> None:
    t = total([])
    assert t.calls == 0 and t.cost_source == "none" and t.estimated is False


def test_cache_hit_ratio_rounds_to_4_decimals() -> None:
    assert cache_hit_ratio(_u(prompt_tokens=3, cache_read_tokens=2)) == 0.6667
    assert cache_hit_ratio(_u(prompt_tokens=0, cache_read_tokens=0)) == 0.0


def test_by_model_groups_by_model_and_gateway() -> None:
    rows = by_model(
        [
            _u(),
            _u(cost_usd=0.002),
            _u(served_model="m/b", gateway=None, cost_source="litellm", cost_usd=0.01),
        ]
    )
    assert [(r.served_model, r.gateway) for r in rows] == [
        ("m/b", None),  # most expensive first
        ("m/a", "openrouter"),
    ]
    assert rows[1].calls == 2 and rows[1].cost_usd == 0.003
    assert rows[1].cost_source == "gateway"
