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

Trust model (decided 2026-10-12, SPEC-RH2-TEN-005): ``tenant.yaml`` and tenant
secrets are **operator-authored** — the host vault is owned by the operator and
tenants never edit their own config. Per-tenant budget limits, gateway quotas
and decision/routing modes are therefore the operator's per-tenant policy, not a
tenant-controlled override, and are allowed by design. The exact allowlist is
defense in depth against operator mistakes: even the operator cannot use a
tenant file to redirect endpoints, enable operator-key fallback, point at host
paths or lower security/privacy/compliance floors — those stay process-level.
Self-service (tenant-authored) config is out of scope; it would need a
tighten-only layer over operator ceilings and a SPEC change.
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

# SPEC-RH2-TEN-002 prefixes. Every key below must also carry one of them.
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
    "a2a_protocol_version",
)

# Exact allowlist (security review of RH2-1): a prefix alone is not enough.
# A tenant may *select and tune* gateways, decisions, routing, plan cache,
# budget and AG-UI. It may NOT redirect an endpoint (a tenant URL plus the
# operator's fallback key exfiltrates credentials), bill the operator's key,
# lower a security/privacy/compliance floor (ZDR, retention and ToS deny-lists,
# risk floor, HITL and calibration gates, remote Ollama, CVE version floors),
# point the engine at host paths, or size process-wide resources. Any engine
# key not listed here — including ones added in future engine releases — is
# refused (fail-closed). Widening this set is a reviewed change.
ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        # Gateways
        "llm_gateway_agent_map",
        "openrouter_enabled",
        "openrouter_api_key",
        "openrouter_app_title",
        "openrouter_app_categories",
        "openrouter_session_stickiness",
        "openrouter_plugins",
        "openrouter_auto_tier",
        "openrouter_provider_sort",
        "openrouter_max_price",
        "openrouter_only",
        "openrouter_ignore",
        "openrouter_fallback_models",
        "opencode_zen_enabled",
        "opencode_go_enabled",
        "opencode_api_key",
        "opencode_go_monthly_cap_usd",
        "opencode_go_window_5h_ratio",
        "opencode_go_window_week_ratio",
        "opencode_go_quota_action",
        "opencode_go_downgrade_model",
        "omniroute_enabled",
        "omniroute_api_key",
        # Typed decisions (DEC)
        "decisions_enabled",
        "decision_routing_enabled",
        "decision_eval_judge_enabled",
        "decision_tool_proposal_enabled",
        "decision_rag_relevance_enabled",
        "decision_ctx_relevance_enabled",
        "decision_swarm_eval_enabled",
        "decision_backend_order",
        "decision_modes",
        "decision_timeout_ms",
        "decision_shadow_sample_rate",
        "decision_llm_fallback_model",
        "decision_ollama_models",
        "decision_ollama_keep_alive",
        "decision_ollama_warmup",
        "decision_ollama_accurate_max_p95_ms",
        "decision_ollama_timeout_ms",
        "decision_ollama_max_batch",
        "jev_api_key",
        "jev_model",
        "jev_transport",
        "jev_max_choice_options",
        "jev_max_request_tokens",
        "laya_mode",
        "laya_api_key",
        "laya_max_choice_options",
        # Adaptive routing (AMR)
        "adaptive_routing_enabled",
        "routing_mode",
        "routing_policy",
        "routing_tier_capabilities",
        "routing_cascade_start_tier",
        "routing_cascade_max_escalations",
        "routing_cascade_tool_error_threshold",
        "routing_adequacy_tau",
        "routing_adequacy_timeout_ms",
        "routing_cache_affinity_enabled",
        "routing_batch_alpha",
        "routing_batch_max_iter",
        "routing_batch_eps",
        # Plan cache
        "plan_cache_enabled",
        "plan_cache_ttl_s",
        # Budget (tenant cost governance, SPEC-RH2-BUD-*)
        "budget_enabled",
        "budget_max_tokens",
        "budget_max_cost_usd",
        "budget_max_calls",
        "budget_max_wall_clock_s",
        "budget_scope",
        "budget_soft_ratio",
        "budget_hard_cap",
        "budget_pricing",
        "budget_alert_usd",
        # AG-UI + A2A
        "agui_events_enabled",
        "agui_include_state_deltas",
        "agui_include_usage",
        "agui_include_metadata",
        "a2a_protocol_version",
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
    return key in ALLOWED_KEYS


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
