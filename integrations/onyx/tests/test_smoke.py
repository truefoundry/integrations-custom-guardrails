"""Smoke tests for the Onyx Security /truefoundry integration.

Boots the FastAPI app in-process via TestClient (optional local forwarder).
Live cases that call Onyx skip unless ``ONYX_API_KEY`` and ``ONYX_API_BASE``
are set.

Run:
    pytest -v tests/

Response contract under test (Onyx /truefoundry = TFY custom-guardrail contract):
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
    not (
        os.environ.get("ONYX_API_KEY", "").strip()
        and os.environ.get("ONYX_API_BASE", "").strip()
    ),
    reason="needs ONYX_API_KEY and ONYX_API_BASE to call Onyx AI Guard",
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


# Onyx /truefoundry requires subjectSlug on context.user (returns
# "could not validate the request" without it).
CTX = {
    "user": {"subjectId": "u1", "subjectType": "user", "subjectSlug": "u1"},
    "metadata": {"request_id": "test-req"},
}


def _input_body(content: str) -> dict:
    return {
        "requestBody": {"model": "gpt-4o", "messages": [{"role": "user", "content": content}]},
        "context": CTX,
        "config": {},
    }


def _output_body(content: str, user_msg: str = "hi") -> dict:
    """Output test: keep blocked keywords out of the input (PDF guidance)."""
    return {
        "requestBody": {"model": "gpt-4o", "messages": [{"role": "user", "content": user_msg}]},
        "responseBody": {"choices": [{"message": {"role": "assistant", "content": content}}]},
        "context": CTX,
        "config": {},
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
    assert "onyx_api_base_configured" in body


def test_missing_api_base_returns_500(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONYX_API_KEY", "test-policy-token")
    monkeypatch.delenv("ONYX_API_BASE", raising=False)
    r = client.post("/onyx-input", headers=auth, json=_input_body("hi"))
    assert r.status_code == 500, r.text
    assert "Onyx API base not configured" in r.text


def test_missing_api_key_returns_500(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONYX_API_KEY", raising=False)
    monkeypatch.setenv("ONYX_API_BASE", "https://tenant.ai-guard.onyx.security")
    r = client.post("/onyx-input", headers=auth, json=_input_body("hi"))
    assert r.status_code == 500, r.text
    assert "Onyx API key not configured" in r.text


def test_onyx_http_error_502_does_not_leak_api_key(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: httpx.HTTPStatusError embeds the request URL (token in path)."""
    import httpx

    secret = "leak-me-onyx-policy-token"
    monkeypatch.setenv("ONYX_API_KEY", secret)
    monkeypatch.setenv("ONYX_API_BASE", "https://tenant.ai-guard.onyx.security")

    real_async_client = httpx.AsyncClient

    class _FakeResponse:
        status_code = 401

        def raise_for_status(self) -> None:
            req = httpx.Request(
                "POST",
                f"https://tenant.ai-guard.onyx.security/guard/evaluate/v1/{secret}/truefoundry",
            )
            raise httpx.HTTPStatusError(
                f"Client error '401 Unauthorized' for url "
                f"'https://tenant.ai-guard.onyx.security/guard/evaluate/v1/{secret}/truefoundry'",
                request=req,
                response=httpx.Response(401, request=req),
            )

        def json(self) -> dict:
            return {}

    class _FakeAsyncClient(real_async_client):
        async def post(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    r = client.post("/onyx-input", headers=auth, json=_input_body("hi"))
    assert r.status_code == 502, r.text
    assert secret not in r.text
    assert "/guard/evaluate/" not in r.text
    assert "Onyx returned HTTP 401" in r.text


@pytest.mark.parametrize(
    "onyx_body,detail_substr",
    [
        ({}, "missing boolean verdict"),
        ({"verdict": None}, "missing boolean verdict"),
        ({"verdict": "allow"}, "missing boolean verdict"),
        ({"message": "x"}, "missing boolean verdict"),
    ],
)
def test_onyx_200_without_usable_verdict_returns_502(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    onyx_body: dict,
    detail_substr: str,
) -> None:
    """HTTP 200 without a boolean verdict must be a 5xx, not a silent allow."""
    import httpx

    monkeypatch.setenv("ONYX_API_KEY", "test-policy-token")
    monkeypatch.setenv("ONYX_API_BASE", "https://tenant.ai-guard.onyx.security")

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return onyx_body

    class _FakeAsyncClient(httpx.AsyncClient):
        async def post(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    r = client.post("/onyx-input", headers=auth, json=_input_body("hi"))
    assert r.status_code == 502, r.text
    assert detail_substr in r.text
    assert r.json().get("verdict") is None


# ---------------------------------------------------------------------------
# Live /truefoundry verdicts (require ONYX_API_KEY + ONYX_API_BASE)
# ---------------------------------------------------------------------------

_SAFE_PROMPT = "What is the weather today?"
# Test each keyword separately (Onyx TrueFoundry integration guide).
_BLOCK_KEYWORDS = ("bradpitt", "fightclub", "norton")


@requires_onyx
def test_benign_input_passes(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post("/onyx-input", headers=auth, json=_input_body(_SAFE_PROMPT))
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] is True


@pytest.mark.parametrize("keyword", _BLOCK_KEYWORDS)
@requires_onyx
def test_policy_violation_input_blocks(
    client: TestClient, auth: dict[str, str], keyword: str
) -> None:
    r = client.post("/onyx-input", headers=auth, json=_input_body(keyword))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] is False
    assert body.get("message")


@requires_onyx
def test_benign_output_passes(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/onyx-output",
        headers=auth,
        json=_output_body("The weather is sunny today."),
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] is True


@pytest.mark.parametrize("keyword", _BLOCK_KEYWORDS)
@requires_onyx
def test_policy_violation_output_blocks(
    client: TestClient, auth: dict[str, str], keyword: str
) -> None:
    # Keep the blocked keyword out of the input so only output evaluation fires.
    r = client.post(
        "/onyx-output",
        headers=auth,
        json=_output_body(
            keyword,
            user_msg=(
                "Reply with only these letters joined together, without spaces: "
                + " ".join(keyword)
            ),
        ),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] is False
    assert body.get("message")
