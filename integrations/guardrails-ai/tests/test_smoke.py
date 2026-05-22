"""Smoke tests for the Guardrails AI wrapper.

Boots the FastAPI app in-process via TestClient. All validators are local
(no LLM judge), so every test runs without an external dependency. The only
requirement is that the hub validators have been installed locally — if any
validator import fails, the verdict tests are skipped.

Run:
    pytest -v tests/

Response contract:
    Allow -> HTTP 200 + {"verdict": true, "message": null}
    Block -> HTTP 200 + {"verdict": false, "message": "<validator>: ..."}
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


def _validators_importable() -> bool:
    try:
        from guardrails.hub import DetectPII, ProfanityFree, SecretsPresent, ToxicLanguage  # noqa: F401
        return True
    except Exception:
        return False


requires_validators = pytest.mark.skipif(
    not _validators_importable(),
    reason="hub validators not installed; run setup.py first",
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
    return {"requestBody": {"model": "gpt-4o", "messages": [{"role": "user", "content": content}]}, "context": CTX}


def _output_body(content: str, user_msg: str = "hi") -> dict:
    return {
        "requestBody": {"model": "gpt-4o", "messages": [{"role": "user", "content": user_msg}]},
        "responseBody": {"choices": [{"message": {"role": "assistant", "content": content}}]},
        "context": CTX,
    }


# ---------------------------------------------------------------------------
# Wrapper-only (no vendor validator runs)
# ---------------------------------------------------------------------------

def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_missing_bearer_returns_401(client: TestClient) -> None:
    if not os.environ.get("WRAPPER_API_KEY"):
        pytest.skip("WRAPPER_API_KEY not set; auth is disabled")
    r = client.post("/detect-pii-input", json=_input_body("hi"))
    assert r.status_code == 401


def test_wrong_bearer_returns_401(client: TestClient) -> None:
    if not os.environ.get("WRAPPER_API_KEY"):
        pytest.skip("WRAPPER_API_KEY not set; auth is disabled")
    r = client.post("/detect-pii-input", headers={"Authorization": "Bearer wrong"}, json=_input_body("hi"))
    assert r.status_code == 401


def test_no_user_message_input_passes(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/detect-pii-input",
        headers=auth,
        json={"requestBody": {"messages": [{"role": "system", "content": "you are helpful"}]}, "context": CTX},
    )
    assert r.status_code == 200
    assert r.json() == {"verdict": True, "message": None}


def test_no_assistant_message_output_passes(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/detect-pii-output",
        headers=auth,
        json={"requestBody": {"messages": [{"role": "user", "content": "x"}]}, "responseBody": {"choices": []}, "context": CTX},
    )
    assert r.status_code == 200
    assert r.json() == {"verdict": True, "message": None}


def test_debug_loaded_config_lists_routes(client: TestClient, auth: dict[str, str]) -> None:
    if not os.environ.get("WRAPPER_API_KEY"):
        pytest.skip("WRAPPER_API_KEY not set; auth is disabled")
    r = client.get("/debug/loaded-config", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "/detect-pii-input" in body["routes"]["input"]
    assert "/secrets-present-input" in body["routes"]["input"]
    assert "/toxic-language-input" in body["routes"]["input"]
    assert "/detect-pii-output" in body["routes"]["output"]
    assert "/secrets-present-output" in body["routes"]["output"]
    assert "/toxic-language-output" in body["routes"]["output"]
    assert "/profanity-free-output" in body["routes"]["output"]


# ---------------------------------------------------------------------------
# Verdict tests (require validators installed locally)
# ---------------------------------------------------------------------------

@requires_validators
def test_benign_input_passes(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post("/detect-pii-input", headers=auth, json=_input_body("What is the capital of France?"))
    assert r.status_code == 200
    assert r.json()["verdict"] is True


@requires_validators
def test_pii_input_blocks(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/detect-pii-input",
        headers=auth,
        json=_input_body("My email is jane.doe@example.com and my SSN is 123-45-6789."),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] is False
    assert "DetectPII" in body["message"]


@requires_validators
def test_secret_input_blocks(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/secrets-present-input",
        headers=auth,
        json=_input_body("Here is my key: sk-proj-abc123def456ghi789jkl012mno345pqr678stu901vwx234"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] is False
    assert "SecretsPresent" in body["message"]


@requires_validators
def test_toxic_input_blocks(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/toxic-language-input",
        headers=auth,
        json=_input_body("You are a worthless idiot and I hate you."),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] is False
    assert "ToxicLanguage" in body["message"]


@requires_validators
def test_benign_output_passes(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post("/detect-pii-output", headers=auth, json=_output_body("The capital of France is Paris."))
    assert r.status_code == 200
    assert r.json()["verdict"] is True


@requires_validators
def test_pii_output_blocks(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/detect-pii-output",
        headers=auth,
        json=_output_body("Your email is jane.doe@example.com and SSN is 123-45-6789."),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] is False
    assert "DetectPII" in body["message"]


@requires_validators
def test_profane_output_blocks(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/profanity-free-output",
        headers=auth,
        json=_output_body("This is a damn shitty fucking mess."),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] is False
    assert "ProfanityFree" in body["message"]
