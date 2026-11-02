"""Import-guard: the host imports the engine only through its sanctioned surface.

SPEC-RH2-NFR-002 (RH2-07-01): the allowlist is **exactly** the engine's public host
read surface (``SPEC-HRS-SUR-001``,
``prismal/specs/composition-root/DELTA-2026-10-host-read-surface.md`` §3), plus a
short, documented list of v0.1 host entry points that HRS §3 forbids silently
removing (``HOST_EXCEPTIONS``). Everything else the host reads is a
``RuntimeContext`` method (HRS §4) and needs no import.

Every rule is **symbol-restricted and exact-module**: only ``from <module> import
<name>`` with a listed name passes. A bare ``import prismal...`` (exposes the whole
module), a sub-module of a listed module (``prismal.composition.runtime``), a
parent package, or an unlisted name is a violation. The scan is static (AST, no
runtime import), so it flags a violation even on a code path that never executes.

SPEC-RHB-NFR-001 / the ``CLAUDE.md`` one-hard-rule: ``prismal-server`` orchestrates
the engine and reimplements none of its logic.
"""

from __future__ import annotations

import ast
from pathlib import Path

# HRS §3, row by row. Changing a row is a coordinated minor in engine + host.
HRS_SURFACE: dict[str, frozenset[str]] = {
    # 1 — composition
    "prismal.composition": frozenset({"build_runtime", "build_test_runtime"}),
    # 2 — execution
    "prismal.agents.graph": frozenset({"get_async_compiled_graph"}),
    # 3 — A2A card inputs (re-exports since 3.10.2)
    "prismal.a2a": frozenset(
        {"build_agent_card", "get_settings", "DEFAULT_CAPABILITY_MAP"}
    ),
    # 4 — AG-UI translator (PRT5)
    "prismal.events.agui": frozenset(
        {
            "AgUiContext",
            "start_run",
            "to_agui_events",
            "finish_run",
            "error_run",
            "AGUI_PROTOCOL_VERSION",
        }
    ),
    # 5 — run summary / usage wire (PRT5)
    "prismal.monitoring": frozenset({"get_run_summary"}),
    # 6 — HRS §4 view types, for annotations / isinstance only (never the
    # module-level report functions: those are reached via RuntimeContext).
    "prismal.composition.host_surface": frozenset(
        {
            "A2ARouteSpec",
            "AuditChainStatus",
            "AuditEntryView",
            "AuditPageView",
            "BudgetAmounts",
            "BudgetStatusView",
            "DecisionIntegrationStats",
            "DecisionsReportView",
            "QuotaWindowView",
            "ReportWindow",
            "RoutingPolicyStats",
            "RoutingReportView",
        }
    ),
}

# v0.1 / RH2-4 host entry points outside the HRS §3 rows. HRS §3 forbids silently
# removing an existing entry point, so they stay — each one named and justified.
# Follow-up (engine): promote them to HRS rows in a coordinated minor.
HOST_EXCEPTIONS: dict[str, frozenset[str]] = {
    # Inbound A2A seam (CLAUDE.md): the handler, its auth context, and the
    # engine-declared route paths (SPEC-HRS-A2A-001 companion).
    "prismal.a2a": frozenset({"A2AServerHandler", "AuthContext", "a2a_route_paths"}),
    # RH2-4: A2A wire constants, so the host dispatches on the engine's own
    # method / header names instead of re-declaring protocol strings.
    "prismal.a2a.v1.constants": frozenset(
        {"METHOD_SEND_STREAMING_MESSAGE", "LEGACY_METHOD_STREAM", "VERSION_HEADER"}
    ),
    # Identity seam (CLAUDE.md, v0.1): auth backends produce engine identities.
    "prismal.identity": frozenset(
        {"IdentityPort", "OidcIdentityProvider", "AgentIdentity", "DID"}
    ),
}


