"""Thin client for the Onyx AI Guard /simple API.

Confirmed against Onyx's integration guide:

  * Policy token (API key) is embedded in the URL PATH — that is the auth.
    The only header sent is Content-Type.
  * Endpoint is ``POST .../guard/evaluate/v1/{key}/simple``.
  * Request body is extracted text only: ``{"user_prompt": "..."}`` on input
    or ``{"response": "..."}`` on output — never both, never the whole body.
  * Response is always HTTP 200 with ``action`` in {allow, block, modify}.
    Block copy lives in ``custom_popup_message``. On ``modify``, validate rails
    fail-safe to block (they cannot apply masking).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import httpx

DEFAULT_TIMEOUT = 10.0
_KNOWN_ACTIONS = frozenset({"allow", "block", "modify"})


class OnyxClientError(Exception):
    """Onyx call failed. Message must never include the evaluate URL (policy token)."""


@dataclass
class OnyxEvaluation:
    action: str  # allow | block | modify
    custom_popup_message: str | None = None


def resolve_settings(config: dict | None) -> tuple[str, str, float]:
    """Resolve (api_key, api_base, timeout) from the dashboard Config JSON, then env.

    Per-request precedence: config.credentials.apiKey / config.api_base override the
    ONYX_API_KEY / ONYX_API_BASE env vars. Mirrors the Lasso integration's pattern.
    """
    cfg = config or {}
    creds = cfg.get("credentials") or {}
    api_key = (creds.get("apiKey") or os.environ.get("ONYX_API_KEY", "")).strip()
    # No soft-default: bare https://ai-guard.onyx.security is not routed (404s).
    # Callers must set ONYX_API_BASE or config.api_base to the tenant host.
    api_base = (cfg.get("api_base") or os.environ.get("ONYX_API_BASE") or "").strip()
    timeout = float(cfg.get("timeout") or os.environ.get("ONYX_TIMEOUT") or DEFAULT_TIMEOUT)
    return api_key, api_base, timeout


async def evaluate(
    *,
    api_base: str,
    api_key: str,
    text: str,
    direction: Literal["input", "output"],
    timeout: float = DEFAULT_TIMEOUT,
) -> OnyxEvaluation:
    """POST extracted text to Onyx /simple and return the normalized verdict.

    Raises OnyxClientError on network errors, non-2xx responses, or HTTP 200
    bodies without a usable ``action`` in {allow, block, modify}. The rail
    handler converts those into a wrapper 5xx so the gateway's `Fail on error`
    policy — not this wrapper — decides pass vs block on an outage.

    Never re-raise raw httpx errors: HTTPStatusError embeds the request URL, and
    the URL path contains the policy token.
    """
    url = f"{api_base.rstrip('/')}/guard/evaluate/v1/{api_key}/simple"
    body = {"user_prompt": text} if direction == "input" else {"response": text}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0)) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        # Do not chain: __cause__ would still carry the URL with the policy token.
        raise OnyxClientError(f"Onyx returned HTTP {e.response.status_code}") from None
    except httpx.RequestError:
        raise OnyxClientError("Onyx request failed") from None
    except ValueError as e:
        # resp.json() decode failure — no URL in the message, safe to surface type.
        raise OnyxClientError(f"Onyx response was not valid JSON: {e}") from None

    if not isinstance(data, dict):
        raise OnyxClientError("Onyx response was not a JSON object")

    raw_action = data.get("action")
    if raw_action is None or (isinstance(raw_action, str) and not raw_action.strip()):
        # Do not default to allow — that would skip policy on unexpected payloads.
        raise OnyxClientError("Onyx response missing action")
    action = str(raw_action).strip().lower()
    if action not in _KNOWN_ACTIONS:
        raise OnyxClientError(f"Onyx returned unrecognized action: {action}")

    popup = data.get("custom_popup_message")
    return OnyxEvaluation(
        action=action,
        custom_popup_message=str(popup).strip() if popup else None,
    )


def format_block_message(direction: str, custom_popup_message: str | None) -> str:
    detail = (custom_popup_message or "").strip() or "blocked by policy"
    return f"Onyx AI Guard ({direction}): {detail}"


def is_allow(action: str) -> bool:
    """True only for ``allow``. ``block`` and ``modify`` both deny on validate rails."""
    return action == "allow"
