"""Per-turn usage: wire models, persistence and whole-window sums (RH2-02).

The engine prices and meters (``RunSummary.to_usage_wire()``); the host only
validates the 12-key wire object, persists one record per finished turn and
sums records field-wise. It never prices or classifies budgets.
"""
