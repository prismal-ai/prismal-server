"""Engine-surface gate for host v0.2 (RH2-00-01..04).

Executable record of the Phase-0 verification: the installed ``prismal-ai``
must expose the **public host read surface** (``SPEC-HRS-SUR-001``,
``prismal/specs/composition-root/DELTA-2026-10-host-read-surface.md`` §3/§4)
with the signatures the v0.2 host codes against. If the engine drops or
reshapes any of these, this test fails *before* a dependent RH2 task silently
degrades — the host never grows a fallback for a missing engine read
(``TASKS.md`` RH2-00-03: missing item ⇒ dependent task ``BLOCKED``).

These are introspection-only checks: nothing is built, no provider I/O.
"""

from __future__ import annotations

import inspect
from importlib.metadata import version

import pytest

# ── RH2-00-01: pin ──────────────────────────────────────────────────────────


def _version_tuple(raw: str) -> tuple[int, ...]:
    return tuple(int(p) for p in raw.split(".")[:3] if p.isdigit())


def test_engine_version_satisfies_pin() -> None:
    """SPEC-RH2-NFR-001: ``prismal-ai>=3.17,<4``."""
    installed = _version_tuple(version("prismal-ai"))
    assert (3, 17) <= installed < (4,), installed


# ── RH2-00-03: HRS §3 import rows ───────────────────────────────────────────

HRS_PUBLIC_SURFACE: dict[str, tuple[str, ...]] = {
    "prismal.composition": ("build_runtime", "build_test_runtime"),
    "prismal.agents.graph": ("get_async_compiled_graph",),
    "prismal.a2a": ("build_agent_card", "get_settings", "DEFAULT_CAPABILITY_MAP"),
    "prismal.events.agui": (
        "AgUiContext",
        "start_run",
        "to_agui_events",
        "finish_run",
        "error_run",
        "AGUI_PROTOCOL_VERSION",
    ),
    "prismal.monitoring": ("get_run_summary",),
    "prismal.composition.host_surface": (
        "BudgetAmounts",
        "BudgetStatusView",
        "QuotaWindowView",
        "AuditEntryView",
        "AuditPageView",
        "AuditChainStatus",
        "ReportWindow",
        "DecisionIntegrationStats",
        "DecisionsReportView",
        "RoutingPolicyStats",
        "RoutingReportView",
        "A2ARouteSpec",
    ),
}


@pytest.mark.parametrize(
    ("module", "symbol"),
    [(m, s) for m, syms in HRS_PUBLIC_SURFACE.items() for s in syms],
)
def test_hrs_public_symbol_exists(module: str, symbol: str) -> None:
    mod = __import__(module, fromlist=[symbol])
    assert hasattr(mod, symbol), f"{module}.{symbol} missing from installed engine"


def _kwonly(func: object) -> set[str]:
    params = inspect.signature(func).parameters.values()  # type: ignore[arg-type]
    return {p.name for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY}


def test_build_runtime_accepts_config_source() -> None:
    """SPEC-HRS-CMP-001 — needed by RH2-01 (tenant config)."""
    from prismal.composition import build_runtime

    assert {"org_id", "config_source"} <= _kwonly(build_runtime)


# ── RH2-00-03: HRS §4 RuntimeContext read methods ───────────────────────────

RUNTIME_READ_METHODS: dict[str, set[str]] = {
    "budget_status": {"thread_id"},  # RH2-02-05
    "quota_windows": set(),  # RH2-02-05 (async)
    "audit_page": {"page", "page_size", "since"},  # RH2-06-04
    "verify_audit_chain": {"max_records"},  # RH2-06-04
    "decisions_report": {"since", "until"},  # RH2-06-06
    "routing_report": {"since", "until"},  # RH2-06-07
    "a2a_routes": {"protocol_version"},  # RH2-04-02
}


@pytest.mark.parametrize(("method", "kwargs"), sorted(RUNTIME_READ_METHODS.items()))
def test_runtime_context_read_method(method: str, kwargs: set[str]) -> None:
    from prismal.composition.runtime import RuntimeContext

    fn = getattr(RuntimeContext, method, None)
    assert fn is not None, f"RuntimeContext.{method} missing"
    assert kwargs <= _kwonly(fn), f"RuntimeContext.{method} lacks {kwargs}"


def test_quota_windows_is_async() -> None:
    from prismal.composition.runtime import RuntimeContext

    assert inspect.iscoroutinefunction(RuntimeContext.quota_windows)


def test_a2a_handler_accepts_headers_and_org_id() -> None:
    """SPEC-RH2-A2A-001 — RH2-04-01 forwards both."""
    from prismal.a2a.server import A2AServerHandler

    for name in ("handle_rpc", "stream_rpc"):
        assert {"headers", "org_id"} <= _kwonly(getattr(A2AServerHandler, name))


def test_agent_card_is_versioned() -> None:
    """SPEC-RH2-A2A-002 — the card builder takes ``protocol_version``."""
    from prismal.a2a import build_agent_card

    assert "protocol_version" in _kwonly(build_agent_card)


# ── RH2-00-02 / 00-04: usage + AG-UI ────────────────────────────────────────


def test_run_summary_exposes_usage_wire() -> None:
    from prismal.monitoring.observability_types import RunSummary

    assert callable(getattr(RunSummary, "to_usage_wire", None))


def test_get_run_summary_signature() -> None:
    from prismal.monitoring import get_run_summary

    params = inspect.signature(get_run_summary).parameters
    assert "session_id" in params and "turn" in params


def test_agui_protocol_version_is_1_0() -> None:
    """ADR-PRT-001 §2.3 pins AG-UI ``"1.0"``."""
    from prismal.events.agui import AGUI_PROTOCOL_VERSION

    assert AGUI_PROTOCOL_VERSION == "1.0"


def test_agui_lifecycle_signatures() -> None:
    from prismal.events.agui import error_run, finish_run, start_run

    assert {"initial_state", "input"} <= _kwonly(start_run)
    assert {"summary", "result"} <= _kwonly(finish_run)
    assert {"code", "message", "summary"} <= _kwonly(error_run)
