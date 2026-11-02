"""OpenAPI response examples for the v0.2 surface (RH2-07-03).

Shapes follow SPEC-RH2 §3 (``done.usage``), §4 (usage / budget), §5 (AG-UI),
§9 (admin) and §11 (error codes). Values are illustrative and carry no secret:
``tests/unit/test_openapi_examples.py`` scans every example for secret-shaped
text, keeps the 12-key usage order and checks each error code is one the host
emits. Documentation only — no route logic reads this module.
"""

from __future__ import annotations

import copy
import json
from typing import Any

AS_OF = "2026-10-04T12:00:00Z"

USAGE: dict[str, Any] = {
    "prompt_tokens": 1200,
    "completion_tokens": 340,
    "cache_read_tokens": 900,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
    "total_tokens": 1540,
    "calls": 3,
    "cost_usd": 0.004213,
    "cost_source": "gateway",
    "estimated": False,
    "served_model": "anthropic/claude-sonnet-4.5",
    "gateway": "openrouter",
}


def error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def _json(status_examples: dict[str, Any]) -> dict[str, Any]:
    """``{name: body}`` → one ``application/json`` content with named examples."""
    return {
        "application/json": {
            "examples": {
                name: {"value": body} for name, body in status_examples.items()
            }
        }
    }


_TAG = "x-prismal-example"
_REGISTRY: dict[str, dict[str, Any]] = {}


def _tagged(content: dict[str, Any]) -> dict[str, Any]:
    """Register ``content`` and return the response object that refers to it."""
    tag = f"ex-{len(_REGISTRY)}"
    _REGISTRY[tag] = content
    return {"content": content, _TAG: tag}


def responses(
    ok: Any, ok_media: str = "application/json", /, **errors: dict[str, Any]
) -> dict[int | str, dict[str, Any]]:
    """FastAPI ``responses=``: the ``200`` example plus named error examples.

    ``errors`` keys are ``"e<status>"`` (e.g. ``e403={...}``) mapping example
    name → error body.
    """
    out: dict[int | str, dict[str, Any]] = {200: _tagged({ok_media: {"example": ok}})}
    for key, examples in errors.items():
        status = int(key.removeprefix("e"))
        out[status] = {"description": _DESCRIPTIONS[status], **_tagged(_json(examples))}
    return out


_DESCRIPTIONS = {
    400: "Bad request",
    402: "Budget exhausted before the turn started",
    403: "Forbidden",
    404: "Not found / disabled",
    406: "Stream protocol not enabled",
    422: "Invalid input",
}

FORBIDDEN_TENANT = {
    "forbidden_tenant": error(
        "forbidden_tenant", "the requested tenant does not match the caller"
    )
}
BUDGET_EXCEEDED = {
    "budget_exceeded": error(
        "budget_exceeded",
        "tenant hard cap reached",
        scope="tenant",
        limit={"tokens": 2000000, "cost_usd": 50.0, "calls": 0, "wall_clock_s": 0.0},
        consumed={
            "tokens": 2000140,
            "cost_usd": 49.9,
            "calls": 410,
            "wall_clock_s": 0.0,
        },
    )
}
PROTOCOL_NOT_ENABLED = {
    "protocol_not_enabled": error(
        "protocol_not_enabled", "the AG-UI stream protocol is not enabled"
    )
}
INVALID_WINDOW = {"invalid_window": error("invalid_window", "`since` is after `until`")}
ADMIN_DISABLED = {
    "admin_disabled": error("admin_disabled", "admin endpoints are disabled")
}
ADMIN_FORBIDDEN = {
    "forbidden": error("forbidden", "the admin scope is required"),
    "forbidden_other_org": error(
        "forbidden", "this org requires the global admin scope"
    ),
}

# ── chat streams ─────────────────────────────────────────────────────────────

NATIVE_SSE = (
    'event: token\ndata: {"delta": "Hello"}\n\n'
    'event: token\ndata: {"delta": " there"}\n\n'
    'event: done\ndata: {"thread_id": "t-42", "finish_reason": "stop", "usage": '
    + json.dumps(USAGE)
    + "}\n\n"
)

AGUI_SSE = (
    'data: {"type":"RUN_STARTED","threadId":"t-42","runId":"r-1"}\n\n'
    'data: {"type":"TEXT_MESSAGE_START","messageId":"m-1","role":"assistant"}\n\n'
    'data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m-1","delta":"Hello"}\n\n'
    'data: {"type":"TEXT_MESSAGE_END","messageId":"m-1"}\n\n'
    'data: {"type":"CUSTOM","name":"prismal.usage","value":{"prompt_tokens":1200}}\n\n'
    'data: {"type":"RUN_FINISHED","threadId":"t-42","runId":"r-1"}\n\n'
)

MESSAGES = responses(
    NATIVE_SSE,
    "text/event-stream",
    e402=BUDGET_EXCEEDED,
    e403=FORBIDDEN_TENANT,
    e406=PROTOCOL_NOT_ENABLED,
    e422={
        "invalid_thread_id": error(
            "invalid_thread_id", "thread ids shaped like `<digits>:` are reserved"
        )
    },
)
# The AG-UI profile streams the same route as engine AG-UI events.
MESSAGES[200]["content"]["text/event-stream"]["examples"] = {
    "native": {"value": NATIVE_SSE},
    "ag-ui": {"value": AGUI_SSE},
}
del MESSAGES[200]["content"]["text/event-stream"]["example"]

AGUI = responses(
    AGUI_SSE,
    "text/event-stream",
    e402=BUDGET_EXCEEDED,
    e406=PROTOCOL_NOT_ENABLED,
    e422={"invalid_input": error("invalid_input", "no user message text")},
)

