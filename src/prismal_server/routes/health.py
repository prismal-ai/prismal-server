"""Health and readiness routes (RHB-01-04, SPEC-RHB-HLT-001/002/003).

``GET /healthz`` is liveness — ``200`` whenever the process is up, independent of
engine state. ``GET /readyz`` is readiness — ``200`` only when the default
(``org_id=None``) ``RuntimeContext`` composes, else ``503`` with a stable,
non-sensitive ``reason`` code. Neither endpoint leaks config or secrets.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from prismal_server.deps import RuntimeRegistry
from prismal_server.errors import map_exception

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: the default runtime composes."""
    registry: RuntimeRegistry = request.app.state.registry
    try:
        await registry.get(None)
    except Exception as exc:
        # Reason is the stable mapped code, never the raw error detail (HLT-003).
        reason = map_exception(exc).code
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "reason": reason}
        )
    return JSONResponse(status_code=200, content={"status": "ready"})
