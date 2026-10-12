"""Tenant secrets backends (RH2-01-02, SPEC-RH2-TEN-003)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from prismal_server.config import HostSettings
from prismal_server.tenancy.secrets import (
    EnvSecrets,
    FileSecrets,
    build_secrets,
    is_secret_key,
)

CANARY = "sk-canary-7f3a9e"


def _write_secret(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, mode)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("openrouter_api_key", True),
        ("jev_token", True),
        ("openrouter_enabled", False),
        ("budget_max_tokens", False),  # "_tokens" is not "_token"
    ],
)
def test_is_secret_key(key: str, expected: bool) -> None:
    assert is_secret_key(key) is expected


def test_file_secrets_reads_one_file_per_key(tmp_path: Path) -> None:
    _write_secret(tmp_path / "acme" / "openrouter_api_key", CANARY)
    secrets = FileSecrets(tmp_path).load("acme")
    assert set(secrets) == {"openrouter_api_key"}
    value = secrets["openrouter_api_key"]
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == CANARY  # trailing newline stripped
    assert CANARY not in repr(secrets)


def test_file_secrets_missing_tenant_is_empty(tmp_path: Path) -> None:
    assert FileSecrets(tmp_path).load("nobody") == {}


def test_file_secrets_subdir_layout(tmp_path: Path) -> None:
    _write_secret(tmp_path / "acme" / "secrets" / "opencode_api_key", CANARY)
    secrets = FileSecrets(tmp_path, subdir="secrets").load("acme")
    assert set(secrets) == {"opencode_api_key"}


def test_file_secrets_skips_hidden_files(tmp_path: Path) -> None:
    _write_secret(tmp_path / "acme" / ".DS_Store", "junk")
    assert FileSecrets(tmp_path).load("acme") == {}


def test_file_secrets_warns_on_loose_mode_without_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_secret(tmp_path / "acme" / "openrouter_api_key", CANARY, mode=0o644)
    with caplog.at_level(logging.WARNING):
        FileSecrets(tmp_path).load("acme")
    assert "0600" in caplog.text
    assert CANARY not in caplog.text


def test_file_secrets_fingerprint_tracks_mtime(tmp_path: Path) -> None:
    path = tmp_path / "acme" / "openrouter_api_key"
    _write_secret(path, CANARY)
    backend = FileSecrets(tmp_path)
    before = backend.fingerprint("acme")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert backend.fingerprint("acme") != before


def test_env_secrets_reads_namespaced_vars() -> None:
    environ = {
        "PRISMAL_SERVER_SECRET__ACME_CORP__OPENROUTER_API_KEY": CANARY,
        "PRISMAL_SERVER_SECRET__BETA__OPENROUTER_API_KEY": "other",
        "OPENROUTER_API_KEY": "process-wide",
    }
    secrets = EnvSecrets(environ).load("acme-corp")
    assert {k: v.get_secret_value() for k, v in secrets.items()} == {
        "openrouter_api_key": CANARY
    }


def test_env_secrets_never_writes_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRISMAL_SERVER_SECRET__ACME__JEV_API_KEY", CANARY)
    before = dict(os.environ)
    EnvSecrets().load("acme")
    assert dict(os.environ) == before


def test_build_secrets_selects_backend(tmp_path: Path) -> None:
    file_backend = build_secrets(
        HostSettings(host_tenant_config_dir=tmp_path, host_secrets_backend="file")
    )
    assert isinstance(file_backend, FileSecrets)
    env_backend = build_secrets(HostSettings(host_secrets_backend="env"))
    assert isinstance(env_backend, EnvSecrets)


def test_build_secrets_defaults_to_tenant_dir_layout(tmp_path: Path) -> None:
    """No ``host_secrets_dir`` ⇒ ``<tenant_config_dir>/<org_id>/secrets/``."""
    _write_secret(tmp_path / "acme" / "secrets" / "openrouter_api_key", CANARY)
    backend = build_secrets(HostSettings(host_tenant_config_dir=tmp_path))
    assert set(backend.load("acme")) == {"openrouter_api_key"}
