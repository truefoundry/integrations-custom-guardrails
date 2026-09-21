"""Thin client for the Onyx AI Guard /truefoundry API.

Onyx's dedicated TrueFoundry endpoint speaks the gateway custom-guardrail
contract directly:

  * URL: ``POST {base}/guard/evaluate/v1/{guard-token}/truefoundry``
  * Guard Token in the URL path is the auth (no Authorization header to Onyx).
  * Request body is the TrueFoundry payload (``requestBody`` / ``responseBody`` /
    ``context`` / ``config``). Presence of ``responseBody`` selects output eval.
  * Response is always HTTP 200 with ``{"verdict": true}`` or
    ``{"verdict": false, "message": "..."}``. Mask/ask map to block.

The recommended production path is for the gateway to call this URL directly
(Auth Data empty). This client exists so the optional local wrapper and tests
can exercise the same contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT = 10.0


class OnyxClientError(Exception):
    """Onyx call failed. Message must never include the evaluate URL (Guard Token)."""


@dataclass
class OnyxEvaluation:
    verdict: bool
    message: str | None = None


def resolve_settings(config: dict | None) -> tuple[str, str, float]:
    """Resolve (guard_token, api_base, timeout) from dashboard Config JSON, then env.

    Per-request precedence: config.credentials.apiKey / config.api_base override
    ONYX_API_KEY / ONYX_API_BASE. No soft-default for api_base: bare
    https://ai-guard.onyx.security is not routed (404s).
    """
    cfg = config or {}
    creds = cfg.get("credentials") or {}
    api_key = (creds.get("apiKey") or os.environ.get("ONYX_API_KEY", "")).strip()
    api_base = (cfg.get("api_base") or os.environ.get("ONYX_API_BASE") or "").strip()
    timeout = float(cfg.get("timeout") or os.environ.get("ONYX_TIMEOUT") or DEFAULT_TIMEOUT)
    return api_key, api_base, timeout


async def evaluate(
    *,
    api_base: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
) -> OnyxEvaluation:
    """POST a TrueFoundry-shaped body to Onyx /truefoundry; return verdict.

    Raises OnyxClientError on network errors, non-2xx, or HTTP 200 bodies without
    a usable boolean ``verdict``. Never re-raise raw httpx errors: HTTPStatusError
    embeds the request URL, and the URL path contains the Guard Token.
    """
    url = f"{api_base.rstrip('/')}/guard/evaluate/v1/{api_key}/truefoundry"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0)) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        raise OnyxClientError(f"Onyx returned HTTP {e.response.status_code}") from None
    except httpx.RequestError:
        raise OnyxClientError("Onyx request failed") from None
    except ValueError as e:
        raise OnyxClientError(f"Onyx response was not valid JSON: {e}") from None

    if not isinstance(data, dict):
        raise OnyxClientError("Onyx response was not a JSON object")

    if "verdict" not in data or not isinstance(data["verdict"], bool):
        raise OnyxClientError("Onyx response missing boolean verdict")

    message = data.get("message")
    return OnyxEvaluation(
        verdict=data["verdict"],
        message=str(message).strip() if message else None,
    )
