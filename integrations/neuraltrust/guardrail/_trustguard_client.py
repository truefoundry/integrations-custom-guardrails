"""Thin async client for NeuralTrust TrustGuard `POST /v1/evaluate`.

Reference: https://docs.neuraltrust.ai/trustguard/api/evaluate.md
           https://docs.neuraltrust.ai/integrations/rest.md

Contract notes (from the NeuralTrust docs):
- Auth: `Authorization: Bearer <collector-api-key>` (tgk_...). The collector is
  resolved from the key; never send collector_id in the body.
- Strict decoding: unknown TOP-LEVEL fields -> 400. Allowed top-level fields:
  payload, direction, protocol, session_id, consumer_id, attributes
  (+ gateway_id / collector_key for service tokens only).
- A detection, including `block`, is HTTP 200. Non-2xx is never a verdict.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger("neuraltrust-guardrails-tfy.client")

EVALUATE_PATH = "/v1/evaluate"


class TrustGuardUnavailable(Exception):
    """TrustGuard could not produce a verdict (network, timeout, 4xx/5xx).

    The wrapper turns this into an HTTP 5xx so the TrueFoundry AI Gateway's
    Enforcing Strategy (Enforce / Enforce But Ignore On Error / Audit) decides
    whether to fail closed or open. It is NEVER mapped to `verdict: true` here.
    """


DEFAULT_BASE_URL = "https://trustguard.neuraltrust.ai"  # SaaS origin from the NeuralTrust docs
DEFAULT_TIMEOUT_SECONDS = 3.5  # below the AI Gateway's ~5 s custom-guardrail wait
MAX_TIMEOUT_SECONDS = 4.5


def resolve_timeout(config: Optional[dict] = None) -> float:
    """Timeout for one /v1/evaluate call.

    Precedence: dashboard Config `timeout_seconds` > env NEURALTRUST_TIMEOUT_SECONDS > 3.5 s.
    Invalid or non-positive values fall back to the default; values are capped at 4.5 s so the
    wrapper always answers (with a 5xx) before the gateway gives up on it (~5 s).
    """
    raw: Any = None
    if config and config.get("timeout_seconds") is not None:
        raw = config.get("timeout_seconds")
    else:
        raw = os.environ.get("NEURALTRUST_TIMEOUT_SECONDS")
    try:
        value = float(raw) if raw is not None and str(raw).strip() != "" else DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        log.warning("invalid timeout %r, using default %.1fs", raw, DEFAULT_TIMEOUT_SECONDS)
        value = DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        value = DEFAULT_TIMEOUT_SECONDS
    return min(value, MAX_TIMEOUT_SECONDS)


def _settings() -> tuple[str, str]:
    base = (os.environ.get("NEURALTRUST_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")
    key = os.environ.get("NEURALTRUST_API_KEY", "").strip()
    return base, key


_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


async def evaluate(
    *,
    payload: dict[str, Any],
    direction: str,
    protocol: str = "llm",
    session_id: Optional[str] = None,
    consumer_id: Optional[str] = None,
    attributes: Optional[dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Call TrustGuard and return the parsed verdict JSON.

    Raises TrustGuardUnavailable for anything that is not a 200 verdict.
    """
    base, key = _settings()
    if not key:
        raise TrustGuardUnavailable("NEURALTRUST_API_KEY not configured")
    if timeout is None:
        timeout = resolve_timeout()

    body: dict[str, Any] = {"payload": payload, "direction": direction, "protocol": protocol}
    if session_id:
        body["session_id"] = session_id
    if consumer_id:
        body["consumer_id"] = consumer_id
    if attributes:
        body["attributes"] = attributes

    try:
        resp = await _http().post(
            f"{base}{EVALUATE_PATH}",
            json=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise TrustGuardUnavailable(f"timeout after {timeout:g}s") from exc
    except httpx.HTTPError as exc:
        raise TrustGuardUnavailable(f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code != 200:
        # 400 malformed / 401-403 auth / 402 usage exhausted / 429 quota / 5xx.
        raise TrustGuardUnavailable(f"TrustGuard HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise TrustGuardUnavailable("TrustGuard returned non-JSON body") from exc
    if not isinstance(data, dict) or "status" not in data:
        raise TrustGuardUnavailable("TrustGuard response missing `status`")
    return data
