"""Admin read endpoints (RH2-06-03/04/06/07, SPEC-RH2-ADM-001..005)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from prismal.composition.host_surface import (
    AuditChainStatus,
    AuditEntryView,
    AuditPageView,
    DecisionIntegrationStats,
    DecisionsReportView,
    ReportWindow,
    RoutingPolicyStats,
    RoutingReportView,
)
from prismal.identity import DID, AgentIdentity, Scope

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.deps import RuntimeRegistry

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class FakeRuntime:
    """Records the HRS calls the admin routes make."""

    def __init__(self, org_id: str | None, *, enabled: bool = True) -> None:
        self.org_id = org_id
        self.enabled = enabled
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.settings = SimpleNamespace(a2a_protocol_version="1.0")
        self.gateways = SimpleNamespace(names=lambda: ["openrouter", "opencode_go"])

    def audit_page(self, **kw: Any) -> AuditPageView:
        self.calls.append(("audit_page", kw))
        entry = AuditEntryView(
            seq=7,
            ts=T1,
            event="tool_call",
            actor="agent",
            org_id=self.org_id,
            hash="h7",
            prev_hash="h6",
            fields={"tool": "search"},
        )
        return AuditPageView(
            page=kw["page"], page_size=kw["page_size"], total=1, entries=(entry,)
        )

    def verify_audit_chain(self, **kw: Any) -> AuditChainStatus:
        self.calls.append(("verify_audit_chain", kw))
        return AuditChainStatus(status="valid", checked=7, first_broken_seq=None)

    def decisions_report(self, **kw: Any) -> DecisionsReportView:
        self.calls.append(("decisions_report", kw))
        if not self.enabled:
            return DecisionsReportView(enabled=False, integrations=())
        row = DecisionIntegrationStats(
            integration="routing",
            mode="shadow",
            samples=0,
            agreement_ratio=None,
            accepted=0,
            escalated=0,
            abstained=0,
            llm_calls_avoided=0,
            # Engine clamps to retained stats: later than requested.
            window=ReportWindow(since=T0 + timedelta(days=1), until=T1),
        )
        return DecisionsReportView(enabled=True, integrations=(row,))

    def routing_report(self, **kw: Any) -> RoutingReportView:
        self.calls.append(("routing_report", kw))
        if not self.enabled:
            return RoutingReportView(enabled=False, mode="shadow", policies=())
        row = RoutingPolicyStats(
            policy="composite",
            decisions=5120,
            decisions_by_tier={"small": 3900, "medium": 1000, "frontier": 220},
            escalations=140,
            escalations_by_reason={"inadequate": 90, "structured_invalid": 50},
            plan_cache_hits=610,
            estimated_savings_usd=12.43,
            window=ReportWindow(since=T0, until=T1),
        )
        return RoutingReportView(enabled=True, mode="enforce", policies=(row,))

    async def aclose(self) -> None:
        return None


class TokenBackend:
    """``Bearer <org>:<scope>,<scope>`` ⇒ identity of ``org`` with those scopes."""

    async def resolve(self, request: Any) -> Any:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        org, _, scopes = auth.removeprefix("Bearer ").partition(":")
        return AgentIdentity(
            did=DID(f"did:key:z{org or 'anon'}"),
            agent_name=org or "anon",
            org_id=org or None,
            scopes=tuple(Scope(s) for s in scopes.split(",") if s),
        )


class World:
    def __init__(
        self, tmp_path: Path | None = None, *, enabled: bool = True, **settings: Any
    ) -> None:
        self.runtimes: dict[str | None, FakeRuntime] = {}

        async def builder(*, org_id: str | None, config_source: Any = None) -> Any:
            runtime = FakeRuntime(org_id, enabled=enabled)
            self.runtimes[org_id] = runtime
            return runtime

        settings.setdefault("host_admin_enabled", True)
        settings.setdefault("host_auth_backend", "bearer")
        if tmp_path is not None:
            settings.setdefault("host_tenant_config_dir", tmp_path)
        host = HostSettings(**settings)
        self.registry = RuntimeRegistry(host, builder=builder)
        self.app = create_app(
            settings=host, registry=self.registry, auth_backend=TokenBackend()
        )

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://t")

    def engine_calls(self) -> list[str]:
        return [name for rt in self.runtimes.values() for name, _ in rt.calls]


ADMIN = {"Authorization": "Bearer acme:prismal:admin"}
GLOBAL = {"Authorization": "Bearer ops:prismal:admin,prismal:admin:global"}
PLAIN = {"Authorization": "Bearer acme:usage:read"}
PATHS = [
    "/v1/admin/tenants",
    "/v1/admin/audit",
    "/v1/admin/decisions",
    "/v1/admin/routing",
]


# ── ADM-001: gate ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", PATHS)
async def test_admin_routes_404_when_disabled(path: str) -> None:
    world = World(host_admin_enabled=False)
    async with world.client() as c:
        resp = await c.get(path, headers=GLOBAL)
    assert resp.status_code == 404


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("headers", [PLAIN, {}])
async def test_admin_routes_403_without_scope_and_no_engine_call(
    path: str, headers: dict[str, str]
) -> None:
    world = World()
    async with world.client() as c:
        resp = await c.get(path, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert world.engine_calls() == []


async def test_unauthenticated_strict_is_401() -> None:
    world = World(host_auth_strict=True)
    async with world.client() as c:
        resp = await c.get("/v1/admin/routing")
    assert resp.status_code == 401


async def test_custom_admin_scope() -> None:
    world = World(host_admin_scope="ops:read")
    async with world.client() as c:
        ok = await c.get(
            "/v1/admin/routing", headers={"Authorization": "Bearer acme:ops:read"}
        )
        denied = await c.get("/v1/admin/routing", headers=ADMIN)
    assert ok.status_code == 200 and denied.status_code == 403


# ── Cross-tenant: own org only unless `<scope>:global` ─────────────────────


@pytest.mark.parametrize("path", PATHS[1:])
@pytest.mark.parametrize("org", ["beta", "*"])
async def test_other_org_needs_global_scope(path: str, org: str) -> None:
    world = World()
    async with world.client() as c:
        resp = await c.get(path, params={"org_id": org}, headers=ADMIN)
    assert resp.status_code == 403
    assert world.engine_calls() == []


async def test_admin_without_org_needs_global_scope() -> None:
    """No org on the identity would mean the process-wide (all-tenant) view."""
    world = World()
    async with world.client() as c:
        resp = await c.get(
            "/v1/admin/audit", headers={"Authorization": "Bearer :prismal:admin"}
        )
    assert resp.status_code == 403


async def test_x_org_id_header_does_not_select_org() -> None:
    world = World()
    async with world.client() as c:
        resp = await c.get("/v1/admin/routing", headers={**ADMIN, "X-Org-Id": "beta"})
    assert resp.status_code == 200
    assert resp.json()["org_id"] == "acme"
    assert set(world.runtimes) == {"acme"}


async def test_global_admin_reads_any_org_and_all() -> None:
    world = World()
    async with world.client() as c:
        beta = await c.get(
            "/v1/admin/routing", params={"org_id": "beta"}, headers=GLOBAL
        )
        every = await c.get("/v1/admin/audit", params={"org_id": "*"}, headers=GLOBAL)
    assert beta.status_code == 200 and beta.json()["org_id"] == "beta"
    assert every.status_code == 200
    # `*` is the process-wide context (the engine's all-records view).
    assert None in world.runtimes and world.runtimes[None].calls[0][0] == "audit_page"


# ── ADM-002: tenants ───────────────────────────────────────────────────────


async def test_tenants_lists_live_and_configured_without_secrets(
    tmp_path: Path,
) -> None:
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "tenant.yaml").write_text("openrouter_enabled: true\n")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "tenant.yaml").write_text("openrouter_enabled: true\n")
    (tmp_path / "notes").mkdir()  # no tenant.yaml: not a tenant
    world = World(tmp_path, host_max_tenants=7)
    await world.registry.get("acme")
    async with world.client() as c:
        resp = await c.get("/v1/admin/tenants", headers=GLOBAL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_tenants"] == 7
    rows = {t["org_id"]: t for t in body["tenants"]}
    assert set(rows) == {"acme", "beta"}
    acme, beta = rows["acme"], rows["beta"]
    assert acme["live"] is True and acme["config_source"] == "vault"
    assert acme["gateways"] == ["openrouter", "opencode_go"]
    assert acme["a2a_protocol_version"] == "1.0"
    assert acme["created_at"] and acme["last_used_at"]
    assert beta["live"] is False and beta["created_at"] is None
    assert beta["gateways"] == [] and beta["config_source"] == "vault"
    assert "secret" not in resp.text.lower() and "api_key" not in resp.text


async def test_tenants_process_tenant_and_own_org_filter() -> None:
    world = World()
    await world.registry.get("acme")
    await world.registry.get("beta")
    async with world.client() as c:
        own = await c.get("/v1/admin/tenants", headers=ADMIN)
    rows = own.json()["tenants"]
    assert [r["org_id"] for r in rows] == ["acme"]
    assert rows[0]["config_source"] == "process"


# ── ADM-003: audit ─────────────────────────────────────────────────────────


async def test_audit_page_and_chain() -> None:
    world = World()
    async with world.client() as c:
        resp = await c.get(
            "/v1/admin/audit",
            params={"page": 2, "page_size": 10, "since": "2026-10-01T00:00:00Z"},
            headers=ADMIN,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert (body["page"], body["page_size"], body["total"]) == (2, 10, 1)
    assert body["entries"] == [
        {
            "seq": 7,
            "ts": "2026-10-04T12:00:00Z",
            "event": "tool_call",
            "actor": "agent",
            "org_id": "acme",
            "hash": "h7",
            "prev_hash": "h6",
            "fields": {"tool": "search"},
        }
    ]
    assert body["chain"] == {"status": "valid", "checked": 7, "first_broken_seq": None}
    name, kw = world.runtimes["acme"].calls[0]
    assert name == "audit_page"
    assert kw == {
        "page": 2,
        "page_size": 10,
        "since": datetime(2026, 10, 1, tzinfo=UTC),
    }


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page_size": 0}, {"page_size": 201}],
)
async def test_audit_paging_bounds_are_422(params: dict[str, int]) -> None:
    world = World()
    async with world.client() as c:
        resp = await c.get("/v1/admin/audit", params=params, headers=ADMIN)
    assert resp.status_code == 422


async def test_audit_defaults() -> None:
    world = World()
    async with world.client() as c:
        await c.get("/v1/admin/audit", headers=ADMIN)
    _, kw = world.runtimes["acme"].calls[0]
    assert kw == {"page": 1, "page_size": 50, "since": None}


# ── ADM-004: decisions ─────────────────────────────────────────────────────


async def test_decisions_report_shape_and_null_ratio() -> None:
    world = World()
    async with world.client() as c:
        resp = await c.get(
            "/v1/admin/decisions",
            params={"since": "2026-10-01T12:00:00Z", "until": "2026-10-04T12:00:00Z"},
            headers=ADMIN,
        )
    body = resp.json()
    assert body["org_id"] == "acme" and body["enabled"] is True
    assert body["window"] == {
        "since": "2026-10-01T12:00:00Z",
        "until": "2026-10-04T12:00:00Z",
    }
    row = body["integrations"][0]
    assert row["agreement_ratio"] is None and row["samples"] == 0
    # Clamped window comes from the engine, not the request.
    assert row["window"]["since"] == "2026-10-02T12:00:00Z"
    assert body["as_of"].endswith("Z")
    _, kw = world.runtimes["acme"].calls[0]
    assert kw == {"since": T0, "until": T1}


async def test_decisions_default_window_is_seven_days() -> None:
    world = World()
    async with world.client() as c:
        body = (await c.get("/v1/admin/decisions", headers=ADMIN)).json()
    since = datetime.fromisoformat(body["window"]["since"])
    until = datetime.fromisoformat(body["window"]["until"])
    assert until - since == timedelta(days=7)


async def test_decisions_disabled() -> None:
    world = World(enabled=False)
    async with world.client() as c:
        body = (await c.get("/v1/admin/decisions", headers=ADMIN)).json()
    assert body["enabled"] is False and body["integrations"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"since": "2026-10-05T00:00:00Z", "until": "2026-10-04T00:00:00Z"},
        {"since": "yesterday"},
        {"until": "2026-10-04T00:00:00"},
    ],
)
async def test_bad_window_is_400(params: dict[str, str]) -> None:
    world = World()
    async with world.client() as c:
        resp = await c.get("/v1/admin/decisions", params=params, headers=ADMIN)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_window"


# ── ADM-005: routing ───────────────────────────────────────────────────────


async def test_routing_report_field_mapping() -> None:
    world = World()
    async with world.client() as c:
        body = (await c.get("/v1/admin/routing", headers=ADMIN)).json()
    assert body["enabled"] is True and body["mode"] == "enforce"
    row = body["policies"][0]
    assert row == {
        "policy": "composite",
        "decisions": 5120,
        "decisions_by_tier": {"small": 3900, "medium": 1000, "frontier": 220},
        "escalations": 140,
        "escalations_by_reason": {"inadequate": 90, "structured_invalid": 50},
        "plan_cache_hits": 610,
        "estimated_savings_usd": 12.43,
        "window": {"since": "2026-10-01T12:00:00Z", "until": "2026-10-04T12:00:00Z"},
    }


async def test_routing_disabled() -> None:
    world = World(enabled=False)
    async with world.client() as c:
        body = (await c.get("/v1/admin/routing", headers=ADMIN)).json()
    assert body["enabled"] is False and body["policies"] == []
