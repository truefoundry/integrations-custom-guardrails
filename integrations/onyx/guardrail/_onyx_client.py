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

DEFAULT_API_BASE = "https://ai-guard.onyx.security"
DEFAULT_TIMEOUT = 10.0


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
    api_base = (
        cfg.get("api_base") or os.environ.get("ONYX_API_BASE") or DEFAULT_API_BASE
    ).strip()
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

    Raises httpx.HTTPError on network errors or non-2xx responses from Onyx. The
    rail handler converts those into a wrapper 5xx so the gateway's `Fail on error`
    policy — not this wrapper — decides pass vs block on an outage.
    """
    url = f"{api_base.rstrip('/')}/guard/evaluate/v1/{api_key}/simple"
    body = {"user_prompt": text} if direction == "input" else {"response": text}
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0)) as client:
        resp = await client.post(
            url,
            json=body,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    action = str(data.get("action") or "allow").strip().lower()
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
