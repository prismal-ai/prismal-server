"""Error model and engine-exception → HTTP mapping.

Filled in Phase 1 (RHB-01-05): maps engine exceptions to HTTP statuses with a
JSON ``{"error": {"code", "message"}}`` body and registers the exception
handlers. ``500`` bodies stay opaque; detail goes only to logs
(SPEC-RHB-ERR-001/002/003).
"""
