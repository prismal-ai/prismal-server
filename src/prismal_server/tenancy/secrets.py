"""Tenant secrets backends (RH2-01-02, SPEC-RH2-TEN-003).

A ``TenantSecretsPort`` returns one tenant's secret values as ``SecretStr``.
Values never touch ``os.environ``, logs, error bodies or ``repr``; only key
*names* are ever logged. Which keys are acceptable is decided by
:mod:`prismal_server.tenancy.config_source` (allowlist + secret suffix).

Backends:

* ``file`` — one file per secret, ``<root>/<org_id>/<key>`` (or
  ``<root>/<org_id>/<subdir>/<key>``), content stripped. Files should be mode
  ``0600``; looser modes are logged as a warning (the value never is).
* ``env`` — read-only view of ``PRISMAL_SERVER_SECRET__<ORG>__<KEY>`` where
  ``<ORG>`` is the upper-cased ``org_id`` with ``-``/``.`` mapped to ``_``.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from prismal_server.config import HostSettings

logger = logging.getLogger(__name__)

SECRET_KEY_SUFFIXES: tuple[str, ...] = ("_api_key", "_token")
ENV_SECRET_PREFIX = "PRISMAL_SERVER_SECRET__"

# Hashable, comparable snapshot used to detect secret changes (TEN-004).
SecretsFingerprint = tuple[tuple[str, int], ...]


def is_secret_key(key: str) -> bool:
    """True for engine keys that carry a credential (``*_api_key``/``*_token``)."""
    return key.endswith(SECRET_KEY_SUFFIXES)


class TenantSecretsPort(Protocol):
    """Source of one tenant's secret values."""

    def load(self, org_id: str) -> Mapping[str, SecretStr]:
        """Return ``{engine_key: SecretStr}`` for ``org_id`` (empty if none)."""
        ...

    def fingerprint(self, org_id: str) -> SecretsFingerprint:
        """A value that changes whenever ``org_id``'s secrets change."""
        ...


class FileSecrets:
    """One file per secret under ``<root>/<org_id>[/<subdir>]/``."""

    def __init__(self, root: Path, *, subdir: str | None = None) -> None:
        self._root = Path(root)
        self._subdir = subdir

    def _dir(self, org_id: str) -> Path:
        base = self._root / org_id
        return base / self._subdir if self._subdir else base

    def _files(self, org_id: str) -> list[Path]:
        directory = self._dir(org_id)
        if not directory.is_dir():
            return []
        return sorted(
            p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")
        )

    def load(self, org_id: str) -> dict[str, SecretStr]:
        secrets: dict[str, SecretStr] = {}
        for path in self._files(org_id):
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                logger.warning(
                    "tenant secret file %s for org_id=%s has mode %o; expected 0600",
                    path.name,
                    org_id,
                    mode,
                )
            secrets[path.name] = SecretStr(path.read_text(encoding="utf-8").strip())
        return secrets

    def fingerprint(self, org_id: str) -> SecretsFingerprint:
        return tuple((p.name, p.stat().st_mtime_ns) for p in self._files(org_id))


def _env_org_token(org_id: str) -> str:
    return re.sub(r"[-.]", "_", org_id).upper()


class EnvSecrets:
    """Read-only view over ``PRISMAL_SERVER_SECRET__<ORG>__<KEY>`` variables."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def load(self, org_id: str) -> dict[str, SecretStr]:
        prefix = f"{ENV_SECRET_PREFIX}{_env_org_token(org_id)}__"
        return {
            name[len(prefix) :].lower(): SecretStr(value)
            for name, value in self._environ.items()
            if name.startswith(prefix)
        }

    def fingerprint(self, org_id: str) -> SecretsFingerprint:
        # The process environment is fixed for the process lifetime.
        return ()


def build_secrets(settings: HostSettings) -> TenantSecretsPort:
    """Select the secrets backend from ``HostSettings``."""
    if settings.host_secrets_backend == "env":
        return EnvSecrets()
    if settings.host_secrets_dir is not None:
        return FileSecrets(settings.host_secrets_dir)
    root = settings.host_tenant_config_dir or Path(".")
    return FileSecrets(root, subdir="secrets")
