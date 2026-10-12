"""WebSocket transport (RH2-05-*, SPEC-RH2-WSK-001..003).

``GET /ws/threads/{thread_id}`` (mounted only WHERE ``host_ws_enabled``) runs
chat turns over one connection. The handshake is the only auth point: the
``Origin`` (when sent) must be in ``cors_origins``, strict mode needs a
credential, and the tenant is resolved fail-closed (``for_read``) — any
refusal closes ``1008`` before accept.

Client frames: ``{"type":"message","content","role","metadata","protocol"}``,
``{"type":"cancel"}``, ``{"type":"ping"}``. Server frames are
``{"event": <native SSE name>, "data": <same payload as SSE>}`` or
``{"event":"agui","data": <engine AG-UI event>}`` (DD-RH2-007), plus
``{"event":"heartbeat"}`` and ``{"event":"pong"}``. One turn at a time
(``4409``); malformed frame ``4400``; oversized frame ``1009``. A turn that
cannot start or fails (budget, AG-UI disabled, …) is an ``error`` frame and the
connection stays open — WebSocket has no status code to carry a ``402``.

Turns reuse :func:`~prismal_server.routes.threads.open_turn`: org-scoped engine
thread key, runtime lease, AG-UI gate and usage record are identical to SSE.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Annotated, Any, Literal

import anyio
import anyio.abc
from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from prismal_server import metrics
from prismal_server.auth import ForbiddenTenant, resolve_org_id
from prismal_server.errors import map_exception
from prismal_server.routes.threads import Protocol, open_turn

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

CLOSE_NORMAL = 1000
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_MALFORMED = 4400
CLOSE_BUSY = 4409

# Buffered graph items per turn: a slow client pauses the graph (backpressure).
_PUMP_MAXSIZE = 64

_AGUI_TERMINAL = frozenset({"RUN_FINISHED", "RUN_ERROR"})
_NATIVE_TERMINAL = frozenset({"done", "error"})


class MessageFrame(BaseModel):
    type: Literal["message"]
    content: str = Field(min_length=1)
    role: str = "user"
    metadata: dict[str, Any] = {}
    protocol: Protocol = "prismal"


class CancelFrame(BaseModel):
    type: Literal["cancel"]


class PingFrame(BaseModel):
    type: Literal["ping"]


ClientFrame = Annotated[
    MessageFrame | CancelFrame | PingFrame, Field(discriminator="type")
]
_FRAMES: TypeAdapter[ClientFrame] = TypeAdapter(ClientFrame)


def origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    """Non-browser clients send no ``Origin``; browsers must match ``cors_origins``."""
    return origin is None or "*" in allowed or origin in allowed


async def _reject(websocket: WebSocket) -> None:
    metrics.increment("ws_sessions_total", close_code=str(CLOSE_POLICY))
    await websocket.close(code=CLOSE_POLICY)


@router.websocket("/ws/threads/{thread_id}")
async def ws_thread(websocket: WebSocket, thread_id: str) -> None:
    """Chat turns for ``thread_id`` over one WebSocket connection."""
    state = websocket.app.state
    settings = state.settings
    if not origin_allowed(websocket.headers.get("origin"), settings.cors_origins):
        await _reject(websocket)
        return
    identity = await state.auth_backend.resolve(websocket)
    if identity is None and settings.host_auth_strict:
        await _reject(websocket)
        return
    try:
        org_id = resolve_org_id(websocket, identity, for_read=True)
    except ForbiddenTenant:
        await _reject(websocket)
        return

    await websocket.accept()
    code = await _Session(websocket, org_id=org_id, thread_id=thread_id).run()
    metrics.increment("ws_sessions_total", close_code=str(code))


class _Session:
    """One accepted connection: frame loop, heartbeat timer, at most one turn.

    Heartbeat and turn run in an anyio task group, so leaving the session —
    client gone, close, or server shutdown cancelling the handler — always
    cancels them; turn cleanup is shielded so its runtime lease is released.
    """

    def __init__(self, websocket: WebSocket, *, org_id: str | None, thread_id: str):
        self._ws = websocket
        self._state = websocket.app.state
        self._settings = self._state.settings
        self._org_id = org_id
        self._thread_id = thread_id
        self._send_lock = anyio.Lock()
        # True until the turn's terminal frame is queued, so a client that
        # answers ``done`` with the next message is never seen as busy.
        self._busy = False
        self._turn_scope: anyio.CancelScope | None = None
        self._turn_done: anyio.Event | None = None

    async def run(self) -> int:
        """Serve frames until the client leaves or a close; the close code."""
        code = CLOSE_NORMAL
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._heartbeat)
            try:
                code = await self._loop(tg)
            finally:
                tg.cancel_scope.cancel()
        return code

    async def _loop(self, tg: anyio.abc.TaskGroup) -> int:
        limit = self._settings.host_ws_max_message_bytes
        while True:
            message = await self._ws.receive()
            if message["type"] == "websocket.disconnect":
                return int(message.get("code", CLOSE_NORMAL))
            text = message.get("text")
            raw: bytes = (
                text.encode() if text is not None else message.get("bytes") or b""
            )
            if len(raw) > limit:
                return await self._close(CLOSE_TOO_BIG)
            try:
                frame = _FRAMES.validate_json(raw)
            except ValidationError:
                return await self._close(CLOSE_MALFORMED)

            if isinstance(frame, PingFrame):
                await self._send({"event": "pong"})
            elif isinstance(frame, CancelFrame):
                await self._cancel()
            elif self._busy:
                return await self._close(CLOSE_BUSY)
            else:
                self._busy = True
                self._turn_scope = anyio.CancelScope()
                self._turn_done = anyio.Event()
                tg.start_soon(self._run_turn, frame, self._turn_scope, self._turn_done)

    async def _send(self, frame: dict[str, Any]) -> None:
        # Sends from the loop, the heartbeat and the turn are serialized; each
        # is awaited, so a slow client back-pressures the turn's pump.
        async with self._send_lock:
            await self._ws.send_json(frame)

    async def _close(self, code: int) -> int:
        async with self._send_lock:
            await self._ws.close(code=code)
        return code

    async def _heartbeat(self) -> None:
        interval = self._settings.sse_heartbeat_s
        with contextlib.suppress(Exception):  # socket gone: the loop ends it
            while True:
                await anyio.sleep(interval)
                await self._send({"event": "heartbeat"})

    async def _cancel(self) -> None:
        if not self._busy or self._turn_scope is None or self._turn_done is None:
            return
        self._turn_scope.cancel()
        await self._turn_done.wait()
        self._busy = False
        await self._send(
            {
                "event": "error",
                "data": {"code": "cancelled", "message": "turn cancelled"},
            }
        )

    async def _send_error(self, exc: BaseException) -> None:
        mapped = map_exception(exc)
        self._busy = False
        await self._send(
            {
                "event": "error",
                "data": {
                    "code": mapped.code,
                    "message": mapped.message,
                    **mapped.extra,
                },
            }
        )

    async def _run_turn(
        self, frame: MessageFrame, scope: anyio.CancelScope, done: anyio.Event
    ) -> None:
        try:
            with scope:
                await self._stream_turn(frame)
        finally:
            done.set()

    async def _stream_turn(self, frame: MessageFrame) -> None:
        try:
            turn = await open_turn(
                self._state,
                org_id=self._org_id,
                thread_id=self._thread_id,
                role=frame.role,
                content=frame.content,
                agui=frame.protocol == "ag-ui",
                pump_maxsize=_PUMP_MAXSIZE,
            )
        except Exception as exc:
            await self._send_error(exc)
            return
        try:
            events = turn.events()
            async with contextlib.aclosing(events):
                async for event in events:
                    if event is None:  # idle: the connection heartbeat covers it
                        continue
                    if turn.agui:
                        wire = event.to_wire()
                        terminal = wire.get("type") in _AGUI_TERMINAL
                        out = {"event": "agui", "data": wire}
                    else:
                        name, data = event
                        terminal = name in _NATIVE_TERMINAL
                        out = {"event": name, "data": data}
                    if terminal:
                        self._busy = False
                    await self._send(out)
        except Exception:  # client gone mid-send; the session loop closes up
            logger.debug("websocket turn ended early", exc_info=True)
        finally:
            # Shielded: a cancelled turn still closes its graph stream and
            # releases the runtime lease (SPEC-RH2-TEN-004).
            with anyio.CancelScope(shield=True):
                await turn.aclose()
