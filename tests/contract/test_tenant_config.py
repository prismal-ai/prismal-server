"""Contract: per-tenant config reaches a real engine runtime (RH2-01, opt-in).

Exercises the released engine's ``build_runtime(org_id=…, config_source=…)``
(SPEC-HRS-CMP-001) with the host's ``TenantConfigSource``: the tenant's keys
and secrets land in that runtime's effective settings, and nothing leaks into
another tenant or ``os.environ``.

Run with: ``uv run pytest -m contract``.
"""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from prismal_server.tenancy.config_source import TenantConfigSource

pytestmark = pytest.mark.contract

CANARY = "sk-canary-7f3a9e"


async def test_build_runtime_applies_tenant_source() -> None:
    from prismal.composition import build_runtime

    environ_before = dict(os.environ)
    source = TenantConfigSource(
        "acme",
        {
            "budget_max_calls": "7",
            "openrouter_enabled": "true",
            "openrouter_api_key": SecretStr(CANARY),
        },
    )
    acme = await build_runtime(org_id="acme", config_source=source)
    beta = await build_runtime(org_id="beta")
    try:
        assert acme.settings is not None
        assert acme.settings.budget_max_calls == 7
        assert acme.settings.openrouter_enabled is True
        assert acme.settings.openrouter_api_key.get_secret_value() == CANARY
        # beta has no tenant source: process config, untouched by acme's keys.
        assert beta.settings is None or beta.settings.budget_max_calls != 7
        assert dict(os.environ) == environ_before  # never mirrored to env
    finally:
        await acme.aclose()
        await beta.aclose()
