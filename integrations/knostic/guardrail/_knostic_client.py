"""HTTP client for Knostic Prompt Gateway / AI guardrails API.

Knostic's enterprise Prompt Gateway inspects prompts and responses for prompt
injection, oversharing, and sensitive-data leakage, with optional inline masking.
Exact URL paths and response fields are provisioned per tenant — configure via env
or dashboard `config` (see README and docs/DESIGN.md).
"""

from __future__ import annotations

import copy
import logging
import os
import uuid
from typing import Any, Optional

import requests
from fastapi import HTTPException

from entities import (
    InputGuardrailRequest,
    MutateGuardrailResponse,
    OutputGuardrailRequest,
    ValidateGuardrailResponse,
)
from guardrail._helpers import first_assistant_text, last_user_text

logger = logging.getLogger(__name__)

# Defaults — override per tenant via KNOSTIC_* env or dashboard config.
DEFAULT_API_BASE = "https://api.knostic.ai"
DEFAULT_INSPECT_PATH = "/v1/guardrails/inspect"
DEFAULT_SANITIZE_PATH = "/v1/guardrails/sanitize"
DEFAULT_TIMEOUT = 15.0
DEFAULT_AUTH_HEADER = "Authorization"
DEFAULT_AUTH_SCHEME = "Bearer"

INVALID_API_KEY_MESSAGE = (
    "Invalid Knostic API key. Verify config.credentials.apiKey or KNOSTIC_API_KEY."
)

_INVALID_API_KEY_PHRASES = (
    "invalid api key",
    "invalid token",
    "authentication failed",
    "unauthorized",
    "forbidden",
    "access denied",
)

_BLOCK_ACTIONS = frozenset({"block", "deny", "reject", "blocked", "denied"})
_ALLOW_ACTIONS = frozenset({"allow", "pass", "permit", "allowed", "passed"})


class KnosticApiError(Exception):
    """Raised when the Knostic API returns a non-success response or malformed payload."""

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


def _resolve_api_key(config: Optional[dict[str, Any]]) -> str:
    api_key = (
        _get_config_value(config, "credentials", "apiKey")
        or _get_config_value(config, "apiKey")
        or os.getenv("KNOSTIC_API_KEY")
    )
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Knostic API key not configured. Set config.credentials.apiKey or KNOSTIC_API_KEY.",
        )
    return str(api_key).strip()


def _resolve_api_base(config: Optional[dict[str, Any]]) -> str:
    base = _get_config_value(config, "api_base") or os.getenv("KNOSTIC_API_BASE", DEFAULT_API_BASE)
    return str(base).rstrip("/")


def _resolve_inspect_path(config: Optional[dict[str, Any]]) -> str:
    path = (
        _get_config_value(config, "inspect_path")
        or os.getenv("KNOSTIC_INSPECT_PATH", DEFAULT_INSPECT_PATH)
    )
    path = str(path)
    return path if path.startswith("/") else f"/{path}"


def _resolve_sanitize_path(config: Optional[dict[str, Any]]) -> str:
    path = (
        _get_config_value(config, "sanitize_path")
        or os.getenv("KNOSTIC_SANITIZE_PATH", DEFAULT_SANITIZE_PATH)
    )
    path = str(path)
    return path if path.startswith("/") else f"/{path}"


def _resolve_timeout(config: Optional[dict[str, Any]]) -> float:
    timeout = _get_config_value(config, "timeout", default=DEFAULT_TIMEOUT)
    return float(timeout)


def _resolve_policy_id(config: Optional[dict[str, Any]]) -> Optional[str]:
    policy = _get_config_value(config, "policyId") or _get_config_value(config, "policy_id")
    if policy:
        return str(policy)
    env_policy = os.getenv("KNOSTIC_POLICY_ID", "").strip()
    return env_policy or None


def _resolve_session_id(request: InputGuardrailRequest | OutputGuardrailRequest) -> str:
    session_id = _get_config_value(request.config, "sessionId")
    if session_id:
        return str(session_id)
    metadata = request.context.metadata or {}
    for key in ("session_id", "sessionId", "knostic-session-id", "conversation_id"):
        if metadata.get(key):
            return str(metadata[key])
    return str(uuid.uuid4())


