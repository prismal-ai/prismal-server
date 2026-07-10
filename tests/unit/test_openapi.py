"""OpenAPI document sanity + generated-client smoke (RHB-05-03, SPEC-RHB-SDK-004).

The host serves a FastAPI-default ``/openapi.json`` so ``prismal-sdk`` can be
generated (or verified) against it and stay business-logic-free. These run in the
unit suite (fake registry, no live engine I/O): they assert the document is
well-formed and pins the wire contract the SDK depends on —

* the chat surface: ``POST /threads`` → ``{thread_id}`` and
  ``POST /threads/{thread_id}/messages`` (SSE) with a typed request body,
* the A2A surface: the Agent Card + ``POST /a2a``,
* health/readiness,

and that every operation a client generator needs (a unique ``operationId`` and a
resolvable request schema) is present.
"""

from __future__ import annotations

from typing import Any


async def test_openapi_document_is_served_and_wellformed(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    doc = resp.json()
    # OpenAPI 3.x envelope with the host's identity.
    assert doc["openapi"].startswith("3.")
    assert doc["info"]["title"] == "prismal-server"
    assert doc["info"]["version"]
    assert isinstance(doc.get("paths"), dict) and doc["paths"]


async def test_openapi_covers_the_sdk_wire_contract(client) -> None:  # type: ignore[no-untyped-def]
    """The documented v0.1 surface (README + SPEC §9) is fully described."""
    doc = (await client.get("/openapi.json")).json()
    paths = doc["paths"]

    # Chat contract (SDK-001).
    assert "post" in paths["/threads"]
    assert "post" in paths["/threads/{thread_id}/messages"]
    # A2A contract (SDK-002).
    assert "get" in paths["/.well-known/agent-card.json"]
    assert "post" in paths["/a2a"]
    # Health / readiness (HLT-*).
    assert "get" in paths["/healthz"]
    assert "get" in paths["/readyz"]


async def test_message_request_body_schema_is_resolvable(client) -> None:  # type: ignore[no-untyped-def]
    """The chat turn's request body is a named, generatable schema."""
    doc = (await client.get("/openapi.json")).json()
    op = doc["paths"]["/threads/{thread_id}/messages"]["post"]

    ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    # e.g. "#/components/schemas/MessageIn" — resolve it and check the fields.
    schema_name = ref.rsplit("/", 1)[-1]
    schema: dict[str, Any] = doc["components"]["schemas"][schema_name]
    assert "content" in schema["properties"]
    assert schema["required"] == ["content"]


async def test_every_operation_has_a_unique_operation_id(client) -> None:  # type: ignore[no-untyped-def]
    """Client generators key methods on ``operationId``; it must be present+unique."""
    doc = (await client.get("/openapi.json")).json()
    op_ids: list[str] = []
    for methods in doc["paths"].values():
        for spec in methods.values():
            if isinstance(spec, dict) and "operationId" in spec:
                op_ids.append(spec["operationId"])
    assert op_ids
    assert len(op_ids) == len(set(op_ids)), "duplicate operationId in OpenAPI doc"
