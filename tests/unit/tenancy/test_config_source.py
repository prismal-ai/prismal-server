"""Per-tenant config source + key allowlist (RH2-01-03, SPEC-RH2-TEN-001/002/003)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from prismal_server.tenancy.config_source import (
    TenantConfigInvalid,
    TenantConfigProvider,
    TenantConfigSource,
    is_allowed_key,
)
from prismal_server.tenancy.secrets import FileSecrets

CANARY = "sk-canary-7f3a9e"


def _tenant(root: Path, org_id: str, yaml_text: str) -> Path:
    path = root / org_id / "tenant.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_text, encoding="utf-8")
    return path


def _secret(root: Path, org_id: str, key: str, value: str) -> None:
    path = root / org_id / "secrets" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


@pytest.fixture
def provider(tmp_path: Path) -> TenantConfigProvider:
    return TenantConfigProvider(tmp_path, FileSecrets(tmp_path, subdir="secrets"))


@pytest.mark.parametrize(
    "key",
    [
        "llm_gateway_agent_map",
        "openrouter_enabled",
        "opencode_go_enabled",
        "omniroute_base_url",
        "decisions_enabled",
        "decision_routing_enabled",
        "jev_model",
        "laya_mode",
        "adaptive_routing_enabled",
        "routing_policy",
        "plan_cache_enabled",
        "budget_max_cost_usd",
        "agui_events_enabled",
        "a2a_protocol_version",
    ],
)
def test_allowed_prefixes(key: str) -> None:
    assert is_allowed_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "security_strict",
        "anthropic_api_key",
        "a2a_inbound_enabled",
        "a2a_protocol_version_x",
        "OPENROUTER_ENABLED",
        "config_source_strict",
    ],
)
def test_forbidden_keys(key: str) -> None:
    assert not is_allowed_key(key)


def test_load_maps_keys_to_engine_env_names(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    _tenant(
        tmp_path,
        "acme",
        "openrouter_enabled: true\n"
        "budget_max_tokens: 2000000\n"
        "budget_max_cost_usd: 50.5\n"
        "routing_policy: composite\n"
        "llm_gateway_agent_map: {researcher: openrouter}\n",
    )
    source = provider.load("acme")
    assert isinstance(source, TenantConfigSource)
    assert source.load() == {
        "PRISMAL_OPENROUTER_ENABLED": "true",
        "PRISMAL_BUDGET_MAX_TOKENS": "2000000",
        "PRISMAL_BUDGET_MAX_COST_USD": "50.5",
        "PRISMAL_ROUTING_POLICY": "composite",
        "PRISMAL_LLM_GATEWAY_AGENT_MAP": '{"researcher": "openrouter"}',
    }


def test_load_returns_none_without_tenant_file(
    provider: TenantConfigProvider,
) -> None:
    assert provider.load("acme") is None
    assert provider.fingerprint("acme") is None


def test_empty_tenant_file_is_valid(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    _tenant(tmp_path, "acme", "")
    source = provider.load("acme")
    assert source is not None and source.load() == {}


def test_forbidden_key_fails_closed(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    """EARS TEN-002: ``security_strict: false`` refuses the tenant."""
    _tenant(tmp_path, "acme", "openrouter_enabled: true\nsecurity_strict: false\n")
    with pytest.raises(TenantConfigInvalid) as info:
        provider.load("acme")
    assert "security_strict" in info.value.reason
    assert str(info.value) == "tenant configuration is invalid"  # opaque


def test_literal_secret_in_yaml_is_rejected(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    _tenant(tmp_path, "acme", f"openrouter_api_key: {CANARY}\n")
    with pytest.raises(TenantConfigInvalid) as info:
        provider.load("acme")
    assert CANARY not in info.value.reason
    assert CANARY not in str(info.value)


@pytest.mark.parametrize(
    "yaml_text",
    ["- not\n- a mapping\n", "openrouter_enabled: [unclosed\n", "budget_scope: null\n"],
)
def test_malformed_tenant_file_fails_closed(
    tmp_path: Path, provider: TenantConfigProvider, yaml_text: str
) -> None:
    _tenant(tmp_path, "acme", yaml_text)
    with pytest.raises(TenantConfigInvalid):
        provider.load("acme")


def test_secrets_are_merged_as_secretstr(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    _tenant(tmp_path, "acme", "openrouter_enabled: true\n")
    _secret(tmp_path, "acme", "openrouter_api_key", CANARY)
    source = provider.load("acme")
    assert source is not None
    loaded = source.load()
    secret = loaded["PRISMAL_OPENROUTER_API_KEY"]
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == CANARY
    assert CANARY not in repr(source)
    assert CANARY not in str(loaded)


def test_secret_outside_allowlist_fails_closed(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    """A tenant cannot inject e.g. the process-wide provider key."""
    _tenant(tmp_path, "acme", "")
    _secret(tmp_path, "acme", "anthropic_api_key", CANARY)
    with pytest.raises(TenantConfigInvalid) as info:
        provider.load("acme")
    assert CANARY not in info.value.reason


def test_non_secret_file_in_secrets_dir_fails_closed(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    _tenant(tmp_path, "acme", "")
    _secret(tmp_path, "acme", "openrouter_enabled", "true")
    with pytest.raises(TenantConfigInvalid):
        provider.load("acme")


@pytest.mark.parametrize("org_id", ["..", "../etc", "a/b", "", ".hidden", "x" * 200])
def test_unsafe_org_id_is_rejected(provider: TenantConfigProvider, org_id: str) -> None:
    with pytest.raises(TenantConfigInvalid):
        provider.load(org_id)
    with pytest.raises(TenantConfigInvalid):
        provider.fingerprint(org_id)


def test_fingerprint_changes_with_tenant_file(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    path = _tenant(tmp_path, "acme", "openrouter_enabled: true\n")
    before = provider.fingerprint("acme")
    assert before is not None
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert provider.fingerprint("acme") != before


def test_fingerprint_changes_with_secret_file(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    _tenant(tmp_path, "acme", "")
    before = provider.fingerprint("acme")
    _secret(tmp_path, "acme", "openrouter_api_key", CANARY)
    assert provider.fingerprint("acme") != before


def test_source_is_structurally_a_config_source_port() -> None:
    """The engine Protocol is satisfied without the host importing it."""
    from prismal.core.config_source import ConfigSourcePort

    source = TenantConfigSource("acme", {"routing_policy": "composite"})
    assert isinstance(source, ConfigSourcePort)


def test_engine_builds_tenant_settings_from_source(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    """End-to-end with the engine's own settings builder (no I/O)."""
    from prismal.core.config import build_settings

    _tenant(tmp_path, "acme", "openrouter_enabled: true\nbudget_max_calls: 7\n")
    _secret(tmp_path, "acme", "openrouter_api_key", CANARY)
    source = provider.load("acme")
    assert source is not None
    settings = build_settings(source)
    assert settings.openrouter_enabled is True
    assert settings.budget_max_calls == 7
    assert settings.openrouter_api_key.get_secret_value() == CANARY


def test_yaml_error_does_not_chain_line_content(
    tmp_path: Path, provider: TenantConfigProvider
) -> None:
    _tenant(tmp_path, "acme", f"budget_scope: [{CANARY}\n")
    with pytest.raises(TenantConfigInvalid) as info:
        provider.load("acme")
    assert info.value.__cause__ is None and info.value.__suppress_context__
