"""Tests for HostSettings (RHB-01-01, SPEC-RHB-CFG-001/002)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from prismal_server.config import HostSettings


def test_defaults_match_spec() -> None:
    s = HostSettings()
    assert s.host == "0.0.0.0"
    assert s.port == 8000
    assert s.cors_origins == []
    assert s.host_auth_backend == "none"
    assert s.host_auth_strict is False
    assert s.sse_heartbeat_s == 15
    assert s.host_max_tenants == 32
    assert s.dev_identity_did == "did:key:zLocalDev"


def test_env_prefix_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRISMAL_SERVER_PORT", "9001")
    monkeypatch.setenv("PRISMAL_SERVER_HOST_AUTH_STRICT", "true")
    monkeypatch.setenv("PRISMAL_SERVER_DEV_IDENTITY_DID", "did:key:zTest")
    s = HostSettings()
    assert s.port == 9001
    assert s.host_auth_strict is True
    assert s.dev_identity_did == "did:key:zTest"


def test_auth_backend_is_constrained() -> None:
    with pytest.raises(ValueError):
        HostSettings(host_auth_backend="banana")  # type: ignore[arg-type]


def test_bearer_static_tokens_are_secret() -> None:
    """Secret-bearing host config MUST be SecretStr (SPEC-RHB-CFG-002)."""
    s = HostSettings(bearer_static_tokens={"tok-abc": "did:key:zCaller"})
    secret = s.bearer_static_tokens["tok-abc"]
    assert isinstance(secret, SecretStr)
    # The raw value never appears in the model's repr.
    assert "zCaller" not in repr(s)
    assert secret.get_secret_value() == "did:key:zCaller"