def _merge(*tables: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    merged: dict[str, frozenset[str]] = {}
    for table in tables:
        for module, names in table.items():
            merged[module] = merged.get(module, frozenset()) | names
    return merged


ALLOWED: dict[str, frozenset[str]] = _merge(HRS_SURFACE, HOST_EXCEPTIONS)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "prismal_server"


def _is_engine_module(module: str | None) -> bool:
    return module is not None and (module == "prismal" or module.startswith("prismal."))


def _violations(tree: ast.AST) -> list[str]:
    """Every engine import in ``tree`` that is not an allowlisted ``from`` import."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # Bare `import prismal.x` exposes the whole module: never allowed.
            found.extend(a.name for a in node.names if _is_engine_module(a.name))
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has module=None, level>0 (relative — never engine).
            if node.level != 0 or not _is_engine_module(node.module):
                continue
            module = node.module or ""
            allowed = ALLOWED.get(module)
            if allowed is None:
                found.append(module)
                continue
            found.extend(
                f"{module}.{a.name}" for a in node.names if a.name not in allowed
            )
    return found


def _source_files() -> list[Path]:
    return sorted(p for p in _SRC_ROOT.rglob("*.py"))


def test_source_tree_exists() -> None:
    assert _source_files(), f"no host source found under {_SRC_ROOT}"


def test_only_sanctioned_engine_surface_is_imported() -> None:
    """No module under src/ imports from `prismal` outside the allowlist."""
    violations: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(_SRC_ROOT.parent.parent)
        violations.extend(f"{rel}: imports `{v}`" for v in _violations(tree))
    assert not violations, "engine boundary violated:\n  " + "\n  ".join(violations)


def test_guard_catches_a_violation() -> None:
    """The scanner is not vacuous: it rejects stray engine imports."""
    bad = ast.parse("from prismal.rag import Retriever\nimport prismal.security\n")
    assert _violations(bad) == ["prismal.rag", "prismal.security"]


def test_guard_accepts_every_hrs_row() -> None:
    """Each HRS §3 row (1–6), imported by name from its exact module, passes."""
    source = "".join(
        f"from {module} import {', '.join(sorted(names))}\n"
        for module, names in HRS_SURFACE.items()
    )
    assert _violations(ast.parse(source)) == []


def test_guard_accepts_documented_host_exceptions() -> None:
    source = "".join(
        f"from {module} import {', '.join(sorted(names))}\n"
        for module, names in HOST_EXCEPTIONS.items()
    )
    assert _violations(ast.parse(source)) == []


def test_guard_rejects_sub_modules_of_a_row() -> None:
    """Rows are exact modules: `prismal.composition.runtime` is not row 1."""
    bad = ast.parse(
        "from prismal.composition.runtime import build_runtime\n"
        "from prismal.a2a.server import A2AServerHandler\n"
        "from prismal.monitoring.run_summary import get_run_summary\n"
        "from prismal.events import agui\n"
    )
    assert _violations(bad) == [
        "prismal.composition.runtime",
        "prismal.a2a.server",
        "prismal.monitoring.run_summary",
        "prismal.events",  # parent package is not a row
    ]


def test_guard_rejects_names_outside_a_row() -> None:
    bad = ast.parse(
        "from prismal.monitoring import ObservabilityPort\n"
        "from prismal.composition import RuntimeContext\n"
        "from prismal.composition.host_surface import audit_page\n"
        "from prismal.a2a import A2AClient\n"
    )
    assert _violations(bad) == [
        "prismal.monitoring.ObservabilityPort",
        "prismal.composition.RuntimeContext",
        "prismal.composition.host_surface.audit_page",  # report fn ≠ view type
        "prismal.a2a.A2AClient",
    ]


def test_guard_rejects_bare_imports_of_a_row() -> None:
    """A bare import exposes the whole module, so even a row module fails."""
    bad = ast.parse("import prismal.events.agui\nimport prismal.composition\n")
    assert _violations(bad) == ["prismal.events.agui", "prismal.composition"]


def test_host_surface_rows_name_real_view_types() -> None:
    """Row 6 lists only names the released engine exports as view types."""
    from prismal.composition import host_surface

    exported = set(getattr(host_surface, "__all__", ()))
    assert HRS_SURFACE["prismal.composition.host_surface"] <= exported
