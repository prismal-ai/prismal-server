"""Thread / chat streaming routes.

Filled in Phase 2 (RHB-02-02/03): ``POST /threads`` mints an opaque
``thread_id``; ``POST /threads/{id}/messages`` resolves the tenant runtime,
obtains the graph via ``get_async_compiled_graph(...)``, and streams
``graph.astream(...)`` as ``text/event-stream`` (SPEC-RHB-THR-*, SPEC-RHB-CHT-*).

Seam: execution — ``prismal.agents.graph.get_async_compiled_graph``.
"""
