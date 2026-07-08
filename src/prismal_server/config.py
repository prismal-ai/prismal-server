"""Host configuration (RHB-01-01).

``HostSettings`` (pydantic-settings, prefix ``PRISMAL_SERVER_``) governs *host*
concerns only — bind host/port, CORS, auth mode, SSE heartbeat, tenant cap.
Engine configuration stays engine-side and is read by the engine via its own
``ConfigSourcePort`` (SPEC-RHB-CFG-001); this class MUST NOT duplicate it.
Secret-bearing values are ``SecretStr`` (SPEC-RHB-CFG-002).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthBackend = Literal["none", "bearer", "oidc"]


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
    # Static bearer tokens (token → DID) for the dev/bearer backend. Secret by
    # construction so raw tokens never surface in logs or responses.
    bearer_static_tokens: dict[str, SecretStr] = {}

    # Streaming
    sse_heartbeat_s: int = 15

    # Multi-tenancy
    host_max_tenants: int = 32


@lru_cache
def get_settings() -> HostSettings:
    """Return a process-wide cached ``HostSettings`` instance."""
    return HostSettings()
