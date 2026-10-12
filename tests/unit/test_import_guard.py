"""Import-guard: the host imports the engine only through its sanctioned surface.

RHB-05-01 (v0.1) established the four seams below; RH2-00-03 (v0.2) extends the
allowlist with the engine-owned **public host read surface** (``SPEC-HRS-SUR-001``,
``prismal/specs/composition-root/DELTA-2026-10-host-read-surface.md`` §3).

SPEC-RHB-NFR-001 / the ``CLAUDE.md`` one-hard-rule: ``prismal-server`` orchestrates
the engine and reimplements none of its logic. The only sanctioned coupling is the
**four entry points**:

| Seam | Engine module(s) |
|---|---|
| Composition | ``prismal.composition`` (``build_runtime`` / ``build_test_runtime``) |
| Execution | ``prismal.agents.graph`` (``get_async_compiled_graph``) |
| Inbound A2A | ``prismal.a2a`` (``A2AServerHandler`` / ``build_agent_card`` / …) |
| Identity | ``prismal.identity`` (``IdentityPort`` / ``OidcIdentityProvider``) |

v0.2 adds two **symbol-restricted** modules (HRS §3 rows 4–5): only the listed
names may be imported from them, via ``from <module> import <name>`` (a bare
``import <module>`` would expose everything and is rejected):

* ``prismal.events.agui`` — ``AgUiContext``, ``start_run``, ``to_agui_events``,
  ``finish_run``, ``error_run``, ``AGUI_PROTOCOL_VERSION``
* ``prismal.monitoring`` — ``get_run_summary``

HRS row 6 (``prismal.composition.host_surface`` view types) is already covered by
the ``prismal.composition`` seam. ``prismal.identity`` is not an HRS row but stays
allowed: HRS §3 forbids silently removing an existing host entry point.

Any other ``prismal.*`` import (``prismal.rag``, ``prismal.security``,
``prismal.mcp``, ``prismal.core.*``, …) is a boundary violation — the logic it
would pull in belongs to the engine. This test AST-scans every module under
``src/prismal_server`` and fails on the first stray import. It is intentionally
static (no runtime import), so it flags a violation even on a code path that never
executes.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Module prefixes the host is allowed to import from `prismal`. A module is
# allowed iff it equals one of these or is a dotted sub-module of one.
ALLOWED_SEAM_PREFIXES: tuple[str, ...] = (
    "prismal.composition",  # build_runtime / build_test_runtime
    "prismal.agents.graph",  # get_async_compiled_graph
    "prismal.a2a",  # A2AServerHandler / build_agent_card / AuthContext / re-exports
    "prismal.identity",  # IdentityPort / OidcIdentityProvider / AgentIdentity / DID
)

# Modules from which only specific names may be imported (exact module match;
# sub-modules are NOT implied). SPEC-RH2-NFR-002 / HRS §3 rows 4–5.
ALLOWED_SYMBOLS: dict[str, frozenset[str]] = {
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
    "prismal.monitoring": frozenset({"get_run_summary"}),
}

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "prismal_server"


def _is_engine_module(module: str | None) -> bool:
    return module is not None and (module == "prismal" or module.startswith("prismal."))


def _is_allowed(module: str) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in ALLOWED_SEAM_PREFIXES
    )


def _symbol_violations(tree: ast.AST) -> list[str]:
    """Imports of symbol-restricted modules that reach beyond their allowlist."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name in ALLOWED_SYMBOLS)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module in ALLOWED_SYMBOLS
        ):
            allowed = ALLOWED_SYMBOLS[node.module]
            found.extend(
                f"{node.module}.{a.name}" for a in node.names if a.name not in allowed
            )
    return found


def _engine_imports(tree: ast.AST) -> list[str]:
    """Every ``prismal`` module referenced by an import in ``tree``."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if _is_engine_module(a.name))
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has module=None, level>0 (relative — never engine).
            if node.level == 0 and _is_engine_module(node.module):
                found.append(node.module)  # type: ignore[arg-type]
    return found


def _source_files() -> list[Path]:
    return sorted(p for p in _SRC_ROOT.rglob("*.py"))


def test_source_tree_exists() -> None:
    files = _source_files()
    assert files, f"no host source found under {_SRC_ROOT}"


def _violations(tree: ast.AST) -> list[str]:
    stray = [
        m
        for m in _engine_imports(tree)
        if not _is_allowed(m) and m not in ALLOWED_SYMBOLS
    ]
    return stray + _symbol_violations(tree)


def test_only_sanctioned_engine_surface_is_imported() -> None:
    """No module under src/ imports from `prismal` outside the allowlist."""
    violations: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(_SRC_ROOT.parent.parent)
        violations.extend(f"{rel}: imports `{v}`" for v in _violations(tree))
    assert not violations, "engine boundary violated:\n  " + "\n  ".join(violations)


def test_guard_catches_a_violation() -> None:
    """The scanner is not vacuous: it rejects a stray engine import."""
    bad = ast.parse("from prismal.rag import Retriever\nimport prismal.security\n")
    modules = _engine_imports(bad)
    assert modules == ["prismal.rag", "prismal.security"]
    assert not any(_is_allowed(m) for m in modules)


def test_guard_accepts_each_seam() -> None:
    """Each sanctioned seam import is recognised as allowed."""
    good = ast.parse(
        "from prismal.composition.runtime import build_runtime\n"
        "from prismal.agents.graph import get_async_compiled_graph\n"
        "from prismal.a2a import build_agent_card\n"
        "from prismal.a2a.server import A2AServerHandler\n"
        "from prismal.identity import OidcIdentityProvider\n"
    )
    modules = _engine_imports(good)
    assert modules and all(_is_allowed(m) for m in modules)


def test_guard_accepts_hrs_read_surface() -> None:
    """HRS §3 rows 4–6 are recognised as allowed (RH2-00-03)."""
    good = ast.parse(
        "from prismal.events.agui import AgUiContext, start_run, to_agui_events\n"
        "from prismal.events.agui import finish_run, error_run, AGUI_PROTOCOL_VERSION\n"
        "from prismal.monitoring import get_run_summary\n"
        "from prismal.composition.host_surface import BudgetStatusView\n"
    )
    assert _violations(good) == []


def test_guard_rejects_symbols_outside_hrs_rows() -> None:
    """Symbol-restricted modules reject other names, bare imports and sub-modules."""
    bad = ast.parse(
        "from prismal.monitoring import ObservabilityPort\n"
        "import prismal.events.agui\n"
        "from prismal.monitoring.run_summary import get_run_summary\n"
        "from prismal.events import agui\n"
    )
    assert set(_violations(bad)) == {
        "prismal.monitoring.ObservabilityPort",  # name outside the row
        "prismal.events.agui",  # bare import exposes the whole module
        "prismal.monitoring.run_summary",  # sub-module is not implied
        "prismal.events",  # parent package is not a row
    }