def _resolve_user_id(request: InputGuardrailRequest | OutputGuardrailRequest) -> Optional[str]:
    user_id = _get_config_value(request.config, "userId")
    if user_id:
        return str(user_id)
    user = request.context.user or {}
    for key in ("subjectSlug", "subjectId", "email"):
        if user.get(key):
            return str(user[key])
    return None


def _extract_messages_from_request_body(body: dict[str, Any]) -> list[dict[str, str]]:
    messages = body.get("messages", [])
    result: list[dict[str, str]] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") is not None and msg.get("content") is not None:
            result.append({"role": str(msg["role"]), "content": str(msg["content"])})
    return result


def _extract_messages_from_response_body(body: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for choice in body.get("choices", []):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message", {})
        if isinstance(message, dict) and message.get("content") is not None:
            role = message.get("role", "assistant")
            messages.append({"role": str(role), "content": str(message["content"])})
    return messages


def _build_knostic_payload(
    messages: list[dict[str, str]],
    *,
    message_type: str,
    session_id: str,
    user_id: Optional[str],
    policy_id: Optional[str],
    model: Optional[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messages": messages,
        "messageType": message_type,
        "sessionId": session_id,
    }
    if user_id:
        payload["userId"] = user_id
    if policy_id:
        payload["policyId"] = policy_id
    if model:
        payload["model"] = model
    return payload


def _parse_error_body(response: requests.Response) -> Optional[dict[str, Any]]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


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


def _knostic_http_error(response: requests.Response) -> KnosticApiError:
    body_text = response.text or ""
    body_json = _parse_error_body(response)
    if _body_indicates_invalid_api_key(response.status_code, body_text, body_json):
        return KnosticApiError(INVALID_API_KEY_MESSAGE, status_code=401)
    if body_json:
        for field in ("message", "detail", "error", "description"):
            value = body_json.get(field)
            if isinstance(value, str) and value.strip():
                return KnosticApiError(
                    f"Knostic API error: {value.strip()}",
                    status_code=502 if response.status_code >= 500 else response.status_code,
                )
    if body_text.strip():
        return KnosticApiError(
            f"Knostic API returned {response.status_code}: {body_text.strip()}",
            status_code=502 if response.status_code >= 500 else response.status_code,
        )
    return KnosticApiError(
        f"Knostic API returned {response.status_code}",
        status_code=502 if response.status_code >= 500 else response.status_code,
    )


def _auth_headers(api_key: str) -> dict[str, str]:
    header_name = os.getenv("KNOSTIC_AUTH_HEADER", DEFAULT_AUTH_HEADER)
    scheme = os.getenv("KNOSTIC_AUTH_SCHEME", DEFAULT_AUTH_SCHEME).strip()
    if header_name.lower() == "authorization" and scheme:
        value = f"{scheme} {api_key}".strip()
    else:
        value = api_key
    return {header_name: value, "Content-Type": "application/json"}


def _call_knostic(
    *,
    path: str,
    payload: dict[str, Any],
    api_key: str,
    api_base: str,
    timeout: float,
) -> dict[str, Any]:
    url = f"{api_base}{path}"
    try:
        response = requests.post(url, json=payload, headers=_auth_headers(api_key), timeout=timeout)
    except requests.RequestException as exc:
        logger.error("Knostic API request failed: %s", exc)
        raise KnosticApiError(f"Failed to connect to Knostic API: {exc}") from exc

    if not response.ok:
        logger.error("Knostic API error %s: %s", response.status_code, response.text)
        raise _knostic_http_error(response)

    try:
        data = response.json()
    except ValueError as exc:
        raise KnosticApiError(f"Invalid JSON from Knostic API: {exc}") from exc

    if not isinstance(data, dict):
        raise KnosticApiError("Knostic API response must be a JSON object")
    return data


def _normalize_action(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip().lower()


def _finding_blocks(finding: dict[str, Any]) -> bool:
    action = _normalize_action(finding.get("action") or finding.get("decision") or finding.get("verdict"))
    if action in _BLOCK_ACTIONS:
        return True
    severity = str(finding.get("severity", "")).lower()
    if severity in ("critical", "high") and action not in _ALLOW_ACTIONS:
        if finding.get("blocked") is True or finding.get("block") is True:
            return True
    return False


def _collect_block_reasons(knostic_response: dict[str, Any]) -> tuple[bool, str]:
    details: list[str] = []

    top_action = _normalize_action(
        knostic_response.get("action")
        or knostic_response.get("decision")
        or knostic_response.get("result")
    )
    if top_action in _BLOCK_ACTIONS or knostic_response.get("allowed") is False:
        reason = knostic_response.get("reason") or knostic_response.get("message") or top_action or "blocked"
        return True, str(reason)

    for key in ("violations", "findings", "issues", "policyViolations"):
        items = knostic_response.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if _finding_blocks(item):
                name = item.get("name") or item.get("type") or item.get("rule") or "violation"
                details.append(str(name))

    if details:
        return True, "; ".join(details)

    if knostic_response.get("blocked") is True or knostic_response.get("block") is True:
        return True, str(knostic_response.get("reason") or knostic_response.get("message") or "blocked")

    return False, ""


def _validate_response(knostic_response: dict[str, Any], *, rail_label: str) -> ValidateGuardrailResponse:
    blocked, detail = _collect_block_reasons(knostic_response)
    if blocked:
        return ValidateGuardrailResponse(
            verdict=False,
            message=f"Knostic {rail_label}: {detail}",
        )
    return ValidateGuardrailResponse(verdict=True)


def _masked_messages(knostic_response: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    for key in ("messages", "maskedMessages", "sanitizedMessages", "resultMessages"):
        value = knostic_response.get(key)
        if isinstance(value, list) and value:
            return value
    result = knostic_response.get("result")
    if isinstance(result, dict):
        for key in ("messages", "maskedMessages", "sanitizedMessages"):
            value = result.get(key)
            if isinstance(value, list) and value:
                return value
    return None


def _apply_masked_messages(
    body: dict[str, Any],
    masked_messages: list[dict[str, Any]],
    *,
    is_output: bool,
) -> tuple[dict[str, Any], bool]:
    updated = copy.deepcopy(body)
    if is_output:
        choices = updated.get("choices", [])
        if not choices or not masked_messages:
            return updated, False
        choice = choices[0]
        if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
            choice["message"]["content"] = masked_messages[0].get("content", choice["message"].get("content"))
            return updated, True
        return updated, False

    original = updated.get("messages", [])
    if not isinstance(original, list) or not masked_messages:
        return updated, False

    if len(masked_messages) != len(original):
        updated["messages"] = masked_messages
        return updated, True

    content_changed = any(
        isinstance(orig, dict)
        and isinstance(masked, dict)
        and str(orig.get("content", "")) != str(masked.get("content", ""))
        for orig, masked in zip(original, masked_messages)
    )
    if content_changed:
        updated["messages"] = masked_messages
        return updated, True
    return updated, False


def _mutate_response(
    body: dict[str, Any],
    knostic_response: dict[str, Any],
    *,
    is_output: bool,
    rail_label: str,
) -> MutateGuardrailResponse:
    blocked, detail = _collect_block_reasons(knostic_response)
    if blocked:
        return MutateGuardrailResponse(verdict=False, transformed=False, result=body)

    result = copy.deepcopy(body)
    transformed = False
    masked = _masked_messages(knostic_response)
    if masked:
        result, transformed = _apply_masked_messages(result, masked, is_output=is_output)

    sanitized_text = knostic_response.get("sanitizedText") or knostic_response.get("redactedText")
    if not transformed and isinstance(sanitized_text, str) and sanitized_text:
        if is_output:
            choices = result.get("choices", [])
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    message["content"] = sanitized_text
                    transformed = True
        else:
            messages = result.get("messages", [])
            if isinstance(messages, list) and messages:
                last = messages[-1]
                if isinstance(last, dict):
                    last["content"] = sanitized_text
                    transformed = True

    if transformed:
        return MutateGuardrailResponse(verdict=True, transformed=True, result=result)

    return MutateGuardrailResponse(verdict=True, transformed=False, result=result)


def _handle_knostic_error(exc: Exception) -> None:
    if isinstance(exc, KnosticApiError):
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    logger.exception("Unexpected Knostic guardrail error")
    raise HTTPException(status_code=500, detail=f"Knostic guardrail error: {exc}") from exc


def _invoke_knostic(
    request: InputGuardrailRequest | OutputGuardrailRequest,
    *,
    path: str,
    message_type: str,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    if not messages:
        return {"action": "allow"}

    config = request.config
    api_key = _resolve_api_key(config)
    api_base = _resolve_api_base(config)
    timeout = _resolve_timeout(config)
    session_id = _resolve_session_id(request)
    user_id = _resolve_user_id(request)
    policy_id = _resolve_policy_id(config)
    model = None
    if isinstance(request, InputGuardrailRequest):
        model = request.requestBody.get("model")
    elif isinstance(request, OutputGuardrailRequest):
        model = request.requestBody.get("model")

    payload = _build_knostic_payload(
        messages,
        message_type=message_type,
        session_id=session_id,
        user_id=user_id,
        policy_id=policy_id,
        model=str(model) if model else None,
    )
    return _call_knostic(
        path=path,
        payload=payload,
        api_key=api_key,
        api_base=api_base,
        timeout=timeout,
    )


def knostic_prompt_inspect_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    """Validate user prompts via Knostic inspect (prompt injection, oversharing, DLP)."""
    try:
        messages = request.requestBody.get("messages") or []
        if not last_user_text(messages if isinstance(messages, list) else []):
            return ValidateGuardrailResponse(verdict=True)
        extracted = _extract_messages_from_request_body(request.requestBody)
        path = _resolve_inspect_path(request.config)
        response = _invoke_knostic(
            request, path=path, message_type="PROMPT", messages=extracted
        )
        return _validate_response(response, rail_label="prompt-inspect-input")
    except HTTPException:
        raise
    except Exception as exc:
        _handle_knostic_error(exc)


def knostic_prompt_inspect_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    """Validate model completions via Knostic inspect."""
    try:
        choices = request.responseBody.get("choices") or []
        if not first_assistant_text(choices if isinstance(choices, list) else []):
            return ValidateGuardrailResponse(verdict=True)
        extracted = _extract_messages_from_response_body(request.responseBody)
        path = _resolve_inspect_path(request.config)
        response = _invoke_knostic(
            request, path=path, message_type="COMPLETION", messages=extracted
        )
        return _validate_response(response, rail_label="prompt-inspect-output")
    except HTTPException:
        raise
    except Exception as exc:
        _handle_knostic_error(exc)


def knostic_prompt_sanitize_input(request: InputGuardrailRequest) -> MutateGuardrailResponse:
    """Mask or block sensitive prompt content via Knostic sanitize."""
    try:
        body = copy.deepcopy(request.requestBody)
        messages = body.get("messages") or []
        if not last_user_text(messages if isinstance(messages, list) else []):
            return MutateGuardrailResponse(verdict=True, transformed=False, result=body)
        extracted = _extract_messages_from_request_body(body)
        path = _resolve_sanitize_path(request.config)
        response = _invoke_knostic(
            request, path=path, message_type="PROMPT", messages=extracted
        )
        return _mutate_response(body, response, is_output=False, rail_label="prompt-sanitize-input")
    except HTTPException:
        raise
    except Exception as exc:
        _handle_knostic_error(exc)


def knostic_prompt_sanitize_output(request: OutputGuardrailRequest) -> MutateGuardrailResponse:
    """Mask or block sensitive completion content via Knostic sanitize."""
    try:
        body = copy.deepcopy(request.responseBody)
        choices = body.get("choices") or []
        if not first_assistant_text(choices if isinstance(choices, list) else []):
            return MutateGuardrailResponse(verdict=True, transformed=False, result=body)
        extracted = _extract_messages_from_response_body(body)
        path = _resolve_sanitize_path(request.config)
        response = _invoke_knostic(
            request, path=path, message_type="COMPLETION", messages=extracted
        )
        return _mutate_response(body, response, is_output=True, rail_label="prompt-sanitize-output")
    except HTTPException:
        raise
    except Exception as exc:
        _handle_knostic_error(exc)
