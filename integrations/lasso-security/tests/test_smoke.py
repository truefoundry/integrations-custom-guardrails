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
    # Agent identity is opt-in; keep a developer's .env out of the assertions.
    monkeypatch.delenv("LASSO_AGENT_ID", raising=False)
    monkeypatch.delenv("LASSO_AGENT_NAME", raising=False)


def _mock_lasso(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    import guardrail.lasso as lasso

    monkeypatch.setattr(lasso.requests, "post", lambda *a, **k: _FakeResponse(payload))


def _mock_lasso_capture(monkeypatch: pytest.MonkeyPatch, payload: dict) -> dict:
    """Like _mock_lasso, but returns a dict that receives the body sent to Lasso."""
    import guardrail.lasso as lasso

    captured: dict = {}

    def _post(*args, **kwargs) -> _FakeResponse:
        captured.update(kwargs.get("json") or {})
        return _FakeResponse(payload)

    monkeypatch.setattr(lasso.requests, "post", _post)
    return captured


def _classifix_with_span(role: str, content: str, action: str = "AUTO_MASKING") -> dict:
    """A realistic classifix response: messages come back UNMASKED, and the
    redaction lives in a finding span. The wrapper applies the span only when
    the finding's action marks it for masking (AUTO_MASKING); alert-only
    actions (e.g. ADMIN_ALERT) carry the span but must not be masked."""
    start = content.index(EMAIL)
    return {
        "deputies": {"pattern-detection": True},
        "findings": {
            "pattern-detection": [
                {
                    "message_index": 0,
                    "name": "Email Address",
                    "category": "PERSONAL_IDENTIFIABLE_INFORMATION",
                    "action": action,
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


def test_output_classifix_alert_only_not_masked(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Policy fidelity: an alert-only finding (ADMIN_ALERT) carries a mask span
    but the operator chose to alert, not redact — so the content must pass
    through unmasked and unchanged."""
    content = f"Sure, contact me at {EMAIL}"
    _mock_lasso(monkeypatch, _classifix_with_span("assistant", content, action="ADMIN_ALERT"))
    body = {
        "requestBody": {"messages": [{"role": "user", "content": "hi"}]},
        "responseBody": {"choices": [{"message": {"role": "assistant", "content": content}}]},
        "context": CTX,
    }
    data = client.post("/lasso-classifix-output", json=body, headers=auth).json()
    assert data["verdict"] is True
    assert data["transformed"] is False
    assert data["result"]["choices"][0]["message"]["content"] == content


def test_classifix_block_finding_denies(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BLOCK finding must deny on a mutate rail even when it carries a mask
    span. Spans are only applied for AUTO_MASKING, so a BLOCK finding is never
    redacted in place — it must not pass through unmasked with verdict=true."""
    content = f"My email is {EMAIL}"
    _mock_lasso(monkeypatch, _classifix_with_span("user", content, action="BLOCK"))
    body = {
        "requestBody": {"messages": [{"role": "user", "content": content}]},
        "context": CTX,
    }
    data = client.post("/lasso-classifix", json=body, headers=auth).json()
    assert data["verdict"] is False
    assert data["transformed"] is False


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


# --- Agent identity (optional agentId / agentName attribution) --------------

_PROMPT_BODY = {"requestBody": {"messages": [{"role": "user", "content": "hi"}]}, "context": CTX}


def test_agent_identity_from_env(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LASSO_AGENT_ID", "agent-42")
    monkeypatch.setenv("LASSO_AGENT_NAME", "Support Bot")
    sent = _mock_lasso_capture(monkeypatch, _CLEAN)
    client.post("/lasso-classify", json=_PROMPT_BODY, headers=auth)
    assert sent["agentId"] == "agent-42"
    assert sent["agentName"] == "Support Bot"


def test_agent_identity_omitted_when_unset(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config, no metadata, no env -> the fields must not appear at all."""
    sent = _mock_lasso_capture(monkeypatch, _CLEAN)
    client.post("/lasso-classify", json=_PROMPT_BODY, headers=auth)
    assert "agentId" not in sent
    assert "agentName" not in sent


def test_agent_identity_metadata_overrides_env(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-request gateway metadata wins over the static deploy env default."""
    monkeypatch.setenv("LASSO_AGENT_ID", "env-agent")
    monkeypatch.setenv("LASSO_AGENT_NAME", "Env Bot")
    sent = _mock_lasso_capture(monkeypatch, _CLEAN)
    body = {
        "requestBody": {"messages": [{"role": "user", "content": "hi"}]},
        "context": {
            **CTX,
            "metadata": {"lasso-agent-id": "req-agent", "lasso-agent-name": "Req Bot"},
        },
    }
    client.post("/lasso-classify", json=body, headers=auth)
    assert sent["agentId"] == "req-agent"
    assert sent["agentName"] == "Req Bot"


def test_agent_identity_metadata_overrides_config(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-request metadata is the most specific source and beats Config JSON."""
    sent = _mock_lasso_capture(monkeypatch, _CLEAN)
    body = {
        "requestBody": {"messages": [{"role": "user", "content": "hi"}]},
        "context": {**CTX, "metadata": {"agent_id": "meta-agent"}},
        "config": {"agentId": "config-agent"},
    }
    client.post("/lasso-classify", json=body, headers=auth)
    assert sent["agentId"] == "meta-agent"


def test_agent_identity_config_overrides_env(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Middle rung: with no per-request metadata, Config JSON beats the deploy env."""
    monkeypatch.setenv("LASSO_AGENT_ID", "env-agent")
    sent = _mock_lasso_capture(monkeypatch, _CLEAN)
    body = {**_PROMPT_BODY, "config": {"agentId": "config-agent"}}
    client.post("/lasso-classify", json=body, headers=auth)
    assert sent["agentId"] == "config-agent"


def test_agent_id_without_agent_name(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two fields resolve independently; agentId alone is a valid payload."""
    sent = _mock_lasso_capture(monkeypatch, _CLEAN)
    body = {**_PROMPT_BODY, "config": {"agentId": "agent-42"}}
    client.post("/lasso-classify", json=body, headers=auth)
    assert sent["agentId"] == "agent-42"
    assert "agentName" not in sent


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("   ", "blank after strip"),
        ("a" * 129, "over the 128-char limit"),
        ("agent\x01id", "Unicode control character (Cc)"),
        ("agent\u200bid", "Unicode format character (Cf)"),
    ],
)
def test_invalid_agent_id_dropped(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    reason: str,
) -> None:
    """Lasso 400s the whole request on a malformed agentId, which would lose all
    scanning for that call. Drop the value and scan anyway."""
    sent = _mock_lasso_capture(monkeypatch, _CLEAN)
    body = {**_PROMPT_BODY, "config": {"agentId": value}}
    data = client.post("/lasso-classify", json=body, headers=auth).json()
    assert data["verdict"] is True, reason
    assert "agentId" not in sent


def test_invalid_metadata_falls_back_to_config(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution takes the first *usable* candidate. An unusable per-request
    value falls through to the next source rather than dropping identity
    altogether — the same as if metadata had not carried the key at all."""
    sent = _mock_lasso_capture(monkeypatch, _CLEAN)
    body = {
        "requestBody": {"messages": [{"role": "user", "content": "hi"}]},
        "context": {**CTX, "metadata": {"agent_id": "a" * 129}},
        "config": {"agentId": "config-agent"},
    }
    client.post("/lasso-classify", json=body, headers=auth)
    assert sent["agentId"] == "config-agent"


def test_agent_identity_sent_on_mutate_output_rail(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every rail shares _invoke_lasso, so the output mutate rail carries it too."""
    monkeypatch.setenv("LASSO_AGENT_ID", "agent-42")
    sent = _mock_lasso_capture(monkeypatch, _CLEAN)
    body = {
        "requestBody": {"messages": [{"role": "user", "content": "hi"}]},
        "responseBody": {"choices": [{"message": {"role": "assistant", "content": "hello"}}]},
        "context": CTX,
    }
    client.post("/lasso-classifix-output", json=body, headers=auth)
    assert sent["agentId"] == "agent-42"
    assert sent["messageType"] == "COMPLETION"
