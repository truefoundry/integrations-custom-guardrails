"""Smoke tests for the Lasso Security wrapper.

Boots the FastAPI app in-process via TestClient. The Lasso API HTTP call is
mocked (``guardrail.lasso.requests.post``) so the suite runs in CI without a
real ``LASSO_API_KEY`` or network access.

Run:
    pytest -v tests/

Response contract:
    Allow   -> HTTP 200 + {"verdict": true}
    Mutate  -> HTTP 200 + {"verdict": true, "transformed": <bool>, "result": {...}}
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


EMAIL = "bob@example.com"
CTX = {"user": {"subjectId": "u1", "subjectType": "user"}}


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> dict:
        return self._payload


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    from main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth() -> dict[str, str]:
    key = os.environ.get("WRAPPER_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


@pytest.fixture(autouse=True)
def _lasso_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # _resolve_api_key reads LASSO_API_KEY before the (mocked) HTTP call.
    monkeypatch.setenv("LASSO_API_KEY", "test-key")


def _mock_lasso(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    import guardrail.lasso as lasso

    monkeypatch.setattr(lasso.requests, "post", lambda *a, **k: _FakeResponse(payload))


def _classifix_with_span(role: str, content: str) -> dict:
    """A realistic classifix response: messages come back UNMASKED, and the
    redaction lives in a finding span (action ADMIN_ALERT does not rewrite
    messages). The wrapper is responsible for applying the span."""
    start = content.index(EMAIL)
    return {
        "deputies": {"pattern-detection": True},
        "findings": {
            "pattern-detection": [
                {
                    "message_index": 0,
                    "name": "Email Address",
                    "category": "PERSONAL_IDENTIFIABLE_INFORMATION",
                    "action": "ADMIN_ALERT",
                    "severity": "HIGH",
                    "start": start,
                    "end": start + len(EMAIL),
                    "mask": "<EMAIL_ADDRESS>",
                }
            ]
        },
        "violations_detected": True,
        "messages": [{"role": role, "content": content}],
    }


_CLEAN = {"deputies": {}, "findings": {}, "violations_detected": False, "messages": []}


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_output_classifix_masks_pii(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the output mutate rail must redact PII via finding span metadata.

    Previously ``_apply_masked_messages(is_output=True)`` returned
    ``transformed=True`` unconditionally, short-circuiting the
    ``_apply_finding_masks`` fallback and passing PII through unmasked while
    reporting a successful transform.
    """
    content = f"Sure, contact me at {EMAIL}"
    _mock_lasso(monkeypatch, _classifix_with_span("assistant", content))
    body = {
        "requestBody": {"messages": [{"role": "user", "content": "hi"}]},
        "responseBody": {"choices": [{"message": {"role": "assistant", "content": content}}]},
        "context": CTX,
    }
    data = client.post("/lasso-classifix-output", json=body, headers=auth).json()
    assert data["verdict"] is True
    assert data["transformed"] is True
    masked = data["result"]["choices"][0]["message"]["content"]
    assert EMAIL not in masked
    assert "<EMAIL_ADDRESS>" in masked


def test_output_classifix_clean_no_transform(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clean = "Paris is the capital of France."
    _mock_lasso(monkeypatch, _CLEAN)
    body = {
        "requestBody": {"messages": [{"role": "user", "content": "hi"}]},
        "responseBody": {"choices": [{"message": {"role": "assistant", "content": clean}}]},
        "context": CTX,
    }
    data = client.post("/lasso-classifix-output", json=body, headers=auth).json()
    assert data["verdict"] is True
    assert data["transformed"] is False
    assert data["result"]["choices"][0]["message"]["content"] == clean


def test_input_classifix_masks_pii(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    content = f"My email is {EMAIL}"
    _mock_lasso(monkeypatch, _classifix_with_span("user", content))
    body = {
        "requestBody": {"messages": [{"role": "user", "content": content}]},
        "context": CTX,
    }
    data = client.post("/lasso-classifix", json=body, headers=auth).json()
    assert data["transformed"] is True
    masked = data["result"]["messages"][0]["content"]
    assert EMAIL not in masked
    assert "<EMAIL_ADDRESS>" in masked
