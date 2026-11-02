"""Field-wise sums over usage records (RH2-02-04, SPEC-RH2-USE-003).

No pricing: ``cost_usd`` is summed as reported by the engine; ``estimated`` is
OR-ed; ``cost_source`` is the common value or ``mixed``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from prismal_server.usage.models import CostSource, UsageWire

_INT_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "calls",
)


def total(
    usages: Iterable[UsageWire],
    *,
    served_model: str | None = None,
    gateway: str | None = None,
) -> UsageWire:
    """Sum ``usages``; ``served_model``/``gateway`` label the result (totals: None)."""
    items = list(usages)
    sums = {f: sum(getattr(u, f) for u in items) for f in _INT_FIELDS}
    sources = {u.cost_source for u in items}
    source: CostSource = sources.pop() if len(sources) == 1 else "mixed"
    if not items:
        source = "none"
    return UsageWire(
        **sums,
        cost_usd=round(sum(u.cost_usd for u in items), 6),
        cost_source=source,
        estimated=any(u.estimated for u in items),
        served_model=served_model,
        gateway=gateway,
    )


def by_model(usages: Iterable[UsageWire]) -> list[UsageWire]:
    """One total per ``(served_model, gateway)``, most expensive first."""
    groups: dict[tuple[str | None, str | None], list[UsageWire]] = defaultdict(list)
    for u in usages:
        groups[(u.served_model, u.gateway)].append(u)
    rows = [total(g, served_model=m, gateway=gw) for (m, gw), g in groups.items()]
    return sorted(
        rows, key=lambda r: (-r.cost_usd, r.served_model or "", r.gateway or "")
    )


def cache_hit_ratio(totals: UsageWire) -> float:
    """``cache_read_tokens / prompt_tokens`` to 4 decimals (0.0 when no prompt)."""
    if totals.prompt_tokens == 0:
        return 0.0
    return round(totals.cache_read_tokens / totals.prompt_tokens, 4)
