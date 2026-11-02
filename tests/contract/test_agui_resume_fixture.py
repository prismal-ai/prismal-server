"""Contract: the published AG-UI interrupt → resume fixture (RH3-02-03, opt-in).

Replays two ``POST /v1/agui`` runs in fake mode (``/interrupt`` then a
``resume``) and compares the AG-UI events with ``fixtures/agui/interrupt-resume.json``,
which front-ends (reflex-agentkit) replay. ``timestamp`` fields are dropped.
Regenerate with ``PRISMAL_SERVER_UPDATE_FIXTURES=1``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.contract

FIXTURE = Path(__file__).parents[2] / "fixtures" / "agui" / "interrupt-resume.json"
BASE = {"tools": [], "context": [], "state": {}, "forwardedProps": {}}
RUNS: list[dict[str, Any]] = [
    {
        "threadId": "thread-1",
        "runId": "run-1",
        "messages": [
            {"id": "msg-1", "role": "user", "content": "/interrupt Approve the plan?"}
        ],
        **BASE,
    },
    {
        "threadId": "thread-1",
        "runId": "run-2",
        "messages": [],
        "resume": [
            {
                "interruptId": "fake-int-1",
                "status": "resolved",
                "payload": {"action": "approve"},
            }
        ],
        **BASE,
    },
]


def _strip(event: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in event.items() if k != "timestamp"}


@pytest.fixture
def agui_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    from prismal.a2a import get_settings

    monkeypatch.setenv("PRISMAL_AGUI_EVENTS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_interrupt_resume_fixture(agui_on: Any) -> None:
    from prismal_server.app import create_app
    from prismal_server.config import HostSettings

    app = create_app(settings=HostSettings(host_fake_llm_mode=True))
    recorded: list[dict[str, Any]] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for run in RUNS:
            resp = await c.post("/v1/agui", json=run)
            assert resp.status_code == 200
            events = [
                _strip(json.loads(line[5:]))
                for line in resp.text.splitlines()
                if line.startswith("data:")
            ]
            recorded.append({"input": run, "events": events})
    document = {
        "description": (
            "prismal-server AG-UI 1.0 interrupt → resume, fake-LLM mode "
            "(timestamps removed). Run 1 ends with RUN_FINISHED.outcome "
            "interrupt; run 2 resumes it via RunAgentInput.resume."
        ),
        "runs": recorded,
    }
    if os.environ.get("PRISMAL_SERVER_UPDATE_FIXTURES") == "1":
        FIXTURE.write_text(json.dumps(document, indent=2) + "\n")
    assert json.loads(FIXTURE.read_text()) == document
