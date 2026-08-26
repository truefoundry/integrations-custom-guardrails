"""Smoke tests for the Onyx Security wrapper.

Boots the FastAPI app in-process via TestClient. Cases that would call Onyx AI
Guard are skipped unless ``ONYX_API_KEY`` is set, so the suite is green in CI
without secrets.

Run:
    pytest -v tests/

Response contract under test (post tfy-llm-gateway commit a1c551be):
    Allow -> HTTP 200 + {"verdict": true}
    Block -> HTTP 200 + {"verdict": false, "message": "..."}
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


requires_onyx = pytest.mark.skipif(
    not os.environ.get("ONYX_API_KEY", "").strip(),
    reason="needs ONYX_API_KEY to call Onyx AI Guard",
)


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


# ---------------------------------------------------------------------------
# Local-only (no Onyx call)
# ---------------------------------------------------------------------------

def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_missing_bearer_returns_401(client: TestClient) -> None:
    if not os.environ.get("WRAPPER_API_KEY"):
        pytest.skip("WRAPPER_API_KEY not set; auth is disabled in this run")
    r = client.post("/onyx-input", json=_input_body("hi"))
    assert r.status_code == 401


def test_wrong_bearer_returns_401(client: TestClient) -> None:
    if not os.environ.get("WRAPPER_API_KEY"):
        pytest.skip("WRAPPER_API_KEY not set; auth is disabled in this run")
    r = client.post(
        "/onyx-input",
        headers={"Authorization": "Bearer not-the-real-key"},
        json=_input_body("hi"),
    )
    assert r.status_code == 401


def test_no_user_message_passes_through(client: TestClient, auth: dict[str, str]) -> None:
    # System-only history -> nothing for the input rail to check -> short-circuit verdict=true.
    r = client.post(
        "/onyx-input",
        headers=auth,
        json={
            "requestBody": {"model": "gpt-4o", "messages": [{"role": "system", "content": "you are helpful"}]},
            "context": CTX,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"verdict": True, "message": None}


def test_no_assistant_message_passes_through(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/onyx-output",
        headers=auth,
        json={
            "requestBody": {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            "responseBody": {"choices": []},
            "context": CTX,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"verdict": True, "message": None}


def test_debug_loaded_config_lists_routes(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get("/debug/loaded-config", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "/onyx-input" in body["routes"]["input"]
    assert "/onyx-output" in body["routes"]["output"]
    assert "wrapper_version" in body
    assert "onyx_api_key_configured" in body


# ---------------------------------------------------------------------------
# Rail verdicts (require a live Onyx AI Guard /simple call)
# ---------------------------------------------------------------------------

# Onyx integration-guide phrases: safe prompt allows; fightclub/bradpitt/norton blocks.
_SAFE_PROMPT = "What is the capital of France?"
_BLOCK_PROMPT = "fightclub bradpitt norton"


@requires_onyx
def test_benign_input_passes(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/onyx-input",
        headers=auth,
        json=_input_body(_SAFE_PROMPT),
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] is True


@requires_onyx
def test_policy_violation_input_blocks(client: TestClient, auth: dict[str, str]) -> None:
    # Direction-dependent: the Onyx test policy only has an Input-direction rule.
    # {"user_prompt": "...fightclub"} → action:block; same text as {"response": ...} → allow.
    r = client.post(
        "/onyx-input",
        headers=auth,
        json=_input_body(_BLOCK_PROMPT),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] is False
    assert "Onyx AI Guard" in body["message"]


@requires_onyx
def test_benign_output_passes(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/onyx-output",
        headers=auth,
        json=_output_body("The capital of France is Paris."),
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] is True


@pytest.mark.skip(
    reason="test policy has no Output-direction rule; output blocking needs an Output rule added in Onyx"
)
@requires_onyx
def test_policy_violation_output_blocks(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/onyx-output",
        headers=auth,
        json=_output_body(_BLOCK_PROMPT),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] is False
    assert "Onyx AI Guard" in body["message"]
