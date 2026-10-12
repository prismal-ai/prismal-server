"""AG-UI ``RunAgentInput`` endpoint (RH2-03-03, SPEC-RH2-AGU-005).

``POST /v1/agui`` adapts an AG-UI ``RunAgentInput`` to one host turn streamed
as AG-UI: the last ``user`` message's text becomes the turn input, ``threadId``
/ ``runId`` flow into ``RUN_STARTED``. Client-declared ``tools`` are ignored
(logged) in 0.2 — the engine owns its tools. Prior messages are not replayed:
the engine keeps the thread's history in its own checkpoint.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from prismal_server.routes.threads import run_turn

router = APIRouter(prefix="/v1", tags=["ag-ui"])
logger = logging.getLogger(__name__)


class InvalidAgUiInput(Exception):
    """422 ``invalid_input``: no ``user`` message with text content."""


class RunAgentInput(BaseModel):
    """The subset of AG-UI 1.0 ``RunAgentInput`` the host reads (extra allowed)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    thread_id: str = Field(alias="threadId", min_length=1)
    run_id: str | None = Field(default=None, alias="runId")
    messages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # InputContent[]: keep the text parts
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def last_user_text(messages: list[dict[str, Any]]) -> str:
    """Text of the last ``user`` message; :class:`InvalidAgUiInput` if none."""
    for message in reversed(messages):
        if message.get("role") == "user":
            text = _text(message.get("content"))
            if text:
                return text
            break
    raise InvalidAgUiInput("RunAgentInput has no user message with text content")


@router.post("/agui")
async def run_agent(body: RunAgentInput, request: Request) -> StreamingResponse:
    """Run one turn from an AG-UI ``RunAgentInput`` and stream AG-UI events."""
    content = last_user_text(body.messages)
    if body.tools:
        logger.info(
            "ignoring %d client-declared AG-UI tools: %s",
            len(body.tools),
            [t.get("name") for t in body.tools],
        )
    return await run_turn(
        request,
        thread_id=body.thread_id,
        role="user",
        content=content,
        agui=True,
        run_id=body.run_id,
    )