# ── usage & budget (SPEC §4) ─────────────────────────────────────────────────

USAGE_RESPONSES = responses(
    {
        "org_id": "acme",
        "thread_id": None,
        "since": "2026-10-03T12:00:00Z",
        "until": AS_OF,
        "totals": {
            **USAGE,
            "cost_source": "mixed",
            "estimated": True,
            "served_model": None,
            "gateway": None,
        },
        "cache_hit_ratio": 0.75,
        "by_model": [USAGE],
        "turns": [
            {
                "thread_id": "t-42",
                "turn_id": "01JABCDEF0123456789",
                "finished_at": "2026-10-04T11:59:58Z",
                "finish_reason": "stop",
                "usage": USAGE,
            }
        ],
        "next_cursor": None,
    },
    e400={
        **INVALID_WINDOW,
        "invalid_cursor": error("invalid_cursor", "not a cursor from this endpoint"),
    },
    e403=FORBIDDEN_TENANT,
)

BUDGET_RESPONSES = responses(
    {
        "org_id": "acme",
        "enabled": True,
        "scope": "tenant",
        "status": "within",
        "breached_dimension": None,
        "soft_ratio": 0.8,
        "hard_cap": True,
        "limits": {
            "tokens": 2000000,
            "cost_usd": 50.0,
            "calls": 0,
            "wall_clock_s": 0.0,
        },
        "consumed": {
            "tokens": 61100,
            "cost_usd": 0.412345,
            "calls": 140,
            "wall_clock_s": 312.5,
        },
        "remaining": {
            "tokens": 1938900,
            "cost_usd": 49.587655,
            "calls": None,
            "wall_clock_s": None,
        },
        "gateways": [
            {
                "gateway": "openrouter",
                "window": "credit",
                "unit": "usd",
                "limit": 50.0,
                "used": 8.8,
                "remaining": 41.2,
                "resets_at": None,
                "source": "api",
                "as_of": AS_OF,
            }
        ],
        "as_of": AS_OF,
    },
    e403=FORBIDDEN_TENANT,
)

# ── admin (SPEC §9) ──────────────────────────────────────────────────────────

_ADMIN_ERRORS: dict[str, dict[str, Any]] = {
    "e403": ADMIN_FORBIDDEN,
    "e404": ADMIN_DISABLED,
}
_WINDOW = {"since": "2026-09-27T12:00:00Z", "until": AS_OF}

ADMIN_TENANTS = responses(
    {
        "tenants": [
            {
                "org_id": "acme",
                "live": True,
                "created_at": "2026-10-04T08:00:00Z",
                "last_used_at": "2026-10-04T11:59:58Z",
                "gateways": ["openrouter", "opencode_go"],
                "a2a_protocol_version": "1.0",
                "config_source": "vault",
            }
        ],
        "max_tenants": 64,
    },
    **_ADMIN_ERRORS,
)

ADMIN_AUDIT = responses(
    {
        "page": 1,
        "page_size": 50,
        "total": 1,
        "entries": [
            {
                "seq": 1042,
                "ts": "2026-10-04T11:59:58Z",
                "event": "a2a.inbound.request",
                "actor": "did:web:agents.example.com:billing",
                "org_id": "acme",
                "hash": "9f2c…e1",
                "prev_hash": "41aa…07",
                "fields": {"method": "SendMessage"},
            }
        ],
        "chain": {"status": "valid", "checked": 1042, "first_broken_seq": None},
    },
    **_ADMIN_ERRORS,
)

ADMIN_DECISIONS = responses(
    {
        "org_id": "acme",
        "enabled": True,
        "window": _WINDOW,
        "integrations": [
            {
                "integration": "routing",
                "mode": "shadow",
                "samples": 2412,
                "agreement_ratio": 0.974,
                "accepted": 1830,
                "escalated": 402,
                "abstained": 180,
                "llm_calls_avoided": 1830,
                "window": {"since": "2026-09-30T08:15:00Z", "until": AS_OF},
            }
        ],
        "as_of": AS_OF,
    },
    e400=INVALID_WINDOW,
    **_ADMIN_ERRORS,
)

ADMIN_ROUTING = responses(
    {
        "org_id": "acme",
        "enabled": True,
        "mode": "shadow",
        "window": _WINDOW,
        "policies": [
            {
                "policy": "composite",
                "decisions": 5120,
                "decisions_by_tier": {"small": 3900, "medium": 1000, "frontier": 220},
                "escalations": 140,
                "escalations_by_reason": {
                    "inadequate": 90,
                    "structured_invalid": 40,
                    "tool_errors": 10,
                },
                "plan_cache_hits": 610,
                "estimated_savings_usd": 12.43,
                "window": _WINDOW,
            }
        ],
        "as_of": AS_OF,
    },
    e400=INVALID_WINDOW,
    **_ADMIN_ERRORS,
)


def install(app: Any) -> None:
    """Serve the examples verbatim in ``app.openapi()``.

    FastAPI encodes the generated document with ``exclude_none=True``, which
    also strips ``null`` members *inside* examples (``served_model``,
    ``next_cursor``, …). Each declared response carries an ``x-prismal-example``
    tag (see :func:`responses`); after generation the tag is removed and the
    response's example content is written back unchanged.
    """
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema  # type: ignore[no-any-return]
        schema: dict[str, Any] = generate()
        for ops in schema.get("paths", {}).values():
            for op in ops.values():
                for documented in op.get("responses", {}).values():
                    tag = documented.pop(_TAG, None)
                    if tag in _REGISTRY:
                        documented["content"] = copy.deepcopy(_REGISTRY[tag])
        app.openapi_schema = schema
        return schema

    app.openapi = openapi
