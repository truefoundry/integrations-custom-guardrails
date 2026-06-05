"""HTTP client for Arthur GenAI Engine stateless validation API."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://engine.platform.arthur.ai"
DEFAULT_TIMEOUT = 30.0
VALIDATE_PATH = "/api/v2/validate"

FAIL_RESULTS = frozenset({"Fail"})
PASS_RESULTS = frozenset({"Pass"})
UNCERTAIN_RESULTS = frozenset(
    {"Skipped", "Unavailable", "Partially Unavailable", "Model Not Available"}
)

INVALID_API_KEY_MESSAGE = (
    "Invalid Arthur API key. Verify config.credentials.apiKey or ARTHUR_API_KEY."
)

_INVALID_API_KEY_PHRASES = (
    "invalid api key",
    "invalid token",
    "authentication failed",
    "unauthorized",
    "forbidden",
    "access denied",
)


class ArthurApiError(Exception):
    """Raised when the Arthur API returns a non-success response."""

    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _get_config_value(config: Optional[dict[str, Any]], *keys: str, default: Any = None) -> Any:
    if not config:
        return default
    value: Any = config
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return value if value is not None else default


def resolve_api_key(config: Optional[dict[str, Any]]) -> str:
    api_key = (
        _get_config_value(config, "credentials", "apiKey")
        or _get_config_value(config, "apiKey")
        or os.getenv("ARTHUR_API_KEY", "").strip()
    )
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Arthur API key not configured. Set config.credentials.apiKey or ARTHUR_API_KEY.",
        )
    return api_key


def resolve_api_base(config: Optional[dict[str, Any]]) -> str:
    base = _get_config_value(config, "api_base") or os.getenv("ARTHUR_API_BASE", DEFAULT_API_BASE)
    return base.rstrip("/")


def resolve_timeout(config: Optional[dict[str, Any]]) -> float:
    timeout = _get_config_value(config, "timeout", default=DEFAULT_TIMEOUT)
    return float(timeout)


def resolve_fail_closed_on_unavailable(config: Optional[dict[str, Any]]) -> bool:
    return bool(_get_config_value(config, "fail_closed_on_unavailable", default=False))


def _body_indicates_invalid_api_key(status_code: int, body_text: str, body_json: Optional[dict[str, Any]]) -> bool:
    if status_code in (401, 403):
        return True

    haystack = body_text.lower()
    if any(phrase in haystack for phrase in _INVALID_API_KEY_PHRASES):
        return True

    if not body_json:
        return False

    for field in ("message", "error", "detail", "description", "title"):
        value = body_json.get(field)
        if isinstance(value, str) and any(phrase in value.lower() for phrase in _INVALID_API_KEY_PHRASES):
            return True

    return False


def _parse_error_body(response: httpx.Response) -> Optional[dict[str, Any]]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _arthur_http_error(response: httpx.Response) -> ArthurApiError:
    body_text = response.text or ""
    body_json = _parse_error_body(response)

    if _body_indicates_invalid_api_key(response.status_code, body_text, body_json):
        return ArthurApiError(INVALID_API_KEY_MESSAGE, status_code=401)

    if body_json:
        for field in ("message", "detail", "error", "description"):
            value = body_json.get(field)
            if isinstance(value, str) and value.strip():
                return ArthurApiError(
                    f"Arthur API error: {value.strip()}",
                    status_code=502 if response.status_code >= 500 else response.status_code,
                )

    if body_text.strip():
        return ArthurApiError(
            f"Arthur API returned {response.status_code}: {body_text.strip()}",
            status_code=502 if response.status_code >= 500 else response.status_code,
        )

    return ArthurApiError(
        f"Arthur API returned {response.status_code}",
        status_code=502 if response.status_code >= 500 else response.status_code,
    )


def resolve_checks(config: Optional[dict[str, Any]], *, for_prompt: bool) -> list[dict[str, Any]]:
    """Return checks from TF config filtered to the current hook direction."""
    raw_checks = _get_config_value(config, "checks", default=[])
    if not isinstance(raw_checks, list) or not raw_checks:
        raise HTTPException(
            status_code=500,
            detail="Arthur checks not configured. Set config.checks in the Custom Guardrail Config.",
        )

    field = "apply_to_prompt" if for_prompt else "apply_to_response"
    filtered = [check for check in raw_checks if isinstance(check, dict) and check.get(field)]
    if not filtered:
        raise HTTPException(
            status_code=500,
            detail=f"No Arthur checks with {field}=true in config.checks.",
        )
    return filtered


def build_validate_payload(
    *,
    checks: list[dict[str, Any]],
    prompt: Optional[str] = None,
    response: Optional[str] = None,
    context: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"checks": checks}
    if prompt is not None:
        payload["prompt"] = prompt
    if response is not None:
        payload["response"] = response
    if context is not None:
        payload["context"] = context
    return payload


def call_arthur_validate(payload: dict[str, Any], config: Optional[dict[str, Any]]) -> dict[str, Any]:
    api_key = resolve_api_key(config)
    api_base = resolve_api_base(config)
    timeout = resolve_timeout(config)
    url = f"{api_base}{VALIDATE_PATH}"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as exc:
        logger.error("Arthur API request failed: %s", exc)
        raise ArthurApiError(f"Failed to connect to Arthur API: {exc}") from exc

    if not response.is_success:
        logger.error("Arthur API error %s: %s", response.status_code, response.text)
        raise _arthur_http_error(response)

    try:
        return response.json()
    except ValueError as exc:
        raise ArthurApiError(f"Invalid JSON from Arthur API: {exc}") from exc


def format_failure_message(results: list[dict[str, Any]]) -> str:
    details: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("result") != "Fail":
            continue
        name = result.get("name", "check")
        rule_type = result.get("rule_type", "")
        suffix = f" ({rule_type})" if rule_type else ""
        details.append(f"{name}{suffix}")
    return "; ".join(details)


def map_arthur_response(
    arthur_response: dict[str, Any],
    *,
    fail_closed_on_unavailable: bool,
) -> tuple[bool, Optional[str]]:
    results = arthur_response.get("results", [])
    if not isinstance(results, list):
        raise ArthurApiError("Arthur API response missing results array")

    failures = [result for result in results if isinstance(result, dict) and result.get("result") in FAIL_RESULTS]
    if failures:
        detail = format_failure_message(failures)
        return False, f"Arthur guardrail blocked: {detail}"

    uncertain = [
        result
        for result in results
        if isinstance(result, dict) and result.get("result") in UNCERTAIN_RESULTS
    ]
    if uncertain:
        names = format_failure_message(
            [{"name": r.get("name", "check"), "rule_type": r.get("rule_type", ""), "result": "Fail"} for r in uncertain]
        )
        if fail_closed_on_unavailable:
            return False, f"Arthur guardrail unavailable (fail-closed): {names}"
        logger.info("Arthur checks returned unavailable/skipped results (fail-open): %s", names)

    return True, None


def handle_arthur_error(exc: Exception) -> None:
    if isinstance(exc, ArthurApiError):
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if isinstance(exc, HTTPException):
        raise
    logger.exception("Unexpected Arthur guardrail error")
    raise HTTPException(status_code=500, detail=f"Arthur guardrail error: {exc}") from exc
