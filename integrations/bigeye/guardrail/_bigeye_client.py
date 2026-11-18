"""Shared async HTTP client for Bigeye's access-decision endpoint.

One keep-alive client per process. Redirects are not followed, so the API key
only goes to the allowlisted host. The Authorization header is never logged.
No retries here: the gateway owns retries, and only for 429/503, which this
module never returns.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from guardrail._payload_log import payload_logging_enabled
from guardrail._settings import BigeyeSettings

log = logging.getLogger("bigeye-guardrails-tfy")

ACCESS_DECISION_PATH = "/api/v1/access-decision"
MAX_CONNECTIONS = 100
MAX_KEEPALIVE_CONNECTIONS = 20
KEEPALIVE_EXPIRY_S = 60
MAX_LOGGED_ERROR_CHARS = 200
MAX_SCANNED_ERROR_CHARS = 2000
AUTH_REJECTED_STATUSES = (401, 403)
# Bigeye answers a key it cannot decode (wrong auth_scheme, or a mangled key) with a 500 that mentions base64.
KEY_DECODE_STATUS = 500
KEY_DECODE_MARKER = "base64"
UPSTREAM_ERROR_STATUS = 502
UPSTREAM_TIMEOUT_STATUS = 504

_client: Optional[httpx.AsyncClient] = None


class BigeyeCallError(Exception):
    """Bigeye could not give a decision. Only 502 or 504: the gateway retries 429 and 503."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=KEEPALIVE_EXPIRY_S,
            ),
            follow_redirects=False,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _error_message(response: httpx.Response) -> str:
    """Bigeye's runtimeError ``message``, truncated. Only with LOG_PAYLOADS, since it can quote the SQL."""
    if not payload_logging_enabled():
        return "(set LOG_PAYLOADS=true to log Bigeye's error message)"
    try:
        body = response.json()
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    return message[:MAX_LOGGED_ERROR_CHARS] if isinstance(message, str) else ""


def _is_key_decode_error(response: httpx.Response) -> bool:
    return (
        response.status_code == KEY_DECODE_STATUS
        and KEY_DECODE_MARKER in response.text[:MAX_SCANNED_ERROR_CHARS].lower()
    )


async def post_access_decision(settings: BigeyeSettings, body: dict, api_key: str) -> tuple:
    """POST the decision request. Returns ``(response_json, latency_ms)`` or raises BigeyeCallError."""
    headers = {"Authorization": f"{settings.auth_scheme} {api_key}", "Accept": "application/json"}
    started = time.perf_counter()
    try:
        response = await get_client().post(
            settings.base_url + ACCESS_DECISION_PATH,
            json=body,
            headers=headers,
            timeout=settings.timeout_ms / 1000,
        )
    except httpx.TimeoutException:
        log.warning("Bigeye did not answer within %d ms", settings.timeout_ms)
        raise BigeyeCallError(UPSTREAM_TIMEOUT_STATUS, f"Bigeye timed out after {settings.timeout_ms} ms") from None
    except httpx.HTTPError as exc:
        log.error("Bigeye request failed: %s", type(exc).__name__)
        raise BigeyeCallError(UPSTREAM_ERROR_STATUS, "could not reach Bigeye") from None
    latency_ms = round((time.perf_counter() - started) * 1000)

    if response.status_code in AUTH_REJECTED_STATUSES:
        log.error(
            "Bigeye rejected the API key (check auth_scheme and workspace access): HTTP %d",
            response.status_code,
        )
        raise BigeyeCallError(UPSTREAM_ERROR_STATUS, f"Bigeye returned HTTP {response.status_code}")
    if _is_key_decode_error(response):
        log.error(
            "Bigeye could not decode the API key (HTTP %d mentioning base64): use auth_scheme apikey "
            "for Bigeye personal API keys, and check the key was copied whole",
            response.status_code,
        )
        raise BigeyeCallError(UPSTREAM_ERROR_STATUS, f"Bigeye returned HTTP {response.status_code}")
    if not response.is_success:
        log.error("Bigeye returned HTTP %d: %s", response.status_code, _error_message(response))
        raise BigeyeCallError(UPSTREAM_ERROR_STATUS, f"Bigeye returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        log.error("Bigeye returned a body that is not JSON (HTTP %d)", response.status_code)
        raise BigeyeCallError(UPSTREAM_ERROR_STATUS, "Bigeye returned an unparseable body") from None
    if not isinstance(data, dict):
        log.error("Bigeye returned JSON that is not an object")
        raise BigeyeCallError(UPSTREAM_ERROR_STATUS, "Bigeye returned an unparseable body")
    return data, latency_ms
