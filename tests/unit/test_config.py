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


# ── Host v0.2 settings (RH2-01-01, SPEC-RH2 §10) ────────────────────────────


def test_v02_defaults_match_spec() -> None:
    s = HostSettings()
    assert s.host_environment == "development"
    assert s.host_tenant_config_dir is None
    assert s.host_secrets_backend == "file"
    assert s.host_secrets_dir is None
    assert s.host_usage_store == "memory"
    assert str(s.host_usage_db_path) == "usage.db"
    assert s.host_usage_retention_days == 90
    assert s.host_agui_enabled is True
    assert s.host_ws_enabled is False
    assert s.host_ws_max_message_bytes == 1_048_576
    assert s.host_fake_llm_mode is False
    assert s.host_admin_enabled is False
    assert s.host_admin_scope == "prismal:admin"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host_environment", "prod"),
        ("host_secrets_backend", "vault"),
        ("host_usage_store", "postgres"),
        ("host_usage_retention_days", 0),
        ("host_ws_max_message_bytes", 0),
        ("host_admin_scope", ""),
    ],
)
def test_v02_settings_are_validated(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        HostSettings(**{field: value})  # type: ignore[arg-type]


def test_v02_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.setenv("PRISMAL_SERVER_HOST_TENANT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("PRISMAL_SERVER_HOST_SECRETS_BACKEND", "env")
    monkeypatch.setenv("PRISMAL_SERVER_HOST_ENVIRONMENT", "staging")
    s = HostSettings()
    assert str(s.host_tenant_config_dir) == str(tmp_path)
    assert s.host_secrets_backend == "env"
    assert s.host_environment == "staging"
