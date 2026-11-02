"""In-process counters (ARCHITECTURE §8).

A minimal, dependency-free counter registry for the host's operational
counters (``prismal_server.usage_missing_total`` …). Exporting them (OTel /
Prometheus) is a Phase RH2-7 concern; until then they are logged-at-source and
readable via :func:`snapshot` (tests, admin).
"""

from __future__ import annotations

from collections import Counter

_PREFIX = "prismal_server."
_counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()


def increment(name: str, value: int = 1, **labels: str) -> None:
    """Add ``value`` to counter ``prismal_server.<name>{labels}``."""
    _counters[(_PREFIX + name, tuple(sorted(labels.items())))] += value


def get(name: str, **labels: str) -> int:
    """Current value of one counter series (0 if never incremented)."""
    return _counters[(_PREFIX + name, tuple(sorted(labels.items())))]


def snapshot() -> dict[str, int]:
    """All series as ``{"name{k=v,…}": value}``."""
    out: dict[str, int] = {}
    for (name, labels), value in _counters.items():
        suffix = ",".join(f"{k}={v}" for k, v in labels)
        out[f"{name}{{{suffix}}}" if suffix else name] = value
    return out
