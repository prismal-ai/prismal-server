"""Per-tenant ``ConfigSourcePort`` (RH2-01-03, SPEC-RH2-TEN-001/002/003).

``TenantConfigProvider.load(org_id)`` reads ``<root>/<org_id>/tenant.yaml`` and
the tenant's secrets, validates every key against a prefix allowlist and returns
a frozen :class:`TenantConfigSource` — structurally the engine's
``ConfigSourcePort`` (``load() -> Mapping[str, str | SecretStr]``), so the host
never imports it. The registry passes it to ``build_runtime(config_source=…)``.

Validation happens **here, eagerly**, not inside ``load()``: the engine's
``ChainedConfigSource`` swallows a failing sub-source and would silently fall
back to process settings. Any problem therefore raises
:class:`TenantConfigInvalid` before the runtime is built (fail-closed), and only
that tenant is refused.

``tenant.yaml`` is a flat mapping of engine ``Settings`` field names to scalars
(lists/maps are JSON-encoded for the engine's settings decoder). Secret keys
(``*_api_key`` / ``*_token``) are **not** accepted inline; they come only from
the ``TenantSecretsPort``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr

from prismal_server.tenancy.secrets import (
    SecretsFingerprint,
    TenantSecretsPort,
    is_secret_key,
)

logger = logging.getLogger(__name__)

TENANT_FILE = "tenant.yaml"
ENGINE_ENV_PREFIX = "PRISMAL_"

# SPEC-RH2-TEN-002. A tenant may tune gateways, decisions, routing, plan cache,
# budget, AG-UI and the A2A version — never security, providers or storage.
ALLOWED_KEY_PREFIXES: tuple[str, ...] = (
    "llm_gateway_",
    "openrouter_",
    "opencode_",
    "omniroute_",
    "decision",
    "jev_",
    "laya_",
    "adaptive_routing_",
    "routing_",
    "plan_cache_",
    "budget_",
    "agui_",
)
ALLOWED_EXACT_KEYS: frozenset[str] = frozenset({"a2a_protocol_version"})

# Denied even inside an allowed prefix (security review of RH2-1). A tenant must
# not redirect an endpoint — with the operator's fallback key that exfiltrates
# credentials — bill the operator's key, lower a security floor, or point the
# engine at host filesystem paths. Endpoints/paths stay operator-only.
DENIED_KEY_SUFFIXES: tuple[str, ...] = (
    "_url",
    "_path",
    "_dir",
    "_file",
    "_min_version",
    "_declared_version",
    "_allow_unknown_version",
)
DENIED_EXACT_KEYS: frozenset[str] = frozenset(
    {
        "llm_gateway_openai_compatible",  # {base_url, auth_ref, headers}
        "llm_gateway_tenant_key_fallback",  # bills the operator's key
        "laya_checkpoint",  # local model checkpoint location
    }
)

_ORG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

ConfigValue = str | SecretStr
# (tenant.yaml mtime_ns, secrets fingerprint); None ⇒ no tenant.yaml.
TenantFingerprint = tuple[int, SecretsFingerprint]


class TenantConfigInvalid(Exception):
    """A tenant's configuration was refused (HTTP 500 ``tenant_config_invalid``).

    ``str(exc)`` is deliberately opaque; ``reason`` (key *names* only, never
    values) is for logs.
    """

    def __init__(self, org_id: str, reason: str) -> None:
        super().__init__("tenant configuration is invalid")
        self.org_id = org_id
        self.reason = reason


def is_allowed_key(key: str) -> bool:
    """Whether a tenant may set engine key ``key`` (SPEC-RH2-TEN-002)."""
    if key != key.lower():
        return False
    if key in ALLOWED_EXACT_KEYS:
        return True
    if key in DENIED_EXACT_KEYS or key.endswith(DENIED_KEY_SUFFIXES):
        return False
    return key.startswith(ALLOWED_KEY_PREFIXES)


class TenantConfigSource:
    """Frozen, validated tenant config; structurally a ``ConfigSourcePort``."""

    def __init__(self, org_id: str, values: Mapping[str, ConfigValue]) -> None:
        self.org_id = org_id
        self._values = dict(values)

    @property
    def keys(self) -> frozenset[str]:
        """Engine field names this tenant overrides (no values)."""
        return frozenset(self._values)

    def load(self) -> dict[str, ConfigValue]:
        """Engine-env-style mapping (``PRISMAL_<KEY>``). Pure; never raises."""
        return {
            ENGINE_ENV_PREFIX + key.upper(): value
            for key, value in self._values.items()
        }

    def __repr__(self) -> str:
        return (
            f"TenantConfigSource(org_id={self.org_id!r}, keys={sorted(self._values)})"
        )


def _to_config_value(org_id: str, key: str, value: Any) -> str:
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    if isinstance(value, list | dict):
        return json.dumps(value)
    raise TenantConfigInvalid(
        org_id, f"key {key!r} has unsupported type {type(value).__name__}"
    )


class TenantConfigProvider:
    """Loads and fingerprints tenant configs under one root directory."""

    def __init__(self, root: Path, secrets: TenantSecretsPort) -> None:
        self._root = Path(root)
        self._secrets = secrets

    def _tenant_file(self, org_id: str) -> Path:
        # org_id becomes a path component: refuse anything that could escape root.
        if not _ORG_ID_RE.fullmatch(org_id):
            raise TenantConfigInvalid(org_id, "org_id is not a safe directory name")
        return self._root / org_id / TENANT_FILE

    def fingerprint(self, org_id: str) -> TenantFingerprint | None:
        """Change-detection token; ``None`` when the tenant has no config file."""
        path = self._tenant_file(org_id)
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        return (mtime, self._secrets.fingerprint(org_id))

    def load(self, org_id: str) -> TenantConfigSource | None:
        """Validated source for ``org_id``; ``None`` ⇒ use process config.

        Raises:
            TenantConfigInvalid: unreadable/malformed file, forbidden or inline
                secret key, unsupported value, or an invalid secret.
        """
        path = self._tenant_file(org_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TenantConfigInvalid(org_id, f"{TENANT_FILE} unreadable") from exc
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError:
            # `from None`: YAML errors quote the offending line, which may hold a
            # pasted secret; the traceback must not carry it into logs.
            raise TenantConfigInvalid(
                org_id, f"{TENANT_FILE} is not valid YAML"
            ) from None
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise TenantConfigInvalid(org_id, f"{TENANT_FILE} must be a mapping")

        values: dict[str, ConfigValue] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not is_allowed_key(key):
                raise TenantConfigInvalid(org_id, f"key {key!r} is not allowed")
            if is_secret_key(key):
                raise TenantConfigInvalid(
                    org_id, f"secret key {key!r} must come from the secrets backend"
                )
            values[key] = _to_config_value(org_id, key, value)

        try:
            secrets = self._secrets.load(org_id)
        except Exception as exc:
            raise TenantConfigInvalid(org_id, "secrets backend failed") from exc
        for key, secret in secrets.items():
            if not (is_allowed_key(key) and is_secret_key(key)):
                raise TenantConfigInvalid(org_id, f"secret {key!r} is not allowed")
            if not secret.get_secret_value():
                raise TenantConfigInvalid(org_id, f"secret {key!r} is empty")
            values[key] = secret

        return TenantConfigSource(org_id, values)
