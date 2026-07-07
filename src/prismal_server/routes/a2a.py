"""Inbound A2A routes.

Filled in Phase 3 (RHB-03-*): ``GET /.well-known/agent-card.json`` via
``build_agent_card(...)`` and ``POST /a2a`` delegating JSON-RPC / SSE to
``RuntimeContext.a2a_handler``. The host owns transport + auth only; the handler
already L1-sanitizes and audits (SPEC-RHB-A2A-*).

Seam: inbound A2A — ``prismal.a2a.server.A2AServerHandler`` /
``prismal.a2a.card.build_agent_card``.
"""
