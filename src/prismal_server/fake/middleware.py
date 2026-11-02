"""``X-Prismal-Fake-LLM: 1`` on every response (SPEC-RH2-FAK-003)."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

FAKE_HEADER = "X-Prismal-Fake-LLM"
_HEADER = (FAKE_HEADER.lower().encode(), b"1")


class FakeHeaderMiddleware:
    """Pure ASGI: tags HTTP responses (SSE included) and WebSocket accepts."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        async def tagged_send(message: Message) -> None:
            if message["type"] in {"http.response.start", "websocket.accept"}:
                message = {
                    **message,
                    "headers": [*message.get("headers", []), _HEADER],
                }
            await send(message)

        await self.app(scope, receive, tagged_send)
