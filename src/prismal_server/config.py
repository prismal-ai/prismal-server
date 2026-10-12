"""Host configuration (RHB-01-01).

``HostSettings`` (pydantic-settings, prefix ``PRISMAL_SERVER_``) governs *host*
concerns only — bind host/port, CORS, auth mode, SSE heartbeat, tenant cap.
Engine configuration stays engine-side and is read by the engine via its own
``ConfigSourcePort`` (SPEC-RHB-CFG-001); this class MUST NOT duplicate it.
Secret-bearing values are ``SecretStr`` (SPEC-RHB-CFG-002).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthBackend = Literal["none", "bearer", "oidc"]
HostEnvironment = Literal["development", "staging", "production"]
SecretsBackend = Literal["file", "env"]
UsageStoreKind = Literal["memory", "sqlite"]


class HostSettings(BaseSettings):
    """Host-only settings, populated from ``PRISMAL_SERVER_*`` env vars.

    See ``SPEC.md`` §7 for the authoritative key table.
    """

    model_config = SettingsConfigDict(
        env_prefix="PRISMAL_SERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Bind
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = []

    # Auth seam (backends wired in Phase 4)
    host_auth_backend: AuthBackend = "none"
    host_auth_strict: bool = False
    dev_identity_did: str = "did:key:zLocalDev"
    dev_identity_agent_name: str = "local-dev"
    # Static bearer tokens (token → DID) for the dev/bearer backend. Secret by
    # construction so raw tokens never surface in logs or responses.
    bearer_static_tokens: dict[str, SecretStr] = {}

    # Streaming
    sse_heartbeat_s: int = 15

    # Multi-tenancy
    host_max_tenants: int = 32

    # Inbound A2A: gates route presence on the host (404 when off). The engine's
    # own a2a_inbound_enabled / a2a_strict govern the handler and strictness.
    a2a_enabled: bool = True

    # ── Host v0.2 (SPEC-RH2 §10) ────────────────────────────────────────────
    host_environment: HostEnvironment = "development"

    # Per-tenant engine config (RH2-01): `<dir>/<org_id>/tenant.yaml` plus
    # secrets from the selected backend. None ⇒ every tenant uses process config.
    host_tenant_config_dir: Path | None = None
    host_secrets_backend: SecretsBackend = "file"
    # File backend root (`<dir>/<org_id>/<key>`). None ⇒
    # `<host_tenant_config_dir>/<org_id>/secrets/<key>`.
    host_secrets_dir: Path | None = None

    # Usage persistence (RH2-02).
    host_usage_store: UsageStoreKind = "memory"
    host_usage_db_path: Path = Path("usage.db")
    host_usage_retention_days: int = Field(default=90, ge=1)

    # AG-UI host gate (RH2-03); the engine's agui_events_enabled is also required.
    host_agui_enabled: bool = True

    # WebSocket transport (RH2-05).
    host_ws_enabled: bool = False
    host_ws_max_message_bytes: int = Field(default=1_048_576, ge=1)

    # Deterministic fake runtime for credential-free contract tests (RH2-06).
    host_fake_llm_mode: bool = False

    # Read-only admin endpoints (RH2-06).
    host_admin_enabled: bool = False
    host_admin_scope: str = Field(default="prismal:admin", min_length=1)


@lru_cache
def get_settings() -> HostSettings:
    """Return a process-wide cached ``HostSettings`` instance."""
    return HostSettings()
