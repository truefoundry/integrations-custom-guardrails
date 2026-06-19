"""HTTP client for HiddenLayer Runtime Security v1 interactions API."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from guardrail._helpers import bodies_differ

logger = logging.getLogger(__name__)

DEFAULT_REGION = "us"
REGION_ENDPOINTS = {
    "us": {
        "api_base": "https://api.hiddenlayer.ai",
        "auth_base": "https://auth.hiddenlayer.ai",
    },
    "eu": {
        "api_base": "https://api.eu.hiddenlayer.ai",
        "auth_base": "https://auth.eu.hiddenlayer.ai",
    },
}
DEFAULT_TIMEOUT = 10.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 60.0
INTERACTIONS_PATH = "/detection/v1/interactions"
TOKEN_PATH = "/oauth2/token"
DEFAULT_PROVIDER = "truefoundry"
MAX_LOG_BODY_CHARS = 200

MISSING_EVALUATION_MESSAGE = (
    "HiddenLayer guardrail error: API response missing evaluation object"
)

ALLOW_ACTIONS = frozenset({"Allow", "Alert"})
REDACT_ACTIONS = frozenset({"Redact"})
BLOCK_ACTIONS = frozenset({"Block"})
VALIDATE_DENY_ACTIONS = BLOCK_ACTIONS | REDACT_ACTIONS

INVALID_CREDENTIALS_MESSAGE = (
    "Invalid HiddenLayer credentials. Verify config.credentials.clientId/clientSecret "
    "or HIDDENLAYER_CLIENT_ID/HIDDENLAYER_CLIENT_SECRET."
)

FORBIDDEN_MESSAGE = (
    "HiddenLayer API forbidden. Verify HL-Project-Id, credential permissions, and tenant region."
)

_ACTION_ALIASES = {
    "allow": "Allow",
    "alert": "Alert",
    "redact": "Redact",
    "block": "Block",
}

_INVALID_CREDENTIAL_PHRASES = (
    "invalid client",
    "invalid credentials",
    "authentication failed",
    "unauthorized",
    "forbidden",
    "access denied",
    "not authorized",
)

_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {
    "access_token": None,
    "cache_key": None,
}


class HiddenLayerApiError(Exception):
    """Raised when the HiddenLayer API returns a non-success response."""

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


def resolve_region(config: Optional[dict[str, Any]]) -> str:
    region = (
        _get_config_value(config, "region")
        or os.getenv("HIDDENLAYER_REGION", DEFAULT_REGION)
    )
    normalized = str(region).strip().lower()
    if normalized not in REGION_ENDPOINTS:
        raise HTTPException(
            status_code=500,
            detail=f"Unsupported HiddenLayer region '{region}'. Use 'us' or 'eu'.",
        )
    return normalized


def resolve_api_base(config: Optional[dict[str, Any]]) -> str:
    override = _get_config_value(config, "api_base") or os.getenv("HIDDENLAYER_API_BASE", "").strip()
    if override:
        return override.rstrip("/")
    region = resolve_region(config)
    return REGION_ENDPOINTS[region]["api_base"]


def resolve_auth_base(config: Optional[dict[str, Any]]) -> str:
    override = _get_config_value(config, "auth_base") or os.getenv("HIDDENLAYER_AUTH_BASE", "").strip()
    if override:
        return override.rstrip("/")
    region = resolve_region(config)
    return REGION_ENDPOINTS[region]["auth_base"]


def resolve_client_credentials(config: Optional[dict[str, Any]]) -> tuple[str, str]:
    client_id = (
        _get_config_value(config, "credentials", "clientId")
        or _get_config_value(config, "credentials", "client_id")
        or _get_config_value(config, "clientId")
        or _get_config_value(config, "client_id")
        or os.getenv("HIDDENLAYER_CLIENT_ID", "").strip()
    )
    client_secret = (
        _get_config_value(config, "credentials", "clientSecret")
        or _get_config_value(config, "credentials", "client_secret")
        or _get_config_value(config, "clientSecret")
        or _get_config_value(config, "client_secret")
        or os.getenv("HIDDENLAYER_CLIENT_SECRET", "").strip()
    )
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=500,
            detail=(
                "HiddenLayer credentials not configured. Set config.credentials.clientId/clientSecret "
                "or HIDDENLAYER_CLIENT_ID/HIDDENLAYER_CLIENT_SECRET."
            ),
        )
    return client_id, client_secret


def resolve_project_id(config: Optional[dict[str, Any]]) -> Optional[str]:
    project_id = (
        _get_config_value(config, "projectId")
        or _get_config_value(config, "project_id")
        or _get_config_value(config, "hlProjectId")
        or os.getenv("HIDDENLAYER_PROJECT_ID", "").strip()
    )
    return project_id or None


def resolve_provider(config: Optional[dict[str, Any]]) -> str:
    provider = _get_config_value(config, "provider") or os.getenv("HIDDENLAYER_PROVIDER", DEFAULT_PROVIDER)
    return str(provider).strip() or DEFAULT_PROVIDER


def resolve_timeout(config: Optional[dict[str, Any]]) -> float:
    timeout = _get_config_value(config, "timeout")
    if timeout is None:
        timeout = os.getenv("HIDDENLAYER_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT))
    value = float(timeout)
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, value))


def resolve_fail_open_on_unavailable(config: Optional[dict[str, Any]]) -> bool:
    return bool(_get_config_value(config, "fail_open_on_unavailable", default=False))


def _credentials_cache_key(client_id: str, client_secret: str, auth_base: str) -> str:
    return f"{auth_base}:{client_id}:{client_secret}"


def _invalidate_token_cache() -> None:
    _token_cache["access_token"] = None
    _token_cache["cache_key"] = None


def _fetch_access_token(client_id: str, client_secret: str, auth_base: str, timeout: float) -> str:
    url = f"{auth_base}{TOKEN_PATH}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                url,
                params={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
            )
    except httpx.RequestError as exc:
        logger.error("HiddenLayer token request failed: %s", exc)
        raise HiddenLayerApiError(f"Failed to connect to HiddenLayer auth API: {exc}") from exc

    if response.status_code == 401:
        raise HiddenLayerApiError(INVALID_CREDENTIALS_MESSAGE, status_code=401)

    if response.status_code == 403:
        raise HiddenLayerApiError(FORBIDDEN_MESSAGE, status_code=403)

    if not response.is_success:
        raise HiddenLayerApiError(
            f"HiddenLayer auth API returned {response.status_code}: {response.text.strip()}",
            status_code=502 if response.status_code >= 500 else response.status_code,
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise HiddenLayerApiError(f"Invalid JSON from HiddenLayer auth API: {exc}") from exc

    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise HiddenLayerApiError("HiddenLayer auth API response missing access_token")
    return token.strip()


def get_access_token(config: Optional[dict[str, Any]], *, force_refresh: bool = False) -> str:
    client_id, client_secret = resolve_client_credentials(config)
    auth_base = resolve_auth_base(config)
    timeout = resolve_timeout(config)
    cache_key = _credentials_cache_key(client_id, client_secret, auth_base)

    with _token_lock:
        if (
            not force_refresh
            and _token_cache.get("access_token")
            and _token_cache.get("cache_key") == cache_key
        ):
            return _token_cache["access_token"]

    token = _fetch_access_token(client_id, client_secret, auth_base, timeout)

    with _token_lock:
        if (
            not force_refresh
            and _token_cache.get("access_token")
            and _token_cache.get("cache_key") == cache_key
        ):
            return _token_cache["access_token"]
        _token_cache["access_token"] = token
        _token_cache["cache_key"] = cache_key
        return token


def _parse_error_body(response: httpx.Response) -> Optional[dict[str, Any]]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _normalize_action(action: Any) -> str:
    raw = str(action or "Allow").strip()
    return _ACTION_ALIASES.get(raw.lower(), raw)


def _safe_response_snippet(response: httpx.Response, limit: int = MAX_LOG_BODY_CHARS) -> str:
    text = (response.text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... (truncated)"


def _parse_evaluation(response: dict[str, Any]) -> Optional[dict[str, Any]]:
    evaluation = response.get("evaluation")
    return evaluation if isinstance(evaluation, dict) else None


def _body_indicates_invalid_credentials(
    status_code: int,
    body_text: str,
    body_json: Optional[dict[str, Any]],
) -> bool:
    if status_code == 401:
        return True

    if status_code == 403:
        return False

    haystack = body_text.lower()
    if any(phrase in haystack for phrase in _INVALID_CREDENTIAL_PHRASES):
        return True

    if not body_json:
        return False

    for field in ("message", "error", "detail", "description", "title"):
        value = body_json.get(field)
        if isinstance(value, str) and any(phrase in value.lower() for phrase in _INVALID_CREDENTIAL_PHRASES):
            return True

    detail = body_json.get("detail")
    if isinstance(detail, list):
        for item in detail:
            if isinstance(item, dict):
                msg = item.get("msg")
                if isinstance(msg, str) and any(phrase in msg.lower() for phrase in _INVALID_CREDENTIAL_PHRASES):
                    return True

    return False


def _hiddenlayer_http_error(response: httpx.Response) -> HiddenLayerApiError:
    body_text = response.text or ""
    body_json = _parse_error_body(response)

    if _body_indicates_invalid_credentials(response.status_code, body_text, body_json):
        return HiddenLayerApiError(INVALID_CREDENTIALS_MESSAGE, status_code=401)

    if response.status_code == 403:
        return HiddenLayerApiError(FORBIDDEN_MESSAGE, status_code=403)

    if body_json:
        for field in ("message", "detail", "error", "description", "title"):
            value = body_json.get(field)
            if isinstance(value, str) and value.strip():
                return HiddenLayerApiError(
                    f"HiddenLayer API error: {value.strip()}",
                    status_code=502 if response.status_code >= 500 else response.status_code,
                )

    if body_text.strip():
        return HiddenLayerApiError(
            f"HiddenLayer API returned {response.status_code}: {body_text.strip()}",
            status_code=502 if response.status_code >= 500 else response.status_code,
        )

    return HiddenLayerApiError(
        f"HiddenLayer API returned {response.status_code}",
        status_code=502 if response.status_code >= 500 else response.status_code,
    )


def build_interactions_payload(
    *,
    metadata: dict[str, Any],
    input_messages: Optional[list[dict[str, str]]] = None,
    output_messages: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"metadata": metadata}
    if input_messages:
        payload["input"] = {"messages": input_messages}
    if output_messages:
        payload["output"] = {"messages": output_messages}
    return payload


def call_hiddenlayer_interactions(
    payload: dict[str, Any],
    config: Optional[dict[str, Any]],
    *,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    api_base = resolve_api_base(config)
    timeout = resolve_timeout(config)
    project_id = resolve_project_id(config)
    url = f"{api_base}{INTERACTIONS_PATH}"

    headers = {
        "Authorization": f"Bearer {get_access_token(config)}",
        "Content-Type": "application/json",
    }
    if project_id:
        headers["HL-Project-Id"] = project_id
    if session_id:
        headers["HL-Runtime-Session-Id"] = session_id

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload, headers=headers)
            if response.status_code == 401:
                _invalidate_token_cache()
                headers["Authorization"] = f"Bearer {get_access_token(config, force_refresh=True)}"
                response = client.post(url, json=payload, headers=headers)
            elif response.status_code == 503:
                logger.warning("HiddenLayer API returned 503; retrying once")
                response = client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        logger.error("HiddenLayer API request failed: %s", exc)
        raise HiddenLayerApiError(f"Failed to connect to HiddenLayer API: {exc}") from exc

    if not response.is_success:
        logger.error(
            "HiddenLayer API error %s: %s",
            response.status_code,
            _safe_response_snippet(response),
        )
        raise _hiddenlayer_http_error(response)

    try:
        body = response.json()
    except ValueError as exc:
        raise HiddenLayerApiError(f"Invalid JSON from HiddenLayer API: {exc}") from exc

    if not isinstance(body, dict):
        raise HiddenLayerApiError("HiddenLayer API response was not a JSON object")
    return body


def format_detection_message(response: dict[str, Any]) -> str:
    evaluation = response.get("evaluation") or {}
    action = _normalize_action(evaluation.get("action") or "Block")
    threat_level = str(evaluation.get("threat_level") or "Unknown")

    detected_analyzers: list[str] = []
    for analyzer in response.get("analysis") or []:
        if not isinstance(analyzer, dict) or not analyzer.get("detected"):
            continue
        name = analyzer.get("name", "detection")
        phase = analyzer.get("phase")
        suffix = f" ({phase})" if phase else ""
        detected_analyzers.append(f"{name}{suffix}")

    detail = "; ".join(detected_analyzers) if detected_analyzers else "policy violation"
    return f"HiddenLayer guardrail {action.lower()}: {threat_level} threat — {detail}"


def map_validate_response(response: dict[str, Any]) -> tuple[bool, Optional[str]]:
    evaluation = _parse_evaluation(response)
    if evaluation is None:
        logger.error("HiddenLayer interactions response missing evaluation object")
        return False, MISSING_EVALUATION_MESSAGE

    action = _normalize_action(evaluation.get("action") or "Allow")

    if action in ALLOW_ACTIONS:
        return True, None
    if action in VALIDATE_DENY_ACTIONS:
        return False, format_detection_message(response)

    logger.warning("Unknown HiddenLayer evaluation.action '%s'; treating as allow", action)
    return True, None


def map_redact_response(
    response: dict[str, Any],
    *,
    original_body: dict[str, Any],
    modified_body: dict[str, Any],
) -> tuple[bool, bool, dict[str, Any], Optional[str]]:
    evaluation = _parse_evaluation(response)
    if evaluation is None:
        logger.error("HiddenLayer interactions response missing evaluation object")
        return True, False, original_body, None

    action = _normalize_action(evaluation.get("action") or "Allow")

    if action in ALLOW_ACTIONS:
        return True, False, original_body, None
    if action in REDACT_ACTIONS:
        if bodies_differ(original_body, modified_body):
            return True, True, modified_body, None
        return True, False, original_body, None
    if action in BLOCK_ACTIONS:
        block_result = modified_body if bodies_differ(original_body, modified_body) else original_body
        return False, False, block_result, format_detection_message(response)

    logger.warning("Unknown HiddenLayer evaluation.action '%s'; passing through unchanged", action)
    return True, False, original_body, None


def handle_hiddenlayer_error(exc: Exception) -> None:
    if isinstance(exc, HiddenLayerApiError):
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if isinstance(exc, HTTPException):
        raise
    logger.exception("Unexpected HiddenLayer guardrail error")
    raise HTTPException(status_code=500, detail=f"HiddenLayer guardrail error: {exc}") from exc
