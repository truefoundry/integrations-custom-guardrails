"""Smoke tests skeleton. Copy and adapt to your rail endpoints.

Required cases:
- /health
- missing/wrong bearer -> 401
- short-circuit cases (no user message, no assistant message) -> 200 + verdict=true
- /debug/loaded-config structure
- benign input/output -> verdict=true
- violating input/output -> verdict=false with message

Live-vendor tests should auto-skip when the vendor isn't reachable so the suite
runs in CI without secrets.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    from main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth() -> dict[str, str]:
    key = os.environ.get("WRAPPER_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


CTX = {"user": {"subjectId": "u1", "subjectType": "user"}}


def _input_body(content: str) -> dict:
    return {
        "requestBody": {"model": "gpt-4o", "messages": [{"role": "user", "content": content}]},
        "context": CTX,
    }


def _output_body(content: str, user_msg: str = "hi") -> dict:
    return {
        "requestBody": {"model": "gpt-4o", "messages": [{"role": "user", "content": user_msg}]},
        "responseBody": {"choices": [{"message": {"role": "assistant", "content": content}}]},
        "context": CTX,
    }


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_missing_bearer_returns_401(client: TestClient) -> None:
    if not os.environ.get("WRAPPER_API_KEY"):
        pytest.skip("WRAPPER_API_KEY not set; auth is disabled")
    # TODO: replace `/example-input` with your actual route
    r = client.post("/example-input", json=_input_body("hi"))
    assert r.status_code == 401


def test_wrong_bearer_returns_401(client: TestClient) -> None:
    if not os.environ.get("WRAPPER_API_KEY"):
        pytest.skip("WRAPPER_API_KEY not set; auth is disabled")
    r = client.post("/example-input", headers={"Authorization": "Bearer wrong"}, json=_input_body("hi"))
    assert r.status_code == 401


# TODO: add benign + violation tests per rail.
# Skip them on env unless the vendor is reachable; see existing integrations for examples.
