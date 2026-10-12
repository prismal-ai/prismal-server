"""Registry ⇄ tenant config (RH2-01-04/05, SPEC-RH2-TEN-001/002/004)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry
from prismal_server.tenancy.config_source import TenantConfigInvalid

CANARY = "sk-canary-7f3a9e"


class FakeRuntime:
    def __init__(self, org_id: str | None, config_source: Any) -> None:
        self.org_id = org_id
        self.config_source = config_source
        self.closed = False
        self.tool_provider = object()

    async def aclose(self) -> None:
        self.closed = True


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


def _touch(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def _registry(root: Path) -> tuple[RuntimeRegistry, list[FakeRuntime]]:
    built: list[FakeRuntime] = []

    async def builder(*, org_id: str | None, config_source: Any = None) -> FakeRuntime:
        runtime = FakeRuntime(org_id, config_source)
        built.append(runtime)
        return runtime

    settings = HostSettings(host_tenant_config_dir=root)
    return RuntimeRegistry(settings, builder=builder), built


async def test_without_config_dir_no_source_is_passed() -> None:
    seen: list[Any] = []

    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        seen.append(config_source)
        return FakeRuntime(org_id, config_source)

    registry = RuntimeRegistry(HostSettings(), builder=builder)
    await registry.get("acme")
    assert seen == [None]


async def test_tenant_without_file_uses_process_config(tmp_path: Path) -> None:
    registry, built = _registry(tmp_path)
    await registry.get("acme")
    await registry.get(None)
    assert [r.config_source for r in built] == [None, None]


async def test_two_tenants_get_different_gateways(tmp_path: Path) -> None:
    """RH2-01-05: per-tenant gateway config reaches the engine builder."""
    from prismal.core.config import build_settings

    _tenant(tmp_path, "acme", "openrouter_enabled: true\n")
    _secret(tmp_path, "acme", "openrouter_api_key", CANARY)
    _tenant(tmp_path, "beta", "opencode_go_enabled: true\nopenrouter_enabled: false\n")
    registry, _ = _registry(tmp_path)

    acme = await registry.get("acme")
    beta = await registry.get("beta")
    assert acme is not beta
    acme_settings = build_settings(acme.config_source)
    beta_settings = build_settings(beta.config_source)
    assert acme_settings.openrouter_enabled is True
    assert acme_settings.openrouter_api_key.get_secret_value() == CANARY
    assert beta_settings.openrouter_enabled is False
    assert beta_settings.opencode_go_enabled is True
    assert beta_settings.openrouter_api_key.get_secret_value() != CANARY


async def test_forbidden_key_refuses_only_that_tenant(tmp_path: Path) -> None:
    """EARS TEN-002: refused tenant, other tenants keep serving."""
    path = _tenant(tmp_path, "acme", "security_strict: false\n")
    _tenant(tmp_path, "beta", "routing_policy: composite\n")
    registry, built = _registry(tmp_path)

    with pytest.raises(TenantConfigInvalid):
        await registry.get("acme")
    assert (await registry.get("beta")).org_id == "beta"
    assert [r.org_id for r in built] == ["beta"]  # nothing built for acme

    # Not cached as a failure: fixing the file serves the tenant.
    path.write_text("routing_policy: composite\n", encoding="utf-8")
    _touch(path)
    assert (await registry.get("acme")).org_id == "acme"


async def test_change_rebuilds_and_closes_idle_runtime(tmp_path: Path) -> None:
    path = _tenant(tmp_path, "acme", "routing_policy: composite\n")
    registry, _ = _registry(tmp_path)
    first = await registry.get("acme")
    assert await registry.get("acme") is first  # unchanged ⇒ cached

    path.write_text("routing_policy: cascade\n", encoding="utf-8")
    _touch(path)
    second = await registry.get("acme")
    assert second is not first
    assert first.closed is True
    assert second.config_source.load()["PRISMAL_ROUTING_POLICY"] == "cascade"


async def test_secret_change_rebuilds(tmp_path: Path) -> None:
    _tenant(tmp_path, "acme", "")
    registry, _ = _registry(tmp_path)
    first = await registry.get("acme")
    _secret(tmp_path, "acme", "openrouter_api_key", CANARY)
    assert await registry.get("acme") is not first


async def test_change_waits_for_in_flight_turns(tmp_path: Path) -> None:
    """TEN-004: the old runtime closes only after its in-flight turn ends."""
    path = _tenant(tmp_path, "acme", "routing_policy: composite\n")
    registry, _ = _registry(tmp_path)
    lease = await registry.acquire("acme")
    old = lease.runtime

    _touch(path)
    new = await registry.get("acme")
    assert new is not old
    assert old.closed is False  # turn still running

    await lease.release()
    assert old.closed is True
    await lease.release()  # idempotent


async def test_lease_on_current_runtime_does_not_close_it(tmp_path: Path) -> None:
    _tenant(tmp_path, "acme", "")
    registry, _ = _registry(tmp_path)
    lease = await registry.acquire("acme")
    await lease.release()
    assert lease.runtime.closed is False
    assert await registry.get("acme") is lease.runtime


async def test_aclose_all_closes_retiring_runtimes(tmp_path: Path) -> None:
    path = _tenant(tmp_path, "acme", "")
    registry, _ = _registry(tmp_path)
    lease = await registry.acquire("acme")
    _touch(path)
    new = await registry.get("acme")
    await registry.aclose_all()
    assert lease.runtime.closed is True and new.closed is True


async def test_lru_eviction_waits_for_in_flight_turns(tmp_path: Path) -> None:
    built: list[FakeRuntime] = []

    async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
        built.append(FakeRuntime(org_id, config_source))
        return built[-1]

    registry = RuntimeRegistry(HostSettings(host_max_tenants=1), builder=builder)
    lease = await registry.acquire("acme")
    await registry.get("beta")  # evicts acme while its turn runs
    assert lease.runtime.closed is False
    await lease.release()
    assert lease.runtime.closed is True
