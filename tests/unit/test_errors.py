"""Tests for the error model + engine-exception mapping (RHB-01-05, ERR-001/2/3).

The host must NOT import engine exception types (that would be a fifth seam), so
mapping is by class name walked over the MRO. These fakes mimic the engine's
class names to exercise that mapping.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from prismal_server.errors import map_exception, register_exception_handlers


# --- engine-shaped fakes (names mirror prismal.core.exceptions) ---------------
class PrismalError(Exception): ...


class RuntimeCompositionError(PrismalError): ...


class IdentityError(PrismalError): ...


class PolicyDenied(IdentityError): ...


class SecurityError(PrismalError): ...


class PermissionDeniedError(SecurityError): ...


class BudgetExceeded(PrismalError): ...


class A2AAgentUnavailable(PrismalError): ...


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (RuntimeCompositionError("no vector store"), 503, "runtime_composition_error"),
        (PolicyDenied("nope"), 403, "policy_denied"),
        (PermissionDeniedError("nope"), 403, "permission_denied"),
        (BudgetExceeded("too much"), 429, "budget_exceeded"),
        (A2AAgentUnavailable("down"), 502, "a2a_agent_unavailable"),
    ],
)
def test_maps_known_engine_exceptions(exc: Exception, status: int, code: str) -> None:
    mapped = map_exception(exc)
    assert mapped.status_code == status
    assert mapped.code == code
    # Non-500 errors may carry the engine message.
    assert mapped.message == str(exc)


def test_unknown_prismal_error_is_opaque_500() -> None:
    mapped = map_exception(PrismalError("secret internal detail"))
    assert mapped.status_code == 500
    assert mapped.code == "internal_error"
    assert "secret internal detail" not in mapped.message


def test_unknown_non_engine_exception_is_opaque_500() -> None:
    mapped = map_exception(ValueError("boom with sensitive path /etc/passwd"))
    assert mapped.status_code == 500
    assert "sensitive" not in mapped.message
    assert "passwd" not in mapped.message


async def test_handler_renders_json_error_body() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeCompositionError("no vector store")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/boom")
    assert resp.status_code == 503
    assert resp.json() == {
        "error": {"code": "runtime_composition_error", "message": "no vector store"}
    }


async def test_handler_opaque_500_does_not_leak_detail() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/kaboom")
    async def kaboom() -> None:
        raise ValueError("stack trace with /secret/path")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/kaboom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret" not in body["error"]["message"]
