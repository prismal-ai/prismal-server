"""Error model and engine-exception → HTTP mapping (RHB-01-05).

Engine exceptions are mapped to HTTP statuses **by class name**, walked over the
exception's MRO — the host deliberately does NOT import ``prismal.core.exceptions``
(that would be a fifth engine seam, forbidden by SPEC-RHB-NFR-001). All error
responses share the body ``{"error": {"code", "message"}}`` (SPEC-RHB-ERR-001).
``500`` responses stay opaque; the real detail goes only to logs
(SPEC-RHB-ERR-002/003).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

_OPAQUE_500_MESSAGE = "Internal Server Error"

# Engine exception class name → (HTTP status, stable error code). See
# ARCHITECTURE.md §6. Matched against every name in the exception's MRO, so
# subclasses inherit their base's mapping unless they have a more specific entry.
_NAME_MAP: dict[str, tuple[int, str]] = {
    "RuntimeCompositionError": (503, "runtime_composition_error"),
    "PolicyDenied": (403, "policy_denied"),
    "PermissionDeniedError": (403, "permission_denied"),
    "ToolPolicyDenied": (403, "policy_denied"),
    # SPEC-RH2 §11 (was 429 in v0.1): 402 before the first token.
    "BudgetExceeded": (402, "budget_exceeded"),
    "A2AAgentUnavailable": (502, "a2a_agent_unavailable"),
    # Host-side (tenancy/config_source.py); its str() is already opaque.
    "TenantConfigInvalid": (500, "tenant_config_invalid"),
    "ForbiddenTenant": (403, "forbidden_tenant"),  # auth/__init__.py
    "InvalidWindow": (400, "invalid_window"),  # routes/usage.py
    "InvalidCursor": (400, "invalid_cursor"),  # routes/usage.py
    "InvalidThreadId": (422, "invalid_thread_id"),  # routes/threads.py
}


@dataclass(frozen=True)
class MappedError:
    """The HTTP shape an exception maps to."""

    status_code: int
    code: str
    message: str
    # Additional ``error`` members (e.g. budget scope/limit/consumed).
    extra: dict[str, Any] = field(default_factory=dict)


def _budget_extra(exc: BaseException) -> dict[str, Any]:
    """Budget ``scope``/``limit``/``consumed`` read off the engine exception."""
    return {
        "scope": getattr(exc, "scope", None),
        "limit": getattr(exc, "limit", None),
        "consumed": getattr(exc, "used", None),
    }


def is_budget_exceeded(exc: BaseException) -> bool:
    """Whether ``exc`` is (a subclass of) the engine's ``BudgetExceeded``."""
    return any(k.__name__ == "BudgetExceeded" for k in type(exc).__mro__)


def map_exception(exc: BaseException) -> MappedError:
    """Map an exception to its HTTP status, stable code, and safe message.

    Unmapped exceptions (including unclassified engine errors) become an opaque
    ``500`` so no internal detail leaks in the response body.
    """
    for klass in type(exc).__mro__:
        entry = _NAME_MAP.get(klass.__name__)
        if entry is not None:
            status_code, code = entry
            extra = _budget_extra(exc) if code == "budget_exceeded" else {}
            return MappedError(status_code, code, str(exc), extra)
    return MappedError(500, "internal_error", _OPAQUE_500_MESSAGE)


def _error_response(mapped: MappedError) -> JSONResponse:
    return JSONResponse(
        status_code=mapped.status_code,
        content={
            "error": {"code": mapped.code, "message": mapped.message, **mapped.extra}
        },
    )


class ExceptionMappingMiddleware(BaseHTTPMiddleware):
    """Map any exception bubbling out of the app to a JSON error response.

    Sits inside Starlette's ``ServerErrorMiddleware`` but outside the router, so
    engine exceptions that no handler claims are turned into a clean mapped
    response here — without the traceback-re-raise a catch-all ``Exception``
    handler would trigger. Intentional ``HTTPException`` responses (e.g. health's
    ``503``) are already rendered upstream and pass through untouched.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            mapped = map_exception(exc)
            if mapped.status_code >= 500:
                # Full detail to logs/telemetry only; the body stays opaque.
                logger.exception(
                    "unhandled error on %s %s", request.method, request.url.path
                )
            return _error_response(mapped)


async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "detail": exc.errors(),
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire the centralized error mapping onto ``app``.

    Validation errors get our JSON shape; everything else the router raises is
    mapped by :class:`ExceptionMappingMiddleware`.
    """
    app.add_exception_handler(RequestValidationError, _handle_validation_error)  # type: ignore[arg-type]
    app.add_middleware(ExceptionMappingMiddleware)
