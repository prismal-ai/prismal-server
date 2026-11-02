"""Per-turn usage from the engine run summary (RH2-02-02, SPEC-RH2-USG-001).

``get_run_summary(thread_id)`` returns the session's *last* turn. A turn that
made no LLM call would otherwise re-report the previous turn, so
:class:`TurnUsageProbe` snapshots the summary's ``run_id`` before the turn and
reports usage only when a new run appeared.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from prismal.monitoring import get_run_summary
from pydantic import ValidationError

from prismal_server.usage.models import UsageWire

logger = logging.getLogger(__name__)

# ``(thread_id) -> RunSummary | None``; injectable for tests and fake mode.
RunSummaryLookup = Callable[[str], Any]


def default_run_summary(thread_id: str) -> Any:
    return get_run_summary(thread_id)


class TurnUsageProbe:
    """Snapshot before a turn; :meth:`usage` after it (never raises)."""

    def __init__(self, thread_id: str, lookup: RunSummaryLookup) -> None:
        self._thread_id = thread_id
        self._lookup = lookup
        self._before = self._run_id()

    def _summary(self) -> Any:
        try:
            return self._lookup(self._thread_id)
        except Exception:
            logger.warning("run summary lookup failed", exc_info=True)
            return None

    def _run_id(self) -> str | None:
        summary = self._summary()
        return None if summary is None else str(getattr(summary, "run_id", ""))

    def summary(self) -> Any:
        """The engine ``RunSummary`` of *this* turn, or ``None`` (stale or absent)."""
        summary = self._summary()
        if summary is None or str(getattr(summary, "run_id", "")) == self._before:
            return None
        return summary

    def usage(self) -> UsageWire | None:
        summary = self.summary()
        if summary is None:
            return None
        try:
            return UsageWire.model_validate(summary.to_usage_wire())
        except (ValidationError, AttributeError, TypeError):
            logger.warning(
                "engine usage wire rejected for thread_id=%s", self._thread_id
            )
            return None
