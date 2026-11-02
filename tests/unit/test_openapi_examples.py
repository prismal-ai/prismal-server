"""OpenAPI examples for the v0.2 surface (RH2-07-03).

Each v0.2 route documents a ``200`` example and its error codes; examples are
taken from SPEC §3/§4/§9 wire shapes, use only error codes the host can emit,
keep the 12-key usage order, and never carry anything secret-shaped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from prismal_server.app import create_app
from prismal_server.config import HostSettings
from prismal_server.errors import _NAME_MAP

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "protocols" / "usage_wire.json"
USAGE_KEYS = [k["name"] for k in json.loads(_FIXTURE.read_text())["keys"]]

# (method, path) → error statuses that must carry an example.
V02_ROUTES: dict[tuple[str, str], set[str]] = {
    ("post", "/threads/{thread_id}/messages"): {"402", "403", "406", "422"},
    ("get", "/v1/usage"): {"400", "403"},
    ("get", "/v1/budget"): {"403"},
    ("post", "/v1/agui"): {"402", "406", "422"},
    ("get", "/v1/admin/tenants"): {"403", "404"},
    ("get", "/v1/admin/audit"): {"403", "404"},
    ("get", "/v1/admin/decisions"): {"400", "403", "404"},
    ("get", "/v1/admin/routing"): {"400", "403", "404"},
}

HOST_CODES = {code for _, code in _NAME_MAP.values()} | {"validation_error"}
SECRETISH = re.compile(
    r"(api[_-]?key|secret|password|bearer\s|sk-[a-z0-9]|token=)", re.I
)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return create_app(settings=HostSettings()).openapi()


def _examples(op: dict[str, Any], status: str) -> list[Any]:
    content = op["responses"].get(status, {}).get("content", {})
    found: list[Any] = []
    for media in content.values():
        if "example" in media:
            found.append(media["example"])
        found.extend(e["value"] for e in media.get("examples", {}).values())
    return found


@pytest.mark.parametrize(("method", "path"), sorted(V02_ROUTES))
def test_route_documents_success_and_error_examples(
    spec: dict[str, Any], method: str, path: str
) -> None:
    op = spec["paths"][path][method]
    assert _examples(op, "200"), f"{method.upper()} {path}: no 200 example"
    for status in V02_ROUTES[(method, path)]:
        examples = _examples(op, status)
        assert examples, f"{method.upper()} {path}: no {status} example"
        for example in examples:
            assert example["error"]["code"] in HOST_CODES


def _walk(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [value, *(x for v in value.values() for x in _walk(v))]
    if isinstance(value, list):
        return [x for v in value for x in _walk(v)]
    return [value]


def test_no_example_carries_a_secret(spec: dict[str, Any]) -> None:
    text = json.dumps(
        [
            _examples(op, status)
            for ops in spec["paths"].values()
            for op in ops.values()
            for status in op.get("responses", {})
        ]
    )
    assert not SECRETISH.search(text), SECRETISH.search(text)


def test_usage_examples_keep_the_wire_key_order(spec: dict[str, Any]) -> None:
    examples = [
        *_examples(spec["paths"]["/v1/usage"]["get"], "200"),
        *_examples(spec["paths"]["/threads/{thread_id}/messages"]["post"], "200"),
    ]
    usages = [
        node["usage"]
        for example in examples
        for node in _walk(example if not isinstance(example, str) else _sse(example))
        if isinstance(node, dict) and isinstance(node.get("usage"), dict)
    ]
    totals = [_walk(e)[0]["totals"] for e in examples if isinstance(e, dict)]
    assert usages and totals
    for usage in [*usages, *totals]:
        assert list(usage) == USAGE_KEYS


def _sse(body: str) -> list[Any]:
    return [
        json.loads(line.removeprefix("data:").strip())
        for line in body.splitlines()
        if line.startswith("data:")
    ]
